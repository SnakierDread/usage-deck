"""Dimension partitioning of session observations (design: analytics-dimensions-breakdown §4-§6).

Pure functions over ``session_db.HomeRead`` results: attribute a provider to every observation in its owning
home, aggregate provenance rows, filter by provider, partition by dimension (all / profile / model with a
top-N + Other roll-up) and build the per-group daily series and totals. Every partition is made of the same
filtered rows, so totals are conserved across dimensions by construction (sums are exactly rounded).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models.dto import (
    EMPTY_API_EQUIVALENT,
    NO_ROW_PRICING,
    TOP_N_MODELS,
    ApiEquivalent,
    Holder,
    ProvenanceRow,
    RowPricing,
    UsageGroup,
    UsageMetrics,
    UsageTimeSeries,
    api_usd,
)
from .base import AnalyticsDay
from .analytics_depth import SessionFact, display_label, repo_group_id, repo_key, repo_paths_of
from .fallback import PROFILE_SERIES_ID, build_series
from .session_db import (
    STATE_OK,
    UNKNOWN,
    HomeRead,
    Metrics,
    attribute,
    known_tokens,
    provider_matches,
    safe_model_id,
    sum_metrics,
)

GLOBAL_LABEL = "global (default home)"


@dataclass(frozen=True)
class Row:
    """A daily aggregate for one ``(home, day, model, provider, attribution)``."""

    id: str
    day: str
    profile: str
    is_global: bool
    model_id: Optional[str]
    provider_id: str
    attribution_source: str
    metrics: Metrics
    #: ``pricing.PriceResolution`` of this leaf once annotated (``annotate_rows``); None = not priced.
    price: Optional[Any] = None
    #: By-repo leaves only: display basename (None = unscoped) and the sanitized contributing full paths.
    repo_basename: Optional[str] = None
    repo_paths: Optional[Tuple[str, ...]] = None
    #: By-repo leaves only: the group identity (``(basename,)`` for a repository, ``()`` for unscoped).
    repo_key: Optional[Tuple[str, ...]] = None

    @property
    def holder(self) -> Tuple[bool, str]:
        return self.is_global, self.profile


def _row_id(parts: list) -> str:
    return "row_" + hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def observation_rows(reads: Iterable[HomeRead]) -> List[Row]:
    """Provenance rows of every readable home, attributed in the home that owns them (unfiltered)."""
    merged: Dict[tuple, List[Metrics]] = {}
    for read in reads:
        if read.state != STATE_OK:
            continue
        for group in read.groups:
            model = safe_model_id(group.model)
            provider, source = attribute(model, group.provider, group.billing, read.models)
            merged.setdefault((read.is_global, read.profile, group.day, model, provider, source), []).append(
                group.metrics)
    rows = [Row(_row_id(list(key)), key[2], key[1], key[0], key[3], key[4], key[5], sum_metrics(parts))
            for key, parts in merged.items()]
    rows.sort(key=lambda r: (r.day, not r.is_global, r.profile, r.model_id is None, r.model_id or "",
                             r.provider_id, r.attribution_source))
    return rows


def filter_rows(rows: Sequence[Row], provider: Optional[str]) -> List[Row]:
    if provider is None:
        return list(rows)
    return [r for r in rows if provider_matches(r.provider_id, provider)]


def _leaf_metrics(members: Sequence[SessionFact]) -> Metrics:
    """Legacy counter formulas (including the old ``COALESCE(SUM(cost), 0)``) over one repo leaf's facts.

    Only the by-repo view uses this; on complete valid data it equals the SQL counters of the same sessions."""
    absent = members[0].absent
    n = len(members)
    missing = set()
    ins = [f.input_tokens for f in members if f.input_tokens is not None]
    outs = [f.output_tokens for f in members if f.output_tokens is not None]
    if "input_tokens" in absent or len(ins) < n:
        missing.add("input_count")
    if "output_tokens" in absent or len(outs) < n:
        missing.add("output_count")
    usd: Optional[float] = None
    if "estimated_cost_usd" in absent:
        missing.add("estimated_usd")
    else:
        usd = float(sum((f.cost for f in members if f.cost is not None), Decimal(0)))
    calls: Optional[int] = None
    if "api_call_count" in absent:
        missing.add("api_calls")
    else:
        calls = sum(f.api_calls or 0 for f in members)
    return Metrics(input_count=sum(ins) if ins else None, output_count=sum(outs) if outs else None,
                   estimated_usd=usd, sessions=n, api_calls=calls, missing=frozenset(missing))


def repo_rows(facts: Iterable[SessionFact]) -> List[Row]:
    """By-repo provenance leaves keyed ``(home, local day, exact model, attributed provider, repo key)`` (§6).

    New leaves with their own ids: the legacy rows (and their ids) of the other dimensions are untouched."""
    merged: Dict[tuple, List[SessionFact]] = {}
    for fact in facts:
        merged.setdefault((fact.holder, fact.day, fact.model, fact.provider, fact.attribution,
                           repo_key(fact.repo_root)), []).append(fact)
    rows = []
    for (holder, day, model, provider, source, key), members in merged.items():
        rows.append(Row(
            _row_id([holder[0], holder[1], day, model, provider, source, "repo", key]), day, holder[1], holder[0],
            model, provider, source, _leaf_metrics(members), None,
            display_label(key) if key is not None else None, repo_paths_of(members),
            (key,) if key is not None else ()))
    rows.sort(key=lambda r: (r.day, not r.is_global, r.profile, r.model_id is None, r.model_id or "",
                             r.provider_id, r.attribution_source, not r.repo_key, r.repo_key))
    return rows


def known_provider_ids(rows: Sequence[Row], reads: Iterable[HomeRead]) -> Set[str]:
    """Provider ids a filter may name: present in the unfiltered rows or in a scoped home's local catalog."""
    known = {r.provider_id for r in rows}
    for read in reads:
        known.update(p for p in read.models.values() if p)
    if any(p == "custom" or p.startswith("custom:") for p in known):
        known.add("custom")  # the roll-up
    known.add(UNKNOWN)
    return known


def provider_options(rows: Sequence[Row], reads: Iterable[HomeRead], selected: Optional[str],
                     label_of: Callable[[str], str]) -> List[Tuple[str, str]]:
    """``(id, label)`` options from the unfiltered rows (+ catalog providers, + the selected one), Unknown last."""
    ids = {r.provider_id for r in rows}
    if selected:
        ids.add(selected)
    if any(p == "custom" or p.startswith("custom:") for p in ids):
        ids.add("custom")
    return sorted(((p, label_of(p)) for p in ids), key=lambda o: (o[0] == UNKNOWN, o[1].lower(), o[0]))


# ------------------------------------------------------------------ API-equivalent annotation


def annotate_rows(rows: Iterable[Row], resolve: Callable[[Optional[str], Optional[int], Optional[int]], Any]) -> List[Row]:
    """Price every provenance leaf exactly once (design §6): ``resolve(model_id, input, output)`` is the pure
    resolver bound to one frozen catalog/alias snapshot. The stored counters are never touched."""
    return [replace(r, price=resolve(r.model_id, r.metrics.input_count, r.metrics.output_count)) for r in rows]


def _amount(price: Any) -> Optional[int]:
    """Integer pico-USD of one leaf, None when unpriced (never zero for unknown)."""
    amount = getattr(price, "amount_pico_usd", None) if price is not None else None
    return amount if isinstance(amount, int) and not isinstance(amount, bool) else None


def api_aggregate(rows: Iterable[Row]) -> Tuple[Optional[float], ApiEquivalent]:
    """Aggregate the leaf integers of ``rows``: known amounts sum exactly, unknown leaves are excluded (never
    priced at zero) and counted; no known leaf -> null amount."""
    total: Optional[int] = None
    priced = unpriced = proxy = 0
    stale = False
    for r in rows:
        amount = _amount(r.price)
        if amount is None:
            unpriced += 1
            continue
        total = amount if total is None else total + amount
        priced += 1
        proxy += 1 if getattr(r.price, "alias_kind", None) == "proxy" else 0
        stale = stale or bool(getattr(r.price, "stale", False))
    text = None if total is None else str(total)
    return api_usd(text), ApiEquivalent(text, priced, unpriced, proxy, unpriced > 0, stale)


def _fmt_fetched(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    utc = moment.astimezone(timezone.utc)
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return base + (f".{utc.microsecond:06d}" if utc.microsecond else "") + "Z"


def row_pricing(price: Any) -> RowPricing:
    """The eight-field wire description of one resolution (the not-annotated row reads as unresolved)."""
    if price is None:
        return NO_ROW_PRICING
    return RowPricing(
        catalog_key=price.catalog_key, method=price.method, alias_kind=price.alias_kind, note=price.note,
        reason=None if _amount(price) is not None else (price.reason or "no_catalog_match"),
        source=price.source, fetched_at=_fmt_fetched(price.fetched_at), stale=bool(price.stale))


def unknown_row_ids(rows: Iterable[Row]) -> Tuple[str, ...]:
    return tuple(sorted(r.id for r in rows if _amount(r.price) is None))


# ------------------------------------------------------------------ DTO helpers


def metrics_dto(m: Metrics, rows: Iterable[Row] = ()) -> UsageMetrics:
    """Baseline counters (old fields unchanged) plus the API-equivalent aggregate of the member leaves."""
    usd, api = api_aggregate(rows)
    return UsageMetrics(m.input_count, m.output_count, m.estimated_usd, m.sessions, m.api_calls,
                        tuple(sorted(m.missing)), usd, api)


def row_dto(r: Row) -> ProvenanceRow:
    return ProvenanceRow(r.id, r.day, r.profile, r.is_global, "state.db", r.model_id, r.provider_id,
                         r.attribution_source, metrics_dto(r.metrics, (r,)), row_pricing(r.price),
                         r.repo_basename, r.repo_paths)


def daily_days(rows: Iterable[Row]) -> Tuple[AnalyticsDay, ...]:
    by_day: Dict[str, List[Row]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)
    out = []
    for day in sorted(by_day):
        m = sum_metrics(r.metrics for r in by_day[day])
        usd, api = api_aggregate(by_day[day])
        out.append(AnalyticsDay(day, m.input_count, m.output_count, m.estimated_usd, m.sessions, m.api_calls,
                                usd, api))
    return tuple(out)


def with_row_api(series: UsageTimeSeries, rows: Sequence[Row]) -> UsageTimeSeries:
    """Replace ONLY the API-equivalent fields of a legacy (host) series with the scoped session leaves of the
    same day; every old point field is preserved and a day with no matching leaf is null."""
    by_day: Dict[str, List[Row]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r)
    points = []
    for p in series.points:
        usd, api = api_aggregate(by_day.get(p.day, ()))
        points.append(replace(p, api_equivalent_usd=usd, api_equivalent=api))
    return replace(series, points=tuple(points))


def _holders(rows: Iterable[Row]) -> Tuple[Holder, ...]:
    return tuple(sorted({Holder(r.profile, r.is_global) for r in rows}, key=lambda h: (h.profile, h.is_global)))


def _group_dto(group_id: str, label: str, series_id: str, model_id: Optional[str], rows: Sequence[Row],
               is_other: bool, totals: Optional[Metrics] = None) -> UsageGroup:
    return UsageGroup(
        id=group_id, label=label, series_id=series_id, model_id=model_id,
        provider_ids=tuple(sorted({r.provider_id for r in rows})), profiles=_holders(rows),
        row_ids=tuple(sorted(r.id for r in rows)),
        totals=metrics_dto(totals if totals is not None else sum_metrics(r.metrics for r in rows), rows),
        is_other=is_other)


def profile_group_id(is_global: bool, profile: str) -> str:
    return "profile:" + json.dumps([is_global, profile], separators=(",", ":"), ensure_ascii=True)


def model_group_id(model_id: Optional[str]) -> str:
    return "model:" + json.dumps(model_id, separators=(",", ":"), ensure_ascii=True)


def profile_label(is_global: bool, profile: str) -> str:
    return GLOBAL_LABEL if is_global else profile


def model_label(model_id: Optional[str], rows: Sequence[Row], label_of: Callable[[str], str]) -> str:
    providers = sorted({label_of(r.provider_id) for r in rows})
    return f"{model_id if model_id is not None else 'Unknown model'} ({', '.join(providers)})"


# ------------------------------------------------------------------ partition


@dataclass(frozen=True)
class Partition:
    groups: Tuple[UsageGroup, ...]
    series: Tuple[UsageTimeSeries, ...]


def rank_models(rows: Sequence[Row]) -> List[Tuple[Optional[str], List[Row]]]:
    """Model groups ordered by known input+output tokens desc; all-unknown after any number; ties by exact
    model id, the null model last."""
    by_model: Dict[Optional[str], List[Row]] = {}
    for r in rows:
        by_model.setdefault(r.model_id, []).append(r)

    def key(item: Tuple[Optional[str], List[Row]]):
        total = known_tokens(sum_metrics(r.metrics for r in item[1]))
        return (total is None, -(total or 0), item[0] is None, item[0] or "")

    return sorted(by_model.items(), key=key)


def partition(rows: Sequence[Row], dimension: str, mode: str, window: int, now, provider: Optional[str],
              label_of: Callable[[str], str],
              override_all: Optional[Tuple[UsageTimeSeries, Metrics]] = None) -> Partition:
    """Build groups + their series for ``dimension``. ``override_all`` replaces the All group's series and
    totals with the baseline (host) numbers so profile-mode All keeps its exact current arithmetic."""
    scope = "machine" if mode == "machine" else "profile"

    def series_of(series_id: str, members: Sequence[Row]) -> UsageTimeSeries:
        return build_series(daily_days(members), window, now, series_id, scope, provider)

    if dimension == "all":
        series_id = PROFILE_SERIES_ID if mode == "profile" else "all"
        if override_all is not None:
            legacy_series, legacy_totals = override_all
            legacy_series = with_row_api(legacy_series, rows)
            return Partition((_group_dto("all", "All", series_id, None, rows, False, legacy_totals),), (legacy_series,))
        return Partition((_group_dto("all", "All", series_id, None, rows, False),), (series_of(series_id, rows),))
    if not rows:
        return Partition((), ())
    groups: List[UsageGroup] = []
    series: List[UsageTimeSeries] = []
    if dimension == "profile":
        by_holder: Dict[Tuple[bool, str], List[Row]] = {}
        for r in rows:
            by_holder.setdefault(r.holder, []).append(r)
        for holder in sorted(by_holder, key=lambda h: (not h[0], h[1])):
            members = by_holder[holder]
            gid = profile_group_id(*holder)
            groups.append(_group_dto(gid, profile_label(*holder), gid, None, members, False))
            series.append(series_of(gid, members))
        return Partition(tuple(groups), tuple(series))
    if dimension == "repo":  # design §6: every repository (no top-N, no Other); missing bucket last
        by_repo: Dict[Tuple[str, ...], List[Row]] = {}
        for r in rows:
            by_repo.setdefault(r.repo_key or (), []).append(r)

        def repo_rank(key: Tuple[str, ...]):
            total = known_tokens(sum_metrics(r.metrics for r in by_repo[key]))
            return (not key, total is None, -(total or 0), key[0] if key else "")

        for key in sorted(by_repo, key=repo_rank):
            members = by_repo[key]
            gid = repo_group_id(key[0] if key else None)
            groups.append(_group_dto(gid, display_label(key[0] if key else None), gid, None, members, False))
            series.append(series_of(gid, members))
        return Partition(tuple(groups), tuple(series))
    ranked = rank_models(rows)  # dimension == "model"
    kept, rest = ranked[:TOP_N_MODELS], ranked[TOP_N_MODELS:]
    for model_id, members in kept:
        gid = model_group_id(model_id)
        groups.append(_group_dto(gid, model_label(model_id, members, label_of), gid, model_id, members, False))
        series.append(series_of(gid, members))
    if rest:
        others = [r for _model, members in rest for r in members]
        groups.append(_group_dto("other", "Other", "other", None, others, True))
        series.append(series_of("other", others))
    return Partition(tuple(groups), tuple(series))
