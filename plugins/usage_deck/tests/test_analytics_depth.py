"""Analytics depth: durations, start-time heatmap, cache economics, repositories, conservation, read-only.

Design: docs/design/analytics-depth-breakdown.md (§9 test contract). Expected values are computed in this
harness from literal vectors / the fixture specs (``depth_fixture``), never from the code under test. Every
database is a synthetic temp file; no network is reachable (the service tests patch ``socket.socket``).
"""

import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from plugins.usage_deck.adapters import analytics_depth as depth
from plugins.usage_deck.adapters.session_db import HomeFacts, HomeRead, RawFact, SessionDbReader
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.dto import DtoError, check_envelope, usage_from_wire

from . import depth_fixture as fx
from .depth_fixture import A, B, D, NOW_TS, ROOT, Machine, utc
from .test_analytics_dimensions import DirectSqlAnalytics  # noqa: F401  (fixture machinery)

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_CHECKOUT = Path(os.environ.get("USAGE_DECK_CORE_CHECKOUT", Path.home() / ".hermes" / "hermes-agent"))
CORE_PIN = "3b421578588243f920886e6fe79a6faa87ea026e"
#: sha256 of the packaged cache-discounts-v1.json. Update ONLY with a deliberate re-pin of the core snapshot.
PACKAGED_DIGEST = "ff60040c7b734b8009f10d84015ddcb1e63ef0454cdf0c8eb964319c775b9ef4"


def rf(**kw):
    base = dict(day="2026-11-05", started_at=utc(2026, 11, 5, 12), ended_at=None, last_activity_at=None,
                model="m", provider="openai", billing=None, repo_root=None, input_tokens=0, output_tokens=0,
                cost=0.0, cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0, api_calls=1)
    base.update(kw)
    return RawFact(**base)


def facts_of(*raws, models=None, absent=frozenset()):
    read = HomeRead("p", False, "ok", (), models or {}, HomeFacts(tuple(raws), frozenset(absent)))
    return depth.facts_from_reads([read])


def build(*raws, dimension="all", scan=None, incomplete=False, rates=None, group_of=None):
    facts, scanned = facts_of(*raws)
    return depth.build_depth(facts, dimension=dimension, rate_table=rates or depth.load_rate_table(),
                             scan=scan or scanned, source_incomplete=incomplete, group_of=group_of)


def independent_quantiles(samples):
    """p50/p90/p99 by Excel-inclusive percentile (== h=(n-1)p linear interpolation) from the stdlib."""
    q = statistics.quantiles(samples, n=100, method="inclusive")
    return q[49], q[89], q[98]


# ------------------------------------------------------------------------------------------------ duration


class DurationTests(unittest.TestCase):
    def test_normative_vector(self):
        stats, n = depth.duration_stats([0, 10, 20, 30])
        self.assertEqual(n, 4)
        self.assertEqual((stats.p50, stats.p90, stats.p99, stats.mean, stats.max), (15.0, 27.0, 29.7, 15.0, 30.0))

    def test_sizes_zero_one_even_odd(self):
        none, n0 = depth.duration_stats([])
        self.assertEqual((n0, (none.p50, none.p90, none.p99, none.mean, none.max)), (0, (None,) * 5))
        one, n1 = depth.duration_stats([7.0])
        self.assertEqual((n1, (one.p50, one.p90, one.p99, one.mean, one.max)), (1, (7.0,) * 5))
        even, _ = depth.duration_stats([1, 2, 3, 4, 5, 6])
        self.assertEqual(even.p50, 3.5)
        odd, _ = depth.duration_stats([1, 2, 3])
        self.assertEqual((odd.p50, odd.max), (2.0, 3.0))
        self.assertAlmostEqual(odd.p90, 2.8, places=12)
        zeros, _ = depth.duration_stats([0, 0, 0])
        self.assertEqual((zeros.p50, zeros.p99, zeros.mean, zeros.max), (0.0, 0.0, 0.0, 0.0))
        frac, _ = depth.duration_stats([0.5, 1.5])
        self.assertEqual((frac.p50, frac.mean), (1.0, 1.0))

    def test_matches_independent_percentiles(self):
        samples = sorted([0.3, 1, 1, 2.5, 7, 7.25, 9, 60, 61, 3600, 7200, 90.5])
        stats, _ = depth.duration_stats(samples)
        want = independent_quantiles(samples)
        for got, exp in zip((stats.p50, stats.p90, stats.p99), want):
            self.assertAlmostEqual(got, exp, places=9)
        self.assertAlmostEqual(stats.mean, sum(samples) / len(samples), places=9)

    def test_sample_validity_rules(self):
        t = utc(2026, 11, 5, 12)
        facts, scan = facts_of(
            rf(started_at=t, ended_at=t + 100),                     # valid
            rf(started_at=t, ended_at=t),                           # zero length counts
            rf(started_at=t, ended_at=None, last_activity_at=t + 9999),  # open: never last_activity / now
            rf(started_at=t, ended_at=t - 5),                       # negative interval
            rf(started_at=t, ended_at=float("nan")),
            rf(started_at=t, ended_at=float("inf")),
            rf(started_at=t, ended_at=True),                        # bool is not a number
            rf(started_at=t, ended_at="soon"),
            rf(started_at=t, ended_at=t + 0.25))                    # fractional
        self.assertEqual(scan.excluded_started_at, 0)
        self.assertEqual(depth.duration_samples(facts), [0.0, 0.25, 100.0])
        stats, n = depth.duration_stats(depth.duration_samples(facts))
        self.assertEqual((n, stats.max), (3, 100.0))
        self.assertEqual(depth.missing_counts(facts)["duration"], 6)  # nulls leave the denominator, are disclosed

    def test_build_depth_reports_samples_and_null_object(self):
        t = utc(2026, 11, 5, 12)
        res = build(rf(started_at=t, ended_at=t + 10), rf(started_at=t + 1, ended_at=None)).data
        self.assertEqual(res["duration_samples"], 1)
        self.assertEqual(res["durations"]["p50"], 10.0)
        empty = build(rf(started_at=t, ended_at=None)).data
        self.assertEqual((empty["duration_samples"], empty["durations"]), (0, dict.fromkeys(("p50", "p90", "p99", "mean", "max"))))


# ------------------------------------------------------------------------------------------------ heatmap

# (UTC start, expected UTC (weekday, hour) with Monday=0, browser zone, expected local (weekday, hour), label)
DST_TABLE = [
    (utc(2026, 3, 8, 6, 59), (6, 6), "America/New_York", (6, 1), "before spring gap Sun 01:59"),
    (utc(2026, 3, 8, 7, 0), (6, 7), "America/New_York", (6, 3), "after spring gap Sun 03:00 (no 02:00 cohort)"),
    (utc(2026, 11, 1, 5, 30), (6, 5), "America/New_York", (6, 1), "fold, first 01:30"),
    (utc(2026, 11, 1, 6, 30), (6, 6), "America/New_York", (6, 1), "fold, second 01:30 (same cell)"),
    (utc(2026, 11, 21, 23, 45), (5, 23), "Asia/Kolkata", (6, 5), "fractional offset crosses the day"),
    (utc(2026, 11, 21, 23, 59), (5, 23), "UTC", (5, 23), "ends after midnight, starts Sat 23"),
    (0, (3, 0), "UTC", (3, 0), "epoch zero (Thursday)"),
    (-3600, (2, 23), "UTC", (2, 23), "negative valid epoch (Wed 23:00)"),
    (utc(2026, 11, 22) - 0.001, (5, 23), "UTC", (5, 23), "fractional second before midnight"),
    (utc(2026, 11, 22), (6, 0), "UTC", (6, 0), "midnight itself is the next day"),
    (utc(2026, 11, 22, 23, 59, 59) + 0.5, (6, 23), "UTC", (6, 23), "Sunday 23:59:59.5"),
    (utc(2026, 11, 23), (0, 0), "UTC", (0, 0), "weekday wraps Sunday -> Monday"),
    (utc(2026, 11, 21, 18, 20), (5, 18), "Asia/Kathmandu", (6, 0), "+05:45 quarter-hour zone"),
    (utc(2026, 11, 21, 13, 40), (5, 13), "Australia/Adelaide", (6, 0), "+10:30 half-hour zone (DST)"),
    (utc(2026, 1, 15, 15), (3, 15), "America/New_York", (3, 10), "winter: same UTC cell as the July row"),
    (utc(2026, 7, 16, 15), (3, 15), "America/New_York", (3, 11), "summer: other offset, same UTC weekday/hour"),
]


def local_cell(epoch, zone):
    moment = datetime.fromtimestamp(epoch, ZoneInfo(zone))
    return moment.weekday(), moment.hour


def regroup_local(starts, zone):
    """The browser algorithm (design §4): re-bucket the exact epochs by the zone's rules for THAT instant."""
    grid = {}
    for s in starts:
        key = local_cell(s["started_at"], zone)
        cell = grid.setdefault(key, {"sessions": 0, "total_tokens": None})
        cell["sessions"] += s["sessions"]
        if s["total_tokens"] is not None:
            cell["total_tokens"] = (cell["total_tokens"] or 0) + s["total_tokens"]
    return grid


class HeatmapTests(unittest.TestCase):
    def heat(self, *raws):
        return build(*raws).data["heatmap"]

    def test_dst_and_utc_table(self):
        for epoch, utc_cell, zone, local, label in DST_TABLE:
            with self.subTest(label):
                # the literals agree with the platform tz database (no hand-coded DST month rule)
                moment = datetime.fromtimestamp(epoch, timezone.utc)
                self.assertEqual((moment.weekday(), moment.hour), utc_cell)
                self.assertEqual(local_cell(epoch, zone), local)
                heat = self.heat(rf(started_at=epoch, input_tokens=3, output_tokens=4))
                self.assertEqual((len(heat["grid"]), {len(r) for r in heat["grid"]}), (7, {24}))
                cells = [(d, h) for d in range(7) for h in range(24) if heat["grid"][d][h]["sessions"]]
                self.assertEqual(cells, [utc_cell])  # exactly one bucket
                self.assertEqual(heat["grid"][utc_cell[0]][utc_cell[1]], {"sessions": 1, "total_tokens": 7})
                self.assertEqual(heat["starts"], [{"started_at": epoch, "sessions": 1, "total_tokens": 7}])
                self.assertEqual(regroup_local(heat["starts"], zone), {local: {"sessions": 1, "total_tokens": 7}})

    def test_midnight_spanning_session_is_in_exactly_one_bucket(self):
        start, end = utc(2026, 11, 21, 23, 59), utc(2026, 11, 22, 0, 10)
        heat = self.heat(rf(started_at=start, ended_at=end, input_tokens=100, output_tokens=50))
        nonzero = {(d, h): c for d, row in enumerate(heat["grid"]) for h, c in enumerate(row) if c["sessions"]}
        self.assertEqual(nonzero, {(5, 23): {"sessions": 1, "total_tokens": 150}})  # whole tokens at the start only
        self.assertEqual(sum(c["sessions"] for row in heat["grid"] for c in row), 1)

    def test_fold_both_cohorts_counted_in_one_local_cell_and_utc_untouched(self):
        a, b = utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 6, 30)
        heat = self.heat(rf(started_at=a, input_tokens=1, output_tokens=0), rf(started_at=b, input_tokens=2, output_tokens=0))
        before = copy.deepcopy(heat)
        local = regroup_local(heat["starts"], "America/New_York")
        self.assertEqual(local, {(6, 1): {"sessions": 2, "total_tokens": 3}})
        self.assertEqual(heat, before)  # re-bucketing never mutates the UTC data
        self.assertEqual(heat["grid"][6][5]["sessions"] + heat["grid"][6][6]["sessions"], 2)
        # a rotated UTC grid could not have told the two apart: the exact epochs are what makes this correct
        self.assertEqual([s["started_at"] for s in heat["starts"]], [a, b])

    def test_local_regrouping_conserves_for_every_zone(self):
        raws = [rf(started_at=e, input_tokens=i + 1, output_tokens=1) for i, (e, *_rest) in enumerate(DST_TABLE)]
        heat = self.heat(*raws)
        total_sessions = sum(c["sessions"] for row in heat["grid"] for c in row)
        total_tokens = sum(c["total_tokens"] or 0 for row in heat["grid"] for c in row)
        for zone in ("UTC", "America/New_York", "Asia/Kolkata", "Asia/Kathmandu", "Australia/Adelaide", "Pacific/Auckland"):
            with self.subTest(zone):
                local = regroup_local(heat["starts"], zone)
                self.assertEqual(sum(c["sessions"] for c in local.values()), total_sessions)
                self.assertEqual(sum(c["total_tokens"] or 0 for c in local.values()), total_tokens)
        self.assertEqual(sum(s["sessions"] for s in heat["starts"]), total_sessions)

    def test_same_epoch_combines_and_null_tokens_are_not_zero(self):
        t = utc(2026, 11, 5, 12)
        heat = self.heat(rf(started_at=t, input_tokens=None, output_tokens=None),
                         rf(started_at=t, input_tokens=None, output_tokens=None),
                         rf(started_at=t + 3600, input_tokens=0, output_tokens=0))
        self.assertEqual(heat["starts"][0], {"started_at": t, "sessions": 2, "total_tokens": None})
        self.assertEqual(heat["starts"][1]["total_tokens"], 0)  # observed zero stays 0
        cell = heat["grid"][t_to_cell(t)[0]][t_to_cell(t)[1]]
        self.assertEqual(cell, {"sessions": 2, "total_tokens": None})
        empty = [c for row in heat["grid"] for c in row if c["sessions"] == 0]
        self.assertTrue(empty and all(c["total_tokens"] is None for c in empty))  # empty cell: no observation

    def test_invalid_epochs_are_excluded_and_disclosed_never_now(self):
        facts, scan = facts_of(rf(started_at=float("nan")), rf(started_at=float("inf")), rf(started_at=True),
                               rf(started_at="yesterday"), rf(started_at=None), rf(started_at=1e30),
                               rf(started_at=utc(2026, 11, 5, 12)))
        self.assertEqual((len(facts), scan.excluded_started_at), (1, 6))
        res = depth.build_depth(facts, dimension="all", rate_table=depth.load_rate_table(), scan=scan).data
        self.assertEqual(res["depth_status"]["excluded_started_at"], 6)
        self.assertIs(res["depth_status"]["partial"], True)
        self.assertEqual(res["depth_totals"]["sessions"], 1)


def t_to_cell(epoch):
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.weekday(), moment.hour


# ------------------------------------------------------------------------------------------------ cache


def core_source():
    try:
        return subprocess.run(["git", "-C", str(CORE_CHECKOUT), "show", f"{CORE_PIN}:agent/usage_pricing.py"],
                              check=True, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def build_script():
    spec = importlib.util.spec_from_file_location("build_cache_discounts_t", REPO_ROOT / "scripts" / "build_cache_discounts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKED = dict(model="gpt-5.6-sol", provider="openai", input_tokens=1_000_000, output_tokens=100_000,
              reasoning_tokens=20_000, cache_write_tokens=100_000, cache_read_tokens=2_000_000, cost=9.625)


class CacheTests(unittest.TestCase):
    def row(self, *raws, incomplete=False, rates=None):
        facts, scan = facts_of(*raws)
        rows = depth.build_cache_metrics(facts, rates or depth.load_rate_table(), incomplete)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_packaged_projection_is_the_pinned_core_snapshot(self):
        table = depth.load_rate_table()
        self.assertEqual(table.core_commit, CORE_PIN)
        self.assertEqual(table.digest, PACKAGED_DIGEST, "re-pin deliberately: scripts/build_cache_discounts.py")
        source = core_source()
        if source is None:
            self.skipTest(f"Hermes core {CORE_PIN[:10]} not available at {CORE_CHECKOUT}")
        module = build_script()
        projected = module.render(module.project(module.load_core(source), CORE_PIN))
        self.assertEqual(projected, (depth.PACKAGE_DATA / depth.RATE_FILE).read_bytes())

    def test_discount_is_per_exact_row_not_a_constant(self):
        table = depth.load_rate_table()
        for provider, model, want, version in (
                ("openai", "gpt-5.6-sol", Decimal("0.1"), "openai-gpt-5.6-2026-07"),
                ("openai", "gpt-4o", Decimal("0.5"), "openai-pricing-2026-03-16"),
                ("deepseek", "deepseek-flash", Decimal("0.02"), "deepseek-pricing-2026-09-10"),
                ("anthropic", "claude-opus-5-5", Decimal("0.05"), "anthropic-pricing-2026-09"),
                ("anthropic", "claude-fable-5-2", Decimal("0.025"), "anthropic-pricing-2026-11")):
            with self.subTest(model):
                row = table.lookup(provider, model)
                self.assertEqual(row.discount, want)
                self.assertEqual(row.pricing_version, version)
        self.assertIsNone(table.lookup("bedrock", "amazon.nova-pro").discount)  # no published cache-read rate
        self.assertIsNone(table.lookup("openai", "claude-sonnet-5-5"))           # no cross-provider tariff
        self.assertIsNone(table.lookup("openai", "GPT-4O"))                       # exact: no case folding
        self.assertIsNone(table.lookup("openai", "openai/gpt-4o"))                # no prefix stripping
        self.assertIsNone(table.lookup(None, "gpt-4o"))

    def test_worked_example_normative_vector(self):
        row = self.row(rf(**WORKED))
        d_tokens = Decimal(1_000_000 + 100_000 + 20_000 + 100_000) + Decimal(2_000_000) * Decimal("0.10")
        self.assertEqual(d_tokens, Decimal(1_420_000))
        want = Decimal(1_000_000) * Decimal("9.625") / d_tokens
        self.assertLess(abs(row.effective_cost_per_mtok - float(want)), 1e-12)
        self.assertLess(abs(row.effective_cost_per_mtok - 6.778169014084507), 1e-12)  # $/Mtok, NOT 9.625/1.42e6
        self.assertLess(abs(row.hit_rate_pct - 64.51612903225806), 1e-12)             # 100*2e6/3.1e6; writes are misses
        self.assertEqual(row.reasoning_share_pct, 20.0)
        self.assertEqual((row.discount_multiplier, row.rate_source, row.reason), (0.1, "openai-gpt-5.6-2026-07", None))
        self.assertEqual((row.cached_input_tokens, row.uncached_input_tokens, row.cache_write_tokens,
                          row.reasoning_tokens, row.output_tokens), (2_000_000, 1_000_000, 100_000, 20_000, 100_000))
        self.assertGreater(abs(row.effective_cost_per_mtok - 6.78), 1e-4)  # a rounded-cents answer would be wrong

    def test_worked_example_cross_checked_against_core_cost_loop(self):
        source = core_source()
        if source is None:
            self.skipTest(f"Hermes core {CORE_PIN[:10]} not available at {CORE_CHECKOUT}")
        core = build_script().load_core(source)
        entry = core._OFFICIAL_DOCS_PRICING[("openai", "gpt-5.6-sol")]
        # core's estimate_usage_cost loop, restated: I,O,C,W each priced at its own per-million rate; R is not billed
        amount = Decimal(0)
        for tokens, rate in ((1_000_000, entry.input_cost_per_million), (100_000, entry.output_cost_per_million),
                             (2_000_000, entry.cache_read_cost_per_million),
                             (100_000, entry.cache_write_cost_per_million)):
            amount += Decimal(tokens) * rate / Decimal(1_000_000)
        self.assertEqual(amount, Decimal("9.625"))
        self.assertEqual(entry.cache_read_cost_per_million / entry.input_cost_per_million, Decimal("0.1"))
        row = self.row(rf(**{**WORKED, "cost": float(amount)}))
        self.assertLess(abs(row.effective_cost_per_mtok - float(Decimal(1_000_000) * amount / Decimal(1_420_000))), 1e-12)
        # every packaged exact row equals core's own snapshot row (rates, version) for the same key
        table = depth.load_rate_table()
        self.assertEqual(len(table.rows), len(core._OFFICIAL_DOCS_PRICING))
        for (provider, model), core_entry in core._OFFICIAL_DOCS_PRICING.items():
            got = table.lookup(provider, model)
            self.assertEqual((got.pricing_version, got.input_cost_per_million, got.cache_read_cost_per_million),
                             (core_entry.pricing_version, core_entry.input_cost_per_million,
                              core_entry.cache_read_cost_per_million), (provider, model))

    def test_reasoning_is_not_priced_and_no_second_input_subtraction(self):
        base = self.row(rf(**WORKED))
        no_reasoning = self.row(rf(**{**WORKED, "reasoning_tokens": 0}))
        # R sits in the denominator (D4) but never adds a charge: same K, bigger D -> smaller effective rate
        self.assertGreater(no_reasoning.effective_cost_per_mtok, base.effective_cost_per_mtok)
        d_without = 1_000_000 + 100_000 + 0 + 100_000 + 200_000
        self.assertLess(abs(no_reasoning.effective_cost_per_mtok - 1e6 * 9.625 / d_without), 1e-9)
        # recorded input is already the uncached figure: it must not be reduced by the cache reads again
        self.assertEqual(base.uncached_input_tokens, 1_000_000)
        self.assertLess(abs(base.hit_rate_pct - 100 * 2_000_000 / 3_100_000), 1e-12)

    def test_weighted_by_tokens_not_a_mean_of_ratios(self):
        small = dict(model="gpt-4o", provider="openai", input_tokens=100, output_tokens=0, reasoning_tokens=0,
                     cache_write_tokens=0, cache_read_tokens=0, cost=0.0005)
        large = dict(model="gpt-4o", provider="openai", input_tokens=900, output_tokens=0, reasoning_tokens=0,
                     cache_write_tokens=0, cache_read_tokens=9000, cost=0.01)
        row = self.row(rf(**small), rf(**large))
        d = 100 + 900 + 9000 * 0.5
        self.assertLess(abs(row.effective_cost_per_mtok - 1e6 * 0.0105 / d), 1e-9)
        per_session = [1e6 * 0.0005 / 100, 1e6 * 0.01 / (900 + 4500)]
        self.assertGreater(abs(row.effective_cost_per_mtok - sum(per_session) / 2), 1.0)  # not the average
        self.assertLess(abs(row.hit_rate_pct - 100 * 9000 / 10000), 1e-12)               # raw sums, not mean(0, 90%)

    def test_unknown_discount_rules(self):
        unknown_route = dict(WORKED, provider="mystery-cloud")
        row = self.row(rf(**unknown_route))
        self.assertEqual((row.discount_multiplier, row.rate_source, row.effective_cost_per_mtok, row.reason),
                         (None, None, None, "unknown_discount"))
        self.assertIsNotNone(row.hit_rate_pct)  # the hit signal needs no tariff
        zero_read = dict(unknown_route, cache_read_tokens=0)
        row = self.row(rf(**zero_read))  # all C == 0: provably no cache contribution -> computed, multiplier null
        self.assertIsNone(row.discount_multiplier)
        self.assertIsNone(row.reason)
        want = 1e6 * 9.625 / (1_000_000 + 100_000 + 20_000 + 100_000)
        self.assertLess(abs(row.effective_cost_per_mtok - want), 1e-9)
        nova = self.row(rf(**dict(WORKED, provider="bedrock", model="amazon.nova-pro")))  # row exists, no cache rate
        self.assertEqual((nova.discount_multiplier, nova.rate_source is not None, nova.reason),
                         (None, True, "unknown_discount"))

    def test_missing_usage_cost_and_denominator_reasons(self):
        row = self.row(rf(**dict(WORKED, input_tokens=None)))
        self.assertEqual((row.reason, row.effective_cost_per_mtok, row.hit_rate_pct), ("missing_usage", None, None))
        self.assertEqual(row.cached_input_tokens, 2_000_000)  # known raw subtotals stay visible
        self.assertIsNone(row.uncached_input_tokens)
        self.assertIsNotNone(row.discount_multiplier)         # the tariff itself is independent of usage
        row = self.row(rf(**dict(WORKED, cost=None)))
        self.assertEqual((row.reason, row.effective_cost_per_mtok), ("missing_cost", None))
        self.assertIsNotNone(row.hit_rate_pct)
        zero = dict(WORKED, input_tokens=0, output_tokens=0, reasoning_tokens=0, cache_write_tokens=0,
                    cache_read_tokens=0, cost=0.0)
        row = self.row(rf(**zero))
        self.assertEqual((row.reason, row.hit_rate_pct, row.effective_cost_per_mtok), ("zero_denominator", None, None))
        # fully recorded zero cost on a nonzero denominator is a real 0 (recorded-cost basis), not null
        free = self.row(rf(**dict(WORKED, cost=0.0)))
        self.assertEqual((free.effective_cost_per_mtok, free.reason), (0.0, None))

    def test_missing_one_session_nulls_the_whole_pair_ratio(self):
        good = rf(**WORKED)
        hole = rf(**dict(WORKED, cost=None))
        row = self.row(good, hole)  # never divide known costs by a larger incomplete population
        self.assertEqual((row.effective_cost_per_mtok, row.reason), (None, "missing_cost"))

    def test_incomplete_source_nulls_ratios_but_keeps_raw_sums(self):
        row = self.row(rf(**WORKED), incomplete=True)
        self.assertEqual((row.reason, row.hit_rate_pct, row.effective_cost_per_mtok, row.reasoning_share_pct),
                         ("incomplete_source", None, None, None))
        self.assertEqual((row.cached_input_tokens, row.uncached_input_tokens), (2_000_000, 1_000_000))
        earlier = self.row(rf(**dict(WORKED, cost=None)), incomplete=True)
        self.assertEqual(earlier.reason, "missing_cost")  # an earlier field-level reason wins

    def test_reasoning_overlap_violation_nulls_the_share_only(self):
        row = self.row(rf(**dict(WORKED, reasoning_tokens=150_000)))  # R > O
        self.assertIsNone(row.reasoning_share_pct)
        self.assertEqual(row.reasoning_tokens, 150_000)  # raw counter retained, never clamped to 100
        self.assertIsNotNone(row.effective_cost_per_mtok)

    def test_pairs_are_exact_ids_sorted_null_first_no_other(self):
        raws = [rf(model=m, provider=p) for m, p in (("zz", "openai"), (None, "openai"), ("aa", "openai"),
                                                    ("aa", "anthropic"), (None, "unknown"))]
        rows = depth.build_cache_metrics(facts_of(*raws)[0], depth.load_rate_table(), False)
        self.assertEqual([(r.model, r.provider_id) for r in rows],
                         [(None, "openai"), (None, "unknown"), ("aa", "anthropic"), ("aa", "openai"), ("zz", "openai")])

    def test_no_network_and_no_core_import(self):
        before = set(os.sys.modules)
        with mock.patch("socket.socket", side_effect=AssertionError("network used")):
            depth.build_cache_metrics(facts_of(rf(**WORKED))[0], depth.load_rate_table(), False)
        self.assertEqual({m for m in set(os.sys.modules) - before if m.split(".")[0] in ("agent", "hermes_cli")}, set())

    def test_broken_rate_file_is_refused_not_guessed(self):
        good = (depth.PACKAGE_DATA / depth.RATE_FILE).read_bytes()
        doc = json.loads(good)
        for mutate in (lambda d: d.pop("core_commit"), lambda d: d.update(extra=1),
                       lambda d: d["entries"].append(dict(d["entries"][0])),            # ambiguous duplicate key
                       lambda d: d["entries"][0].update(input_cost_per_million=5),     # not a decimal string
                       lambda d: d["entries"][0].update(input_cost_per_million="-1"),
                       lambda d: d["entries"][0].update(pricing_version="bad version")):
            bad = copy.deepcopy(doc)
            mutate(bad)
            with self.assertRaises(depth.RateTableError):
                depth.parse_rate_table(json.dumps(bad).encode())


# ------------------------------------------------------------------------------------------------ repo


class RepoKeyTests(unittest.TestCase):
    def test_basename_table(self):
        table = [
            ("/work/usage-deck", "usage-deck"), ("/work/usage-deck/", "usage-deck"), ("/work/usage-deck///", "usage-deck"),
            ("  /work/usage-deck  ", "usage-deck"), ("C:\\Users\\dev\\usage-deck", "usage-deck"),
            ("C:\\Users\\dev\\usage-deck\\", "usage-deck"), ("relative/dir/Name", "Name"), ("Name", "Name"),
            ("/work/Usage-Deck", "Usage-Deck"), ("/srv/Unscoped", "Unscoped"), ("/a/b c/d e", "d e"),
            ("/mixed\\sep/last", "last"), ("C:", "C:"),
            (None, None), ("", None), ("   ", None), ("/", None), ("\\", None), ("//", None), (" / ", None),
            (123, None), (b"/x", None), ("/a/ /", None),
        ]
        for root, want in table:
            with self.subTest(root=root):
                self.assertEqual(depth.repo_key(root), want)

    def test_group_ids_fit_the_grammar_and_never_leak_secrets(self):
        pattern = dto._GROUP_ID_RE
        self.assertEqual(depth.repo_group_id(None), "repo:null")
        self.assertEqual(depth.repo_group_id("usage-deck"), 'repo:"usage-deck"')
        self.assertEqual(depth.repo_group_id("Unscoped"), 'repo:"Unscoped"')  # literal repo != the missing bucket
        self.assertNotEqual(depth.repo_group_id("Unscoped"), depth.repo_group_id(None))
        for name in ("my project", 'qu"ote', "back\\slash", "プロジェクト", "a" * 128, "x.y_z-1"):
            gid = depth.repo_group_id(name)
            self.assertRegex(gid, pattern)
            self.assertEqual(json.loads(gid[len("repo:"):]), name)
        secret = "sk-abcdefgh12345678"
        for name in (secret, "Bearer abc.def", "a" * 129, "tab\there", "del\x7fx", "http://u:p@h"):
            gid = depth.repo_group_id(name)
            self.assertTrue(gid.startswith("repo:sha256:"), name)
            self.assertRegex(gid, pattern)
            self.assertEqual(gid, "repo:sha256:" + hashlib.sha256(name.encode()).hexdigest())
            self.assertNotIn(name, gid)

    def test_display_path_is_bounded_clean_and_scrubbed(self):
        self.assertEqual(depth.display_path("/work/x/"), "/work/x")
        self.assertIsNone(depth.display_path("/work/\x00x"))
        self.assertIsNone(depth.display_path("/work/\x1b[31mx"))
        self.assertIsNone(depth.display_path("/" + "a" * 4096))
        self.assertIsNone(depth.display_path(None))
        self.assertEqual(depth.display_path("/w/<script>alert(1)</script>"), "/w/<script>alert(1)</script>")  # text only
        self.assertNotIn("sk-abcdefgh12345678", depth.display_path("/w/sk-abcdefgh12345678"))
        self.assertNotIn("\x07", depth.display_label("a\x07b"))

    def test_collisions_combine_and_expose_every_path(self):
        res = build(rf(repo_root="/a/usage-deck", input_tokens=1, output_tokens=1),
                    rf(repo_root="/b/usage-deck/", input_tokens=2, output_tokens=2),
                    rf(repo_root="/b/usage-deck", input_tokens=4, output_tokens=4),
                    rf(repo_root="/srv/Unscoped", input_tokens=8, output_tokens=0),
                    rf(repo_root=None, input_tokens=16, output_tokens=0)).data
        rows = {(r["repo_basename"], tuple(r["full_paths"])): r for r in res["repo_rollup"]}
        merged = rows[("usage-deck", ("/a/usage-deck", "/b/usage-deck"))]
        self.assertEqual((merged["sessions"], merged["total_tokens"], merged["full_path"]), (3, 14, None))
        single = rows[("Unscoped", ("/srv/Unscoped",))]
        self.assertEqual((single["full_path"], single["sessions"]), ("/srv/Unscoped", 1))
        missing = rows[("Unscoped", ())]
        self.assertEqual((missing["full_path"], missing["sessions"], missing["total_tokens"]), (None, 1, 16))
        self.assertEqual(res["repo_rollup"][-1], missing)  # the missing bucket is always last
        self.assertEqual(len(res["repo_rollup"]), 3)

    def test_unscoped_row_is_always_present_even_empty(self):
        res = build().data
        self.assertEqual(res["repo_rollup"], [{"repo_basename": "Unscoped", "full_path": None, "sessions": 0,
                                               "total_tokens": None, "total_cost": None, "full_paths": []}])
        self.assertEqual(res["depth_totals"], {"sessions": 0, "total_tokens": None, "total_cost": None})
        self.assertEqual(res["model_rollup"], [])
        self.assertEqual(res["cache_metrics"], [])
        self.assertIs(res["depth_status"]["partial"], False)

    def test_ordering_known_tokens_desc_unknown_last_then_name_missing_last(self):
        res = build(rf(repo_root="/x/zeta", input_tokens=5, output_tokens=0), rf(repo_root="/x/alpha", input_tokens=5, output_tokens=0),
                    rf(repo_root="/x/big", input_tokens=50, output_tokens=0),
                    rf(repo_root="/x/nodata", input_tokens=None, output_tokens=None),
                    rf(repo_root=None, input_tokens=999, output_tokens=0)).data
        self.assertEqual([r["repo_basename"] for r in res["repo_rollup"]], ["big", "alpha", "zeta", "nodata", "Unscoped"])


class NullDoctrineTests(unittest.TestCase):
    def test_missing_only_money_stays_null_recorded_zero_stays_zero(self):
        res = build(rf(cost=None), rf(cost=None)).data
        self.assertIsNone(res["depth_totals"]["total_cost"])
        self.assertIsNone(res["model_rollup"][0]["total_cost"])
        res = build(rf(cost=None), rf(cost=0.0)).data
        self.assertEqual(res["depth_totals"]["total_cost"], 0.0)  # recorded zero is real; the null is excluded
        self.assertEqual(res["depth_status"]["missing"]["estimated_cost_usd"], 1)
        tokens = build(rf(input_tokens=None, output_tokens=None), rf(input_tokens=None, output_tokens=7)).data
        self.assertEqual(tokens["depth_totals"]["total_tokens"], 7)

    def test_invalid_observations_become_missing_in_the_new_projection_only(self):
        facts, _ = facts_of(rf(input_tokens=-1, output_tokens=2.5, cost=-0.5), rf(input_tokens=True, cost=float("nan")))
        for f in facts:
            self.assertEqual((f.input_tokens, f.cost), (None, None))
        self.assertEqual(facts[0].output_tokens, None)  # non-integral float
        self.assertEqual([f.total_tokens for f in facts], [None, 0])  # bool/negative never count; recorded 0 does

    def test_exact_money_sums_use_decimal(self):
        res = build(*[rf(cost=0.1) for _ in range(10)]).data
        self.assertEqual(res["depth_totals"]["total_cost"], 1.0)  # 10 x 0.1 float sum would be 0.9999999999999999

    def test_conservation_assertion_catches_a_dropped_session(self):
        facts, scan = facts_of(rf(repo_root="/a/x"), rf(repo_root="/a/y"))
        totals = dto.DepthTotals(2, 0, 0.0)
        with self.assertRaises(depth.DepthInvariantError):
            depth.assert_conserved(totals, [dto.RepoRow("x", "/a/x", 1, 0, 0.0, ("/a/x",))],
                                   list(depth.build_model_rollup(facts)), facts)
        with self.assertRaises(depth.DepthInvariantError):
            depth.assert_conserved(dto.DepthTotals(3, 0, 0.0), list(depth.build_repo_rollup(facts)),
                                   list(depth.build_model_rollup(facts)), facts)


# ------------------------------------------------------------------------------------------------ service

NO_NET = mock.patch("socket.socket", side_effect=AssertionError("network used"))


def spec_window(specs, window):
    return [s for s in specs if isinstance(s["started_at"], float) and s["started_at"] > NOW_TS - window * fx.DAY]


def known(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def exp_totals(specs):
    toks = [None if s["input_tokens"] is None and s["output_tokens"] is None
            else (s["input_tokens"] or 0) + (s["output_tokens"] or 0) for s in specs]
    return {"sessions": len(specs), "total_tokens": known(toks),
            "total_cost": None if known(s["estimated_cost_usd"] for s in specs) is None
            else known(s["estimated_cost_usd"] for s in specs)}


def want_matches(spec, provider):
    if provider is None:
        return True
    if provider == "custom":
        return spec["want"] == "custom" or spec["want"].startswith("custom:")
    return spec["want"] == provider


class DepthServiceBase(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)
        self.m = fx.seed(Machine(self))
        self.homes = {None: ROOT, "a": A, "b": B}

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def get(self, params, svc=None, home=None, **kw):
        svc = svc or self.m.service(home=home)
        with NO_NET:
            status, body = svc.handle("usage", list(params), **kw)
        self.assertEqual(status, 200, body)
        check_envelope(body)  # the strict decoder: exact keys, enums, conservation
        return body["data"]

    def all_specs(self):
        return [s for specs in self.homes.values() for s in specs]


class ConservationTests(DepthServiceBase):
    def test_every_dimension_provider_window_conserves_over_one_session_set(self):
        providers = (None, "anthropic", "openai", "openrouter", "nous", "custom", "unknown")
        for window in (7, 30, 90):
            for provider in providers:
                want_specs = [s for s in spec_window(self.all_specs(), window) if want_matches(s, provider)]
                want = exp_totals(want_specs)
                reference = None
                for dimension in ("all", "profile", "model", "repo"):
                    with self.subTest(window=window, provider=provider, dimension=dimension):
                        params = [("mode", "machine"), ("dimension", dimension), ("window", str(window))]
                        if provider:
                            params.append(("provider", provider))
                        try:
                            data = self.get(params)
                        except AssertionError:
                            self.assertEqual(want["sessions"], 0)  # provider known but empty is still a 200
                            raise
                        # decoder-level conservation already ran; restate it independently of the code
                        self.assertEqual(data["depth_totals"], {**want, "total_cost": data["depth_totals"]["total_cost"]})
                        self.assertAlmostEqual(data["depth_totals"]["total_cost"] or 0, want["total_cost"] or 0, places=9)
                        self.assertEqual(sum(r["sessions"] for r in data["repo_rollup"]), want["sessions"])
                        self.assertEqual(sum(r["sessions"] for r in data["model_rollup"]), want["sessions"])
                        self.assertEqual(known(r["total_tokens"] for r in data["repo_rollup"]), want["total_tokens"])
                        self.assertEqual(known(r["total_tokens"] for r in data["model_rollup"]), want["total_tokens"])
                        self.assertAlmostEqual(known(r["total_cost"] for r in data["repo_rollup"]) or 0,
                                               want["total_cost"] or 0, places=9)
                        self.assertAlmostEqual(known(r["total_cost"] for r in data["model_rollup"]) or 0,
                                               want["total_cost"] or 0, places=9)
                        snapshot = {k: data[k] for k in ("durations", "duration_samples", "heatmap", "cache_metrics",
                                                         "repo_rollup", "model_rollup", "depth_totals", "depth_status")}
                        if reference is None:
                            reference = snapshot
                        self.assertEqual(snapshot, reference)  # every dimension sees the same depth members

    def test_model_group_totals_equal_model_rollup_and_other_is_included(self):
        data = self.get([("mode", "machine"), ("dimension", "model")])
        self.assertTrue(any(g["is_other"] for g in data["groups"]))  # >8 models really exercise Other
        self.assertGreater(len(data["model_rollup"]), 8)
        legacy_sessions = sum(g["totals"]["sessions"] for g in data["groups"])
        self.assertEqual(legacy_sessions, data["depth_totals"]["sessions"])  # legacy counters agree on valid data
        self.assertEqual(data["depth_status"]["excluded_started_at"], 1)     # the one unbucketable start

    def test_exact_recorded_model_ids_and_multiple_providers_per_model(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        pairs = {(r["model"], r["provider_id"]) for r in data["model_rollup"]}
        self.assertIn(("claude-sonnet-5.5", "anthropic"), pairs)
        self.assertIn(("Claude-Sonnet-5.5", "anthropic"), pairs)       # case is part of the exact id
        self.assertIn(("gpt-6-luna", "openai"), pairs)
        self.assertIn(("gpt-6-luna", "openrouter"), pairs)             # one model, two providers: two rows
        self.assertIn(("claude-sonnet-5.5", "nous"), pairs)            # attributed through billing_provider
        self.assertIn((None, "unknown"), pairs)                        # honest unknown group
        cache_pairs = {(r["model"], r["provider_id"]) for r in data["cache_metrics"]}
        self.assertEqual(cache_pairs, pairs)                           # every pair, no top-N
        self.assertEqual([(r["model"], r["provider_id"]) for r in data["cache_metrics"]],
                         [(r["model"], r["provider_id"]) for r in data["model_rollup"]])

    def test_live_union_of_authorized_homes_equals_the_machine_view(self):
        machine = self.get([("mode", "machine"), ("dimension", "all")])
        per_home = {name: self.get([("mode", "profile"), ("dimension", "all")], home=name) for name in self.homes}
        # same observation set: nothing mutates the fixture between the reads
        self.assertEqual(machine["depth_totals"]["sessions"], sum(d["depth_totals"]["sessions"] for d in per_home.values()))
        self.assertEqual(machine["depth_totals"]["total_tokens"], known(d["depth_totals"]["total_tokens"] for d in per_home.values()))
        merged = {}
        for d in per_home.values():
            for r in d["model_rollup"]:
                acc = merged.setdefault((r["model"], r["provider_id"]), [0, None])
                acc[0] += r["sessions"]
                if r["total_tokens"] is not None:
                    acc[1] = (acc[1] or 0) + r["total_tokens"]
        self.assertEqual({(r["model"], r["provider_id"]): [r["sessions"], r["total_tokens"]] for r in machine["model_rollup"]},
                         merged)
        starts = {}
        for d in per_home.values():
            for s in d["heatmap"]["starts"]:
                starts[s["started_at"]] = starts.get(s["started_at"], 0) + s["sessions"]
        self.assertEqual({s["started_at"]: s["sessions"] for s in machine["heatmap"]["starts"]}, starts)
        # profile mode keeps the baseline counters while depth reads only its own home
        self.assertEqual(per_home["a"]["totals"]["sessions"], len(spec_window(A, 30)))

    def test_missing_counts_match_the_specs(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        specs = spec_window(self.all_specs(), 30)
        missing = data["depth_status"]["missing"]
        for name, field in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                            ("estimated_cost_usd", "estimated_cost_usd"), ("cache_read_tokens", "cache_read_tokens"),
                            ("cache_write_tokens", "cache_write_tokens"), ("reasoning_tokens", "reasoning_tokens")):
            self.assertEqual(missing[name], sum(1 for s in specs if s[field] is None), name)
        self.assertEqual(missing["duration"], sum(1 for s in specs if s["ended_at"] is None or s["ended_at"] < s["started_at"]))


class BadStartAccounting(DepthServiceBase):
    def test_unbucketable_start_is_excluded_and_disclosed_not_assigned_to_now(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        self.assertEqual(data["depth_status"]["excluded_started_at"], 1)
        self.assertIs(data["depth_status"]["partial"], True)
        specs = spec_window(self.all_specs(), 30)
        self.assertEqual(data["depth_totals"]["sessions"], len(specs))
        epochs = {s["started_at"] for s in data["heatmap"]["starts"]}
        self.assertTrue(all(e < NOW_TS for e in epochs))  # nothing was bucketed at "now"
