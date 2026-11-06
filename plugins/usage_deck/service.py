"""Orchestration, caching and envelopes (DESIGN.md §1.2, §1.5, §3).

Pure stdlib. ``UsageDeckService`` returns ``(http_status, body)`` tuples and
never raises for ordinary provider failures; every body has been through
``sanitize`` before it is cached or returned.
"""

from __future__ import annotations

import contextvars
import copy
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters import create_adapter
from .adapters.analytics import HostAnalyticsReader, MemoAnalyticsReader
from .adapters.base import AdapterContext, AnalyticsReader, EndpointCache
from .adapters.cloud import OPENROUTER_TARGET
from .adapters.fallback import SessionAnalyticsAdapter
from .adapters.transport import AllowedTarget, GuardedTransport, UrllibTransport
from .discovery import InventoryItem, InventoryUnavailable, discover, host_inventory_source
from .models.dto import (
    DebugInfo,
    DeckError,
    HealthStripEntry,
    SubscriptionCard,
    build_meta,
    check_envelope,
    check_wire,
    failure_envelope,
    fmt_ts,
    make_error,
    success_envelope,
    to_plain,
)
from .models.redaction import sanitize

log = logging.getLogger(__name__)

API_BASE = "/api/plugins/usage-deck"
CACHE_TTL_S = 30.0
MAX_WORKERS = 4
REFRESH_DEADLINE_S = 8.0
DEFAULT_WINDOW = 30
_ALLOWED_PARAMS = {
    "subscriptions": {"profile"},
    "health": {"profile"},
    "usage": {"profile", "window", "provider"},
}
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# ------------------------------------------------------------------ query


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.error = make_error(code)


@dataclass(frozen=True)
class Query:
    window: int = DEFAULT_WINDOW
    provider: Optional[str] = None
    profile: Optional[str] = None


def parse_query(endpoint: str, items: List[Tuple[str, str]]) -> Query:
    allowed = _ALLOWED_PARAMS[endpoint]
    seen: Dict[str, str] = {}
    for key, value in items:
        if key not in allowed or key in seen:
            raise ApiError(400, "invalid_query")  # unknown or repeated parameter
        seen[key] = value
    window = DEFAULT_WINDOW
    if "window" in seen:
        if seen["window"] not in ("7", "30", "90"):
            raise ApiError(400, "invalid_query")
        window = int(seen["window"])
    provider = None
    if "provider" in seen:
        value = seen["provider"]
        if not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ApiError(400, "invalid_query")
        provider = value
    profile = None
    if "profile" in seen:
        value = seen["profile"]
        if value.lower() != "current" and not _PROFILE_RE.match(value):
            raise ApiError(400, "invalid_query")
        profile = value
    return Query(window, provider, profile)


# ---------------------------------------------------------------- snapshot


@dataclass
class _Snapshot:
    cards: List[dict]
    health: List[dict]
    created: float


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class UsageDeckService:
    def __init__(
        self,
        inventory_factory: Optional[Callable[[], Any]] = None,
        analytics_factory: Optional[Callable[[Optional[str]], AnalyticsReader]] = None,
        transport: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        cache_ttl: float = CACHE_TTL_S,
        max_workers: int = MAX_WORKERS,
        deadline: float = REFRESH_DEADLINE_S,
    ):
        self._inventory_factory = inventory_factory or host_inventory_source
        self._analytics_factory = analytics_factory or HostAnalyticsReader
        self._transport = transport or UrllibTransport()
        self._clock = clock or _utc_now
        self._monotonic = monotonic
        self._ttl = cache_ttl
        self._max_workers = max_workers
        self._deadline = deadline
        self._cache: Dict[tuple, Tuple[float, Any]] = {}
        self._locks: Dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- public API ------------------------------------------------------
    def subscriptions(self, query: Query) -> Tuple[int, dict]:
        return self._guarded(lambda: self._subscriptions(query))

    def usage(self, query: Query) -> Tuple[int, dict]:
        return self._guarded(lambda: self._usage(query))

    def health(self, query: Query) -> Tuple[int, dict]:
        return self._guarded(lambda: self._health(query))

    def handle(self, endpoint: str, items: List[Tuple[str, str]]) -> Tuple[int, dict]:
        """Validate the query then dispatch; the single entry point for the router."""
        try:
            query = parse_query(endpoint, items)
        except ApiError as exc:
            return self._fail(exc.status, exc.error)
        return getattr(self, endpoint)(query)

    # -- envelope helpers ------------------------------------------------
    def _meta(self, errors: List[dict]) -> dict:
        return build_meta(str(uuid.uuid4()), fmt_ts(self._clock()), errors)

    def _ok(self, data: dict, errors: List[dict]) -> Tuple[int, dict]:
        # Masking must never change the wire shape; if it would, fail via internal_error (500).
        return 200, check_envelope(sanitize(success_envelope(data, self._meta(errors))))

    def _fail(self, status: int, error: DeckError) -> Tuple[int, dict]:
        return status, sanitize(failure_envelope(error, self._meta([])))

    def _guarded(self, fn: Callable[[], Tuple[int, dict]]) -> Tuple[int, dict]:
        try:
            return fn()
        except ApiError as exc:
            return self._fail(exc.status, exc.error)
        except InventoryUnavailable:
            return self._fail(503, make_error("inventory_unavailable"))
        except Exception as exc:  # noqa: BLE001 - invariant failure at the coordinator boundary
            log.error("usage_deck internal error: %s", type(exc).__name__)
            return self._fail(500, make_error("internal_error"))

    # -- cache -----------------------------------------------------------
    def _cached(self, key: tuple, compute: Callable[[], Any]) -> Any:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # coalesce concurrent identical requests
            now = self._monotonic()
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return copy.deepcopy(hit[1])
            value = compute()
            with self._guard:
                self._cache[key] = (self._monotonic(), value)
                for stale_key in [k for k, (ts, _) in self._cache.items() if self._monotonic() - ts > self._ttl * 4]:
                    self._cache.pop(stale_key, None)
            return copy.deepcopy(value)

    # -- endpoints -------------------------------------------------------
    def _subscriptions(self, query: Query) -> Tuple[int, dict]:
        snap = self._deck(query)
        cards = snap.cards
        errors = [c["error"] for c in cards if c["error"]]
        data = {"cards": cards, "inventory_state": "detected" if cards else "empty"}
        return self._ok(data, errors)

    def _health(self, query: Query) -> Tuple[int, dict]:
        snap = self._deck(query)
        errors = [h["error"] for h in snap.health if h["error"]]
        return self._ok({"entries": snap.health}, errors)

    def _usage(self, query: Query) -> Tuple[int, dict]:
        if query.provider is not None:
            source = self._inventory_factory()
            items = discover(source)
            if not any(item.provider_id == query.provider for item in items):
                raise ApiError(404, "provider_not_found")
            # Session analytics has no provider/account attribution: never clone profile totals.
            data = {"window_days": query.window, "provider_id": query.provider,
                    "series": [], "reason": "attribution_unavailable"}
            return self._ok(data, [])
        source = self._inventory_factory()
        key = (source.home_key(), query.profile or "", "usage", query.window)
        series = self._cached(key, lambda: self._read_series(query))
        if series is None:
            raise ApiError(503, "analytics_unavailable")
        data = {"window_days": query.window, "provider_id": None, "series": [series],
                "reason": None if series["points"] else "no_observations"}
        return self._ok(data, [])

    def _read_series(self, query: Query) -> Optional[dict]:
        now = self._clock()
        reader = self._analytics_factory(query.profile)
        ctx = AdapterContext(
            provider_id="profile", account_id="profile", provider_label="Profile", account_label="Profile",
            kind="cloud", analytics=reader, transport=GuardedTransport(self._transport, ()),
            clock=lambda: now,
        )
        result = SessionAnalyticsAdapter(ctx).get_usage(query.window)
        if result.error is not None or not result.series:
            return None
        return check_wire("series", sanitize(to_plain(result.series[0])))

    # -- deck snapshot (cards + health) ----------------------------------
    def _deck(self, query: Query) -> _Snapshot:
        source = self._inventory_factory()
        key = (source.home_key(), query.profile or "", "deck")
        return self._cached(key, lambda: self._build_deck(source, query))

    def _build_deck(self, source: Any, query: Query) -> _Snapshot:
        items = discover(source)
        now = self._clock()
        reader = MemoAnalyticsReader(self._analytics_factory(query.profile))
        endpoints = EndpointCache()
        health_owner: Dict[str, str] = {}
        for item in items:
            if item.endpoint_id and item.endpoint_id not in health_owner:
                health_owner[item.endpoint_id] = item.account_id

        executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="usage-deck")
        futures = {}
        try:
            for item in items:
                owns_health = bool(item.endpoint_id) and health_owner.get(item.endpoint_id) == item.account_id
                ctx = contextvars.copy_context()  # a Context may only be entered once at a time
                futures[executor.submit(ctx.run, self._run_item, item, reader, now, owns_health, endpoints)] = item
            done, _pending = wait(list(futures), timeout=self._deadline)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        cards: List[dict] = []
        health: List[dict] = []
        for future, item in futures.items():
            card_dict: Optional[dict] = None
            health_dict: Optional[dict] = None
            owns = bool(item.endpoint_id) and health_owner.get(item.endpoint_id) == item.account_id
            if future in done:
                try:
                    card_dict, health_dict = future.result()
                except Exception as exc:  # noqa: BLE001 - one card must not crash its siblings
                    log.error("usage_deck adapter error: %s", type(exc).__name__)
                    card_dict = self._error_card(item, now, "internal_error")
                    if owns:
                        health_dict = self._unknown_health(item, now, "internal_error")
            else:
                card_dict = self._error_card(item, now, "timeout")
                if owns:
                    health_dict = self._unknown_health(item, now, "timeout")
            cards.append(card_dict)
            if health_dict is not None:
                health.append(health_dict)
        cards.sort(key=lambda c: (c["provider_id"], c["account_id"]))
        health.sort(key=lambda h: (h["provider_id"], h["endpoint_id"]))
        return _Snapshot(cards, health, self._monotonic())

    def _context(self, item: InventoryItem, reader: AnalyticsReader, now: datetime,
                 endpoints: Optional[EndpointCache] = None) -> AdapterContext:
        targets: List[AllowedTarget] = []
        if item.provider_id.lower() == "openrouter":
            targets.append(OPENROUTER_TARGET)
        if item.endpoint is not None and item.endpoint.safe:
            targets.append(AllowedTarget(item.endpoint.scheme, item.endpoint.host, item.endpoint.port, None))
        return AdapterContext(
            provider_id=item.provider_id,
            account_id=item.account_id,
            provider_label=item.provider_label,
            account_label=item.account_label,
            kind=item.kind,
            analytics=reader,
            transport=GuardedTransport(self._transport, targets),
            clock=lambda: now,
            endpoint=item.endpoint,
            endpoint_id=item.endpoint_id,
            credential=item.credential,
            auth_status=item.auth_status,
            plan_name=item.plan_name,
            plan_source=item.plan_source,
            secrets=item.secrets,
            health_credential=item.health_credential,
            endpoint_cache=endpoints,
            credential_expected=item.credential_expected,
        )

    def _run_item(self, item: InventoryItem, reader: AnalyticsReader, now: datetime,
                  owns_health: bool, endpoints: Optional[EndpointCache] = None) -> Tuple[dict, Optional[dict]]:
        adapter = create_adapter(self._context(item, reader, now, endpoints))
        probe = adapter.probe()
        usage = adapter.get_usage(DEFAULT_WINDOW)
        health: Optional[HealthStripEntry] = adapter.get_health() if owns_health else None
        confidence = adapter.get_confidence()

        error = probe.error
        if usage.error is not None and usage.error.code != "analytics_unavailable":
            error = usage.error  # card content never depends on profile analytics
        meters = tuple(usage.meters)
        if error is not None:
            state, confidence, reason = "error", "opaque", usage.reason or probe.reason
            meters = ()
        elif any(m.value is not None for m in meters):
            state, reason = "ready", None
        elif usage.reason == "no_observations":
            state, reason = "empty", "no_observations"
        else:
            state, reason = "opaque", usage.reason or probe.reason or "unsupported"
        caps = tuple(probe.capabilities)
        card = SubscriptionCard(
            id=item.account_id,
            provider_id=item.provider_id,
            account_id=item.account_id,
            provider_label=item.provider_label,
            account_label=item.account_label,
            kind=item.kind,
            plan_name=item.plan_name,
            plan_source=item.plan_source,
            auth_status="rejected" if adapter.credential_rejected() else item.auth_status,
            endpoint_id=item.endpoint_id,
            state=state,
            confidence=confidence,
            reason=reason,
            meters=meters,
            checked_at=fmt_ts(now),
            stale=False,
            error=error,
            debug=DebugInfo(adapter=adapter.adapter_name, capabilities=caps, source=adapter.source_label),
        )
        card_dict = check_wire("card", sanitize(to_plain(card), item.secrets))
        health_dict = check_wire("health", sanitize(to_plain(health), item.secrets)) if health is not None else None
        return card_dict, health_dict

    def _error_card(self, item: InventoryItem, now: datetime, code: str) -> dict:
        card = SubscriptionCard(
            id=item.account_id, provider_id=item.provider_id, account_id=item.account_id,
            provider_label=item.provider_label, account_label=item.account_label, kind=item.kind,
            plan_name=item.plan_name, plan_source=item.plan_source, auth_status=item.auth_status,
            endpoint_id=item.endpoint_id, state="error", confidence="opaque", reason="source_unavailable",
            meters=(), checked_at=fmt_ts(now), stale=False,
            error=make_error(code, item.provider_id, item.account_id),
            debug=DebugInfo(adapter="none", capabilities=(), source="none"),
        )
        return check_wire("card", sanitize(to_plain(card), item.secrets))

    def _unknown_health(self, item: InventoryItem, now: datetime, code: str) -> dict:
        entry = HealthStripEntry(
            endpoint_id=item.endpoint_id or "", provider_id=item.provider_id,
            display_name=item.provider_label, status="unknown", confidence="opaque",
            metrics_available=False, latency_ms=None, cpu_percent=None, memory_bytes=None,
            checked_at=fmt_ts(now), stale=False, reason="source_unavailable",
            error=make_error(code, item.provider_id, item.account_id),
        )
        return check_wire("health", sanitize(to_plain(entry), item.secrets))
