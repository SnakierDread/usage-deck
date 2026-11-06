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
METER_SOURCES = ("openrouter_key", "session_analytics", "prometheus", "none")
PLAN_SOURCES = ("unverified_claim", "provider", "none")
AUTH_STATUSES = ("available", "missing")
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
