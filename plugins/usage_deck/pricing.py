"""API-equivalent pricing: pure resolution plus the catalog/cache manager (design: docs/design/api-equivalent-pricing.md).

"API-equivalent" is a hypothetical *current base text API tariff* applied to recorded input/output token
counts. It is never an invoice, a subscription cost or a claim of actual spend, and it never replaces the
existing ``estimated_usd`` / ``estimated_cost_usd`` / ``cost_source`` fields. This module only annotates at
read time; it never touches a ``state.db``.

Layers (all stdlib, relative imports only so the staged ``_usage_deck_bundle`` package can load it):

* ``resolve_api_equivalent`` - pure: alias -> heuristic -> unknown. No network, disk or clock side effects.
* ``PricingCatalogManager`` - packaged seed + validated on-disk cache + jittered background refresh from
  LiteLLM (primary) and OpenRouter (secondary). A failed fetch never blanks a price already held.
* ``aggregate_resolutions`` - integer pico-USD aggregation with the explicit unpriced ("unknown") bucket.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from .models.redaction import contains_secret

log = logging.getLogger("usage_deck.pricing")
log.addHandler(logging.NullHandler())  # library logger: the host decides where records go

# ------------------------------------------------------------------ constants

SCHEMA_VERSION = 1
PRIMARY = "litellm"
SECONDARY = "openrouter"
SOURCE_IDS = (PRIMARY, SECONDARY)
SOURCE_URLS: Mapping[str, str] = MappingProxyType({
    PRIMARY: "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
    SECONDARY: "https://openrouter.ai/api/v1/models",
})

DATA_DIR_PARTS = ("pricing",)
CACHE_FILE = "catalog-v1.json"
LOCK_FILE = ".catalog.lock"
PACKAGE_DATA = Path(__file__).resolve().parent / "pricing_data"
SEED_FILE = "seed-v1.json"
ALIASES_FILE = "aliases.json"

MAX_FETCH_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_SEED_BYTES = 32 * 1024 * 1024
FETCH_TIMEOUT_S = 5.0
FETCH_ATTEMPTS = 2
RETRY_JITTER_S = (0.5, 1.5)
REFRESH_INTERVAL = timedelta(hours=24)
REFRESH_JITTER = (0.9, 1.1)
FAILURE_RETRY = timedelta(minutes=15)
STALE_AFTER = timedelta(days=7)
FUTURE_SKEW = timedelta(minutes=5)
MAX_NEXT_REFRESH = timedelta(hours=26.5)  # 24h * 1.1 plus slack; anything further out is corrupt
LOCK_WAIT_S = 2.0

MAX_ALIAS_NOTE = 300
MAX_KEY_LEN = 256

WARN_STALE = "pricing_stale"
WARN_REFRESH_FAILED = "pricing_refresh_failed"
WARN_CACHE_INVALID = "pricing_cache_invalid"
WARN_CACHE_UNWRITABLE = "pricing_cache_unwritable"
WARN_ALIASES_INVALID = "pricing_aliases_invalid"
WARN_SEED_INVALID = "pricing_seed_invalid"

REASONS = ("model_unknown", "no_catalog_match", "ambiguous_model", "alias_target_missing",
           "tokens_missing", "tokens_invalid")

PICO = Decimal("1E-12")
_CTX = Context(prec=80)
_MAX_RATE_ADJUSTED = 6      # |rate| < 1e7 USD/token: anything above is a corrupt record, not a price
_MIN_RATE_ADJUSTED = -40


class PricingDataError(ValueError):
    """Packaged/cached data failed validation. The message is a fixed, path-free code."""


class SourceError(PricingDataError):
    """A fetched catalog payload was unusable (parse/schema/empty)."""


class FetchError(Exception):
    """Transport-level failure (HTTP, size, redirect policy, timeout)."""


# ------------------------------------------------------------------ data model


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    source_key: str
    input_usd_per_token: Decimal
    output_usd_per_token: Decimal
    fetched_at: datetime

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_key}"


@dataclass(frozen=True)
class Alias:
    catalog_key: str
    kind: Literal["equivalent", "proxy"]
    note: str


@dataclass(frozen=True)
class CatalogSnapshot:
    entries: Mapping[str, CatalogEntry]
    revision: str
    origin: Literal["seed", "cache", "network"]
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceResolution:
    api_equivalent_usd: Optional[float]
    amount_pico_usd: Optional[int]
    catalog_key: Optional[str]
    method: Literal["alias", "heuristic", "none"]
    alias_kind: Optional[Literal["equivalent", "proxy"]]
    note: Optional[str]
    reason: Optional[str]
    source: Optional[str]
    fetched_at: Optional[datetime]
    stale: bool


@dataclass(frozen=True)
class AggregateResolution:
    """Integer-exact aggregate of leaf resolutions (design §6/§7)."""

    amount_pico_usd: Optional[int]
    api_equivalent_usd: Optional[float]
    priced_rows: int
    unpriced_rows: int
    proxy_rows: int
    partial: bool
    stale: bool
    unknown_row_ids: Tuple[str, ...]


@dataclass(frozen=True)
class SourceMeta:
    fetched_at: datetime
    next_refresh_at: datetime
    payload_sha256: str


# ------------------------------------------------------------------ time / number helpers

_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$")
_RATE_RE = re.compile(r"^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,128}$")  # mirrors adapters.session_db (no import: cycle-free)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PricingDataError("bad timestamp")
    m = _TS_RE.match(value)
    if not m:
        raise PricingDataError("bad timestamp")
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    frac = (m.group(7) or "").ljust(6, "0")
    try:
        return datetime(y, mo, d, h, mi, s, int(frac), tzinfo=timezone.utc)
    except ValueError:
        raise PricingDataError("bad timestamp") from None


def _rate_from(value: Any, *, allow_str: bool) -> Optional[Decimal]:
    """A finite nonnegative per-token rate, or ``None``. Booleans, null, negatives, NaN/inf are all invalid; zero is valid."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, str) and allow_str:
        if not _RATE_RE.match(value.strip()):
            return None
        try:
            d = Decimal(value.strip())
        except Exception:
            return None
    else:
        return None
    if not d.is_finite() or d < 0:
        return None
    if d != 0 and not (_MIN_RATE_ADJUSTED <= d.adjusted() <= _MAX_RATE_ADJUSTED):
        return None
    return d


def _rate_str(d: Decimal) -> str:
    return format(d, "f")


def _strict_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite constant")


def _loads(data: bytes, *, decimals: bool = False) -> Any:
    """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity; ``decimals`` keeps numbers as exact ``Decimal``."""
    kwargs: Dict[str, Any] = {"object_pairs_hook": _strict_pairs, "parse_constant": _reject_constant}
    if decimals:
        kwargs["parse_float"] = Decimal
        kwargs["parse_int"] = Decimal
    return json.loads(data.decode("utf-8"), **kwargs)


# ------------------------------------------------------------------ entries and envelopes


def _entry_wire(e: CatalogEntry) -> Dict[str, str]:
    return {
        "source": e.source,
        "source_key": e.source_key,
        "input_usd_per_token": _rate_str(e.input_usd_per_token),
        "output_usd_per_token": _rate_str(e.output_usd_per_token),
        "fetched_at": format_ts(e.fetched_at),
    }


def compute_revision(entries: Mapping[str, CatalogEntry]) -> str:
    canon = {k: _entry_wire(entries[k]) for k in sorted(entries)}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _clean_key(text: Any) -> Optional[str]:
    if not isinstance(text, str) or not text or len(text) > MAX_KEY_LEN:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None
    return text


def _split_qualified(key: Any) -> Tuple[str, str]:
    """``source:key`` -> (source, key); only the first colon separates."""
    if not isinstance(key, str) or ":" not in key:
        raise PricingDataError("bad catalog key")
    source, source_key = key.split(":", 1)
    if source not in SOURCE_IDS or _clean_key(source_key) is None:
        raise PricingDataError("bad catalog key")
    return source, source_key


def _parse_entry(key: Any, raw: Any, *, now: Optional[datetime]) -> CatalogEntry:
    source, source_key = _split_qualified(key)
    if not isinstance(raw, dict) or set(raw) != {"source", "source_key", "input_usd_per_token",
                                                 "output_usd_per_token", "fetched_at"}:
        raise PricingDataError("bad entry")
    if raw["source"] != source or raw["source_key"] != source_key:
        raise PricingDataError("entry source/key disagreement")
    rin = _rate_from(raw["input_usd_per_token"], allow_str=True)
    rout = _rate_from(raw["output_usd_per_token"], allow_str=True)
    if rin is None or rout is None or not isinstance(raw["input_usd_per_token"], str) \
            or not isinstance(raw["output_usd_per_token"], str):
        raise PricingDataError("bad rate")
    fetched = parse_ts(raw["fetched_at"])
    if now is not None and fetched > _utc(now) + FUTURE_SKEW:
        raise PricingDataError("future timestamp")
    return CatalogEntry(source, source_key, rin, rout, fetched)


def _parse_envelope(data: bytes, *, now: Optional[datetime]) -> Tuple[Dict[str, SourceMeta], Dict[str, CatalogEntry]]:
    """Validate a complete seed/cache envelope; any defect rejects the whole thing."""
    try:
        doc = _loads(data)
    except Exception:
        raise PricingDataError("unparseable") from None
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "sources", "entries"}:
        raise PricingDataError("bad envelope")
    version = doc["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise PricingDataError("unknown schema version")
    sources_raw, entries_raw = doc["sources"], doc["entries"]
    if not isinstance(sources_raw, dict) or not isinstance(entries_raw, dict):
        raise PricingDataError("bad envelope")
    sources: Dict[str, SourceMeta] = {}
    for sid, meta in sources_raw.items():
        if sid not in SOURCE_IDS or not isinstance(meta, dict) \
                or set(meta) != {"fetched_at", "next_refresh_at", "payload_sha256"}:
            raise PricingDataError("bad source")
        sha = meta["payload_sha256"]
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise PricingDataError("bad source")
        fetched = parse_ts(meta["fetched_at"])
        if now is not None and fetched > _utc(now) + FUTURE_SKEW:
            raise PricingDataError("future timestamp")
        sources[sid] = SourceMeta(fetched, parse_ts(meta["next_refresh_at"]), sha)
    entries: Dict[str, CatalogEntry] = {}
    for key, raw in entries_raw.items():
        entry = _parse_entry(key, raw, now=now)
        if entry.source not in sources:
            raise PricingDataError("entry without source")
        entries[key] = entry
    return sources, entries


def _serialize_envelope(sources: Mapping[str, SourceMeta], entries: Mapping[str, CatalogEntry]) -> bytes:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "sources": {sid: {"fetched_at": format_ts(m.fetched_at), "next_refresh_at": format_ts(m.next_refresh_at),
                          "payload_sha256": m.payload_sha256} for sid, m in sorted(sources.items())},
        "entries": {k: _entry_wire(entries[k]) for k in sorted(entries)},
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------------ upstream normalization


def _text_modalities(arch: Any) -> bool:
    if not isinstance(arch, dict):
        return False
    ins, outs = arch.get("input_modalities"), arch.get("output_modalities")
    return isinstance(ins, list) and isinstance(outs, list) and "text" in ins and "text" in outs


def normalize_litellm(payload: bytes, fetched_at: datetime) -> Tuple[Dict[str, CatalogEntry], int]:
    """Usable chat/completion records of a LiteLLM price file -> (entries by qualified key, skipped count)."""
    try:
        doc = _loads(payload, decimals=True)
    except Exception:
        raise SourceError("unparseable") from None
    if not isinstance(doc, dict):
        raise SourceError("bad payload")
    out: Dict[str, CatalogEntry] = {}
    skipped = 0
    for raw_key, rec in doc.items():
        key = _clean_key(raw_key)
        if key is None or not isinstance(rec, dict) or rec.get("mode") not in ("chat", "completion"):
            skipped += 1
            continue
        rin = _rate_from(rec.get("input_cost_per_token"), allow_str=False)
        rout = _rate_from(rec.get("output_cost_per_token"), allow_str=False)
        if rin is None or rout is None:
            skipped += 1
            continue
        out[f"{PRIMARY}:{key}"] = CatalogEntry(PRIMARY, key, rin, rout, fetched_at)
    if not out:
        raise SourceError("empty catalog")
    return out, skipped


def normalize_openrouter(payload: bytes, fetched_at: datetime) -> Tuple[Dict[str, CatalogEntry], int]:
    """Usable text records of an OpenRouter ``/models`` listing -> (entries by qualified key, skipped count)."""
    try:
        doc = _loads(payload, decimals=True)
    except Exception:
        raise SourceError("unparseable") from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise SourceError("bad payload")
    out: Dict[str, CatalogEntry] = {}
    skipped = 0
    for rec in data:
        key = _clean_key(rec.get("id")) if isinstance(rec, dict) else None
        pricing = rec.get("pricing") if isinstance(rec, dict) else None
        if key is None or not isinstance(pricing, dict) or not _text_modalities(rec.get("architecture")):
            skipped += 1
            continue
        rin = _rate_from(pricing.get("prompt"), allow_str=True)
        rout = _rate_from(pricing.get("completion"), allow_str=True)
        qualified = f"{SECONDARY}:{key}"
        if rin is None or rout is None or qualified in out:
            skipped += 1
            continue
        out[qualified] = CatalogEntry(SECONDARY, key, rin, rout, fetched_at)
    if not out:
        raise SourceError("empty catalog")
    return out, skipped


_NORMALIZERS: Mapping[str, Callable[[bytes, datetime], Tuple[Dict[str, CatalogEntry], int]]] = MappingProxyType({
    PRIMARY: normalize_litellm,
    SECONDARY: normalize_openrouter,
})


# ------------------------------------------------------------------ packaged data (seed, aliases)

_PARSE_CACHE: Dict[Tuple[str, int, int], Any] = {}
_PARSE_LOCK = threading.Lock()


def _read_packaged(path: Path, limit: int) -> bytes:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise PricingDataError("unusable file")
        return path.read_bytes()
    except OSError:
        raise PricingDataError("unreadable file") from None


def _cached_parse(path: Path, limit: int, parse: Callable[[bytes], Any]) -> Tuple[Any, bytes]:
    try:
        st = path.lstat()
        sig = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        raise PricingDataError("unreadable file") from None
    with _PARSE_LOCK:
        hit = _PARSE_CACHE.get(sig)
    if hit is not None:
        return hit
    data = _read_packaged(path, limit)
    value = (parse(data), data)
    with _PARSE_LOCK:
        if len(_PARSE_CACHE) > 8:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[sig] = value
    return value


def load_seed_envelope(path: Optional[Path] = None) -> Tuple[Dict[str, SourceMeta], Dict[str, CatalogEntry]]:
    """Validated packaged seed (sources, entries). Raises ``PricingDataError``; package-relative, never CWD."""
    target = Path(path) if path is not None else PACKAGE_DATA / SEED_FILE
    (sources, entries), _ = _cached_parse(target, MAX_SEED_BYTES, lambda d: _parse_envelope(d, now=None))
    return dict(sources), dict(entries)


def load_seed(path: Optional[Path] = None) -> CatalogSnapshot:
    _, entries = load_seed_envelope(path)
    return CatalogSnapshot(MappingProxyType(entries), compute_revision(entries), "seed", ())


def _parse_aliases(data: bytes) -> Dict[str, Alias]:
    try:
        doc = _loads(data)
    except Exception:
        raise PricingDataError("unparseable") from None
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "aliases"}:
        raise PricingDataError("bad alias file")
    if isinstance(doc["schema_version"], bool) or doc["schema_version"] != SCHEMA_VERSION:
        raise PricingDataError("unknown schema version")
    raw = doc["aliases"]
    if not isinstance(raw, dict):
        raise PricingDataError("bad alias file")
    out: Dict[str, Alias] = {}
    for model_id, rec in raw.items():
        if not isinstance(model_id, str) or not _MODEL_RE.match(model_id):
            raise PricingDataError("bad alias id")
        if not isinstance(rec, dict) or set(rec) != {"catalog_key", "kind", "note"}:
            raise PricingDataError("bad alias")
        _split_qualified(rec["catalog_key"])
        kind, note = rec["kind"], rec["note"]
        if kind not in ("equivalent", "proxy"):
            raise PricingDataError("bad alias kind")
        if not isinstance(note, str) or not note.strip() or len(note) > MAX_ALIAS_NOTE \
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in note):
            raise PricingDataError("bad alias note")
        out[model_id] = Alias(rec["catalog_key"], kind, note)
    return out


def load_aliases_strict(path: Optional[Path] = None) -> Mapping[str, Alias]:
    """The curated alias map; raises ``PricingDataError`` when the file is invalid."""
    target = Path(path) if path is not None else PACKAGE_DATA / ALIASES_FILE
    aliases, _ = _cached_parse(target, 1024 * 1024, _parse_aliases)
    return MappingProxyType(dict(aliases))


def load_aliases(path: Optional[Path] = None) -> Mapping[str, Alias]:
    """The curated alias map; an invalid file is a safe EMPTY map (the manager adds ``pricing_aliases_invalid``)."""
    try:
        return load_aliases_strict(path)
    except PricingDataError:
        log.warning("pricing: alias file invalid; using empty alias map")
        return MappingProxyType({})


def aliases_digest(path: Optional[Path] = None) -> Optional[str]:
    """SHA-256 of the alias file bytes (response-cache identity, design §7), ``None`` when unreadable."""
    target = Path(path) if path is not None else PACKAGE_DATA / ALIASES_FILE
    try:
        return hashlib.sha256(_read_packaged(target, 1024 * 1024)).hexdigest()
    except PricingDataError:
        return None


# ------------------------------------------------------------------ heuristic normalization (design §5)

_SIZE_RE = re.compile(r":(\d+(?:\.\d+)?)b$")
_SUFFIX_PATTERNS = (
    re.compile(r"-iq[2-8]$"),
    re.compile(r"-q[2-8](?:-(?:k|s|m|l))*$"),
    re.compile(r"-gpu-[0-9]+k$"),
    re.compile(r"-b[0-9]+k$"),
)
_VENDOR_DUPLICATES = {"qwen": "qwen", "openai": "gpt", "anthropic": "claude"}
_FULL_DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_MONTH_RE = re.compile(r"^(?:0[1-9]|1[0-2])$")
_DAY_RE = re.compile(r"^(?:0[1-9]|[12]\d|3[01])$")


def _base_form(text: str) -> str:
    """Step 3: lowercase, underscores -> hyphens, terminal ``:<n>b`` -> ``-<n>b`` (other colons stay meaningful)."""
    lowered = text.lower().replace("_", "-")
    return _SIZE_RE.sub(lambda m: f"-{m.group(1)}b", lowered)


def _strip_decorations(text: str) -> str:
    """Step 4: strip terminal quant/hardware suffixes repeatedly until stable. Never removes interior tokens."""
    changed = True
    while changed:
        changed = False
        for pattern in _SUFFIX_PATTERNS:
            stripped = pattern.sub("", text)
            if stripped != text and stripped:
                text, changed = stripped, True
    return text


def _tokens(text: str) -> Tuple[str, ...]:
    toks = [t for t in re.split(r"[/-]+", text) if t]
    if len(toks) >= 2 and _VENDOR_DUPLICATES.get(toks[0]) and toks[1].startswith(_VENDOR_DUPLICATES[toks[0]]):
        toks = toks[1:]
    return tuple(toks)


def query_tokens(model_id: str) -> Tuple[str, ...]:
    """Token vector of a recorded session model id (steps 3-5); the original id is never altered elsewhere."""
    text = _base_form(model_id)
    had_size = text != model_id.lower().replace("_", "-")
    text = _strip_decorations(text)
    if not had_size:
        # ``name:7b-q4_k_m``: the size only becomes terminal once the quant suffix is gone; keep it as a token.
        text = _SIZE_RE.sub(lambda m: f"-{m.group(1)}b", text)
    return _tokens(text)


def catalog_tokens(source: str, source_key: str) -> Tuple[str, ...]:
    """Token vector of a catalog key: provider/vendor namespace removed, then the same base form (no quant strip)."""
    key = source_key
    if "/" in key:
        key = key.split("/", 1)[1]  # LiteLLM provider component / OpenRouter vendor component
    return _tokens(_base_form(key))


def coalesce_dates(tokens: Sequence[str]) -> Tuple[str, ...]:
    """``YYYY-MM-DD`` runs and ``YYYYMMDD`` tokens normalize to ``YYYYMMDD``; terminal ``MMDD`` stays distinct."""
    out: List[str] = []
    i = 0
    while i < len(tokens):
        if i + 2 < len(tokens) and _YEAR_RE.match(tokens[i]) and _MONTH_RE.match(tokens[i + 1]) \
                and _DAY_RE.match(tokens[i + 2]):
            out.append(tokens[i] + tokens[i + 1] + tokens[i + 2])
            i += 3
        else:
            out.append(tokens[i])
            i += 1
    return tuple(out)


class _Index:
    """Per-snapshot lookup tables: exact keys per source, and raw/date-coalesced token vectors per source."""

    __slots__ = ("raw", "dated")

    def __init__(self, entries: Mapping[str, CatalogEntry]) -> None:
        self.raw: Dict[Tuple[str, Tuple[str, ...]], List[str]] = {}
        self.dated: Dict[Tuple[str, Tuple[str, ...]], List[str]] = {}
        for key, entry in entries.items():
            toks = catalog_tokens(entry.source, entry.source_key)
            if not toks:
                continue
            self.raw.setdefault((entry.source, toks), []).append(key)
            self.dated.setdefault((entry.source, coalesce_dates(toks)), []).append(key)


def _index_for(catalog: CatalogSnapshot) -> _Index:
    idx = getattr(catalog, "_index", None)
    if idx is None:
        idx = _Index(catalog.entries)
        object.__setattr__(catalog, "_index", idx)  # frozen dataclass: memoize outside the declared fields
    return idx


def _pick(candidates: Sequence[str], entries: Mapping[str, CatalogEntry]) -> Any:
    """Step 7: unique wins; several only when every rate pair is identical (lexicographic key), else ambiguous."""
    if len(candidates) == 1:
        return entries[candidates[0]]
    rates = {(entries[k].input_usd_per_token, entries[k].output_usd_per_token) for k in candidates}
    if len(rates) == 1:
        return entries[min(candidates)]
    return "ambiguous_model"


def _heuristic(model_id: str, catalog: CatalogSnapshot) -> Any:
    """``CatalogEntry`` or a reason code. Primary decides first; secondary only when primary has NO candidate."""
    entries = catalog.entries
    idx = _index_for(catalog)
    query = query_tokens(model_id)
    for source in SOURCE_IDS:
        exact = entries.get(f"{source}:{model_id}")  # step 2: exact upstream key, still method=heuristic
        if exact is not None:
            return exact
        if not query:
            continue
        for table, vector in ((idx.raw, query), (idx.dated, coalesce_dates(query))):
            found = table.get((source, vector))
            if found:
                return _pick(found, entries)
    return "no_catalog_match"


# ------------------------------------------------------------------ resolution (pure)


def safe_model_id(raw: Any) -> Optional[str]:
    """Same acceptance rule as ``adapters.session_db.safe_model_id`` (printable id, no secrets)."""
    if not isinstance(raw, str) or not _MODEL_RE.match(raw) or contains_secret(raw):
        return None
    return raw


def _unresolved(reason: str) -> PriceResolution:
    return PriceResolution(None, None, None, "none", None, None, reason, None, None, False)


def _token_problem(input_count: Any, output_count: Any) -> Optional[str]:
    for value in (input_count, output_count):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            return "tokens_invalid"
    if input_count is None or output_count is None:
        return "tokens_missing"
    return None


def amount_pico(entry: CatalogEntry, input_count: int, output_count: int) -> int:
    """``in*rate_in + out*rate_out`` quantized ONCE to pico-USD (ROUND_HALF_EVEN); the authoritative additive integer."""
    raw = _CTX.add(_CTX.multiply(Decimal(input_count), entry.input_usd_per_token),
                   _CTX.multiply(Decimal(output_count), entry.output_usd_per_token))
    return int(raw.quantize(PICO, rounding=ROUND_HALF_EVEN, context=_CTX).scaleb(12, context=_CTX))


def pico_to_usd(amount: int) -> float:
    return float(Decimal(amount).scaleb(-12, context=_CTX))


def resolve_api_equivalent(model_id: Optional[str], input_count: Optional[int], output_count: Optional[int], *,
                           catalog: CatalogSnapshot, aliases: Mapping[str, Alias],
                           now: datetime) -> PriceResolution:
    """Order: exact curated alias > heuristics > unknown. Pure: no network, disk, clock; unpriced cases never raise.

    ``api_equivalent_usd`` is ``None`` (never 0) whenever the price is not known; a priced zero-token or
    free-rate observation is a genuine ``0.0``.
    """
    model = safe_model_id(model_id)
    if model is None:
        return _unresolved("model_unknown")
    method: str
    kind: Optional[str] = None
    note: Optional[str] = None
    alias = aliases.get(model)
    if alias is not None:
        entry = catalog.entries.get(alias.catalog_key)
        if entry is None:  # never fall through to a different heuristic key
            return _unresolved("alias_target_missing")
        method, kind, note = "alias", alias.kind, alias.note
    else:
        found = _heuristic(model, catalog)
        if isinstance(found, str):
            return _unresolved(found)
        entry, method = found, "heuristic"
    stale = (_utc(now) - _utc(entry.fetched_at)) > STALE_AFTER
    problem = _token_problem(input_count, output_count)
    pico = usd = None
    if problem is None:
        pico = amount_pico(entry, input_count, output_count)  # type: ignore[arg-type]
        usd = pico_to_usd(pico)
    return PriceResolution(usd, pico, entry.key, method, kind, note, problem,  # type: ignore[arg-type]
                           entry.source, _utc(entry.fetched_at), stale)


def aggregate_resolutions(items: Iterable[Tuple[str, PriceResolution]]) -> AggregateResolution:
    """Sum known leaf integers; unknown leaves are excluded (never zero) and listed in ``unknown_row_ids``.

    ``items`` are ``(row_id, resolution)`` per canonical provenance row. No known leaf -> null amount.
    """
    total = 0
    priced = unpriced = proxy = 0
    stale = False
    unknown: List[str] = []
    for row_id, res in items:
        if res.amount_pico_usd is None:
            unpriced += 1
            unknown.append(row_id)
            continue
        total += res.amount_pico_usd
        priced += 1
        proxy += 1 if res.alias_kind == "proxy" else 0
        stale = stale or res.stale
    amount = total if priced else None
    return AggregateResolution(amount, pico_to_usd(amount) if amount is not None else None, priced, unpriced,
                               proxy, unpriced > 0, stale, tuple(sorted(set(unknown))))


# ------------------------------------------------------------------ transport


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str) -> None:
        self._host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != self._host:
            raise FetchError("redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url: str, timeout_s: float, max_bytes: int) -> bytes:
    """Production transport: the two fixed HTTPS endpoints only, anonymous, bounded, same-host redirects only."""
    if url not in SOURCE_URLS.values():
        raise FetchError("endpoint not allowed")
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = urllib.request.build_opener(_SameHostRedirect(host))
    request = urllib.request.Request(url, headers={"User-Agent": "usage-deck-pricing", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout_s) as resp:
            if getattr(resp, "status", 200) != 200:
                raise FetchError("http status")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("response too large")
            body = resp.read(max_bytes + 1)
    except FetchError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FetchError("transport failure") from exc
    if len(body) > max_bytes:
        raise FetchError("response too large")
    return body


# ------------------------------------------------------------------ on-disk cache (openat-style, like captures.py)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class _CacheUnsafe(Exception):
    """Symlink / non-regular / wrong layout somewhere on the cache path: never read, never written."""


def _open_cache_dir(data_dir: Path, *, create: bool) -> Optional[int]:
    """fd of ``<data_dir>/pricing`` (``None`` when absent and not creating). Symlinks anywhere are refused."""
    parent = data_dir.parent
    try:
        if parent.is_symlink():
            raise _CacheUnsafe()
        if create:
            parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(data_dir, 0o700)
        top = os.open(data_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _CacheUnsafe() from None
        raise
    try:
        name = DATA_DIR_PARTS[0]
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=top)
        try:
            fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=top)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _CacheUnsafe() from None
            raise
        return fd
    finally:
        os.close(top)


def _read_cache_bytes(data_dir: Path) -> Optional[bytes]:
    dir_fd = _open_cache_dir(data_dir, create=False)
    if dir_fd is None:
        return None
    try:
        try:
            fd = os.open(CACHE_FILE, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):
                raise _CacheUnsafe() from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _CacheUnsafe()
            chunks: List[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CACHE_BYTES:
                    raise PricingDataError("cache too large")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


@contextlib.contextmanager
def _file_lock(dir_fd: int):
    try:
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
    except OSError:
        raise _CacheUnsafe() from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _CacheUnsafe()
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("cache lock timeout") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_cache_atomically(dir_fd: int, payload: bytes) -> None:
    try:
        st = os.stat(CACHE_FILE, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise _CacheUnsafe()
    tmp = f".catalog-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
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
        os.rename(tmp, CACHE_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


# ------------------------------------------------------------------ catalog manager


class PricingCatalogManager:
    """Seed + validated disk cache + jittered refresh. ``snapshot()`` never blocks on the network.

    ``data_dir`` is the UsageDeck data directory (``<authorized root>/plugin-data/usage-deck``). ``clock``,
    ``random_uniform``, ``fetch`` and ``sleep`` are injectable; ``background=False`` disables the worker thread
    that ``snapshot()`` otherwise starts when a refresh is due (tests drive ``refresh_if_due()`` directly).
    """

    def __init__(self, data_dir: Path, *, clock: Optional[Callable[[], datetime]] = None,
                 random_uniform: Optional[Callable[[float, float], float]] = None,
                 fetch: Optional[Callable[[str, float, int], bytes]] = None,
                 sleep: Optional[Callable[[float], None]] = None, background: bool = True,
                 seed_path: Optional[Path] = None, aliases_path: Optional[Path] = None) -> None:
        self._data_dir = Path(data_dir)
        self._clock = clock or _now_utc
        self._uniform = random_uniform or random.uniform
        self._fetch = fetch or default_fetch
        self._sleep = sleep or time.sleep
        self._background = background
        self._aliases_path = aliases_path
        self._lock = threading.RLock()
        self._flight = threading.Lock()
        self._scheduled = False
        self._version = 0
        self._memo: Optional[Tuple[int, Tuple[str, ...], CatalogSnapshot]] = None
        self._merged: Optional[Tuple[int, Dict[str, CatalogEntry], str]] = None
        self._seed_sources: Dict[str, SourceMeta] = {}
        self._seed_entries: Dict[str, CatalogEntry] = {}
        self._cache_sources: Dict[str, SourceMeta] = {}
        self._cache_entries: Dict[str, CatalogEntry] = {}
        self._retry_at: Dict[str, datetime] = {}
        self._failed: Dict[str, bool] = {}
        self._network_ok = False
        self._seed_invalid = False
        self._cache_invalid = False
        self._cache_unwritable = False
        self._aliases: Mapping[str, Alias] = MappingProxyType({})
        self._aliases_invalid = False
        self._load_seed(seed_path)
        self._load_aliases()
        self._load_cache()

    # -- loading ---------------------------------------------------------------------------------------------

    def _load_seed(self, path: Optional[Path]) -> None:
        try:
            self._seed_sources, self._seed_entries = load_seed_envelope(path)
        except Exception:
            log.warning("pricing: packaged seed invalid; running with an empty seed")
            self._seed_invalid = True

    def _load_aliases(self) -> None:
        try:
            self._aliases = load_aliases_strict(self._aliases_path)
        except Exception:
            log.warning("pricing: alias file invalid; using empty alias map")
            self._aliases_invalid = True

    def _load_cache(self) -> None:
        try:
            raw = _read_cache_bytes(self._data_dir)
        except _CacheUnsafe:
            self._cache_invalid = self._cache_unwritable = True
            log.warning("pricing: cache path unsafe; using seed without disk persistence")
            return
        except Exception:
            self._cache_invalid = True
            log.warning("pricing: cache unreadable; using seed")
            return
        if raw is None:
            return
        try:
            sources, entries = _parse_envelope(raw, now=self._clock())
        except Exception:
            self._cache_invalid = True  # left on disk untouched until a validated replacement exists
            log.warning("pricing: cache invalid; using seed")
            return
        self._cache_sources, self._cache_entries = sources, entries
        self._version += 1

    def aliases(self) -> Mapping[str, Alias]:
        return self._aliases

    def aliases_digest(self) -> Optional[str]:
        return aliases_digest(self._aliases_path)

    # -- snapshot ---------------------------------------------------------------------------------------------

    def _merged_entries(self) -> Tuple[Dict[str, CatalogEntry], str]:
        if self._merged is not None and self._merged[0] == self._version:
            return self._merged[1], self._merged[2]
        merged = dict(self._seed_entries)
        for key, entry in self._cache_entries.items():
            held = merged.get(key)
            if held is None or entry.fetched_at >= held.fetched_at:
                merged[key] = entry
        revision = compute_revision(merged)
        self._merged = (self._version, merged, revision)
        return merged, revision

    def _best_source_meta(self, source: str) -> Optional[SourceMeta]:
        metas = [m for m in (self._cache_sources.get(source), self._seed_sources.get(source)) if m is not None]
        return max(metas, key=lambda m: m.fetched_at) if metas else None

    def _warnings(self, now: datetime) -> Tuple[str, ...]:
        out = set()
        if self._seed_invalid:
            out.add(WARN_SEED_INVALID)
        if self._aliases_invalid:
            out.add(WARN_ALIASES_INVALID)
        if self._cache_invalid:
            out.add(WARN_CACHE_INVALID)
        if self._cache_unwritable:
            out.add(WARN_CACHE_UNWRITABLE)
        if any(self._failed.values()):
            out.add(WARN_REFRESH_FAILED)
        for source in SOURCE_IDS:
            meta = self._best_source_meta(source)
            if meta is not None and _utc(now) - meta.fetched_at > STALE_AFTER:
                out.add(WARN_STALE)
        return tuple(sorted(out))

    def snapshot(self) -> CatalogSnapshot:
        """Immutable current view; never waits on the network. Schedules a background refresh when one is due."""
        now = self._clock()
        if self._background:
            self._maybe_schedule(now)
        with self._lock:
            merged, revision = self._merged_entries()
            warnings = self._warnings(now)
            if self._memo is not None and self._memo[0] == self._version and self._memo[1] == warnings:
                return self._memo[2]
            if self._network_ok:
                origin = "network"
            elif self._cache_entries:
                origin = "cache"
            else:
                origin = "seed"
            snap = CatalogSnapshot(MappingProxyType(dict(merged)), revision, origin, warnings)  # type: ignore[arg-type]
            self._memo = (self._version, warnings, snap)
            return snap

    # -- refresh ----------------------------------------------------------------------------------------------

    def _due_at(self, source: str) -> datetime:
        retry = self._retry_at.get(source)
        if retry is not None:
            return retry
        meta = self._best_source_meta(source)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        if meta is None:
            return epoch
        if meta.fetched_at > _utc(self._clock()) + FUTURE_SKEW:
            return epoch
        if meta.next_refresh_at < meta.fetched_at or meta.next_refresh_at > meta.fetched_at + MAX_NEXT_REFRESH:
            return epoch  # corrupt / far-future: due now
        return meta.next_refresh_at

    def _is_due(self, now: datetime) -> bool:
        return any(self._due_at(s) <= _utc(now) for s in SOURCE_IDS)

    def _maybe_schedule(self, now: datetime) -> None:
        with self._lock:
            if self._scheduled or not self._is_due(now):
                return
            self._scheduled = True
        try:
            threading.Thread(target=self._run_scheduled, name="usage-deck-pricing", daemon=True).start()
        except Exception:
            with self._lock:
                self._scheduled = False

    def _run_scheduled(self) -> None:
        try:
            self.refresh_if_due()
        except Exception:  # never let the worker die noisily
            log.warning("pricing: background refresh crashed")
        finally:
            with self._lock:
                self._scheduled = False

    def refresh_if_due(self) -> None:
        """Bounded refresh of every due source (primary first). Single-flight; failures keep every held price."""
        if not self._flight.acquire(blocking=False):
            return
        try:
            for source in SOURCE_IDS:
                with self._lock:
                    due = self._due_at(source) <= _utc(self._clock())
                if due:
                    self._refresh_source(source)
        finally:
            self._flight.release()

    def _attempt(self, source: str) -> Tuple[Dict[str, CatalogEntry], int, str, datetime]:
        last: Optional[Exception] = None
        for attempt in range(FETCH_ATTEMPTS):
            if attempt:
                self._sleep(self._uniform(*RETRY_JITTER_S))
            try:
                body = self._fetch(SOURCE_URLS[source], FETCH_TIMEOUT_S, MAX_FETCH_BYTES)
                if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_FETCH_BYTES:
                    raise SourceError("bad body")
                fetched_at = _utc(self._clock())
                entries, skipped = _NORMALIZERS[source](bytes(body), fetched_at)
                return entries, skipped, hashlib.sha256(bytes(body)).hexdigest(), fetched_at
            except Exception as exc:  # HTTP/schema/parse/timeout/empty all count as failure
                last = exc
        raise last if last is not None else SourceError("no attempt")

    def _refresh_source(self, source: str) -> None:
        started = _utc(self._clock())
        try:
            entries, skipped, sha, fetched_at = self._attempt(source)
        except Exception as exc:
            with self._lock:
                self._failed[source] = True
                self._retry_at[source] = started + FAILURE_RETRY * self._uniform(*REFRESH_JITTER)
                self._version += 1
            log.warning("pricing: refresh failed source=%s attempted_at=%s result=%s", source, format_ts(started),
                        type(exc).__name__)
            return
        next_refresh = fetched_at + REFRESH_INTERVAL * self._uniform(*REFRESH_JITTER)  # drawn once, persisted
        with self._lock:
            self._cache_entries.update(entries)  # per key; absent old keys keep their original fetched_at
            self._cache_sources[source] = SourceMeta(fetched_at, next_refresh, sha)
            self._failed.pop(source, None)
            self._retry_at.pop(source, None)
            self._network_ok = True
            self._version += 1
            sources_copy, entries_copy = dict(self._cache_sources), dict(self._cache_entries)
        log.info("pricing: refreshed source=%s fetched_at=%s entries=%d skipped=%d", source, format_ts(fetched_at),
                 len(entries), skipped)
        self._persist(sources_copy, entries_copy)

    # -- persistence ------------------------------------------------------------------------------------------

    def _persist(self, sources: Dict[str, SourceMeta], entries: Dict[str, CatalogEntry]) -> None:
        """Atomic, serialized write; a failure leaves memory and the existing disk file untouched."""
        try:
            dir_fd = _open_cache_dir(self._data_dir, create=True)
            if dir_fd is None:
                raise OSError("no cache dir")
            try:
                with _file_lock(dir_fd):
                    merged_sources, merged_entries = self._merge_with_disk(sources, entries)
                    _write_cache_atomically(dir_fd, _serialize_envelope(merged_sources, merged_entries))
            finally:
                os.close(dir_fd)
        except Exception as exc:
            with self._lock:
                self._cache_unwritable = True
                self._version += 1
            log.warning("pricing: cache write failed (%s); keeping in-memory snapshot", type(exc).__name__)
            return
        with self._lock:
            if self._cache_unwritable or self._cache_invalid:
                self._cache_unwritable = self._cache_invalid = False  # a validated replacement now exists
                self._version += 1

    def _merge_with_disk(self, sources: Dict[str, SourceMeta], entries: Dict[str, CatalogEntry]):
        """Under the file lock: fold in another process's newer valid records so writers never regress each other."""
        try:
            raw = _read_cache_bytes(self._data_dir)
            disk_sources, disk_entries = _parse_envelope(raw, now=self._clock()) if raw is not None else ({}, {})
        except Exception:
            return sources, entries  # invalid/unsafe-looking existing file is replaced by our validated data
        out_entries = dict(disk_entries)
        for key, entry in entries.items():
            held = out_entries.get(key)
            if held is None or entry.fetched_at >= held.fetched_at:
                out_entries[key] = entry
        out_sources = dict(disk_sources)
        for sid, meta in sources.items():
            held_meta = out_sources.get(sid)
            if held_meta is None or meta.fetched_at >= held_meta.fetched_at:
                out_sources[sid] = meta
        return out_sources, out_entries
