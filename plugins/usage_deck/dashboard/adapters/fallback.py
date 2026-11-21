"""SessionAnalyticsAdapter: fallback for every provider without a verified source.

Only the injected AnalyticsReader (a logical read of ``/api/analytics/usage``)
is used. No provider HTTP, no DB access, no model-prefix attribution. The
returned series is profile-scoped and estimated; it is never cloned per
provider.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple

from ..models.dto import (
    Period,
    ProbeResult,
    UsagePoint,
    UsageResult,
    UsageTimeSeries,
    fmt_ts,
    make_error,
)
from .base import AnalyticsDay, AnalyticsUnavailable, BaseAdapter

PROFILE_SERIES_ID = "profile_session_analytics"


def build_series(rows: Tuple[AnalyticsDay, ...], window: int, now, series_id: str, scope: str,
                provider_id: Optional[str] = None) -> UsageTimeSeries:
    """One estimated daily series (the same point arithmetic for every scope and dimension group)."""
    points = []
    seen = set()
    for row in sorted(rows, key=lambda r: r.day):
        observed = (row.input_count, row.output_count, row.estimated_usd, row.sessions, row.api_calls)
        if all(value is None for value in observed):
            continue  # no numeric observation: not a measured zero
        if row.day in seen:
            continue
        seen.add(row.day)
        points.append(UsagePoint(row.day, row.input_count, row.output_count, row.estimated_usd,
                                 row.sessions, row.api_calls, "estimated",
                                 row.api_equivalent_usd, row.api_equivalent))
    end = now
    start = end - timedelta(days=window)
    return UsageTimeSeries(
        id=series_id,
        provider_id=provider_id,
        account_id=None,
        scope=scope,
        confidence="estimated",
        source="session_analytics",
        period=Period(fmt_ts(start), fmt_ts(end), f"rolling_{window}d"),
        window_days=window,
        bucket="day",
        timezone="server_local",
        coverage="partial",
        points=tuple(points),
        checked_at=fmt_ts(now),
        stale=False,
        error=None,
    )


def build_profile_series(rows: Tuple[AnalyticsDay, ...], window: int, now) -> UsageTimeSeries:
    return build_series(rows, window, now, PROFILE_SERIES_ID, "profile")


class SessionAnalyticsAdapter(BaseAdapter):
    adapter_name = "SessionAnalyticsAdapter"
    source_label = "session_analytics"

    def _do_probe(self) -> ProbeResult:
        try:
            available = bool(self.ctx.analytics.available())
        except Exception:  # noqa: BLE001
            available = False
        if not available:
            return self._probe_result(False, None, (), reason="source_unavailable")
        return self._probe_result(False, None, ("session_analytics",), reason="unsupported")

    def _do_usage(self, window: int) -> UsageResult:
        ctx = self.ctx
        try:
            rows = ctx.analytics.read_usage(window)
        except AnalyticsUnavailable:
            return UsageResult(confidence="opaque", reason="source_unavailable",
                               error=make_error("analytics_unavailable", ctx.provider_id, ctx.account_id))
        except Exception:  # noqa: BLE001 - corrupt analytics must not crash siblings
            return UsageResult(confidence="opaque", reason="source_unavailable",
                               error=make_error("analytics_unavailable", ctx.provider_id, ctx.account_id))
        series = build_profile_series(tuple(rows), window, ctx.now())
        # Card-level: quota is unknown and unsupported. The series carries its own
        # "estimated" confidence; whether it is empty is decided by the coordinator.
        return UsageResult(meters=(), series=(series,), confidence="opaque", reason="unsupported")
