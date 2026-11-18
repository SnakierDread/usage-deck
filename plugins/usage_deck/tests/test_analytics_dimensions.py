"""Analytics dimensions: machine-wide union, provider attribution, dimension partitions and read-only guarantees.

Design: docs/design/analytics-dimensions-breakdown.md (§8 named checks). Every database here is a synthetic,
temporary SQLite file created BEFORE the reader runs; no production home or state.db is ever opened. Expected
values are computed in this harness from the fixture specs / direct SQL, never from the code under test.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from plugins.usage_deck.adapters.analytics import normalize_analytics
from plugins.usage_deck.adapters.session_db import (
    MAX_MODELS_JSON_BYTES,
    SessionDbReader,
    attribute,
    load_models_map,
)
from plugins.usage_deck.discovery import MAX_PROFILES, FileInventorySource
from plugins.usage_deck.service import UsageDeckService

from plugins.usage_deck.models.dto import DtoError, check_envelope
from plugins.usage_deck.models.redaction import sanitize

from .helpers import FakeTransport, scan_for

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()
DAY = 86400

FULL_COLUMNS = ("id TEXT, source TEXT, model TEXT, provider TEXT, billing_provider TEXT, started_at REAL, "
                "input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL, api_call_count INTEGER")


def S(sid, days_ago, model, provider, billing, inp, out, cost, calls, want, hour=0):
    """A session spec; ``want`` is the provider id the contract says it attributes to (hand-written)."""
    return {"id": sid, "started_at": NOW_TS - days_ago * DAY - hour * 3600, "model": model, "provider": provider,
            "billing_provider": billing, "input_tokens": inp, "output_tokens": out, "estimated_cost_usd": cost,
            "api_call_count": calls, "want": want}


def local_day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Machine:
    """A temporary Hermes root: ``root/`` (global), ``root/profiles/<name>/``; DBs written with plain sqlite3."""

    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory(prefix="usage-deck-dims-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "profiles").mkdir()
        self.specs = {}

    def home(self, name):
        return self.root if name is None else self.root / "profiles" / name

    def write_db(self, name, sessions, columns=FULL_COLUMNS, wal=False, idle_wal=False):
        """``wal``: keep a live WAL writer open (frames stay in -wal); ``idle_wal``: WAL format, no sidecars."""
        directory = self.home(name)
        directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(directory / "state.db")
        if wal or idle_wal:
            conn.execute("PRAGMA journal_mode=WAL")
        if wal:
            conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(f"CREATE TABLE sessions ({columns})")
        have = [c.split()[0] for c in columns.split(", ")]
        for spec in sessions:
            conn.execute(f"INSERT INTO sessions ({', '.join(have)}) VALUES ({', '.join('?' * len(have))})",
                         [spec.get(c) for c in have])
        conn.commit()
        self.specs[name] = list(sessions)
        if wal:
            return conn  # keep the writer open: committed frames stay in the -wal file
        conn.close()
        return None

    def write_models(self, name, entries):
        (self.home(name) / "models.json").write_text(json.dumps({"models": entries}))

    def service(self, home=None, reader_factory=None, clock=lambda: NOW):
        source_home = self.home(home)
        src = FileInventorySource(source_home, self.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)

        def analytics(profile):
            if profile is None or profile.lower() == "current":
                return DirectSqlAnalytics(source_home)
            return DirectSqlAnalytics(self.root if profile == "default" else self.home(profile))

        kwargs = {"session_reader_factory": reader_factory} if reader_factory else {}
        return UsageDeckService(inventory_factory=lambda: src, analytics_factory=analytics,
                                transport=FakeTransport(), clock=clock, **kwargs)


class DirectSqlAnalytics:
    """The host's baseline daily SQL (hermes_cli/web_routers/analytics.py), run directly on one temp DB."""

    def __init__(self, home):
        self.home = home

    def available(self):
        return True

    def read_usage(self, days):
        conn = sqlite3.connect(self.home / "state.db")
        try:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("""
                SELECT date(started_at, 'unixepoch', 'localtime') as day, SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens, COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                       COUNT(*) as sessions, SUM(COALESCE(api_call_count, 0)) as api_calls
                FROM sessions WHERE started_at > ? GROUP BY day ORDER BY day""", (NOW_TS - days * DAY,))]
        finally:
            conn.close()
        return normalize_analytics({"daily": rows})


def direct_totals(sessions, window=30, want=None, wanted_models=None):
    """Totals computed in the harness from session specs (sum-known; None when nothing is known)."""
    use = [s for s in sessions if s["started_at"] > NOW_TS - window * DAY
           and (want is None or s["want"] == want or (want == "custom" and s["want"].startswith("custom")))
           and (wanted_models is None or s["model"] in wanted_models)]

    def known(field):
        values = [s[field] for s in use if s[field] is not None]
        return sum(values) if values else None

    return {"input_count": known("input_tokens"), "output_count": known("output_tokens"),
            "sessions": len(use) if use else None,
            "api_calls": sum(s["api_call_count"] or 0 for s in use) if use else None,
            "estimated_usd": sum(s["estimated_cost_usd"] or 0 for s in use) if use else None}


def totals_of(data):
    t = data["totals"]
    return {k: t[k] for k in ("input_count", "output_count", "sessions", "api_calls", "estimated_usd")}


def approx_equal(a, b):
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-9


def assert_totals(case, got, want):
    for key in ("input_count", "output_count", "sessions", "api_calls"):
        case.assertEqual(got[key], want[key], key)
    case.assertTrue(approx_equal(got["estimated_usd"], want["estimated_usd"]), (got, want))


ROOT_SESSIONS = [
    S("s1", 1, "claude-sonnet-5.5", "anthropic", None, 1000, 500, 0.5, 4, "anthropic"),
    S("s2", 2, "claude-sonnet-5.5", "anthropic", None, 2000, 700, 1.0, 6, "anthropic"),
    S("s3", 2, "local-x", "custom:box", None, 300, 100, 0.0, 1, "custom:box"),
]
A_SESSIONS = [
    S("s1", 1, "gpt-6-luna", "openai", None, 4000, 1500, 2.0, 10, "openai"),   # equal session id as the root's s1
    S("s2", 3, "claude-sonnet-5.5", None, "nous", 800, 200, 0.2, 2, "nous"),      # billing provider only
    S("s3", 2, "mapped-m", None, "custom", 50, 25, 0.01, 1, "custom:gpu"),        # bare billing + own models.json
    S("s4", 4, None, None, "auto", 70, 30, 0.02, 1, "unknown"),                   # nothing attributable
]
B_SESSIONS = [
    S("s1", 1, "claude-sonnet-5.5", "anthropic", None, 123, 45, 0.1, 2, "anthropic"),
    S("s2", 1, "claude-sonnet-5.5", "anthropic", None, None, None, 0.3, 2, "anthropic", hour=1),  # blank tokens
    S("s9", 40, "claude-sonnet-5.5", "anthropic", None, 10 ** 9, 10 ** 9, 99.0, 9, "anthropic"),  # outside 30d
]


def seed_machine(m):
    m.write_db(None, ROOT_SESSIONS)
    m.write_db("a", A_SESSIONS)  # DB-only home: no auth.json, no config
    m.write_models("a", [{"id": "mapped-m", "provider": "custom:gpu"}])
    m.write_db("b", B_SESSIONS)


class UnionTests(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self)
        seed_machine(self.m)
        self.svc = self.m.service()

    def get(self, params=(), svc=None, **kw):
        status, body = (svc or self.svc).handle("usage", list(params), **kw)
        self.assertEqual(status, 200, body)
        return body["data"]

    def test_machine_all_equals_sum_profile_totals(self):
        for window in (7, 30, 90):
            data = self.get([("mode", "machine"), ("dimension", "all"), ("window", str(window))])
            self.assertEqual((data["mode"], data["dimension"], data["complete"]), ("machine", "all", True))
            per_home = []
            for name, key in ((None, "default"), ("a", "a"), ("b", "b")):
                profile = self.get([("mode", "profile"), ("profile", key), ("window", str(window))])
                direct = direct_totals(self.m.specs[name], window)
                assert_totals(self, totals_of(profile), direct)  # profile All == its state.db alone
                per_home.append(direct)
            for field in ("input_count", "output_count", "sessions", "api_calls"):
                known = [h[field] for h in per_home if h[field] is not None]
                self.assertEqual(data["totals"][field], sum(known) if known else None, (window, field))
            self.assertTrue(approx_equal(data["totals"]["estimated_usd"], sum(h["estimated_usd"] for h in per_home if h["estimated_usd"] is not None)))
        # daily buckets: sum-known across homes against independent per-day arithmetic from the specs
        data = self.get([("window", "30")])
        expected = {}
        for specs in self.m.specs.values():
            for s in specs:
                if s["started_at"] > NOW_TS - 30 * DAY:
                    d = expected.setdefault(local_day(s["started_at"]), {"in": None, "n": 0})
                    d["n"] += 1
                    if s["input_tokens"] is not None:
                        d["in"] = (d["in"] or 0) + s["input_tokens"]
        points = {p["day"]: p for p in data["series"][0]["points"]}
        self.assertEqual(sorted(points), sorted(expected))
        for day, want in expected.items():
            self.assertEqual((points[day]["input_count"], points[day]["sessions"]), (want["in"], want["n"]), day)
        self.assertEqual(len(points), len(data["series"][0]["points"]))  # one point per day, never concatenated
        # overlap really happened: equal days across homes and equal session ids in different DBs
        self.assertLess(len(expected), sum(1 for specs in self.m.specs.values() for s in specs if s["started_at"] > NOW_TS - 30 * DAY))
        # all-null tokens (b/s2) stay blank in its own row but the day still carries the known subtotal
        self.assertIn("input_count", data["totals"]["missing_fields"])

    def test_totals_conserved_across_every_dimension_and_provider(self):
        base = self.get([])
        for dimension in ("all", "profile", "model"):
            data = self.get([("dimension", dimension)])
            for field in ("input_count", "output_count", "sessions", "api_calls"):
                self.assertEqual(data["totals"][field], base["totals"][field], (dimension, field))
                parts = [g["totals"][field] for g in data["groups"] if g["totals"][field] is not None]
                self.assertEqual(sum(parts), base["totals"][field], (dimension, field, "group sum"))
            self.assertTrue(approx_equal(sum(g["totals"]["estimated_usd"] or 0 for g in data["groups"]), base["totals"]["estimated_usd"]))
            self.assertEqual(sorted(r["id"] for r in data["rows"]), sorted(r["id"] for r in base["rows"]))
            self.assertEqual(sorted(rid for g in data["groups"] for rid in g["row_ids"]), sorted(r["id"] for r in data["rows"]))

    def test_cross_home_rows_stamp_their_owning_db(self):
        data = self.get([])
        stamped = {(r["profile"], r["is_global"]) for r in data["rows"]}
        self.assertEqual(stamped, {("global", True), ("a", False), ("b", False)})
        self.assertTrue(all(r["source"] == "state.db" for r in data["rows"]))
        self.assertEqual([(s["profile"], s["is_global"], s["state"]) for s in data["sources"]],
                         [("global", True, "ok"), ("a", False, "ok"), ("b", False, "ok")])
        blob = json.dumps(data)
        self.assertNotIn(str(self.m.root), blob)  # no absolute path leaves the service
        self.assertNotIn("state.db\"/", blob)

    def test_profile_all_state_db_only_contract(self):
        """Profile All is exactly its own DB's baseline whatever the siblings hold (and no sibling is opened)."""
        opened = []

        class Spy(SessionDbReader):
            def read_groups(self, directory, cutoff):
                opened.append(Path(directory).name)
                return super().read_groups(directory, cutoff)

        svc = self.m.service(home="a", reader_factory=Spy)
        before = {w: self.get([("mode", "profile"), ("window", str(w))], svc) for w in (7, 30, 90)}
        # populate every other home with distinctive huge counts, then re-ask a fresh service
        self.m.home("a")  # unchanged
        for name in (None, "b"):
            conn = sqlite3.connect(self.m.home(name) / "state.db")
            conn.execute("INSERT INTO sessions (id, model, provider, started_at, input_tokens, output_tokens, "
                         "estimated_cost_usd, api_call_count) VALUES ('big', 'x', 'openai', ?, 777777777, 888888888, 5.0, 3)",
                         (NOW_TS - DAY,))
            conn.commit()
            conn.close()
        svc2 = self.m.service(home="a", reader_factory=Spy)
        for window, old in before.items():
            now = self.get([("mode", "profile"), ("window", str(window))], svc2)
            self.assertEqual(totals_of(now), totals_of(old))
            self.assertEqual(now["series"], old["series"])
            assert_totals(self, totals_of(now), direct_totals(A_SESSIONS, window))
        self.assertEqual(set(opened), {"a"})  # only the selected profile's state.db was ever opened
        # explicit profile and `current` resolve the same home; root/default resolves the root alone
        explicit = self.get([("mode", "profile"), ("profile", "a")], svc2)
        current = self.get([("mode", "profile"), ("profile", "current")], svc2)
        self.assertEqual(totals_of(explicit), totals_of(current))
        root = self.get([("mode", "profile"), ("profile", "default")], svc2)
        self.assertNotEqual(totals_of(root), totals_of(explicit))

    def test_dimension_switch_changes_series_and_rows(self):
        a = self.get([("dimension", "all")])
        p = self.get([("dimension", "profile")])
        mo = self.get([("dimension", "model")])
        self.assertEqual((len(a["series"]), len(a["groups"])), (1, 1))
        self.assertEqual([g["label"] for g in p["groups"]], ["global (default home)", "a", "b"])
        self.assertEqual(len(p["series"]), 3)
        models = {g["model_id"] for g in mo["groups"]}
        self.assertEqual(models, {"claude-sonnet-5.5", "gpt-6-luna", "local-x", "mapped-m", None})
        self.assertEqual(mo["top_n"], 8)
        self.assertEqual({len(x["series"]) for x in (a, p, mo)}, {1, 3, 5})
        # By model labels: model id with provider labels in parentheses; null model is "Unknown model"
        labels = {g["model_id"]: g["label"] for g in mo["groups"]}
        self.assertEqual(labels["gpt-6-luna"], "gpt-6-luna (openai)")
        self.assertEqual(labels["claude-sonnet-5.5"], "claude-sonnet-5.5 (Anthropic, Nous Portal)")
        self.assertEqual(labels[None], "Unknown model (Unknown)")
        self.assertEqual(labels["mapped-m"], "mapped-m (gpu)")
        # profile mode: All | By model only (single profile), by-profile is a 400
        status, body = self.svc.handle("usage", [("mode", "profile"), ("dimension", "profile")])
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"))
        pm = self.get([("mode", "profile"), ("profile", "a"), ("dimension", "model")])
        self.assertEqual({g["model_id"] for g in pm["groups"]}, {"gpt-6-luna", "claude-sonnet-5.5", "mapped-m", None})
        self.assertEqual(pm["series"][0]["scope"], "profile")
        self.assertEqual(a["series"][0]["scope"], "machine")

    def test_provider_filter_real_subsets(self):
        every = [s for specs in self.m.specs.values() for s in specs]
        unfiltered = self.get([])
        assert_totals(self, totals_of(unfiltered), direct_totals(every))
        seen = {}
        for provider, want in (("anthropic", "anthropic"), ("custom", "custom"), ("custom:box", "custom:box"),
                               ("custom:gpu", "custom:gpu"), ("nous", "nous"), ("openai", "openai"),
                               ("unknown", "unknown")):
            data = self.get([("provider", provider)])
            self.assertEqual(data["provider_id"], provider)
            self.assertTrue(all(s["provider_id"] == provider or (provider == "custom" and s["provider_id"].startswith("custom"))
                                for s in data["series"] and data["rows"]), provider)
            assert_totals(self, totals_of(data), direct_totals(every, want=want))
            seen[provider] = totals_of(data)
            for s in data["series"]:
                self.assertEqual(s["provider_id"], provider)
        # three independently expected, DISTINCT numeric subsets
        self.assertEqual(len({json.dumps(seen[k], sort_keys=True) for k in ("anthropic", "custom", "unknown")}), 3)
        # the roll-up is not the exact route; a recorded provider beats a conflicting map; unmapped bare billing is Unknown
        self.assertNotEqual(seen["custom"], seen["custom:box"])
        self.assertEqual(seen["unknown"]["input_count"], 70)
        self.assertEqual(seen["custom:gpu"]["input_count"], 50)  # model-map fallback, own home only
        none = self.get([])
        for opt in ("anthropic", "custom", "custom:box", "custom:gpu", "nous", "openai", "unknown"):
            self.assertIn(opt, [o["id"] for o in none["providers"]])
        self.assertEqual([o["label"] for o in none["providers"] if o["id"] == "custom"], ["Custom endpoint"])
        self.assertEqual(none["providers"][-1]["id"], "unknown")  # Unknown is listed last
        # provider options keep listing every historical provider while one is selected
        filtered = self.get([("provider", "anthropic")])
        self.assertEqual([o["id"] for o in filtered["providers"]], [o["id"] for o in none["providers"]])
        # attribution_source is stamped per row
        sources = {(r["provider_id"], r["attribution_source"]) for r in none["rows"]}
        self.assertIn(("anthropic", "session_provider"), sources)
        self.assertIn(("nous", "billing_provider"), sources)
        self.assertIn(("custom:gpu", "models_json"), sources)
        self.assertIn(("unknown", "unknown"), sources)

    def test_unknown_and_known_but_empty_provider(self):
        status, body = self.svc.handle("usage", [("provider", "nope")])
        self.assertEqual((status, body["error"]["code"]), (404, "provider_not_found"))
        # historical provider that has no observation in a short window: 200 + no_observations, never unfiltered
        m = Machine(self)
        m.write_db(None, [S("x", 40, "gpt-6-luna", "openai", None, 5, 5, 0.1, 1, "openai"),
                          S("y", 1, "claude-sonnet-5.5", "anthropic", None, 9, 9, 0.2, 1, "anthropic")])
        svc = m.service()
        # openai is only in the 90d window
        status, body = svc.handle("usage", [("provider", "openai"), ("window", "30")])
        self.assertEqual(status, 404)  # not known in this window and not in the catalog
        status, body = svc.handle("usage", [("provider", "openai"), ("window", "90")])
        self.assertEqual(status, 200)
        self.assertEqual(totals_of(body["data"])["input_count"], 5)
        # a catalog-known provider with zero matching observations returns 200 no_observations (not other data)
        m.write_models(None, [{"id": "q", "provider": "nous"}])
        status, body = m.service().handle("usage", [("provider", "nous")])
        data = body["data"]
        self.assertEqual((status, data["reason"], data["rows"], data["groups"][0]["row_ids"]), (200, "no_observations", [], []))
        self.assertEqual(data["series"][0]["points"], [])
        self.assertIsNone(data["totals"]["sessions"])
        self.assertEqual(data["provider_id"], "nous")

    def test_null_vs_zero_is_preserved(self):
        m = Machine(self)
        m.write_db(None, [S("n1", 1, "m", "openai", None, None, None, None, None, "openai"),
                          S("z1", 2, "z", "openai", None, 0, 0, 0.0, 0, "openai")])
        data = m.service().handle("usage", [])[1]["data"]
        by_model = {r["model_id"]: r["metrics"] for r in data["rows"]}
        self.assertEqual((by_model["m"]["input_count"], by_model["m"]["output_count"]), (None, None))
        self.assertEqual((by_model["z"]["input_count"], by_model["z"]["output_count"]), (0, 0))  # measured zero
        self.assertEqual(by_model["m"]["missing_fields"], ["input_count", "output_count"])
        self.assertEqual(by_model["z"]["missing_fields"], [])
        self.assertEqual((data["totals"]["input_count"], data["totals"]["sessions"]), (0, 2))
        # a mixed aggregate carries the known subtotal and says what is missing (never a zero for the blank one)
        mixed = Machine(self)
        mixed.write_db(None, [S("k1", 1, "m", "openai", None, 10, 4, 0.1, 1, "openai"),
                              S("k2", 1, "m", "openai", None, None, 6, 0.1, 1, "openai", hour=1)])
        md = mixed.service().handle("usage", [("dimension", "model")])[1]["data"]
        self.assertEqual((md["totals"]["input_count"], md["totals"]["output_count"], md["totals"]["sessions"]), (10, 10, 2))
        self.assertEqual(md["totals"]["missing_fields"], ["input_count"])
        self.assertEqual(md["groups"][0]["totals"]["missing_fields"], ["input_count"])
        # a sessions-only home (old schema: no token / cost / call columns) never invents numbers
        old = Machine(self)
        old.write_db(None, [S("o1", 1, None, None, None, 1, 1, 1, 1, "unknown")], columns="id TEXT, started_at REAL")
        d = old.service().handle("usage", [])[1]["data"]
        self.assertEqual({k: d["totals"][k] for k in ("input_count", "output_count", "estimated_usd", "api_calls")},
                         {"input_count": None, "output_count": None, "estimated_usd": None, "api_calls": None})
        self.assertEqual(d["totals"]["sessions"], 1)
        self.assertEqual(d["totals"]["missing_fields"], ["api_calls", "estimated_usd", "input_count", "output_count"])
        point = d["series"][0]["points"][0]
        self.assertEqual((point["input_count"], point["estimated_usd"], point["sessions"]), (None, None, 1))
        self.assertEqual(d["rows"][0]["provider_id"], "unknown")


class TopEightTests(unittest.TestCase):
    def test_top_eight_other_conserves_metrics(self):
        m = Machine(self)
        specs = []
        for i in range(10):  # model-00 .. model-09 with strictly distinct token totals (descending by i)
            specs.append(S(f"m{i}", 1 + i % 3, f"model-{i:02d}", "anthropic", None, 1000 - i * 10, 100, 0.1 * (i + 1), 1, "anthropic"))
        specs.append(S("tie1", 1, "tie-b", "openai", None, 400, 0, 0.1, 1, "openai"))
        specs.append(S("tie2", 1, "tie-a", "openai", None, 400, 0, 0.1, 1, "openai"))   # equal total: id order
        specs.append(S("u1", 1, None, None, "auto", 5000, 0, 0.1, 1, "unknown"))       # Unknown model ranks normally
        specs.append(S("two", 2, "model-00", "nous", None, 1, 1, 0.1, 1, "nous"))      # same model, two providers
        specs.append(S("none", 2, "model-blank", "openai", None, None, None, 0.1, 1, "openai"))  # unknown tokens last
        m.write_db(None, specs)
        data = m.service().handle("usage", [("dimension", "model")])[1]["data"]
        groups = data["groups"]
        self.assertEqual(len(groups), 9)  # exactly eight ranked groups + Other
        self.assertEqual([g["is_other"] for g in groups], [False] * 8 + [True])
        ranked = [g["model_id"] for g in groups[:8]]
        self.assertEqual(ranked[0], None)  # 5000 tokens
        self.assertEqual(ranked[1:4], ["model-00", "model-01", "model-02"])
        # tie-a and tie-b both have 400: exact model id breaks it; they land in the tail/other, deterministically
        member_models = {r["model_id"] for r in data["rows"] if r["id"] in set(groups[-1]["row_ids"])}
        self.assertEqual(member_models & set(ranked), set())  # membership is disjoint from the ranked groups
        self.assertIn("model-blank", member_models)  # unknown-token model ranks after every numeric total
        # model-00 spans two providers: provider labels sorted, in parentheses
        self.assertEqual(groups[1]["label"], "model-00 (Anthropic, Nous Portal)")
        self.assertEqual(groups[-1]["label"], "Other")
        # Other's row ids cover the original models exactly once, and nothing is dropped from `rows`
        all_ids = sorted(r["id"] for r in data["rows"])
        self.assertEqual(sorted(rid for g in groups for rid in g["row_ids"]), all_ids)
        want = direct_totals(specs)
        assert_totals(self, totals_of(data), want)
        for field in ("input_count", "output_count", "sessions", "api_calls"):
            self.assertEqual(sum(g["totals"][field] or 0 for g in groups), want[field], field)
        # membership is constant over dates: Other's series days are the union of its members' days
        other_days = {p["day"] for p in data["series"][-1]["points"]}
        self.assertEqual(other_days, {r["day"] for r in data["rows"] if r["id"] in set(groups[-1]["row_ids"])})
        # a provider filter re-ranks AFTER filtering
        filtered = m.service().handle("usage", [("dimension", "model"), ("provider", "nous")])[1]["data"]
        self.assertEqual([g["model_id"] for g in filtered["groups"]], ["model-00"])
        # exactly eight models: no empty Other
        m2 = Machine(self)
        m2.write_db(None, [S(f"e{i}", 1, f"e-{i}", "openai", None, 10 + i, 1, 0.0, 1, "openai") for i in range(8)])
        eight = m2.service().handle("usage", [("dimension", "model")])[1]["data"]
        self.assertEqual((len(eight["groups"]), any(g["is_other"] for g in eight["groups"])), (8, False))


class AttributionTests(unittest.TestCase):
    def test_precedence_and_unknown_rules(self):
        catalog = {"claude-x": "anthropic", "local-x": "custom:box", "dup": None}
        # recorded routed provider wins (even over billing and over a conflicting catalog)
        self.assertEqual(attribute("claude-x", "openrouter", "nous", catalog), ("openrouter", "session_provider"))
        # billing provider next; bare auto/custom never count as a provider identity
        self.assertEqual(attribute("claude-x", None, "nous", catalog), ("nous", "billing_provider"))
        self.assertEqual(attribute("claude-x", "", "custom:box", catalog), ("custom:box", "billing_provider"))
        self.assertEqual(attribute("claude-x", "auto", "nous", catalog), ("nous", "billing_provider"))
        # catalog fallback only for model-only / bare-bucket sessions, exact model id, never a prefix guess
        self.assertEqual(attribute("claude-x", None, None, catalog), ("anthropic", "models_json"))
        self.assertEqual(attribute("local-x", None, "custom", catalog), ("custom:box", "models_json"))
        self.assertEqual(attribute("claude-x-2", None, "auto", catalog), ("unknown", "unknown"))
        self.assertEqual(attribute("claude-sonnet-5.5", None, None, {}), ("unknown", "unknown"))
        self.assertEqual(attribute(None, None, "custom", catalog), ("unknown", "unknown"))
        self.assertEqual(attribute("dup", None, None, catalog), ("unknown", "unknown"))  # ambiguous map
        # a literal recorded generic custom route is that route; whitespace/case are normalised
        self.assertEqual(attribute("m", " Custom ", None, {}), ("custom", "session_provider"))
        self.assertEqual(attribute("m", " Anthropic ", None, {}), ("anthropic", "session_provider"))
        self.assertEqual(attribute("m", "custom:Box", None, {}), ("custom:Box", "session_provider"))
        # secret-like / malformed provider text is never published
        for bad in ("sk-abcdefghijkl", "http://x.test/a?k=1", "a b", "x" * 200, "Bearer abcdefgh"):
            self.assertEqual(attribute("m", bad, "nous", {}), ("unknown", "unknown"), bad)

    def test_models_json_contract(self):
        m = Machine(self)
        home = m.home(None)
        path = home / "models.json"
        self.assertEqual(load_models_map(home), {})  # missing
        path.write_text(json.dumps({"models": [{"id": "a", "provider": "anthropic"}, {"id": "a", "provider": "anthropic"},
                                               {"id": "d", "provider": "nous"}, {"id": "d", "provider": "openai"},
                                               {"id": "c", "provider": "custom"}, {"id": "u", "provider": "auto"},
                                               {"id": "e", "provider": "custom:box"}]}))
        self.assertEqual(load_models_map(home), {"a": "anthropic", "d": None, "e": "custom:box"})  # dup collapses, conflict ambiguous, bare targets rejected
        for bad in ("not json", "[]", json.dumps({"models": "x"}), json.dumps({"models": [{"id": "a"}]}),
                    json.dumps({"models": [{"id": "a", "provider": "x", "extra": 1}]}),
                    json.dumps({"models": [], "extra": 1}), json.dumps({"models": [{"id": "", "provider": "x"}]}),
                    json.dumps({"models": [{"id": "a\x00b", "provider": "x"}]}),
                    json.dumps({"models": [{"id": "x" * 257, "provider": "p"}]})):
            path.write_text(bad)
            self.assertEqual(load_models_map(home), {}, bad[:40])
        path.write_text(json.dumps({"models": [{"id": "pad", "provider": "p" * 10}], "pad": "x" * MAX_MODELS_JSON_BYTES}))
        self.assertEqual(load_models_map(home), {})  # oversized
        path.unlink()
        real = home / "real.json"
        real.write_text(json.dumps({"models": [{"id": "a", "provider": "anthropic"}]}))
        path.symlink_to(real)
        self.assertEqual(load_models_map(home), {})  # symlinked catalog is never followed

    def test_models_json_is_scoped_to_its_own_home(self):
        m = Machine(self)
        m.write_db(None, [S("r", 1, "only-in-a", None, None, 10, 1, 0.0, 1, "unknown")])
        m.write_db("a", [S("a", 1, "only-in-a", None, None, 20, 1, 0.0, 1, "anthropic")])
        m.write_models("a", [{"id": "only-in-a", "provider": "anthropic"}])
        data = m.service().handle("usage", [("provider", "anthropic")])[1]["data"]
        self.assertEqual({(r["profile"], r["provider_id"]) for r in data["rows"]}, {("a", "anthropic")})
        self.assertEqual(data["totals"]["input_count"], 20)  # the root's identical model is NOT attributed from a's map
        # a recorded provider beats a conflicting map
        m.write_models("a", [{"id": "only-in-a", "provider": "nous"}])
        m2 = Machine(self)
        m2.write_db(None, [S("r", 1, "claude-z", "openrouter", None, 10, 1, 0.0, 1, "openrouter")])
        m2.write_models(None, [{"id": "claude-z", "provider": "anthropic"}])
        rows = m2.service().handle("usage", [])[1]["data"]["rows"]
        self.assertEqual([(r["provider_id"], r["attribution_source"]) for r in rows], [("openrouter", "session_provider")])


class ReadOnlyAndFailureTests(unittest.TestCase):
    def snapshot(self, machine, skip_shm=False):
        out = {}
        for path in sorted(machine.root.rglob("*")):
            if skip_shm and path.name.endswith("-shm"):
                continue  # the wal-index of a LIVE store holds reader marks: volatile by SQLite design
            if path.is_symlink():
                out[str(path)] = ("link", os.readlink(path))
            elif path.is_file():
                info = path.stat()
                out[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size, info.st_mtime_ns)
            else:
                out[str(path)] = ("dir", tuple(sorted(p.name for p in path.iterdir())))
        return out

    def test_home_union_is_read_only(self):
        m = Machine(self)
        seed_machine(m)
        m.write_db("idle", [S("i1", 1, "m", "openai", None, 1, 2, 0.5, 1, "openai")], idle_wal=True)
        self.assertEqual(sorted(p.name for p in m.home("idle").iterdir()), ["state.db"])  # WAL format, no sidecars
        before_idle = self.snapshot(m)
        idle_only = m.service(reader_factory=lambda: SessionDbReader()).handle(
            "usage", [("mode", "profile"), ("profile", "idle"), ("dimension", "model")])
        self.assertEqual(idle_only[0], 200)
        self.assertEqual(idle_only[1]["data"]["totals"]["input_count"], 1)
        # reading an idle WAL store must not create -wal/-shm sidecars (that would be a write to the home)
        self.assertEqual(self.snapshot(m), before_idle)
        writer = m.write_db("w", [S("w1", 1, "m", "openai", None, 11, 22, 0.5, 1, "openai")], wal=True)
        self.addCleanup(writer.close)
        before = self.snapshot(m, skip_shm=True)
        trace = []
        svc = m.service(reader_factory=lambda: SessionDbReader(trace=trace.append))
        for params in ([], [("dimension", "profile")], [("dimension", "model")], [("provider", "openai")],
                       [("mode", "profile"), ("profile", "a"), ("dimension", "model")]):
            status, body = svc.handle("usage", params)
            self.assertEqual(status, 200, body)
        self.assertTrue(trace)
        for stmt in trace:
            head = stmt.lstrip().split(None, 1)[0].upper()
            self.assertIn(head, ("SELECT", "PRAGMA", "BEGIN"), stmt)
            self.assertNotRegex(stmt.upper(), r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|VACUUM|ATTACH|REPLACE)\b")
            self.assertNotRegex(stmt.upper(), r"JOURNAL_MODE|WAL_CHECKPOINT|USER_VERSION\s*=")
        self.assertEqual(self.snapshot(m, skip_shm=True), before)  # DB/WAL bytes, sizes, mtimes, listings unchanged
        # committed WAL frames are visible without writable recovery
        data = svc.handle("usage", [("provider", "openai")])[1]["data"]
        self.assertIn("w", {r["profile"] for r in data["rows"]})
        self.assertTrue((m.home("w") / "state.db-wal").exists())

    def test_failure_modes_give_partial_machine_results(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        m.write_db("good", A_SESSIONS)
        (m.home("garbage")).mkdir()
        (m.home("garbage") / "state.db").write_bytes(b"this is not a sqlite database" * 100)   # malformed
        m.home("empty").mkdir()                                                                  # absent DB, no files at all
        m.write_db("oldschema", [], columns="id TEXT")                                          # no started_at: unusable
        outside = Path(tempfile.mkdtemp(prefix="usage-deck-outside-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, outside, True)
        sqlite3.connect(outside / "state.db").close()
        m.home("linked").mkdir()
        (m.home("linked") / "state.db").symlink_to(outside / "state.db")                        # symlinked DB
        (m.root / "profiles" / "linkdir").symlink_to(outside, target_is_directory=True)         # symlinked profile dir
        before = self.snapshot(m)
        status, body = m.service().handle("usage", [])
        self.assertEqual(status, 200, body)
        data = body["data"]
        states = {s["profile"]: s["state"] for s in data["sources"]}
        self.assertEqual(states, {"global": "ok", "good": "ok", "garbage": "unavailable", "empty": "absent",
                                  "oldschema": "unavailable", "linked": "excluded", "linkdir": "excluded"})
        self.assertEqual((data["complete"], data["reason"]), (False, "source_unavailable"))
        self.assertEqual([e["code"] for e in body["meta"]["errors"]], ["analytics_unavailable"])
        self.assertTrue(body["meta"]["partial"])
        # only readable homes are summed; nothing is guessed for the failed ones
        assert_totals(self, totals_of(data), direct_totals(ROOT_SESSIONS + A_SESSIONS))
        self.assertEqual(self.snapshot(m), before)
        text = json.dumps(body)
        for leak in (str(m.root), str(outside), "not a sqlite", "Traceback"):
            self.assertNotIn(leak, text)

    def test_nothing_readable_and_something_broken_is_503(self):
        m = Machine(self)
        (m.root / "state.db").write_bytes(b"garbage" * 64)
        status, body = m.service().handle("usage", [])
        self.assertEqual((status, body["error"]["code"]), (503, "analytics_unavailable"))
        self.assertEqual(body["meta"]["schema_version"], 2)
        # nothing at all (no DB anywhere) is a normal empty 200, not an error
        empty = Machine(self)
        status, body = empty.service().handle("usage", [])
        self.assertEqual(status, 200)
        self.assertEqual((body["data"]["reason"], body["data"]["rows"], body["data"]["complete"]), ("no_observations", [], True))
        self.assertEqual([s["state"] for s in body["data"]["sources"]], ["absent"])

    def test_profile_mode_is_strict_about_its_own_store(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        m.home("bad").mkdir()
        (m.home("bad") / "state.db").write_bytes(b"garbage" * 64)
        svc = m.service()
        status, body = svc.handle("usage", [("mode", "profile"), ("profile", "bad"), ("dimension", "model")])
        self.assertEqual((status, body["error"]["code"]), (503, "analytics_unavailable"))
        # an unreadable sibling does not matter to a healthy profile
        m.write_db("ok", A_SESSIONS)
        status, body = svc.handle("usage", [("mode", "profile"), ("profile", "ok"), ("dimension", "model")])
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["complete"])

    def test_locked_database_is_unavailable_not_hung(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        m.write_db("locked", A_SESSIONS)
        blocker = sqlite3.connect(m.home("locked") / "state.db", isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        self.addCleanup(blocker.close)
        svc = m.service(reader_factory=lambda: SessionDbReader(busy_timeout=0.05))
        status, body = svc.handle("usage", [])
        self.assertEqual(status, 200)
        self.assertEqual({s["profile"]: s["state"] for s in body["data"]["sources"]}, {"global": "ok", "locked": "unavailable"})
        assert_totals(self, totals_of(body["data"]), direct_totals(ROOT_SESSIONS))

    def test_hard_linked_duplicate_db_is_excluded_not_double_counted(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        m.home("twin").mkdir()
        os.link(m.root / "state.db", m.home("twin") / "state.db")
        data = m.service().handle("usage", [])[1]["data"]
        self.assertEqual({s["profile"]: s["state"] for s in data["sources"]}, {"global": "ok", "twin": "excluded"})
        self.assertFalse(data["complete"])
        assert_totals(self, totals_of(data), direct_totals(ROOT_SESSIONS))  # the root wins; counted once

    def test_over_sixty_four_profiles_is_incomplete_not_silently_truncated(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        for i in range(MAX_PROFILES + 2):
            m.home(f"p{i:03d}").mkdir()
        data = m.service().handle("usage", [])[1]["data"]
        self.assertEqual(len(data["sources"]), 1 + MAX_PROFILES)
        self.assertFalse(data["complete"])
        self.assertEqual(data["reason"], "source_unavailable")

    def test_symlinked_profiles_parent_is_never_followed(self):
        m = Machine(self)
        m.write_db(None, ROOT_SESSIONS)
        other = Path(tempfile.mkdtemp(prefix="usage-deck-profiles-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, other, True)
        (other / "evil").mkdir()
        sqlite3.connect(other / "evil" / "state.db").close()
        (m.root / "profiles").rmdir()
        (m.root / "profiles").symlink_to(other, target_is_directory=True)
        data = m.service().handle("usage", [])[1]["data"]
        self.assertEqual([s["profile"] for s in data["sources"]], ["global"])
        self.assertFalse(data["complete"])

    def test_principal_clamp_and_query_isolation(self):
        m = Machine(self)
        seed_machine(m)
        opened = []

        class Spy(SessionDbReader):
            def read_groups(self, directory, cutoff):
                opened.append(Path(directory).name)
                return super().read_groups(directory, cutoff)

        svc = m.service(home="a", reader_factory=Spy)
        # a profile-restricted principal asking for machine + by-profile is clamped to profile mode / All
        status, body = svc.handle("usage", [("mode", "machine"), ("dimension", "profile")], profile_only=True)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["data"]["mode"], body["data"]["dimension"]), ("profile", "all"))
        status, body = svc.handle("usage", [("view", "machine")], profile_only=True)
        self.assertEqual(body["data"]["mode"], "profile")
        assert_totals(self, totals_of(body["data"]), direct_totals(A_SESSIONS))
        self.assertEqual(set(opened) - {"a"}, set())  # never opened another home's DB
        # both aliases share one computation and response shape
        one = svc.handle("usage", [("view", "machine"), ("window", "7")])[1]["data"]
        two = svc.handle("usage", [("mode", "machine"), ("window", "7")])[1]["data"]
        self.assertEqual(one, two)
        # every query component separates cache entries
        seen = []
        for params in ([], [("dimension", "profile")], [("dimension", "model")], [("window", "7")], [("window", "90")],
                       [("provider", "anthropic")], [("mode", "profile")], [("mode", "profile"), ("dimension", "model")]):
            data = svc.handle("usage", params)[1]["data"]
            seen.append((data["mode"], data["dimension"], data["window_days"], data["provider_id"]))
        self.assertEqual(len(set(seen)), len(seen))
        # editing the optional catalog invalidates the cached result (identity includes its mtime/size)
        before = svc.handle("usage", [("provider", "custom:gpu")])[1]["data"]["totals"]["input_count"]
        self.assertEqual(before, 50)
        m.write_models("a", [{"id": "mapped-m", "provider": "custom:other"}])
        status, body = svc.handle("usage", [("provider", "custom:gpu")])
        self.assertEqual(status, 404)  # the map changed: the old attribution is not served from cache

    def test_no_secret_or_path_in_usage_payload(self):
        m = Machine(self)
        m.write_db(None, [S("k", 1, "sk-abcdefghijklmnop", "sk-zzzzzzzzzzzz", None, 5, 5, 0.1, 1, "unknown"),
                          S("u", 1, "http://user:pw@host/x?token=abc", None, "nous", 7, 7, 0.1, 1, "nous")])
        status, body = m.service().handle("usage", [("dimension", "model")])
        self.assertEqual(status, 200)
        text = json.dumps(body)
        for needle in ("sk-abcdefghijklmnop", "sk-zzzzzzzzzzzz", "user:pw", "token=abc", str(m.root)):
            self.assertNotIn(needle, text)
        self.assertEqual(scan_for(body, ["sk-"]), [])
        self.assertEqual({g["model_id"] for g in body["data"]["groups"]}, {None})  # unpublishable ids collapse to Unknown model


class AuthorizedHomeTests(unittest.TestCase):
    """A profile-restricted principal is bound to the host-authorized home: ``profile=`` can name only that home."""

    SIBLING_MARK = 777777777

    def setUp(self):
        self.m = Machine(self)
        seed_machine(self.m)
        # distinctive sibling/root rows: if any leaks, these huge counts show up in the totals
        for name in (None, "b"):
            conn = sqlite3.connect(self.m.home(name) / "state.db")
            conn.execute("INSERT INTO sessions (id, model, provider, started_at, input_tokens, output_tokens, "
                         "estimated_cost_usd, api_call_count) VALUES ('big', 'x', 'openai', ?, ?, 1, 5.0, 3)",
                         (NOW_TS - DAY, self.SIBLING_MARK))
            conn.commit()
            conn.close()
        self.opened = []
        self.analytics_profiles = []
        opened = self.opened

        class Spy(SessionDbReader):
            def read_groups(self, directory, cutoff):
                opened.append(Path(directory).resolve())
                return super().read_groups(directory, cutoff)

        self.Spy = Spy

    def service(self, home):
        svc = self.m.service(home=home, reader_factory=self.Spy)
        inner = svc._analytics_factory
        seen = self.analytics_profiles

        def recording(profile):
            seen.append(profile)
            return inner(profile)

        svc._analytics_factory = recording
        return svc

    def assert_only_home(self, home):
        allowed = {self.m.home(home).resolve()}
        self.assertEqual(set(self.opened) - allowed, set())
        self.assertTrue(all(p is None for p in self.analytics_profiles), self.analytics_profiles)

    def test_profile_principal_cannot_select_sibling_or_root(self):
        svc = self.service("a")
        for selector in ("b", "default", "DEFAULT", "global", "A", "nope"):
            for params in ([], [("mode", "machine")], [("dimension", "model")],
                           [("provider", "openai")], [("window", "7")]):
                with self.subTest(selector=selector, params=params):
                    status, body = svc.handle("usage", [("profile", selector), *params], profile_only=True)
                    self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"), body)
                    self.assertNotIn(str(self.SIBLING_MARK), json.dumps(body))
        for endpoint in ("subscriptions", "health"):
            for selector in ("b", "default"):
                status, body = svc.handle(endpoint, [("profile", selector)], profile_only=True)
                self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"), (endpoint, selector))
        self.assertEqual(self.opened, [])  # refused before any home was opened
        self.assertEqual(self.analytics_profiles, [])

    def test_own_selector_is_bound_to_the_authorized_home(self):
        svc = self.service("a")
        for window in (7, 30, 90):
            base = svc.handle("usage", [("window", str(window))], profile_only=True)[1]["data"]
            assert_totals(self, totals_of(base), direct_totals(A_SESSIONS, window))
            for selector in ("a", "current", "CURRENT"):
                for dimension in ("all", "model"):
                    with self.subTest(window=window, selector=selector, dimension=dimension):
                        params = [("profile", selector), ("dimension", dimension), ("window", str(window))]
                        status, body = svc.handle("usage", params, profile_only=True)
                        self.assertEqual(status, 200, body)
                        self.assertEqual(body["data"]["mode"], "profile")
                        assert_totals(self, totals_of(body["data"]), direct_totals(A_SESSIONS, window))
        # machine/by-profile asks keep clamping to this home even alongside an own-home selector
        status, body = svc.handle("usage", [("profile", "a"), ("mode", "machine"), ("dimension", "profile")],
                                  profile_only=True)
        self.assertEqual((status, body["data"]["mode"], body["data"]["dimension"]), (200, "profile", "all"))
        self.assertEqual({s["profile"] for s in body["data"]["sources"]}, {"a"})
        self.assert_only_home("a")

    def test_root_principal_may_name_default_but_not_a_profile(self):
        svc = self.service(None)
        for selector in ("a", "b"):
            status, body = svc.handle("usage", [("profile", selector)], profile_only=True)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_query"), selector)
        self.assertEqual(self.opened, [])
        big = S("big", 1, "x", "openai", None, self.SIBLING_MARK, 1, 5.0, 3, "openai")
        expected = direct_totals(ROOT_SESSIONS + [big])
        for selector in ("default", "DEFAULT", "current"):
            status, body = svc.handle("usage", [("profile", selector)], profile_only=True)
            self.assertEqual(status, 200, body)
            assert_totals(self, totals_of(body["data"]), expected)
        self.assert_only_home(None)

    def test_rejection_does_not_poison_the_cache(self):
        svc = self.service("a")
        first = svc.handle("usage", [], profile_only=True)[1]["data"]
        self.assertEqual(svc.handle("usage", [("profile", "b")], profile_only=True)[0], 400)
        self.assertEqual(svc.handle("usage", [], profile_only=True)[1]["data"], first)
        assert_totals(self, totals_of(first), direct_totals(A_SESSIONS))

    def test_unrestricted_dashboard_session_still_selects_any_home(self):
        svc = self.service("a")
        status, body = svc.handle("usage", [("profile", "b"), ("mode", "profile")])
        self.assertEqual(status, 200, body)
        self.assertEqual(totals_of(body["data"])["input_count"], 123 + self.SIBLING_MARK)


class UsageSchemaGuardTests(unittest.TestCase):
    """The v2 usage payload is validated exactly like the legacy shapes: no unknown keys, no bad enums."""

    def setUp(self):
        m = Machine(self)
        seed_machine(m)
        status, self.body = m.service().handle("usage", [("dimension", "model")])
        self.assertEqual(status, 200)

    def mutated(self, fn):
        body = json.loads(json.dumps(self.body))
        fn(body["data"])
        return body

    def test_control_passes_and_every_mutation_is_rejected(self):
        check_envelope(self.body)
        mutations = {
            "extra top-level key": lambda d: d.update(extra=1),
            "missing top-level key": lambda d: d.pop("sources"),
            "extra group key": lambda d: d["groups"][0].update(extra=1),
            "extra row key": lambda d: d["rows"][0].update(session_id="x"),
            "extra metrics key": lambda d: d["totals"].update(cached=1),
            "extra source key": lambda d: d["sources"][0].update(path="/x"),
            "extra provider key": lambda d: d["providers"][0].update(url="x"),
            "bad mode": lambda d: d.update(mode="all"),
            "bad dimension": lambda d: d.update(dimension="family"),
            "bad attribution": lambda d: d["rows"][0].update(attribution_source="guess"),
            "bad source state": lambda d: d["sources"][0].update(state="error"),
            "bad row source": lambda d: d["rows"][0].update(source="/home/x/state.db"),
            "bad reason": lambda d: d.update(reason="attribution_unavailable"),
            "top_n wrong": lambda d: d.update(top_n=5),
            "complete not bool": lambda d: d.update(complete=1),
            "negative count": lambda d: d["totals"].update(sessions=-1),
            "float count": lambda d: d["totals"].update(input_count=1.5),
            "unsorted missing_fields": lambda d: d["totals"].update(missing_fields=["output_count", "input_count"]),
            "unknown missing field": lambda d: d["totals"].update(missing_fields=["cache_read"]),
            "orphan row": lambda d: d["groups"][0]["row_ids"].pop(),
            "duplicated row": lambda d: d["groups"][1]["row_ids"].append(d["groups"][0]["row_ids"][0]),
            "series/groups mismatch": lambda d: d["series"].pop(),
            "bad scope": lambda d: d["series"][0].update(scope="provider_x"),
            "other flag mismatch": lambda d: d["groups"][0].update(is_other=True),
            "renamed key": lambda d: d.__setitem__("lab[REDACTED]", d.pop("groups")),
        }
        for name, fn in mutations.items():
            with self.subTest(name):
                with self.assertRaises(DtoError):
                    check_envelope(self.mutated(fn))

    def test_usage_is_v2_everything_else_stays_v1(self):
        self.assertEqual(self.body["meta"]["schema_version"], 2)
        bad = json.loads(json.dumps(self.body))
        bad["meta"]["schema_version"] = 1
        with self.assertRaises(DtoError):
            check_envelope(bad)  # a usage payload under the legacy version is rejected
        m = Machine(self)
        seed_machine(m)
        svc = m.service()
        for endpoint in ("subscriptions", "health"):
            status, body = svc.handle(endpoint, [])
            self.assertEqual((status, body["meta"]["schema_version"]), (200, 1), endpoint)
        status, body = svc.handle("subscriptions", [("window", "7")])
        self.assertEqual((status, body["meta"]["schema_version"]), (400, 1))

    def test_new_wire_vocabulary_survives_every_colliding_secret(self):
        sample = {"mode": "machine", "dimension": "model", "attribution_source": "billing_provider",
                  "source": "state.db", "state": "unavailable", "profile": "verifier", "reason": "source_unavailable",
                  "scope": "machine", "day": "2026-11-06"}
        for secret in sorted({c for c in json.dumps(sample)} | {"machine", "model", "state", "billing", "unavailable"}):
            with self.subTest(secret=secret):
                self.assertEqual(sanitize(sample, {secret}), sample)


if __name__ == "__main__":
    unittest.main()
