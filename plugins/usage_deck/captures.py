"""Captured Anthropic unified rate-limit headers (DESIGN.md §9, amended by §12).

Anthropic subscription (OAuth) responses carry ``anthropic-ratelimit-unified-*`` headers describing
the 5-hour session and 7-day weekly windows. UsageDeck never makes a request to obtain them (no
inference call, no spend). Its optional, opt-in agent observer (``observer.py``, DESIGN.md §12.1)
receives them from Hermes' in-process ``provider_ratelimit_headers`` hook after an *ordinary* request
and persists them here; the dashboard only ever *reads* this file. Without the observer (or without
the opt-in setting) the store is absent and every Anthropic card stays opaque.

File: ``<hermes root>/plugin-data/usage-deck/anthropic-ratelimit.json`` (capture schema **2**, not to
be confused with the HTTP wire schema, which stays 1)::

    {"schema_version": 2,
     "captures": [{"provider": "anthropic",
                   "holder": {"profile": "example", "is_global": false},
                   "credential_id": "<public pool row id>",
                   "observed_at": "2026-11-06T12:00:00Z",
                   "headers": {"anthropic-ratelimit-unified-5h-utilization": "0.12", ...}}]}

An entry is keyed by ``(holder.is_global, holder.profile, provider, credential_id)``: the public pool
row id is only unique inside ONE auth store, so the holder (the store that owns the row) is part of
the key and an entry can never supply another holder's card. Readers look up *scoped* references
``(is_global, profile, credential_id)``; an unscoped row id is never looked up. Files of any other
schema version (notably the unscoped v1) are refused, never migrated: their attribution is unknowable.

Safety: only allow-listed header names with strictly validated values are stored (the same rules as
the Hermes core hook); the store is bounded (256 KiB / 256 entries); writers serialise across
processes with an ``flock`` on a same-directory lock (100 ms maximum wait, then the observation is
dropped), replace the file atomically and never regress a key to an older observation; the store,
its lock and its temporary files are created 0600 in a 0700 directory, and a symlink anywhere below
the Hermes root is refused. Every failure is opaque (no path, header or exception text) and fails
closed for telemetry. A file or entry flagged ``"synthetic": true`` and any observation dated in the
reader's future are refused, so fixtures and clock errors can never render as observations.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

try:  # POSIX advisory locking; absent on Windows -> writes are skipped, never thread-only locked
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 2
MAX_CAPTURE_BYTES = 256 * 1024
MAX_CAPTURES = 256
LOCK_WAIT_S = 0.1
STORE_RELATIVE_PATH = ("plugin-data", "usage-deck", "anthropic-ratelimit.json")
LOCK_NAME = ".anthropic-ratelimit.lock"
GLOBAL_PROFILE = "global"

UNIFIED_PREFIX = "anthropic-ratelimit-unified-"
#: suffix -> value kind. Exactly the twelve names the Hermes core hook publishes (DESIGN.md §12.2).
_SUFFIX_KINDS = {
    "status": "atom", "reset": "reset", "representative-claim": "atom", "fallback-percentage": "number",
    "5h-status": "atom", "5h-reset": "reset", "5h-utilization": "number",
    "7d-status": "atom", "7d-reset": "reset", "7d-utilization": "number",
    "overage-status": "atom", "overage-disabled-reason": "atom",
}
#: Header names this plugin understands. Anything else is dropped.
ALLOWED_HEADERS = frozenset(UNIFIED_PREFIX + suffix for suffix in _SUFFIX_KINDS)
_KIND_BY_HEADER = {UNIFIED_PREFIX + suffix: kind for suffix, kind in _SUFFIX_KINDS.items()}
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_ATOM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SECRET_PREFIXES = ("sk-", "eyJ", "cc-")
_RESET_EPOCH_RANGE = (946684800, 4102444800)  # 2000-01-01 .. 2100-01-01 (``parse_reset_epoch``)
_ENTRY_KEYS = frozenset({"provider", "holder", "credential_id", "observed_at", "headers"})
MAX_VALUE_LEN = 64
_FUTURE_TOLERANCE = timedelta(seconds=1)

#: ``(is_global, profile, credential_id)``: a scoped reference to one row of one auth store.
CredentialRef = Tuple[bool, str, str]


class CaptureError(ValueError):
    """A capture or capture file is not valid / could not be written (never carries file content)."""


@dataclass(frozen=True)
class Capture:
    provider: str
    holder_is_global: bool
    holder_profile: str
    credential_id: str
    observed_at: datetime  # UTC, time the response headers were received
    headers: Mapping[str, str]  # allow-listed, validated, lower-case names
    synthetic: bool = False

    @property
    def ref(self) -> CredentialRef:
        return (self.holder_is_global, self.holder_profile, self.credential_id)

    @property
    def key(self) -> Tuple[bool, str, str, str]:
        return (self.holder_is_global, self.holder_profile, self.provider, self.credential_id)


def default_store_path(root: Path) -> Path:
    return Path(root).joinpath(*STORE_RELATIVE_PATH)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ------------------------------------------------------------------ validation


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise CaptureError("bad observed_at")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise CaptureError("bad observed_at") from None


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_value(name: str, value: Any) -> Optional[str]:
    """Strictly validated header value, or None (the core hook's rules, DESIGN.md §12.2)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_VALUE_LEN or any(not 0x20 <= ord(ch) <= 0x7E for ch in value):
        return None
    kind = _KIND_BY_HEADER[name]
    if kind == "atom":
        return value if _ATOM_RE.match(value) and not value.startswith(_SECRET_PREFIXES) else None
    if not _NUMBER_RE.match(value):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    if kind == "reset" and not _RESET_EPOCH_RANGE[0] <= number <= _RESET_EPOCH_RANGE[1]:
        return None
    return value


def sanitize_headers(raw: Any) -> Dict[str, str]:
    """Allow-listed, validated copy of ``raw``: unknown names and bad values are dropped one by one."""
    try:
        items = list(raw.items())
    except (AttributeError, TypeError):
        return {}
    clean: Dict[str, str] = {}
    for name, value in items:
        lowered = name.lower() if isinstance(name, str) else ""
        if lowered not in ALLOWED_HEADERS:
            continue
        cleaned = _clean_value(lowered, value)
        if cleaned is not None:
            clean[lowered] = cleaned
        else:
            clean.pop(lowered, None)  # a later bad duplicate never leaves an earlier value standing
    return clean


def _stored_headers(raw: Any) -> Dict[str, str]:
    """Headers of a stored entry: unknown names dropped, an invalid allow-listed value refuses the entry."""
    if not isinstance(raw, dict):
        raise CaptureError("headers must be an object")
    out: Dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or name not in ALLOWED_HEADERS:
            continue  # unknown names are never stored, never trusted
        cleaned = _clean_value(name, value)
        if cleaned is None or cleaned != value:
            raise CaptureError("bad header value")
        out[name] = cleaned
    return out


def _validate_holder(is_global: Any, profile: Any) -> Tuple[bool, str]:
    if not isinstance(is_global, bool) or not isinstance(profile, str) or not _PROFILE_RE.match(profile):
        raise CaptureError("bad holder")
    if is_global and profile != GLOBAL_PROFILE:
        raise CaptureError("bad holder")
    return is_global, profile


def _validate_identity(provider: Any, credential_id: Any) -> None:
    if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
        raise CaptureError("bad provider")
    if not isinstance(credential_id, str) or not _ID_RE.match(credential_id) \
            or credential_id.startswith(("eyJ", "sk-")):
        raise CaptureError("bad credential_id")


def _parse_entry(entry: Any, file_synthetic: bool) -> Capture:
    if not isinstance(entry, dict):
        raise CaptureError("capture must be an object")
    synthetic = entry.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise CaptureError("bad synthetic flag")
    if set(entry) - _ENTRY_KEYS - {"synthetic"}:
        raise CaptureError("unexpected capture field")
    holder = entry.get("holder")
    if not isinstance(holder, dict) or set(holder) != {"profile", "is_global"}:
        raise CaptureError("bad holder")
    is_global, profile = _validate_holder(holder.get("is_global"), holder.get("profile"))
    provider, credential_id = entry.get("provider"), entry.get("credential_id")
    _validate_identity(provider, credential_id)
    return Capture(provider, is_global, profile, credential_id, _parse_ts(entry.get("observed_at")),
                   _stored_headers(entry.get("headers")), synthetic or file_synthetic)


def _parse_store(data: Any, *, strict: bool) -> List[Capture]:
    """Entries of a decoded store. ``strict`` (writer): ANY invalid entry or unexpected shape raises."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise CaptureError("unexpected schema")
    synthetic = data.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise CaptureError("bad synthetic flag")
    entries = data.get("captures")
    if not isinstance(entries, list) or len(entries) > MAX_CAPTURES:
        raise CaptureError("bad captures")
    found: List[Capture] = []
    for entry in entries:
        try:
            capture = _parse_entry(entry, synthetic)
        except CaptureError:
            if strict:
                raise
            continue  # one bad entry never hides the others from a reader
        if strict and (capture.synthetic or not capture.headers):
            raise CaptureError("unexpected capture")
        found.append(capture)
    return found


# -------------------------------------------------------- confined file access


def _split_store_path(path: Path, *, canonical: bool = True) -> Tuple[Path, Tuple[str, ...]]:
    """``(root, components below root)``. Writers require the canonical place below a root
    (``<root>/plugin-data/usage-deck/anthropic-ratelimit.json``); a reader of any other path (test
    fixtures; the dashboard itself always passes the canonical path) still refuses a symlinked file."""
    path = Path(path)
    parts = path.parts
    n = len(STORE_RELATIVE_PATH)
    if len(parts) > n and tuple(parts[-n:]) == STORE_RELATIVE_PATH:
        return Path(*parts[:-n]), STORE_RELATIVE_PATH
    if canonical or len(parts) < 2:
        raise CaptureError("unsafe path")
    return path.parent, (path.name,)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
#: ``openat``-style confinement needs dir_fd + O_NOFOLLOW (POSIX). Elsewhere writes are skipped.
_CONFINED_IO = bool(_NOFOLLOW and _DIRECTORY and os.open in os.supports_dir_fd
                    and os.mkdir in os.supports_dir_fd and os.rename in os.supports_dir_fd
                    and os.unlink in os.supports_dir_fd)


def _open_dir_chain(root: Path, names: Iterable[str], *, create: bool) -> int:
    """Descend ``names`` below ``root`` with ``O_NOFOLLOW`` at every step (no symlink may be traversed).

    ``root`` itself is the host-resolved Hermes root (opened as given). Returns an fd the caller must
    close. ``create`` makes missing directories 0700.
    """
    try:
        fd = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    except OSError:
        raise CaptureError("unsafe path") from None
    try:
        for name in names:
            try:
                child = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise CaptureError("absent") from None
                try:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass  # a concurrent writer created it first; the O_NOFOLLOW open below still vets it
                    child = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
                except OSError:
                    raise CaptureError("unsafe path") from None
            except OSError:
                raise CaptureError("unsafe path") from None  # symlink / not a directory
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_bounded(fd: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE_BYTES:
        raise CaptureError("store unusable")
    with os.fdopen(os.dup(fd), "rb") as handle:
        data = handle.read(MAX_CAPTURE_BYTES + 1)
    if len(data) > MAX_CAPTURE_BYTES:
        raise CaptureError("store unusable")
    return data


def _read_store_bytes(path: Path) -> Optional[bytes]:
    """Bytes of the store, ``None`` when it does not exist; CaptureError when unusable / unsafe."""
    root, names = _split_store_path(path, canonical=False)
    if _CONFINED_IO:
        try:
            dir_fd = _open_dir_chain(root, names[:-1], create=False)
        except CaptureError as exc:
            if str(exc) == "absent":
                return None
            raise
        try:
            try:
                fd = os.open(names[-1], os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError:
                raise CaptureError("unsafe path") from None
            try:
                return _read_bounded(fd)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
    # Non-POSIX readers: lstat every component below the root, then read (best effort).
    current = root
    for name in names:
        current = current / name
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise CaptureError("unsafe path") from None
        if stat.S_ISLNK(info.st_mode):
            raise CaptureError("unsafe path")
    try:
        if current.stat().st_size > MAX_CAPTURE_BYTES:
            raise CaptureError("store unusable")
        return current.read_bytes()
    except OSError:
        raise CaptureError("store unusable") from None


# ---------------------------------------------------------------------- reader


class CaptureStore:
    """Read-only view of the capture file; every failure degrades to "no capture"."""

    def __init__(self, path: Optional[Path], allow_synthetic: bool = False, clock=None):
        self._path = Path(path) if path is not None else None
        self._allow_synthetic = allow_synthetic
        self._clock = clock or _utc_now
        self._loaded: Optional[Dict[Tuple[bool, str, str, str], Capture]] = None
        self._lock = threading.Lock()  # one snapshot, loaded once, shared by concurrent card workers

    def _load(self) -> Dict[Tuple[bool, str, str, str], Capture]:
        with self._lock:
            if self._loaded is None:
                self._loaded = self._read_snapshot()
            return self._loaded

    def _read_snapshot(self) -> Dict[Tuple[bool, str, str, str], Capture]:
        found: Dict[Tuple[bool, str, str, str], Capture] = {}
        if self._path is None:
            return found
        try:
            raw = _read_store_bytes(self._path)
            if raw is None:
                return found
            captures = _parse_store(json.loads(raw.decode("utf-8")), strict=False)
        except (CaptureError, OSError, ValueError, UnicodeDecodeError, RecursionError):
            return found  # opaque: unreadable, oversize, symlinked, malformed or the wrong schema
        for capture in captures:
            if capture.synthetic and not self._allow_synthetic:
                continue
            if not capture.headers:
                continue
            previous = found.get(capture.key)
            if previous is None or capture.observed_at >= previous.observed_at:
                found[capture.key] = capture
        return found

    def latest(self, provider: str, credential_refs, now: Optional[datetime] = None) -> Optional[Capture]:
        """Newest capture for ``provider`` among the given SCOPED references.

        Each reference is ``(is_global, profile, credential_id)``. Anything else (notably a bare row id)
        matches nothing: an unscoped public id is never looked up. Observations dated after ``now``
        (default: the reader's clock) are refused.
        """
        store = self._load()
        limit = (now if now is not None else self._clock()) + _FUTURE_TOLERANCE
        best: Optional[Capture] = None
        for ref in credential_refs or ():
            if not (isinstance(ref, tuple) and len(ref) == 3 and isinstance(ref[0], bool)
                    and isinstance(ref[1], str) and isinstance(ref[2], str)):
                continue
            capture = store.get((ref[0], ref[1], provider, ref[2]))
            if capture is None or capture.observed_at > limit:
                continue
            if best is None or (capture.observed_at, capture.key) > (best.observed_at, best.key):
                best = capture
        return best


# ---------------------------------------------------------------------- writer


def _entry_dict(capture: Capture) -> Dict[str, Any]:
    return {
        "provider": capture.provider,
        "holder": {"profile": capture.holder_profile, "is_global": capture.holder_is_global},
        "credential_id": capture.credential_id,
        "observed_at": _format_ts(capture.observed_at),
        "headers": dict(sorted(capture.headers.items())),
    }


def _serialize(captures: List[Capture]) -> bytes:
    ordered = sorted(captures, key=lambda c: c.key)
    doc = {"schema_version": SCHEMA_VERSION, "captures": [_entry_dict(c) for c in ordered]}
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _evict(captures: List[Capture]) -> List[Capture]:
    """Keep the newest observations within 256 entries / 256 KiB; ties break on the key tuple (deterministic)."""
    newest_first = sorted(captures, key=lambda c: (c.observed_at, c.key), reverse=True)
    kept = newest_first[:MAX_CAPTURES]
    while kept and len(_serialize(kept)) > MAX_CAPTURE_BYTES:
        kept.pop()  # drop the oldest
    return kept


@contextlib.contextmanager
def _locked(dir_fd: int):
    """Exclusive cross-process lock on the same-directory lock file; ``CaptureError`` after 100 ms."""
    if fcntl is None:
        raise CaptureError("locking unavailable")
    try:
        fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
    except OSError:
        raise CaptureError("unsafe path") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CaptureError("unsafe path")
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CaptureError("lock timeout") from None
                time.sleep(0.002)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _replace_atomically(dir_fd: int, name: str, payload: bytes) -> None:
    tmp = f".capture-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
    except OSError:
        raise CaptureError("write failed") from None
    try:
        try:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)  # POSIX rename(2) replaces atomically
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def record_headers(path: Path, provider: str, credential_id: str, headers: Mapping[str, str],
                   observed_at: datetime, *, holder: Tuple[bool, str], clock=None) -> Optional[Capture]:
    """Record one response's headers (called by the opt-in observer only; never by the dashboard API).

    ``holder`` is ``(is_global, profile)`` of the auth store that owns row ``credential_id`` and is
    mandatory. Only allow-listed, validated headers are kept. Returns the stored ``Capture``, or ``None``
    when an equal-or-newer observation of the same scoped key is already stored (never regresses).
    Raises ``CaptureError`` (fixed, path-free message) for invalid input, a future timestamp, an unsafe
    path (symlink below the root, wrong layout), lock timeout, an existing store that is malformed,
    oversize or of another schema (it is left untouched), or any filesystem failure.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise CaptureError("naive observed_at")
    if not (isinstance(holder, tuple) and len(holder) == 2):
        raise CaptureError("bad holder")
    is_global, profile = _validate_holder(*holder)
    _validate_identity(provider, credential_id)
    stamp = observed_at.astimezone(timezone.utc).replace(microsecond=0)
    clean = sanitize_headers(headers)
    if not clean:
        raise CaptureError("no unified rate-limit headers")
    now = (clock or _utc_now)()
    if stamp > now + _FUTURE_TOLERANCE:
        raise CaptureError("future observed_at")
    new = Capture(provider, is_global, profile, credential_id, stamp, clean)

    if not _CONFINED_IO or fcntl is None:
        raise CaptureError("locking unavailable")
    root, names = _split_store_path(Path(path))
    dir_fd = _open_dir_chain(root, names[:-1], create=True)
    try:
        with _locked(dir_fd):
            try:
                fd = os.open(names[-1], os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
            except FileNotFoundError:
                existing: List[Capture] = []
            except OSError:
                raise CaptureError("unsafe path") from None
            else:
                try:
                    raw = _read_bounded(fd)
                finally:
                    os.close(fd)
                try:
                    existing = _parse_store(json.loads(raw.decode("utf-8")), strict=True)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise CaptureError("store malformed") from None
            by_key: Dict[Tuple[bool, str, str, str], Capture] = {}
            horizon = now + _FUTURE_TOLERANCE
            for entry in existing:
                if entry.observed_at > horizon:
                    continue  # a fabricated future observation never blocks a real one
                previous = by_key.get(entry.key)
                if previous is None or entry.observed_at >= previous.observed_at:
                    by_key[entry.key] = entry
            held = by_key.get(new.key)
            if held is not None and held.observed_at >= new.observed_at:
                return None  # out-of-order or duplicate delivery: never regress
            by_key[new.key] = new
            kept = _evict(list(by_key.values()))
            _replace_atomically(dir_fd, names[-1], _serialize(kept))
            return new if new.key in {c.key for c in kept} else None
    except OSError:
        raise CaptureError("write failed") from None
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------- header parsing


def parse_utilization(value: Optional[str]) -> Optional[float]:
    """Utilization fraction (``0.18`` = 18 %); finite and >= 0 else None. May exceed 1.0."""
    if not isinstance(value, str):
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_reset_epoch(value: Optional[str]) -> Optional[datetime]:
    """Reset instant from an epoch-seconds header; None when absent or implausible."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or not (946684800 <= seconds <= 4102444800):  # 2000..2100
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
