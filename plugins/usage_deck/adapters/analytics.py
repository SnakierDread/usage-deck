"""AnalyticsReader implementations (DESIGN.md §1.4).

``HostAnalyticsReader`` reuses the dashboard's analytics service in-process (a
logical read of ``/api/analytics/usage``) with a lazy import, so a missing or
corrupt host analytics module becomes ``AnalyticsUnavailable`` rather than an
import failure of the plugin. Wire field names (``input_tokens`` ...) exist
only in ``normalize_analytics``; everything downstream sees ``*_count``.
"""

from __future__ import annotations

import math
import re
import threading
from typing import Any, Dict, Optional, Tuple

from .base import AnalyticsDay, AnalyticsReader, AnalyticsUnavailable

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_DAYS = 400


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value != int(value):
            return None
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _usd(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value) if value >= 0 else None


def normalize_analytics(payload: Any) -> Tuple[AnalyticsDay, ...]:
    """Map the host payload to ``AnalyticsDay`` rows. ``actual_cost`` is ignored."""
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), list):
        raise AnalyticsUnavailable("malformed analytics payload")
    rows = []
    for row in payload["daily"][:MAX_DAYS]:
        if not isinstance(row, dict):
            continue
        day = row.get("day")
        if not isinstance(day, str) or not _DAY_RE.match(day):
            continue
        rows.append(AnalyticsDay(
            day=day,
            input_count=_count(row.get("input_tokens")),
            output_count=_count(row.get("output_tokens")),
            estimated_usd=_usd(row.get("estimated_cost")),
            sessions=_count(row.get("sessions")),
            api_calls=_count(row.get("api_calls")),
        ))
    return tuple(rows)


class HostAnalyticsReader:
    """Reads the host's usage analytics for one profile (None = dashboard active)."""

    def __init__(self, profile: Optional[str] = None):
        self._profile = profile if profile and profile.lower() != "current" else None

    def _service(self):
        try:
            import hermes_cli.web_routers.analytics as host  # type: ignore
        except Exception:  # noqa: BLE001
            raise AnalyticsUnavailable("host analytics unavailable") from None
        fn = getattr(host, "_get_usage_analytics", None)
        if not callable(fn):
            raise AnalyticsUnavailable("host analytics unavailable")
        return fn

    def available(self) -> bool:
        try:
            self._service()
        except AnalyticsUnavailable:
            return False
        return True

    def read_usage(self, days: int) -> Tuple[AnalyticsDay, ...]:
        service = self._service()
        try:
            payload = service(days, self._profile)
        except AnalyticsUnavailable:
            raise
        except Exception:  # noqa: BLE001 - corrupt/locked stores must not leak details
            raise AnalyticsUnavailable("analytics read failed") from None
        return normalize_analytics(payload)


class MemoAnalyticsReader:
    """Per-refresh memo so N adapters cause one analytics read per window."""

    def __init__(self, inner: AnalyticsReader):
        self._inner = inner
        self._lock = threading.Lock()
        self._results: Dict[int, Any] = {}
        self._available: Optional[bool] = None

    def available(self) -> bool:
        with self._lock:
            if self._available is None:
                try:
                    self._available = bool(self._inner.available())
                except Exception:  # noqa: BLE001
                    self._available = False
            return self._available

    def read_usage(self, days: int) -> Tuple[AnalyticsDay, ...]:
        with self._lock:
            if days not in self._results:
                try:
                    self._results[days] = ("ok", self._inner.read_usage(days))
                except AnalyticsUnavailable:
                    self._results[days] = ("err", None)
                except Exception:  # noqa: BLE001
                    self._results[days] = ("err", None)
            state, rows = self._results[days]
        if state == "err":
            raise AnalyticsUnavailable("analytics unavailable")
        return rows
