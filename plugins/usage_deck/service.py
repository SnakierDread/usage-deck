"""Orchestration, caching and envelopes (DESIGN.md §1.2, §1.5, §3).

Pure stdlib. ``UsageDeckService`` returns ``(http_status, body)`` tuples and
never raises for ordinary provider failures; every body has been through
``sanitize`` before it is cached or returned.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .adapters import create_adapter
from .adapters.analytics import HostAnalyticsReader, MemoAnalyticsReader
from .adapters.base import AdapterContext, AnalyticsReader, EndpointCache
from .adapters.cloud import ANTHROPIC_PROVIDER_ID, ANTHROPIC_TARGET, CODEX_PROVIDER_ID, CODEX_TARGET, OPENROUTER_TARGET
from .adapters.credentials import default_resolver
from .adapters import dimensions as dims
from .adapters import analytics_depth as depth
from .adapters.fallback import SessionAnalyticsAdapter
from .adapters.session_db import (
    STATE_ABSENT,
    STATE_EXCLUDED,
    STATE_OK,
    STATE_UNAVAILABLE,
    UNKNOWN,
    HomeRead,
    Metrics,
    SessionDbReader,
    catalog_identity,
    normalize_provider,
    provider_matches,
    sum_known as _known_sum,
    sum_metrics,
)
from .adapters.transport import AllowedTarget, CrossProfileProbeTransport, GuardedTransport, UrllibTransport
from .captures import CaptureStore
from .discovery import (
    GLOBAL_PROFILE,
    InventoryItem,
    InventoryUnavailable,
    discover,
    discover_machine,
    home_candidates,
    home_rejects,
    host_inventory_source,
    provider_label_for,
)
from .models.dto import (
    DIMENSIONS,
    TOP_N_MODELS,
    USAGE_SCHEMA_VERSION,
    DebugInfo,
    DeckError,
    HealthStripEntry,
    PricingInfo,
    PRICING_WARNINGS,
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
DEFAULT_VIEW = "machine"
VIEWS = ("machine", "profile")
DEFAULT_DIMENSION = "all"
_ALLOWED_PARAMS = {
    "subscriptions": {"profile", "view"},
    "health": {"profile", "view"},
    "usage": {"profile", "window", "provider", "view", "mode", "dimension"},
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
    #: Home scope. For ``usage`` this is the ``mode`` parameter (``view`` is its compatibility alias).
    view: str = DEFAULT_VIEW
    dimension: str = DEFAULT_DIMENSION


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
    view = DEFAULT_VIEW
    for name in ("view", "mode"):  # ``mode`` is the usage-only name; ``view`` stays its alias
        if name in seen:
            if seen[name] not in VIEWS:
                raise ApiError(400, "invalid_query")
    if "view" in seen and "mode" in seen and seen["view"] != seen["mode"]:
        raise ApiError(400, "invalid_query")
    if "mode" in seen:
        view = seen["mode"]
    elif "view" in seen:
        view = seen["view"]
    dimension = DEFAULT_DIMENSION
    if "dimension" in seen:
        if seen["dimension"] not in DIMENSIONS:
            raise ApiError(400, "invalid_query")
        dimension = seen["dimension"]
    return Query(window, provider, profile, view, dimension)


# ---------------------------------------------------------------- snapshot


@dataclass
class _Snapshot:
    cards: List[dict]
    health: List[dict]
    created: float
    view: str = "profile"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class PricingInputs:
    """One frozen view of the pricing resolver for a single analytics response (design §7): the immutable
    catalog snapshot, the alias map and the pure ``resolve_api_equivalent`` callable (or a test double)."""

    catalog: Any  # pricing.CatalogSnapshot: revision / origin / warnings
    aliases: Mapping[str, Any]
    resolve: Callable[..., Any]


def alias_digest(aliases: Mapping[str, Any]) -> str:
    """Digest of the alias map contents (response-cache identity: an alias edit never serves an old total)."""
    items = sorted((str(k), getattr(a, "catalog_key", None), getattr(a, "kind", None), getattr(a, "note", None))
                   for k, a in aliases.items())
    return hashlib.sha256(json.dumps(items, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


_UNAVAILABLE_REVISION = hashlib.sha256(b"usage-deck:pricing-unavailable").hexdigest()


def _default_pricing(service: "UsageDeckService", source: Any) -> PricingInputs:
    """Resolver backend bound to the inventory's validated root: seed + overlay cache under
    ``<root>/plugin-data/usage-deck``; never a request-supplied path, never another root."""
    from . import pricing  # resolver module (design §6); imported lazily so a broken module degrades to unpriced

    root_of = getattr(source, "root_dir", None)
    if not callable(root_of):
        return PricingInputs(pricing.load_seed(), pricing.load_aliases(), pricing.resolve_api_equivalent)
    data_dir = Path(root_of()) / "plugin-data" / "usage-deck"
    with service._guard:
        manager = service._pricing_managers.get(str(data_dir))
        if manager is None:
            manager = pricing.PricingCatalogManager(
                data_dir, clock=service._clock, random_uniform=random.uniform, fetch=pricing.default_fetch)
            service._pricing_managers[str(data_dir)] = manager
    return PricingInputs(manager.snapshot(), manager.aliases(), pricing.resolve_api_equivalent)


def _default_captures(source: Any) -> CaptureStore:
    """Read-only header captures next to the Hermes root; absent file = no captures."""
    path_of = getattr(source, "captures_path", None)
    return CaptureStore(path_of() if callable(path_of) else None)  # scoped refs + the adapter's refresh clock


def _effective_view(source: Any, query: "Query") -> str:
    """The machine view needs a source that can enumerate every home; otherwise fall back honestly."""
    if query.view == "machine" and callable(getattr(source, "read_stores", None)):
        return "machine"
    return "profile"


class UsageDeckService:
    def __init__(
        self,
        inventory_factory: Optional[Callable[[], Any]] = None,
        analytics_factory: Optional[Callable[[Optional[str]], AnalyticsReader]] = None,
        transport: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        capture_factory: Optional[Callable[[Any], CaptureStore]] = None,
        cache_ttl: float = CACHE_TTL_S,
        max_workers: int = MAX_WORKERS,
        deadline: float = REFRESH_DEADLINE_S,
        token_resolver: Any = None,
        session_reader_factory: Optional[Callable[[], SessionDbReader]] = None,
        pricing_factory: Optional[Callable[[Any], PricingInputs]] = None,
        depth_rates_factory: Optional[Callable[[], "depth.RateTable"]] = None,
    ):
        self._inventory_factory = inventory_factory or host_inventory_source
        # Packaged cache-discount projection (design §5); a test may inject a table.
        self._depth_rates_factory = depth_rates_factory or depth.load_rate_table
        # Read-only state.db reader for the dimensions payload (tests inject one with a SQL trace).
        self._session_reader_factory = session_reader_factory or SessionDbReader
        # API-equivalent resolver inputs (catalog snapshot + aliases + resolve); tests inject a double.
        self._pricing_factory = pricing_factory or (lambda source: _default_pricing(self, source))
        self._pricing_managers: Dict[str, Any] = {}
        # §9.1.1: the Anthropic bearer is resolved through core's credential pool (refreshing an expiring row);
        # one resolver per service so its failure backoff outlives a single snapshot.
        self._token_resolver = token_resolver if token_resolver is not None else default_resolver()
        self._analytics_factory = analytics_factory or HostAnalyticsReader
        self._transport = transport or UrllibTransport()
        self._capture_factory = capture_factory or _default_captures
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
        return self._guarded(lambda: self._usage(query), "usage")

    def health(self, query: Query) -> Tuple[int, dict]:
        return self._guarded(lambda: self._health(query))

    def handle(self, endpoint: str, items: List[Tuple[str, str]], *, profile_only: bool = False) -> Tuple[int, dict]:
        """Validate the query then dispatch; the single entry point for the router.

        ``profile_only`` is set by the router for a profile-restricted host principal (DESIGN.md §12.5): the
        machine view is a full-trust machine-owner inventory, so such a principal is clamped to the profile
        view whatever ``view=``/``mode=`` says (a query string can never widen authorization); a by-profile
        dimension is then normalised to All. The clamp runs before any home is discovered.
        """
        try:
            query = parse_query(endpoint, items)
        except ApiError as exc:
            return self._fail(exc.status, exc.error, endpoint)
        if profile_only:
            try:
                query = self._bind_authorized_home(query)
            except ApiError as exc:
                return self._fail(exc.status, exc.error, endpoint)
            except InventoryUnavailable:
                return self._fail(503, make_error("inventory_unavailable"), endpoint)
            except Exception as exc:  # noqa: BLE001 - invariant failure at the coordinator boundary
                log.error("usage_deck internal error: %s", type(exc).__name__)
                return self._fail(500, make_error("internal_error"), endpoint)
        if profile_only and query.view != "profile":
            query = replace(query, view="profile", dimension="all" if query.dimension == "profile" else query.dimension)
        elif endpoint == "usage" and query.view == "profile" and query.dimension == "profile":
            return self._fail(400, make_error("invalid_query"), endpoint)  # a single profile has no by-profile view
        return getattr(self, endpoint)(query)

    def _bind_authorized_home(self, query: Query) -> Query:
        """Pin a profile-restricted principal to the host-authorized (request-scoped) home.

        The inventory source reads exactly the home the host authorized for this request. A ``profile``
        selector is accepted only when it names that very home (its profile name, ``default`` for the global
        home, or ``current``) and is then dropped so every reader resolves the authorized home itself; any
        other selector (a sibling profile, or ``default``/root from a profile home) is refused before any
        home is opened. Fails closed: with no way to name the authorized home only the absent/``current``
        selector is accepted.
        """
        selector = query.profile
        if selector is None:
            return query
        if selector.lower() != "current":
            name_of = getattr(self._inventory_factory(), "profile_name", None)
            name, is_global = name_of() if callable(name_of) else ("current", False)
            own = selector.lower() == "default" if is_global else (name != "current" and selector == name)
            if not own:
                raise ApiError(400, "invalid_query")
        return replace(query, profile=None)

    # -- envelope helpers ------------------------------------------------
    def _meta(self, errors: List[dict], schema_version: int = 1) -> dict:
        return build_meta(str(uuid.uuid4()), fmt_ts(self._clock()), errors, schema_version)

    def _ok(self, data: dict, errors: List[dict], schema_version: int = 1) -> Tuple[int, dict]:
        # Masking must never change the wire shape; if it would, fail via internal_error (500).
        return 200, check_envelope(sanitize(success_envelope(data, self._meta(errors, schema_version))))

    def _fail(self, status: int, error: DeckError, endpoint: Optional[str] = None) -> Tuple[int, dict]:
        version = USAGE_SCHEMA_VERSION if endpoint == "usage" else 1
        return status, sanitize(failure_envelope(error, self._meta([], version)))

    def _guarded(self, fn: Callable[[], Tuple[int, dict]], endpoint: Optional[str] = None) -> Tuple[int, dict]:
        try:
            return fn()
        except ApiError as exc:
            return self._fail(exc.status, exc.error, endpoint)
        except InventoryUnavailable:
            return self._fail(503, make_error("inventory_unavailable"), endpoint)
        except Exception as exc:  # noqa: BLE001 - invariant failure at the coordinator boundary
            log.error("usage_deck internal error: %s", type(exc).__name__)
            return self._fail(500, make_error("internal_error"), endpoint)

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
        data = {"cards": cards, "inventory_state": "detected" if cards else "empty", "view": snap.view}
        return self._ok(data, errors)

    def _health(self, query: Query) -> Tuple[int, dict]:
        snap = self._deck(query)
        errors = [h["error"] for h in snap.health if h["error"]]
        return self._ok({"entries": snap.health}, errors)

    def _usage(self, query: Query) -> Tuple[int, dict]:
        source = self._inventory_factory()
        mode = _effective_view(source, query)
        if mode == "machine" and not callable(getattr(source, "root_dir", None)):
            mode = "profile"
        dimension = query.dimension if (mode == "machine" or query.dimension != "profile") else "all"
        homes, truncated, rejected = self._scope_homes(source, query, mode)
        catalog = tuple((name, is_global, catalog_identity(directory)) for name, is_global, directory in homes)
        priced = self._pricing(source)  # frozen once: this request prices every leaf against one snapshot
        price_id = (priced.catalog.revision, alias_digest(priced.aliases)) if priced else (_UNAVAILABLE_REVISION, "")
        rates = self._depth_rates()  # frozen once per request, like the pricing snapshot
        key = (self._source_key(source), query.profile or "", "usage", mode, dimension, query.window, query.provider,
               catalog, truncated, tuple(rejected[0]), rejected[1], price_id, rates.digest)
        data, errors = self._cached(
            key, lambda: self._compute_usage(query, mode, dimension, homes, truncated, rejected, priced, rates))
        return self._ok(data, errors, USAGE_SCHEMA_VERSION)

    def _pricing(self, source: Any) -> Optional[PricingInputs]:
        """The resolver inputs, or None when the resolver cannot load (every row then reads honestly as unpriced)."""
        try:
            return self._pricing_factory(source)
        except Exception as exc:  # noqa: BLE001 - pricing is an annotation; it must never take usage down
            log.warning("usage_deck pricing unavailable: %s", type(exc).__name__)
            return None

    @staticmethod
    def _scope_homes(source: Any, query: Query, mode: str):
        """``(homes, truncated, (rejected symlink names, parent symlink))`` of the analytics scope.

        Machine: every home under the request-scoped root (the same enumeration the inventory uses). Profile:
        exactly the selected home and nothing else."""
        root_of = getattr(source, "root_dir", None)
        if mode == "machine":
            root = Path(root_of())
            homes, truncated = home_candidates(root)
            return homes, truncated, home_rejects(root)
        home_of = getattr(source, "home_dir", None)
        name_of = getattr(source, "profile_name", None)
        if query.profile is None or query.profile.lower() == "current":
            if not callable(home_of) or not callable(name_of):
                return [], False, ([], False)
            name, is_global = name_of()
            return [(name, is_global, Path(home_of()))], False, ([], False)
        root = Path(root_of()) if callable(root_of) else None
        if root is None:
            return [], False, ([], False)
        if query.profile.lower() == "default":
            return [(GLOBAL_PROFILE, True, root)], False, ([], False)
        return [(query.profile, False, root / "profiles" / query.profile)], False, ([], False)

    def _compute_usage(self, query: Query, mode: str, dimension: str, homes: list, truncated: bool,
                       rejected: tuple, priced: Optional[PricingInputs] = None,
                       rates: Optional["depth.RateTable"] = None) -> Tuple[dict, List[dict]]:
        rates = rates if rates is not None else self._depth_rates()
        now = self._clock()  # one frozen clock for every DB, group and timestamp of this response
        cutoff = now.timestamp() - query.window * 86400
        reads = self._session_reader_factory().read_homes(homes, cutoff)
        states = [r.state for r in reads]
        sources = [(r.profile, r.is_global, r.state) for r in reads]
        sources += [(name, False, STATE_EXCLUDED) for name in rejected[0]]
        sources.sort(key=lambda s: (not s[1], s[0]))
        incomplete = truncated or rejected[1] or any(s[2] in (STATE_UNAVAILABLE, STATE_EXCLUDED) for s in sources)

        override = None
        if mode == "profile" and dimension == "all" and query.provider is None:
            # Profile-All keeps its exact baseline numbers: the host analytics reader and the shared
            # profile-series arithmetic stay the source of truth for the series and its totals.
            series = self._read_series(query, now)
            if series is None:
                raise ApiError(503, "analytics_unavailable")
            override = series
        elif mode == "profile":
            if any(s in (STATE_UNAVAILABLE, STATE_EXCLUDED) for s in states) or not reads:
                raise ApiError(503, "analytics_unavailable")  # the selected profile's store must be readable
        elif not any(s == STATE_OK for s in states) and any(
                s[2] in (STATE_UNAVAILABLE, STATE_EXCLUDED) for s in sources):
            raise ApiError(503, "analytics_unavailable")  # nothing readable and something broken

        def label_of(provider_id: str) -> str:
            return "Unknown" if provider_id == UNKNOWN else provider_label_for(provider_id, frozenset())

        rows_all = dims.observation_rows(reads)
        wanted = None
        if query.provider is not None:
            wanted, bad = normalize_provider(query.provider)
            if bad or wanted is None or wanted not in dims.known_provider_ids(rows_all, reads):
                raise ApiError(404, "provider_not_found")
        # The depth projection is scoped with the SAME home set, window predicate, attribution and provider
        # predicate as the counters above: every dimension sees one identical authorized session set.
        all_facts, scan = depth.facts_from_reads(reads)
        facts = depth.filter_facts(all_facts, (lambda p: provider_matches(p, wanted)) if wanted else (lambda p: True))
        if dimension == "repo":
            # The repository view is built from the facts: a home whose projection failed cannot be shown.
            if scan.scanned == 0 and scan.unavailable > 0:
                raise ApiError(503, "analytics_unavailable")
            incomplete = incomplete or scan.unavailable > 0
            rows = dims.repo_rows(facts)
        else:
            rows = dims.filter_rows(rows_all, wanted)
        if priced is not None:  # price each authorized, filtered leaf once; groups/series/totals only sum these
            rows = dims.annotate_rows(rows, lambda model, tokens_in, tokens_out: priced.resolve(
                model, tokens_in, tokens_out, catalog=priced.catalog, aliases=priced.aliases, now=now))
        row_metrics = sum_metrics(r.metrics for r in rows)
        override_all = None
        if override is not None:
            points = override.points
            totals = Metrics(
                input_count=_known_sum(p.input_count for p in points),
                output_count=_known_sum(p.output_count for p in points),
                estimated_usd=_known_sum(p.estimated_usd for p in points),
                sessions=_known_sum(p.sessions for p in points),
                api_calls=_known_sum(p.api_calls for p in points),
                missing=row_metrics.missing)
            override_all = (override, totals)
        else:
            totals = row_metrics
        part = dims.partition(rows, dimension, mode, query.window, now, wanted, label_of, override_all)
        kept = {g.id for g in part.groups}

        def group_of(fact: depth.SessionFact) -> Optional[str]:
            """The id of the ``data.groups`` entry a session belongs to (model Other = every unlisted model)."""
            if dimension == "all":
                return "all"
            if dimension == "profile":
                return dims.profile_group_id(*fact.holder)
            if dimension == "repo":
                return depth.repo_group_id(depth.repo_key(fact.repo_root))
            gid = dims.model_group_id(fact.model)
            return gid if gid in kept else "other"

        depth_result = depth.build_depth(facts, dimension=dimension, rate_table=rates, scan=scan,
                                         source_incomplete=incomplete, group_of=group_of)
        groups = [self._with_durations(g, depth_result) for g in part.groups]
        has_data = bool(override.points) if override is not None else bool(rows)
        reason = "source_unavailable" if incomplete else (None if has_data else "no_observations")
        totals_dto = dims.metrics_dto(totals, rows)
        warnings = {w for w in (getattr(priced.catalog, "warnings", ()) if priced else ("pricing_seed_invalid",))
                    if w in PRICING_WARNINGS}
        if totals_dto.api_equivalent.stale:
            warnings.add("pricing_stale")
        pricing_info = PricingInfo(
            priced.catalog.revision if priced else _UNAVAILABLE_REVISION,
            priced.catalog.origin if priced else "seed", tuple(sorted(warnings)), dims.unknown_row_ids(rows))
        data = {
            "window_days": query.window,
            "mode": mode,
            "dimension": dimension,
            "provider_id": wanted,
            "series": [to_plain(s) for s in part.series],
            "groups": [to_plain(g) for g in groups],
            "rows": [to_plain(dims.row_dto(r)) for r in rows],
            "totals": to_plain(totals_dto),
            "providers": [{"id": pid, "label": label} for pid, label
                          in dims.provider_options(rows_all, reads, wanted, label_of)],
            "sources": [{"profile": n, "is_global": g, "state": s} for n, g, s in sources],
            "complete": not incomplete,
            "top_n": TOP_N_MODELS if dimension == "model" else None,
            "reason": reason,
            "pricing": to_plain(pricing_info),
            **depth_result.data,
        }
        errors = [to_plain(make_error("analytics_unavailable"))] if incomplete else []
        return data, errors

    @staticmethod
    def _with_durations(group: Any, result: "depth.DepthResult") -> Any:
        """Attach the group's own duration stats (computed from its true session membership, never from other
        groups' percentiles); stays null while the depth projection is unavailable."""
        if result.data["durations"] is None:
            return group
        stats, samples = result.group_durations.get(group.id, (depth.duration_stats(())[0], 0))
        return replace(group, durations=stats, duration_samples=samples)

    def _depth_rates(self) -> "depth.RateTable":
        """The packaged cache-discount table; a broken file degrades to 'no known discount', never to an error."""
        try:
            return self._depth_rates_factory()
        except Exception as exc:  # noqa: BLE001 - cache economics is additive; usage must keep working
            log.warning("usage_deck cache discounts unavailable: %s", type(exc).__name__)
            return depth.EMPTY_RATE_TABLE

    def _read_series(self, query: Query, now: Optional[datetime] = None) -> Optional[Any]:
        now = now or self._clock()
        reader = self._analytics_factory(query.profile)
        ctx = AdapterContext(
            provider_id="profile", account_id="profile", provider_label="Profile", account_label="Profile",
            kind="cloud", analytics=reader, transport=GuardedTransport(self._transport, ()),
            clock=lambda: now,
        )
        result = SessionAnalyticsAdapter(ctx).get_usage(query.window)
        if result.error is not None or not result.series:
            return None
        return result.series[0]

    @staticmethod
    def _source_key(source: Any) -> str:
        """Cache identity of the inventory source: the viewed home AND the root whose stores it enumerates,
        so a snapshot can never leak across roots (machine inventory) or across profiles."""
        root_of = getattr(source, "root_dir", None)
        root = str(root_of()) if callable(root_of) else ""
        return f"{source.home_key()}|{root}"

    # -- deck snapshot (cards + health) ----------------------------------
    def _deck(self, query: Query) -> _Snapshot:
        source = self._inventory_factory()
        view = _effective_view(source, query)
        key = (self._source_key(source), query.profile or "", "deck", view)
        return self._cached(key, lambda: self._build_deck(source, query, view))

    def _build_deck(self, source: Any, query: Query, view: str = "profile") -> _Snapshot:
        items = discover_machine(source) if view == "machine" else discover(source)
        captures = self._capture_factory(source)
        now = self._clock()
        reader = MemoAnalyticsReader(self._analytics_factory(query.profile))
        endpoints = EndpointCache()
        health_owner: Dict[str, str] = {}
        for item in items:  # a card with a live source owns the endpoint's single health row
            # (the selected profile's own card first; a cross-profile scrape only when nothing else is live)
            if item.endpoint_id and item.live_source and not item.cross_profile and item.endpoint_id not in health_owner:
                health_owner[item.endpoint_id] = item.account_id
        for item in items:
            if item.endpoint_id and item.live_source and item.endpoint_id not in health_owner:
                health_owner[item.endpoint_id] = item.account_id
        for item in items:
            if item.endpoint_id and item.endpoint_id not in health_owner:
                health_owner[item.endpoint_id] = item.account_id

        executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="usage-deck")
        futures = {}
        try:
            for item in items:
                owns_health = bool(item.endpoint_id) and health_owner.get(item.endpoint_id) == item.account_id
                ctx = contextvars.copy_context()  # a Context may only be entered once at a time
                futures[executor.submit(ctx.run, self._run_item, item, reader, now, owns_health, endpoints, captures,
                                        view == "machine")] = item
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
        return _Snapshot(cards, health, self._monotonic(), view)

    def _context(self, item: InventoryItem, reader: AnalyticsReader, now: datetime,
                 endpoints: Optional[EndpointCache] = None,
                 captures: Optional[CaptureStore] = None,
                 machine_view: bool = False) -> AdapterContext:
        # Defence in depth (DESIGN.md §13): a cross-profile item outside the machine view is local
        # inventory again, whatever produced it: no credential, no live source, no target.
        blocked = item.cross_profile and not machine_view
        targets: List[AllowedTarget] = []
        transport: Any
        if item.cross_profile and not blocked:
            # DESIGN.md §13: another profile's credential/endpoint, dashboard MACHINE view only, read-only
            # usage probes only (the ceiling in ``CROSS_PROFILE_PROBES`` is re-checked on every request).
            if item.provider_id.strip().lower() == CODEX_PROVIDER_ID:
                targets.append(CODEX_TARGET)
            elif item.provider_id.strip().lower() == ANTHROPIC_PROVIDER_ID:
                targets.append(ANTHROPIC_TARGET)  # DESIGN.md §9.1.2: fixed, GET /api/oauth/usage only
            elif item.endpoint is not None and item.endpoint.safe:
                targets.append(AllowedTarget(item.endpoint.scheme, item.endpoint.host, item.endpoint.port, ("/metrics",)))
            transport = CrossProfileProbeTransport(GuardedTransport(self._transport, targets))
        else:
            if item.live_source and not blocked:  # machine-only inventory never gets an outbound target (§12.5)
                if item.provider_id.lower() == "openrouter":
                    targets.append(OPENROUTER_TARGET)
                if item.provider_id.strip().lower() == CODEX_PROVIDER_ID:
                    targets.append(CODEX_TARGET)  # DESIGN.md §9.2: fixed, never derived from claims/config/response
                if item.provider_id.strip().lower() == ANTHROPIC_PROVIDER_ID:
                    targets.append(ANTHROPIC_TARGET)  # DESIGN.md §9.1.2: fixed, never derived from config/response
                if item.endpoint is not None and item.endpoint.safe:
                    targets.append(AllowedTarget(item.endpoint.scheme, item.endpoint.host, item.endpoint.port, None))
            transport = GuardedTransport(self._transport, targets)
        return AdapterContext(
            provider_id=item.provider_id,
            account_id=item.account_id,
            provider_label=item.provider_label,
            account_label=item.account_label,
            kind=item.kind,
            analytics=reader,
            transport=transport,
            clock=lambda: now,
            endpoint=item.endpoint,
            endpoint_id=item.endpoint_id,
            credential=None if blocked else item.credential,
            auth_status=item.auth_status,
            plan_name=item.plan_name,
            plan_source=item.plan_source,
            secrets=item.secrets,
            health_credential=None if blocked else item.health_credential,
            endpoint_cache=endpoints,
            credential_refs=item.credential_refs,
            captures=captures,
            live_source=item.live_source and not blocked,
            cross_profile=item.cross_profile and not blocked,
            credential_expected=item.credential_expected,
            oauth_credential=item.oauth_credential and not blocked,
            credential_row_id=None if blocked else item.credential_row_id,
            credential_expires_at=item.expires_at,
            token_resolver=self._token_resolver,
        )

    def _run_item(self, item: InventoryItem, reader: AnalyticsReader, now: datetime,
                  owns_health: bool, endpoints: Optional[EndpointCache] = None,
                  captures: Optional[CaptureStore] = None, machine_view: bool = False) -> Tuple[dict, Optional[dict]]:
        adapter = create_adapter(self._context(item, reader, now, endpoints, captures, machine_view))
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
        elif any(m.value is not None or m.limit is not None or m.remaining is not None for m in meters):
            state, reason = "ready", None  # any non-null observation (e.g. limit-only) is a reading
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
            holders=item.holders,
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
            holders=item.holders,
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
