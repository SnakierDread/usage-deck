"""Analytics depth: durations, start-time heatmap, cache economics, repositories (design: analytics-depth-breakdown).

A pure module over the optional session-fact projection of ``session_db`` (one scan per home, inside the same
read-only transaction as the legacy counters). ``facts_from_reads`` validates and attributes the raw rows;
``build_depth`` derives every additive ``data`` member of the v2 usage envelope from ONE fact set, so the model
rollup, the repository rollup and the grand total are conserved by construction (and asserted).

Null doctrine (§2): an unrecorded observation is None, never 0. Sums are known-only (None operands dropped; no
known operand -> None; recorded zeros count). Ratios need complete operands for their whole population. Money
is summed as exact ``Decimal`` and only converted to a JSON float at the edge.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..models.dto import (
    DEPTH_MISSING_NAMES,
    HEATMAP_DAYS,
    HEATMAP_HOURS,
    MAX_PATH_LEN,
    CacheRow,
    DepthStatus,
    DepthTotals,
    DurationStats,
    HeatCell,
    Heatmap,
    HeatStart,
    ModelRow,
    RepoRow,
    to_plain,
)
from ..models.redaction import contains_secret, scrub_string
from .analytics import _count
from .session_db import STATE_ABSENT, STATE_OK, HomeRead, RawFact, attribute, safe_model_id

RATE_FILE = "cache-discounts-v1.json"
PACKAGE_DATA = Path(__file__).resolve().parent.parent / "pricing_data"
MAX_RATE_BYTES = 4 * 1024 * 1024
UNSCOPED = "Unscoped"
MAX_LABEL = 255
_RATE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MILLION = Decimal(1000000)


class DepthInvariantError(ValueError):
    """A conservation identity failed: a programming error, never an upstream condition."""


# ------------------------------------------------------------------ rate table (cache-discounts-v1.json)


class RateTableError(ValueError):
    """The packaged cache-discount projection is missing or malformed."""


@dataclass(frozen=True)
class RateRow:
    provider_id: str
    model: str
    pricing_version: str
    input_cost_per_million: Optional[Decimal]
    cache_read_cost_per_million: Optional[Decimal]

    @property
    def discount(self) -> Optional[Decimal]:
        """``cache read rate / input rate`` of this exact row; None unless input > 0 and cache read >= 0."""
        inp, read = self.input_cost_per_million, self.cache_read_cost_per_million
        if inp is None or read is None or inp <= 0 or read < 0:
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            return read / inp


@dataclass(frozen=True)
class RateTable:
    core_commit: Optional[str]
    rows: Mapping[Tuple[str, str], RateRow]
    #: sha256 of the packaged file's bytes (response-cache identity; changes whenever a rate changes).
    digest: str

    def lookup(self, provider_id: Optional[str], model: Optional[str]) -> Optional[RateRow]:
        """The row of this EXACT provider/model pair, else None: no fuzzy matching, no prefix stripping."""
        if provider_id is None or model is None:
            return None
        return self.rows.get((provider_id, model))


EMPTY_RATE_TABLE = RateTable(None, {}, hashlib.sha256(b"usage-deck:cache-discounts-unavailable").hexdigest())


def _decimal(value: Any, name: str) -> Optional[Decimal]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RateTableError(f"{name}: rates are decimal strings")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise RateTableError(f"{name}: not a decimal") from None
    if not number.is_finite() or number < 0:
        raise RateTableError(f"{name}: invalid rate")
    return number


def parse_rate_table(raw: bytes) -> RateTable:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RateTableError("not JSON") from None
    if not isinstance(doc, dict) or set(doc) != {"core_commit", "entries"} or not isinstance(doc["entries"], list):
        raise RateTableError("unexpected top-level shape")
    commit = doc["core_commit"]
    if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
        raise RateTableError("core_commit")
    rows: Dict[Tuple[str, str], RateRow] = {}
    keys = {"provider_id", "model", "pricing_version", "input_cost_per_million", "cache_read_cost_per_million"}
    for entry in doc["entries"]:
        if not isinstance(entry, dict) or set(entry) != keys:
            raise RateTableError("entry shape")
        provider, model, version = entry["provider_id"], entry["model"], entry["pricing_version"]
        if not all(isinstance(v, str) and v for v in (provider, model)) \
                or not isinstance(version, str) or not _RATE_VERSION_RE.match(version):
            raise RateTableError("entry identity")
        if (provider, model) in rows:
            raise RateTableError("duplicate provider/model entry")  # ambiguous tariff: refuse the whole file
        rows[(provider, model)] = RateRow(
            provider, model, version, _decimal(entry["input_cost_per_million"], "input"),
            _decimal(entry["cache_read_cost_per_million"], "cache_read"))
    return RateTable(commit, rows, hashlib.sha256(raw).hexdigest())


_RATE_CACHE: Dict[Tuple[str, int, int], RateTable] = {}


def load_rate_table(path: Optional[Path] = None) -> RateTable:
    """The packaged snapshot projection (local file only; never fetched, never read from another home)."""
    target = Path(path) if path is not None else PACKAGE_DATA / RATE_FILE
    try:
        info = os.stat(target)
        stamp = (str(target), info.st_mtime_ns, info.st_size)
        hit = _RATE_CACHE.get(stamp)
        if hit is not None:
            return hit
        if info.st_size > MAX_RATE_BYTES:
            raise RateTableError("too large")
        table = parse_rate_table(target.read_bytes())
    except OSError:
        raise RateTableError("unreadable") from None
    _RATE_CACHE.clear()
    _RATE_CACHE[stamp] = table
    return table


# ------------------------------------------------------------------ facts


@dataclass(frozen=True)
class SessionFact:
    """One validated, attributed session. Numeric observations are None when unrecorded or invalid."""

    holder: Tuple[bool, str]  # (is_global, profile) of the owning home
    day: str  # legacy local day of the counters (same SQL expression)
    started_at: float
    ended_at: Optional[float]
    model: Optional[str]
    provider: str
    attribution: str
    repo_root: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    reasoning_tokens: Optional[int]
    cost: Optional[Decimal]
    api_calls: Optional[int]
    absent: FrozenSet[str] = frozenset()  # projected columns the owning database lacks

    @property
    def total_tokens(self) -> Optional[int]:
        """Known input + output (never cache or reasoning); None when neither is recorded."""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def duration(self) -> Optional[float]:
        """``ended_at - started_at`` when both are finite and end >= start; open/invalid/negative -> None."""
        if self.ended_at is None or self.ended_at < self.started_at:
            return None
        value = self.ended_at - self.started_at
        return value if math.isfinite(value) else None


@dataclass(frozen=True)
class FactScan:
    """What the scan could and could not see (drives ``depth_status`` and the unavailable states)."""

    scanned: int = 0  # homes whose projection was read (an absent store is a successful empty scan)
    unavailable: int = 0  # authorized homes whose projection is unavailable
    excluded_started_at: int = 0  # rows rejected for an invalid start in scanned homes


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def valid_epoch(value: Any) -> Optional[float]:
    """A finite epoch (seconds) that the platform can convert to UTC, else None (never bucketed as 'now')."""
    number = _finite(value)
    if number is None:
        return None
    try:
        datetime.fromtimestamp(number, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else number


def _cost(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return Decimal(repr(value))
    return Decimal(value) if value >= 0 else None


def make_fact(raw: RawFact, read: HomeRead, absent: FrozenSet[str]) -> Optional[SessionFact]:
    """Validate and attribute one raw row; None when its start cannot bucket (counted by the caller)."""
    started = valid_epoch(raw.started_at)
    if started is None or not isinstance(raw.day, str):
        return None
    model = safe_model_id(raw.model)
    provider, source = attribute(model, raw.provider, raw.billing, read.models)
    ended = valid_epoch(raw.ended_at)
    return SessionFact(
        holder=(read.is_global, read.profile), day=raw.day, started_at=started, ended_at=ended,
        model=model, provider=provider, attribution=source,
        repo_root=raw.repo_root if isinstance(raw.repo_root, str) else None,
        input_tokens=_count(raw.input_tokens), output_tokens=_count(raw.output_tokens),
        cache_read_tokens=_count(raw.cache_read_tokens), cache_write_tokens=_count(raw.cache_write_tokens),
        reasoning_tokens=_count(raw.reasoning_tokens), cost=_cost(raw.cost), api_calls=_count(raw.api_calls),
        absent=absent)


def facts_from_reads(reads: Iterable[HomeRead]) -> Tuple[Tuple[SessionFact, ...], FactScan]:
    """Every authorized home's facts (unfiltered) plus what the scan covered."""
    facts: List[SessionFact] = []
    scanned = unavailable = excluded = 0
    for read in reads:
        if read.state == STATE_ABSENT:
            scanned += 1  # no store: a successful scan with nothing in it
            continue
        if read.state != STATE_OK or read.facts is None:
            unavailable += 1
            continue
        scanned += 1
        for raw in read.facts.rows:
            fact = make_fact(raw, read, read.facts.absent)
            if fact is None:
                excluded += 1
            else:
                facts.append(fact)
    return tuple(facts), FactScan(scanned, unavailable, excluded)


def filter_facts(facts: Sequence[SessionFact], keep: Callable[[str], bool]) -> List[SessionFact]:
    """Scope facts with the SAME provider predicate the counters use (``keep(provider_id)``)."""
    return [f for f in facts if keep(f.provider)]


# ------------------------------------------------------------------ arithmetic


def known_sum(values: Iterable[Any]) -> Any:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def duration_samples(facts: Iterable[SessionFact]) -> List[float]:
    return sorted(d for d in (f.duration for f in facts) if d is not None)


def _percentile(sorted_samples: Sequence[float], p: Fraction) -> float:
    """Linear interpolation ``h=(n-1)p``; exact rational arithmetic so [0,10,20,30] gives exactly 15/27/29.7."""
    h = (len(sorted_samples) - 1) * p
    lo = h.numerator // h.denominator
    hi = min(lo + 1, len(sorted_samples) - 1)
    a, b = Fraction(sorted_samples[lo]), Fraction(sorted_samples[hi])
    return float(a + (h - lo) * (b - a))


def duration_stats(samples: Sequence[float]) -> Tuple[DurationStats, int]:
    """``(stats, n)`` of ``samples`` (sorted ascending). No sample -> all-null stats and n == 0."""
    n = len(samples)
    if n == 0:
        return DurationStats(None, None, None, None, None), 0
    return DurationStats(
        _percentile(samples, Fraction(50, 100)), _percentile(samples, Fraction(90, 100)),
        _percentile(samples, Fraction(99, 100)), math.fsum(samples) / n, float(samples[-1])), n


class _Acc:
    """Known-only accumulator of the shared depth basis: sessions, input+output tokens, recorded cost."""

    __slots__ = ("sessions", "tokens", "cost")

    def __init__(self) -> None:
        self.sessions = 0
        self.tokens: Optional[int] = None
        self.cost: Optional[Decimal] = None

    def add(self, fact: SessionFact) -> None:
        self.sessions += 1
        tokens = fact.total_tokens
        if tokens is not None:
            self.tokens = tokens if self.tokens is None else self.tokens + tokens
        if fact.cost is not None:
            self.cost = fact.cost if self.cost is None else self.cost + fact.cost


# ------------------------------------------------------------------ heatmap


def build_heatmap(facts: Sequence[SessionFact]) -> Heatmap:
    """UTC weekday×hour start cohorts plus the exact start epochs (design §4). Each session counts once."""
    grid = [[_Acc() for _ in range(HEATMAP_HOURS)] for _ in range(HEATMAP_DAYS)]
    starts: Dict[float, _Acc] = {}
    for fact in facts:
        moment = datetime.fromtimestamp(fact.started_at, timezone.utc)
        grid[moment.weekday()][moment.hour].add(fact)
        starts.setdefault(fact.started_at, _Acc()).add(fact)
    return Heatmap(
        tuple(tuple(HeatCell(c.sessions, c.tokens) for c in row) for row in grid),
        tuple(HeatStart(epoch, acc.sessions, acc.tokens) for epoch, acc in sorted(starts.items())))


# ------------------------------------------------------------------ repositories


def repo_key(root: Any) -> Optional[str]:
    """Lexical basename of a recorded repository root, else None (the missing/Unscoped key).

    Surrounding whitespace and trailing ``/`` or ``\\`` are trimmed, both separators split, nothing is resolved
    (no stat, no git, no cwd). Case is preserved. Blank / separator-only / blank-basename roots are missing."""
    if not isinstance(root, str):
        return None
    text = root.strip().rstrip("/\\")
    if not text:
        return None
    base = re.split(r"[\\/]", text)[-1]
    return base if base.strip() else None


def _canonical_path(root: Any) -> Optional[str]:
    if not isinstance(root, str):
        return None
    text = root.strip().rstrip("/\\")
    return text or None


def display_path(root: Any) -> Optional[str]:
    """The sanitized full path for the detail view, or None when it cannot be shown safely."""
    path = _canonical_path(root)
    if path is None or len(path) > MAX_PATH_LEN or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return None
    return scrub_string(path)


def display_label(key: Optional[str]) -> str:
    """The text label of a repository group (control characters never reach the wire)."""
    if key is None:
        return UNSCOPED
    text = "".join("\ufffd" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in key)
    return scrub_string(text[:MAX_LABEL])


def repo_group_id(key: Optional[str]) -> str:
    """``repo:null`` | ``repo:"<json basename>"`` | ``repo:sha256:<hex>`` (secret-bearing/oversized/control names)."""
    if key is None:
        return "repo:null"
    plain = len(key) <= 128 and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in key) \
        and not contains_secret(key)
    if plain:
        encoded = json.dumps(key, ensure_ascii=True)
        if len(encoded) - 2 <= 768:
            return "repo:" + encoded
    return "repo:sha256:" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()


def repo_paths_of(facts: Iterable[SessionFact]) -> Tuple[str, ...]:
    return tuple(sorted({p for p in (display_path(f.repo_root) for f in facts) if p is not None}))


def _by_repo(facts: Sequence[SessionFact]) -> Dict[Optional[str], List[SessionFact]]:
    out: Dict[Optional[str], List[SessionFact]] = {}
    for fact in facts:
        out.setdefault(repo_key(fact.repo_root), []).append(fact)
    return out


def _repo_order(item: Tuple[Optional[str], _Acc]) -> tuple:
    key, acc = item
    return (key is None, acc.tokens is None, -(acc.tokens or 0), key or "")


def build_repo_rollup(facts: Sequence[SessionFact]) -> Tuple[RepoRow, ...]:
    groups = _by_repo(facts)
    groups.setdefault(None, [])  # the Unscoped row is always present, even empty
    accs = {}
    for key, members in groups.items():
        acc = _Acc()
        for fact in members:
            acc.add(fact)
        accs[key] = acc
    rows = []
    for key, acc in sorted(accs.items(), key=_repo_order):
        paths = repo_paths_of(groups[key])
        rows.append(RepoRow(
            display_label(key), paths[0] if len(paths) == 1 else None, acc.sessions, acc.tokens,
            _float(acc.cost), paths))
    return tuple(rows)


# ------------------------------------------------------------------ model rollup and cache economics


def _pair_order(pair: Tuple[Optional[str], Optional[str]]) -> tuple:
    model, provider = pair
    return (model is not None, model or "", provider is not None, provider or "")


def _by_pair(facts: Sequence[SessionFact]) -> Dict[Tuple[Optional[str], Optional[str]], List[SessionFact]]:
    out: Dict[Tuple[Optional[str], Optional[str]], List[SessionFact]] = {}
    for fact in facts:
        out.setdefault((fact.model, fact.provider), []).append(fact)
    return out


def build_model_rollup(facts: Sequence[SessionFact]) -> Tuple[ModelRow, ...]:
    rows = []
    for pair, members in sorted(_by_pair(facts).items(), key=lambda kv: _pair_order(kv[0])):
        acc = _Acc()
        for fact in members:
            acc.add(fact)
        rows.append(ModelRow(pair[0], pair[1], acc.sessions, acc.tokens, _float(acc.cost)))
    return tuple(rows)


def _share(numerator: int, denominator: int) -> float:
    return float(Fraction(100 * numerator, denominator))


def cache_row(model: Optional[str], provider: Optional[str], members: Sequence[SessionFact],
              rates: RateTable, source_incomplete: bool) -> CacheRow:
    """Cache economics of one exact model/provider pair (design §5). Every ratio is computed from raw sums."""
    inp = known_sum(f.input_tokens for f in members)
    out = known_sum(f.output_tokens for f in members)
    reasoning = known_sum(f.reasoning_tokens for f in members)
    write = known_sum(f.cache_write_tokens for f in members)
    read = known_sum(f.cache_read_tokens for f in members)
    rate = rates.lookup(provider, model)
    d = rate.discount if rate is not None else None

    def complete(*names: str) -> bool:
        return all(getattr(f, n) is not None for f in members for n in names)

    usage_known = complete("input_tokens", "output_tokens", "reasoning_tokens", "cache_write_tokens",
                           "cache_read_tokens")
    cost_known = all(f.cost is not None for f in members)

    hit_rate: Optional[float] = None
    hit_denominator = None
    if complete("input_tokens", "cache_write_tokens", "cache_read_tokens"):
        hit_denominator = (inp or 0) + (write or 0) + (read or 0)  # writes are input misses, not hits
        if hit_denominator > 0 and not source_incomplete:
            hit_rate = _share(read or 0, hit_denominator)

    share: Optional[float] = None
    if complete("reasoning_tokens", "output_tokens") and (out or 0) > 0 and not source_incomplete \
            and all(f.reasoning_tokens <= f.output_tokens for f in members):  # type: ignore[operator]
        share = _share(reasoning or 0, out or 0)

    effective: Optional[float] = None
    weighted: Optional[Decimal] = None
    unknown_discount = False
    if usage_known:
        if d is not None:
            cached_weight: Optional[Decimal] = Decimal(read or 0) * d
        elif (read or 0) == 0:
            cached_weight = Decimal(0)  # provably no cache contribution: the missing tariff is irrelevant
        else:
            cached_weight, unknown_discount = None, True
        if cached_weight is not None:
            weighted = Decimal((inp or 0) + (out or 0) + (reasoning or 0) + (write or 0)) + cached_weight
            if cost_known and weighted > 0 and not source_incomplete:
                total_cost = sum((f.cost for f in members), Decimal(0))  # type: ignore[arg-type]
                with localcontext() as ctx:
                    ctx.prec = 60
                    effective = float(_MILLION * total_cost / weighted)

    reason = None
    zero_denominator = (hit_denominator == 0) or (weighted is not None and weighted == 0)
    for code, applies in (("missing_usage", not usage_known), ("missing_cost", not cost_known),
                          ("unknown_discount", unknown_discount), ("zero_denominator", zero_denominator),
                          ("incomplete_source", source_incomplete)):
        if applies:
            reason = code
            break
    return CacheRow(
        model=model, provider_id=provider, hit_rate_pct=hit_rate, cached_input_tokens=read,
        uncached_input_tokens=inp, reasoning_tokens=reasoning, effective_cost_per_mtok=effective,
        cache_write_tokens=write, output_tokens=out, reasoning_share_pct=share,
        discount_multiplier=_float(d), rate_source=rate.pricing_version if rate is not None else None,
        reason=reason)


def build_cache_metrics(facts: Sequence[SessionFact], rates: RateTable, source_incomplete: bool) -> Tuple[CacheRow, ...]:
    return tuple(cache_row(m, p, members, rates, source_incomplete)
                 for (m, p), members in sorted(_by_pair(facts).items(), key=lambda kv: _pair_order(kv[0])))


# ------------------------------------------------------------------ the whole thing


@dataclass(frozen=True)
class DepthResult:
    """``data``: the eight additive members of the usage envelope (plain JSON values);
    ``group_durations``: duration stats of each group's true session membership, keyed by group id."""

    data: Dict[str, Any]
    group_durations: Dict[str, Tuple[DurationStats, int]]


UNAVAILABLE_DATA: Dict[str, Any] = {
    "durations": None, "duration_samples": None, "heatmap": None, "cache_metrics": None, "repo_rollup": None,
    "model_rollup": None, "depth_totals": None,
    "depth_status": {"partial": True, "excluded_started_at": None, "missing": {n: None for n in DEPTH_MISSING_NAMES}},
}


def missing_counts(facts: Sequence[SessionFact]) -> Dict[str, int]:
    """Included sessions lacking a usable value, per fixed name (design §2)."""
    attr = {"input_tokens": "input_tokens", "output_tokens": "output_tokens", "estimated_cost_usd": "cost",
            "cache_read_tokens": "cache_read_tokens", "cache_write_tokens": "cache_write_tokens",
            "reasoning_tokens": "reasoning_tokens"}
    counts = {name: sum(1 for f in facts if getattr(f, field) is None) for name, field in attr.items()}
    counts["duration"] = sum(1 for f in facts if f.duration is None)
    return {name: counts[name] for name in DEPTH_MISSING_NAMES}


def assert_conserved(totals: DepthTotals, repos: Sequence[RepoRow], models: Sequence[ModelRow],
                     facts: Sequence[SessionFact]) -> None:
    """Design §6: sum(repo) == sum(model) == total for sessions, tokens and money.

    Integers are compared exactly. Money is first regrouped from the facts in exact ``Decimal`` (so a dropped or
    duplicated session cannot hide behind float rounding); the already-rounded wire floats are then compared
    within the wire tolerance (<= 1e-12 USD per unit of magnitude)."""
    want_cost = known_sum(f.cost for f in facts)
    if totals.sessions != len(facts):
        raise DepthInvariantError("depth_totals.sessions != fact count")
    for name, keyed in (("repo_rollup", _by_repo(facts)), ("model_rollup", _by_pair(facts))):
        grouped = known_sum(known_sum(f.cost for f in members) for members in keyed.values())
        if grouped != want_cost or sum(len(m) for m in keyed.values()) != len(facts):
            raise DepthInvariantError(f"{name}: facts are not partitioned exactly once")
    for name, rows in (("repo_rollup", repos), ("model_rollup", models)):
        if sum(r.sessions for r in rows) != totals.sessions:
            raise DepthInvariantError(f"{name}: sessions do not conserve")
        if known_sum(r.total_tokens for r in rows) != totals.total_tokens:
            raise DepthInvariantError(f"{name}: total_tokens do not conserve")
        got = known_sum(r.total_cost for r in rows)
        if (got is None) != (want_cost is None):
            raise DepthInvariantError(f"{name}: total_cost null-ness does not conserve")
        if got is not None and abs(got - float(want_cost)) > 1e-12 * max(1.0, abs(got)) * max(1, len(rows)):
            raise DepthInvariantError(f"{name}: total_cost does not conserve")


def build_depth(facts: Sequence[SessionFact], *, dimension: str, rate_table: RateTable,
                scan: Optional[FactScan] = None, source_incomplete: bool = False,
                group_of: Optional[Callable[[SessionFact], Optional[str]]] = None) -> DepthResult:
    """All additive depth members from ONE fact set.

    ``scan`` discloses what could not be read; ``source_incomplete`` is the service's overall incompleteness
    (unreadable/excluded/truncated homes) and nulls every ratio. ``group_of`` maps a fact to the id of the
    ``data.groups`` entry it belongs to for the requested ``dimension`` (None = in no group)."""
    scan = scan or FactScan(scanned=1)
    if scan.scanned == 0 and scan.unavailable > 0:
        return DepthResult(json.loads(json.dumps(UNAVAILABLE_DATA)), {})
    incomplete = source_incomplete or scan.unavailable > 0
    stats, samples = duration_stats(duration_samples(facts))
    totals_acc = _Acc()
    for fact in facts:
        totals_acc.add(fact)
    totals = DepthTotals(totals_acc.sessions, totals_acc.tokens, _float(totals_acc.cost))
    repos = build_repo_rollup(facts)
    models = build_model_rollup(facts)
    assert_conserved(totals, repos, models, facts)
    missing = missing_counts(facts)
    status = DepthStatus(
        partial=incomplete or scan.excluded_started_at > 0 or any(missing.values()),
        excluded_started_at=scan.excluded_started_at, missing=missing)
    data = {
        "durations": to_plain(stats), "duration_samples": samples,
        "heatmap": to_plain(build_heatmap(facts)),
        "cache_metrics": to_plain(build_cache_metrics(facts, rate_table, incomplete)),
        "repo_rollup": to_plain(repos), "model_rollup": to_plain(models),
        "depth_totals": to_plain(totals), "depth_status": to_plain(status),
    }
    group_durations: Dict[str, Tuple[DurationStats, int]] = {}
    if group_of is not None:
        members: Dict[str, List[SessionFact]] = {}
        for fact in facts:
            gid = group_of(fact)
            if gid is not None:
                members.setdefault(gid, []).append(fact)
        group_durations = {gid: duration_stats(duration_samples(m)) for gid, m in members.items()}
    return DepthResult(data, group_durations)
