"""API-equivalent pricing resolver, heuristics, catalog normalization, cache and refresh (design §6/§8).

Hermetic: injected clock/random/fetch/sleep, tmp data dirs, small synthetic seeds. Nothing here reads live
prices, touches the network or opens a ``state.db``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from plugins.usage_deck import pricing as p
from plugins.usage_deck.adapters.session_db import safe_model_id as session_safe_model_id

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
INVENTORY = (
    "gpt-6-luna", "gpt-6.1-sol", "claude-sonnet-5-5", "qwen3.8-flash-next", "qwen3.6:35b",
    "cyber-tiel-coder:35b", "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k",
)
KINDS = {
    "gpt-6-luna": "equivalent", "gpt-6.1-sol": "equivalent", "claude-sonnet-5-5": "equivalent",
    "qwen3.8-flash-next": "proxy", "qwen3.6:35b": "proxy", "cyber-tiel-coder:35b": "proxy",
    "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k": "proxy",
}
# Added after the live host check (t_5561a3ef): (recorded id, seeded twin, kind). Not part of the design's seven.
FOLLOWUP = (
    ("claude-sonnet-5.5", "claude-sonnet-5-5", "equivalent"),
    ("cybertiel-q5", "cyber-tiel-coder:35b", "proxy"),
)
TARGETS = {
    "gpt-6-luna": "litellm:azure_ai/gpt-6-luna",
    "gpt-6.1-sol": "litellm:azure_ai/gpt-6.1-sol",
    "claude-sonnet-5-5": "litellm:claude-sonnet-5-5",
    "qwen3.8-flash-next": "litellm:dashscope/qwen3.8-flash",
    "qwen3.6:35b": "litellm:aihubmix/qwen3.6-35b-a3b",
    "cyber-tiel-coder:35b": "litellm:aihubmix/qwen3.6-35b-a3b",
    "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k": "litellm:aiand/qwen/qwen3.8-27b",
}


# ------------------------------------------------------------------ builders


def entry(qualified: str, rin: str = "0.000001", rout: str = "0.000002", at: datetime = T0) -> p.CatalogEntry:
    source, key = qualified.split(":", 1)
    return p.CatalogEntry(source, key, Decimal(rin), Decimal(rout), at)


def catalog(*entries: p.CatalogEntry) -> p.CatalogSnapshot:
    mapping = {e.key: e for e in entries}
    return p.CatalogSnapshot(mapping, p.compute_revision(mapping), "seed", ())


def alias(key: str, kind: str = "equivalent", note: str = "n") -> p.Alias:
    return p.Alias(key, kind, note)  # type: ignore[arg-type]


def resolve(model, i=1_000_000, o=1_000_000, *, cat, aliases=None, now=T0):
    return p.resolve_api_equivalent(model, i, o, catalog=cat, aliases=aliases or {}, now=now)


def litellm_payload(models: dict) -> bytes:
    body = {"sample_spec": {"mode": "chat", "input_cost_per_token": 0, "output_cost_per_token": 0, "note": "ignored"}}
    body.pop("sample_spec")
    for key, val in models.items():
        body[key] = val if isinstance(val, dict) else {"mode": "chat", "input_cost_per_token": val[0],
                                                       "output_cost_per_token": val[1]}
    return json.dumps(body).encode()


def openrouter_payload(models: dict) -> bytes:
    data = []
    for key, val in models.items():
        if isinstance(val, dict):
            data.append(dict(id=key, **val))
        else:
            data.append({"id": key, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                         "pricing": {"prompt": val[0], "completion": val[1]}})
    return json.dumps({"data": data}).encode()


def write_seed(path: Path, entries: dict, *, fetched_at: datetime = T0 - timedelta(hours=30),
               sources=(p.PRIMARY, p.SECONDARY)) -> Path:
    metas = {s: p.SourceMeta(fetched_at, fetched_at + timedelta(hours=24), "a" * 64) for s in sources}
    ents = {k: entry(k, *v, at=fetched_at) for k, v in entries.items()}
    path.write_bytes(p._serialize_envelope(metas, ents))
    return path


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class Fetcher:
    """url -> bytes | Exception | callable; records every call."""

    def __init__(self, **by_source) -> None:
        self.by_url = {p.SOURCE_URLS[s]: v for s, v in by_source.items()}
        self.calls = []

    def set(self, source, value) -> None:
        self.by_url[p.SOURCE_URLS[source]] = value

    def __call__(self, url, timeout_s, max_bytes):
        self.calls.append((url, timeout_s, max_bytes))
        value = self.by_url.get(url, OSError("offline"))
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    def count(self, source) -> int:
        return sum(1 for c in self.calls if c[0] == p.SOURCE_URLS[source])


def uniform_lo(a, b):
    return a


def uniform_hi(a, b):
    return b


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "plugin-data" / "usage-deck"

    def manager(self, fetch=None, *, clock=None, uniform=uniform_lo, seed=None, sleeps=None, **kw):
        return p.PricingCatalogManager(
            self.data_dir, clock=clock or Clock(), random_uniform=uniform, fetch=fetch or Fetcher(),
            sleep=(sleeps.append if sleeps is not None else (lambda s: None)), background=False,
            seed_path=seed, **kw)

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "pricing" / "catalog-v1.json"


# ------------------------------------------------------------------ packaged data: seed + aliases


class PackagedDataTests(unittest.TestCase):
    def test_seed_loads_and_is_a_real_two_source_snapshot(self):
        sources, entries = p.load_seed_envelope()
        self.assertEqual(set(sources), {"litellm", "openrouter"})
        self.assertGreater(sum(1 for e in entries.values() if e.source == "litellm"), 1000)
        self.assertGreater(sum(1 for e in entries.values() if e.source == "openrouter"), 100)
        snap = p.load_seed()
        self.assertEqual(snap.origin, "seed")
        self.assertEqual(snap.revision, p.compute_revision(snap.entries))

    def test_seed_age_is_original_retrieval_time_not_build_time(self):
        sources, entries = p.load_seed_envelope()
        for meta in sources.values():
            self.assertLess(meta.fetched_at, datetime.now(timezone.utc))
        self.assertTrue(all(e.fetched_at <= max(m.fetched_at for m in sources.values()) for e in entries.values()))

    def test_every_alias_target_is_seeded_with_valid_rates(self):
        aliases = p.load_aliases_strict()
        _, entries = p.load_seed_envelope()
        self.assertEqual(set(INVENTORY) - set(aliases), set())
        for model_id, a in aliases.items():
            self.assertIn(a.catalog_key, entries, model_id)
            e = entries[a.catalog_key]
            self.assertGreaterEqual(e.input_usd_per_token, 0)
            self.assertGreaterEqual(e.output_usd_per_token, 0)

    def test_alias_table_matches_the_ratified_design(self):
        aliases = p.load_aliases_strict()
        self.assertEqual({k: a.catalog_key for k, a in aliases.items() if k in INVENTORY}, TARGETS)
        self.assertEqual({k: a.kind for k, a in aliases.items() if k in INVENTORY}, KINDS)
        self.assertTrue(all(a.note.strip() for a in aliases.values()))

    def test_loaders_are_package_relative_not_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(set(INVENTORY) - set(p.load_aliases()), set())
                self.assertTrue(p.load_seed().entries)
            finally:
                os.chdir(cwd)

    def test_alias_digest_is_the_file_sha256(self):
        raw = (p.PACKAGE_DATA / "aliases.json").read_bytes()
        self.assertEqual(p.aliases_digest(), hashlib.sha256(raw).hexdigest())


class AliasFileValidationTests(TmpCase):
    def load(self, text: str):
        f = self.tmp / "aliases.json"
        f.write_text(text)
        return f

    def good(self, **over):
        rec = {"catalog_key": "litellm:a/b", "kind": "equivalent", "note": "ok"}
        rec.update(over)
        return json.dumps({"schema_version": 1, "aliases": {"m": rec}})

    def test_valid(self):
        self.assertEqual(p.load_aliases_strict(self.load(self.good()))["m"].catalog_key, "litellm:a/b")

    def test_invalid_variants_give_empty_map_and_manager_warning(self):
        bad = [
            self.good(kind="exact"), self.good(note=""), self.good(note="x" * 400), self.good(catalog_key="a/b"),
            self.good(catalog_key="other:x"), self.good(catalog_key="litellm:"),
            '{"schema_version": 2, "aliases": {}}', '{"schema_version": 1}', "not json", "[]",
            '{"schema_version": 1, "aliases": {"m": {"catalog_key": "litellm:a", "kind": "proxy", "note": "n"},'
            ' "m": {"catalog_key": "litellm:a", "kind": "proxy", "note": "n"}}}',  # duplicate key
            '{"schema_version": 1, "aliases": {"m": {"catalog_key": "litellm:a", "kind": "proxy", "note": "n",'
            ' "extra": 1}}}',
        ]
        for text in bad:
            f = self.load(text)
            with self.assertRaises(p.PricingDataError, msg=text):
                p.load_aliases_strict(f)
            self.assertEqual(dict(p.load_aliases(f)), {})
        seed = write_seed(self.tmp / "seed.json", {"litellm:a/b": ("1", "2")})
        mgr = self.manager(seed=seed, aliases_path=self.load(bad[0]))
        self.assertIn(p.WARN_ALIASES_INVALID, mgr.snapshot().warnings)
        self.assertEqual(dict(mgr.aliases()), {})

    def test_unsafe_alias_model_id_rejected(self):
        f = self.load('{"schema_version": 1, "aliases": {"has space": {"catalog_key": "litellm:a", '
                      '"kind": "proxy", "note": "n"}}}')
        with self.assertRaises(p.PricingDataError):
            p.load_aliases_strict(f)


# ------------------------------------------------------------------ the seven inventory models


class InventoryResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = p.load_seed()
        cls.aliases = p.load_aliases()

    def test_each_of_the_seven_resolves_by_alias_to_its_seeded_target(self):
        for model_id in INVENTORY:
            with self.subTest(model_id):
                r = resolve(model_id, 1_234_567, 89_012, cat=self.cat, aliases=self.aliases, now=T0)
                e = self.cat.entries[TARGETS[model_id]]
                want = (Decimal(1_234_567) * e.input_usd_per_token + Decimal(89_012) * e.output_usd_per_token)
                want_pico = int(want.quantize(Decimal("1E-12")).scaleb(12))
                self.assertEqual(r.method, "alias")
                self.assertEqual(r.catalog_key, TARGETS[model_id])
                self.assertEqual(r.alias_kind, KINDS[model_id])
                self.assertEqual(r.source, "litellm")
                self.assertIsNone(r.reason)
                self.assertEqual(r.amount_pico_usd, want_pico)
                self.assertEqual(r.api_equivalent_usd, float(Decimal(want_pico).scaleb(-12)))
                self.assertGreater(r.amount_pico_usd, 0)
                self.assertTrue(r.note)
                self.assertEqual(r.fetched_at, e.fetched_at)

    def test_custom_names_are_visible_proxies_and_cyber_tiel_is_not_inferred_identity(self):
        for model_id in INVENTORY:
            r = resolve(model_id, cat=self.cat, aliases=self.aliases)
            self.assertEqual(r.alias_kind == "proxy", KINDS[model_id] == "proxy")
        cyber = resolve("cyber-tiel-coder:35b", cat=self.cat, aliases=self.aliases)
        self.assertEqual(cyber.alias_kind, "proxy")
        self.assertIn("not a verified underlying model", cyber.note)
        self.assertEqual(cyber.catalog_key, resolve("qwen3.6:35b", cat=self.cat, aliases=self.aliases).catalog_key)

    def test_without_aliases_exact_keys_are_heuristic_and_custom_names_never_get_a_fabricated_price(self):
        for model_id in ("gpt-6-luna", "gpt-6.1-sol", "claude-sonnet-5-5"):
            r = resolve(model_id, cat=self.cat, aliases={})
            self.assertEqual(r.method, "heuristic", model_id)
            self.assertIsNotNone(r.amount_pico_usd)
        for model_id in ("qwen3.8-flash-next", "qwen3.6:35b", "cyber-tiel-coder:35b",
                         "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k"):
            r = resolve(model_id, cat=self.cat, aliases={})
            self.assertEqual(r.method, "none", model_id)
            self.assertIsNone(r.api_equivalent_usd)
            self.assertIn(r.reason, ("no_catalog_match", "ambiguous_model"))

    def test_followup_spellings_alias_to_the_same_targets_with_visible_method_and_key(self):
        # Ids stay verbatim (brief decision 1): each recorded spelling carries its own alias line.
        for model_id, twin, kind in FOLLOWUP:
            with self.subTest(model_id):
                aliases = p.load_aliases_strict()
                self.assertEqual(aliases[model_id].catalog_key, aliases[twin].catalog_key)
                self.assertEqual(aliases[model_id].kind, kind)
                r = resolve(model_id, 1_234_567, 89_012, cat=self.cat, aliases=self.aliases, now=T0)
                t = resolve(twin, 1_234_567, 89_012, cat=self.cat, aliases=self.aliases, now=T0)
                self.assertEqual((r.method, r.catalog_key, r.alias_kind), ("alias", TARGETS[twin], kind))
                self.assertEqual(r.amount_pico_usd, t.amount_pico_usd)
                self.assertGreater(r.amount_pico_usd, 0)
                self.assertIsNone(r.reason)
                self.assertTrue(r.note)

    def test_cybertiel_q5_is_a_visible_proxy_that_discloses_the_quantized_variant(self):
        r = resolve("cybertiel-q5", cat=self.cat, aliases=self.aliases)
        self.assertEqual(r.alias_kind, "proxy")
        self.assertIn("Quantized", r.note)
        self.assertIn("no catalog identity", r.note)
        self.assertEqual(r.catalog_key, "litellm:aihubmix/qwen3.6-35b-a3b")

    def test_followup_ids_without_an_alias_never_get_a_fabricated_price(self):
        r = resolve("cybertiel-q5", cat=self.cat, aliases={})
        self.assertEqual(r.method, "none")
        self.assertIsNone(r.api_equivalent_usd)
        self.assertIn(r.reason, ("no_catalog_match", "ambiguous_model"))
        # near-miss spellings are not silently folded onto the alias
        for near in ("cybertiel-q4", "cybertiel", "claude-sonnet-5.50"):
            self.assertNotEqual(resolve(near, cat=self.cat, aliases=self.aliases).method, "alias", near)

    def test_hauhau_reduces_to_27b_and_gets_no_fictitious_36_match(self):
        self.assertEqual(p.query_tokens("qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k"),
                         ("qwen3.8", "hauhau", "aggressive", "27b"))
        r = resolve("qwen3.6:35b", cat=self.cat, aliases={})
        self.assertIsNone(r.catalog_key)

    def test_an_alias_cannot_be_overridden_by_a_heuristic_exact_key(self):
        cat = catalog(entry("litellm:gpt-6-luna", "1", "1"), entry("litellm:azure_ai/gpt-6-luna", "5", "5"))
        r = resolve("gpt-6-luna", 1, 0, cat=cat, aliases={"gpt-6-luna": alias("litellm:azure_ai/gpt-6-luna")})
        self.assertEqual((r.method, r.catalog_key, r.amount_pico_usd), ("alias", "litellm:azure_ai/gpt-6-luna", 5 * 10**12))


# ------------------------------------------------------------------ null vs zero, tokens, arithmetic


class NullVersusZeroTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalog(entry("litellm:m1", "0.000001", "0.000003"), entry("litellm:free", "0", "0"))

    def test_unknown_model_is_null_never_zero_even_with_zero_tokens(self):
        for tokens in ((0, 0), (100, 100), (None, None)):
            r = resolve("nonexistent", *tokens, cat=self.cat)
            self.assertIsNone(r.api_equivalent_usd)
            self.assertIsNone(r.amount_pico_usd)
            self.assertEqual((r.method, r.reason), ("none", "no_catalog_match" if tokens[0] is not None else "no_catalog_match"))

    def test_priced_model_with_zero_tokens_is_a_real_zero(self):
        r = resolve("m1", 0, 0, cat=self.cat)
        self.assertEqual((r.api_equivalent_usd, r.amount_pico_usd, r.reason), (0.0, 0, None))
        self.assertIsNotNone(r.api_equivalent_usd)
        self.assertIsNot(r.api_equivalent_usd, None)

    def test_free_rate_is_a_genuine_zero_distinct_from_unknown(self):
        free = resolve("free", 5_000, 5_000, cat=self.cat)
        unknown = resolve("nope", 5_000, 5_000, cat=self.cat)
        self.assertEqual(free.api_equivalent_usd, 0.0)
        self.assertIsNone(unknown.api_equivalent_usd)

    def test_missing_tokens_keep_identity_metadata_but_no_amount(self):
        for i, o in ((None, 5), (5, None), (None, None)):
            r = resolve("m1", i, o, cat=self.cat)
            self.assertEqual(r.reason, "tokens_missing")
            self.assertEqual((r.method, r.catalog_key), ("heuristic", "litellm:m1"))
            self.assertIsNone(r.api_equivalent_usd)
            self.assertIsNone(r.amount_pico_usd)

    def test_invalid_tokens(self):
        for i, o in ((True, 1), (1, False), (-1, 1), (1, -5), (1.5, 1), ("3", 1), (1, None.__class__)):
            r = resolve("m1", i, o, cat=self.cat)  # type: ignore[arg-type]
            self.assertEqual(r.reason, "tokens_invalid", (i, o))
            self.assertIsNone(r.amount_pico_usd)
        self.assertEqual(resolve("m1", -1, None, cat=self.cat).reason, "tokens_invalid")

    def test_unsafe_or_absent_model_ids_are_model_unknown_without_echo(self):
        for bad in (None, "", 7, "has space", 'q"uote', "back\\slash", "x" * 129, "sk-ant-api03-" + "a" * 40):
            r = resolve(bad, 1, 1, cat=self.cat)  # type: ignore[arg-type]
            self.assertEqual((r.reason, r.method, r.api_equivalent_usd), ("model_unknown", "none", None), bad)
            self.assertIsNone(r.catalog_key)
            self.assertNotIn("sk-ant", repr(r))

    def test_safe_model_id_matches_the_session_db_rule(self):
        for sample in ("gpt-6-luna", "qwen3.6:35b", "a/b@c", "has space", "", None, 3, "sk-ant-api03-" + "b" * 40,
                       'q"', "x" * 128, "x" * 129, "ünï"):
            self.assertEqual(p.safe_model_id(sample), session_safe_model_id(sample), repr(sample))

    def test_missing_alias_target_is_unknown_and_never_falls_through(self):
        cat = catalog(entry("litellm:gpt-x", "1", "1"))
        r = resolve("gpt-x", 1, 1, cat=cat, aliases={"gpt-x": alias("litellm:azure_ai/gpt-x")})
        self.assertEqual((r.reason, r.method, r.api_equivalent_usd), ("alias_target_missing", "none", None))
        self.assertIsNone(r.catalog_key)

    def test_arithmetic_quantizes_once_half_even_at_the_leaf(self):
        # 1 token at 0.5e-12 USD: exactly half a pico -> rounds to even (0), 3 tokens -> 1.5 -> 2
        cat = catalog(entry("litellm:tiny", "0.0000000000005", "0"))
        self.assertEqual(resolve("tiny", 1, 0, cat=cat).amount_pico_usd, 0)
        self.assertEqual(resolve("tiny", 3, 0, cat=cat).amount_pico_usd, 2)
        self.assertEqual(resolve("tiny", 5, 0, cat=cat).amount_pico_usd, 2)
        self.assertEqual(resolve("tiny", 3, 0, cat=cat).api_equivalent_usd, 2e-12)

    def test_large_counts_are_exact_integers(self):
        cat = catalog(entry("litellm:big", "0.000002", "0.00001"))
        r = resolve("big", 10**12, 10**12, cat=cat)
        self.assertEqual(r.amount_pico_usd, 12 * 10**18)
        self.assertIsInstance(r.amount_pico_usd, int)

    def test_stale_is_flag_only_never_nulls_the_amount(self):
        old = T0 - timedelta(days=8)
        cat = catalog(entry("litellm:m", "0.000001", "0.000001", at=old))
        r = resolve("m", 1000, 0, cat=cat, now=T0)
        self.assertTrue(r.stale)
        self.assertEqual(r.amount_pico_usd, 1000 * 10**6)
        edge = resolve("m", 1, 0, cat=cat, now=old + timedelta(days=7))
        self.assertFalse(edge.stale)  # age must EXCEED 7 days
        self.assertTrue(resolve("m", 1, 0, cat=cat, now=old + timedelta(days=7, seconds=1)).stale)

    def test_resolver_is_pure_and_never_raises_for_unpriced_cases(self):
        before = dict(self.cat.entries)
        for model in (None, "x", "a:b:c", "//", "-", ":35b", "q-q4", "iq3", "a" * 128):
            resolve(model, 1, 1, cat=self.cat)
        self.assertEqual(dict(self.cat.entries), before)


# ------------------------------------------------------------------ heuristic pipeline on synthetic names


class HeuristicTests(unittest.TestCase):
    def test_every_quant_tag_iq2_to_iq8_and_q2_to_q8_is_stripped(self):
        cat = catalog(entry("litellm:vendor/foo-9b", "1", "1"))
        for n in range(2, 9):
            for suffix in (f"iq{n}", f"q{n}"):
                r = resolve(f"foo-9b-{suffix}", 1, 0, cat=cat)
                self.assertEqual(r.catalog_key, "litellm:vendor/foo-9b", suffix)
                self.assertEqual(r.method, "heuristic")
        for n in (0, 1, 9):
            self.assertIsNone(resolve(f"foo-9b-q{n}", 1, 0, cat=cat).catalog_key)
            self.assertIsNone(resolve(f"foo-9b-iq{n}", 1, 0, cat=cat).catalog_key)

    def test_quant_modifier_chains_and_underscores(self):
        for name in ("foo-9b-q4_k_m", "foo-9b-q4-k-s", "foo-9b-Q5_K_L", "foo_9b_q6_k", "foo-9b-q3-k-m-q4"):
            self.assertEqual(p.query_tokens(name)[:2], ("foo", "9b"), name)
            self.assertEqual(p.query_tokens(name), ("foo", "9b"), name)
        # only k/s/m/l modifiers are accepted; arbitrary words are never consumed
        self.assertEqual(p.query_tokens("foo-9b-q4-xl"), ("foo", "9b", "q4", "xl"))
        self.assertEqual(p.query_tokens("foo-9b-q4-k-extra"), ("foo", "9b", "q4", "k", "extra"))

    def test_chained_hardware_suffixes_strip_until_stable(self):
        self.assertEqual(p.query_tokens("foo-9b-iq3-gpu-96k-b8k"), ("foo", "9b"))
        self.assertEqual(p.query_tokens("foo-9b-b8k-gpu-96k-iq3"), ("foo", "9b"))
        self.assertEqual(p.query_tokens("foo-9b-q4-gpu-24k"), ("foo", "9b"))
        self.assertEqual(p.query_tokens("foo-9b-gpu-96k-b8k-q4"), ("foo", "9b"))

    def test_interior_and_size_tokens_are_never_removed(self):
        self.assertEqual(p.query_tokens("foo-q4-chat-9b"), ("foo", "q4", "chat", "9b"))
        self.assertEqual(p.query_tokens("foo-35b-a3b"), ("foo", "35b", "a3b"))
        self.assertEqual(p.query_tokens("foo-27b-iq3"), ("foo", "27b"))
        self.assertEqual(p.query_tokens("foo-6.1-sol"), ("foo", "6.1", "sol"))

    def test_colon_size_is_preserved_other_colons_are_not_stripped(self):
        self.assertEqual(p.query_tokens("qwen3.6:35b"), ("qwen3.6", "35b"))
        self.assertEqual(p.query_tokens("llama3.1:8B"), ("llama3.1", "8b"))
        self.assertEqual(p.query_tokens("foo:free"), ("foo:free",))
        self.assertEqual(p.query_tokens("foo:7b-q4_k_m"), ("foo", "7b"))  # size becomes terminal after the quant strip
        cat = catalog(entry("openrouter:vendor/foo", "0", "0"), entry("openrouter:vendor/foo:free", "9", "9"))
        self.assertEqual(resolve("foo:free", 1, 0, cat=cat).catalog_key, "openrouter:vendor/foo:free")
        # `:free` can never map onto the paid/base model automatically
        self.assertIsNone(resolve("bar:free", 1, 0, cat=catalog(entry("openrouter:v/bar", "1", "1"))).catalog_key)

    def test_synthetic_hauhau_style_name_matches_single_candidate_as_heuristic_success(self):
        cat = catalog(entry("litellm:aiand/qwen/qwen3.8-27b", "0.0000004", "0.000003"))
        r = resolve("qwen3.8-27b-iq3-gpu-96k-b8k", 1_000_000, 0, cat=cat)
        self.assertEqual((r.method, r.catalog_key, r.alias_kind), ("heuristic", "litellm:aiand/qwen/qwen3.8-27b", None))
        self.assertEqual(r.amount_pico_usd, 400_000_000_000)

    def test_colon_size_query_matches_dash_size_catalog_key(self):
        cat = catalog(entry("litellm:ollama/qwen3.6-35b", "1", "1"))
        self.assertEqual(resolve("qwen3.6:35b", 1, 0, cat=cat).catalog_key, "litellm:ollama/qwen3.6-35b")

    def test_variant_version_size_and_date_mismatches_are_rejected(self):
        cat = catalog(entry("litellm:aprov/qwen3.8-27b", "1", "1"), entry("litellm:aprov/model-a3b-35b", "1", "1"),
                      entry("litellm:aprov/dated-1-20260101", "1", "1"), entry("litellm:aprov/plain-1", "1", "1"))
        for model in ("qwen3.6-27b", "qwen3.8-35b", "qwen3.8-27b-instruct", "qwen3.8", "qwen3.8-a3b-27b",
                      "model-35b", "model-a3b", "plain-1-20260101", "dated-1", "dated-1-20260102", "dated-1-0101"):
            self.assertIsNone(resolve(model, 1, 0, cat=cat).catalog_key, model)
        self.assertEqual(resolve("qwen3.8-27b", 1, 0, cat=cat).catalog_key, "litellm:aprov/qwen3.8-27b")

    def test_no_cross_version_fallback_qwen38_to_qwen36(self):
        cat = catalog(entry("litellm:aihubmix/qwen3.6-35b-a3b", "1", "1"), entry("litellm:aprov/qwen3.6-27b", "1", "1"))
        for model in ("qwen3.8-27b", "qwen3.8-35b-a3b", "qwen3.7-27b"):
            self.assertEqual(resolve(model, 1, 0, cat=cat).reason, "no_catalog_match", model)

    def test_no_substring_or_edit_distance_matching(self):
        cat = catalog(entry("litellm:aprov/gpt-6-luna", "1", "1"))
        for model in ("gpt-6-lun", "gpt-6-luna-pro", "xgpt-6-luna", "gpt-6-luna2", "luna"):
            self.assertIsNone(resolve(model, 1, 0, cat=cat).catalog_key, model)

    def test_dates_normalize_and_stay_distinct(self):
        cat = catalog(entry("litellm:aprov/m-1-2026-03-04", "1", "1"), entry("litellm:aprov/n-1-0315", "1", "1"))
        self.assertEqual(resolve("m-1-20260304", 1, 0, cat=cat).catalog_key, "litellm:aprov/m-1-2026-03-04")
        self.assertEqual(resolve("m-1-2026-03-04", 1, 0, cat=cat).catalog_key, "litellm:aprov/m-1-2026-03-04")
        self.assertEqual(resolve("n-1-0315", 1, 0, cat=cat).catalog_key, "litellm:aprov/n-1-0315")
        self.assertIsNone(resolve("n-1-20260315", 1, 0, cat=cat).catalog_key)  # MMDD is not a YYYYMMDD
        self.assertIsNone(resolve("m-1-20260305", 1, 0, cat=cat).catalog_key)
        self.assertIsNone(resolve("m-1", 1, 0, cat=cat).catalog_key)  # undated query -> undated models only
        self.assertEqual(p.coalesce_dates(("a", "2026", "13", "04")), ("a", "2026", "13", "04"))

    def test_exact_case_sensitive_upstream_key_is_still_method_heuristic(self):
        cat = catalog(entry("litellm:Weird/Key-1", "1", "1"))
        r = resolve("Weird/Key-1", 1, 0, cat=cat)
        self.assertEqual((r.method, r.catalog_key), ("heuristic", "litellm:Weird/Key-1"))
        self.assertIsNone(p.safe_model_id("Weird Key"))

    def test_provider_namespaces_and_vendor_duplicates_compare_equal(self):
        cat = catalog(entry("litellm:aiand/qwen/qwen3.8-27b", "1", "1"))
        for model in ("qwen3.8-27b", "qwen/qwen3.8-27b"):
            self.assertEqual(resolve(model, 1, 0, cat=cat).catalog_key, "litellm:aiand/qwen/qwen3.8-27b", model)
        cat2 = catalog(entry("litellm:azure/gpt-7", "1", "1"), entry("litellm:vertex_ai/claude-x-1", "1", "1"))
        self.assertTrue(resolve("openai/gpt-7", 1, 0, cat=cat2).catalog_key)
        self.assertTrue(resolve("anthropic/claude-x-1", 1, 0, cat=cat2).catalog_key)
        self.assertEqual(p.catalog_tokens("litellm", "gpt-7"), ("gpt", "7"))  # bare keys: no namespace removal
        self.assertEqual(p.catalog_tokens("openrouter", "meta-llama/llama-3-8b"), ("llama", "3", "8b"))

    def test_same_suffix_regional_variants_are_never_conflated(self):
        cat = catalog(entry("litellm:eu/foo-1", "1", "1"), entry("litellm:us/foo-1", "2", "2"))
        self.assertEqual(resolve("foo-1", 1, 0, cat=cat).reason, "ambiguous_model")
        self.assertIsNone(resolve("foo-1", 1, 0, cat=cat).amount_pico_usd)

    def test_provider_rate_ambiguity_returns_unknown_not_a_lexical_guess(self):
        cat = catalog(entry("litellm:a/model-9b", "1", "1"), entry("litellm:b/model-9b", "1", "2"))
        r = resolve("model-9b", 1, 1, cat=cat)
        self.assertEqual((r.reason, r.method, r.api_equivalent_usd), ("ambiguous_model", "none", None))

    def test_identical_rate_variants_tie_break_lexicographically(self):
        cat = catalog(entry("litellm:z/model-9b", "1", "2"), entry("litellm:a/model-9b", "1", "2"),
                      entry("litellm:m/model-9b", "1", "2"))
        self.assertEqual(resolve("model-9b", 1, 0, cat=cat).catalog_key, "litellm:a/model-9b")
        # same input but different output rate is still ambiguous
        cat = catalog(entry("litellm:a/model-9b", "1", "2"), entry("litellm:b/model-9b", "1", "3"))
        self.assertEqual(resolve("model-9b", 1, 0, cat=cat).reason, "ambiguous_model")

    def test_primary_wins_over_secondary_even_on_rate_mismatch(self):
        cat = catalog(entry("openrouter:vendor/model-9b", "9", "9"), entry("litellm:prov/model-9b", "1", "1"))
        r = resolve("model-9b", 1, 0, cat=cat)
        self.assertEqual(r.catalog_key, "litellm:prov/model-9b")
        self.assertEqual(r.source, "litellm")

    def test_secondary_only_when_primary_has_no_candidate(self):
        cat = catalog(entry("openrouter:vendor/model-9b", "3", "4"))
        r = resolve("model-9b", 1, 0, cat=cat)
        self.assertEqual((r.source, r.catalog_key), ("openrouter", "openrouter:vendor/model-9b"))

    def test_ambiguous_primary_never_falls_back_to_secondary(self):
        cat = catalog(entry("litellm:a/model-9b", "1", "1"), entry("litellm:b/model-9b", "2", "2"),
                      entry("openrouter:vendor/model-9b", "3", "3"))
        self.assertEqual(resolve("model-9b", 1, 0, cat=cat).reason, "ambiguous_model")

    def test_alias_may_pin_openrouter_and_overrides_source_preference(self):
        cat = catalog(entry("openrouter:vendor/model-9b", "3", "4"), entry("litellm:prov/model-9b", "1", "1"))
        r = resolve("model-9b", 1, 0, cat=cat, aliases={"model-9b": alias("openrouter:vendor/model-9b", "proxy")})
        self.assertEqual((r.method, r.source, r.alias_kind), ("alias", "openrouter", "proxy"))

    def test_free_zero_rate_entry_prices_as_zero_not_unknown(self):
        cat = catalog(entry("openrouter:vendor/model:free", "0", "0"))
        r = resolve("model:free", 100, 100, cat=cat)
        self.assertEqual((r.amount_pico_usd, r.api_equivalent_usd, r.reason), (0, 0.0, None))

    def test_empty_normalized_tokens_do_not_match_everything(self):
        cat = catalog(entry("litellm:p/x", "1", "1"))
        for model in ("-", "/", "---", "--/--"):
            self.assertIsNone(resolve(model, 1, 0, cat=cat).catalog_key, model)


# ------------------------------------------------------------------ upstream normalization


class NormalizeTests(unittest.TestCase):
    def test_litellm_keeps_only_chat_completion_with_valid_rates(self):
        payload = litellm_payload({
            "ok-chat": (1e-06, 2e-06),
            "ok-zero": (0, 0),
            "completion": {"mode": "completion", "input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
            "embedding": {"mode": "embedding", "input_cost_per_token": 1e-06, "output_cost_per_token": 0},
            "responses": {"mode": "responses", "input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
            "nomode": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
            "neg": (-1, 1e-06), "neg_out": (1e-06, -1e-06), "nullrate": (None, 1e-06),
            "missing_out": {"mode": "chat", "input_cost_per_token": 1e-06},
            "bool": (True, 1e-06), "string": ("0.1", "0.1"),
            "huge": (1e30, 1e-06),
        })
        entries, skipped = p.normalize_litellm(payload, T0)
        self.assertEqual(sorted(entries), ["litellm:completion", "litellm:ok-chat", "litellm:ok-zero"])
        self.assertEqual(skipped, 10)
        self.assertEqual(entries["litellm:ok-zero"].input_usd_per_token, 0)
        self.assertEqual(entries["litellm:ok-chat"].source_key, "ok-chat")

    def test_litellm_nan_and_infinity_constants_are_a_parse_failure(self):
        for body in (b'{"m": {"mode": "chat", "input_cost_per_token": NaN, "output_cost_per_token": 1}}',
                     b'{"m": {"mode": "chat", "input_cost_per_token": Infinity, "output_cost_per_token": 1}}'):
            with self.assertRaises(p.SourceError):
                p.normalize_litellm(body, T0)

    def test_precision_is_retained_as_exact_decimal_strings(self):
        entries, _ = p.normalize_litellm(b'{"m": {"mode": "chat", "input_cost_per_token": 2.54e-07, '
                                         b'"output_cost_per_token": 1.524e-06}}', T0)
        e = entries["litellm:m"]
        self.assertEqual(e.input_usd_per_token, Decimal("0.000000254"))
        self.assertEqual(p._entry_wire(e)["output_usd_per_token"], "0.000001524")

    def test_openrouter_requires_text_modalities_and_valid_rates(self):
        text = {"input_modalities": ["text", "image"], "output_modalities": ["text"]}
        payload = openrouter_payload({
            "v/ok": ("0.000001", "0.000002"),
            "v/free": ("0", "0"),
            "v/img-out": {"architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                          "pricing": {"prompt": "1", "completion": "1"}},
            "v/no-arch": {"pricing": {"prompt": "1", "completion": "1"}},
            "v/router": ("-1", "-1"),
            "v/neg": ("-0.1", "1"),
            "v/nan": ("NaN", "1"),
            "v/inf": ("Infinity", "1"),
            "v/blank": ("", "1"),
            "v/under": ("1_0", "1"),
            "v/missing": {"architecture": text, "pricing": {"prompt": "1"}},
            "v/num-ok": {"architecture": text, "pricing": {"prompt": 0.000001, "completion": 0.000002}},
            "v/bool": {"architecture": text, "pricing": {"prompt": True, "completion": "1"}},
        })
        entries, skipped = p.normalize_openrouter(payload, T0)
        self.assertEqual(sorted(entries), ["openrouter:v/free", "openrouter:v/num-ok", "openrouter:v/ok"])
        self.assertEqual(skipped, 10)
        self.assertEqual(entries["openrouter:v/ok"].input_usd_per_token, Decimal("0.000001"))

    def test_unparseable_empty_or_wrong_shape_payloads_are_source_failures(self):
        for fn, bad in ((p.normalize_litellm, b"nope"), (p.normalize_litellm, b"[]"), (p.normalize_litellm, b"{}"),
                        (p.normalize_litellm, b'{"a": {"mode": "embedding"}}'),
                        (p.normalize_litellm, b'{"a": 1, "a": 2}'),
                        (p.normalize_openrouter, b"{}"), (p.normalize_openrouter, b'{"data": []}'),
                        (p.normalize_openrouter, b'{"data": "x"}'), (p.normalize_openrouter, b"\xff\xfe")):
            with self.assertRaises(p.SourceError, msg=bad):
                fn(bad, T0)

    def test_keys_are_source_qualified_and_only_the_first_colon_separates(self):
        entries, _ = p.normalize_litellm(litellm_payload({"bedrock/x:0": (1e-06, 1e-06)}), T0)
        self.assertEqual(list(entries), ["litellm:bedrock/x:0"])
        self.assertEqual(p._split_qualified("litellm:bedrock/x:0"), ("litellm", "bedrock/x:0"))
        with self.assertRaises(p.PricingDataError):
            p._split_qualified("nosource")
        with self.assertRaises(p.PricingDataError):
            p._split_qualified("bogus:key")

    def test_revision_is_stable_and_changes_with_rates_and_timestamps(self):
        a = {"litellm:m": entry("litellm:m", "1", "2")}
        self.assertEqual(p.compute_revision(a), p.compute_revision(dict(a)))
        self.assertNotEqual(p.compute_revision(a), p.compute_revision({"litellm:m": entry("litellm:m", "1", "3")}))
        self.assertNotEqual(p.compute_revision(a), p.compute_revision(
            {"litellm:m": entry("litellm:m", "1", "2", at=T0 + timedelta(seconds=1))}))


# ------------------------------------------------------------------ aggregation / unknown bucket


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalog(entry("litellm:a", "0.000001", "0"), entry("litellm:px", "0.000002", "0"))
        self.aliases = {"proxy-m": alias("litellm:px", "proxy")}

    def r(self, model, i=1_000_000, o=0, now=T0):
        return resolve(model, i, o, cat=self.cat, aliases=self.aliases, now=now)

    def test_empty_is_null_not_zero(self):
        agg = p.aggregate_resolutions([])
        self.assertIsNone(agg.amount_pico_usd)
        self.assertIsNone(agg.api_equivalent_usd)
        self.assertEqual((agg.priced_rows, agg.unpriced_rows, agg.partial, agg.unknown_row_ids), (0, 0, False, ()))

    def test_all_unknown_stays_null_and_lists_every_row_in_the_unknown_bucket(self):
        agg = p.aggregate_resolutions([("r2", self.r("zzz", 0, 0)), ("r1", self.r("yyy"))])
        self.assertIsNone(agg.amount_pico_usd)
        self.assertEqual((agg.unpriced_rows, agg.partial, agg.unknown_row_ids), (2, True, ("r1", "r2")))

    def test_mixed_group_sums_known_leaves_marks_partial_and_never_prices_unknown_at_zero(self):
        items = [("r1", self.r("a")), ("r2", self.r("proxy-m")), ("r3", self.r("zzz")),
                 ("r4", self.r("a", None, 5)), ("r5", self.r("a", 0, 0))]
        agg = p.aggregate_resolutions(items)
        self.assertEqual(agg.amount_pico_usd, 10**12 + 2 * 10**12)
        self.assertEqual(agg.api_equivalent_usd, 3.0)
        self.assertEqual((agg.priced_rows, agg.unpriced_rows, agg.proxy_rows), (2 + 1, 2, 1))
        self.assertTrue(agg.partial)
        self.assertEqual(agg.unknown_row_ids, ("r3", "r4"))

    def test_known_zero_is_zero_but_distinct_from_no_known_leaf(self):
        agg = p.aggregate_resolutions([("r1", self.r("a", 0, 0))])
        self.assertEqual((agg.amount_pico_usd, agg.api_equivalent_usd, agg.partial), (0, 0.0, False))

    def test_conservation_across_any_grouping(self):
        leaves = [(f"r{i}", self.r("a", 7 * i + 1, 3 * i)) for i in range(9)] + \
                 [(f"p{i}", self.r("proxy-m", 11 * i + 3, 0)) for i in range(5)] + [("u", self.r("nope"))]
        whole = p.aggregate_resolutions(leaves)
        halves = [p.aggregate_resolutions(leaves[:6]), p.aggregate_resolutions(leaves[6:])]
        self.assertEqual(whole.amount_pico_usd, sum(h.amount_pico_usd for h in halves))
        self.assertEqual(whole.priced_rows, sum(h.priced_rows for h in halves))
        self.assertEqual(whole.unpriced_rows, sum(h.unpriced_rows for h in halves))
        self.assertEqual(whole.unknown_row_ids, ("u",))

    def test_stale_if_any_priced_contributor_is_stale(self):
        old = T0 - timedelta(days=9)
        cat = catalog(entry("litellm:a", "1", "1", at=old), entry("litellm:b", "1", "1"))
        rows = [("1", resolve("a", 1, 1, cat=cat)), ("2", resolve("b", 1, 1, cat=cat))]
        self.assertTrue(p.aggregate_resolutions(rows).stale)
        self.assertFalse(p.aggregate_resolutions(rows[1:]).stale)


# ------------------------------------------------------------------ manager: cold start / seed / cache


SMALL = {"litellm:prov/m1": ("0.000001", "0.000002"), "openrouter:v/m2": ("0.000003", "0.000004")}


class ColdStartTests(TmpCase):
    def test_offline_cold_start_resolves_all_seven_from_the_packaged_seed(self):
        fetch = Fetcher()  # every request fails
        mgr = self.manager(fetch)
        snap = mgr.snapshot()
        self.assertEqual(snap.origin, "seed")
        for model_id in INVENTORY:
            r = resolve(model_id, 1000, 1000, cat=snap, aliases=mgr.aliases(), now=T0)
            self.assertIsNone(r.reason, model_id)
            self.assertIsNotNone(r.api_equivalent_usd, model_id)
        self.assertEqual(fetch.calls, [])  # snapshot() with background=False performs no network work

    def test_cold_start_writes_nothing_to_disk(self):
        self.manager().snapshot()
        self.assertFalse(self.data_dir.exists())

    def test_failed_first_refresh_still_serves_the_seed(self):
        mgr = self.manager(Fetcher(), clock=Clock(T0 + timedelta(days=2)))  # the packaged seed is due
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snap.origin, "seed")
        self.assertIn(p.WARN_REFRESH_FAILED, snap.warnings)
        self.assertEqual(resolve("gpt-6-luna", 1, 1, cat=snap, aliases=mgr.aliases()).method, "alias")
        self.assertFalse(self.cache_path.exists())

    def test_snapshot_is_nonblocking_while_a_background_refresh_is_in_flight(self):
        gate, entered = threading.Event(), threading.Event()

        def slow():
            entered.set()
            gate.wait(10)
            return litellm_payload({"prov/new": (1e-06, 1e-06)})

        seed = write_seed(self.tmp / "seed.json", SMALL)
        mgr = p.PricingCatalogManager(self.data_dir, clock=Clock(), random_uniform=uniform_lo,
                                      fetch=Fetcher(litellm=slow, openrouter=OSError("x")), sleep=lambda s: None,
                                      background=True, seed_path=seed)
        snap = mgr.snapshot()  # due (seed is 30h old): schedules the worker and returns the seed right away
        self.assertEqual(snap.origin, "seed")
        self.assertTrue(entered.wait(5))
        self.assertNotIn("litellm:prov/new", mgr.snapshot().entries)  # still not blocked by the in-flight fetch
        gate.set()
        for _ in range(200):
            if "litellm:prov/new" in mgr.snapshot().entries:
                break
            threading.Event().wait(0.02)
        self.assertIn("litellm:prov/new", mgr.snapshot().entries)
        self.assertEqual(snap.origin, "seed")  # the earlier snapshot object is immutable

    def test_single_flight_refresh(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        gate, entered = threading.Event(), threading.Event()

        def slow():
            entered.set()
            gate.wait(10)
            return litellm_payload({"prov/new": (1e-06, 1e-06)})

        fetch = Fetcher(litellm=slow, openrouter=openrouter_payload({"v/n": ("1", "1")}))
        mgr = self.manager(fetch, seed=seed)
        t = threading.Thread(target=mgr.refresh_if_due)
        t.start()
        self.assertTrue(entered.wait(5))
        mgr.refresh_if_due()  # second caller returns immediately instead of fetching again
        self.assertEqual(fetch.count("litellm"), 1)
        gate.set()
        t.join(10)
        self.assertEqual(fetch.count("litellm"), 1)


class CacheLoadTests(TmpCase):
    def write_cache(self, entries, *, at=None, sources=None, raw: bytes | None = None):
        at = at or T0 - timedelta(hours=1)
        metas = sources if sources is not None else {
            s: p.SourceMeta(at, at + timedelta(hours=24), "b" * 64) for s in (p.PRIMARY, p.SECONDARY)}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_bytes(raw if raw is not None else p._serialize_envelope(
            metas, {k: entry(k, *v, at=at) for k, v in entries.items()}))

    def test_valid_cache_overrides_seed_per_key_and_keeps_other_seed_keys(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        self.write_cache({"litellm:prov/m1": ("0.5", "0.6"), "litellm:prov/extra": ("1", "1")})
        snap = self.manager(seed=seed).snapshot()
        self.assertEqual(snap.origin, "cache")
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.5"))
        self.assertEqual(snap.entries["openrouter:v/m2"].input_usd_per_token, Decimal("0.000003"))  # seed key kept
        self.assertIn("litellm:prov/extra", snap.entries)

    def test_cache_older_than_a_newer_seed_does_not_regress_it(self):
        seed = write_seed(self.tmp / "seed.json", SMALL, fetched_at=T0 - timedelta(hours=1))
        self.write_cache({"litellm:prov/m1": ("0.5", "0.6")}, at=T0 - timedelta(days=5))
        snap = self.manager(seed=seed).snapshot()
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000001"))

    def test_invalid_caches_fall_back_to_seed_with_warning_and_are_left_on_disk(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        good = {"litellm:prov/m1": ("0.5", "0.6")}
        at = T0 - timedelta(hours=1)
        base = json.loads(p._serialize_envelope(
            {s: p.SourceMeta(at, at + timedelta(hours=24), "b" * 64) for s in (p.PRIMARY, p.SECONDARY)},
            {k: entry(k, *v, at=at) for k, v in good.items()}))

        def variant(mutate):
            doc = json.loads(json.dumps(base))
            mutate(doc)
            return json.dumps(doc).encode()

        e = "litellm:prov/m1"
        cases = {
            "garbage": b"{not json",
            "not-object": b"[]",
            "unknown-version": variant(lambda d: d.update(schema_version=2)),
            "bool-version": variant(lambda d: d.update(schema_version=True)),
            "extra-top-key": variant(lambda d: d.update(extra=1)),
            "negative-rate": variant(lambda d: d["entries"][e].update(input_usd_per_token="-1")),
            "nan-rate": variant(lambda d: d["entries"][e].update(output_usd_per_token="NaN")),
            "numeric-rate": variant(lambda d: d["entries"][e].update(input_usd_per_token=0.5)),
            "null-rate": variant(lambda d: d["entries"][e].update(input_usd_per_token=None)),
            "future-fetched-at": variant(lambda d: d["entries"][e].update(fetched_at="2099-01-01T00:00:00.000000Z")),
            "bad-timestamp": variant(lambda d: d["entries"][e].update(fetched_at="yesterday")),
            "extra-entry-field": variant(lambda d: d["entries"][e].update(extra="x")),
            "missing-entry-field": variant(lambda d: d["entries"][e].pop("source_key")),
            "key-prefix-mismatch": variant(lambda d: d["entries"][e].update(source="openrouter")),
            "key-source-mismatch": variant(lambda d: d["entries"][e].update(source_key="other")),
            "unknown-source": variant(lambda d: d["sources"].update(bogus=d["sources"]["litellm"])),
            "source-extra-field": variant(lambda d: d["sources"]["litellm"].update(extra=1)),
            "bad-sha": variant(lambda d: d["sources"]["litellm"].update(payload_sha256="zz")),
            "entry-without-source": variant(lambda d: d["sources"].pop("litellm")),
            "duplicate-entry-key": json.dumps(base).replace(
                '"entries": {', '"entries": {"%s": %s, ' % (e, json.dumps(base["entries"][e])), 1).encode(),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.write_cache({}, raw=raw)
                mgr = self.manager(seed=seed)
                snap = mgr.snapshot()
                self.assertIn(p.WARN_CACHE_INVALID, snap.warnings)
                self.assertEqual(snap.origin, "seed")
                self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000001"))
                self.assertEqual(self.cache_path.read_bytes(), raw)  # never deleted/replaced on load

    def test_oversized_cache_is_invalid(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        self.write_cache({}, raw=b" " * 4096)
        with mock.patch.object(p, "MAX_CACHE_BYTES", 1024):
            snap = self.manager(seed=seed).snapshot()
        self.assertIn(p.WARN_CACHE_INVALID, snap.warnings)

    def test_invalid_cache_is_replaced_only_by_a_validated_successful_refresh(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        self.write_cache({}, raw=b"{broken")
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (7e-06, 8e-06)}), openrouter=OSError("down"))
        mgr = self.manager(fetch, seed=seed)
        mgr.refresh_if_due()
        doc = json.loads(self.cache_path.read_text())
        self.assertEqual(doc["schema_version"], 1)
        self.assertNotIn(p.WARN_CACHE_INVALID, mgr.snapshot().warnings)

    def test_symlinked_cache_file_is_refused_not_read_not_written(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        target = self.tmp / "elsewhere.json"
        target.write_bytes(b"sentinel")
        self.cache_path.parent.mkdir(parents=True)
        os.symlink(target, self.cache_path)
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (7e-06, 8e-06)}), openrouter=OSError("x"))
        mgr = self.manager(fetch, seed=seed)
        self.assertIn(p.WARN_CACHE_INVALID, mgr.snapshot().warnings)
        mgr.refresh_if_due()
        self.assertEqual(target.read_bytes(), b"sentinel")
        self.assertTrue(self.cache_path.is_symlink())
        snap = mgr.snapshot()
        self.assertIn(p.WARN_CACHE_UNWRITABLE, snap.warnings)
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000007"))  # memory still fresh

    def test_symlinked_pricing_dir_and_data_dir_are_refused(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        real = self.tmp / "real"
        real.mkdir()
        self.data_dir.mkdir(parents=True)
        os.symlink(real, self.data_dir / "pricing")
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (7e-06, 8e-06)}), openrouter=OSError("x"))
        mgr = self.manager(fetch, seed=seed)
        mgr.refresh_if_due()
        self.assertEqual(list(real.iterdir()), [])
        self.assertIn(p.WARN_CACHE_UNWRITABLE, mgr.snapshot().warnings)
        # symlinked data dir
        other = Path(tempfile.mkdtemp(dir=self.tmp))
        link = self.tmp / "link"
        os.symlink(other, link)
        mgr2 = p.PricingCatalogManager(link, clock=Clock(), random_uniform=uniform_lo, fetch=fetch, background=False,
                                       sleep=lambda s: None, seed_path=seed)
        mgr2.refresh_if_due()
        self.assertEqual(list(other.iterdir()), [])
        self.assertIn(p.WARN_CACHE_UNWRITABLE, mgr2.snapshot().warnings)

    def test_non_regular_cache_path_is_refused(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        self.cache_path.mkdir(parents=True)  # a directory where the file should be
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (7e-06, 8e-06)}), openrouter=OSError("x"))
        mgr = self.manager(fetch, seed=seed)
        mgr.refresh_if_due()
        self.assertTrue(self.cache_path.is_dir())
        self.assertIn(p.WARN_CACHE_UNWRITABLE, mgr.snapshot().warnings)

    def test_unwritable_location_keeps_in_memory_snapshot_and_serves_seed_offline(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        blocker = self.tmp / "plugin-data"
        blocker.write_text("a regular file where the data dir parent should be")
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (7e-06, 8e-06)}), openrouter=OSError("x"))
        mgr = self.manager(fetch, seed=seed)
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertIn(p.WARN_CACHE_UNWRITABLE, snap.warnings)
        self.assertEqual(snap.origin, "network")
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000007"))


# ------------------------------------------------------------------ manager: refresh / jitter / failure


class RefreshTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.seed = write_seed(self.tmp / "seed.json", {
            "litellm:prov/m1": ("0.000001", "0.000002"), "litellm:prov/gone": ("0.000009", "0.000009"),
            "openrouter:v/m2": ("0.000003", "0.000004")})
        self.clock = Clock()
        self.fetch = Fetcher(litellm=litellm_payload({"prov/m1": (5e-06, 6e-06), "prov/new": (7e-06, 8e-06)}),
                             openrouter=openrouter_payload({"v/m2": ("0.000010", "0.000020")}))

    def mgr(self, **kw):
        return self.manager(self.fetch, clock=self.clock, seed=self.seed, **kw)

    def test_success_overlays_per_key_persists_and_marks_network_origin(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snap.origin, "network")
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000005"))
        self.assertEqual(snap.entries["litellm:prov/new"].fetched_at, T0)
        self.assertEqual(snap.entries["openrouter:v/m2"].output_usd_per_token, Decimal("0.00002"))
        self.assertEqual(snap.warnings, ())
        doc = json.loads(self.cache_path.read_text())
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(set(doc["sources"]), {"litellm", "openrouter"})
        self.assertEqual(set(doc["sources"]["litellm"]), {"fetched_at", "next_refresh_at", "payload_sha256"})
        self.assertEqual(doc["sources"]["litellm"]["payload_sha256"],
                         hashlib.sha256(self.fetch.by_url[p.SOURCE_URLS["litellm"]]).hexdigest())
        self.assertEqual(set(doc["entries"]["litellm:prov/new"]),
                         {"source", "source_key", "input_usd_per_token", "output_usd_per_token", "fetched_at"})

    def test_cache_file_permissions_and_no_temp_leftovers(self):
        self.mgr().refresh_if_due()
        self.assertEqual(stat.S_IMODE(self.cache_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.cache_path.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(f.name for f in self.cache_path.parent.iterdir() if f.name.endswith(".tmp")), [])

    def test_fetch_uses_fixed_endpoints_timeout_and_size_limit(self):
        self.mgr().refresh_if_due()
        urls = [c[0] for c in self.fetch.calls]
        self.assertEqual(urls, [p.SOURCE_URLS["litellm"], p.SOURCE_URLS["openrouter"]])  # primary first
        self.assertTrue(all(u.startswith("https://") for u in urls))
        self.assertTrue(all(c[1] == 5.0 and c[2] == 16 * 1024 * 1024 for c in self.fetch.calls))

    def test_next_refresh_is_24h_with_ten_percent_jitter_drawn_once_and_persisted(self):
        for uniform, hours in ((uniform_lo, 21.6), (uniform_hi, 26.4)):
            with self.subTest(hours=hours):
                mgr = self.manager(self.fetch, clock=self.clock, seed=self.seed, uniform=uniform)
                mgr.refresh_if_due()
                doc = json.loads(self.cache_path.read_text())
                for sid in ("litellm", "openrouter"):
                    got = p.parse_ts(doc["sources"][sid]["next_refresh_at"]) - p.parse_ts(doc["sources"][sid]["fetched_at"])
                    self.assertAlmostEqual(got.total_seconds(), hours * 3600, delta=1e-3)
                self.cache_path.unlink()

    def test_draws_use_the_documented_uniform_ranges(self):
        draws = []

        def spy(a, b):
            draws.append((a, b))
            return (a + b) / 2

        self.fetch.set("openrouter", OSError("down"))
        self.mgr(uniform=spy).refresh_if_due()
        self.assertIn((0.9, 1.1), draws)

    def test_not_due_means_no_network_and_restart_honors_the_persisted_due_time(self):
        self.mgr().refresh_if_due()
        calls = len(self.fetch.calls)
        self.clock.advance(hours=20)
        restarted = self.mgr()  # fresh process, same disk
        restarted.refresh_if_due()
        self.assertEqual(len(self.fetch.calls), calls)
        self.assertEqual(restarted.snapshot().origin, "cache")
        self.clock.advance(hours=2)  # 22h > 21.6h lower bound of the jitter window
        restarted.refresh_if_due()
        self.assertEqual(len(self.fetch.calls), calls + 2)

    def test_corrupt_or_far_future_next_refresh_is_clamped_to_due_now(self):
        self.mgr().refresh_if_due()
        doc = json.loads(self.cache_path.read_text())
        doc["sources"]["litellm"]["next_refresh_at"] = "2099-01-01T00:00:00.000000Z"
        doc["sources"]["openrouter"]["next_refresh_at"] = "2000-01-01T00:00:00.000000Z"  # before fetched_at
        self.cache_path.write_text(json.dumps(doc))
        before = len(self.fetch.calls)
        self.mgr().refresh_if_due()
        self.assertEqual(len(self.fetch.calls), before + 2)

    def test_failure_preserves_every_price_timestamp_and_current_response(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        good = mgr.snapshot()
        self.clock.advance(days=2)
        self.fetch.set("litellm", OSError("down"))
        self.fetch.set("openrouter", b"<html>nope</html>")
        mgr.refresh_if_due()
        after = mgr.snapshot()
        self.assertEqual(dict(after.entries), dict(good.entries))
        self.assertEqual(after.revision, good.revision)
        self.assertIn(p.WARN_REFRESH_FAILED, after.warnings)
        self.assertEqual(after.entries["litellm:prov/m1"].fetched_at, T0)  # failure never resets age

    def test_failed_refresh_does_not_blank_previously_cached_values_across_restart(self):
        self.mgr().refresh_if_due()
        self.clock.advance(days=2)
        self.fetch.by_url.clear()  # everything fails from now on
        restarted = self.mgr()
        restarted.refresh_if_due()
        snap = restarted.snapshot()
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000005"))
        self.assertEqual(resolve("prov/m1", 1_000_000, 0, cat=snap).amount_pico_usd, 5 * 10**12)

    def test_every_failure_kind_counts_and_none_blank_the_catalog(self):
        bad_bodies = {
            "http": OSError("HTTP 503"), "timeout": TimeoutError(), "fetch-error": p.FetchError("x"),
            "empty": b"", "html": b"<html/>", "wrong-json": b"[1,2,3]", "no-usable": b'{"a": {"mode": "embedding"}}',
            "malformed": b'{"a": ', "dup-keys": b'{"a": 1, "a": 2}', "oversize": b"x" * 64,
        }
        for name, body in bad_bodies.items():
            with self.subTest(name):
                mgr = self.mgr()
                self.fetch.set("litellm", body)
                self.fetch.set("openrouter", body)
                with mock.patch.object(p, "MAX_FETCH_BYTES", 32 if name == "oversize" else p.MAX_FETCH_BYTES):
                    mgr.refresh_if_due()
                snap = mgr.snapshot()
                self.assertIn(p.WARN_REFRESH_FAILED, snap.warnings)
                self.assertIn("litellm:prov/gone", snap.entries)
                self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000001"))
                self.assertFalse(self.cache_path.exists())

    def test_at_most_two_attempts_per_source_with_jittered_retry_sleep(self):
        sleeps = []
        self.fetch.set("litellm", OSError("down"))
        self.fetch.set("openrouter", OSError("down"))
        self.mgr(sleeps=sleeps, uniform=uniform_hi).refresh_if_due()
        self.assertEqual((self.fetch.count("litellm"), self.fetch.count("openrouter")), (2, 2))
        self.assertEqual(sleeps, [1.5, 1.5])
        sleeps.clear()
        self.fetch.calls.clear()
        self.mgr(sleeps=sleeps, uniform=uniform_lo).refresh_if_due()
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_second_attempt_can_succeed(self):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("blip")
            return litellm_payload({"prov/m1": (5e-06, 6e-06)})

        self.fetch.set("litellm", flaky)
        mgr = self.mgr()
        mgr.refresh_if_due()
        self.assertEqual(state["n"], 2)
        self.assertNotIn(p.WARN_REFRESH_FAILED, mgr.snapshot().warnings)

    def test_failure_retries_after_about_fifteen_minutes_not_on_every_render(self):
        self.fetch.by_url.clear()
        mgr = self.mgr(uniform=uniform_lo)
        mgr.refresh_if_due()
        first = len(self.fetch.calls)
        self.assertEqual(first, 4)
        for _ in range(5):
            mgr.snapshot()
            mgr.refresh_if_due()  # no hot loop
        self.assertEqual(len(self.fetch.calls), first)
        self.clock.advance(minutes=13)  # 15min * 0.9 = 13.5
        mgr.refresh_if_due()
        self.assertEqual(len(self.fetch.calls), first)
        self.clock.advance(minutes=1, seconds=1)
        mgr.refresh_if_due()
        self.assertEqual(len(self.fetch.calls), first + 4)
        # upper bound of the window
        mgr2 = self.mgr(uniform=uniform_hi)
        mgr2.refresh_if_due()
        n = len(self.fetch.calls)
        self.clock.advance(minutes=16)
        mgr2.refresh_if_due()
        self.assertEqual(len(self.fetch.calls), n)
        self.clock.advance(minutes=1)
        mgr2.refresh_if_due()
        self.assertGreater(len(self.fetch.calls), n)

    def test_one_source_failing_does_not_erase_the_other(self):
        self.fetch.set("openrouter", OSError("down"))
        mgr = self.mgr()
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000005"))
        self.assertEqual(snap.entries["openrouter:v/m2"].input_usd_per_token, Decimal("0.000003"))  # seed value kept
        self.assertIn(p.WARN_REFRESH_FAILED, snap.warnings)
        doc = json.loads(self.cache_path.read_text())
        self.assertEqual(set(doc["sources"]), {"litellm"})

    def test_primary_failure_never_reprices_a_primary_key_with_a_secondary_one(self):
        seed = write_seed(self.tmp / "seed2.json", {"litellm:prov/model-9b": ("0.000001", "0.000001")})
        fetch = Fetcher(litellm=OSError("down"), openrouter=openrouter_payload({"v/model-9b": ("0.5", "0.5")}))
        mgr = self.manager(fetch, clock=self.clock, seed=seed)
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snap.entries["litellm:prov/model-9b"].input_usd_per_token, Decimal("0.000001"))
        r = resolve("model-9b", 1_000_000, 0, cat=snap)
        self.assertEqual(r.source, "litellm")  # secondary supplements gaps only; it cannot replace primary prices
        self.assertEqual(r.amount_pico_usd, 10**12)

    def test_delisted_entries_keep_their_price_and_original_timestamp(self):
        mgr = self.mgr()
        mgr.refresh_if_due()
        gone = mgr.snapshot().entries["litellm:prov/gone"]
        self.assertEqual(gone.input_usd_per_token, Decimal("0.000009"))
        self.assertEqual(gone.fetched_at, T0 - timedelta(hours=30))  # still the seed's retrieval time

    def test_a_key_changes_rate_only_when_that_key_validates(self):
        self.fetch.set("litellm", litellm_payload({
            "prov/m1": (-1, 1e-06),                  # invalid -> old price retained
            "prov/gone": (1e-06, 1e-06),             # valid -> changes
            "prov/new": {"mode": "embedding", "input_cost_per_token": 1, "output_cost_per_token": 1},
            "prov/other": (1e-06, 1e-06)}))
        mgr = self.mgr()
        mgr.refresh_if_due()
        snap = mgr.snapshot()
        self.assertEqual(snap.entries["litellm:prov/m1"].input_usd_per_token, Decimal("0.000001"))
        self.assertEqual(snap.entries["litellm:prov/m1"].fetched_at, T0 - timedelta(hours=30))
        self.assertEqual(snap.entries["litellm:prov/gone"].input_usd_per_token, Decimal("0.000001"))
        self.assertEqual(snap.entries["litellm:prov/gone"].fetched_at, T0)
        self.assertNotIn("litellm:prov/new", snap.entries)

    def test_an_earlier_snapshot_is_immutable_across_a_refresh(self):
        mgr = self.mgr()
        old = mgr.snapshot()
        old_entries, old_rev = dict(old.entries), old.revision
        mgr.refresh_if_due()
        new = mgr.snapshot()
        self.assertEqual(dict(old.entries), old_entries)
        self.assertEqual(old.revision, old_rev)
        self.assertNotEqual(new.revision, old_rev)
        with self.assertRaises(TypeError):
            old.entries["litellm:x"] = entry("litellm:x")  # type: ignore[index]

    def test_unchanged_state_returns_the_same_snapshot_object(self):
        mgr = self.mgr()
        self.assertIs(mgr.snapshot(), mgr.snapshot())

    def test_concurrent_writers_do_not_regress_each_other_on_disk(self):
        a, b = self.mgr(), self.mgr()
        a.refresh_if_due()
        self.fetch.set("litellm", litellm_payload({"prov/only-b": (1e-06, 1e-06)}))
        self.clock.advance(days=2)
        b.refresh_if_due()
        keys = set(json.loads(self.cache_path.read_text())["entries"])
        self.assertTrue({"litellm:prov/m1", "litellm:prov/new", "litellm:prov/only-b"} <= keys)

    def test_future_dated_source_fetched_at_is_treated_as_due_now(self):
        self.mgr().refresh_if_due()
        doc = json.loads(self.cache_path.read_text())
        for s in doc["sources"].values():
            s["fetched_at"] = "2099-01-01T00:00:00.000000Z"
            s["next_refresh_at"] = "2099-01-02T00:00:00.000000Z"
        for e in doc["entries"].values():
            e["fetched_at"] = "2099-01-01T00:00:00.000000Z"
        self.cache_path.write_text(json.dumps(doc))
        mgr = self.mgr()
        self.assertIn(p.WARN_CACHE_INVALID, mgr.snapshot().warnings)  # future timestamps invalidate the envelope


class StaleTests(TmpCase):
    def test_stale_warning_after_seven_days_including_seed_age_and_failure_does_not_reset_it(self):
        seed = write_seed(self.tmp / "seed.json", SMALL, fetched_at=T0 - timedelta(days=6, hours=23))
        clock = Clock()
        mgr = self.manager(Fetcher(), clock=clock, seed=seed)
        self.assertNotIn(p.WARN_STALE, mgr.snapshot().warnings)
        clock.advance(hours=2)
        snap = mgr.snapshot()
        self.assertIn(p.WARN_STALE, snap.warnings)
        mgr.refresh_if_due()  # fails
        self.assertIn(p.WARN_STALE, mgr.snapshot().warnings)
        r = resolve("prov/m1", 1_000_000, 0, cat=snap, now=clock.now)
        self.assertTrue(r.stale)
        self.assertIsNotNone(r.amount_pico_usd)  # retained indefinitely, never blanked

    def test_fresh_refresh_clears_staleness(self):
        seed = write_seed(self.tmp / "seed.json", SMALL, fetched_at=T0 - timedelta(days=30))
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (1e-06, 1e-06)}),
                        openrouter=openrouter_payload({"v/m2": ("1", "1")}))
        mgr = self.manager(fetch, seed=seed)
        self.assertIn(p.WARN_STALE, mgr.snapshot().warnings)
        mgr.refresh_if_due()
        self.assertNotIn(p.WARN_STALE, mgr.snapshot().warnings)

    def test_warnings_are_sorted_unique_codes_only(self):
        seed = write_seed(self.tmp / "seed.json", SMALL, fetched_at=T0 - timedelta(days=30))
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_bytes(b"{")
        mgr = self.manager(Fetcher(), seed=seed)
        mgr.refresh_if_due()
        warnings = mgr.snapshot().warnings
        self.assertEqual(list(warnings), sorted(set(warnings)))
        known = {p.WARN_STALE, p.WARN_REFRESH_FAILED, p.WARN_CACHE_INVALID, p.WARN_CACHE_UNWRITABLE,
                 p.WARN_ALIASES_INVALID, p.WARN_SEED_INVALID}
        self.assertTrue(set(warnings) <= known)
        self.assertFalse(any("/" in w or " " in w for w in warnings))  # no raw exceptions or paths

    def test_invalid_seed_is_a_warning_not_an_exception(self):
        bad = self.tmp / "seed.json"
        bad.write_text("{nope")
        mgr = self.manager(seed=bad)
        snap = mgr.snapshot()
        self.assertIn(p.WARN_SEED_INVALID, snap.warnings)
        self.assertEqual(dict(snap.entries), {})
        self.assertEqual(resolve("gpt-6-luna", 1, 1, cat=snap, aliases=mgr.aliases()).reason, "alias_target_missing")

    def test_logging_never_contains_urls_paths_or_usage(self):
        seed = write_seed(self.tmp / "seed.json", SMALL)
        fetch = Fetcher(litellm=litellm_payload({"prov/m1": (1e-06, 1e-06)}), openrouter=OSError("secret-token-123"))
        with self.assertLogs("usage_deck.pricing", level="INFO") as logs:
            self.manager(fetch, seed=seed).refresh_if_due()
        text = "\n".join(logs.output)
        self.assertNotIn("secret-token-123", text)
        self.assertNotIn(str(self.tmp), text)
        self.assertIn("source=litellm", text)


# ------------------------------------------------------------------ transport policy and module hygiene


class TransportPolicyTests(unittest.TestCase):
    def test_default_fetch_refuses_any_endpoint_not_fixed_in_code(self):
        for url in ("http://openrouter.ai/api/v1/models", "https://evil.example/x", "file:///etc/passwd",
                    p.SOURCE_URLS["litellm"] + "?x=1"):
            with self.assertRaises(p.FetchError):
                p.default_fetch(url, 1.0, 1024)

    def test_redirects_are_limited_to_the_original_https_host(self):
        handler = p._SameHostRedirect("openrouter.ai")
        req = mock.Mock(full_url=p.SOURCE_URLS["openrouter"])
        for target in ("https://evil.example/x", "http://openrouter.ai/x"):
            with self.assertRaises(p.FetchError):
                handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_endpoints_are_https_and_not_derived_from_data(self):
        self.assertEqual(set(p.SOURCE_URLS), {"litellm", "openrouter"})
        self.assertTrue(all(u.startswith("https://") for u in p.SOURCE_URLS.values()))


class HygieneTests(unittest.TestCase):
    SRC = (Path(p.__file__)).read_text()

    def test_module_never_opens_a_session_store(self):
        self.assertNotIn("sqlite3", self.SRC)
        self.assertNotIn("state.db", re.sub(r'"""[\s\S]*?"""', "", self.SRC).replace("# state.db", ""))

    def test_module_has_no_dependency_on_the_adapters_package(self):
        self.assertNotRegex(self.SRC, r"from \.adapters|import adapters")

    def test_resolver_does_not_import_hermes(self):
        self.assertNotIn("hermes_cli", self.SRC)


if __name__ == "__main__":
    unittest.main()
