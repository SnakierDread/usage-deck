"""API-equivalent row counters across dimensions (design: analytics-depth-breakdown §6, api-equivalent-pricing §7).

The row counters count the canonical provenance leaves of the response's own ``rows``. all/profile/model share
the legacy leaves, so their blocks are identical. ``repo`` splits a legacy leaf ``(home, day, model, provider,
attribution)`` into one leaf per repository, so its counters can differ from the others.

Dollars are conserved across dimensions only when every split part has *complete* recorded tokens (input AND output
both recorded, zeros included). That covers the live ``priced +1 / proxy +1`` finding, where the extra part is a
recorded 0/0 session: it prices to a genuine 0. When a part is missing its input or its output count, the legacy
leaf sums each token column independently over the known values (``sum_known``) and prices the merged sums, while
a repo leaf requires both counts on its own sums and is otherwise unpriced (``tokens_missing``); money then differs
between dimensions. That is existing behavior, outside this card's scope; it is pinned here so a change is loud
and so the documentation cannot overclaim.
"""

import collections
import unittest

from .depth_fixture import DEPTH_COLUMNS, D
from .test_analytics_depth import DepthServiceBase
from .test_analytics_dimensions import Machine

DIMENSIONS = ("all", "profile", "model", "repo")
LEGACY = ("all", "profile", "model")
BLOCK = ("amount_pico_usd", "priced_rows", "unpriced_rows", "proxy_rows", "partial", "stale")


def legacy_key(row):
    return row["day"], row["profile"], row["is_global"], row["model_id"], row["provider_id"], row["attribution_source"]


def leaf_class(row):
    """(priced, unpriced, proxy) of one leaf, from its own wire fields."""
    api = row["metrics"]["api_equivalent"]
    return api["priced_rows"], api["unpriced_rows"], api["proxy_rows"]


class RowCountReconciliation(DepthServiceBase):
    def usage(self, dimension, window, machine=None, **extra):
        params = [("mode", "machine"), ("dimension", dimension), ("window", str(window))] + list(extra.items())
        return self.get(params, svc=machine.service() if machine is not None else None)

    def blocks(self, window, machine=None, **extra):
        return {d: self.usage(d, window, machine, **extra) for d in DIMENSIONS}

    def machine(self, specs):
        m = Machine(self)
        m.write_db(None, specs, columns=DEPTH_COLUMNS)
        return m

    def assert_structure(self, by_dim):
        """What holds in EVERY case, missing tokens included: the legacy dimensions share the legacy leaves, so the
        WHOLE block agrees, and every dimension's counters describe its own rows."""
        base = by_dim["all"]["totals"]["api_equivalent"]
        for dim in LEGACY:
            self.assertEqual({k: by_dim[dim]["totals"]["api_equivalent"][k] for k in BLOCK},
                             {k: base[k] for k in BLOCK}, dim)
        for dim, data in by_dim.items():
            api = data["totals"]["api_equivalent"]
            self.assertEqual(api["priced_rows"] + api["unpriced_rows"], len(data["rows"]), dim)
            self.assertLessEqual(api["proxy_rows"], api["priced_rows"], dim)
            self.assertEqual(api["partial"], api["unpriced_rows"] > 0, dim)
            self.assertEqual(sorted(data["pricing"]["unknown_row_ids"]),
                             sorted(r["id"] for r in data["rows"]
                                    if r["metrics"]["api_equivalent"]["amount_pico_usd"] is None), dim)

    def assert_reconciled(self, by_dim):
        """The dollar-conserving invariant, valid when every split part has complete recorded tokens (zeros count
        as recorded), stated independently of how the service builds the blocks. Returns the per-counter excess of
        the repo view over the legacy view."""
        self.assert_structure(by_dim)
        base = by_dim["all"]["totals"]["api_equivalent"]
        repo = by_dim["repo"]
        self.assertEqual(repo["totals"]["api_equivalent"]["amount_pico_usd"], base["amount_pico_usd"])
        self.assertEqual(repo["totals"]["api_equivalent_usd"], by_dim["all"]["totals"]["api_equivalent_usd"])
        # reconcile leaf by leaf: every repo leaf descends from exactly one legacy leaf and the money of the
        # parts equals the money of the whole
        legacy = {legacy_key(r): r for r in by_dim["all"]["rows"]}
        parts = collections.defaultdict(list)
        for r in repo["rows"]:
            parts[legacy_key(r)].append(r)
        self.assertEqual(set(parts), set(legacy), "repo leaves must cover exactly the legacy leaves")
        extra = collections.Counter()
        for key, leaves in parts.items():
            whole = legacy[key]["metrics"]["api_equivalent"]["amount_pico_usd"]
            known = [int(p["metrics"]["api_equivalent"]["amount_pico_usd"]) for p in leaves
                     if p["metrics"]["api_equivalent"]["amount_pico_usd"] is not None]
            if whole is None:
                self.assertEqual(known, [], key)  # an unpriced whole has no priced part
            else:
                self.assertEqual(sum(known), int(whole), key)
            for i, name in enumerate(("priced", "unpriced", "proxy")):
                extra[name] += sum(leaf_class(p)[i] for p in leaves) - leaf_class(legacy[key])[i]
        for name in ("priced", "unpriced", "proxy"):
            self.assertEqual(repo["totals"]["api_equivalent"][name + "_rows"] - base[name + "_rows"], extra[name], name)
        return {name: extra[name] for name in ("priced", "unpriced", "proxy")}

    # ---------------------------------------------------------------- the depth fixture homes

    def test_depth_fixture_every_window_and_provider_reconciles(self):
        for window in (7, 30, 90):
            for provider in (None, "anthropic", "openai", "custom"):
                with self.subTest(window=window, provider=provider):
                    extra = {"provider": provider} if provider else {}
                    self.assert_reconciled(self.blocks(window, **extra))

    def test_depth_fixture_split_is_the_missing_token_leaf_and_is_dollar_neutral(self):
        by_dim = self.blocks(30)
        # r4 (10/5 tokens, /srv/Unscoped) and r5 (no tokens, no repo) share one legacy leaf: the legacy merge
        # prices the known sums, the repo view leaves r5's part unpriced. Same money, one more unpriced leaf.
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 0, "unpriced": 1, "proxy": 0})
        reasons = [r["pricing"]["reason"] for r in by_dim["repo"]["rows"]
                   if r["metrics"]["api_equivalent"]["amount_pico_usd"] is None and r["repo_basename"] is None
                   and r["day"] == "2026-11-03"]
        self.assertEqual(reasons, ["tokens_missing"])

    def test_no_split_means_identical_blocks(self):
        # a provider filter that keeps no split leaf: every dimension carries the same block, counters included
        by_dim = self.blocks(30, provider="openai")
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 0, "unpriced": 0, "proxy": 0})
        blocks = [{k: d["totals"]["api_equivalent"][k] for k in BLOCK} for d in by_dim.values()]
        self.assertTrue(all(b == blocks[0] for b in blocks), blocks)

    # ---------------------------------------------------------------- the live shape: zero-token proxy part

    def test_zero_token_proxy_part_adds_one_priced_and_one_proxy_row_with_no_dollars(self):
        # One (day, model, provider) leaf over two repositories: the real machine's finding (window=90)
        # showed priced +1 / proxy +1 with identical dollars. The extra leaf is a recorded-zero part of a
        # proxy-priced model: priced at a genuine 0, and a proxy because the model resolves through a proxy alias.
        m = self.machine([
            D("z1", 2, "cybertiel-q5", "custom:box", 0, 0, 0.0, "/work/hermes-agent"),
            D("z2", 2, "cybertiel-q5", "custom:box", 1000, 500, 0.0, None),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 1, "unpriced": 0, "proxy": 1})
        legacy = by_dim["all"]["totals"]["api_equivalent"]
        repo = by_dim["repo"]["totals"]["api_equivalent"]
        self.assertEqual((legacy["priced_rows"], legacy["proxy_rows"]), (1, 1))
        self.assertEqual((repo["priced_rows"], repo["proxy_rows"], repo["unpriced_rows"]), (2, 2, 0))
        self.assertEqual(repo["amount_pico_usd"], legacy["amount_pico_usd"])
        self.assertNotEqual(legacy["amount_pico_usd"], "0")
        self.assertEqual(sorted(r["metrics"]["api_equivalent"]["amount_pico_usd"] == "0"
                                for r in by_dim["repo"]["rows"]), [False, True])

    def test_two_priced_parts_split_the_same_money(self):
        m = self.machine([
            D("s1", 3, "cybertiel-q5", "custom:box", 400, 100, 0.0, "/work/alpha"),
            D("s2", 3, "cybertiel-q5", "custom:box", 600, 400, 0.0, "/work/beta"),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 1, "unpriced": 0, "proxy": 1})

    def test_missing_token_part_can_flip_partial_without_moving_money(self):
        m = self.machine([
            D("m1", 3, "cybertiel-q5", "custom:box", 10, 5, 0.0, "/work/alpha"),
            D("m2", 3, "cybertiel-q5", "custom:box", None, None, None, None),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 0, "unpriced": 1, "proxy": 0})
        self.assertFalse(by_dim["all"]["totals"]["api_equivalent"]["partial"])
        self.assertTrue(by_dim["repo"]["totals"]["api_equivalent"]["partial"])

    # ---------------------------------------------------------------- one-sided missing counts: dollars DIFFER
    # Existing behavior, pinned as a known limitation (not remediated here): the legacy leaf sums each token
    # column over its known values independently (sum_known) and prices the merged pair; a repo leaf is priced only
    # when its OWN input and output sums are both recorded. The same data therefore prices differently per dimension.

    def test_one_sided_missing_tokens_complementary_parts_money_differs_by_dimension(self):
        m = self.machine([
            D("c1", 3, "cybertiel-q5", "custom:box", 10, None, 0.0, "/work/alpha"),
            D("c2", 3, "cybertiel-q5", "custom:box", None, 5, 0.0, "/work/beta"),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assert_structure(by_dim)
        base = by_dim["all"]["totals"]["api_equivalent"]
        repo = by_dim["repo"]["totals"]["api_equivalent"]
        self.assertEqual({k: base[k] for k in BLOCK},
                         {"amount_pico_usd": "10160000", "priced_rows": 1, "unpriced_rows": 0, "proxy_rows": 1,
                          "partial": False, "stale": False})
        self.assertEqual({k: repo[k] for k in BLOCK},
                         {"amount_pico_usd": None, "priced_rows": 0, "unpriced_rows": 2, "proxy_rows": 0,
                          "partial": True, "stale": False})
        self.assertIsNone(by_dim["repo"]["totals"]["api_equivalent_usd"])
        self.assertEqual([r["pricing"]["reason"] for r in by_dim["repo"]["rows"]], ["tokens_missing"] * 2)

    def test_one_sided_missing_tokens_incomplete_part_drops_from_repo_money(self):
        m = self.machine([
            D("i1", 3, "cybertiel-q5", "custom:box", 10, 5, 0.0, "/work/alpha"),
            D("i2", 3, "cybertiel-q5", "custom:box", 100, None, 0.0, "/work/beta"),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assert_structure(by_dim)
        base = by_dim["all"]["totals"]["api_equivalent"]
        repo = by_dim["repo"]["totals"]["api_equivalent"]
        self.assertEqual((base["amount_pico_usd"], base["priced_rows"], base["unpriced_rows"], base["partial"]),
                         ("35560000", 1, 0, False))
        self.assertEqual((repo["amount_pico_usd"], repo["priced_rows"], repo["unpriced_rows"], repo["partial"]),
                         ("10160000", 1, 1, True))
        self.assertLess(int(repo["amount_pico_usd"]), int(base["amount_pico_usd"]))

    def test_complete_tokens_control_conserves_money_with_the_same_prices(self):
        # same two models/rates as above but every count recorded: dollars conserve, one extra priced+proxy leaf
        m = self.machine([
            D("k1", 3, "cybertiel-q5", "custom:box", 10, 5, 0.0, "/work/alpha"),
            D("k2", 3, "cybertiel-q5", "custom:box", 100, 5, 0.0, "/work/beta"),
        ])
        by_dim = self.blocks(30, machine=m)
        self.assertEqual(self.assert_reconciled(by_dim), {"priced": 1, "unpriced": 0, "proxy": 1})
        self.assertEqual(by_dim["all"]["totals"]["api_equivalent"]["amount_pico_usd"], "43180000")
        self.assertEqual(by_dim["repo"]["totals"]["api_equivalent"]["amount_pico_usd"], "43180000")


if __name__ == "__main__":
    unittest.main()
