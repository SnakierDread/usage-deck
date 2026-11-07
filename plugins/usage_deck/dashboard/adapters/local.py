"""Self-hosted / custom endpoint adapter: health and optional metrics only.

Requests go solely to the inventory-configured origin. ``/metrics`` is tried
without the inference credential; models/tags endpoints provide the health
evidence. A successful metrics exposition yields ``self_hosted`` confidence for
operational numbers; it never becomes a token quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ..models.dto import (
    HealthStripEntry,
    ProbeResult,
    UsageResult,
    make_error,
)
from .base import BaseAdapter, error_for_status, error_for_transport, parse_json_body
from .prometheus import Metrics, parse_exposition
from .transport import TransportError


@dataclass(frozen=True)
class _Attempt:
    ok: bool
    failure: str  # "" | "connection" | "timeout" | "status" | "invalid"
    status: Optional[int] = None
    latency_ms: Optional[float] = None


@dataclass(frozen=True)
class _Snapshot:
    configured: bool
    metrics: _Attempt
    health: _Attempt
    values: Optional[Metrics]
    status: str
    confidence: str
    reason: Optional[str]
    error_code: Optional[str]
    latency_ms: Optional[float]
    credential_sent: bool = False  # the models/tags requests carried a bearer
    unauthorized: bool = False  # a models/tags request answered 401


_NOT_RUN = _Attempt(False, "")


def _health_paths(endpoint, provider_id: str) -> Tuple[str, ...]:
    ollama_like = provider_id.lower() == "ollama" or endpoint.port == 11434
    if ollama_like and not endpoint.path.endswith("/v1"):
        return ("/api/tags", "/v1/models")
    return ("/v1/models", "/api/tags")


class SelfHostedAdapter(BaseAdapter):
    adapter_name = "SelfHostedAdapter"
    source_label = "endpoint_health"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._snapshot: Optional[_Snapshot] = None

    # -- snapshot --------------------------------------------------------
    def _attempt(self, path: str, validator, credential) -> Tuple[_Attempt, Any]:
        ctx = self.ctx
        headers = {"Accept": "application/json, text/plain"}
        if credential:
            headers["Authorization"] = "Bearer " + credential.reveal()
        try:
            response = ctx.transport.get(ctx.endpoint.url(path), headers=headers)
        except TransportError as exc:
            kind = "timeout" if exc.kind == TransportError.TIMEOUT else (
                "connection" if exc.kind in (TransportError.CONNECTION, TransportError.TLS) else "invalid")
            return _Attempt(False, kind), None
        if response.status != 200:
            return _Attempt(False, "status", response.status), None
        parsed = validator(response)
        if parsed is None:
            return _Attempt(False, "invalid", response.status), None
        return _Attempt(True, "", response.status, response.latency_ms), parsed

    @staticmethod
    def _metrics_validator(response):
        try:
            return parse_exposition(response.body.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _health_validator(path):
        def validate(response):
            try:
                body = parse_json_body(response)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return None
            if not isinstance(body, dict):
                return None
            expected = "models" if path == "/api/tags" else "data"
            return body if isinstance(body.get(expected), list) else None

        return validate

    def _check(self) -> _Snapshot:
        if self._snapshot is not None:
            return self._snapshot
        ctx = self.ctx
        if ctx.endpoint_cache is not None and ctx.endpoint_id:
            # A machine-only (no live source) card must never poison, or reuse, a live card's result.
            snap = ctx.endpoint_cache.get_or_compute(f"{ctx.endpoint_id}|{int(ctx.live_source)}|{int(ctx.cross_profile)}", self._compute)
        else:
            snap = self._compute()
        self._snapshot = snap
        return snap

    def _compute(self) -> _Snapshot:
        ctx = self.ctx
        endpoint = ctx.endpoint
        if endpoint is None or not endpoint.safe or not ctx.live_source:
            # No safe endpoint, or machine-only inventory: no outbound request at all (DESIGN.md §12.5).
            return _Snapshot(False, _NOT_RUN, _NOT_RUN, None, "unknown", "opaque",
                             "source_unavailable", None, None)
        if ctx.credential_expected and not ctx.credential:
            # key_env names an unset variable: nothing is sent (an anonymous probe would only
            # produce a misleading 401); the honest state is "no credential".
            return _Snapshot(True, _NOT_RUN, _NOT_RUN, None, "unknown", "opaque",
                             "credentials_missing", None, None)

        metrics_attempt, metrics_values = self._attempt("/metrics", self._metrics_validator, ctx.health_credential)
        if ctx.cross_profile:
            return self._compute_metrics_only(metrics_attempt, metrics_values)
        health_attempt = _Attempt(False, "")
        tried: List[_Attempt] = []
        for path in _health_paths(endpoint, ctx.provider_id):
            health_attempt, _ = self._attempt(path, self._health_validator(path), ctx.credential)
            tried.append(health_attempt)
            if health_attempt.ok or health_attempt.failure in ("connection", "timeout"):
                break  # a down host will not answer the sibling path either

        all_attempts = [metrics_attempt] + tried
        m_ok, h_ok = metrics_attempt.ok, health_attempt.ok
        credential_sent = bool(ctx.credential)
        unauthorized = (not h_ok) and any(a.failure == "status" and a.status == 401 for a in tried)
        # A 401 means "credential rejected" only if one was sent; anonymously it means none was.
        anonymous_401 = unauthorized and not credential_sent
        error_code = "auth_required" if unauthorized and credential_sent else None
        if m_ok and h_ok:
            status, confidence, reason = "healthy", "self_hosted", None
        elif h_ok:
            status, confidence, reason = "degraded", "opaque", "metrics_unavailable"
        elif m_ok:
            status, confidence = "degraded", "self_hosted"
            reason = "credentials_missing" if anonymous_401 else "source_unavailable"
        elif all(a.failure in ("connection", "timeout") for a in all_attempts):
            status, confidence, reason = "offline", "opaque", "source_unavailable"
            error_code = "timeout" if all(a.failure == "timeout" for a in all_attempts) else "upstream_unavailable"
        elif anonymous_401:
            status, confidence, reason = "unknown", "opaque", "credentials_missing"
        else:
            status, confidence, reason = "unknown", "opaque", "source_unavailable"
            if error_code is None:
                for attempt in tried + [metrics_attempt]:
                    if attempt.failure == "status" and attempt.status:
                        failure = error_for_status(attempt.status, ctx.provider_id, ctx.account_id)
                        error_code = failure.code if failure else None
                        break
                else:
                    error_code = "malformed_response"
        latency = health_attempt.latency_ms if h_ok else (metrics_attempt.latency_ms if m_ok else None)
        snap = _Snapshot(True, metrics_attempt, health_attempt, metrics_values if m_ok else None,
                         status, confidence, reason, error_code, latency, credential_sent, unauthorized)
        return snap

    # -- protocol --------------------------------------------------------
    def _compute_metrics_only(self, metrics_attempt: _Attempt, values: Optional[Metrics]) -> _Snapshot:
        """Machine-view cross-profile card (DESIGN.md §13): the one ``/metrics`` scrape was the only request.

        The models/tags probes carry an inference credential and are not a usage probe, so they are not
        sent; a valid exposition proves the endpoint is up, anything else is honestly "unknown"/offline.
        """
        if metrics_attempt.ok:
            return _Snapshot(True, metrics_attempt, _NOT_RUN, values, "healthy", "self_hosted", None, None,
                             metrics_attempt.latency_ms)
        if metrics_attempt.failure in ("connection", "timeout"):
            code = "timeout" if metrics_attempt.failure == "timeout" else "upstream_unavailable"
            return _Snapshot(True, metrics_attempt, _NOT_RUN, None, "offline", "opaque", "source_unavailable",
                             code, None)
        return _Snapshot(True, metrics_attempt, _NOT_RUN, None, "unknown", "opaque", "source_unavailable",
                         None, None)

    def _do_probe(self) -> ProbeResult:
        snap = self._check()
        if not snap.configured:
            return self._probe_result(False, None, self._capabilities(), reason="source_unavailable")
        reachable = snap.status != "offline"
        if snap.status == "unknown":
            reachable = None
        caps = self._capabilities(*(("metrics",) if snap.metrics.ok else ()))
        error = make_error(snap.error_code, self.ctx.provider_id, self.ctx.account_id) \
            if snap.status == "offline" else None
        reason = None if snap.metrics.ok else (
            "source_unavailable" if snap.status == "offline"
            else "credentials_missing" if snap.reason == "credentials_missing" else "metrics_unavailable")
        return self._probe_result(snap.metrics.ok, reachable, caps, reason=reason, error=error)

    def credential_rejected(self) -> bool:
        snap = self._check()
        return bool(self.ctx.credential and snap.credential_sent and snap.unauthorized)

    def _do_usage(self, window: int) -> UsageResult:
        snap = self._check()
        if not snap.configured:
            return UsageResult(confidence="opaque", reason="source_unavailable")
        if snap.status == "offline":
            return UsageResult(confidence="opaque", reason="source_unavailable",
                               error=make_error(snap.error_code, self.ctx.provider_id, self.ctx.account_id))
        confidence = "self_hosted" if snap.metrics.ok else "opaque"
        reason = "unsupported" if snap.metrics.ok else (
            "credentials_missing" if snap.reason == "credentials_missing" else "metrics_unavailable")
        return UsageResult(meters=(), series=(), confidence=confidence, reason=reason)

    def get_health(self) -> Optional[HealthStripEntry]:
        ctx = self.ctx
        if ctx.endpoint is None or ctx.endpoint_id is None:
            return None
        snap = self._check()
        error = make_error(snap.error_code, ctx.provider_id, ctx.account_id) if snap.error_code else None
        values = snap.values
        return HealthStripEntry(
            endpoint_id=ctx.endpoint_id,
            provider_id=ctx.provider_id,
            display_name=ctx.provider_label,
            status=snap.status,
            confidence=snap.confidence,
            metrics_available=snap.metrics.ok,
            latency_ms=snap.latency_ms,
            cpu_percent=values.cpu_percent if values else None,
            memory_bytes=values.memory_bytes if values else None,
            checked_at=ctx.now_ts(),
            stale=False,
            reason=snap.reason,
            error=error,
        )
