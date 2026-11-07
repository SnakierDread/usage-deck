"""Cloud provider adapters (DESIGN.md §1.3).

Nous and OpenAI Codex have no verified quota capability: they are opaque by policy,
never probed over the network, and never send OAuth credentials anywhere (Codex: recon
t_7dce2222 located a ChatGPT-backend usage endpoint by reading Hermes source only; no live
response was verified, so it is not called -- DESIGN.md §9). Anthropic reads
*previously captured* unified rate-limit headers from a local file (never a request).
OpenRouter reads exactly one documented endpoint.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

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
    adapter_name = "OpenAICodexAdapter"


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
