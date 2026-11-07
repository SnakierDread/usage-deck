"""Frozen stdlib DTOs for the UsageDeck wire contract (DESIGN.md §2).

Every listed field is always serialized (omitted != null). Constructors
validate enums and finite numbers; free text never comes from upstream, only
from the fixed tables in this module. ``to_dict()`` output must still pass
through ``redaction.sanitize`` at the coordinator boundary before it is cached
or returned.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

SCHEMA_VERSION = 1

CONFIDENCE_LABELS = ("exact", "estimated", "opaque", "self_hosted")
CARD_STATES = ("ready", "error", "opaque", "empty")
REASONS = (
    "unsupported",
    "credentials_missing",
    "permission_denied",
    "attribution_unavailable",
    "no_observations",
    "metrics_unavailable",
    "source_unavailable",
)
CAPABILITIES = ("quota", "usage", "metrics", "session_analytics")
ERROR_CODES = (
    "invalid_query",
    "provider_not_found",
    "auth_required",
    "permission_denied",
    "timeout",
    "upstream_unavailable",
    "malformed_response",
    "inventory_unavailable",
    "analytics_unavailable",
    "internal_error",
)
UNITS = ("usd", "count", "percent", "bytes")
METER_SCOPES = ("api_credential", "provider_account", "endpoint", "profile")
METER_SOURCES = ("openrouter_key", "session_analytics", "prometheus", "anthropic_headers", "none")
VIEWS = ("machine", "profile")
PLAN_SOURCES = ("unverified_claim", "provider", "none")
AUTH_STATUSES = ("available", "missing", "rejected")
KINDS = ("cloud", "self_hosted")
HEALTH_STATUSES = ("healthy", "degraded", "offline", "unknown")
SERIES_SCOPES = ("profile", "provider", "account")
WINDOWS = (7, 30, 90)

# Fixed, safe public text. Upstream text never reaches a DTO.
ERROR_MESSAGES = {
    "invalid_query": "The request parameters are not valid.",
    "provider_not_found": "No such provider is configured in this profile.",
    "auth_required": "The provider rejected the stored credential.",
    "permission_denied": "The credential does not permit reading usage.",
    "timeout": "The provider did not answer in time.",
    "upstream_unavailable": "The provider could not be reached.",
    "malformed_response": "The provider returned data in an unexpected format.",
    "inventory_unavailable": "The provider inventory could not be read.",
    "analytics_unavailable": "Session analytics are unavailable.",
    "internal_error": "An internal error occurred.",
}
ERROR_RETRYABLE = {
    "invalid_query": False,
    "provider_not_found": False,
    "auth_required": False,
    "permission_denied": False,
    "timeout": True,
    "upstream_unavailable": True,
    "malformed_response": False,
    "inventory_unavailable": False,
    "analytics_unavailable": True,
    "internal_error": False,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class DtoError(ValueError):
    """A DTO invariant failed; a programming error, never an upstream error."""


# ---------------------------------------------------------------- helpers


def fmt_ts(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise DtoError("naive datetime")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enum(name: str, value: Any, allowed: Tuple[Any, ...], nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if isinstance(value, bool) and not any(isinstance(a, bool) for a in allowed):
        raise DtoError(f"{name}: invalid value")
    if value not in allowed:
        raise DtoError(f"{name}: invalid value")
    return value


def _str(name: str, value: Any, nullable: bool = False, max_len: int = 256) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise DtoError(f"{name}: expected string")
    return value


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise DtoError(f"{name}: expected bool")
    return value


def _number(name: str, value: Any, nullable: bool = True, minimum: Optional[float] = None) -> Optional[float]:
    if value is None:
        if nullable:
            return None
        raise DtoError(f"{name}: required")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DtoError(f"{name}: expected number")
    if isinstance(value, float) and not math.isfinite(value):
        raise DtoError(f"{name}: non-finite")
    if minimum is not None and value < minimum:
        raise DtoError(f"{name}: below minimum")
    return value


def _integer(name: str, value: Any, minimum: Optional[int] = 0) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DtoError(f"{name}: expected integer")
    if minimum is not None and value < minimum:
        raise DtoError(f"{name}: below minimum")
    return value


def _ts(name: str, value: Any, nullable: bool = True) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise DtoError(f"{name}: bad timestamp")
    return value


def to_plain(obj: Any) -> Any:
    """Convert DTOs to plain dict/list structures, every field included."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_plain(item) for item in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- DTOs


@dataclass(frozen=True)
class DeckError:
    code: str
    message: str
    retryable: bool
    provider_id: Optional[str] = None
    account_id: Optional[str] = None

    def __post_init__(self) -> None:
        _enum("code", self.code, ERROR_CODES)
        _str("message", self.message)
        _bool("retryable", self.retryable)
        _str("provider_id", self.provider_id, nullable=True)
        _str("account_id", self.account_id, nullable=True)


def make_error(code: str, provider_id: Optional[str] = None, account_id: Optional[str] = None) -> DeckError:
    """Build an error from the fixed message table only."""
    _enum("code", code, ERROR_CODES)
    return DeckError(code, ERROR_MESSAGES[code], ERROR_RETRYABLE[code], provider_id, account_id)


@dataclass(frozen=True)
class Period:
    start: Optional[str] = None
    end: Optional[str] = None
    label: str = "unknown"

    def __post_init__(self) -> None:
        _ts("period.start", self.start)
        _ts("period.end", self.end)
        if (self.start is None) != (self.end is None):
            raise DtoError("period: both bounds or neither")
        if not isinstance(self.label, str) or not re.match(r"^[a-z0-9_]{1,32}$", self.label):
            raise DtoError("period.label")


UNKNOWN_PERIOD = Period(None, None, "unknown")


@dataclass(frozen=True)
class UsageMeter:
    id: str
    label: str
    value: Optional[float]
    limit: Optional[float]
    remaining: Optional[float]
    unit: str
    scope: str
    period: Period
    confidence: str
    source: str
    observed_at: Optional[str]
    #: Provider-reported reset instant of the metered window (never computed or extrapolated).
    resets_at: Optional[str] = None

    def __post_init__(self) -> None:
        _str("id", self.id)
        _str("label", self.label)
        _number("value", self.value, minimum=0)
        _number("limit", self.limit, minimum=0)
        _number("remaining", self.remaining)  # signed: finite negative = overspend
        _enum("unit", self.unit, UNITS)
        _enum("scope", self.scope, METER_SCOPES)
        if not isinstance(self.period, Period):
            raise DtoError("period")
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _enum("source", self.source, METER_SOURCES)
        _ts("observed_at", self.observed_at)
        _ts("resets_at", self.resets_at)


_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class Holder:
    """One Hermes home (the global root or a named profile) whose auth.json holds an account."""

    profile: str
    is_global: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not _HOLDER_RE.match(self.profile):
            raise DtoError("holder.profile")
        _bool("is_global", self.is_global)


@dataclass(frozen=True)
class DebugInfo:
    adapter: str
    capabilities: Tuple[str, ...]
    source: str
    sanitized: bool = True

    def __post_init__(self) -> None:
        _str("adapter", self.adapter)
        for cap in self.capabilities:
            _enum("capability", cap, CAPABILITIES)
        _str("source", self.source)
        if self.sanitized is not True:
            raise DtoError("sanitized")


@dataclass(frozen=True)
class SubscriptionCard:
    id: str
    provider_id: str
    account_id: str
    provider_label: str
    account_label: str
    kind: str
    plan_name: Optional[str]
    plan_source: str
    auth_status: str
    endpoint_id: Optional[str]
    state: str
    confidence: str
    reason: Optional[str]
    meters: Tuple[UsageMeter, ...]
    checked_at: str
    stale: bool
    error: Optional[DeckError]
    debug: DebugInfo
    #: Machine view only: the homes holding this account (empty in profile view).
    holders: Tuple[Holder, ...] = ()

    def __post_init__(self) -> None:
        _str("id", self.id)
        if self.id != self.account_id:
            raise DtoError("card id must equal account_id")
        _str("provider_id", self.provider_id)
        _str("provider_label", self.provider_label)
        _str("account_label", self.account_label)
        _enum("kind", self.kind, KINDS)
        _str("plan_name", self.plan_name, nullable=True, max_len=32)
        _enum("plan_source", self.plan_source, PLAN_SOURCES)
        _enum("auth_status", self.auth_status, AUTH_STATUSES)
        _str("endpoint_id", self.endpoint_id, nullable=True)
        _enum("state", self.state, CARD_STATES)
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _enum("reason", self.reason, REASONS, nullable=True)
        _ts("checked_at", self.checked_at, nullable=False)
        _bool("stale", self.stale)
        if self.error is not None and not isinstance(self.error, DeckError):
            raise DtoError("error")
        if self.plan_name is None and self.plan_source == "unverified_claim":
            raise DtoError("plan_source without plan")
        if not all(isinstance(h, Holder) for h in self.holders):
            raise DtoError("holders")


@dataclass(frozen=True)
class UsagePoint:
    day: str
    input_count: Optional[int]
    output_count: Optional[int]
    estimated_usd: Optional[float]
    sessions: Optional[int]
    api_calls: Optional[int]
    confidence: str

    def __post_init__(self) -> None:
        if not isinstance(self.day, str) or not _DATE_RE.match(self.day):
            raise DtoError("day")
        _integer("input_count", self.input_count)
        _integer("output_count", self.output_count)
        _number("estimated_usd", self.estimated_usd, minimum=0)
        _integer("sessions", self.sessions)
        _integer("api_calls", self.api_calls)
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)


@dataclass(frozen=True)
class UsageTimeSeries:
    id: str
    provider_id: Optional[str]
    account_id: Optional[str]
    scope: str
    confidence: str
    source: str
    period: Period
    window_days: int
    bucket: str
    timezone: str
    coverage: str
    points: Tuple[UsagePoint, ...]
    checked_at: str
    stale: bool
    error: Optional[DeckError]

    def __post_init__(self) -> None:
        _str("id", self.id)
        _str("provider_id", self.provider_id, nullable=True)
        _str("account_id", self.account_id, nullable=True)
        _enum("scope", self.scope, SERIES_SCOPES)
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _enum("source", self.source, ("session_analytics",))
        _enum("window_days", self.window_days, WINDOWS)
        _enum("bucket", self.bucket, ("day",))
        _enum("timezone", self.timezone, ("server_local",))
        _enum("coverage", self.coverage, ("partial", "complete"))
        days = [p.day for p in self.points]
        if days != sorted(set(days)):
            raise DtoError("points must be unique and ascending")
        _ts("checked_at", self.checked_at, nullable=False)
        _bool("stale", self.stale)


@dataclass(frozen=True)
class HealthStripEntry:
    endpoint_id: str
    provider_id: str
    display_name: str
    status: str
    confidence: str
    metrics_available: bool
    latency_ms: Optional[float]
    cpu_percent: Optional[float]
    memory_bytes: Optional[int]
    checked_at: str
    stale: bool
    reason: Optional[str]
    error: Optional[DeckError]

    def __post_init__(self) -> None:
        _str("endpoint_id", self.endpoint_id)
        _str("provider_id", self.provider_id)
        _str("display_name", self.display_name)
        _enum("status", self.status, HEALTH_STATUSES)
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _bool("metrics_available", self.metrics_available)
        _number("latency_ms", self.latency_ms, minimum=0)
        _number("cpu_percent", self.cpu_percent, minimum=0)
        _integer("memory_bytes", self.memory_bytes)
        _ts("checked_at", self.checked_at, nullable=False)
        _bool("stale", self.stale)
        _enum("reason", self.reason, REASONS, nullable=True)
        if self.error is not None and not isinstance(self.error, DeckError):
            raise DtoError("error")


# --------------------------------------------------------- adapter results


@dataclass(frozen=True)
class ProbeResult:
    supported: bool
    reachable: Optional[bool]
    checked_at: str
    capabilities: Tuple[str, ...] = ()
    reason: Optional[str] = None
    error: Optional[DeckError] = None

    def __post_init__(self) -> None:
        _bool("supported", self.supported)
        if self.reachable is not None:
            _bool("reachable", self.reachable)
        _ts("checked_at", self.checked_at, nullable=False)
        for cap in self.capabilities:
            _enum("capability", cap, CAPABILITIES)
        _enum("reason", self.reason, REASONS, nullable=True)


@dataclass(frozen=True)
class UsageResult:
    meters: Tuple[UsageMeter, ...] = ()
    series: Tuple[UsageTimeSeries, ...] = ()
    confidence: str = "opaque"
    reason: Optional[str] = None
    error: Optional[DeckError] = None

    def __post_init__(self) -> None:
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _enum("reason", self.reason, REASONS, nullable=True)


# ---------------------------------------------------------------- envelope


def build_meta(request_id: str, generated_at: str, errors: list) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "generated_at": generated_at,
        "partial": bool(errors),
        "errors": list(errors),
    }


def success_envelope(data: dict, meta: dict) -> dict:
    return {"ok": True, "data": data, "error": None, "meta": meta}


def failure_envelope(error: DeckError, meta: dict) -> dict:
    return {"ok": False, "data": None, "error": to_plain(error), "meta": meta}


# ------------------------------------------------------- wire schema (§2)
#
# The masking pass (``redaction.sanitize``) must never change the *shape* of a
# DTO: field names are the normative schema (DESIGN.md §2) and enum / format
# fields come from fixed local vocabularies, never from private data. The
# tables below are derived from the dataclasses above so they cannot drift.

_WIRE_CLASSES = (
    DeckError, Period, UsageMeter, Holder, DebugInfo, SubscriptionCard, UsagePoint, UsageTimeSeries, HealthStripEntry,
)

# Envelope (§3) and per-endpoint ``data`` keys that are not DTO fields.
_ENVELOPE_KEYS = ("ok", "data", "error", "meta", "schema_version", "request_id", "generated_at", "partial", "errors")
_DATA_KEYS = ("cards", "inventory_state", "view", "window_days", "provider_id", "series", "reason", "entries")

#: Every field name the wire contract defines. Keys outside this set are not
#: schema: the sanitizer scrubs them like any other string.
WIRE_KEYS = frozenset(
    {f.name for cls in _WIRE_CLASSES for f in fields(cls)} | set(_ENVELOPE_KEYS) | set(_DATA_KEYS)
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH_ID_RE = re.compile(r"^(?:acct|ep)_[0-9a-f]{24}$")
_CONST_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PERIOD_LABEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _in(allowed):
    allowed = frozenset(allowed)
    return lambda value: value in allowed


def _match(pattern):
    return lambda value: bool(pattern.match(value))


#: key -> predicate over *string* values. A value that satisfies its key's
#: predicate is contract vocabulary or a generated identifier/timestamp, not
#: private context, so it is preserved verbatim; any other value (free text:
#: labels, plan names, provider ids, debug text) is scrubbed. A secret that
#: collides with vocabulary therefore cannot turn an enum into an invalid
#: success-response value.
WIRE_VALUE_RULES = {
    "confidence": _in(CONFIDENCE_LABELS),
    "state": _in(CARD_STATES),
    "reason": _in(REASONS),
    "code": _in(ERROR_CODES),
    "message": _in(ERROR_MESSAGES.values()),
    "unit": _in(UNITS),
    "scope": _in(set(METER_SCOPES) | set(SERIES_SCOPES)),
    "source": _in(METER_SOURCES),
    "plan_source": _in(PLAN_SOURCES),
    "auth_status": _in(AUTH_STATUSES),
    "kind": _in(KINDS),
    "capabilities": _in(CAPABILITIES),  # list items inherit the key
    "status": _in(HEALTH_STATUSES),
    "inventory_state": _in(("empty", "detected")),
    "view": _in(VIEWS),
    "profile": _match(_HOLDER_RE),
    "bucket": _in(("day",)),
    "timezone": _in(("server_local",)),
    "coverage": _in(("partial", "complete")),
    "checked_at": _match(_TS_RE),
    "observed_at": _match(_TS_RE),
    "generated_at": _match(_TS_RE),
    "start": _match(_TS_RE),
    "end": _match(_TS_RE),
    "resets_at": _match(_TS_RE),
    "day": _match(_DATE_RE),
    "request_id": _match(_UUID_RE),
    "account_id": _match(_HASH_ID_RE),
    "endpoint_id": _match(_HASH_ID_RE),
    "id": lambda value: bool(_HASH_ID_RE.match(value) or _CONST_ID_RE.match(value)),
}

#: Period.label is a safe enum-like token (``rolling_30d``); it shares the key
#: ``label`` with free-text labels, so the sanitizer applies this rule only to
#: mappings shaped exactly like a Period.
PERIOD_KEYS = frozenset(("start", "end", "label"))
PERIOD_LABEL_RULE = _match(_PERIOD_LABEL_RE)


def preserves_wire_value(key: str, value: Any, siblings: Any = ()) -> bool:
    """True when ``value`` under ``key`` is contract vocabulary that masking must not alter."""
    if not isinstance(value, str):
        return False
    if key == "label":
        return frozenset(siblings) == PERIOD_KEYS and PERIOD_LABEL_RULE(value)
    rule = WIRE_VALUE_RULES.get(key)
    return rule is not None and rule(value)


def _exact_keys(name: str, data: Any, cls: Any) -> dict:
    if not isinstance(data, dict):
        raise DtoError(f"{name}: expected object")
    if sorted(data) != sorted(f.name for f in fields(cls)):
        raise DtoError(f"{name}: wire keys differ from schema")
    return data


def _period_from_wire(data: Any) -> Period:
    data = _exact_keys("period", data, Period)
    return Period(data["start"], data["end"], data["label"])


def _error_from_wire(data: Any) -> Optional[DeckError]:
    if data is None:
        return None
    data = _exact_keys("error", data, DeckError)
    return DeckError(data["code"], data["message"], data["retryable"], data["provider_id"], data["account_id"])


def _meter_from_wire(data: Any) -> UsageMeter:
    data = _exact_keys("meter", data, UsageMeter)
    return UsageMeter(**{**data, "period": _period_from_wire(data["period"])})


def _holder_from_wire(data: Any) -> Holder:
    return Holder(**_exact_keys("holder", data, Holder))


def card_from_wire(data: Any) -> SubscriptionCard:
    """Rebuild (and thereby validate) a card from its wire dict; strict key set."""
    data = _exact_keys("card", data, SubscriptionCard)
    debug = _exact_keys("debug", data["debug"], DebugInfo)
    if not isinstance(data["meters"], list) or not isinstance(debug["capabilities"], list) \
            or not isinstance(data["holders"], list):
        raise DtoError("card: expected lists")
    return SubscriptionCard(**{
        **data,
        "meters": tuple(_meter_from_wire(m) for m in data["meters"]),
        "error": _error_from_wire(data["error"]),
        "debug": DebugInfo(debug["adapter"], tuple(debug["capabilities"]), debug["source"], debug["sanitized"]),
        "holders": tuple(_holder_from_wire(h) for h in data["holders"]),
    })


def health_from_wire(data: Any) -> HealthStripEntry:
    data = _exact_keys("health", data, HealthStripEntry)
    return HealthStripEntry(**{**data, "error": _error_from_wire(data["error"])})


def series_from_wire(data: Any) -> UsageTimeSeries:
    data = _exact_keys("series", data, UsageTimeSeries)
    if not isinstance(data["points"], list):
        raise DtoError("series: expected list")
    return UsageTimeSeries(**{
        **data,
        "period": _period_from_wire(data["period"]),
        "points": tuple(UsagePoint(**_exact_keys("point", p, UsagePoint)) for p in data["points"]),
        "error": _error_from_wire(data["error"]),
    })


def check_wire(kind: str, data: Any) -> Any:
    """Assert ``data`` is exactly the wire shape of DTO ``kind`` (card|health|series).

    Raises ``DtoError`` on any missing/extra/renamed key, invalid enum or
    format, or when rebuilding the DTO does not reproduce ``data`` verbatim
    (so a masked value that still "validates" is caught too). Returns ``data``.
    """
    rebuild = {"card": card_from_wire, "health": health_from_wire, "series": series_from_wire}[kind]
    if to_plain(rebuild(data)) != data:
        raise DtoError(f"{kind}: wire dict does not round-trip")
    return data


_META_KEYS = ("schema_version", "request_id", "generated_at", "partial", "errors")
_DATA_SHAPES = {
    frozenset(("cards", "inventory_state", "view")): "subscriptions",
    frozenset(("window_days", "provider_id", "series", "reason")): "usage",
    frozenset(("entries",)): "health",
}


def check_envelope(body: Any) -> Any:
    """Assert ``body`` is exactly a §3 envelope with schema-exact ``data``.

    Used at the coordinator boundary *after* masking: if masking (or anything
    else) altered a key or produced an invalid enum/format, this raises
    ``DtoError`` and the caller answers with the existing ``internal_error``
    model instead of a malformed 200. Returns ``body``.
    """
    if not isinstance(body, dict) or sorted(body) != sorted(("ok", "data", "error", "meta")):
        raise DtoError("envelope: wire keys differ from schema")
    meta = body["meta"]
    if not isinstance(meta, dict) or sorted(meta) != sorted(_META_KEYS):
        raise DtoError("envelope.meta: wire keys differ from schema")
    if meta["schema_version"] != SCHEMA_VERSION or not isinstance(meta["request_id"], str) \
            or not _UUID_RE.match(meta["request_id"]):
        raise DtoError("envelope.meta: invalid")
    _ts("meta.generated_at", meta["generated_at"], nullable=False)
    _bool("meta.partial", meta["partial"])
    if not isinstance(meta["errors"], list) or meta["partial"] != bool(meta["errors"]):
        raise DtoError("envelope.meta.errors: invalid")
    for err in meta["errors"]:
        if to_plain(_error_from_wire(err)) != err or err is None:
            raise DtoError("envelope.meta.errors: invalid")
    if not isinstance(body["ok"], bool):
        raise DtoError("envelope.ok")
    if not body["ok"]:
        if body["data"] is not None or body["error"] is None or to_plain(_error_from_wire(body["error"])) != body["error"]:
            raise DtoError("envelope: bad failure shape")
        return body
    data = body["data"]
    if body["error"] is not None or not isinstance(data, dict):
        raise DtoError("envelope: bad success shape")
    shape = _DATA_SHAPES.get(frozenset(data))
    if shape is None:
        raise DtoError("envelope.data: wire keys differ from schema")
    if shape == "subscriptions":
        _enum("inventory_state", data["inventory_state"], ("empty", "detected"))
        _enum("view", data["view"], VIEWS)
        if not isinstance(data["cards"], list):
            raise DtoError("cards")
        for card in data["cards"]:
            check_wire("card", card)
    elif shape == "health":
        if not isinstance(data["entries"], list):
            raise DtoError("entries")
        for entry in data["entries"]:
            check_wire("health", entry)
    else:
        _enum("window_days", data["window_days"], WINDOWS)
        _str("provider_id", data["provider_id"], nullable=True)
        _enum("reason", data["reason"], REASONS, nullable=True)
        if not isinstance(data["series"], list):
            raise DtoError("series")
        for series in data["series"]:
            check_wire("series", series)
    return body
