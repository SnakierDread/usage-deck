"""Cloud provider adapters (DESIGN.md §1.3).

Nous has no verified quota capability: it is opaque by policy, never probed over the network,
and never sends credentials anywhere. OpenAI Codex reads exactly one route, the ratified
ChatGPT-backend workspace spend control (DESIGN.md §9.2, docs/decisions/codex-wham-usage.md):
the selected OAuth access token and its same-token routing claims go to that one guarded GET
target and nowhere else. Anthropic reads *previously captured* unified rate-limit headers from
a local file (never a request). OpenRouter reads exactly one documented endpoint.
"""

from __future__ import annotations

import base64
import json
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
from .transport import AllowedTarget, TransportError

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
#: Residency values shorter than this are not added to the masking context: a two-letter region
#: code is a routing hint, not a secret, and masking it would corrupt unrelated text (plan
#: "business" for region "us"). The account id is always masked.
_MIN_MASKED_RESIDENCY = 4


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
        if self.residency is not None and len(self.residency) >= _MIN_MASKED_RESIDENCY:
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
    claims; never refreshes, retries, follows redirects or tries another route. Exactly one meter,
    ``workspace_spend``, and only for the recorded ``workspace_spend_controls``/``credit`` shape.
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
            meter = codex_spend_meter(payload, ctx.now())
        except ValueError:
            return malformed
        if meter is None:
            return UsageResult(confidence="opaque", reason="source_unavailable")
        return UsageResult(meters=(meter,), confidence="exact")


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


class AnthropicAdapter(OpaqueCloudAdapter):
    """Session / weekly utilization from captured ``anthropic-ratelimit-unified-*`` headers.

    No request is made. Without a capture for this credential the card is opaque, exactly as
    before. Freshness is the capture's own call timestamp.
    """

    adapter_name = "AnthropicAdapter"

    @property
    def source_label(self) -> str:  # type: ignore[override]
        return "anthropic_headers" if self._capture() is not None else "local_inventory"

    def _capture(self) -> Optional[Capture]:
        store = self.ctx.captures
        if store is None or not self.ctx.row_ids:
            return None
        return store.latest("anthropic", self.ctx.row_ids)

    def _do_probe(self) -> ProbeResult:
        capture = self._capture()
        if capture is None:
            return self._probe_result(False, None, self._capabilities(), reason="unsupported")
        return self._probe_result(True, None, self._capabilities("quota"))

    def _do_usage(self, window: int) -> UsageResult:
        capture = self._capture()
        if capture is None:
            return UsageResult(meters=(), series=(), confidence="opaque", reason="unsupported")
        meters = anthropic_meters(capture, self.ctx.now())
        if not meters:
            # Capture exists but carries no current-window utilization (expired window / no unified headers).
            return UsageResult(meters=(), series=(), confidence="opaque", reason="source_unavailable")
        return UsageResult(meters=tuple(meters), series=(), confidence="exact")


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
