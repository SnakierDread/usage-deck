"""Cloud provider adapters (DESIGN.md §1.3).

Nous has no verified quota capability: it is opaque by policy, never probed over the network,
and never sends credentials anywhere. OpenAI Codex reads exactly one route, the ratified
ChatGPT-backend workspace spend control (DESIGN.md §9.2, docs/decisions/codex-wham-usage.md):
the selected OAuth access token and its same-token routing claims go to that one guarded GET
target and nowhere else. Anthropic pulls its meters live from one OAuth usage route (DESIGN.md
§9.1.2) and falls back to *previously captured* unified rate-limit headers (a local file, never a
request). OpenRouter reads exactly one documented endpoint.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from ..captures import UNIFIED_PREFIX, Capture, parse_reset_epoch, parse_utilization
from ..models.dto import UNKNOWN_PERIOD, Period, ProbeResult, UsageMeter, UsageResult, fmt_ts, make_error
from .base import (
    BaseAdapter,
    error_for_status,
    error_for_transport,
    finite_number,
    parse_json_body,
)
from .credentials import OK, REAUTH, UNAVAILABLE, StoredRow, TokenResolution
from .transport import AllowedTarget, TransportError

log = logging.getLogger("usage_deck.adapters.cloud")

# DESIGN.md §9.1.2: the sole Anthropic egress (live OAuth usage pull). Granted only to the anthropic context.
ANTHROPIC_PROVIDER_ID = "anthropic"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_TARGET = AllowedTarget("https", "api.anthropic.com", 443, ("/api/oauth/usage",))
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_TARGET = AllowedTarget("https", "openrouter.ai", 443, ("/api/v1/key",))

# DESIGN.md §9.2: the sole Codex egress. Granted only to the selected openai-codex context.
CODEX_PROVIDER_ID = "openai-codex"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TARGET = AllowedTarget("https", "chatgpt.com", 443, ("/backend-api/wham/usage",))
CODEX_USER_AGENT = "HermesAgent/usage-deck"
CODEX_ORIGINATOR = "hermes-agent"
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
_HEADER_CLAIM_RE = re.compile(r"[\x21-\x7e]{1,256}")  # printable ASCII, no whitespace/control
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_DECIMAL_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_EPOCH_RE = re.compile(r"[0-9]+(?:\.0+)?")
_MAX_TOKEN_CHARS = 16384
_MAX_NUMBER_CHARS = 64


@dataclass(frozen=True)
class CodexRouting:
    """Private routing claims of one Codex access token. Never serialized, logged or repr'd."""

    account_id: str
    residency: Optional[str] = None

    def __repr__(self) -> str:
        return "CodexRouting([REDACTED])"

    __str__ = __repr__

    def masking_values(self) -> frozenset:
        """Values to add to the private masking context (DESIGN.md §9.2 term 2)."""
        values = {self.account_id}
        if self.residency is not None:  # every private routing value joins, whatever its length
            values.add(self.residency)
        return frozenset(values)


def codex_routing(token: Any) -> Optional[CodexRouting]:
    """Account/residency claims of *this* access token for the Codex request headers, else None.

    Decodes only the ``https://api.openai.com/auth`` claim object, exactly as Hermes'
    ``codex_account_headers`` does: ``chatgpt_account_id`` -> account header;
    ``chatgpt_data_residency`` (fallback ``chatgpt_compute_residency``) -> optional residency header.
    Decoding is not signature verification. Anything missing/malformed/hostile (non-printable or
    over-long header text, non-string values, a token that is not three base64url segments) yields
    None, which means: make no request. Absent (null/empty) residency is simply omitted.
    """
    if not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 3 or not _B64URL_RE.fullmatch(parts[0]) or not _B64URL_RE.fullmatch(parts[1]) \
            or (parts[2] and not _B64URL_RE.fullmatch(parts[2])):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, RecursionError):  # includes binascii.Error / UnicodeDecodeError
        return None
    auth = payload.get(_OPENAI_AUTH_CLAIM) if isinstance(payload, dict) else None
    if not isinstance(auth, dict):
        return None
    account = auth.get("chatgpt_account_id")
    if not isinstance(account, str) or not _HEADER_CLAIM_RE.fullmatch(account):
        return None
    residency = auth.get("chatgpt_data_residency")
    if residency is None or residency == "":
        residency = auth.get("chatgpt_compute_residency")
    if residency is None or residency == "":
        return CodexRouting(account, None)
    if not isinstance(residency, str) or not _HEADER_CLAIM_RE.fullmatch(residency):
        return None  # present but not a safe header value: no request
    return CodexRouting(account, residency)


def codex_request_headers(token: str, routing: CodexRouting) -> dict:
    """The complete, fixed header set of the one Codex request (DESIGN.md §9.2 term 2); nothing else."""
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": CODEX_USER_AGENT,
        "originator": CODEX_ORIGINATOR,
        "ChatGPT-Account-ID": routing.account_id,
    }
    if routing.residency is not None:
        headers["x-openai-internal-codex-residency"] = routing.residency
    return headers


def _credit_amount(value: Any) -> Optional[float]:
    """Finite nonnegative JSON number or decimal numeric string -> float; null -> None; else ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean")
    if isinstance(value, str):
        if len(value) > _MAX_NUMBER_CHARS or not _DECIMAL_RE.fullmatch(value):
            raise ValueError("malformed numeric string")
        number = float(value)
    elif isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            raise ValueError("out of range") from None
    else:
        raise ValueError("not a number")
    if not math.isfinite(number) or number < 0:
        raise ValueError("non-finite or negative")
    return number + 0.0  # normalises -0.0


def _reset_instant(value: Any) -> Optional[datetime]:
    """``reset_at`` epoch seconds (finite nonnegative integral number or decimal string) -> UTC datetime."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean")
    if isinstance(value, str):
        if len(value) > 24 or not _EPOCH_RE.fullmatch(value):
            raise ValueError("malformed epoch")
        epoch = int(value.split(".", 1)[0])
    elif isinstance(value, int):
        epoch = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("non-integral epoch")
        epoch = int(value)
    else:
        raise ValueError("not an epoch")
    if epoch < 0:
        raise ValueError("negative epoch")
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("epoch out of range") from None


def codex_spend_meter(payload: dict, now: datetime) -> Optional[UsageMeter]:
    """The single ``workspace_spend`` meter from a wham/usage body, or None when unsupported/expired.

    Raises ``ValueError`` for a wrong-shaped or invalid present field (-> malformed_response).
    Unsupported shapes (no spend_control / individual_limit, other source or unit, no numeric
    observation) and an already-reset window return None (-> opaque/source_unavailable). Nothing is
    synthesized: a null stays null, rounded percentages and every other response field are ignored.
    """
    spend = payload.get("spend_control")
    if spend is None:
        return None
    if not isinstance(spend, dict):
        raise ValueError("spend_control")
    individual = spend.get("individual_limit")
    if individual is None:
        return None
    if not isinstance(individual, dict):
        raise ValueError("individual_limit")
    if individual.get("source") != "workspace_spend_controls" or individual.get("unit") != "credit":
        return None
    used = _credit_amount(individual.get("used"))
    limit = _credit_amount(individual.get("limit"))
    remaining = _credit_amount(individual.get("remaining"))
    reset = _reset_instant(individual.get("reset_at"))
    if used is None and limit is None and remaining is None:
        return None
    if reset is not None and reset <= now:
        return None  # the reported window already ended: never present it (or zero) as current
    return UsageMeter(
        id="workspace_spend",
        label="Workspace spend limit (monthly)",
        value=used,
        limit=limit,
        remaining=remaining,
        unit="credit",
        scope="provider_account",
        period=Period(None, None, "monthly_workspace"),
        confidence="exact",
        source="chatgpt_wham_usage",
        observed_at=fmt_ts(now),
        resets_at=fmt_ts(reset),
    )


_MAX_WINDOW_SECONDS = 10 * 365 * 86400

#: (window key, meter id, label prefix) for the two plan rate-limit windows (DESIGN.md §9.2.1).
CODEX_RATE_WINDOWS = (
    ("primary_window", "rate_limit_primary", "Plan usage limit"),
    ("secondary_window", "rate_limit_secondary", "Secondary plan usage limit"),
)


def _window_seconds(value: Any) -> Optional[int]:
    """``limit_window_seconds``: positive integral JSON number or decimal string; null -> None; else ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean")
    if isinstance(value, str):
        if len(value) > 24 or not _EPOCH_RE.fullmatch(value):
            raise ValueError("malformed window")
        seconds = int(value.split(".", 1)[0])
    elif isinstance(value, int):
        seconds = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("non-integral window")
        seconds = int(value)
    else:
        raise ValueError("not a window")
    if seconds <= 0 or seconds > _MAX_WINDOW_SECONDS:
        raise ValueError("window out of range")
    return seconds


def _window_names(seconds: Optional[int]) -> Tuple[str, str]:
    """(period label, human phrase) for a reported window length; whole units only, never rounded."""
    if seconds is None:
        return "unknown", "window"
    for size, tag, word in ((86400, "d", "day"), (3600, "h", "hour"), (60, "m", "minute")):
        if seconds % size == 0:
            count = seconds // size
            return f"rolling_{count}{tag}", f"{count}-{word} window"
    return f"rolling_{seconds}s", f"{seconds}-second window"


def codex_rate_limit_meters(payload: dict, now: datetime) -> List[UsageMeter]:
    """Percent meters from ``rate_limit.primary_window`` / ``secondary_window`` (DESIGN.md §9.2.1).

    This is the plan shape without a workspace spend control (e.g. ``go``): ``used_percent`` of a
    fixed-length window plus the provider's ``reset_at`` instant. ``limit_reached: true`` is the
    provider's own statement that the cap was hit: the primary meter reports ``remaining = 0`` (and
    keeps the observed ``used_percent``: nothing is clamped or estimated up). A window whose reset
    already passed is not presented as current. ``rate_limit`` null/absent or without any of
    allowed/limit_reached/windows is an unsupported shape (no meters). Wrong-shaped present fields
    raise ``ValueError`` (-> malformed_response).
    """
    rate = payload.get("rate_limit")
    if rate is None:
        return []
    if not isinstance(rate, dict):
        raise ValueError("rate_limit")
    if not any(key in rate for key in ("allowed", "limit_reached", "primary_window", "secondary_window")):
        return []
    for flag in ("allowed", "limit_reached"):
        if rate.get(flag) is not None and not isinstance(rate[flag], bool):
            raise ValueError(flag)
    reached = rate.get("limit_reached") is True
    meters: List[UsageMeter] = []
    for index, (key, meter_id, prefix) in enumerate(CODEX_RATE_WINDOWS):
        window = rate.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError(key)
        used = _credit_amount(window.get("used_percent"))
        seconds = _window_seconds(window.get("limit_window_seconds"))
        reset = _reset_instant(window.get("reset_at"))
        is_reached = reached and index == 0
        if used is None and not is_reached:
            continue  # no observation: never a zero
        if reset is not None and reset <= now:
            continue  # that window already ended: its current usage is unknown
        meters.append(_rate_meter(meter_id, prefix, used, seconds, reset, is_reached, now))
    if reached and not meters and rate.get("primary_window") is None:
        meters.append(_rate_meter("rate_limit_primary", CODEX_RATE_WINDOWS[0][2], None, None, None, True, now))
    return meters


def _rate_meter(meter_id, prefix, used, seconds, reset, reached, now) -> UsageMeter:
    period_label, phrase = _window_names(seconds)
    return UsageMeter(
        id=meter_id,
        label=f"{prefix} ({phrase})" if seconds is not None else prefix,
        value=used,
        limit=100.0,
        remaining=0.0 if reached else None,
        unit="percent",
        scope="provider_account",
        period=Period(None, None, period_label),
        confidence="exact",
        source="chatgpt_wham_usage",
        observed_at=fmt_ts(now),
        resets_at=fmt_ts(reset),
    )


def codex_meters(payload: dict, now: datetime) -> List[UsageMeter]:
    """Every supported meter of one wham/usage body; the spend-control meter always comes first.

    The two shapes are disjoint per plan but may both be present: spend-control first, rate-limit
    windows after it. When a spend meter exists, a malformed ``rate_limit`` is dropped rather than
    discarding the good primary meter; otherwise it is a malformed response.
    """
    meters: List[UsageMeter] = []
    spend = codex_spend_meter(payload, now)
    if spend is not None:
        meters.append(spend)
    try:
        meters.extend(codex_rate_limit_meters(payload, now))
    except ValueError:
        if spend is None:
            raise
    return meters


class OpaqueCloudAdapter(BaseAdapter):
    """Local inventory + analytics only; subscription quota is opaque."""

    adapter_name = "opaque_cloud"
    source_label = "local_inventory"

    def _do_probe(self) -> ProbeResult:
        # No network: there is no documented, verified quota endpoint to probe.
        return self._probe_result(False, None, self._capabilities(), reason="unsupported")

    def _do_usage(self, window: int) -> UsageResult:
        return UsageResult(meters=(), series=(), confidence="opaque", reason="unsupported")


class NousAdapter(OpaqueCloudAdapter):
    adapter_name = "NousAdapter"


class OpenAICodexAdapter(OpaqueCloudAdapter):
    """ChatGPT workspace spend control from the one ratified route (DESIGN.md §9.2).

    One guarded GET per snapshot (probe is local-only and the result is memoized across windows).
    Sends only the selected access token as Bearer plus the same token's account/residency routing
    claims; never refreshes, retries, follows redirects or tries another route. Two disjoint
    shapes are mapped (spend-control first): ``workspace_spend`` for the recorded
    ``workspace_spend_controls``/``credit`` shape, and percent window meters from
    ``rate_limit.primary_window``/``secondary_window`` for plans without a spend control (§9.2.1).
    """

    adapter_name = "OpenAICodexAdapter"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._snapshot: Optional[UsageResult] = None

    @property
    def source_label(self) -> str:  # type: ignore[override]
        snapshot = self._snapshot
        return "chatgpt_wham_usage" if snapshot is not None and snapshot.meters else "local_inventory"

    def _routing(self) -> Optional[CodexRouting]:
        credential = self.ctx.credential
        return codex_routing(credential.reveal()) if credential else None

    def _do_probe(self) -> ProbeResult:
        # Local-only: no request is spent to obtain a badge; get_usage makes the one request.
        if not self.ctx.live_source:  # machine-only inventory: not the selected profile's credential
            return self._probe_result(False, None, self._capabilities(), reason="source_unavailable")
        if not self.ctx.credential:
            return self._probe_result(False, None, self._capabilities(), reason="credentials_missing")
        if self._routing() is None:
            return self._probe_result(False, None, self._capabilities(), reason="source_unavailable")
        return self._probe_result(True, None, self._capabilities("quota"))

    def _do_usage(self, window: int) -> UsageResult:
        if self._snapshot is None:
            self._snapshot = self._fetch()
        return self._snapshot

    def _fetch(self) -> UsageResult:
        ctx = self.ctx
        if not ctx.live_source:
            # Machine-only inventory: another profile's credential is never used for an outbound request.
            return UsageResult(reason="source_unavailable")
        if not ctx.credential:
            return UsageResult(reason="credentials_missing")
        token = ctx.credential.reveal()
        routing = codex_routing(token)
        if routing is None:
            return UsageResult(reason="source_unavailable")  # no request with unusable routing claims
        headers = codex_request_headers(token, routing)
        try:
            response = ctx.transport.get(CODEX_USAGE_URL, headers=headers)
        except TransportError as exc:
            return UsageResult(error=error_for_transport(exc, ctx.provider_id, ctx.account_id))
        if response.status == 401:
            # Credential presence stays "available"; no refresh, no retry (DESIGN.md §9.2).
            return UsageResult(error=make_error("auth_required", ctx.provider_id, ctx.account_id),
                               reason="permission_denied")
        if response.status in (403, 404):
            return UsageResult(reason="permission_denied")
        failure = error_for_status(response.status, ctx.provider_id, ctx.account_id)
        if failure is not None:
            return UsageResult(error=failure)
        malformed = UsageResult(error=make_error("malformed_response", ctx.provider_id, ctx.account_id))
        try:
            payload = parse_json_body(response)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return malformed
        if not isinstance(payload, dict):
            return malformed
        try:
            meters = codex_meters(payload, ctx.now())
        except ValueError:
            return malformed
        if not meters:
            return UsageResult(confidence="opaque", reason="source_unavailable")
        return UsageResult(meters=tuple(meters), confidence="exact")


#: (header window tag, meter id, label, period label) for the two subscription windows.
ANTHROPIC_WINDOWS = (
    ("5h", "session_5h", "Session (5-hour window)", "rolling_5h"),
    ("7d", "weekly_7d", "Weekly (7-day window)", "rolling_7d"),
)


def anthropic_meters(capture: Capture, now) -> List[UsageMeter]:
    """Meters from one captured response. Never extrapolates.

    ``value`` is the reported utilization as a percentage of the window (may exceed 100),
    ``resets_at`` the reported reset instant, ``observed_at`` the time of the call that returned
    the headers. A window whose reported reset is already in the past no longer describes the
    current window, so it yields no meter (its utilization since the reset is unknown).
    """
    meters: List[UsageMeter] = []
    for tag, meter_id, label, period_label in ANTHROPIC_WINDOWS:
        utilization = parse_utilization(capture.headers.get(f"{UNIFIED_PREFIX}{tag}-utilization"))
        if utilization is None:
            continue
        reset = parse_reset_epoch(capture.headers.get(f"{UNIFIED_PREFIX}{tag}-reset"))
        if reset is not None and reset <= now:
            continue
        meters.append(UsageMeter(
            id=meter_id,
            label=label,
            value=round(utilization * 100.0, 4),
            limit=100.0,
            remaining=None,
            unit="percent",
            scope="provider_account",
            period=Period(None, None, period_label),
            confidence="exact",
            source="anthropic_headers",
            observed_at=fmt_ts(capture.observed_at),
            resets_at=fmt_ts(reset),
        ))
    return meters


#: (response window key(s), meter id, label, period label) for the live ``/api/oauth/usage`` body. The
#: recorded body (DESIGN.md §9.1.2) names the windows ``five_hour`` / ``seven_day``; ``week`` is accepted as
#: the alias the ratifying card used. Tuple order is card order (shortest window first).
ANTHROPIC_USAGE_WINDOWS = (
    (("five_hour",), "session_5h", "Session (5-hour window)", "rolling_5h"),
    (("seven_day", "week"), "weekly_7d", "Weekly (7-day window)", "rolling_7d"),
)


def _oauth_utilization(value: Any) -> Optional[float]:
    """``utilization`` of a live window: a finite nonnegative JSON number already in PERCENT (7.0 = 7 %).

    null -> None (no observation); a boolean, string or negative/non-finite number raises ValueError.
    The header capture reports a fraction; this endpoint does not, so nothing is multiplied here.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("utilization")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("utilization")
    return number + 0.0


def _oauth_reset(value: Any) -> Optional[datetime]:
    """``resets_at``: RFC 3339 / ISO 8601 instant string (offset or Z; naive = UTC); null -> None."""
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise ValueError("resets_at")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("resets_at") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def anthropic_usage_meters(payload: dict, now: datetime) -> List[UsageMeter]:
    """Meters from one live ``/api/oauth/usage`` body: ``five_hour`` then ``seven_day`` (DESIGN.md §9.1.2).

    ``value`` is the reported ``utilization`` percent (may exceed 100, never clamped); ``observed_at`` is the
    poll time; ``resets_at`` the provider's own reset instant. A window that is null, carries no
    utilization (never a zero) or whose reset is at/before ``now`` yields NO meter. A present
    wrong-shaped field raises ``ValueError`` (-> malformed_response). Every other field of the body
    (``extra_usage``, ``limits``, scoped/promotional windows, dollars) is ignored.
    """
    meters: List[UsageMeter] = []
    for keys, meter_id, label, period_label in ANTHROPIC_USAGE_WINDOWS:
        window = next((payload[k] for k in keys if payload.get(k) is not None), None)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError(keys[0])
        utilization = _oauth_utilization(window.get("utilization"))
        reset = _oauth_reset(window.get("resets_at"))
        if utilization is None:
            continue
        if reset is not None and reset <= now:
            continue  # that window already ended: its current usage is unknown, never shown as zero
        meters.append(UsageMeter(
            id=meter_id,
            label=label,
            value=utilization,
            limit=100.0,
            remaining=None,
            unit="percent",
            scope="provider_account",
            period=Period(None, None, period_label),
            confidence="exact",
            source="anthropic_oauth_usage",
            observed_at=fmt_ts(now),
            resets_at=fmt_ts(reset),
        ))
    return meters


class AnthropicAdapter(OpaqueCloudAdapter):
    """Session / weekly utilization: live OAuth usage pull first, captured headers as the fallback.

    Pull (DESIGN.md §9.1.2): one guarded ``GET https://api.anthropic.com/api/oauth/usage`` per snapshot
    with the selected persisted OAuth access token (``Authorization: Bearer`` + the OAuth beta header and
    nothing else); read on every ``/subscriptions`` build, never from a timer. The bearer is never read
    off the stored row by this adapter: it comes from the token resolver (``adapters.credentials``), which
    asks Hermes' own credential pool and so refreshes an expired/expiring access token exactly as
    ``hermes auth refresh`` does (§9.1.1 credential lifecycle). One request, no retry, no redirects, no other
    route. A dead stored login (refresh refused) is ``auth_status: rejected`` + the re-auth hint, no request.
    Fallback: previously captured ``anthropic-ratelimit-unified-*`` headers
    (§9.1), used only when the pull is unavailable (transport failure, 429/5xx, 403/404, unreadable body,
    a transient refresh failure) and only for windows that are still current by the capture's own reset;
    otherwise the window is hidden (never estimated). A 401 is the credential's verdict:
    ``auth_status: rejected`` and no fallback.
    """

    adapter_name = "AnthropicAdapter"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._snapshot: Optional[UsageResult] = None
        self._rejected = False
        self._verdict = False  # the endpoint itself answered (401 / 403 / 404 / 200): provenance of a meterless card

    @property
    def source_label(self) -> str:  # type: ignore[override]
        snapshot = self._snapshot
        if snapshot is not None and snapshot.meters:
            return snapshot.meters[0].source  # the one source that produced the meters (never mixed)
        if self._verdict:
            return "anthropic_oauth_usage"  # the live endpoint answered with no meter: it is still the source
        return "anthropic_headers" if self._capture() is not None else "local_inventory"

    def credential_rejected(self) -> bool:
        return self._rejected

    def _capture(self) -> Optional[Capture]:
        store = self.ctx.captures
        if store is None or not self.ctx.credential_refs:
            return None
        return store.latest("anthropic", self.ctx.credential_refs, now=self.ctx.now())

    def _can_pull(self) -> bool:
        ctx = self.ctx
        # Machine-only inventory (not the selected profile's credential, no §13 exemption) makes no request.
        # No resolver = no way to obtain a bearer: the adapter never reads the stored token itself (§9.1.1).
        return bool(ctx.live_source and ctx.credential and ctx.oauth_credential and ctx.token_resolver is not None)

    def _do_probe(self) -> ProbeResult:
        # Local-only: no request is spent to obtain a badge; get_usage makes the one request.
        if self._can_pull() or self._capture() is not None:
            return self._probe_result(True, None, self._capabilities("quota"))
        return self._probe_result(False, None, self._capabilities(), reason="unsupported")

    def _do_usage(self, window: int) -> UsageResult:
        if self._snapshot is None:
            self._snapshot = self._resolve()
        return self._snapshot

    def _resolve(self) -> UsageResult:
        pulled, unavailable = self._pull() if self._can_pull() else (None, False)
        if pulled is not None and (pulled.meters or not unavailable):
            return pulled  # live meters, or a definitive live verdict (401 / no current window): never overridden
        fallback = self._from_capture()
        if fallback is not None:
            return fallback
        if pulled is not None:
            return pulled  # endpoint unavailable and no current captured window: honest error, never an estimate
        reason = "unsupported" if self._capture() is None else "source_unavailable"
        return UsageResult(meters=(), series=(), confidence="opaque", reason=reason)

    def _from_capture(self) -> Optional[UsageResult]:
        capture = self._capture()
        if capture is None:
            return None
        meters = anthropic_meters(capture, self.ctx.now())
        if not meters:
            return None
        return UsageResult(meters=tuple(meters), series=(), confidence="exact")

    def _bearer(self) -> TokenResolution:
        """The one door to the OAuth bearer: core's credential machinery (refreshes an expiring row)."""
        ctx = self.ctx
        row = StoredRow(token=ctx.credential, expires_at=ctx.credential_expires_at, row_id=ctx.credential_row_id,
                        may_refresh=not ctx.cross_profile)
        try:
            return ctx.token_resolver.resolve(row, ctx.now())
        except Exception as exc:  # noqa: BLE001 - a resolver bug is "cannot resolve", never a crash or a raw read
            log.warning("usage_deck: anthropic token resolver failed (%s)", type(exc).__name__)
            return TokenResolution(UNAVAILABLE)

    def _pull(self) -> Tuple[UsageResult, bool]:
        """One guarded GET. Returns ``(result, unavailable)``; ``unavailable`` allows the header fallback."""
        ctx = self.ctx
        resolution = self._bearer()
        if resolution.status == REAUTH:
            # The stored login is dead (refresh refused / nothing to refresh with): no request is made, the
            # card says so with the repair command. Definitive, like a 401: no older data in its place.
            self._rejected = True
            return UsageResult(error=make_error("auth_required", ctx.provider_id, ctx.account_id,
                                                variant="anthropic_reauth"), reason="permission_denied"), False
        if resolution.status != OK or resolution.token is None:
            return UsageResult(error=make_error("upstream_unavailable", ctx.provider_id, ctx.account_id,
                                                variant="anthropic_refresh_unavailable"),
                               reason="source_unavailable"), True
        headers = {
            "Authorization": "Bearer " + resolution.token.reveal(),
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": CODEX_USER_AGENT,
        }
        try:
            response = ctx.transport.get(ANTHROPIC_USAGE_URL, headers=headers)
        except TransportError as exc:
            return UsageResult(error=error_for_transport(exc, ctx.provider_id, ctx.account_id)), True
        if response.status == 401:
            # The bearer was sent and refused (after core had the chance to refresh it): no retry, no older
            # data in its place (§9.1.2). The repair is a new login.
            self._rejected = True
            self._verdict = True
            return UsageResult(error=make_error("auth_required", ctx.provider_id, ctx.account_id,
                                                variant="anthropic_http_401"),
                               reason="permission_denied"), False
        if response.status == 404:
            # Credential/endpoint error, told apart from 401 and 429 (§9.1.2 taxonomy). A current capture may
            # still answer for the card (the provider's own number); otherwise this error is the card.
            self._verdict = True
            return UsageResult(error=make_error("permission_denied", ctx.provider_id, ctx.account_id,
                                                variant="anthropic_http_404"), reason="permission_denied"), True
        if response.status == 403:
            self._verdict = True
            return UsageResult(reason="permission_denied"), True  # e.g. a token without the profile scope
        if response.status == 429:
            # Retry later: the source is unavailable, the credential is NOT rejected.
            return UsageResult(error=make_error("upstream_unavailable", ctx.provider_id, ctx.account_id,
                                                variant="anthropic_http_429"), reason="source_unavailable"), True
        failure = error_for_status(response.status, ctx.provider_id, ctx.account_id)
        if failure is not None:
            return UsageResult(error=failure), True
        malformed = (UsageResult(error=make_error("malformed_response", ctx.provider_id, ctx.account_id)), True)
        try:
            payload = parse_json_body(response)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return malformed
        if not isinstance(payload, dict):
            return malformed
        try:
            meters = anthropic_usage_meters(payload, ctx.now())
        except ValueError:
            return malformed
        if not meters:
            self._verdict = True  # a definitive 200 that names no current window
            return UsageResult(confidence="opaque", reason="source_unavailable"), False
        return UsageResult(meters=tuple(meters), confidence="exact"), False


class OpenRouterAdapter(BaseAdapter):
    adapter_name = "OpenRouterAdapter"
    source_label = "openrouter_key"
    _rejected = False

    def credential_rejected(self) -> bool:
        return self._rejected

    def _do_probe(self) -> ProbeResult:
        # Capability probe only: a missing key is reported by get_usage, and
        # we do not spend a request just to obtain a badge.
        return self._probe_result(True, None, self._capabilities("usage"))

    def _do_usage(self, window: int) -> UsageResult:
        ctx = self.ctx
        if not ctx.live_source:
            # Machine-only inventory: another profile's credential is never used for an outbound request.
            return UsageResult(reason="source_unavailable")
        if not ctx.credential:
            return UsageResult(reason="credentials_missing")
        headers = {
            "Authorization": "Bearer " + ctx.credential.reveal(),
            "Accept": "application/json",
        }
        try:
            response = ctx.transport.get(OPENROUTER_KEY_URL, headers=headers)
        except TransportError as exc:
            return UsageResult(error=error_for_transport(exc, ctx.provider_id, ctx.account_id))
        observed_at = ctx.now_ts()
        if response.status in (403, 404):
            return UsageResult(reason="permission_denied")
        failure = error_for_status(response.status, ctx.provider_id, ctx.account_id)
        if failure is not None:
            self._rejected = response.status == 401  # the bearer above was sent and refused
            return UsageResult(error=failure)
        malformed = UsageResult(error=make_error("malformed_response", ctx.provider_id, ctx.account_id))
        try:
            payload = parse_json_body(response)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return malformed
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return malformed

        fields = {}
        for name in ("usage", "limit", "limit_remaining"):
            raw = data.get(name)
            if raw is None:
                fields[name] = None
                continue
            number = finite_number(raw)
            if number is None:
                return malformed
            if name in ("usage", "limit") and number < 0:
                return malformed  # negative usage/limit is invalid; negative remaining = overspend
            fields[name] = number
        if all(v is None for v in fields.values()):
            return UsageResult(confidence="opaque", reason="unsupported")

        meter = UsageMeter(
            id="api_spend",
            label="API credential spend",
            value=fields["usage"],
            limit=fields["limit"],
            remaining=fields["limit_remaining"],
            unit="usd",
            scope="api_credential",
            period=UNKNOWN_PERIOD,
            confidence="exact",
            source="openrouter_key",
            observed_at=observed_at,
        )
        return UsageResult(meters=(meter,), confidence="exact")
