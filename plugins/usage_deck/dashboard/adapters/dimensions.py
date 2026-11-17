"""Dimension partitioning of session observations (design: analytics-dimensions-breakdown §4-§6).

Pure functions over ``session_db.HomeRead`` results: attribute a provider to every observation in its owning
home, aggregate provenance rows, filter by provider, partition by dimension (all / profile / model with a
top-N + Other roll-up) and build the per-group daily series and totals. Every partition is made of the same
filtered rows, so totals are conserved across dimensions by construction (sums are exactly rounded).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models.dto import (
    TOP_N_MODELS,
    Holder,
    ProvenanceRow,
    UsageGroup,
    UsageMetrics,
    UsageTimeSeries,
)
from .base import AnalyticsDay
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


# ------------------------------------------------------------------ DTO helpers


def metrics_dto(m: Metrics) -> UsageMetrics:
    return UsageMetrics(m.input_count, m.output_count, m.estimated_usd, m.sessions, m.api_calls,
                        tuple(sorted(m.missing)))


def row_dto(r: Row) -> ProvenanceRow:
    return ProvenanceRow(r.id, r.day, r.profile, r.is_global, "state.db", r.model_id, r.provider_id,
                         r.attribution_source, metrics_dto(r.metrics))


def daily_days(rows: Iterable[Row]) -> Tuple[AnalyticsDay, ...]:
    by_day: Dict[str, List[Metrics]] = {}
    for r in rows:
        by_day.setdefault(r.day, []).append(r.metrics)
    out = []
    for day in sorted(by_day):
        m = sum_metrics(by_day[day])
        out.append(AnalyticsDay(day, m.input_count, m.output_count, m.estimated_usd, m.sessions, m.api_calls))
    return tuple(out)


def _holders(rows: Iterable[Row]) -> Tuple[Holder, ...]:
    return tuple(sorted({Holder(r.profile, r.is_global) for r in rows}, key=lambda h: (h.profile, h.is_global)))


def _group_dto(group_id: str, label: str, series_id: str, model_id: Optional[str], rows: Sequence[Row],
               is_other: bool, totals: Optional[Metrics] = None) -> UsageGroup:
    return UsageGroup(
        id=group_id, label=label, series_id=series_id, model_id=model_id,
        provider_ids=tuple(sorted({r.provider_id for r in rows})), profiles=_holders(rows),
        row_ids=tuple(sorted(r.id for r in rows)),
        totals=metrics_dto(totals if totals is not None else sum_metrics(r.metrics for r in rows)),
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
