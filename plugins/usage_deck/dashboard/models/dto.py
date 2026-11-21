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
from decimal import Decimal
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
UNITS = ("usd", "count", "percent", "bytes", "credit")
METER_SCOPES = ("api_credential", "provider_account", "endpoint", "profile")
METER_SOURCES = ("openrouter_key", "session_analytics", "prometheus", "anthropic_headers", "anthropic_oauth_usage",
                 "chatgpt_wham_usage", "none")
VIEWS = ("machine", "profile")
PLAN_SOURCES = ("unverified_claim", "provider", "none")
AUTH_STATUSES = ("available", "missing", "rejected")
KINDS = ("cloud", "self_hosted")
HEALTH_STATUSES = ("healthy", "degraded", "offline", "unknown")
SERIES_SCOPES = ("profile", "provider", "account", "machine")
WINDOWS = (7, 30, 90)
#: Usage responses carry the expanded dimensions payload (analytics-dimensions-breakdown §4); every other
#: endpoint stays at ``SCHEMA_VERSION``.
USAGE_SCHEMA_VERSION = 2
DIMENSIONS = ("all", "profile", "model")
ATTRIBUTION_SOURCES = ("session_provider", "billing_provider", "models_json", "unknown")
SOURCE_STATES = ("ok", "absent", "unavailable", "excluded")
USAGE_REASONS = ("no_observations", "source_unavailable")
USAGE_METRICS = ("api_calls", "estimated_usd", "input_count", "output_count", "sessions")
TOP_N_MODELS = 8

# API-equivalent pricing annotation (docs/design/api-equivalent-pricing.md §7): fixed vocabularies.
PRICING_METHODS = ("alias", "heuristic", "none")
PRICING_ALIAS_KINDS = ("equivalent", "proxy")
PRICING_REASONS = ("tokens_missing", "tokens_invalid", "model_unknown", "no_catalog_match", "ambiguous_model",
                   "alias_target_missing")
PRICING_SOURCES = ("litellm", "openrouter")
PRICING_ORIGINS = ("seed", "cache", "network")
PRICING_WARNINGS = ("pricing_aliases_invalid", "pricing_cache_invalid", "pricing_cache_unwritable",
                    "pricing_refresh_failed", "pricing_seed_invalid", "pricing_stale")
PICO = 10 ** 12  # one USD in the authoritative integer unit (pico-USD)

#: Wire field names that contain a sensitive fragment ("key") but are plain public vocabulary: the sanitizer
#: must not redact them (the value is a source-qualified catalog id, never a credential).
NON_SECRET_WIRE_NAMES = frozenset({"catalog_key"})

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

#: Status-specific wording for an error whose ``code`` is already one of ERROR_CODES (DESIGN.md §9.1.1/§9.1.2
#: taxonomy): variant -> (code, fixed message, retryable). The wire vocabulary stays closed: a variant adds NO new
#: code, only a fixed message, so 401 / 404 / 429 and a dead stored login can be told apart on the card.
REAUTH_HINT = "stored login needs re-auth: run hermes auth add anthropic"
ERROR_VARIANTS = {
    "anthropic_reauth": ("auth_required", REAUTH_HINT, False),
    "anthropic_http_401": ("auth_required",
                           "Anthropic usage answered HTTP 401 (credential refused); " + REAUTH_HINT, False),
    "anthropic_http_404": ("permission_denied",
                           "Anthropic usage answered HTTP 404 (credential or endpoint not valid for this read); "
                           "no reading is estimated.", False),
    "anthropic_http_429": ("upstream_unavailable",
                           "Anthropic usage answered HTTP 429 (rate limited); retry later.", True),
    "anthropic_refresh_unavailable": ("upstream_unavailable",
                                      "The stored login could not be refreshed right now; retry later.", True),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TS_FRAC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_PICO_RE = re.compile(r"^(?:0|[1-9][0-9]{0,30})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATALOG_KEY_RE = re.compile(r"^(?:litellm|openrouter):[\x21-\x7e]{1,200}$")
_ROW_ID_RE = re.compile(r"^row_[0-9a-f]{64}$")
_GROUP_ID_RE = re.compile(r'^(?:all|other|profile:\[(?:true|false),"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"\]'
                          r'|model:null|model:"[\x21\x23-\x5b\x5d-\x7e]{1,128}")$')


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


def make_error(code: str, provider_id: Optional[str] = None, account_id: Optional[str] = None,
               variant: Optional[str] = None) -> DeckError:
    """Build an error from the fixed message tables only (``ERROR_MESSAGES`` or one ``ERROR_VARIANTS`` entry)."""
    if variant is not None:
        if variant not in ERROR_VARIANTS:
            raise DtoError("variant")
        v_code, message, retryable = ERROR_VARIANTS[variant]
        return DeckError(v_code, message, retryable, provider_id, account_id)
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
class ApiEquivalent:
    """Additive API-equivalent summary of one leaf or aggregate (design §7). ``amount_pico_usd`` is a base-10
    integer string (authoritative, exact) or None when no leaf is priced; counters count provenance rows."""

    amount_pico_usd: Optional[str]
    priced_rows: int
    unpriced_rows: int
    proxy_rows: int
    partial: bool
    stale: bool

    def __post_init__(self) -> None:
        if self.amount_pico_usd is not None and (not isinstance(self.amount_pico_usd, str)
                                                 or not _PICO_RE.match(self.amount_pico_usd)):
            raise DtoError("api_equivalent.amount_pico_usd")
        for name in ("priced_rows", "unpriced_rows", "proxy_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DtoError(f"api_equivalent.{name}")
        _bool("api_equivalent.partial", self.partial)
        _bool("api_equivalent.stale", self.stale)
        if self.proxy_rows > self.priced_rows or (self.amount_pico_usd is None) != (self.priced_rows == 0) \
                or self.partial != (self.unpriced_rows > 0) or (self.stale and self.priced_rows == 0):
            raise DtoError("api_equivalent: inconsistent")


EMPTY_API_EQUIVALENT = ApiEquivalent(None, 0, 0, 0, False, False)


def api_usd(amount_pico_usd: Optional[str]) -> Optional[float]:
    """The display float of an integer pico-USD string (never used for sums)."""
    if amount_pico_usd is None:
        return None
    return float(Decimal(amount_pico_usd) / PICO)


@dataclass(frozen=True)
class UsageMetrics:
    """One aggregate of the baseline daily counters (``None`` = unavailable, never a computed zero)."""

    input_count: Optional[int]
    output_count: Optional[int]
    estimated_usd: Optional[float]
    sessions: Optional[int]
    api_calls: Optional[int]
    missing_fields: Tuple[str, ...]
    #: Hypothetical current base text API tariff (pricing annotation; None = no priced observation).
    api_equivalent_usd: Optional[float] = None
    api_equivalent: ApiEquivalent = EMPTY_API_EQUIVALENT

    def __post_init__(self) -> None:
        for name in ("input_count", "output_count", "sessions", "api_calls"):
            _integer(name, getattr(self, name))
        _number("estimated_usd", self.estimated_usd, minimum=0)
        fields_ = tuple(self.missing_fields)
        if list(fields_) != sorted(set(fields_)) or any(f not in USAGE_METRICS for f in fields_):
            raise DtoError("missing_fields")
        _api_pair(self.api_equivalent_usd, self.api_equivalent)


def _api_pair(usd: Any, api: Any) -> None:
    """``api_equivalent_usd`` must be the float image of the integer amount (null iff null)."""
    _number("api_equivalent_usd", usd, minimum=0)
    if not isinstance(api, ApiEquivalent):
        raise DtoError("api_equivalent")
    if (usd is None) != (api.amount_pico_usd is None):
        raise DtoError("api_equivalent_usd: null mismatch")
    if usd is not None and abs(usd - api_usd(api.amount_pico_usd)) > 1e-12:
        raise DtoError("api_equivalent_usd: amount mismatch")


@dataclass(frozen=True)
class RowPricing:
    """How one provenance row was priced (exactly eight wire fields; null metadata per ``PriceResolution``)."""

    catalog_key: Optional[str]
    method: str
    alias_kind: Optional[str]
    note: Optional[str]
    reason: Optional[str]
    source: Optional[str]
    fetched_at: Optional[str]
    stale: bool

    def __post_init__(self) -> None:
        if self.catalog_key is not None and (not isinstance(self.catalog_key, str)
                                             or not _CATALOG_KEY_RE.match(self.catalog_key)):
            raise DtoError("pricing.catalog_key")
        _enum("pricing.method", self.method, PRICING_METHODS)
        _enum("pricing.alias_kind", self.alias_kind, PRICING_ALIAS_KINDS, nullable=True)
        _str("pricing.note", self.note, nullable=True, max_len=512)
        _enum("pricing.reason", self.reason, PRICING_REASONS, nullable=True)
        _enum("pricing.source", self.source, PRICING_SOURCES, nullable=True)
        if self.fetched_at is not None and (not isinstance(self.fetched_at, str)
                                            or not _TS_FRAC_RE.match(self.fetched_at)):
            raise DtoError("pricing.fetched_at")
        _bool("pricing.stale", self.stale)
        if self.method == "none" and (self.catalog_key is not None or self.alias_kind is not None):
            raise DtoError("pricing: none method carries no identity")


NO_ROW_PRICING = RowPricing(None, "none", None, None, "no_catalog_match", None, None, False)


@dataclass(frozen=True)
class PricingInfo:
    """``data.pricing`` of a usage response: the frozen catalog identity and the explicit unpriced bucket."""

    catalog_revision: str
    origin: str
    warnings: Tuple[str, ...]
    unknown_row_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_revision, str) or not _SHA256_RE.match(self.catalog_revision):
            raise DtoError("pricing.catalog_revision")
        _enum("pricing.origin", self.origin, PRICING_ORIGINS)
        if list(self.warnings) != sorted(set(self.warnings)) or any(w not in PRICING_WARNINGS for w in self.warnings):
            raise DtoError("pricing.warnings")
        if list(self.unknown_row_ids) != sorted(set(self.unknown_row_ids)) \
                or not all(isinstance(r, str) and _ROW_ID_RE.match(r) for r in self.unknown_row_ids):
            raise DtoError("pricing.unknown_row_ids")


@dataclass(frozen=True)
class ProvenanceRow:
    """A daily aggregate for one ``(home, day, model, provider, attribution)``; never raw session content."""

    id: str
    day: str
    profile: str
    is_global: bool
    source: str
    model_id: Optional[str]
    provider_id: str
    attribution_source: str
    metrics: UsageMetrics
    pricing: RowPricing = NO_ROW_PRICING

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ROW_ID_RE.match(self.id):
            raise DtoError("row.id")
        if not isinstance(self.day, str) or not _DATE_RE.match(self.day):
            raise DtoError("row.day")
        Holder(self.profile, self.is_global)
        _enum("row.source", self.source, ("state.db",))
        _str("model_id", self.model_id, nullable=True, max_len=128)
        _str("provider_id", self.provider_id, max_len=128)
        if not self.provider_id:
            raise DtoError("row.provider_id")
        _enum("attribution_source", self.attribution_source, ATTRIBUTION_SOURCES)
        if not isinstance(self.metrics, UsageMetrics):
            raise DtoError("row.metrics")
        if not isinstance(self.pricing, RowPricing):
            raise DtoError("row.pricing")
        if (self.pricing.reason is None) != (self.metrics.api_equivalent.amount_pico_usd is not None):
            raise DtoError("row.pricing: reason must be null exactly when priced")


@dataclass(frozen=True)
class UsageGroup:
    id: str
    label: str
    series_id: str
    model_id: Optional[str]
    provider_ids: Tuple[str, ...]
    profiles: Tuple[Holder, ...]
    row_ids: Tuple[str, ...]
    totals: UsageMetrics
    is_other: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _GROUP_ID_RE.match(self.id):
            raise DtoError("group.id")
        _str("label", self.label, max_len=512)
        _str("series_id", self.series_id)
        if self.series_id != self.id and self.series_id != "profile_session_analytics":
            raise DtoError("group.series_id")
        _str("model_id", self.model_id, nullable=True, max_len=128)
        for seq in (self.provider_ids, self.row_ids):
            if list(seq) != sorted(set(seq)) or not all(isinstance(s, str) and s for s in seq):
                raise DtoError("group sets")
        if not all(isinstance(h, Holder) for h in self.profiles) \
                or list(self.profiles) != sorted(set(self.profiles), key=lambda h: (h.profile, h.is_global)):
            raise DtoError("group.profiles")
        if not isinstance(self.totals, UsageMetrics):
            raise DtoError("group.totals")
        _bool("is_other", self.is_other)
        if self.is_other != (self.id == "other") or (self.is_other and self.model_id is not None):
            raise DtoError("group.is_other")


@dataclass(frozen=True)
class SourceStatus:
    profile: str
    is_global: bool
    state: str

    def __post_init__(self) -> None:
        Holder(self.profile, self.is_global)
        _enum("state", self.state, SOURCE_STATES)


@dataclass(frozen=True)
class ProviderOption:
    id: str
    label: str

    def __post_init__(self) -> None:
        _str("id", self.id, max_len=128)
        _str("label", self.label, max_len=128)
        if not self.id or not self.label:
            raise DtoError("provider option")


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
    #: Matching leaf amounts of this day/group (pricing annotation); None = no priced leaf that day.
    api_equivalent_usd: Optional[float] = None
    api_equivalent: ApiEquivalent = EMPTY_API_EQUIVALENT

    def __post_init__(self) -> None:
        if not isinstance(self.day, str) or not _DATE_RE.match(self.day):
            raise DtoError("day")
        _integer("input_count", self.input_count)
        _integer("output_count", self.output_count)
        _number("estimated_usd", self.estimated_usd, minimum=0)
        _integer("sessions", self.sessions)
        _integer("api_calls", self.api_calls)
        _enum("confidence", self.confidence, CONFIDENCE_LABELS)
        _api_pair(self.api_equivalent_usd, self.api_equivalent)


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


def build_meta(request_id: str, generated_at: str, errors: list, schema_version: int = SCHEMA_VERSION) -> dict:
    return {
        "schema_version": schema_version,
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
    UsageMetrics, ProvenanceRow, UsageGroup, SourceStatus, ProviderOption, ApiEquivalent, RowPricing, PricingInfo,
)

# Envelope (§3) and per-endpoint ``data`` keys that are not DTO fields.
_ENVELOPE_KEYS = ("ok", "data", "error", "meta", "schema_version", "request_id", "generated_at", "partial", "errors")
_DATA_KEYS = ("cards", "inventory_state", "view", "window_days", "provider_id", "series", "reason", "entries",
              "mode", "dimension", "groups", "rows", "totals", "providers", "sources", "complete", "top_n", "pricing")
USAGE_DATA_KEYS = frozenset(("window_days", "mode", "dimension", "provider_id", "series", "groups", "rows", "totals",
                             "providers", "sources", "complete", "top_n", "reason", "pricing"))

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
    "state": _in(set(CARD_STATES) | set(SOURCE_STATES)),
    "reason": _in(set(REASONS) | set(PRICING_REASONS)),
    "code": _in(ERROR_CODES),
    "message": _in(set(ERROR_MESSAGES.values()) | {v[1] for v in ERROR_VARIANTS.values()}),
    "unit": _in(UNITS),
    "scope": _in(set(METER_SCOPES) | set(SERIES_SCOPES)),
    "source": _in(set(METER_SOURCES) | {"state.db"} | set(PRICING_SOURCES)),
    "plan_source": _in(PLAN_SOURCES),
    "auth_status": _in(AUTH_STATUSES),
    "kind": _in(KINDS),
    "capabilities": _in(CAPABILITIES),  # list items inherit the key
    "status": _in(HEALTH_STATUSES),
    "inventory_state": _in(("empty", "detected")),
    "view": _in(VIEWS),
    "mode": _in(VIEWS),
    "dimension": _in(DIMENSIONS),
    "attribution_source": _in(ATTRIBUTION_SOURCES),
    "profile": _match(_HOLDER_RE),
    "bucket": _in(("day",)),
    "timezone": _in(("server_local",)),
    "coverage": _in(("partial", "complete")),
    "method": _in(PRICING_METHODS),
    "alias_kind": _in(PRICING_ALIAS_KINDS),
    "origin": _in(PRICING_ORIGINS),
    "warnings": _in(PRICING_WARNINGS),  # list items inherit the key
    "unknown_row_ids": _match(_ROW_ID_RE),  # list items inherit the key
    "catalog_revision": _match(_SHA256_RE),
    "catalog_key": _match(_CATALOG_KEY_RE),
    "amount_pico_usd": _match(_PICO_RE),
    "fetched_at": _match(_TS_FRAC_RE),
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


def _api_from_wire(data: Any) -> ApiEquivalent:
    return ApiEquivalent(**_exact_keys("api_equivalent", data, ApiEquivalent))


def _point_from_wire(data: Any) -> UsagePoint:
    data = _exact_keys("point", data, UsagePoint)
    return UsagePoint(**{**data, "api_equivalent": _api_from_wire(data["api_equivalent"])})


def series_from_wire(data: Any) -> UsageTimeSeries:
    data = _exact_keys("series", data, UsageTimeSeries)
    if not isinstance(data["points"], list):
        raise DtoError("series: expected list")
    return UsageTimeSeries(**{
        **data,
        "period": _period_from_wire(data["period"]),
        "points": tuple(_point_from_wire(p) for p in data["points"]),
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


def _metrics_from_wire(data: Any) -> UsageMetrics:
    data = _exact_keys("metrics", data, UsageMetrics)
    if not isinstance(data["missing_fields"], list):
        raise DtoError("metrics: expected list")
    return UsageMetrics(**{**data, "missing_fields": tuple(data["missing_fields"]),
                           "api_equivalent": _api_from_wire(data["api_equivalent"])})


def _row_from_wire(data: Any) -> ProvenanceRow:
    data = _exact_keys("row", data, ProvenanceRow)
    return ProvenanceRow(**{**data, "metrics": _metrics_from_wire(data["metrics"]),
                            "pricing": RowPricing(**_exact_keys("pricing", data["pricing"], RowPricing))})


def _group_from_wire(data: Any) -> UsageGroup:
    data = _exact_keys("group", data, UsageGroup)
    if not all(isinstance(data[k], list) for k in ("provider_ids", "profiles", "row_ids")):
        raise DtoError("group: expected lists")
    return UsageGroup(**{
        **data,
        "provider_ids": tuple(data["provider_ids"]),
        "profiles": tuple(_holder_from_wire(h) for h in data["profiles"]),
        "row_ids": tuple(data["row_ids"]),
        "totals": _metrics_from_wire(data["totals"]),
    })


def usage_from_wire(data: Any) -> dict:
    """Validate (strictly, every key) an expanded usage ``data`` object and return its canonical rebuild."""
    if not isinstance(data, dict) or frozenset(data) != USAGE_DATA_KEYS:
        raise DtoError("usage: wire keys differ from schema")
    _enum("window_days", data["window_days"], WINDOWS)
    _enum("mode", data["mode"], VIEWS)
    _enum("dimension", data["dimension"], DIMENSIONS)
    _str("provider_id", data["provider_id"], nullable=True, max_len=128)
    _enum("reason", data["reason"], USAGE_REASONS, nullable=True)
    _bool("complete", data["complete"])
    if data["top_n"] != (TOP_N_MODELS if data["dimension"] == "model" else None) or isinstance(data["top_n"], bool):
        raise DtoError("top_n")
    for name in ("series", "groups", "rows", "providers", "sources"):
        if not isinstance(data[name], list):
            raise DtoError(f"usage.{name}: expected list")
    series = [to_plain(series_from_wire(s)) for s in data["series"]]
    groups = [to_plain(_group_from_wire(g)) for g in data["groups"]]
    rows = [to_plain(_row_from_wire(r)) for r in data["rows"]]
    totals = to_plain(_metrics_from_wire(data["totals"]))
    providers = [to_plain(ProviderOption(**_exact_keys("provider", p, ProviderOption))) for p in data["providers"]]
    sources = [to_plain(SourceStatus(**_exact_keys("source", s, SourceStatus))) for s in data["sources"]]
    pricing = _exact_keys("pricing", data["pricing"], PricingInfo)
    if not all(isinstance(pricing[k], list) for k in ("warnings", "unknown_row_ids")):
        raise DtoError("usage.pricing: expected lists")
    pricing = to_plain(PricingInfo(**{**pricing, "warnings": tuple(pricing["warnings"]),
                                      "unknown_row_ids": tuple(pricing["unknown_row_ids"])}))
    if [g["series_id"] for g in groups] != [s["id"] for s in series]:
        raise DtoError("usage: groups and series are not one-to-one")
    row_ids = [r["id"] for r in rows]
    if len(set(row_ids)) != len(row_ids):
        raise DtoError("usage: duplicate row ids")
    grouped = [rid for g in groups for rid in g["row_ids"]]
    if sorted(grouped) != sorted(row_ids):
        raise DtoError("usage: every row must belong to exactly one group")
    if pricing["unknown_row_ids"] != sorted(r["id"] for r in rows if r["metrics"]["api_equivalent"]["amount_pico_usd"] is None):
        raise DtoError("usage: pricing.unknown_row_ids does not match the unpriced rows")
    out = {**data, "series": series, "groups": groups, "rows": rows, "totals": totals, "providers": providers,
           "sources": sources, "pricing": pricing}
    if out != data:
        raise DtoError("usage: wire dict does not round-trip")
    return data


_META_KEYS = ("schema_version", "request_id", "generated_at", "partial", "errors")
_DATA_SHAPES = {
    frozenset(("cards", "inventory_state", "view")): "subscriptions",
    USAGE_DATA_KEYS: "usage",
    frozenset(("entries",)): "health",
}


def check_envelope(body: Any, endpoint: Optional[str] = None) -> Any:
    """Assert ``body`` is exactly a §3 envelope with schema-exact ``data``.

    Used at the coordinator boundary *after* masking: if masking (or anything
    else) altered a key or produced an invalid enum/format, this raises
    ``DtoError`` and the caller answers with the existing ``internal_error``
    model instead of a malformed 200. ``meta.schema_version`` is 2 for usage and
    1 for every other endpoint; a failure envelope with no ``endpoint`` accepts either. Returns ``body``.
    """
    if not isinstance(body, dict) or sorted(body) != sorted(("ok", "data", "error", "meta")):
        raise DtoError("envelope: wire keys differ from schema")
    meta = body["meta"]
    if not isinstance(meta, dict) or sorted(meta) != sorted(_META_KEYS):
        raise DtoError("envelope.meta: wire keys differ from schema")
    data = body["data"]
    if endpoint is not None:
        expected = {USAGE_SCHEMA_VERSION} if endpoint == "usage" else {SCHEMA_VERSION}
    elif body["ok"] is True and isinstance(data, dict):
        expected = {USAGE_SCHEMA_VERSION} if frozenset(data) == USAGE_DATA_KEYS else {SCHEMA_VERSION}
    else:
        expected = {SCHEMA_VERSION, USAGE_SCHEMA_VERSION}
    if type(meta["schema_version"]) is not int or meta["schema_version"] not in expected \
            or not isinstance(meta["request_id"], str) or not _UUID_RE.match(meta["request_id"]):
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
        usage_from_wire(data)
    return body
