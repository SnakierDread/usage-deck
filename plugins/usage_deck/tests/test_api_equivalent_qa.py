"""QA suite for the API-equivalent cost feature: completion clauses C1-C5 + the read-only doctrine.

Independent of the implementation lanes' own tests (``test_pricing`` / ``test_api_equivalent``): expected values are
hand-written literals or derived from the RAW packaged JSON with ``Decimal``, never from the code under test, and
the C1 expectation is a snapshot captured from the PRE-FEATURE tree (see ``spend_golden``). Hermetic: no network,
no live prices, no production state.db; every database is a temp SQLite file.

* C1  SpendGoldenTests              pre-feature snapshot, byte-identical host spend
* C2  NullVersusZeroServiceTests    null (unpriced) vs 0 (priced, zero tokens) on rows/groups/totals, all dimensions
* C3  ResolverInventoryTests        7 inventory ids, alias precedence, heuristics, lower-quant == full cloud rate
* C4  CacheResilienceTests          failed refresh never blanks, end-to-end through the service; refresh logging
* C5  ConservationTests             rows == groups == totals == daily series, hand-computed literals
* RO  ReadOnlyTests                 no SQL write, no new/changed file beside any state.db, resolver never opens a DB
"""

import hashlib
import json
import re
import sqlite3
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from plugins.usage_deck import pricing as p
from plugins.usage_deck.service import PricingInputs

from . import spend_golden as sg
from .test_analytics_dimensions import NOW, S
from .test_api_equivalent import (
    EQUIVALENT,
    PROXY,
    SEVEN,
    Calls,
    PricedBase,
    aliases,
    catalog,
)
from .test_pricing import (
    T0,
    Clock,
    Fetcher,
    TmpCase,
    alias,
    litellm_payload,
    openrouter_payload,
    write_seed,
)
from .test_pricing import catalog as raw_catalog
from .test_pricing import entry as raw_entry
from .test_pricing import resolve as raw_resolve

PKG = Path(p.__file__).resolve().parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "spend_golden_v1.json"
PICO = 10 ** 12


def pico(usd: str) -> int:
    """Exact pico-USD of a decimal-dollar literal (hand-written expectations)."""
    return int(Decimal(usd) * PICO)


# ============================================================================================================ C1


class SpendGoldenTests(PricedBase):
    """C1: ``estimated_usd`` and every other pre-feature field are byte-identical to the pre-feature tree.

    ``fixtures/spend_golden_v1.json`` is the canonical JSON of the 8 snapshotted /usage payloads produced by the
    PRE-FEATURE code (origin/main c4a662d, no pricing module) for the fixture in ``spend_golden``. The feature
    branch must reproduce it exactly once the additive annotation keys are removed - with the default resolver
    wiring, with the resolver unavailable, and with every inventory model priced.
    """

    @classmethod
    def setUpClass(cls):
        cls.golden_text = GOLDEN.read_text()
        cls.golden = json.loads(cls.golden_text)

    def assertMatchesGolden(self, payloads):
        self.assertEqual(sorted(payloads), sorted(self.golden))
        for key in sorted(payloads):
            with self.subTest(request=key):
                self.assertEqual(sg.canonical(sg.strip(payloads[key])), sg.canonical(self.golden[key]))
        # whole-file byte identity, not just per-payload equality
        self.assertEqual(sg.canonical(sg.strip(payloads)) + "\n", self.golden_text)

    def test_golden_is_pre_feature_and_not_vacuous(self):
        text = self.golden_text
        for key in ("api_equivalent", "pricing"):
            self.assertNotIn(f'"{key}"', text, "the snapshot must predate the feature")
        totals = self.golden["machine/all/30/None"]["totals"]
        # hand sum of the 30-day fixture: 1.25 + 0.3125 + 0.0000125 + 0.5 + 7.5 (9 days ago is inside 30d)
        self.assertAlmostEqual(totals["estimated_usd"], 9.5625125, places=9)
        self.assertGreater(len(self.golden["machine/model/30/None"]["rows"]), 10)

    def test_default_resolver_wiring_leaves_spend_byte_identical(self):
        payloads = sg.collect(self.addCleanup)
        self.assertMatchesGolden(payloads)
        # ... and the feature really is present (the equality above is not trivially true)
        self.assertIn("api_equivalent_usd", payloads["machine/all/30/None"]["totals"])
        self.assertIn("pricing", payloads["machine/model/30/None"])

    def test_resolver_unavailable_leaves_spend_byte_identical(self):
        def broken(source):
            raise RuntimeError("resolver unavailable")

        self.assertMatchesGolden(sg.collect(self.addCleanup, pricing_factory=broken))

    def test_every_model_priced_leaves_spend_byte_identical(self):
        priced = lambda source: PricingInputs(catalog(), aliases(), Calls())  # noqa: E731
        payloads = sg.collect(self.addCleanup, pricing_factory=priced)
        self.assertMatchesGolden(payloads)
        self.assertIsNotNone(payloads["machine/all/30/None"]["totals"]["api_equivalent_usd"])

    def test_host_cost_is_never_substituted_into_or_by_the_new_field(self):
        priced = lambda source: PricingInputs(catalog(), aliases(), Calls())  # noqa: E731
        data = sg.collect(self.addCleanup, pricing_factory=priced)["machine/model/30/None"]
        by_model = {}
        for row in data["rows"]:
            by_model.setdefault(row["model_id"], []).append(row["metrics"])
        # gpt-6-luna has host dollars (1.25, 0.5, ...): the API-equivalent is a different number.
        for m in by_model["gpt-6-luna"]:
            self.assertNotEqual(m["api_equivalent_usd"], m["estimated_usd"])
        # mystery-model has no price: it is null, not the host's 0.0 and not a coerced zero.
        (m,) = by_model["mystery-model"]
        self.assertEqual(m["estimated_usd"], 0.0)
        self.assertIsNone(m["api_equivalent_usd"])


# ============================================================================================================ C2


class NullVersusZeroServiceTests(PricedBase):
    """C2: ``api_equivalent_usd`` on rows/groups/totals for dimension all, profile, model; null != 0."""

    def setUp(self):
        super().setUp()
        # global: priced gpt-6-luna with ZERO tokens (a real $0), priced gpt-6.1-sol (500k in/100k out = $2.00)
        self.machine.write_db(None, [
            S("z1", 1, "gpt-6-luna", "custom:lane", None, 0, 0, 0.0, 1, "custom:lane"),
            S("z2", 2, "gpt-6.1-sol", "custom:lane", None, 500_000, 100_000, 0.0, 1, "custom:lane"),
        ])
        # alpha holds ONLY an unpriced model that nevertheless has host spend and tokens.
        self.machine.write_db("alpha", [
            S("u1", 3, "mystery-model", "custom:lane", None, 4_000_000, 1_000_000, 9.99, 3, "custom:lane"),
        ])

    def data(self, dimension):
        return self.usage(self.service(), mode="machine", dimension=dimension, window=30)

    def rows(self, data):
        return {r["model_id"]: r for r in data["rows"]}

    def test_unpriced_row_is_null_not_zero_and_never_the_host_cost_in_every_dimension(self):
        for dimension in ("all", "profile", "model"):
            with self.subTest(dimension=dimension):
                row = self.rows(self.data(dimension))["mystery-model"]
                m = row["metrics"]
                self.assertIsNone(m["api_equivalent_usd"])
                self.assertIsNone(m["api_equivalent"]["amount_pico_usd"])
                self.assertEqual((m["api_equivalent"]["priced_rows"], m["api_equivalent"]["unpriced_rows"]), (0, 1))
                self.assertIs(m["api_equivalent"]["partial"], True)
                self.assertEqual(m["estimated_usd"], 9.99)  # host spend untouched, not borrowed
                self.assertEqual((row["pricing"]["method"], row["pricing"]["reason"]), ("none", "no_catalog_match"))
                self.assertIsNone(row["pricing"]["catalog_key"])
                self.assertEqual(m["input_count"], 4_000_000)  # tokens stay verbatim

    def test_priced_zero_is_a_real_zero_distinct_from_null(self):
        for dimension in ("all", "profile", "model"):
            with self.subTest(dimension=dimension):
                row = self.rows(self.data(dimension))["gpt-6-luna"]
                m = row["metrics"]
                self.assertIsNotNone(m["api_equivalent_usd"])
                self.assertEqual(m["api_equivalent_usd"], 0)
                self.assertEqual(m["api_equivalent"]["amount_pico_usd"], "0")
                self.assertEqual((m["api_equivalent"]["priced_rows"], m["api_equivalent"]["unpriced_rows"]), (1, 0))
                self.assertIs(m["api_equivalent"]["partial"], False)
                self.assertIsNone(row["pricing"]["reason"])

    def test_totals_exclude_unpriced_and_list_it_in_the_unknown_bucket(self):
        for dimension in ("all", "profile", "model"):
            with self.subTest(dimension=dimension):
                data = self.data(dimension)
                t = data["totals"]
                self.assertEqual(t["api_equivalent"]["amount_pico_usd"], str(pico("2.00")))  # luna $0 + sol $2
                self.assertEqual(t["api_equivalent_usd"], 2.0)  # NOT 2.0 + 9.99-ish and not shifted by a fake 0
                self.assertEqual((t["api_equivalent"]["priced_rows"], t["api_equivalent"]["unpriced_rows"]), (2, 1))
                self.assertIs(t["api_equivalent"]["partial"], True)
                bad = self.rows(data)["mystery-model"]["id"]
                self.assertEqual(data["pricing"]["unknown_row_ids"], [bad])
                for r in data["rows"]:
                    self.assertEqual(r["id"] == bad, r["metrics"]["api_equivalent"]["amount_pico_usd"] is None)

    def test_group_that_holds_only_unpriced_rows_is_null_and_a_zero_group_is_zero(self):
        data = self.data("profile")
        groups = {g["label"]: g for g in data["groups"]}
        self.assertEqual(len(groups), 2, groups.keys())
        null_groups = [g for g in groups.values() if g["totals"]["api_equivalent_usd"] is None]
        self.assertEqual(len(null_groups), 1)
        self.assertEqual(null_groups[0]["totals"]["api_equivalent"]["unpriced_rows"], 1)
        self.assertEqual(null_groups[0]["totals"]["api_equivalent"]["priced_rows"], 0)
        (priced,) = [g for g in groups.values() if g is not null_groups[0]]
        self.assertEqual(priced["totals"]["api_equivalent_usd"], 2.0)
        model = {g["model_id"]: g for g in self.data("model")["groups"] if g["model_id"]}
        self.assertIsNone(model["mystery-model"]["totals"]["api_equivalent_usd"])
        self.assertEqual(model["gpt-6-luna"]["totals"]["api_equivalent_usd"], 0)
        self.assertIsNotNone(model["gpt-6-luna"]["totals"]["api_equivalent_usd"])

    def test_daily_series_keeps_null_days_null_and_zero_days_zero(self):
        points = {pt["day"]: pt for s in self.data("all")["series"] for pt in s["points"]}
        self.assertEqual(len(points), 3)
        nulls = [pt for pt in points.values() if pt["api_equivalent_usd"] is None]
        zeros = [pt for pt in points.values() if pt["api_equivalent_usd"] == 0 and pt["api_equivalent_usd"] is not None]
        self.assertEqual((len(nulls), len(zeros)), (1, 1))
        self.assertIsNone(nulls[0]["api_equivalent"]["amount_pico_usd"])
        self.assertEqual(zeros[0]["api_equivalent"]["amount_pico_usd"], "0")

    def test_wire_json_spells_null_and_zero_differently(self):
        data = self.data("model")
        text = json.dumps(self.rows(data)["mystery-model"]["metrics"], sort_keys=True)
        self.assertIn('"api_equivalent_usd": null', text)
        self.assertIn('"amount_pico_usd": null', text)
        text0 = json.dumps(self.rows(data)["gpt-6-luna"]["metrics"], sort_keys=True)
        self.assertRegex(text0, r'"api_equivalent_usd": 0(\.0)?[,}]')
        self.assertIn('"amount_pico_usd": "0"', text0)

    def test_unpriced_models_when_the_resolver_itself_is_unavailable_are_null_everywhere(self):
        data = self.usage(self.service(pricing=None), mode="machine", dimension="model", window=30)
        self.assertIsNone(data["totals"]["api_equivalent_usd"])
        self.assertEqual(len(data["pricing"]["unknown_row_ids"]), len(data["rows"]))
        for r in data["rows"]:
            self.assertIsNone(r["metrics"]["api_equivalent_usd"])  # never a fabricated 0


# ============================================================================================================ C3


def raw_seed():
    return json.loads((PKG / "pricing_data" / "seed-v1.json").read_text())


def raw_aliases():
    return json.loads((PKG / "pricing_data" / "aliases.json").read_text())["aliases"]


def with_entries(base, *extra):
    mapping = {**base.entries, **{e.key: e for e in extra}}
    return p.CatalogSnapshot(mapping, p.compute_revision(mapping), "seed", ())


class ResolverInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = p.load_seed()
        cls.aliases = p.load_aliases()
        cls.raw_seed = raw_seed()["entries"]
        cls.raw_aliases = raw_aliases()

    def price(self, model, tin=1_000_000, tout=1_000_000, **kw):
        return p.resolve_api_equivalent(model, tin, tout, catalog=kw.get("cat", self.seed),
                                        aliases=kw.get("aliases", self.aliases), now=T0)

    def test_the_inventory_is_exactly_the_seven_expected_ids(self):
        # The design's seven plus the two follow-up spellings found by the live host check (t_5561a3ef).
        self.assertEqual(set(self.raw_aliases), set(SEVEN) | {"claude-sonnet-5.5", "cybertiel-q5"})
        self.assertEqual(len(SEVEN), 7)

    def test_each_inventory_model_resolves_to_a_non_null_price_matching_the_raw_seed(self):
        for model in SEVEN:
            with self.subTest(model):
                key = self.raw_aliases[model]["catalog_key"]
                raw = self.raw_seed[key]  # straight from the packaged JSON, not via the loader
                want = (Decimal(1_234_567) * Decimal(raw["input_usd_per_token"])
                        + Decimal(89_012) * Decimal(raw["output_usd_per_token"]))
                r = self.price(model, 1_234_567, 89_012)
                self.assertIsNotNone(r.amount_pico_usd)
                self.assertIsNotNone(r.api_equivalent_usd)
                self.assertGreater(r.amount_pico_usd, 0)
                self.assertEqual(r.amount_pico_usd, int((want * PICO).quantize(Decimal(1))))
                self.assertEqual(r.catalog_key, key)
                self.assertIsNone(r.reason)

    def test_inventory_kinds_three_equivalent_four_proxy(self):
        kinds = {m: self.price(m).alias_kind for m in SEVEN}
        self.assertEqual(sorted(m for m, k in kinds.items() if k == EQUIVALENT),
                         ["claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol"])
        self.assertEqual(sum(1 for k in kinds.values() if k == PROXY), 4)
        for m in SEVEN:
            self.assertTrue(self.price(m).note, m)  # every price carries its disclosure

    def test_alias_beats_a_heuristic_exact_key_for_every_inventory_model(self):
        # A decoy catalog entry whose key IS the recorded model id: the heuristic would take it. The alias wins.
        # (claude-sonnet-5-5 aliases to its own exact key, so a decoy there would replace the alias target itself.)
        decoyed = [m for m in SEVEN if self.raw_aliases[m]["catalog_key"] != "litellm:" + m]
        self.assertEqual(len(decoyed), 6)
        cat = with_entries(self.seed, *[raw_entry("litellm:" + m, "0.000000001", "0.000000001") for m in decoyed])
        for model in decoyed:
            with self.subTest(model):
                with_alias = self.price(model, cat=cat)
                self.assertEqual((with_alias.method, with_alias.catalog_key),
                                 ("alias", self.raw_aliases[model]["catalog_key"]))
                without = self.price(model, cat=cat, aliases={})
                self.assertEqual((without.method, without.catalog_key), ("heuristic", "litellm:" + model))
                self.assertEqual(without.amount_pico_usd, 2 * 10 ** 9)  # 2M tokens * 1e-9 USD: it is the decoy
                self.assertNotEqual(with_alias.amount_pico_usd, without.amount_pico_usd)

    def test_without_the_alias_file_the_three_exact_keys_still_price_and_the_custom_four_stay_unknown(self):
        for model in ("gpt-6-luna", "gpt-6.1-sol", "claude-sonnet-5-5"):
            r = self.price(model, aliases={})
            self.assertEqual(r.method, "heuristic", model)
            self.assertEqual(r.amount_pico_usd, self.price(model).amount_pico_usd, model)  # same tariff either way
        for model in ("qwen3.8-flash-next", "qwen3.6:35b", "cyber-tiel-coder:35b",
                      "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k"):
            r = self.price(model, aliases={})
            self.assertEqual((r.method, r.amount_pico_usd, r.api_equivalent_usd), ("none", None, None), model)

    def test_cyber_tiel_has_no_heuristic_signal_even_when_a_35b_entry_exists(self):
        cat = raw_catalog(raw_entry("litellm:vendor/qwen3.6-35b", "0.000001", "0.000002"),
                          raw_entry("litellm:vendor/coder-35b", "0.000001", "0.000002"))
        self.assertEqual(p.query_tokens("cyber-tiel-coder:35b"), ("cyber", "tiel", "coder", "35b"))
        r = raw_resolve("cyber-tiel-coder:35b", cat=cat)
        self.assertEqual((r.method, r.catalog_key, r.amount_pico_usd), ("none", None, None))
        proxied = raw_resolve("cyber-tiel-coder:35b", cat=cat,
                              aliases={"cyber-tiel-coder:35b": alias("litellm:vendor/qwen3.6-35b", "proxy")})
        self.assertEqual((proxied.method, proxied.alias_kind), ("alias", "proxy"))

    def test_heuristic_strips_quant_and_hardware_tags_and_colon_sizes_to_the_full_model(self):
        cat = raw_catalog(raw_entry("litellm:vendor/qwen3.8-27b", "0.0000004", "0.000003"),
                          raw_entry("litellm:vendor/qwen3.8-flash", "0.0000005", "0.000002"))
        full = raw_resolve("qwen3.8-27b", cat=cat)
        self.assertEqual((full.method, full.catalog_key), ("heuristic", "litellm:vendor/qwen3.8-27b"))
        for name in ("qwen3.8-27b-iq3-gpu-96k-b8k", "qwen3.8-27b-iq2-gpu-8k", "qwen3.8-27b-q4_k_m",
                     "qwen3.8-27b-q8-gpu-32k-b4k", "qwen3.8:27b", "qwen3.8:27b-iq4"):
            with self.subTest(name):
                r = raw_resolve(name, cat=cat)
                self.assertEqual((r.method, r.catalog_key), ("heuristic", full.catalog_key))
                self.assertEqual(r.amount_pico_usd, full.amount_pico_usd)  # lower quant == full cloud rate
                self.assertEqual(full.amount_pico_usd, 3_400_000_000_000)  # 0.4 + 3.0 per 1M/1M tokens

    def test_custom_variant_words_are_not_stripped_so_flash_next_needs_its_alias(self):
        cat = raw_catalog(raw_entry("litellm:vendor/qwen3.8-flash", "0.0000005", "0.000002"))
        self.assertEqual(raw_resolve("qwen3.8-flash-next", cat=cat).method, "none")
        r = raw_resolve("qwen3.8-flash-next", cat=cat, aliases={"qwen3.8-flash-next": alias("litellm:vendor/qwen3.8-flash",
                                                                                             "proxy")})
        self.assertEqual((r.method, r.amount_pico_usd), ("alias", 2_500_000_000_000))

    def test_the_hauhau_custom_name_prices_exactly_like_the_full_cloud_27b(self):
        model = "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k"
        key = self.raw_aliases[model]["catalog_key"]
        self.assertEqual(key, "litellm:aiand/qwen/qwen3.8-27b")
        for tin, tout in ((1_000_000, 1_000_000), (123_456, 7_890), (0, 5), (999, 0)):
            cloud = self.raw_seed[key]
            want = int(((Decimal(tin) * Decimal(cloud["input_usd_per_token"])
                         + Decimal(tout) * Decimal(cloud["output_usd_per_token"])) * PICO).quantize(Decimal(1)))
            # the "full cloud" reference: the same 27B entry priced through its own plain-name alias
            ref = self.price("qwen3.8-27b", tin, tout,
                             aliases={"qwen3.8-27b": alias(key, "equivalent")})
            got = self.price(model, tin, tout)
            self.assertEqual(got.amount_pico_usd, ref.amount_pico_usd)
            self.assertEqual(got.amount_pico_usd, want)
            self.assertEqual(got.catalog_key, ref.catalog_key)
        self.assertEqual(self.price(model).amount_pico_usd, 3_400_000_000_000)  # $0.40 + $3.00 per 1M/1M

    def test_hauhau_style_name_with_a_cloud_twin_in_the_catalog_prices_as_that_twin(self):
        cat = raw_catalog(raw_entry("litellm:vendor/qwen3.8-hauhau-aggressive-27b", "0.0000004", "0.000003"))
        full = raw_resolve("qwen3.8-hauhau-aggressive-27b", cat=cat)
        quant = raw_resolve("qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k", cat=cat)
        self.assertEqual((quant.method, quant.catalog_key, quant.amount_pico_usd),
                         ("heuristic", full.catalog_key, full.amount_pico_usd))


# ============================================================================================================ C4


def snapshot_amount(snap, model="prov/m1", tin=1_000_000, tout=0):
    return p.resolve_api_equivalent(model, tin, tout, catalog=snap, aliases={}, now=T0).amount_pico_usd


class CacheResilienceTests(TmpCase):
    """C4: a failed refresh never blanks shown values; every refresh is logged with fetched_at."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.seed = write_seed(self.tmp / "seed.json", {
            "litellm:prov/m1": ("0.000001", "0.000002"), "openrouter:v/m2": ("0.000003", "0.000004")},
            fetched_at=T0 - timedelta(hours=30))
        self.fetch = Fetcher(litellm=litellm_payload({"prov/m1": (5e-06, 6e-06)}),
                             openrouter=openrouter_payload({"v/m2": ("0.000010", "0.000020")}))

    def mgr(self, **kw):
        return self.manager(self.fetch, clock=self.clock, seed=self.seed, **kw)

    def break_everything(self):
        self.fetch.set("litellm", OSError("down"))
        self.fetch.set("openrouter", b"<html>captive portal</html>")

    def test_failed_refresh_keeps_the_previously_fetched_price_not_blank_not_the_seed(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        self.assertEqual(snapshot_amount(mgr.snapshot()), 5 * PICO // 1)  # 1M tokens * 5e-6 USD
        self.clock.advance(days=3)
        self.break_everything()
        mgr.refresh_if_due()
        after = mgr.snapshot()
        self.assertEqual(snapshot_amount(after), 5 * PICO)
        self.assertEqual(snapshot_amount(after, "v/m2"), 10 * PICO)
        self.assertIn(p.WARN_REFRESH_FAILED, after.warnings)
        self.assertEqual(after.entries["litellm:prov/m1"].fetched_at, T0)  # the age of the data is not reset

    def test_failed_refresh_on_a_cold_install_still_serves_the_seed(self):
        self.break_everything()
        mgr = self.mgr()
        mgr.refresh_if_due()
        self.assertEqual(snapshot_amount(mgr.snapshot()), PICO)  # seed: 1M * 1e-6
        self.assertIn(p.WARN_REFRESH_FAILED, mgr.snapshot().warnings)

    def test_failure_after_restart_serves_the_last_good_disk_cache(self):
        self.mgr().refresh_if_due()
        self.clock.advance(days=3)
        self.break_everything()
        restarted = self.mgr()
        restarted.refresh_if_due()
        self.assertEqual(snapshot_amount(restarted.snapshot()), 5 * PICO)

    def test_a_partial_failure_updates_the_good_source_and_keeps_the_failed_one(self):
        self.fetch.set("openrouter", OSError("down"))
        mgr = self.mgr()
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snapshot_amount(snap), 5 * PICO)  # litellm refreshed
        self.assertEqual(snapshot_amount(snap, "v/m2"), 3 * PICO)  # openrouter kept its seeded price

    def test_refresh_success_is_logged_with_fetched_at_matching_cache_and_entries(self):
        mgr = self.mgr()
        with self.assertLogs("usage_deck.pricing", level="INFO") as logs:
            mgr.refresh_if_due()
        stamp = p.format_ts(T0)
        refreshed = [m for m in logs.output if "pricing: refreshed" in m]
        self.assertEqual(len(refreshed), 2, logs.output)
        for source in ("litellm", "openrouter"):
            (line,) = [m for m in refreshed if f"source={source} " in m]
            self.assertIn(f"fetched_at={stamp}", line)
            self.assertEqual(json.loads(self.cache_path.read_text())["sources"][source]["fetched_at"], stamp)
        self.assertEqual(mgr.snapshot().entries["litellm:prov/m1"].fetched_at, T0)
        self.assertTrue(all(m.startswith("INFO:") for m in refreshed))

    def test_each_later_refresh_logs_its_own_fetched_at(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        self.clock.advance(days=2)
        with self.assertLogs("usage_deck.pricing", level="INFO") as logs:
            mgr.refresh_if_due()
        later = p.format_ts(T0 + timedelta(days=2))
        self.assertEqual(sum(1 for m in logs.output if f"fetched_at={later}" in m), 2, logs.output)
        self.assertFalse(any(f"fetched_at={p.format_ts(T0)}" in m for m in logs.output))

    def test_failed_refresh_is_logged_as_a_warning_without_claiming_a_fetched_at(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        self.clock.advance(days=2)
        self.break_everything()
        with self.assertLogs("usage_deck.pricing", level="INFO") as logs:
            mgr.refresh_if_due()
        failed = [m for m in logs.output if "refresh failed" in m]
        self.assertEqual(len(failed), 2, logs.output)
        for line in failed:
            self.assertTrue(line.startswith("WARNING:"))
            self.assertIn(f"attempted_at={p.format_ts(T0 + timedelta(days=2))}", line)
            self.assertNotIn("fetched_at=", line)
        self.assertFalse(any("pricing: refreshed" in m for m in logs.output))
        self.assertNotIn("down", "\n".join(logs.output))  # no upstream error text / URL echoed
        self.assertNotIn("http", "\n".join(logs.output))


class ServiceCacheResilienceTests(PricedBase):
    """C4 end to end: the numbers on the wire survive a failed refresh (default resolver class, injected fetch)."""

    def test_wire_values_survive_a_failed_refresh_and_a_restart(self):
        tmp = TmpCase("run")
        tmp.setUp()
        self.addCleanup(tmp.tearDown)
        self.machine.write_db(None, [S("g1", 1, "prov/m1", "custom:lane", None, 1_000_000, 0, 0.0, 1, "custom:lane")])
        clock = Clock(NOW)
        seed = write_seed(tmp.tmp / "seed.json", {"litellm:prov/m1": ("0.000001", "0.000002")}, fetched_at=NOW - timedelta(hours=30))
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (5e-06, 6e-06)}), openrouter=OSError("down"))
        data_dir = self.machine.root / "plugin-data" / "usage-deck"

        def service(manager):
            svc = self.service(pricing=False)  # the default wiring, with our manager pre-registered for the root
            svc._pricing_managers[str(data_dir)] = manager
            return svc

        def manager():
            return p.PricingCatalogManager(data_dir, clock=clock, random_uniform=lambda a, b: a, fetch=fetch,
                                           sleep=lambda s: None, background=False, seed_path=seed)

        def total(svc):  # "prov/m1" is not aliased: it prices heuristically by its exact catalog key
            data = self.usage(svc, mode="machine", dimension="model", window=30)
            return data["totals"]["api_equivalent"]["amount_pico_usd"], data

        mgr = manager()
        svc = service(mgr)
        seeded, _ = total(svc)
        self.assertEqual(seeded, str(PICO))  # seed price 1e-6 * 1M tokens
        mgr.refresh_if_due()
        fresh, data = total(svc)
        self.assertEqual(fresh, str(5 * PICO))
        self.assertEqual(data["pricing"]["origin"], "network")
        fetched = data["rows"][0]["pricing"]["fetched_at"]
        self.assertEqual(p.parse_ts(fetched), NOW)
        clock.advance(days=2)
        fetch.set("litellm", OSError("down"))
        mgr.refresh_if_due()
        kept, data = total(svc)
        self.assertEqual(kept, str(5 * PICO))  # not blanked, not rolled back to the seed
        self.assertIsNotNone(data["totals"]["api_equivalent_usd"])
        self.assertEqual(data["rows"][0]["pricing"]["fetched_at"], fetched)
        self.assertIn("pricing_refresh_failed", data["pricing"]["warnings"])
        restarted = manager()
        restarted.refresh_if_due()
        again, _ = total(service(restarted))
        self.assertEqual(again, str(5 * PICO))


# ============================================================================================================ C5


class ConservationTests(PricedBase):
    """C5: per-model amounts == profile rollups == grand total == daily series, with hand-computed literals.

    Tariffs ($ per 1M tokens, in/out) of the double catalog: luna 0.10/0.50, sol 2.00/10.00, flash-next 0.15/0.47.
      global  luna  1M in / 2M out   = 0.10 + 1.00 = 1.10
      global  sol   500k in / 100k out = 1.00 + 1.00 = 2.00
      alpha   luna  3M in / 0 out    = 0.30
      alpha   flash-next 2M in / 1M out = 0.30 + 0.47 = 0.77
      alpha   mystery-model (unpriced, excluded)
    """

    TOTAL = pico("4.17")
    BY_PROFILE = {"global": pico("3.10"), "alpha": pico("1.07")}
    BY_MODEL = {"gpt-6-luna": pico("1.40"), "gpt-6.1-sol": pico("2.00"), "qwen3.8-flash-next": pico("0.77"),
                "mystery-model": None}

    def setUp(self):
        super().setUp()
        self.machine.write_db(None, [
            S("g1", 1, "gpt-6-luna", "custom:lane", None, 1_000_000, 2_000_000, 0.0, 1, "custom:lane"),
            S("g2", 2, "gpt-6.1-sol", "custom:lane", None, 500_000, 100_000, 0.0, 1, "custom:lane"),
        ])
        self.machine.write_db("alpha", [
            S("a1", 1, "gpt-6-luna", "custom:lane", None, 3_000_000, 0, 0.0, 1, "custom:lane"),
            S("a2", 3, "qwen3.8-flash-next", "custom:lane", None, 2_000_000, 1_000_000, 0.0, 1, "custom:lane"),
            S("a3", 2, "mystery-model", "custom:lane", None, 123, 45, 0.0, 1, "custom:lane"),
        ])

    @staticmethod
    def amount(metrics):
        value = metrics["api_equivalent"]["amount_pico_usd"]
        return None if value is None else int(value)

    def test_hand_computed_literals_hold_for_every_dimension(self):
        for dimension in ("all", "profile", "model"):
            with self.subTest(dimension=dimension):
                data = self.usage(self.service(), mode="machine", dimension=dimension, window=30)
                self.assertEqual(self.amount(data["totals"]), self.TOTAL)
                self.assertEqual(data["totals"]["api_equivalent_usd"], 4.17)
                rows = [r for r in data["rows"]]
                known = [self.amount(r["metrics"]) for r in rows if self.amount(r["metrics"]) is not None]
                self.assertEqual(len(rows), 5)
                self.assertEqual(sum(known), self.TOTAL)  # rows == grand total
                self.assertEqual(sum(self.amount(g["totals"]) or 0 for g in data["groups"]), self.TOTAL)  # groups == total
                self.assertEqual(sum(1 for r in rows if self.amount(r["metrics"]) is None), 1)

    def test_per_model_amounts_match_the_literals(self):
        data = self.usage(self.service(), mode="machine", dimension="model", window=30)
        groups = {g["model_id"]: self.amount(g["totals"]) for g in data["groups"] if g["model_id"]}
        self.assertEqual(groups, self.BY_MODEL)
        # and each group equals the sum of the rows it names
        by_id = {r["id"]: r for r in data["rows"]}
        for g in data["groups"]:
            parts = [self.amount(by_id[i]["metrics"]) for i in g["row_ids"]]
            self.assertEqual(sum(x or 0 for x in parts), self.amount(g["totals"]) or 0, g["label"])

    def test_profile_rollups_match_the_literals_and_sum_to_the_grand_total(self):
        data = self.usage(self.service(), mode="machine", dimension="profile", window=30)
        got = {("global" if g["profiles"] == ["global"] or g["label"] == "global" else g["label"]): self.amount(g["totals"])
               for g in data["groups"]}
        self.assertEqual(sorted(got.values()), sorted(self.BY_PROFILE.values()), data["groups"])
        self.assertEqual(sum(got.values()), self.amount(data["totals"]))
        alpha = [g for g in data["groups"] if self.amount(g["totals"]) == self.BY_PROFILE["alpha"]][0]
        self.assertIs(alpha["totals"]["api_equivalent"]["partial"], True)  # carries the unpriced mystery row
        glob = [g for g in data["groups"] if self.amount(g["totals"]) == self.BY_PROFILE["global"]][0]
        self.assertIs(glob["totals"]["api_equivalent"]["partial"], False)

    def test_daily_series_sum_to_the_total_in_every_dimension_and_to_their_group(self):
        for dimension in ("all", "profile", "model"):
            with self.subTest(dimension=dimension):
                data = self.usage(self.service(), mode="machine", dimension=dimension, window=30)
                series_sum = 0
                groups = {g["series_id"]: g for g in data["groups"]}
                for s in data["series"]:
                    mine = sum(self.amount(pt) or 0 for pt in s["points"])
                    self.assertEqual(mine, self.amount(groups[s["id"]]["totals"]) or 0, s["id"])
                    series_sum += mine
                self.assertEqual(series_sum, self.TOTAL)

    def test_float_image_is_the_exact_integer_over_1e12_at_every_level(self):
        data = self.usage(self.service(), mode="machine", dimension="model", window=30)
        nodes = [data["totals"]] + [g["totals"] for g in data["groups"]] + [r["metrics"] for r in data["rows"]]
        for m in nodes:
            amount = self.amount(m)
            self.assertEqual(m["api_equivalent_usd"], None if amount is None else float(Decimal(amount) / PICO))

    def test_priced_plus_unpriced_row_counts_conserve(self):
        for dimension in ("all", "profile", "model"):
            data = self.usage(self.service(), mode="machine", dimension=dimension, window=30)
            api = data["totals"]["api_equivalent"]
            self.assertEqual((api["priced_rows"], api["unpriced_rows"], api["proxy_rows"]), (4, 1, 1))
            self.assertEqual(api["priced_rows"] + api["unpriced_rows"], len(data["rows"]))

    def test_the_services_aggregator_agrees_with_the_resolver_contract_aggregator(self):
        """Production sums leaves in ``dimensions.api_aggregate``; ``pricing.aggregate_resolutions`` is the contract
        twin. Two implementations of one rule must agree on every grouping (no silent divergence)."""
        from types import SimpleNamespace

        from plugins.usage_deck.adapters.dimensions import api_aggregate

        cat, als = p.load_seed(), p.load_aliases()
        models = SEVEN + ["mystery-model", "another-unknown", None]
        late = T0 + timedelta(days=30)  # past the 7-day staleness bound of the packaged seed
        leaves = []
        for i, model in enumerate(models * 2):
            tokens = (i * 1_000_003, i * 7) if i % 5 else (0, 0)
            leaves.append(p.resolve_api_equivalent(model, *tokens, catalog=cat, aliases=als, now=late if i % 4 == 0 else T0))
        groupings = [leaves, leaves[:3], leaves[5:6], [x for x in leaves if x.amount_pico_usd is None], []]
        for items in groupings:
            want = p.aggregate_resolutions((f"row{i}", r) for i, r in enumerate(items))
            usd, got = api_aggregate([SimpleNamespace(price=r) for r in items])
            self.assertEqual(got.amount_pico_usd, None if want.amount_pico_usd is None else str(want.amount_pico_usd))
            self.assertEqual(usd, want.api_equivalent_usd)
            self.assertEqual((got.priced_rows, got.unpriced_rows, got.proxy_rows, got.partial, got.stale),
                             (want.priced_rows, want.unpriced_rows, want.proxy_rows, want.partial, want.stale))
        self.assertTrue(any(r.stale for r in leaves) and any(r.amount_pico_usd is None for r in leaves))

    def test_every_window_conserves(self):
        for window in (7, 30, 90):
            data = self.usage(self.service(), mode="machine", dimension="model", window=window)
            self.assertEqual(self.amount(data["totals"]), self.TOTAL, window)  # every fixture session is <= 3 days old
            self.assertEqual(sum(self.amount(g["totals"]) or 0 for g in data["groups"]), self.TOTAL, window)


# ============================================================================================================ RO

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|REINDEX|ANALYZE|SAVEPOINT|"
                       r"BEGIN\s+(IMMEDIATE|EXCLUSIVE)|PRAGMA\s+\w+\s*=\s*(?!ON\b)\S)", re.IGNORECASE)


class SqlSpy:
    """Records every sqlite3 connection (args) and statement while installed over ``sqlite3.connect``."""

    def __init__(self, test):
        self.connects, self.statements = [], []
        real = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real(*args, **kwargs)
            self.connects.append((args, kwargs))
            conn.set_trace_callback(self.statements.append)
            return conn

        patcher = mock.patch.object(sqlite3, "connect", connect)
        patcher.start()
        test.addCleanup(patcher.stop)

    def writes(self):
        return [s for s in self.statements if WRITE_SQL.match(s)]

    def uri_targets(self):
        return [a[0] for a, kw in self.connects if kw.get("uri")]


def tree(root: Path, skip=("plugin-data",)):
    """{relative path: (size, mtime_ns, sha256)} of every file below ``root`` outside ``skip`` dirs."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in skip:
            continue
        if path.is_file():
            st = path.stat()
            out[str(rel)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


class ReadOnlyTests(PricedBase):
    """Read-only doctrine (C5 of the brief): no state.db writes from the resolver, the service or the UI."""

    def seed_machine(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:lane", None, 1000, 10, 0.0, 1, "custom:lane"),
                                     S("g2", 2, "mystery-model", "custom:lane", None, 7, 1, 0.0, 1, "custom:lane")])
        self.machine.write_db("alpha", [S("a1", 1, "gpt-6.1-sol", "custom:lane", None, 5000, 50, 0.0, 1, "custom:lane")])

    def test_priced_requests_leave_every_file_beside_state_db_untouched(self):
        self.seed_machine()
        before = tree(self.machine.root)
        self.assertIn("state.db", before)
        for wiring in (True, None, False):  # double / resolver unavailable / default manager wiring
            for mode, dimension in (("machine", "all"), ("machine", "profile"), ("machine", "model"),
                                    ("profile", "all"), ("profile", "model")):
                svc = self.service(pricing=wiring)
                self.usage(svc, mode=mode, dimension=dimension, window=30)
                self.usage(svc, mode=mode, dimension=dimension, window=30)  # second one is cache-served
        self.assertEqual(tree(self.machine.root), before)  # bytes, size AND mtime of every state.db/sidecar
        names = sorted(Path(k).name for k in before)
        self.assertEqual([n for n in names if n.startswith("state.db")], ["state.db", "state.db"])  # no -wal/-journal

    def test_no_write_statement_and_only_read_only_uris_during_priced_requests(self):
        self.seed_machine()
        spy = SqlSpy(self)
        for dimension in ("all", "profile", "model"):
            self.usage(self.service(), mode="machine", dimension=dimension, window=30)
        self.assertTrue(spy.statements, "the spy saw no SQL - it is not wired to the code under test")
        self.assertEqual(spy.writes(), [])
        # every session-store connection opened by the product code is an explicit mode=ro URI
        self.assertTrue(spy.uri_targets())
        for target in spy.uri_targets():
            self.assertRegex(target, r"[?&]mode=ro(&|$)", target)

    def test_the_resolver_and_catalog_manager_never_open_a_database(self):
        tmp = TmpCase("run")
        tmp.setUp()
        self.addCleanup(tmp.tearDown)

        def refuse(*a, **kw):
            raise AssertionError("pricing must not open a sqlite database: %r" % (a,))

        seed = write_seed(tmp.tmp / "seed.json", {"litellm:prov/m1": ("0.000001", "0.000002")})
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (5e-06, 6e-06)}), openrouter=OSError("down"))
        with mock.patch.object(sqlite3, "connect", refuse):
            seed_cat, al = p.load_seed(), p.load_aliases()
            for model in SEVEN + ["mystery-model", None, 42]:
                p.resolve_api_equivalent(model, 10, 10, catalog=seed_cat, aliases=al, now=T0)
            mgr = p.PricingCatalogManager(tmp.data_dir, clock=Clock(), random_uniform=lambda a, b: a, fetch=fetch,
                                          sleep=lambda s: None, background=False, seed_path=seed)
            mgr.refresh_if_due()
            mgr.snapshot()
            p.aggregate_resolutions([])  # contract: pure aggregation, still no DB

    def test_catalog_refresh_writes_only_under_plugin_data_never_beside_a_state_db(self):
        self.seed_machine()
        tmp = TmpCase("run")
        tmp.setUp()
        self.addCleanup(tmp.tearDown)
        before = tree(self.machine.root)
        data_dir = self.machine.root / "plugin-data" / "usage-deck"
        # the service's OWN choice of catalog directory (default wiring) is under <root>/plugin-data/usage-deck
        probe = self.service(pricing=False)
        self.usage(probe, mode="machine", dimension="all", window=30)
        self.assertEqual(list(probe._pricing_managers), [str(data_dir)])
        seed = write_seed(tmp.tmp / "seed.json", {"litellm:gpt-6-luna": ("0.000001", "0.000002")})
        fetch = Fetcher(litellm=litellm_payload({"gpt-6-luna": (5e-06, 6e-06)}), openrouter=OSError("down"))
        mgr = p.PricingCatalogManager(data_dir, clock=Clock(NOW), random_uniform=lambda a, b: a, fetch=fetch,
                                      sleep=lambda s: None, background=False, seed_path=seed)
        svc = self.service(pricing=False)
        svc._pricing_managers[str(data_dir)] = mgr
        mgr.refresh_if_due()
        self.usage(svc, mode="machine", dimension="model", window=30)
        self.assertEqual(tree(self.machine.root), before)  # everything outside plugin-data is byte-identical
        written = sorted(str(f.relative_to(self.machine.root)) for f in data_dir.rglob("*") if f.is_file())
        self.assertEqual(written, ["plugin-data/usage-deck/pricing/.catalog.lock",
                                   "plugin-data/usage-deck/pricing/catalog-v1.json"])
        self.assertFalse(any(Path(w).name.startswith("state.db") for w in written))

    def test_authorized_home_binding_applies_to_the_new_field(self):
        # clause 6 of the dimensions contract: a request cannot price (or read) a home outside the authorized set.
        self.seed_machine()
        svc = self.service()
        status, body = svc.handle("usage", [("mode", "profile"), ("profile", "../../etc"), ("dimension", "model"),
                                            ("window", "30")])
        self.assertNotEqual(status, 200, body)
        data = self.usage(svc, mode="machine", dimension="model", window=30)
        self.assertEqual(sorted(r["profile"] for r in data["rows"]), ["alpha", "global", "global"])


if __name__ == "__main__":
    unittest.main()
