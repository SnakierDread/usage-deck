"""Analytics depth through the service: repository dimension, per-group durations, heatmap, partial sources,
read-only proof (Gate 6 / C5), authorized-home binding, hostile paths, baseline parity (C6) and decoder guards.

Design: docs/design/analytics-depth-breakdown.md §6, §9. Expectations are derived from the fixture specs in
this harness (independent implementations of basename/percentile/bucketing), never from ``analytics_depth``.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from plugins.usage_deck.adapters import analytics_depth as depth
from plugins.usage_deck.adapters.session_db import SessionDbReader
from plugins.usage_deck.models.dto import DtoError, check_envelope, usage_from_wire

from . import depth_fixture as fx
from .depth_fixture import A, B, D, NOW_TS, ROOT, Machine, utc
from .test_analytics_depth import NO_NET, DepthServiceBase, known, spec_window, want_matches

GOLDEN = Path(__file__).parent / "fixtures" / "depth_parity_golden.json"


def h_basename(root):
    """Harness copy of the contract (design §6): trim, strip trailing separators, split on / and \\."""
    if not isinstance(root, str):
        return None
    text = root.strip().rstrip("/\\")
    if not text:
        return None
    base = re.split(r"[\\/]", text)[-1]
    return base if base.strip() else None


def h_quantiles(samples):
    samples = sorted(samples)
    n = len(samples)
    if n == 0:
        return None
    if n == 1:
        return (samples[0],) * 3
    q = statistics.quantiles(samples, n=100, method="inclusive")
    return q[49], q[89], q[98]


def h_durations(specs):
    return [s["ended_at"] - s["started_at"] for s in specs if s["ended_at"] is not None and s["ended_at"] >= s["started_at"]]


def assert_stats(case, stats, n, samples):
    case.assertEqual(n, len(samples))
    if not samples:
        case.assertEqual(stats, dict.fromkeys(("p50", "p90", "p99", "mean", "max")))
        return
    want = h_quantiles(samples)
    for key, exp in zip(("p50", "p90", "p99"), want):
        case.assertAlmostEqual(stats[key], exp, places=6, msg=key)
    case.assertAlmostEqual(stats["mean"], sum(samples) / len(samples), places=6)
    case.assertEqual(stats["max"], max(samples))


class RepoDimensionTests(DepthServiceBase):
    SESSIONS_BY_REPO = {
        "usage-deck": {"r1", "r2", "r3", "r9", "r10", "a1", "a14", "b1", "b2"},
        "Unscoped": {"r4"},
        "alpha": {"a2", "a3", "a5"}, "beta": {"a6", "a7"}, "gamma": {"a8", "a9"}, "delta": {"a10", "a11"},
        "epsilon": {"a12", "a13"}, "Usage-Deck": {"a15"},
        None: {"r5", "r6", "r7", "r8", "a4"},
    }

    def specs_by_repo(self):
        out = {}
        for s in spec_window(self.all_specs(), 30):
            out.setdefault(h_basename(s["git_repo_root"]), []).append(s)
        return out

    def test_membership_matches_the_contract_and_the_specs(self):
        by_repo = self.specs_by_repo()
        self.assertEqual({k: {s["id"] for s in v} for k, v in by_repo.items()}, self.SESSIONS_BY_REPO)
        data = self.get([("mode", "machine"), ("dimension", "repo")])
        self.assertEqual((data["dimension"], data["top_n"]), ("repo", None))
        self.assertFalse(any(g["is_other"] for g in data["groups"]))  # no Other for repositories
        ids = [g["id"] for g in data["groups"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[-1], "repo:null")  # the missing bucket sorts last, whatever its volume
        self.assertIn('repo:"Unscoped"', ids)   # the literal repository is its own group
        expected_ids = {"repo:null" if k is None else "repo:" + json.dumps(k) for k in by_repo}
        self.assertEqual(set(ids), expected_ids)
        by_id = {g["id"]: g for g in data["groups"]}
        for key, specs in by_repo.items():
            gid = "repo:null" if key is None else "repo:" + json.dumps(key)
            self.assertEqual(by_id[gid]["totals"]["sessions"], len(specs), key)
            self.assertEqual(by_id[gid]["totals"]["input_count"], known(s["input_tokens"] for s in specs), key)
            self.assertEqual(by_id[gid]["label"], "Unscoped" if key is None else key)
            self.assertIsNone(by_id[gid]["model_id"])
        # order: known input+output desc, unknown last, then lexical, missing bucket last

        def tokens(key):
            vals = [None if s["input_tokens"] is None and s["output_tokens"] is None
                    else (s["input_tokens"] or 0) + (s["output_tokens"] or 0) for s in by_repo[key]]
            return known(vals)

        want_order = sorted(by_repo, key=lambda k: (k is None, tokens(k) is None, -(tokens(k) or 0), k or ""))
        self.assertEqual(ids, ["repo:null" if k is None else "repo:" + json.dumps(k) for k in want_order])

    def test_collisions_combine_and_every_path_is_inspectable(self):
        data = self.get([("mode", "machine"), ("dimension", "repo")])
        rollup = {(r["repo_basename"], bool(r["full_paths"])): r for r in data["repo_rollup"]}
        deck = rollup[("usage-deck", True)]
        self.assertEqual(deck["full_paths"], sorted(["/home/u/other/usage-deck", "/work/usage-deck",
                                                     "C:\\Users\\dev\\usage-deck"]))
        self.assertIsNone(deck["full_path"])
        self.assertEqual(rollup[("Unscoped", True)]["full_path"], "/srv/Unscoped")  # literal repo, one path
        self.assertEqual(rollup[("Unscoped", False)]["full_paths"], [])             # the missing bucket
        self.assertIsNone(rollup[("Unscoped", False)]["full_path"])
        self.assertEqual(rollup[("Usage-Deck", True)]["full_path"], "/work/Usage-Deck")  # case is preserved
        self.assertEqual(len(data["repo_rollup"]), len(self.SESSIONS_BY_REPO))
        rows = [r for r in data["rows"] if r["repo_basename"] == "usage-deck"]
        self.assertTrue(rows and all(isinstance(r["repo_paths"], list) for r in rows))
        self.assertTrue(all(r["repo_basename"] is None and r["repo_paths"] == []
                            for r in data["rows"] if r["repo_basename"] is None))

    def test_legacy_totals_and_money_are_conserved_with_the_other_dimensions(self):
        base = self.get([("mode", "machine"), ("dimension", "all")])
        repo = self.get([("mode", "machine"), ("dimension", "repo")])
        for field in ("input_count", "output_count", "sessions", "api_calls"):
            self.assertEqual(repo["totals"][field], base["totals"][field], field)
        self.assertAlmostEqual(repo["totals"]["estimated_usd"], base["totals"]["estimated_usd"], places=9)
        # API-equivalent amounts are exact integers: splitting leaves by repo must not change them
        self.assertEqual(repo["totals"]["api_equivalent"]["amount_pico_usd"], base["totals"]["api_equivalent"]["amount_pico_usd"])
        amounts = [int(g["totals"]["api_equivalent"]["amount_pico_usd"]) for g in repo["groups"]
                   if g["totals"]["api_equivalent"]["amount_pico_usd"] is not None]
        self.assertEqual(sum(amounts), int(base["totals"]["api_equivalent"]["amount_pico_usd"]))
        # new leaves carry new ids: no collision with the rows of any other dimension
        model = self.get([("mode", "machine"), ("dimension", "model")])
        self.assertFalse({r["id"] for r in repo["rows"]} & {r["id"] for r in model["rows"]})
        self.assertEqual(sorted(rid for g in repo["groups"] for rid in g["row_ids"]), sorted(r["id"] for r in repo["rows"]))
        for g, s in zip(repo["groups"], repo["series"]):
            self.assertEqual(g["series_id"], s["id"])
            self.assertEqual(sum(p["sessions"] or 0 for p in s["points"]), g["totals"]["sessions"])

    def test_other_dimensions_row_ids_are_byte_identical_to_the_baseline_shape(self):
        model = self.get([("mode", "machine"), ("dimension", "model")])
        self.assertTrue(all(r["repo_basename"] is None and r["repo_paths"] is None for r in model["rows"]))
        self.assertTrue(all(re.match(r"^row_[0-9a-f]{64}$", r["id"]) for r in model["rows"]))

    def test_provider_filter_and_profile_mode(self):
        data = self.get([("mode", "machine"), ("dimension", "repo"), ("provider", "openai")])
        specs = [s for s in spec_window(self.all_specs(), 30) if s["want"] == "openai"]
        self.assertEqual(sum(g["totals"]["sessions"] for g in data["groups"]), len(specs))
        self.assertEqual(data["depth_totals"]["sessions"], len(specs))
        single = self.get([("mode", "profile"), ("dimension", "repo")], home="a")
        self.assertEqual(single["mode"], "profile")
        self.assertEqual(single["depth_totals"]["sessions"], len(spec_window(A, 30)))
        self.assertEqual(sum(g["totals"]["sessions"] for g in single["groups"]), len(spec_window(A, 30)))
        self.assertEqual({p["profile"] for p in [{"profile": r["profile"]} for r in single["rows"]]}, {"a"})


class DurationServiceTests(DepthServiceBase):
    def test_overall_durations_come_from_raw_samples_only(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        specs = spec_window(self.all_specs(), 30)
        samples = h_durations(specs)
        self.assertLess(len(samples), len(specs))  # open + negative sessions are excluded
        assert_stats(self, data["durations"], data["duration_samples"], samples)
        # r3 is open with a far-future last_activity_at: it must not become a sample
        self.assertNotIn(max(s["last_activity_at"] - s["started_at"] for s in specs), samples)

    def test_every_group_uses_its_own_members_including_model_other(self):
        data = self.get([("mode", "machine"), ("dimension", "model")])
        specs = spec_window(self.all_specs(), 30)
        kept = {json.loads(g["id"][len("model:"):]) for g in data["groups"] if g["id"].startswith("model:")}
        other = [g for g in data["groups"] if g["is_other"]][0]
        other_specs = [s for s in specs if s["model"] not in kept]
        self.assertGreater(len(other_specs), 1)
        assert_stats(self, other["durations"], other["duration_samples"], h_durations(other_specs))
        # a percentile of percentiles would differ: prove the Other stats are not derived from group stats
        for g in data["groups"]:
            if g["is_other"]:
                continue
            members = [s for s in specs if s["model"] == json.loads(g["id"][len("model:"):])]
            assert_stats(self, g["durations"], g["duration_samples"], h_durations(members))
        self.assertEqual(sum(g["duration_samples"] for g in data["groups"]), data["duration_samples"])

    def test_repo_and_profile_groups(self):
        specs = spec_window(self.all_specs(), 30)
        repo = self.get([("mode", "machine"), ("dimension", "repo")])
        for g in repo["groups"]:
            key = None if g["id"] == "repo:null" else json.loads(g["id"][len("repo:"):])
            members = [s for s in specs if h_basename(s["git_repo_root"]) == key]
            assert_stats(self, g["durations"], g["duration_samples"], h_durations(members))
        profile = self.get([("mode", "machine"), ("dimension", "profile")])
        self.assertEqual(len(profile["groups"]), 3)
        self.assertEqual(sum(g["duration_samples"] for g in profile["groups"]), profile["duration_samples"])

    def test_profile_mode_all_group_carries_the_home_durations(self):
        data = self.get([("mode", "profile"), ("dimension", "all")], home="a")
        samples = h_durations(spec_window(A, 30))
        assert_stats(self, data["durations"], data["duration_samples"], samples)
        assert_stats(self, data["groups"][0]["durations"], data["groups"][0]["duration_samples"], samples)


class HeatmapServiceTests(DepthServiceBase):
    def test_grid_equals_an_independent_utc_bucketing_of_the_specs(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        specs = spec_window(self.all_specs(), 30)
        want = [[{"sessions": 0, "tokens": None} for _ in range(24)] for _ in range(7)]
        for s in specs:
            moment = datetime.fromtimestamp(s["started_at"], timezone.utc)
            cell = want[moment.weekday()][moment.hour]
            cell["sessions"] += 1
            if s["input_tokens"] is not None or s["output_tokens"] is not None:
                cell["tokens"] = (cell["tokens"] or 0) + (s["input_tokens"] or 0) + (s["output_tokens"] or 0)
        got = [[{"sessions": c["sessions"], "tokens": c["total_tokens"]} for c in row] for row in data["heatmap"]["grid"]]
        self.assertEqual(got, want)
        self.assertEqual(sum(c["sessions"] for row in data["heatmap"]["grid"] for c in row), len(specs))
        self.assertEqual({s["started_at"] for s in data["heatmap"]["starts"]}, {s["started_at"] for s in specs})

    def test_midnight_spanning_session_lands_in_exactly_one_bucket(self):
        m = Machine(self)
        spec = D("x1", 0, "m", "openai", 40, 2, 0.5, "/w/x", started=utc(2026, 11, 5, 23, 59), ended=utc(2026, 11, 6, 0, 10))
        m.write_db(None, [spec], columns=fx.DEPTH_COLUMNS)
        data = self.get([("mode", "profile"), ("dimension", "all")], svc=m.service())
        cells = {(d, h): c for d, row in enumerate(data["heatmap"]["grid"]) for h, c in enumerate(row) if c["sessions"]}
        self.assertEqual(cells, {(3, 23): {"sessions": 1, "total_tokens": 42}})  # Thu 23, whole tokens, never split
        self.assertEqual(data["durations"]["p50"], 11 * 60)
        self.assertEqual(data["heatmap"]["starts"], [{"started_at": utc(2026, 11, 5, 23, 59), "sessions": 1, "total_tokens": 42}])


def luna_table(cache_read=None):
    """The packaged table where openrouter prices ``gpt-6-luna`` exactly like openai does (the fixture routes
    this model through both, and a merged per-model row needs every resolved provider priced to weight it)."""
    doc = json.loads((depth.PACKAGE_DATA / depth.RATE_FILE).read_bytes())
    clone = None
    for entry in doc["entries"]:
        if (entry["provider_id"], entry["model"]) == ("openai", "gpt-6-luna"):
            if cache_read is not None:
                entry["cache_read_cost_per_million"] = cache_read
            clone = dict(entry, provider_id="openrouter")
    doc["entries"].append(clone)
    return depth.parse_rate_table(json.dumps(doc).encode())


class PartialSourceTests(DepthServiceBase):
    def test_unreadable_home_is_disclosed_and_the_rest_is_conserved(self):
        broken = self.m.home("c")
        broken.mkdir(parents=True)
        (broken / "state.db").write_bytes(b"this is not a sqlite database at all" * 20)
        svc = self.m.service()
        svc._depth_rates_factory = luna_table  # one model fully priced, so incomplete_source is the first reason
        data = self.get([("mode", "machine"), ("dimension", "all")], svc=svc)
        self.assertIs(data["complete"], False)
        self.assertIn("unavailable", {s["state"] for s in data["sources"]})
        self.assertIs(data["depth_status"]["partial"], True)
        observed = spec_window(self.all_specs(), 30)
        self.assertEqual(data["depth_totals"]["sessions"], len(observed))  # same observed subset, flagged partial
        self.assertEqual(sum(r["sessions"] for r in data["repo_rollup"]), len(observed))
        self.assertEqual(sum(r["sessions"] for r in data["model_rollup"]), len(observed))
        for row in data["cache_metrics"]:  # ratios are never computed over a partially unreadable population
            self.assertIsNone(row["hit_rate_pct"])
            self.assertIsNone(row["effective_cost_per_mtok"])
            self.assertIn(row["reason"], ("incomplete_source", "missing_usage", "missing_cost", "unknown_discount",
                                          "zero_denominator"))
            self.assertIsNotNone(row["reason"])
        self.assertTrue(any(r["reason"] == "incomplete_source" for r in data["cache_metrics"]))
        self.assertEqual([c["uncached_input_tokens"] for c in data["cache_metrics"] if c["model"] == "m-01"], [100])
        self.assertEqual(len({r["model"] for r in data["cache_metrics"]}), len(data["cache_metrics"]))  # one row per model

    def test_only_unreadable_homes_make_every_depth_member_null(self):
        m = Machine(self)
        (m.home(None)).mkdir(parents=True, exist_ok=True)
        (m.root / "state.db").write_bytes(b"garbage" * 50)
        svc = m.service()
        with NO_NET:
            status, body = svc.handle("usage", [("mode", "machine"), ("dimension", "all")])
        self.assertEqual(status, 503)  # nothing readable and something broken: the existing contract

    def test_failed_projection_alone_degrades_depth_not_the_counters(self):
        class NoFactsForA(SessionDbReader):
            def read_groups_with_facts(self, directory, cutoff):
                both = super().read_groups_with_facts(directory, cutoff)
                if both is not None and Path(directory).name == "a":
                    return both[0], None
                return both

        svc = self.m.service(reader_factory=NoFactsForA)
        base = self.get([("mode", "machine"), ("dimension", "all")])
        data = self.get([("mode", "machine"), ("dimension", "all")], svc=svc)
        self.assertIs(data["complete"], True)  # the legacy counters are complete
        self.assertEqual(data["totals"]["sessions"], base["totals"]["sessions"])
        self.assertIs(data["depth_status"]["partial"], True)
        self.assertEqual(data["depth_totals"]["sessions"], base["depth_totals"]["sessions"] - len(spec_window(A, 30)))
        self.assertTrue(all(r["hit_rate_pct"] is None for r in data["cache_metrics"]))
        repo = self.get([("mode", "machine"), ("dimension", "repo")], svc=svc)
        self.assertIs(repo["complete"], False)  # the repo view IS the facts: honest incompleteness
        self.assertEqual(repo["reason"], "source_unavailable")
        with NO_NET:
            status, body = self.m.service(home="a", reader_factory=NoFactsForA).handle(
                "usage", [("mode", "profile"), ("dimension", "repo")])
        self.assertEqual(status, 503)
        profile_all = self.get([("mode", "profile"), ("dimension", "all")], svc=self.m.service(home="a", reader_factory=NoFactsForA))
        self.assertIsNone(profile_all["heatmap"])
        self.assertIsNone(profile_all["depth_totals"])
        self.assertIs(profile_all["depth_status"]["partial"], True)
        self.assertIsNone(profile_all["depth_status"]["excluded_started_at"])
        self.assertEqual(profile_all["totals"]["sessions"], len(spec_window(A, 30)))  # profile numbers do not shift

    def test_missing_optional_columns_are_unavailable_not_zero(self):
        m = Machine(self)
        columns = "id TEXT, source TEXT, model TEXT, provider TEXT, billing_provider TEXT, started_at REAL"
        m.write_db(None, [D("n1", 1, "claude-x", "anthropic", None, None, None)], columns=columns)
        data = self.get([("mode", "machine"), ("dimension", "all")], svc=m.service())
        self.assertEqual(data["depth_totals"], {"sessions": 1, "total_tokens": None, "total_cost": None})
        self.assertEqual(data["durations"], dict.fromkeys(("p50", "p90", "p99", "mean", "max")))
        self.assertEqual(data["duration_samples"], 0)
        self.assertEqual(data["repo_rollup"][0]["sessions"], 1)  # no git_repo_root column: Unscoped, not an error
        row = data["cache_metrics"][0]
        self.assertEqual((row["cached_input_tokens"], row["hit_rate_pct"], row["effective_cost_per_mtok"]), (None, None, None))
        self.assertEqual(row["reason"], "missing_usage")
        repo = self.get([("mode", "profile"), ("dimension", "repo")], svc=m.service())
        self.assertEqual([g["id"] for g in repo["groups"]], ["repo:null"])


def tree_state(root, volatile=("state.db-shm",)):
    """Everything a plugin write could change in a home tree: names, sizes, mtimes and content hashes.

    A WAL ``-shm`` index is shared memory that EVERY reader updates (read marks): its bytes are volatile by
    design, so only its presence/size is compared."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.name in volatile:
            out[rel] = (info.st_size, None, None)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
        out[rel] = (info.st_size, info.st_mtime_ns, digest)
    return out


class ReadOnlyTests(DepthServiceBase):
    FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|ATTACH|REINDEX|ANALYZE)\b|"
                           r"PRAGMA\s+(journal_mode|wal|synchronous|user_version\s*=|writable_schema)", re.I)
    ALLOWED = re.compile(r"^\s*(BEGIN|PRAGMA query_only = ON|PRAGMA table_info\(sessions\)|SELECT )", re.I)

    def test_gate6_checksum_no_state_db_is_written_by_any_dimension(self):
        before = tree_state(self.m.root)
        self.assertTrue(any(rel.endswith("state.db") for rel in before))
        for window in ("7", "30", "90"):
            for dimension in ("all", "profile", "model", "repo"):
                self.get([("mode", "machine"), ("dimension", dimension), ("window", window)])
        for home in (None, "a", "b"):
            for dimension in ("all", "model", "repo"):
                self.get([("mode", "profile"), ("dimension", dimension)], home=home)
        after = tree_state(self.m.root)
        self.assertEqual(after, before)  # sizes, mtimes, bytes and the set of files (no -wal/-shm/-journal appeared)
        # per-DB content checksum, the Gate-6 evidence form
        for rel in (r for r in before if r.endswith("state.db")):
            self.assertEqual(after[rel][2], before[rel][2], rel)

    def test_every_statement_is_read_only_and_one_snapshot_serves_both_projections(self):
        statements = []
        reader = lambda: SessionDbReader(trace=statements.append)  # noqa: E731
        self.get([("mode", "machine"), ("dimension", "repo")], svc=self.m.service(reader_factory=reader))
        self.assertTrue(statements)
        for sql in statements:
            self.assertRegex(sql, self.ALLOWED, sql[:80])
            self.assertIsNone(self.FORBIDDEN.search(sql), sql[:80])
        selects = [s for s in statements if "FROM sessions" in s]
        self.assertEqual(len(selects), 6)  # counters + facts for each of three homes: no per-session N+1
        self.assertEqual(sum(1 for s in statements if s.strip().upper().startswith("BEGIN")), 3)  # one txn per home
        facts = [s for s in selects if "git_repo_root" in s]
        self.assertEqual(len(facts), 3)
        self.assertTrue(all("started_at >" in s for s in facts))  # the exact legacy window predicate

    def test_live_wal_home_gains_no_sidecars(self):
        m = Machine(self)
        writer = m.write_db(None, [D("w1", 1, "claude-x", "anthropic", 5, 5, 0.1, "/w/x", dur=3)],
                            columns=fx.DEPTH_COLUMNS, wal=True)
        self.addCleanup(writer.close)
        before = tree_state(m.root)
        data = self.get([("mode", "profile"), ("dimension", "repo")], svc=m.service())
        self.assertEqual(data["depth_totals"]["sessions"], 1)
        self.assertEqual(tree_state(m.root), before)

    def test_queries_never_touch_repository_paths_on_disk(self):
        target = Path(self.m.root) / "secret-target"
        target.write_text("TOP-SECRET-CONTENT")
        link = Path(self.m.root) / "linked-repo"
        link.symlink_to(target)
        facts, scan = depth.facts_from_reads([_read_with(str(link), "/definitely/not/there/at/all", "/")])
        boom = AssertionError("filesystem touched")
        rates = depth.load_rate_table()  # the packaged table is read once, outside the guarded region
        with mock.patch("os.stat", side_effect=boom), mock.patch("os.lstat", side_effect=boom), \
                mock.patch("os.path.realpath", side_effect=boom), mock.patch("os.path.exists", side_effect=boom), \
                mock.patch("os.listdir", side_effect=boom), mock.patch("subprocess.run", side_effect=boom):
            res = depth.build_depth(facts, dimension="repo", rate_table=rates, scan=scan).data
        blob = json.dumps(res)
        self.assertNotIn("TOP-SECRET-CONTENT", blob)
        self.assertEqual(sum(r["sessions"] for r in res["repo_rollup"]), 3)
        self.assertIn(str(link), blob)  # the recorded lexical text, not a resolved target
        self.assertNotIn(str(target), blob)


def _read_with(*roots):
    from plugins.usage_deck.adapters.session_db import HomeFacts, HomeRead, RawFact
    raws = tuple(RawFact("2026-11-05", utc(2026, 11, 5, 12) + i, None, None, "m", "openai", None, root, 1, 1, 0.1,
                         None, None, None, 1) for i, root in enumerate(roots))
    return HomeRead("p", False, "ok", (), {}, HomeFacts(raws, frozenset()))


class AuthorizedHomeTests(DepthServiceBase):
    def test_profile_principal_depth_reads_only_the_authorized_home(self):
        opened = []

        class Recording(SessionDbReader):
            def read_groups_with_facts(self, directory, cutoff):
                opened.append(Path(directory).name)
                return super().read_groups_with_facts(directory, cutoff)

        svc = self.m.service(home="a", reader_factory=Recording)
        for dimension in ("all", "model", "repo"):
            with NO_NET:
                status, body = svc.handle("usage", [("mode", "machine"), ("dimension", dimension)], profile_only=True)
            self.assertEqual(status, 200, body)
            data = body["data"]
            self.assertEqual(data["mode"], "profile")
            self.assertEqual(data["depth_totals"]["sessions"], len(spec_window(A, 30)))
            self.assertEqual({r["profile"] for r in data["rows"]}, {"a"})
        self.assertEqual(set(opened), {"a"})
        for selector in ("b", "default"):
            with NO_NET:
                status, body = svc.handle("usage", [("profile", selector), ("dimension", "repo")], profile_only=True)
            self.assertEqual(status, 400)
        self.assertEqual(set(opened), {"a"})  # a refused selector opened nothing
        blob = json.dumps(self.get([("mode", "profile"), ("dimension", "repo")], svc=svc))
        self.assertNotIn("/work/usage-deck/", blob.replace("/work/usage-deck\"", ""))  # b/root paths are not exposed
        self.assertNotIn("/srv/Unscoped", blob)

    def test_by_profile_dimension_is_clamped_for_a_profile_principal(self):
        svc = self.m.service(home="a")
        with NO_NET:
            status, body = svc.handle("usage", [("mode", "machine"), ("dimension", "profile")], profile_only=True)
        self.assertEqual((status, body["data"]["dimension"]), (200, "all"))


class HostilePathTests(DepthServiceBase):
    def test_hostile_roots_never_drop_metrics_or_leak(self):
        secret = "sk-abcdefgh12345678"
        roots = ["/w/<img src=x onerror=alert(1)>", "/w/ctl\x07name", "/" + "a" * 5000, f"/w/{secret}", "/w/Bearer xyz.abc",
                 "/w/日本語プロジェクト", '/w/quote"d', "/w/back\\slash-ok"]
        m = Machine(self)
        m.write_db(None, [D(f"h{i}", 1, "claude-x", "anthropic", 10, 5, 0.1, root, hour=i, dur=1) for i, root in enumerate(roots)],
                   columns=fx.DEPTH_COLUMNS)
        data = self.get([("mode", "profile"), ("dimension", "repo")], svc=m.service())
        self.assertEqual(data["depth_totals"]["sessions"], len(roots))
        self.assertEqual(sum(g["totals"]["sessions"] for g in data["groups"]), len(roots))  # nothing dropped
        blob = json.dumps(data)
        self.assertNotIn(secret, blob)
        self.assertNotIn("\x07", blob)
        rollup = {tuple(r["full_paths"]): r for r in data["repo_rollup"]}
        self.assertEqual(rollup[("/w/<img src=x onerror=alert(1)>",)]["repo_basename"], "<img src=x onerror=alert(1)>")  # text only
        control = [r for r in data["repo_rollup"] if r["full_paths"] == [] and r["sessions"] and r["repo_basename"] != "Unscoped"]
        self.assertTrue(control)  # control-char and oversized paths: metrics kept, path detail withheld
        long_ids = [g["id"] for g in data["groups"] if g["id"].startswith("repo:sha256:")]
        self.assertGreaterEqual(len(long_ids), 3)  # secret-bearing, oversized and Bearer names hash their ids
        self.assertTrue(any("[REDACTED]" in g["label"] for g in data["groups"]))
        self.assertTrue(any(g["id"] == 'repo:"\\u65e5\\u672c\\u8a9e\\u30d7\\u30ed\\u30b8\\u30a7\\u30af\\u30c8"' for g in data["groups"]))
        self.assertEqual(len({g["id"] for g in data["groups"]}), len(data["groups"]))


class ParityRegressionTests(DepthServiceBase):
    """C6: existing views are byte-identical for the same data once only the declared additions are removed.

    ``fixtures/depth_parity_golden.json`` tracks the pre-depth BASELINE, i.e. the last main commit before the depth
    code (origin/main 41c3bf4), not a frozen pre-pricing tree. The projection keeps the api_equivalent members and
    the ``pricing`` block, so any upstream change to pricing aliases/catalog data for a model the depth fixture
    uses (``claude-sonnet-5.5`` etc.) legitimately moves these hashes. Refresh trigger: a pricing PR that turns a
    fixture model priced/unpriced fails this test with only api_equivalent/pricing deltas. Regenerate by running
    ``depth_fixture.run_golden`` (TZ=UTC, ``fx.seed(Machine(...))``) on a checkout of the new pre-depth baseline
    (the depth fixture imports nothing from ``analytics_depth``, so it runs on that tree unchanged), update
    ``baseline_commit`` here and in the JSON, and prove the delta is api_equivalent/pricing only (tokens, cost,
    sessions, row ids and ordering must stay byte-identical). Same doctrine as ``spend_golden_v1.json``.
    """

    def test_baseline_projection_matches_the_pre_depth_goldens(self):
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(golden["baseline_commit"], "41c3bf47ae03cffb00058052b469882d64e1fd21")
        got = fx.run_golden(self.m, lambda home: self.m.service(home=home))
        self.assertEqual(set(got), set(golden["hashes"]))
        self.assertEqual(len(got), 63)
        diff = sorted(label for label in got if got[label] != golden["hashes"][label])
        self.assertEqual(diff, [], "baseline values/row ids/order changed for these requests")

    def test_additions_are_the_only_new_keys(self):
        data = self.get([("mode", "machine"), ("dimension", "model")])
        extra = set(data) - set(fx.project_baseline(data))
        self.assertEqual(extra, {"durations", "duration_samples", "heatmap", "cache_metrics", "repo_rollup",
                                 "model_rollup", "depth_totals", "depth_status"})
        self.assertEqual({k for g in data["groups"] for k in g} - set(fx.project_baseline(data)["groups"][0]),
                         {"durations", "duration_samples"})
        self.assertEqual({k for r in data["rows"] for k in r} - set(fx.project_baseline(data)["rows"][0]),
                         {"repo_basename", "repo_paths"})


class CacheIdentityTests(DepthServiceBase):
    def table(self, cache_read):
        return luna_table(cache_read)

    @staticmethod
    def luna(data):
        rows = [r for r in data["cache_metrics"] if r["model"] == "gpt-6-luna"]
        assert len(rows) == 1, "one row per model id"
        return rows[0]

    def test_a_changed_rate_snapshot_never_serves_a_cached_answer(self):
        current = [self.table("0.01")]
        svc = self.m.service()
        svc._depth_rates_factory = lambda: current[0]
        first = self.luna(self.get([("mode", "machine"), ("dimension", "all")], svc=svc))
        current[0] = self.table("0.05")
        second = self.luna(self.get([("mode", "machine"), ("dimension", "all")], svc=svc))  # inside the response TTL
        self.assertEqual((first["discount_multiplier"], second["discount_multiplier"]), (0.1, 0.5))
        self.assertEqual(first["provider_id"], "openai,openrouter")  # one row; both resolved providers listed

    def test_unavailable_rate_file_degrades_to_unknown_discounts_not_errors(self):
        svc = self.m.service()
        def boom():
            raise depth.RateTableError("gone")
        svc._depth_rates_factory = boom
        data = self.get([("mode", "machine"), ("dimension", "all")], svc=svc)
        luna = self.luna(data)
        self.assertEqual((luna["discount_multiplier"], luna["rate_source"]), (None, None))
        self.assertEqual(data["depth_totals"]["sessions"], len(spec_window(self.all_specs(), 30)))

    def test_default_service_loads_the_packaged_table(self):
        data = self.get([("mode", "machine"), ("dimension", "all")])
        luna = self.luna(data)
        # packaged table: openai prices gpt-6-luna, openrouter does not -> the merged row has no single tariff
        self.assertEqual((luna["provider_id"], luna["discount_multiplier"], luna["rate_source"], luna["reason"]),
                         ("openai,openrouter", None, None, "unknown_discount"))
        self.assertTrue(any(r["rate_source"] is None for r in data["cache_metrics"]))  # unknown routes stay unknown
        svc = self.m.service()
        svc._depth_rates_factory = luna_table
        priced = self.luna(self.get([("mode", "machine"), ("dimension", "all")], svc=svc))
        self.assertEqual((priced["discount_multiplier"], priced["rate_source"]), (0.1, "openai-gpt-6-tiers-2026-09"))


class DecoderTests(DepthServiceBase):
    def data(self):
        return self.get([("mode", "machine"), ("dimension", "repo")])

    def test_baseline_shape_is_accepted_with_additions_absent(self):
        data = self.get([("mode", "machine"), ("dimension", "model")])
        legacy = fx.project_baseline(data)
        self.assertIs(usage_from_wire(legacy), legacy)  # absent additions read as null
        check_envelope({"ok": True, "data": legacy, "error": None,
                        "meta": {"schema_version": 2, "request_id": "00000000-0000-4000-8000-000000000000",
                                 "generated_at": "2026-11-06T12:00:00Z", "partial": False, "errors": []}})
        self.assertIs(usage_from_wire(data), data)
        repo = fx.project_baseline(self.data())
        with self.assertRaises(DtoError):  # an old server cannot have produced the repo dimension
            usage_from_wire(repo)

    def test_partial_additions_unknown_keys_and_tampering_are_rejected(self):
        import copy
        data = self.data()

        def mutated(fn):
            bad = copy.deepcopy(data)
            fn(bad)
            return bad

        cases = {
            "one addition missing": lambda d: d.pop("heatmap"),
            "unknown key": lambda d: d.update(surprise=1),
            "group durations missing": lambda d: d["groups"][0].pop("durations"),
            "row repo detail missing": lambda d: d["rows"][0].pop("repo_paths"),
            "grid has six rows": lambda d: d["heatmap"]["grid"].pop(),
            "grid cell extra key": lambda d: d["heatmap"]["grid"][0][0].update(x=1),
            "starts unsorted": lambda d: d["heatmap"]["starts"].reverse(),
            "starts disagree with grid": lambda d: d["heatmap"]["starts"][0].update(sessions=99),
            "sessions do not conserve": lambda d: d["repo_rollup"][0].update(sessions=d["repo_rollup"][0]["sessions"] + 1),
            "model tokens do not conserve": lambda d: d["model_rollup"][0].update(total_tokens=10 ** 12),
            "cost does not conserve": lambda d: d["depth_totals"].update(total_cost=12345.0),
            "percentage above 100": lambda d: d["cache_metrics"][0].update(hit_rate_pct=100.5),
            "negative duration": lambda d: d["durations"].update(p50=-1.0),
            "bool as number": lambda d: d["durations"].update(max=True),
            "nan": lambda d: d["durations"].update(mean=float("nan")),
            "unknown cache reason": lambda d: d["cache_metrics"][0].update(reason="because"),
            "missing key name": lambda d: d["depth_status"]["missing"].pop("duration"),
            "samples without stats": lambda d: d.update(duration_samples=0),
            "repo path with control char": lambda d: d["repo_rollup"][0].update(full_paths=["/a\x00b"], full_path=None),
            "full_path not in full_paths": lambda d: d["repo_rollup"][0].update(full_path="/elsewhere"),
            "bad repo group id": lambda d: d["groups"][0].update(id='repo:"x', series_id='repo:"x'),
            "dimension outside enum": lambda d: d.update(dimension="path"),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                with self.assertRaises(DtoError):
                    usage_from_wire(mutated(fn))
        # only the all-or-none rule: the full set minus the addition is NOT the baseline shape
        partial = {k: v for k, v in data.items() if k != "depth_status"}
        with self.assertRaises(DtoError):
            usage_from_wire(partial)


if __name__ == "__main__":
    unittest.main()
