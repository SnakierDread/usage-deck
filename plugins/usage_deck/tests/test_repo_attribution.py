"""By-repo attribution without ``sessions.git_repo_root`` (card t_ff59364f; design §6.1).

The core never records the column, so each session's repository is derived at aggregation time, in this order:

    P1 recorded root  >  P2 kanban workspace / board worktree  >  P3 nearest ``.git`` ancestor of cwd  >  Unscoped

Expectations are written out in tables below from the specs (paths I build on disk), never read back from the
implementation. Nothing here uses the real hermes home or any real board: every Hermes root, board, database and
"repository" is a temporary directory.
"""

import hashlib
import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from plugins.usage_deck.adapters import repo_attribution as ra
from plugins.usage_deck.adapters.repo_attribution import (
    SOURCE_CWD, SOURCE_KANBAN, SOURCE_NONE, SOURCE_RECORDED, RepoResolver, load_boards, normalize_cwd)
from plugins.usage_deck.models.dto import check_envelope

from .depth_fixture import DEPTH_COLUMNS, NOW_TS, D, Machine
from .test_analytics_depth import NO_NET


def tree_digest(root):
    """Everything a write could change below ``root``: names, sizes, mtimes, content hashes (the checksum gate).

    A WAL ``-shm`` index is shared memory that EVERY reader updates (read marks): only its presence and size count."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.name.endswith("-shm"):
            out[rel] = (info.st_size, None, None)
        elif path.is_file() and not path.is_symlink():
            out[rel] = (info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            out[rel] = (None, None, None)
    return out


class World:
    """A temp Hermes root with two boards plus a directory of fake repositories (``.git`` entries only)."""

    def __init__(self, test, *, with_task_roots=False):
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix="usage-deck-repo-attr-")
        test.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "hermes"
        self.kanban = self.root / "kanban"
        self.repos = self.base / "repos"
        self.repos.mkdir(parents=True)
        for name in ("usage-deck", "invoice-manager", "plain-repo"):
            (self.repos / name / ".git").mkdir(parents=True)
        self.deck_repo = str(self.repos / "usage-deck")
        self.invoice_repo = str(self.repos / "invoice-manager")
        self.board("usage-deck", self.deck_repo)
        self.board("invoice-manager", self.invoice_repo)
        self.no_git = self.base / "elsewhere" / "no-git-here"
        self.no_git.mkdir(parents=True)

    def board(self, slug, workdir, *, tasks=None, kanban=None):
        directory = (kanban or self.kanban) / "boards" / slug
        (directory / "workspaces").mkdir(parents=True, exist_ok=True)
        meta = {"slug": slug, "name": slug}
        if workdir is not None:
            meta["default_workdir"] = workdir
        (directory / "board.json").write_text(json.dumps(meta))
        if tasks is not None:
            self.tasks_db(directory / "kanban.db", tasks)
        return directory

    @staticmethod
    def tasks_db(path, rows):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tasks (id TEXT, workspace_kind TEXT, workspace_path TEXT)")
        for i, (kind, ws) in enumerate(rows):
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (f"t_{i}", kind, ws))
        conn.commit()
        conn.close()

    def workspace(self, slug, task="t_0001", sub=""):
        path = self.kanban / "boards" / slug / "workspaces" / task
        path.mkdir(parents=True, exist_ok=True)
        return str(path / sub) if sub else str(path)

    def worktree(self, repo, task, git_file=True):
        """``<repo>/.worktrees/<task>``: like a real linked worktree it holds a ``.git`` FILE at its root."""
        path = Path(repo) / ".worktrees" / task
        (path / "pkg").mkdir(parents=True, exist_ok=True)
        if git_file:
            (path / ".git").write_text("gitdir: /nowhere\n")
        return str(path)


class PrecedenceTable(unittest.TestCase):
    """C2: P1 > P2 > P3 > Unscoped, one fixture mixing all four."""

    def setUp(self):
        self.w = World(self)
        w = self.w
        git_ws = w.workspace("usage-deck", "t_withgit")
        Path(git_ws, ".git").write_text("gitdir: /nowhere\n")  # a workspace that ALSO looks like a repository (P2 over P3)
        plain = self.w.repos / "plain-repo" / "src" / "pkg"
        plain.mkdir(parents=True)
        self.cases = [
            # (name, recorded, cwd, expected root, expected source)
            ("P1 beats a deck workspace and a git ancestor", "/rec/explicit-repo", w.workspace("usage-deck"), "/rec/explicit-repo", SOURCE_RECORDED),
            ("P1 beats a git ancestor", "/rec/other/", str(plain), "/rec/other/", SOURCE_RECORDED),
            ("P1 without any cwd", "/rec/solo", None, "/rec/solo", SOURCE_RECORDED),
            ("P2 usage-deck workspace root", None, w.workspace("usage-deck"), w.deck_repo, SOURCE_KANBAN),
            ("P2 usage-deck workspace subdir", None, w.workspace("usage-deck", "t_0002", "repo/plugins"), w.deck_repo, SOURCE_KANBAN),
            ("P2 invoice-manager workspace", None, w.workspace("invoice-manager"), w.invoice_repo, SOURCE_KANBAN),
            ("P2 beats P3 (workspace holds .git)", None, git_ws, w.deck_repo, SOURCE_KANBAN),
            ("P2 worktree of usage-deck", None, w.worktree(w.deck_repo, "t_aaa"), w.deck_repo, SOURCE_KANBAN),
            ("P2 worktree subdir of usage-deck", None, w.worktree(w.deck_repo, "t_aaa") + "/pkg", w.deck_repo, SOURCE_KANBAN),
            ("P2 worktree of invoice-manager", None, w.worktree(w.invoice_repo, "t_bbb"), w.invoice_repo, SOURCE_KANBAN),
            ("P2 worktree subdir of invoice-manager", None, w.worktree(w.invoice_repo, "t_bbb") + "/pkg", w.invoice_repo, SOURCE_KANBAN),
            ("blank P1 falls through to P2", "   ", w.workspace("invoice-manager"), w.invoice_repo, SOURCE_KANBAN),
            ("separator-only P1 falls through to P3", "/", str(plain), str(w.repos / "plain-repo"), SOURCE_CWD),
            ("P3 repo root itself", None, str(w.repos / "plain-repo"), str(w.repos / "plain-repo"), SOURCE_CWD),
            ("P3 nested dir finds the repo", None, str(plain), str(w.repos / "plain-repo"), SOURCE_CWD),
            ("P3 worktree of a repo NO board knows is its own repo", None, w.worktree(str(w.repos / "plain-repo"), "t_zzz"),
             str(w.repos / "plain-repo" / ".worktrees" / "t_zzz"), SOURCE_CWD),
            ("Unscoped: no .git anywhere above", None, str(w.no_git), None, SOURCE_NONE),
            ("Unscoped: no cwd", None, None, None, SOURCE_NONE),
            ("Unscoped: relative cwd", None, "relative/dir", None, SOURCE_NONE),
            ("Unscoped: empty cwd", None, "", None, SOURCE_NONE),
            ("Unscoped: unknown board's workspace", None, w.workspace("ghost-board"), None, SOURCE_NONE),
            ("Unscoped: the workspaces dir itself is not a task workspace", None,
             str(w.kanban / "boards" / "usage-deck" / "workspaces"), None, SOURCE_NONE),
            ("Unscoped: non-text cwd", None, 42, None, SOURCE_NONE),
        ]

    def test_every_row_of_the_table(self):
        resolver = RepoResolver(self.w.kanban)
        for name, recorded, cwd, root, source in self.cases:
            with self.subTest(name):
                self.assertEqual(resolver.resolve(recorded, cwd), (root, source))

    def test_all_four_sources_occur_in_the_fixture(self):
        self.assertEqual({c[4] for c in self.cases}, set(ra.SOURCES))

    def test_answers_do_not_depend_on_session_order_or_memoization(self):
        shared = RepoResolver(self.w.kanban)
        forward = [shared.resolve(r, c) for _, r, c, _, _ in self.cases]
        backward = [RepoResolver(self.w.kanban).resolve(r, c) for _, r, c, _, _ in reversed(self.cases)]
        self.assertEqual(forward, list(reversed(backward)))
        self.assertEqual(forward, [(root, src) for _, _, _, root, src in self.cases])

    def test_without_a_kanban_dir_rule_2_is_off_and_p3_applies(self):
        w = self.w
        git_ws = w.workspace("usage-deck", "t_withgit")  # holds .git
        resolver = RepoResolver(None)
        self.assertEqual(resolver.resolve(None, w.workspace("usage-deck", "t_nogit")), (None, SOURCE_NONE))
        self.assertEqual(resolver.resolve(None, git_ws), (git_ws, SOURCE_CWD))
        self.assertEqual(resolver.resolve(None, w.worktree(w.deck_repo, "t_aaa")), (w.deck_repo + "/.worktrees/t_aaa", SOURCE_CWD))

    def test_repository_key_is_the_lexical_basename(self):
        """The root path P2/P3 return reduces to the same key the by-repo view groups by."""
        from plugins.usage_deck.adapters.analytics_depth import repo_key
        resolver = RepoResolver(self.w.kanban)
        deck, _ = resolver.resolve(None, self.w.worktree(self.w.deck_repo, "t_k1"))
        inv, _ = resolver.resolve(None, self.w.worktree(self.w.invoice_repo, "t_k2"))
        self.assertEqual((repo_key(deck), repo_key(inv)), ("usage-deck", "invoice-manager"))


class BoardEnumeration(unittest.TestCase):
    def test_board_repo_comes_from_board_json_else_the_dominant_task_root(self):
        w = World(self)
        self.assertEqual(load_boards(w.kanban)["usage-deck"].repo, w.deck_repo)
        tasks = [("dir", w.invoice_repo), ("dir", w.invoice_repo), ("worktree", w.deck_repo + "/.worktrees/t_q"),
                 ("scratch", "/x/y"), ("dir", str(w.kanban / "boards" / "nocfg" / "workspaces" / "t_s"))]
        w.board("nocfg", None, tasks=tasks)
        board = load_boards(w.kanban)["nocfg"]
        self.assertEqual(board.repo, w.invoice_repo)  # 2 votes vs 1; scratch + the board's own workspaces ignored
        self.assertEqual(set(board.roots), {w.invoice_repo, w.deck_repo})
        resolver = RepoResolver(w.kanban)
        self.assertEqual(resolver.resolve(None, w.workspace("nocfg")), (w.invoice_repo, SOURCE_KANBAN))
        # a worktree of ANY enumerated board's repository resolves to that repository
        self.assertEqual(resolver.resolve(None, w.worktree(w.deck_repo, "t_n1")), (w.deck_repo, SOURCE_KANBAN))

    def test_board_json_wins_over_task_rows(self):
        w = World(self)
        w.board("mixed", w.deck_repo, tasks=[("dir", w.invoice_repo)] * 3)
        self.assertEqual(load_boards(w.kanban)["mixed"].repo, w.deck_repo)

    def test_a_board_without_any_repository_is_unscoped_not_an_error(self):
        w = World(self)
        w.board("empty", None)
        self.assertIsNone(load_boards(w.kanban)["empty"].repo)
        self.assertEqual(RepoResolver(w.kanban).resolve(None, w.workspace("empty")), (None, SOURCE_NONE))

    def test_hostile_boards_are_skipped_or_harmless(self):
        w = World(self)
        boards = w.kanban / "boards"
        (boards / "bad json").mkdir()                 # not an allowed slug
        (boards / "..hidden").mkdir()
        (boards / "corrupt").mkdir()
        (boards / "corrupt" / "board.json").write_text("{not json")
        (boards / "corrupt" / "kanban.db").write_bytes(b"this is not sqlite")
        (boards / "huge").mkdir()
        (boards / "huge" / "board.json").write_text(json.dumps({"default_workdir": "/x/" + "a" * 200000}))
        target = w.base / "outside"
        (target / "workspaces").mkdir(parents=True)
        (boards / "linked").symlink_to(target, target_is_directory=True)
        (boards / "linkjson").mkdir()
        secret = w.base / "secret.json"
        secret.write_text(json.dumps({"default_workdir": "/leaked/repo"}))
        (boards / "linkjson" / "board.json").symlink_to(secret)
        loaded = load_boards(w.kanban)
        self.assertNotIn("bad json", loaded)
        self.assertNotIn("linked", loaded)
        self.assertIsNone(loaded["corrupt"].repo)
        self.assertIsNone(loaded["huge"].repo)
        self.assertIsNone(loaded["linkjson"].repo)  # board.json is read without following symlinks
        self.assertEqual(loaded["usage-deck"].repo, w.deck_repo)  # the rest still resolve
        self.assertEqual(RepoResolver(w.kanban).resolve(None, str(target / "workspaces" / "t_1")), (None, SOURCE_NONE))

    def test_symlinked_boards_dir_and_missing_dir_yield_no_boards(self):
        w = World(self)
        self.assertEqual(load_boards(w.base / "nope"), {})
        real = w.base / "real-boards"
        (real / "x").mkdir(parents=True)
        k = w.base / "k2"
        k.mkdir()
        (k / "boards").symlink_to(real, target_is_directory=True)
        self.assertEqual(load_boards(k), {})

    def test_normalize_cwd(self):
        self.assertEqual(normalize_cwd("/a/b/../c/"), "/a/c")
        self.assertEqual(normalize_cwd("//a//b"), "/a/b")
        for bad in (None, 5, "", "  ", "rel/path", "/", "/a\x00b", "C:\\x\\y", "/" + "a" * 5000):
            self.assertIsNone(normalize_cwd(bad), repr(bad)[:20])


class ReadOnlyKanbanAccess(unittest.TestCase):
    """C3: ``mode=ro`` only, board path from enumeration of THIS root only, bytes untouched."""

    def test_checksum_gate_rollback_journal_and_live_wal_boards(self):
        w = World(self)
        rows = [("dir", w.invoice_repo), ("worktree", w.deck_repo + "/.worktrees/t_x")]
        w.board("journal", None, tasks=rows)
        live = w.board("walboard", None, tasks=rows)
        writer = sqlite3.connect(live / "kanban.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO tasks VALUES ('t_live', 'dir', ?)", (w.invoice_repo,))
        writer.commit()  # committed frames live only in the -wal file
        self.addCleanup(writer.close)
        idle = w.board("idlewal", None, tasks=rows)
        conn = sqlite3.connect(idle / "kanban.db")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()
        for sidecar in ("kanban.db-wal", "kanban.db-shm"):
            Path(idle, sidecar).unlink(missing_ok=True)  # an idle WAL database has no sidecars
        for slug in ("journal", "walboard", "idlewal"):
            w.workspace(slug)  # created up front: the gate measures the resolver, not the fixture
        before = tree_digest(w.root)
        for _ in range(2):
            resolver = RepoResolver(w.kanban)
            self.assertEqual(resolver.resolve(None, w.workspace("journal"))[1], SOURCE_KANBAN)
            self.assertEqual(resolver.resolve(None, w.workspace("walboard"))[1], SOURCE_KANBAN)
            self.assertEqual(resolver.resolve(None, w.workspace("idlewal"))[1], SOURCE_KANBAN)
        self.assertEqual(tree_digest(w.root), before)  # no byte changed, no sidecar or journal appeared
        self.assertFalse((idle / "kanban.db-wal").exists() or (idle / "kanban.db-shm").exists())

    def test_every_connection_is_mode_ro_and_only_selects_run(self):
        w = World(self)
        w.board("withdb", None, tasks=[("dir", w.invoice_repo)])
        uris, statements = [], []
        real = sqlite3.connect

        def spy(database, *a, **kw):
            uris.append(str(database))
            return real(database, *a, **kw)

        resolver = RepoResolver(w.kanban, trace=statements.append)
        with mock.patch.object(sqlite3, "connect", side_effect=spy):
            resolver.resolve(None, w.workspace("withdb"))
        self.assertTrue(uris)
        for uri in uris:
            self.assertTrue(uri.startswith("file:"), uri)
            self.assertTrue("mode=ro" in uri or "immutable=1" in uri, uri)
            self.assertIn(str(w.kanban / "boards" / "withdb" / "kanban.db").replace(" ", "%20"), uri)
        self.assertTrue(statements)
        for sql in statements:
            head = sql.strip().split(None, 1)[0].upper()
            self.assertIn(head, {"PRAGMA", "BEGIN", "SELECT"}, sql)
            self.assertNotRegex(sql.upper(), r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE|VACUUM|ATTACH)\b")

    def test_a_connection_that_resolves_a_board_cannot_write(self):
        """Prove the URI is truly read-only by opening it the way the module does and attempting a write."""
        w = World(self)
        directory = w.board("probe", None, tasks=[("dir", w.invoice_repo)])
        uri = ra.open_uri(directory / "kanban.db")
        self.assertIsNotNone(uri)
        conn = sqlite3.connect(uri, uri=True)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO tasks VALUES ('x', 'dir', '/y')")

    def test_other_roots_boards_are_never_enumerated_or_opened(self):
        w = World(self)
        World.tasks_db(w.kanban / "boards" / "usage-deck" / "kanban.db", [("dir", w.deck_repo)])
        decoy = w.base / "other-home"
        decoy_repo = w.base / "repos" / "decoy-repo"
        (decoy_repo / ".git").mkdir(parents=True)
        w.board("usage-deck", str(decoy_repo), tasks=[("dir", str(decoy_repo))], kanban=decoy / "kanban")
        opened = []
        real = sqlite3.connect

        def spy(database, *a, **kw):
            opened.append(str(database))
            return real(database, *a, **kw)

        resolver = RepoResolver(w.kanban)
        decoy_ws = str(decoy / "kanban" / "boards" / "usage-deck" / "workspaces" / "t_1")
        with mock.patch.object(sqlite3, "connect", side_effect=spy):
            self.assertEqual(resolver.resolve(None, w.workspace("usage-deck"))[0], w.deck_repo)
            # a session that merely NAMES the other home's workspace does not make us read that home
            self.assertEqual(resolver.resolve(None, decoy_ws), (None, SOURCE_NONE))
        self.assertTrue(opened)
        self.assertTrue(all(str(decoy) not in p for p in opened), opened)
        self.assertTrue(all(str(w.kanban) in p for p in opened), opened)

    def test_p3_issues_only_lstat_of_dot_git(self):
        w = World(self)
        deep = w.repos / "plain-repo" / "a" / "b" / "c"
        deep.mkdir(parents=True)
        seen = []
        real = os.lstat

        def spy(path, *a, **kw):
            seen.append(str(path))
            return real(path, *a, **kw)

        boom = AssertionError("must not run")
        resolver = RepoResolver(None)
        with mock.patch("os.lstat", side_effect=spy), mock.patch("subprocess.run", side_effect=boom), \
                mock.patch("subprocess.Popen", side_effect=boom), mock.patch("os.listdir", side_effect=boom), \
                mock.patch("os.scandir", side_effect=boom), mock.patch("builtins.open", side_effect=boom):
            self.assertEqual(resolver.resolve(None, str(deep)), (str(w.repos / "plain-repo"), SOURCE_CWD))
        self.assertTrue(seen and all(p.endswith("/.git") for p in seen), seen)

    def test_each_directory_is_stat_ed_once_per_resolver(self):
        w = World(self)
        deep = w.repos / "plain-repo" / "a" / "b"
        deep.mkdir(parents=True)
        probes = []
        resolver = RepoResolver(None, probe=lambda p: probes.append(p) or os.path.lexists(p))
        for sub in ("a/b", "a", "a/b", "", "a/b/"):
            target = str(w.repos / "plain-repo" / sub) if sub else str(w.repos / "plain-repo")
            self.assertEqual(resolver.resolve(None, target), (str(w.repos / "plain-repo"), SOURCE_CWD))
        self.assertEqual(len(probes), len(set(probes)))


def sess(sid, cwd=None, repo=None, *, tokens=(100, 50), day=1, model="claude-sonnet-5.5", provider="anthropic", cost=0.1):
    spec = D(sid, day, model, provider, tokens[0], tokens[1], cost, repo, dur=60)
    spec["cwd"] = cwd
    return spec


COLUMNS = DEPTH_COLUMNS + ", cwd TEXT"


class ServiceByRepo(unittest.TestCase):
    """The by-repo view end to end: attribution at query time, conservation C1, coverage inputs C4."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)
        self.m = Machine(self)
        self.w = World(self)
        w = self.w
        # the service reads boards from <machine root>/kanban: build the boards there
        self.w.kanban = self.m.root / "kanban"
        w.board("usage-deck", w.deck_repo)
        w.board("invoice-manager", w.invoice_repo)
        plain_src = w.repos / "plain-repo" / "src"
        plain_src.mkdir()
        self.specs = {
            None: [
                sess("g1", w.workspace("usage-deck"), tokens=(1000, 100)),
                sess("g2", w.worktree(w.deck_repo, "t_g2"), tokens=(2000, 200), day=2),
                sess("g3", w.workspace("invoice-manager"), tokens=(300, 30)),
                sess("g4", str(plain_src), tokens=(40, 4)),
                sess("g5", str(w.no_git), tokens=(5000, 500)),
                sess("g6", w.workspace("usage-deck"), "/recorded/explicit-repo", tokens=(70, 7)),  # P1 beats P2
                sess("g7", None, None, tokens=(9, 1)),
                sess("g8", w.worktree(w.invoice_repo, "t_g8"), tokens=(600, 60), model="gpt-6-luna", provider="openai"),
            ],
            "a": [
                sess("a1", w.workspace("usage-deck"), tokens=(11, 1), model="gpt-6-luna", provider="openai"),
                sess("a2", str(w.no_git), tokens=(22, 2)),
            ],
        }
        self.m.write_db(None, self.specs[None], columns=COLUMNS)
        self.m.write_db("a", self.specs["a"], columns=COLUMNS)
        # a profile home's own kanban dir must never be consulted (decoy repo name would show up)
        w.board("usage-deck", "/decoy/decoy-repo", kanban=self.m.home("a") / "kanban")

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def get(self, dimension, **kw):
        svc = self.m.service()
        with NO_NET:
            status, body = svc.handle("usage", [("mode", "machine"), ("dimension", dimension)], **kw)
        self.assertEqual(status, 200, body)
        check_envelope(body)
        return body["data"]

    def test_groups_membership_and_order(self):
        data = self.get("repo")
        by_id = {g["id"]: g for g in data["groups"]}
        self.assertEqual(set(by_id), {'repo:"usage-deck"', 'repo:"explicit-repo"', 'repo:"invoice-manager"',
                                      'repo:"plain-repo"', "repo:null"})
        sessions = {gid: g["totals"]["sessions"] for gid, g in by_id.items()}
        self.assertEqual(sessions, {'repo:"usage-deck"': 3, 'repo:"explicit-repo"': 1, 'repo:"invoice-manager"': 2,
                                    'repo:"plain-repo"': 1, "repo:null": 3})
        self.assertEqual(data["groups"][-1]["id"], "repo:null")  # explicit bucket, last, never folded into Other
        self.assertFalse(any(g["is_other"] for g in data["groups"]))
        rollup = {(r["repo_basename"], bool(r["full_paths"])): r for r in data["repo_rollup"]}
        self.assertEqual(rollup[("usage-deck", True)]["full_path"], self.w.deck_repo)
        self.assertEqual(rollup[("usage-deck", True)]["total_tokens"], 1100 + 2200 + 12)
        self.assertEqual(rollup[("invoice-manager", True)]["total_tokens"], 330 + 660)
        self.assertEqual(rollup[("plain-repo", True)]["full_path"], str(self.w.repos / "plain-repo"))
        self.assertEqual(rollup[("explicit-repo", True)]["full_path"], "/recorded/explicit-repo")
        unscoped = rollup[("Unscoped", False)]
        self.assertEqual((unscoped["sessions"], unscoped["total_tokens"], unscoped["full_paths"]), (3, 5500 + 10 + 24, []))
        self.assertNotIn("decoy-repo", json.dumps(data))

    def test_attributed_share_is_derivable_from_the_rollup(self):
        rollup = self.get("repo")["repo_rollup"]
        total = sum(r["total_tokens"] for r in rollup)
        missing = sum(r["total_tokens"] for r in rollup if r["repo_basename"] == "Unscoped" and not r["full_paths"])
        self.assertEqual(total, 1100 + 2200 + 12 + 77 + 330 + 660 + 44 + 5500 + 10 + 24)
        self.assertEqual((total - missing) / total, (1100 + 2200 + 12 + 77 + 990 + 44) / total)

    def test_c1_repo_totals_conserve_against_all_profile_and_model(self):
        repo = self.get("repo")
        for other in ("all", "profile", "model"):
            base = self.get(other)
            for field in ("input_count", "output_count", "sessions", "api_calls"):
                self.assertEqual(repo["totals"][field], base["totals"][field], (other, field))
            self.assertAlmostEqual(repo["totals"]["estimated_usd"], base["totals"]["estimated_usd"], places=9)
            self.assertEqual(repo["totals"]["api_equivalent"]["amount_pico_usd"],
                             base["totals"]["api_equivalent"]["amount_pico_usd"], other)
            self.assertEqual(repo["depth_totals"], base["depth_totals"], other)
        self.assertEqual(sum(g["totals"]["sessions"] for g in repo["groups"]), 10)
        self.assertEqual(sum(g["totals"]["input_count"] for g in repo["groups"]), repo["totals"]["input_count"])
        self.assertEqual(sum(r["sessions"] for r in repo["repo_rollup"]), repo["depth_totals"]["sessions"])
        self.assertEqual(sum(r["total_tokens"] for r in repo["repo_rollup"]), repo["depth_totals"]["total_tokens"])
        self.assertEqual(sum(r["sessions"] for r in repo["model_rollup"]), repo["depth_totals"]["sessions"])

    def test_attribution_is_computed_per_query_and_nothing_is_persisted(self):
        """C2: moving a repo marker changes the next answer; no state db or board file changes."""
        before = tree_digest(self.m.root)
        first = self.get("repo")
        self.assertEqual(tree_digest(self.m.root), before)  # read-only gate
        conn = sqlite3.connect(self.m.home(None) / "state.db")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        conn.close()
        self.assertEqual(columns, {c.split()[0] for c in COLUMNS.split(", ")})  # no derived column was added
        # the same cwd gains a repository: a fresh service sees it immediately
        (self.w.no_git / ".git").mkdir()
        second = self.get("repo")
        self.assertNotEqual(json.dumps(first["groups"]), json.dumps(second["groups"]))
        self.assertIn('repo:"no-git-here"', {g["id"] for g in second["groups"]})
        self.assertEqual([g for g in second["groups"] if g["id"] == "repo:null"][0]["totals"]["sessions"], 1)

    def test_recorded_root_still_wins_when_the_core_starts_recording_it(self):
        m = Machine(self)
        self.w.kanban = m.root / "kanban"
        self.w.board("usage-deck", self.w.deck_repo)
        specs = [sess("n1", self.w.workspace("usage-deck"), "/srv/core/recorded-one"),
                 sess("n2", self.w.workspace("usage-deck"), None)]
        m.write_db(None, specs, columns=COLUMNS)
        svc = m.service()
        with NO_NET:
            status, body = svc.handle("usage", [("mode", "machine"), ("dimension", "repo")])
        self.assertEqual(status, 200)
        ids = {g["id"]: g["totals"]["sessions"] for g in body["data"]["groups"]}
        self.assertEqual(ids, {'repo:"recorded-one"': 1, 'repo:"usage-deck"': 1})  # an empty Unscoped group is not listed

    def test_databases_without_a_cwd_column_degrade_to_recorded_only(self):
        m = Machine(self)
        m.write_db(None, [sess("o1", None, "/w/old-repo"), sess("o2", None, None)], columns=DEPTH_COLUMNS)  # no cwd column
        svc = m.service()
        with NO_NET:
            status, body = svc.handle("usage", [("mode", "machine"), ("dimension", "repo")])
        self.assertEqual(status, 200)
        ids = {g["id"]: g["totals"]["sessions"] for g in body["data"]["groups"]}
        self.assertEqual(ids, {'repo:"old-repo"': 1, "repo:null": 1})

    def test_no_subprocess_and_no_network_while_resolving(self):
        boom = AssertionError("must not run")
        with mock.patch("subprocess.run", side_effect=boom), mock.patch("subprocess.Popen", side_effect=boom):
            data = self.get("repo")
        self.assertEqual(data["depth_totals"]["sessions"], 10)


if __name__ == "__main__":
    unittest.main()
