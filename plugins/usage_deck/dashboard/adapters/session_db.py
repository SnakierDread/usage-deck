"""Read-only session observations from Hermes ``state.db`` files (design: analytics-dimensions-breakdown §5/§6).

One ``SessionDbReader.read_home`` call reads ONE home's ``<home>/state.db`` with SQLite ``mode=ro`` (never a
``SessionDB`` constructor, a migration, a checkpoint or a writable fallback) and returns the baseline daily
``sessions`` counters grouped by recorded model/provider columns. Columns are discovered by schema
introspection: an absent column is an unavailable value (``None``), never zero and never inferred.

Nothing here touches a network, a credential or a home other than the one it is handed. Model/provider text
is untrusted: anything that is not a short printable identifier, or that looks like a secret, is dropped
(model -> null, provider -> ``unknown``) instead of being published.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import quote

from ..models.redaction import contains_secret
from .analytics import _count, _usd

DB_NAME = "state.db"
MODELS_JSON = "models.json"
MAX_MODELS_JSON_BYTES = 1024 * 1024
MAX_MODEL_ENTRIES = 20000
BUSY_TIMEOUT_S = 2.0
UNKNOWN = "unknown"
BARE_BILLING = frozenset({"auto", "custom"})

STATE_OK, STATE_ABSENT, STATE_UNAVAILABLE, STATE_EXCLUDED = "ok", "absent", "unavailable", "excluded"
METRIC_NAMES = ("api_calls", "estimated_usd", "input_count", "output_count", "sessions")

_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")
_MODEL_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,128}$")  # printable ASCII, no space, no quote, no backslash


# ------------------------------------------------------------------ metrics


@dataclass(frozen=True)
class Metrics:
    """One aggregate of the baseline daily counters. ``None`` = unavailable (never a computed zero)."""

    input_count: Optional[int] = None
    output_count: Optional[int] = None
    estimated_usd: Optional[float] = None
    sessions: Optional[int] = None
    api_calls: Optional[int] = None
    missing: FrozenSet[str] = frozenset()


def sum_known(values: Iterable[Any]) -> Any:
    """Sum of the known (non-None) values; None when no value is known (never 0). Floats use ``fsum``."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    if any(isinstance(v, float) for v in known):
        return math.fsum(known)  # exactly rounded: independent of grouping and order
    return sum(known)


_sum_known = sum_known


def sum_metrics(items: Iterable[Metrics]) -> Metrics:
    """Field-wise sum of the known values; a field with no known value stays ``None`` (never 0)."""
    items = list(items)
    return Metrics(
        input_count=_sum_known(m.input_count for m in items),
        output_count=_sum_known(m.output_count for m in items),
        estimated_usd=_sum_known(m.estimated_usd for m in items),
        sessions=_sum_known(m.sessions for m in items),
        api_calls=_sum_known(m.api_calls for m in items),
        missing=frozenset().union(*(m.missing for m in items)) if items else frozenset(),
    )


def known_tokens(m: Metrics) -> Optional[int]:
    """Known input+output total (ranking only): None when neither component is known."""
    if m.input_count is None and m.output_count is None:
        return None
    return (m.input_count or 0) + (m.output_count or 0)


# ------------------------------------------------------------- identifiers


def safe_model_id(raw: Any) -> Optional[str]:
    """A recorded model id that is safe to publish verbatim, else None."""
    if not isinstance(raw, str) or not _MODEL_RE.match(raw) or contains_secret(raw):
        return None
    return raw


def normalize_provider(raw: Any) -> Tuple[Optional[str], bool]:
    """``(provider id, unsafe)``: blank -> (None, False); unpublishable text -> (None, True)."""
    if raw is None:
        return None, False
    if not isinstance(raw, str):
        return None, True
    text = raw.strip()
    if not text:
        return None, False
    if not _PROVIDER_RE.match(text) or contains_secret(text):
        return None, True
    if text.lower().startswith("custom:"):
        name = text[7:]
        return ("custom:" + name) if name else "custom", False
    return text.lower(), False


def attribute(model: Optional[str], provider: Any, billing: Any,
              models: Dict[str, Optional[str]]) -> Tuple[str, str]:
    """``(provider_id, attribution_source)`` by the §5 precedence. Never guesses from a model prefix."""
    recorded, bad = normalize_provider(provider)
    if bad:
        return UNKNOWN, "unknown"
    if recorded and recorded not in ("auto", UNKNOWN):
        return recorded, "session_provider"
    bucket, bad = normalize_provider(billing)
    if bad:
        return UNKNOWN, "unknown"
    if bucket and bucket not in BARE_BILLING and bucket != UNKNOWN:
        return bucket, "billing_provider"
    if model is not None:
        mapped = models.get(model)
        if mapped:
            return mapped, "models_json"
    return UNKNOWN, "unknown"


def provider_matches(provider_id: str, wanted: str) -> bool:
    """Filter semantics: ``custom`` rolls up generic ``custom`` and every ``custom:*``; all else exact."""
    if wanted == "custom":
        return provider_id == "custom" or provider_id.startswith("custom:")
    return provider_id == wanted


# ------------------------------------------------------------- models.json


def _clean_text(value: Any, limit: int = 256) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def load_models_map(directory: Path) -> Dict[str, Optional[str]]:
    """The owning home's optional ``models.json`` as ``{model id: provider id}`` (None = ambiguous).

    ``{"models": [{"id": ..., "provider": ...}]}`` only; regular non-symlink UTF-8 file of at most 1 MiB.
    Anything missing/oversized/malformed disables the fallback for this home (empty map). Never created,
    never fetched, never read from another home.
    """
    path = Path(directory) / MODELS_JSON
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MODELS_JSON_BYTES:
            return {}
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MODELS_JSON_BYTES + 1)
        if len(raw) > MAX_MODELS_JSON_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(data, dict) or set(data) != {"models"} or not isinstance(data["models"], list) \
            or len(data["models"]) > MAX_MODEL_ENTRIES:
        return {}
    mapping: Dict[str, Optional[str]] = {}
    for entry in data["models"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "provider"}:
            return {}
        model_id = _clean_text(entry["id"])
        provider = _clean_text(entry["provider"])
        if model_id is None or provider is None:
            return {}
    for entry in data["models"]:
        model_id = _clean_text(entry["id"])
        provider, bad = normalize_provider(_clean_text(entry["provider"]))
        if model_id is None:
            continue
        if bad or provider is None or provider in BARE_BILLING or provider == UNKNOWN:
            continue  # not a usable target (bare ``custom`` must be spelled ``custom:<name>``)
        if safe_model_id(model_id) is None:
            continue
        if model_id in mapping and mapping[model_id] != provider:
            mapping[model_id] = None  # conflicting rows: ambiguous -> Unknown
        elif model_id not in mapping:
            mapping[model_id] = provider
    return mapping


def catalog_identity(directory: Path) -> Optional[Tuple[int, int]]:
    """``(mtime_ns, size)`` of the home's ``models.json`` (cache identity), None when absent."""
    try:
        info = os.lstat(Path(directory) / MODELS_JSON)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


# ------------------------------------------------------------- DB reading


@dataclass(frozen=True)
class ObsGroup:
    """Baseline daily counters of ONE ``(day, recorded model, provider, billing_provider)`` SQL group."""

    day: str
    model: Any
    provider: Any
    billing: Any
    metrics: Metrics


@dataclass(frozen=True)
class HomeRead:
    profile: str
    is_global: bool
    state: str
    groups: Tuple[ObsGroup, ...] = ()
    models: Dict[str, Optional[str]] = field(default_factory=dict)


def _db_uri(path: Path, immutable: bool = False) -> str:
    return "file:" + quote(str(path), safe="/") + ("?mode=ro&immutable=1" if immutable else "?mode=ro")


def _plain_size(path: Path) -> Optional[int]:
    """Size of a regular, non-symlink file; None when it does not exist (OSError/other kinds raise ValueError)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    return info.st_size


def open_uri(path: Path) -> Optional[str]:
    """The strictly read-only URI for ``path``, or None when it cannot be read without writing.

    A WAL-mode database opened even with ``mode=ro`` CREATES its ``-wal``/``-shm`` sidecars when none exist
    (recovery bookkeeping), which is a write to the owning home. So: a rollback-journal database opens plainly
    ``mode=ro``; a WAL database whose sidecars both exist (a live store) opens ``mode=ro`` and sees committed
    WAL frames; a WAL database with NO sidecars (no connection is open, nothing is pending) is read with
    ``immutable=1`` (nothing can be in a WAL that does not exist); any other sidecar state would need
    recovery, which this reader never performs: unavailable.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
        if len(header) < 20 or header[18] != 2:  # byte 18 == 2: write-ahead log format
            return _db_uri(path)
        wal = _plain_size(path.with_name(path.name + "-wal"))
        shm = _plain_size(path.with_name(path.name + "-shm"))
    except (OSError, ValueError):
        return None
    if not wal and shm is None:
        return _db_uri(path, immutable=True)
    if wal is not None and shm is not None:
        return _db_uri(path)
    return None


class SessionDbReader:
    """Reads session counters from state DBs strictly read-only. ``trace`` receives every SQL statement."""

    def __init__(self, trace: Optional[Callable[[str], None]] = None, busy_timeout: float = BUSY_TIMEOUT_S):
        self._trace = trace
        self._busy = busy_timeout

    # -- one home ----------------------------------------------------------------------------------
    def identity(self, directory: Path) -> Tuple[str, Optional[Tuple[int, int]]]:
        """``(state, (dev, ino))`` of ``<directory>/state.db`` without opening it."""
        path = Path(directory) / DB_NAME
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return STATE_ABSENT, None
        except OSError:
            return STATE_UNAVAILABLE, None
        if stat.S_ISLNK(info.st_mode):
            return STATE_EXCLUDED, None
        if not stat.S_ISREG(info.st_mode):
            return STATE_UNAVAILABLE, None
        return STATE_OK, (info.st_dev, info.st_ino)

    def read_groups(self, directory: Path, cutoff: float) -> Optional[Tuple[ObsGroup, ...]]:
        """The grouped counters of ``<directory>/state.db`` newer than ``cutoff``; None = unavailable."""
        path = Path(directory) / DB_NAME
        conn = None
        uri = open_uri(path)
        if uri is None:
            return None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self._busy, isolation_level=None)
            conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
            if self._trace is not None:
                conn.set_trace_callback(self._trace)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "started_at" not in columns:
                return None
            return self._query(conn, columns, cutoff)
        except (sqlite3.Error, OSError, ValueError, UnicodeError, OverflowError):
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _query(conn: sqlite3.Connection, columns: set, cutoff: float) -> Tuple[ObsGroup, ...]:
        def col(name: str) -> str:
            return name if name in columns else "NULL"

        tokens_in, tokens_out = col("input_tokens"), col("output_tokens")
        cost = "COALESCE(SUM(estimated_cost_usd), 0)" if "estimated_cost_usd" in columns else "NULL"
        calls = "SUM(COALESCE(api_call_count, 0))" if "api_call_count" in columns else "NULL"
        sql = (
            "SELECT date(started_at, 'unixepoch', 'localtime'), "
            f"{col('model')}, {col('provider')}, {col('billing_provider')}, "
            f"SUM({tokens_in}), COUNT({tokens_in}), SUM({tokens_out}), COUNT({tokens_out}), "
            f"{cost}, COUNT(*), {calls} "
            "FROM sessions WHERE started_at > ? GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4"
        )
        out: List[ObsGroup] = []
        for day, model, provider, billing, s_in, n_in, s_out, n_out, usd, n, api in conn.execute(sql, (cutoff,)):
            if not isinstance(day, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                continue
            missing = set()
            if "input_tokens" not in columns or (n_in or 0) < n:
                missing.add("input_count")
            if "output_tokens" not in columns or (n_out or 0) < n:
                missing.add("output_count")
            if "estimated_cost_usd" not in columns:
                missing.add("estimated_usd")
            if "api_call_count" not in columns:
                missing.add("api_calls")
            out.append(ObsGroup(day, model, provider, billing, Metrics(
                input_count=_count(s_in), output_count=_count(s_out), estimated_usd=_usd(usd),
                sessions=_count(n), api_calls=_count(api), missing=frozenset(missing))))
        return tuple(out)

    # -- many homes ----------------------------------------------------------------------------------
    def read_homes(self, homes: Iterable[Tuple[str, bool, Path]], cutoff: float) -> List[HomeRead]:
        """Read every home once, in the given order (root first). A physical DB already read for an earlier
        home (hard link / same file) is excluded, never double counted."""
        seen: set = set()
        out: List[HomeRead] = []
        for name, is_global, directory in homes:
            if not is_global and Path(directory).is_symlink():
                state, ident = STATE_EXCLUDED, None  # a symlinked profile home is never followed
            else:
                state, ident = self.identity(directory)
            if state == STATE_OK and ident in seen:
                state = STATE_EXCLUDED
            groups: Tuple[ObsGroup, ...] = ()
            if state == STATE_OK:
                seen.add(ident)
                read = self.read_groups(directory, cutoff)
                if read is None:
                    state = STATE_UNAVAILABLE
                else:
                    groups = read
            models = load_models_map(directory) if state in (STATE_OK, STATE_ABSENT) else {}
            out.append(HomeRead(name, is_global, state, groups, models))
        return out
