"""Captured Anthropic unified rate-limit headers (DESIGN.md §9, recon t_7dce2222).

Anthropic subscription (OAuth) responses carry ``anthropic-ratelimit-unified-*``
headers describing the 5-hour session and 7-day weekly windows. UsageDeck never
makes a request to obtain them (no inference call, no spend): it only *reads* a
capture file that some other, separately authorised component recorded after a
real call. Nothing in Hermes records these headers today (recon: the existing
seams parse ``x-ratelimit-*`` and the per-bucket ``anthropic-ratelimit-
requests/tokens-reset`` headers only), so on a stock install this store is
absent and every Anthropic card stays opaque. ``record_headers`` is the seam a
future capture hook calls; this plugin itself never calls it.

File: ``<hermes root>/plugin-data/usage-deck/anthropic-ratelimit.json``::

    {"schema_version": 1,
     "captures": [{"provider": "anthropic", "credential_id": "<public pool row id>",
                   "observed_at": "2026-11-06T12:00:00Z",
                   "headers": {"anthropic-ratelimit-unified-5h-utilization": "0.12", ...}}]}

A capture is keyed by the credential's public pool row ``id`` (never a token,
email or fingerprint) and holds only allow-listed header names with short
values. ``observed_at`` is the time of the call that returned the headers; it is
the freshness shown on the card and is never replaced by the read time.
A file (or capture) flagged ``"synthetic": true`` is refused unless the caller
opts in, so test fixtures cannot be mistaken for real observations.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 256 * 1024
MAX_CAPTURES = 256
STORE_RELATIVE_PATH = ("plugin-data", "usage-deck", "anthropic-ratelimit.json")

UNIFIED_PREFIX = "anthropic-ratelimit-unified-"
#: Header names this plugin understands (recon t_7dce2222). Anything else is dropped.
ALLOWED_HEADERS = frozenset(UNIFIED_PREFIX + suffix for suffix in (
    "status", "reset", "representative-claim", "fallback-percentage",
    "5h-status", "5h-reset", "5h-utilization",
    "7d-status", "7d-reset", "7d-utilization",
    "overage-status", "overage-disabled-reason",
))
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_VALUE_LEN = 64


class CaptureError(ValueError):
    """A capture or capture file is not valid (never carries file content)."""


@dataclass(frozen=True)
class Capture:
    provider: str
    credential_id: str
    observed_at: datetime  # UTC, time of the call that returned the headers
    headers: Mapping[str, str]  # allow-listed, lower-case names
    synthetic: bool = False


def default_store_path(root: Path) -> Path:
    return Path(root).joinpath(*STORE_RELATIVE_PATH)


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise CaptureError("bad observed_at")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _clean_headers(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, Mapping):
        raise CaptureError("headers must be an object")
    out: Dict[str, str] = {}
    for name, value in raw.items():
        lowered = str(name).lower()
        if lowered not in ALLOWED_HEADERS:
            continue
        if not isinstance(value, str) or len(value) > MAX_VALUE_LEN or any(ord(c) < 32 for c in value):
            raise CaptureError("bad header value")
        out[lowered] = value.strip()
    return out


def _parse_capture(entry: Any, file_synthetic: bool) -> Capture:
    if not isinstance(entry, dict):
        raise CaptureError("capture must be an object")
    provider = entry.get("provider")
    credential_id = entry.get("credential_id")
    if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
        raise CaptureError("bad provider")
    if not isinstance(credential_id, str) or not _ID_RE.match(credential_id) \
            or credential_id.startswith(("eyJ", "sk-")):
        raise CaptureError("bad credential_id")
    synthetic = entry.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise CaptureError("bad synthetic flag")
    return Capture(provider, credential_id, _parse_ts(entry.get("observed_at")),
                   _clean_headers(entry.get("headers")), synthetic or file_synthetic)


class CaptureStore:
    """Read-only view of the capture file; every failure degrades to "no capture"."""

    def __init__(self, path: Optional[Path], allow_synthetic: bool = False):
        self._path = Path(path) if path is not None else None
        self._allow_synthetic = allow_synthetic
        self._loaded: Optional[Dict[Tuple[str, str], Capture]] = None

    def _load(self) -> Dict[Tuple[str, str], Capture]:
        if self._loaded is not None:
            return self._loaded
        found: Dict[Tuple[str, str], Capture] = {}
        self._loaded = found
        path = self._path
        try:
            if path is None or not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
                return found
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return found
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return found
        file_synthetic = data.get("synthetic", False) is True
        entries = data.get("captures")
        if not isinstance(entries, list):
            return found
        for entry in entries[:MAX_CAPTURES]:
            try:
                capture = _parse_capture(entry, file_synthetic)
            except CaptureError:
                continue  # one bad entry never hides the others
            if capture.synthetic and not self._allow_synthetic:
                continue
            key = (capture.provider, capture.credential_id)
            previous = found.get(key)
            if previous is None or capture.observed_at >= previous.observed_at:
                found[key] = capture
        return found

    def latest(self, provider: str, credential_ids) -> Optional[Capture]:
        """Newest capture for ``provider`` among the given public credential ids."""
        store = self._load()
        best: Optional[Capture] = None
        for credential_id in credential_ids:
            capture = store.get((provider, credential_id))
            if capture is not None and (best is None or capture.observed_at > best.observed_at):
                best = capture
        return best


# --------------------------------------------------------------- producer seam


def record_headers(path: Path, provider: str, credential_id: str, headers: Mapping[str, str],
                   observed_at: datetime) -> Capture:
    """Record one call's headers (the seam for a future capture hook; unused by the plugin).

    Keeps only allow-listed headers, replaces any earlier capture of the same
    credential and writes atomically. Raises ``CaptureError`` on invalid input
    or when the headers carry no unified rate-limit data.
    """
    if observed_at.tzinfo is None:
        raise CaptureError("naive observed_at")
    stamp = observed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    capture = _parse_capture({"provider": provider, "credential_id": credential_id,
                              "observed_at": stamp, "headers": dict(headers)}, False)
    if not capture.headers:
        raise CaptureError("no unified rate-limit headers")
    path = Path(path)
    existing: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        for entry in current.get("captures", []):
            existing[(entry["provider"], entry["credential_id"])] = entry
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        existing = {}
    existing[(provider, credential_id)] = {
        "provider": provider, "credential_id": credential_id, "observed_at": stamp,
        "headers": dict(capture.headers),
    }
    entries = sorted(existing.values(), key=lambda e: (e["provider"], e["credential_id"]))[:MAX_CAPTURES]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".capture-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": SCHEMA_VERSION, "captures": entries}, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return capture


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
