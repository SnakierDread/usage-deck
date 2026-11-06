"""Cloud provider adapters (DESIGN.md §1.3).

Nous, OpenAI Codex and Anthropic have no verified quota capability in the MVP:
they are opaque by policy, never probed over the network, and never send OAuth
credentials anywhere. OpenRouter reads exactly one documented endpoint.
"""

from __future__ import annotations

from typing import Optional

from ..models.dto import UNKNOWN_PERIOD, ProbeResult, UsageMeter, UsageResult, make_error
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


class AnthropicAdapter(OpaqueCloudAdapter):
    adapter_name = "AnthropicAdapter"


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
