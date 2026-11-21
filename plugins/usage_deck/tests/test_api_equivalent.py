"""API-equivalent cost annotation of the analytics payload (docs/design/api-equivalent-pricing.md §6-§7).

Backend integration lane: DTO/sanitizer/service/dimensions. The resolver is consumed through its contract via the
``pricing_double`` (no network, no disk catalog, no live prices). Every expected amount is computed here from the
fixture sessions and the per-million tariffs with ``Decimal`` arithmetic, never from the code under test.
"""

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from plugins.usage_deck.discovery import FileInventorySource
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.dto import (
    EMPTY_API_EQUIVALENT,
    ApiEquivalent,
    DtoError,
    ProvenanceRow,
    RowPricing,
    UsageMetrics,
    check_envelope,
)
from plugins.usage_deck.models.redaction import is_sensitive_name, sanitize
from plugins.usage_deck.service import PricingInputs, UsageDeckService, alias_digest

from . import pricing_double as pd
from .helpers import FakeTransport
from .test_analytics_dimensions import NOW, DirectSqlAnalytics, Machine, S

# The seven inventory ids (design §4) with the per-million tariffs observed in the design's captured payload.
EQUIVALENT = "equivalent"
PROXY = "proxy"
INVENTORY = {
    "gpt-6-luna": ("litellm:azure_ai/gpt-6-luna", EQUIVALENT, "0.10", "0.50"),
    "gpt-6.1-sol": ("litellm:azure_ai/gpt-6.1-sol", EQUIVALENT, "2.00", "10.00"),
    "claude-sonnet-5-5": ("litellm:claude-sonnet-5-5", EQUIVALENT, "2.00", "10.00"),
    "qwen3.8-flash-next": ("litellm:dashscope/qwen3.8-flash", PROXY, "0.15", "0.47"),
    "qwen3.6:35b": ("litellm:aihubmix/qwen3.6-35b-a3b", PROXY, "0.254", "1.524"),
    "cyber-tiel-coder:35b": ("litellm:aihubmix/qwen3.6-35b-a3b", PROXY, "0.254", "1.524"),
    "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k": ("litellm:aiand/qwen/qwen3.8-27b", PROXY, "0.40", "3.00"),
}
SEVEN = list(INVENTORY)
NOTE = "Prices as a cataloged tariff; not subscription spend."
HALF_PICO_MODEL = "half-pico-model"  # 0.5 pico-USD per token: proves one quantization per leaf, not per rollup


def catalog(**extra):
    entries = {}
    for key, _kind, tin, tout in INVENTORY.values():
        entries[key] = pd.entry(key, tin, tout)
    entries["litellm:half/pico"] = pd.CatalogEntry("litellm", "half/pico", Decimal("0.0000000000005"),
                                                   Decimal(0), pd.FETCHED)
    entries.update(extra)
    return pd.snapshot(entries.values())


def aliases():
    out = {model: pd.Alias(key, kind, NOTE) for model, (key, kind, _i, _o) in INVENTORY.items()}
    out[HALF_PICO_MODEL] = pd.Alias("litellm:half/pico", EQUIVALENT, NOTE)
    return out


def expected_pico(model, tokens_in, tokens_out):
    _key, _kind, tin, tout = INVENTORY[model]
    total = Decimal(tokens_in) * pd.per_million(tin) + Decimal(tokens_out) * pd.per_million(tout)
    return int((total * 10 ** 12).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


class Calls:
    """Counts resolver calls (one per leaf per computed response)."""

    def __init__(self):
        self.n = 0

    def __call__(self, *a, **kw):
        self.n += 1
        return pd.resolve_api_equivalent(*a, **kw)


class PricedBase(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(self)
        self.calls = Calls()
        self.snapshot = catalog()
        self.aliases = aliases()
        self.sources = []

    def service(self, home=None, pricing=True, clock=lambda: NOW, snapshot=None, alias_map=None):
        source_home = self.machine.home(home)
        src = FileInventorySource(source_home, self.machine.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)

        def analytics(profile):
            if profile is None or profile.lower() == "current":
                return DirectSqlAnalytics(source_home)
            return DirectSqlAnalytics(self.machine.root if profile == "default" else self.machine.home(profile))

        def factory(source):
            self.sources.append(source)
            if pricing is None:
                raise RuntimeError("resolver unavailable")
            return PricingInputs(snapshot or self.snapshot, alias_map or self.aliases, self.calls)

        kwargs = {"pricing_factory": factory} if pricing is not False else {}
        return UsageDeckService(inventory_factory=lambda: src, analytics_factory=analytics,
                                transport=FakeTransport(), clock=clock, **kwargs)

    def usage(self, svc, **params):
        items = [(k, str(v)) for k, v in params.items()]
        status, body = svc.handle("usage", items)
        self.assertEqual(status, 200, body)
        check_envelope(body)
        return body["data"]


def seven_sessions():
    """One session per model on its own day (so every model is its own leaf), 1M tokens scale for clean maths."""
    out = []
    for i, model in enumerate(SEVEN):
        out.append(S(f"s{i}", i + 1, model, "custom:lane", None, 1_000_000 + i, 200_000 + 3 * i, 0.0, 1, "custom:lane"))
    return out


class AllSevenModelsTests(PricedBase):
    def write(self, sessions):
        path = self.machine.home(None) / "state.db"
        if path.exists():
            path.unlink()
        self.machine.write_db(None, sessions)

    def setUp(self):
        super().setUp()
        self.write(seven_sessions())

    def test_every_inventory_model_is_priced_by_alias_with_visible_proxy_disclosure(self):
        data = self.usage(self.service(), mode="machine", dimension="model", window=30)
        rows = {r["model_id"]: r for r in data["rows"]}
        self.assertEqual(sorted(rows), sorted(SEVEN))  # exact recorded ids, nothing normalized
        for i, model in enumerate(SEVEN):
            key, kind, _tin, _tout = INVENTORY[model]
            row = rows[model]
            want = expected_pico(model, 1_000_000 + i, 200_000 + 3 * i)
            self.assertEqual(row["metrics"]["api_equivalent"]["amount_pico_usd"], str(want), model)
            self.assertEqual(row["metrics"]["api_equivalent_usd"], float(Decimal(want) / 10 ** 12), model)
            p = row["pricing"]
            self.assertEqual((p["catalog_key"], p["method"], p["alias_kind"], p["reason"], p["source"], p["stale"]),
                             (key, "alias", kind, None, "litellm", False), model)
            self.assertEqual(p["note"], NOTE)
            self.assertEqual(p["fetched_at"], "2026-11-05T12:00:00.831243Z")
            self.assertEqual(row["metrics"]["api_equivalent"]["proxy_rows"], 1 if kind == PROXY else 0)
        self.assertEqual(data["pricing"]["unknown_row_ids"], [])
        self.assertEqual(sum(1 for r in rows.values() if r["pricing"]["alias_kind"] == PROXY), 4)

    def test_totals_groups_and_series_conserve_integer_pico_usd(self):
        want = sum(expected_pico(m, 1_000_000 + i, 200_000 + 3 * i) for i, m in enumerate(SEVEN))
        for dimension in ("all", "model"):
            data = self.usage(self.service(), mode="machine", dimension=dimension, window=30)
            self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(want), dimension)
            group_sum = sum(int(g["totals"]["api_equivalent"]["amount_pico_usd"]) for g in data["groups"])
            self.assertEqual(group_sum, want, dimension)
            daily = sum(int(p["api_equivalent"]["amount_pico_usd"]) for s in data["series"] for p in s["points"])
            if dimension == "all":
                self.assertEqual(daily, want)
            self.assertEqual(data["totals"]["api_equivalent"]["priced_rows"], 7)
            self.assertEqual(data["totals"]["api_equivalent"]["proxy_rows"], 4)
            self.assertFalse(data["totals"]["api_equivalent"]["partial"])

    def test_other_rollup_includes_every_member_amount(self):
        # 7 models + 3 more (all priced as gpt-6-luna) = 10 > top-N 8: the tail lands in Other
        extra = {f"filler-{i}": pd.Alias("litellm:azure_ai/gpt-6-luna", EQUIVALENT, NOTE) for i in range(3)}
        sessions = seven_sessions() + [S(f"f{i}", 9 + i, f"filler-{i}", "custom:lane", None, 10 + i, 1, 0.0, 1,
                                         "custom:lane") for i in range(3)]
        self.write(sessions)
        data = self.usage(self.service(alias_map={**self.aliases, **extra}), mode="machine", dimension="model")
        self.assertTrue(any(g["is_other"] for g in data["groups"]))
        other = [g for g in data["groups"] if g["is_other"]][0]
        members = {r["id"]: r for r in data["rows"] if r["id"] in other["row_ids"]}
        self.assertEqual(int(other["totals"]["api_equivalent"]["amount_pico_usd"]),
                         sum(int(r["metrics"]["api_equivalent"]["amount_pico_usd"]) for r in members.values()))
        self.assertEqual(sum(int(g["totals"]["api_equivalent"]["amount_pico_usd"]) for g in data["groups"]),
                         int(data["totals"]["api_equivalent"]["amount_pico_usd"]))


class NullVersusZeroTests(PricedBase):
    def test_unknown_model_is_null_excluded_and_listed_never_zero(self):
        sessions = [S("a", 1, "gpt-6-luna", "custom:x", None, 1000, 100, 0.0, 1, "custom:x"),
                    S("b", 2, "mystery-model", "custom:x", None, 5000, 500, 0.0, 1, "custom:x")]
        self.machine.write_db(None, sessions)
        data = self.usage(self.service(), mode="machine", dimension="model")
        rows = {r["model_id"]: r for r in data["rows"]}
        unknown = rows["mystery-model"]
        self.assertIsNone(unknown["metrics"]["api_equivalent_usd"])
        self.assertIsNone(unknown["metrics"]["api_equivalent"]["amount_pico_usd"])
        self.assertEqual((unknown["pricing"]["method"], unknown["pricing"]["reason"]), ("none", "no_catalog_match"))
        self.assertEqual(data["pricing"]["unknown_row_ids"], [unknown["id"]])
        want = expected_pico("gpt-6-luna", 1000, 100)
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(want))  # unknown excluded
        self.assertTrue(data["totals"]["api_equivalent"]["partial"])
        self.assertEqual(data["totals"]["api_equivalent"]["unpriced_rows"], 1)
        group = {g["model_id"]: g for g in data["groups"]}["mystery-model"]
        self.assertIsNone(group["totals"]["api_equivalent_usd"])  # an all-unknown group stays null, not 0

    def test_zero_tokens_price_to_numeric_zero_but_missing_tokens_are_null(self):
        sessions = [S("z", 1, "gpt-6-luna", "custom:x", None, 0, 0, 0.0, 1, "custom:x"),
                    S("m", 2, "gpt-6.1-sol", "custom:x", None, None, 50, 0.0, 1, "custom:x")]
        self.machine.write_db(None, sessions)
        data = self.usage(self.service(), mode="machine", dimension="model")
        rows = {r["model_id"]: r for r in data["rows"]}
        zero = rows["gpt-6-luna"]
        self.assertEqual(zero["metrics"]["api_equivalent"]["amount_pico_usd"], "0")
        self.assertIs(type(zero["metrics"]["api_equivalent_usd"]), float)
        self.assertEqual(zero["metrics"]["api_equivalent_usd"], 0.0)
        self.assertIsNone(zero["pricing"]["reason"])
        missing = rows["gpt-6.1-sol"]
        self.assertIsNone(missing["metrics"]["api_equivalent_usd"])
        self.assertEqual(missing["pricing"]["reason"], "tokens_missing")
        self.assertEqual(missing["pricing"]["method"], "alias")  # identity resolved even without tokens
        self.assertIn(missing["id"], data["pricing"]["unknown_row_ids"])
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], "0")  # a known zero, not null

    def test_no_observations_gives_null_everywhere(self):
        self.machine.write_db(None, [])
        data = self.usage(self.service(), mode="machine", dimension="all")
        self.assertIsNone(data["totals"]["api_equivalent_usd"])
        self.assertEqual(data["totals"]["api_equivalent"],
                         {"amount_pico_usd": None, "priced_rows": 0, "unpriced_rows": 0, "proxy_rows": 0,
                          "partial": False, "stale": False})
        self.assertEqual(data["pricing"]["unknown_row_ids"], [])

    def test_alias_target_missing_is_unknown_not_another_key(self):
        sessions = [S("a", 1, "gpt-6-luna", "custom:x", None, 10, 10, 0.0, 1, "custom:x")]
        self.machine.write_db(None, sessions)
        snap = pd.snapshot([e for k, e in self.snapshot.entries.items() if k != "litellm:azure_ai/gpt-6-luna"])
        data = self.usage(self.service(snapshot=snap), mode="machine", dimension="model")
        row = data["rows"][0]
        self.assertIsNone(row["metrics"]["api_equivalent_usd"])
        self.assertEqual((row["pricing"]["reason"], row["pricing"]["catalog_key"]),
                         ("alias_target_missing", "litellm:azure_ai/gpt-6-luna"))


class QuantizationAndConservationTests(PricedBase):
    def test_quantized_once_per_leaf_never_at_rollups(self):
        # one token on each of three days at 0.5 pico/token: each leaf rounds half-even to 0; the merged
        # token count (3) would round to 2. Rollups sum leaves, so the total is 0, not 2.
        sessions = [S(f"h{i}", i + 1, HALF_PICO_MODEL, "custom:x", None, 1, 0, 0.0, 1, "custom:x") for i in range(3)]
        sessions.append(S("h4", 5, HALF_PICO_MODEL, "custom:x", None, 3, 0, 0.0, 1, "custom:x"))  # 1.5 -> 2
        self.machine.write_db(None, sessions)
        data = self.usage(self.service(), mode="machine", dimension="model")
        amounts = sorted(int(r["metrics"]["api_equivalent"]["amount_pico_usd"]) for r in data["rows"])
        self.assertEqual(amounts, [0, 0, 0, 2])
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], "2")
        self.assertEqual(data["groups"][0]["totals"]["api_equivalent"]["amount_pico_usd"], "2")

    def test_profiles_models_all_and_daily_agree_over_two_homes(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:x", None, 1000, 10, 1.0, 1, "custom:x"),
                                     S("g2", 2, "mystery", "custom:x", None, 77, 7, 0.0, 1, "custom:x")])
        self.machine.write_db("alpha", [S("a1", 1, "gpt-6-luna", "custom:x", None, 3000, 30, 0.0, 1, "custom:x"),
                                        S("a2", 3, "claude-sonnet-5-5", "custom:x", None, 5000, 50, 0.0, 1, "custom:x")])
        want = (expected_pico("gpt-6-luna", 1000, 10) + expected_pico("gpt-6-luna", 3000, 30)
                + expected_pico("claude-sonnet-5-5", 5000, 50))
        totals = {}
        for dimension in ("all", "profile", "model"):
            data = self.usage(self.service(), mode="machine", dimension=dimension, window=30)
            totals[dimension] = data
            self.assertEqual(int(data["totals"]["api_equivalent"]["amount_pico_usd"]), want, dimension)
            self.assertEqual(sum(int(g["totals"]["api_equivalent"]["amount_pico_usd"]) for g in data["groups"]
                                 if g["totals"]["api_equivalent"]["amount_pico_usd"] is not None), want, dimension)
            self.assertEqual(sum(int(p["api_equivalent"]["amount_pico_usd"]) for s in data["series"]
                                 for p in s["points"] if p["api_equivalent"]["amount_pico_usd"] is not None),
                             want, dimension)
            self.assertEqual(data["totals"]["api_equivalent"]["unpriced_rows"], 1, dimension)
            self.assertEqual(sum(g["totals"]["api_equivalent"]["unpriced_rows"] for g in data["groups"]), 1, dimension)
        by_profile = {g["id"]: g for g in totals["profile"]["groups"]}
        alpha = by_profile['profile:[false,"alpha"]']
        self.assertEqual(int(alpha["totals"]["api_equivalent"]["amount_pico_usd"]),
                         expected_pico("gpt-6-luna", 3000, 30) + expected_pico("claude-sonnet-5-5", 5000, 50))
        self.assertFalse(alpha["totals"]["api_equivalent"]["partial"])
        default = by_profile['profile:[true,"global"]']
        self.assertTrue(default["totals"]["api_equivalent"]["partial"])  # carries the unpriced mystery row

    def test_provider_filter_scopes_every_new_field_to_the_filtered_leaves(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:x", None, 1000, 10, 0.0, 1, "custom:x"),
                                     S("g2", 2, "gpt-6-luna", "custom:y", None, 2000, 20, 0.0, 1, "custom:y"),
                                     S("g3", 3, "mystery", "custom:y", None, 1, 1, 0.0, 1, "custom:y")])
        data = self.usage(self.service(), mode="machine", dimension="model", provider="custom:y")
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(expected_pico("gpt-6-luna", 2000, 20)))
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(len(data["pricing"]["unknown_row_ids"]), 1)


class ProfileLegacySeriesTests(PricedBase):
    def test_profile_all_keeps_host_numbers_and_prices_only_from_scoped_rows(self):
        self.machine.write_db("alpha", [S("a1", 1, "gpt-6-luna", "custom:x", None, 4000, 40, 0.25, 1, "custom:x"),
                                        S("a2", 4, "mystery", "custom:x", None, 9, 9, 0.5, 1, "custom:x")])
        priced = self.service(home="alpha")
        plain = self.service(home="alpha", pricing=None)
        a = self.usage(priced, mode="profile", dimension="all", window=30)
        b = self.usage(plain, mode="profile", dimension="all", window=30)
        self.assertEqual(strip(a), strip(b))  # C1: every pre-feature field identical, host arithmetic kept
        pts = {p["day"]: p for p in a["series"][0]["points"]}
        priced_day = [p for p in pts.values() if p["api_equivalent"]["priced_rows"] == 1]
        unpriced_day = [p for p in pts.values() if p["api_equivalent"]["unpriced_rows"] == 1]
        self.assertEqual(len(priced_day), 1)
        self.assertEqual(priced_day[0]["api_equivalent"]["amount_pico_usd"], str(expected_pico("gpt-6-luna", 4000, 40)))
        self.assertEqual(len(unpriced_day), 1)
        self.assertIsNone(unpriced_day[0]["api_equivalent_usd"])  # a day with only unpriced leaves is null
        self.assertEqual(a["totals"]["estimated_usd"], 0.75)  # host estimated spend untouched


def strip(obj):
    """A copy with every pricing-annotation key removed (what the payload was before this feature)."""
    drop = {"api_equivalent_usd", "api_equivalent", "pricing"}
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


class SpendParityTests(PricedBase):
    """C1: estimated_usd and every pre-feature field are byte-identical with or without the annotation."""

    def test_old_fields_byte_identical_across_dimensions_with_and_without_pricing(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "openrouter", None, 1000, 10, 1.25, 1, "openrouter"),
                                     S("g2", 2, "mystery", "custom:x", None, 77, 7, 0.0, 2, "custom:x")])
        self.machine.write_db("alpha", [S("a1", 1, "claude-sonnet-5-5", "custom:x", None, 5000, 50, 0.5, 1, "custom:x")])
        for dimension in ("all", "profile", "model"):
            for mode in ("machine",):
                a = self.usage(self.service(), mode=mode, dimension=dimension, window=30)
                b = self.usage(self.service(pricing=None), mode=mode, dimension=dimension, window=30)
                self.assertEqual(json.dumps(strip(a), sort_keys=True), json.dumps(strip(b), sort_keys=True), dimension)
                self.assertEqual(a["totals"]["estimated_usd"], 1.75)

    def test_pricing_resolver_failure_degrades_to_unpriced_not_an_error(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:x", None, 1000, 10, 0.0, 1, "custom:x")])
        data = self.usage(self.service(pricing=None), mode="machine", dimension="model")
        self.assertIsNone(data["totals"]["api_equivalent_usd"])
        self.assertEqual(data["pricing"]["warnings"], ["pricing_seed_invalid"])
        self.assertEqual(data["pricing"]["unknown_row_ids"], [data["rows"][0]["id"]])
        self.assertEqual(data["rows"][0]["metrics"]["input_count"], 1000)  # counts stay verbatim


class StaleAndCacheTests(PricedBase):
    def test_stale_entries_keep_their_amount_and_surface_everywhere(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:x", None, 1000, 10, 0.0, 1, "custom:x")])
        late = lambda: NOW + timedelta(days=30)  # noqa: E731 - fetched_at is now > 7 days old
        # window 90 so the session is still inside the window after the clock moved
        data = self.usage(self.service(clock=late), mode="machine", dimension="model", window=90)
        row = data["rows"][0]
        self.assertTrue(row["pricing"]["stale"])
        self.assertEqual(row["metrics"]["api_equivalent"]["amount_pico_usd"], str(expected_pico("gpt-6-luna", 1000, 10)))
        self.assertTrue(data["totals"]["api_equivalent"]["stale"])
        self.assertTrue(data["groups"][0]["totals"]["api_equivalent"]["stale"])
        self.assertEqual(data["pricing"]["warnings"], ["pricing_stale"])

    def test_catalog_revision_and_alias_digest_key_the_response_cache(self):
        self.machine.write_db(None, [S("g1", 1, "gpt-6-luna", "custom:x", None, 1000, 10, 0.0, 1, "custom:x")])
        svc = self.service()
        # Same inputs: the second request is served from the response cache (resolver not called again).
        self.usage(svc, mode="machine", dimension="all")
        first = self.calls.n
        self.usage(svc, mode="machine", dimension="all")
        self.assertEqual(self.calls.n, first)
        # A refreshed catalog (new revision) is a new cache identity: recomputed on the very next lookup.
        cheaper = pd.snapshot([pd.entry("litellm:azure_ai/gpt-6-luna", "0.05", "0.25"),
                               *[e for k, e in self.snapshot.entries.items() if k != "litellm:azure_ai/gpt-6-luna"]])
        self.assertNotEqual(cheaper.revision, self.snapshot.revision)
        self.snapshot = cheaper
        svc2 = svc  # same service, same cache: factory now serves the new snapshot
        data = self.usage(svc2, mode="machine", dimension="all")
        self.assertGreater(self.calls.n, first)
        want = int((Decimal(1000) * pd.per_million("0.05") + Decimal(10) * pd.per_million("0.25")) * 10 ** 12)
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(want))
        self.assertEqual(data["pricing"]["catalog_revision"], cheaper.revision)
        # An alias edit alone (same catalog) is also a new identity.
        before = self.calls.n
        self.aliases = {**self.aliases, "gpt-6-luna": pd.Alias("litellm:azure_ai/gpt-6.1-sol", EQUIVALENT, NOTE)}
        data = self.usage(svc2, mode="machine", dimension="all")
        self.assertGreater(self.calls.n, before)
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(expected_pico("gpt-6.1-sol", 1000, 10)))
        self.assertNotEqual(alias_digest(aliases()), alias_digest(self.aliases))


class ReadOnlyTests(PricedBase):
    def test_state_db_bytes_are_unchanged_by_priced_requests(self):
        self.machine.write_db(None, seven_sessions())
        path = self.machine.home(None) / "state.db"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        stat = path.stat()
        for dimension in ("all", "profile", "model"):
            self.usage(self.service(), mode="machine", dimension=dimension)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir() if p.name.startswith("state.db")), ["state.db"])


class DtoAndSanitizerTests(unittest.TestCase):
    def test_catalog_key_is_wire_vocabulary_not_a_redacted_secret(self):
        self.assertFalse(is_sensitive_name("catalog_key"))
        self.assertTrue(is_sensitive_name("api_key"))  # the exemption is exact, not a loosening
        sample = {"catalog_key": "litellm:azure_ai/gpt-6-luna", "method": "alias", "alias_kind": "proxy",
                  "reason": "alias_target_missing", "source": "openrouter", "fetched_at": "2026-11-21T00:57:39.831243Z",
                  "amount_pico_usd": "1000000", "origin": "network", "warnings": ["pricing_stale"],
                  "catalog_revision": "a" * 64, "unknown_row_ids": ["row_" + "b" * 64]}
        for secret in ("litellm", "azure", "alias", "proxy", "stale", "network", "openrouter", "a", "row_", "1", "Z"):
            with self.subTest(secret=secret):
                self.assertEqual(sanitize(sample, {secret}), sample)

    def test_alias_note_is_scrubbed_like_any_free_text(self):
        out = sanitize({"note": "see https://user:pw@example.com/p?token=abc"}, set())
        self.assertNotIn("pw@", out["note"])
        self.assertNotIn("token=abc", out["note"])

    def test_api_equivalent_invariants_are_enforced(self):
        ApiEquivalent("5", 1, 0, 0, False, False)
        ApiEquivalent("0", 1, 1, 1, True, True)
        with self.assertRaises(DtoError):
            ApiEquivalent(None, 1, 0, 0, False, False)  # amount null but a priced row
        with self.assertRaises(DtoError):
            ApiEquivalent("5", 1, 1, 0, False, False)  # partial must be unpriced_rows > 0
        with self.assertRaises(DtoError):
            ApiEquivalent("05", 1, 0, 0, False, False)  # not a canonical integer string
        with self.assertRaises(DtoError):
            ApiEquivalent("5", 1, 0, 2, False, False)  # proxies are a subset of priced rows
        with self.assertRaises(DtoError):
            ApiEquivalent("5", True, 0, 0, False, False)

    def test_usd_must_be_the_float_image_of_the_integer_amount(self):
        base = dict(input_count=1, output_count=1, estimated_usd=0.0, sessions=1, api_calls=1, missing_fields=())
        UsageMetrics(**base, api_equivalent_usd=0.000001, api_equivalent=ApiEquivalent("1000000", 1, 0, 0, False, False))
        with self.assertRaises(DtoError):
            UsageMetrics(**base, api_equivalent_usd=0.5, api_equivalent=ApiEquivalent("1000000", 1, 0, 0, False, False))
        with self.assertRaises(DtoError):
            UsageMetrics(**base, api_equivalent_usd=0.0, api_equivalent=EMPTY_API_EQUIVALENT)  # 0 is not null
        with self.assertRaises(DtoError):
            UsageMetrics(**base, api_equivalent_usd=None,
                         api_equivalent=ApiEquivalent("0", 1, 0, 0, False, False))  # null is not 0

    def test_row_pricing_reason_is_null_exactly_when_priced(self):
        m = dict(input_count=1, output_count=1, estimated_usd=0.0, sessions=1, api_calls=1, missing_fields=())
        priced = UsageMetrics(**m, api_equivalent_usd=0.0, api_equivalent=ApiEquivalent("0", 1, 0, 0, False, False))
        common = dict(id="row_" + "0" * 64, day="2026-11-05", profile="alpha", is_global=False, source="state.db",
                      model_id="m", provider_id="p", attribution_source="unknown")
        ProvenanceRow(**common, metrics=priced,
                      pricing=RowPricing("litellm:k", "alias", "equivalent", "n", None, "litellm", None, False))
        with self.assertRaises(DtoError):
            ProvenanceRow(**common, metrics=priced,
                          pricing=RowPricing("litellm:k", "alias", "equivalent", "n", "tokens_missing", "litellm",
                                             None, False))
        with self.assertRaises(DtoError):
            ProvenanceRow(**common, metrics=UsageMetrics(**m), pricing=RowPricing(
                "litellm:k", "alias", "equivalent", "n", None, "litellm", None, False))
        with self.assertRaises(DtoError):
            RowPricing("not-a-qualified-key", "alias", None, None, None, None, None, False)

    def test_usage_validator_rejects_unknown_bucket_drift(self):
        # data.pricing.unknown_row_ids must be exactly the rows whose amount is null (fail closed otherwise)
        row_id = "row_" + "1" * 64
        metrics = UsageMetrics(1, 1, 0.0, 1, 1, ())
        row = dto.to_plain(ProvenanceRow(row_id, "2026-11-05", "alpha", False, "state.db", "m", "p", "unknown", metrics))
        group = dto.to_plain(dto.UsageGroup("all", "All", "all", None, ("p",), (dto.Holder("alpha", False),),
                                            (row_id,), metrics, False))
        series = dto.to_plain(dto.UsageTimeSeries(
            "all", None, None, "machine", "estimated", "session_analytics", dto.Period(None, None, "rolling_30d"), 30,
            "day", "server_local", "partial", (), "2026-11-06T12:00:00Z", False, None))
        data = {"window_days": 30, "mode": "machine", "dimension": "all", "provider_id": None, "series": [series],
                "groups": [group], "rows": [row], "totals": dto.to_plain(metrics), "providers": [], "sources": [],
                "complete": True, "top_n": None, "reason": None,
                "pricing": {"catalog_revision": "c" * 64, "origin": "seed", "warnings": [], "unknown_row_ids": [row_id]}}
        dto.usage_from_wire(data)  # the unpriced row is listed: consistent
        bad = copy.deepcopy(data)
        bad["pricing"]["unknown_row_ids"] = []
        with self.assertRaises(DtoError):
            dto.usage_from_wire(bad)


class RealResolverIntegrationTests(PricedBase):
    """The service's DEFAULT wiring (``PricingCatalogManager`` + packaged seed + curated aliases): no injected double.

    Expected amounts are recomputed here from the packaged seed's per-token rates with Decimal arithmetic.
    """

    CLOCK = lambda self: datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731 - seed is fresh then

    def seed_expected(self, model, tokens_in, tokens_out):
        from plugins.usage_deck import pricing

        entry = pricing.load_seed().entries[pricing.load_aliases()[model].catalog_key]
        total = Decimal(tokens_in) * entry.input_usd_per_token + Decimal(tokens_out) * entry.output_usd_per_token
        return int((total * 10 ** 12).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))

    def write(self, sessions):
        path = self.machine.home(None) / "state.db"
        if path.exists():
            path.unlink()
        self.machine.write_db(None, sessions)

    def test_default_wiring_prices_all_seven_inventory_models_and_conserves(self):
        self.write(seven_sessions())
        data = self.usage(self.service(pricing=False, clock=self.CLOCK), mode="machine", dimension="model", window=30)
        rows = {r["model_id"]: r for r in data["rows"]}
        self.assertEqual(sorted(rows), sorted(SEVEN))
        total = 0
        for i, model in enumerate(SEVEN):
            want = self.seed_expected(model, 1_000_000 + i, 200_000 + 3 * i)
            total += want
            self.assertEqual(rows[model]["metrics"]["api_equivalent"]["amount_pico_usd"], str(want), model)
            self.assertEqual(rows[model]["pricing"]["method"], "alias", model)
            self.assertIsNone(rows[model]["pricing"]["reason"], model)
        self.assertEqual(data["pricing"]["unknown_row_ids"], [])
        self.assertEqual(data["totals"]["api_equivalent"]["amount_pico_usd"], str(total))
        self.assertEqual(sum(int(g["totals"]["api_equivalent"]["amount_pico_usd"]) for g in data["groups"]), total)
        self.assertEqual(data["totals"]["api_equivalent"]["proxy_rows"], 4)
        self.assertIn(data["pricing"]["origin"], ("seed", "cache"))

    def test_default_wiring_unknown_model_is_null_and_listed(self):
        self.write(seven_sessions() + [S("sx", 9, "no-such-model-xyz", "custom:lane", None, 5000, 100, 0.0, 1, "custom:lane")])
        data = self.usage(self.service(pricing=False, clock=self.CLOCK), mode="machine", dimension="all", window=30)
        bad = [r for r in data["rows"] if r["model_id"] == "no-such-model-xyz"][0]
        self.assertIsNone(bad["metrics"]["api_equivalent"]["amount_pico_usd"])
        self.assertIsNone(bad["metrics"]["api_equivalent_usd"])
        self.assertEqual(data["pricing"]["unknown_row_ids"], [bad["id"]])
        self.assertTrue(data["totals"]["api_equivalent"]["partial"])


if __name__ == "__main__":
    unittest.main()
