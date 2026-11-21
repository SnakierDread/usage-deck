"""Per-model tables group by model id ONLY; provider is a displayed attribute (t_e364e654).

Provider attribution is sparse at the source, so keying on (model, attributed provider) listed one model twice
(its resolved provider and ``unknown``). Contract: C1 resolved > Unknown and differing resolved providers share
ONE comma-listed row; C2 raw sums and the $/Mtok numerator/denominator are conserved under re-grouping (ratios are
recomputed from merged sums, never averaged); C3 ``unknown`` only when no session of the model resolves; C4
attribution inputs are unchanged (exact model ids, read-only). Expected values are literals or are computed here
from the fixture specs, never from the code under test.
"""

import unittest
from decimal import Decimal
from unittest import mock

from plugins.usage_deck.adapters import analytics_depth as depth
from plugins.usage_deck.adapters.session_db import UNKNOWN
from plugins.usage_deck.models import dto

from .test_analytics_depth import build, facts_of, rf

M = "gpt-5.6-sol"  # pinned tariff: openai d=0.1 (see test_analytics_depth.CacheTests)
USAGE = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_write_tokens", "cache_read_tokens")


def sess(provider=None, billing=None, model=M, i=0, o=0, r=0, w=0, c=0, cost=0.0):
    return rf(model=model, provider=provider, billing=billing, input_tokens=i, output_tokens=o, reasoning_tokens=r,
              cache_write_tokens=w, cache_read_tokens=c, cost=cost)


def cache_rows(*raws, incomplete=False):
    return depth.build_cache_metrics(facts_of(*raws)[0], depth.load_rate_table(), incomplete)


def model_rows(*raws):
    return depth.build_model_rollup(facts_of(*raws)[0])


# Table of provider-resolution states for ONE model id: (name, sessions, displayed provider label).
CASES = (
    ("all resolved, same provider", [sess("openai", i=10), sess("openai", i=20)], "openai"),
    ("resolved + null", [sess("openai", i=10), sess(None, i=20)], "openai"),
    ("null + resolved (order independent)", [sess(None, i=20), sess("openai", i=10)], "openai"),
    ("all null", [sess(None, i=10), sess(None, i=20)], UNKNOWN),
    ("auto and blank never resolve", [sess("auto", i=10), sess("", i=20), sess(None, billing="auto", i=5)], UNKNOWN),
    ("two differing resolved providers", [sess("openai", i=10), sess("anthropic", i=20)], "anthropic,openai"),
    ("two resolved + null: no Unknown label", [sess("openai", i=10), sess("anthropic", i=20), sess(None, i=30)],
     "anthropic,openai"),
    ("billing-only resolution", [sess(None, billing="custom:pgx", i=10), sess(None, i=20)], "custom:pgx"),
    ("session + billing resolutions differ", [sess("openai", i=10), sess(None, billing="custom:pgx", i=20),
                                              sess(None, i=30)], "custom:pgx,openai"),
    ("bare custom billing is not a resolution", [sess(None, billing="custom", i=10), sess(None, i=20)], UNKNOWN),
)


class OneRowPerModel(unittest.TestCase):
    def test_table_driven_one_row_with_precedence_label(self):
        for name, raws, label in CASES:
            with self.subTest(name):
                cache, models = cache_rows(*raws), model_rows(*raws)
                self.assertEqual([r.model for r in cache], [M])  # C1: one row, grouped by model id only
                self.assertEqual([r.model for r in models], [M])
                self.assertEqual(cache[0].provider_id, label)
                self.assertEqual(models[0].provider_id, label)
                if label != UNKNOWN:  # C3: Unknown never rides along with a resolved label
                    self.assertNotIn(UNKNOWN, cache[0].provider_id.split(","))
                self.assertEqual(models[0].sessions, len(raws))
                self.assertEqual(cache[0].uncached_input_tokens, sum(r.input_tokens for r in raws))

    def test_exact_model_ids_stay_separate_and_null_model_is_one_row_first(self):
        raws = [sess("openai", model="aa", i=1), sess(None, model="aa", i=2), sess("anthropic", model="aa", i=3),
                sess("openai", model="AA", i=4), sess(None, model=None, i=5), sess("openai", model=None, i=6),
                sess("openai", model="zz", i=7)]
        rows = cache_rows(*raws)
        self.assertEqual([(r.model, r.provider_id) for r in rows],
                         [(None, "openai"), ("AA", "openai"), ("aa", "anthropic,openai"), ("zz", "openai")])
        self.assertEqual([(r.model, r.provider_id, r.sessions) for r in model_rows(*raws)],
                         [(None, "openai", 2), ("AA", "openai", 1), ("aa", "anthropic,openai", 3), ("zz", "openai", 1)])

    def test_provider_list_is_sorted_and_stable_across_input_order(self):
        a = cache_rows(sess("openai", i=1), sess("anthropic", i=1), sess("deepseek", i=1))
        b = cache_rows(sess("deepseek", i=1), sess("openai", i=1), sess("anthropic", i=1))
        self.assertEqual(a[0].provider_id, "anthropic,deepseek,openai")
        self.assertEqual(a, b)

    def test_overflowing_provider_list_is_cut_at_a_provider_boundary_and_stays_valid(self):
        names = [f"vendor-{n:03d}-" + "x" * 20 for n in range(12)]  # joined > 128 characters
        raws = [sess(p, i=1) for p in names]
        row = cache_rows(*raws)[0]
        self.assertLessEqual(len(row.provider_id), depth.MAX_PROVIDER_LABEL)
        parts = row.provider_id.split(",")
        self.assertEqual(parts[:-1], sorted(names)[:len(parts) - 1])
        self.assertEqual(parts[-1], f"+{len(names) - (len(parts) - 1)} more")
        self.assertEqual(model_rows(*raws)[0].provider_id, row.provider_id)  # CacheRow/ModelRow validate max_len

    def test_unresolved_provider_is_not_a_mixed_row_key_anywhere_in_the_envelope(self):
        data = build(*CASES[1][1]).data
        self.assertEqual([(r["model"], r["provider_id"]) for r in data["cache_metrics"]], [(M, "openai")])
        self.assertEqual([(r["model"], r["provider_id"]) for r in data["model_rollup"]], [(M, "openai")])


# ---- pre-fix snapshot (captured from origin/main e90a859 / 61d182e BEFORE this change) ------------------------
# fixture: M openai (1000/100/10/50/2000, $0.01) + M openai (500/50, $0.004) + M unresolved (300/30/5/20, $0.002)
PRE_FIX_FIXTURE = [
    sess("openai", i=1000, o=100, r=10, w=50, c=2000, cost=0.01),
    sess("openai", i=500, o=50, cost=0.004),
    sess(None, i=300, o=30, r=5, w=20, cost=0.002),
]
PRE_FIX_CACHE = (  # (provider_id, hit_rate_pct, cached, uncached, reasoning, effective $/Mtok, writes, output)
    ("openai", 56.33802816901409, 2000, 1500, 10, 7.329842931937173, 50, 150),
    ("unknown", 0.0, 0, 300, 5, 5.633802816901408, 20, 30),
)
PRE_FIX_MODEL = (("openai", 2, 1650, 0.014), ("unknown", 1, 330, 0.002))
PRE_FIX_COSTS = (0.014, 0.002)


class ConservationAgainstPreFix(unittest.TestCase):
    def test_pre_fix_snapshot_shows_the_duplicate_this_card_removes(self):
        self.assertEqual([p[0] for p in PRE_FIX_CACHE], ["openai", "unknown"])  # same model, two rows

    def test_merged_row_conserves_every_split_row_sum(self):
        (row,) = cache_rows(*PRE_FIX_FIXTURE)
        self.assertEqual(row.provider_id, "openai")
        pre = PRE_FIX_CACHE
        self.assertEqual(row.cached_input_tokens, sum(p[2] for p in pre))
        self.assertEqual(row.uncached_input_tokens, sum(p[3] for p in pre))
        self.assertEqual(row.reasoning_tokens, sum(p[4] for p in pre))
        self.assertEqual(row.cache_write_tokens, sum(p[6] for p in pre))
        self.assertEqual(row.output_tokens, sum(p[7] for p in pre))
        # hit rate recomputed from the merged numerator/denominator, never the average of 56.33.. and 0.0
        cached = sum(p[2] for p in pre)
        denominator = sum(p[3] + p[6] + p[2] for p in pre)
        self.assertAlmostEqual(row.hit_rate_pct, 100 * cached / denominator, places=12)
        self.assertNotAlmostEqual(row.hit_rate_pct, sum(p[1] for p in pre) / 2, places=3)
        # the weighted denominators of the split rows (recovered from their recorded effective rate) add up
        weighted = sum(1e6 * cost / p[5] for cost, p in zip(PRE_FIX_COSTS, pre))
        self.assertAlmostEqual(weighted, 2265.0, places=6)
        self.assertAlmostEqual(row.effective_cost_per_mtok, 1e6 * sum(PRE_FIX_COSTS) / weighted, places=9)
        self.assertEqual((row.discount_multiplier, row.rate_source, row.reason), (0.1, "openai-gpt-5.6-2026-07", None))

    def test_model_rollup_conserves_sessions_tokens_and_cost(self):
        (row,) = model_rows(*PRE_FIX_FIXTURE)
        self.assertEqual(row.sessions, sum(p[1] for p in PRE_FIX_MODEL))
        self.assertEqual(row.total_tokens, sum(p[2] for p in PRE_FIX_MODEL))
        self.assertAlmostEqual(row.total_cost, sum(p[3] for p in PRE_FIX_MODEL), places=12)

    def test_every_case_sums_equal_the_old_per_provider_split(self):
        """For each table case: group the facts the OLD way (model, provider), then the merged row's raw sums must
        equal the sum of the split groups', and its ratio must come from those sums (independent Decimal math)."""
        for name, raws, _label in CASES:
            with self.subTest(name):
                facts = facts_of(*raws)[0]
                split = {}
                for f in facts:
                    split.setdefault((f.model, f.provider), []).append(f)
                (row,) = depth.build_cache_metrics(facts, depth.load_rate_table(), False)
                total = {n: sum(getattr(f, n) for f in facts) for n in USAGE}
                self.assertEqual(row.uncached_input_tokens, total["input_tokens"])
                self.assertEqual(row.output_tokens, total["output_tokens"])
                self.assertEqual(row.uncached_input_tokens, sum(sum(f.input_tokens for f in g) for g in split.values()))
                self.assertEqual(row.cached_input_tokens, sum(sum(f.cache_read_tokens for f in g) for g in split.values()))
                denominator = total["input_tokens"] + total["cache_write_tokens"] + total["cache_read_tokens"]
                want = None if denominator == 0 else 100 * total["cache_read_tokens"] / denominator
                if want is None:
                    self.assertIsNone(row.hit_rate_pct)
                else:
                    self.assertAlmostEqual(row.hit_rate_pct, want, places=12)

    def test_merging_is_reversible_in_sums_and_costs_across_a_shuffled_fixture(self):
        raws = [sess("openai", i=10, o=5, w=1, c=40, cost=0.5), sess(None, i=20, o=7, c=0, cost=0.25),
                sess("openai", i=30, o=9, r=2, c=60, cost=0.125), sess(None, i=1, o=1, cost=0.0625)]
        (fwd,) = cache_rows(*raws)
        (rev,) = cache_rows(*reversed(raws))
        self.assertEqual(fwd, rev)
        weighted = Decimal(61 + 22 + 2 + 1) + Decimal(100) * Decimal("0.1")  # I+O+R+W, plus C*d
        self.assertAlmostEqual(fwd.effective_cost_per_mtok, float(Decimal(10 ** 6) * Decimal("0.9375") / weighted), places=9)


class TariffOfAMergedRow(unittest.TestCase):
    def test_resolved_tariff_weights_the_unresolved_sessions_of_the_same_model(self):
        (row,) = cache_rows(sess("openai", i=100, c=1000, cost=1.0), sess(None, i=100, c=1000, cost=1.0))
        self.assertEqual(row.discount_multiplier, 0.1)
        self.assertEqual(row.reason, None)
        self.assertAlmostEqual(row.effective_cost_per_mtok, 1e6 * 2.0 / (200 + 2000 * 0.1), places=9)

    def test_all_unknown_keeps_the_old_unknown_discount_semantics(self):
        (reads,) = cache_rows(sess(None, i=100, c=1000, cost=1.0))
        self.assertEqual((reads.provider_id, reads.reason, reads.effective_cost_per_mtok), (UNKNOWN, "unknown_discount", None))
        (clean,) = cache_rows(sess(None, i=100, c=0, cost=1.0))  # no reads: the missing tariff is irrelevant
        self.assertEqual((clean.reason, clean.effective_cost_per_mtok), (None, 1e6 * 1.0 / 100))

    def test_resolved_provider_without_a_tariff_is_still_unknown_discount(self):
        (row,) = cache_rows(sess("custom:pgx", i=100, c=1000, cost=1.0), sess(None, i=1, cost=0.0))
        self.assertEqual((row.provider_id, row.discount_multiplier, row.rate_source), ("custom:pgx", None, None))
        self.assertEqual((row.reason, row.effective_cost_per_mtok), ("unknown_discount", None))

    def test_providers_with_different_tariffs_keep_their_own_discount_per_session(self):
        table = depth.load_rate_table()
        pairs = [(p, m) for (p, m), r in table.rows.items() if r.discount is not None]
        by_model = {}
        for p, m in pairs:
            by_model.setdefault(m, {})[p] = table.rows[(p, m)].discount
        shared = [(m, ds) for m, ds in by_model.items() if len(set(ds.values())) > 1]
        if not shared:
            self.skipTest("no model in the pinned snapshot has two providers with different cache discounts")
        model, ds = shared[0]
        (p1, d1), (p2, d2) = sorted(ds.items())[:2]
        if d1 == d2:
            (p1, d1), (p2, d2) = sorted(ds.items(), key=lambda kv: kv[1])[0], sorted(ds.items(), key=lambda kv: kv[1])[-1]
        raws = [sess(p1, model=model, i=100, c=1000, cost=1.0), sess(p2, model=model, i=100, c=1000, cost=1.0)]
        (row,) = cache_rows(*raws)
        weighted = Decimal(200) + 1000 * d1 + 1000 * d2
        self.assertIsNone(row.discount_multiplier)  # no single multiplier describes the row
        self.assertAlmostEqual(row.effective_cost_per_mtok, float(Decimal(10 ** 6) * 2 / weighted), places=9)
        unresolved = cache_rows(*raws, sess(None, model=model, i=1, c=5, cost=0.0))[0]
        self.assertEqual((unresolved.reason, unresolved.effective_cost_per_mtok), ("unknown_discount", None))

    def test_incomplete_source_and_missing_usage_still_null_the_merged_ratios(self):
        (row,) = cache_rows(sess("openai", i=100, c=100, cost=1.0), sess(None, i=100, c=0, cost=1.0), incomplete=True)
        self.assertEqual((row.hit_rate_pct, row.effective_cost_per_mtok, row.reason), (None, None, "incomplete_source"))
        raws = [sess("openai", i=100, c=100, cost=1.0), rf(model=M, provider=None, input_tokens=None, cost=1.0)]
        (partial,) = cache_rows(*raws)
        self.assertEqual((partial.reason, partial.hit_rate_pct), ("missing_usage", None))
        self.assertEqual(partial.discount_multiplier, 0.1)  # the multiplier needs no usage


class WireAndGate(unittest.TestCase):
    def test_depth_envelope_conserves_and_validates_with_a_merged_label(self):
        raws = [sess("openai", i=10, cost=0.1), sess("anthropic", i=20, cost=0.2), sess(None, i=30, cost=0.3),
                sess(None, model="solo", i=5, cost=0.05)]
        data = build(*raws).data
        self.assertEqual([(r["model"], r["provider_id"]) for r in data["cache_metrics"]],
                         [(M, "anthropic,openai"), ("solo", "unknown")])
        self.assertEqual(data["depth_totals"]["sessions"], 4)
        self.assertEqual(sum(r["sessions"] for r in data["model_rollup"]), 4)
        dto.CacheRow(**data["cache_metrics"][0])  # wire validators accept the comma label
        dto.ModelRow(**data["model_rollup"][0])

    def test_grouping_reads_no_provider_state_and_makes_no_network_call(self):
        facts, rates = facts_of(*PRE_FIX_FIXTURE)[0], depth.load_rate_table()
        with mock.patch("socket.socket", side_effect=AssertionError("network used")), \
                mock.patch("builtins.open", side_effect=AssertionError("file opened")):
            rows = depth.build_cache_metrics(facts, rates, False)
        self.assertEqual(len(rows), 1)

    def test_attribution_inputs_are_unchanged(self):
        f = facts_of(*PRE_FIX_FIXTURE)[0]
        self.assertEqual([(x.model, x.provider, x.attribution) for x in f],
                         [(M, "openai", "session_provider"), (M, "openai", "session_provider"), (M, UNKNOWN, "unknown")])


if __name__ == "__main__":
    unittest.main()
