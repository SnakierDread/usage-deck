"""Per-session repository attribution (design: analytics-depth-breakdown §6.1).

The Hermes core never records ``sessions.git_repo_root`` (NULL on virtually every row), so the by-repo view derives
a repository from data the plugin can already see, at aggregation time, for each session independently. Nothing is
persisted and nothing is written anywhere. The first rule that yields a repository wins:

1. **recorded**: ``sessions.git_repo_root`` when it is a non-blank string (future-proof: wins as soon as the core
   records it).
2. **kanban**: ``cwd`` is a kanban workspace of a board of THIS Hermes root, or a git worktree below a board's
   repository: ``<root>/kanban/boards/<slug>/workspaces/<task>[/...]`` -> the board's repository (``board.json``
   ``default_workdir``, else the most common repository among the board's ``dir``/``worktree`` tasks read from the
   board's ``kanban.db``); ``<repo>/.worktrees/<task>[/...]`` for a repository of any enumerated board -> ``<repo>``.
3. **cwd**: the nearest ancestor of ``cwd`` (itself included) that contains a ``.git`` entry. Only an ``lstat`` of
   ``<dir>/.git`` is ever issued: no git binary, no file read, no directory listing.
4. otherwise **unscoped** (``None``): the explicit Unscoped bucket.

A rule's result is a repository ROOT PATH; its lexical basename is the repository key everywhere downstream, so
every rule shares the collision/path-detail machinery of the by-repo view.

Boundaries: only ``<root>/kanban/boards`` of the request-scoped root is enumerated (never another home's boards, never
a path named by a session), each board's ``kanban.db`` is opened ``mode=ro`` + ``query_only`` (through the same
sidecar-safe URI rule as the state databases) and ``board.json`` is read without following symlinks.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .session_db import open_uri

SOURCE_RECORDED = "recorded"
SOURCE_KANBAN = "kanban"
SOURCE_CWD = "cwd"
SOURCE_NONE = "unscoped"
SOURCES = (SOURCE_RECORDED, SOURCE_KANBAN, SOURCE_CWD, SOURCE_NONE)

BOARD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_BOARDS = 256
MAX_BOARD_JSON_BYTES = 64 * 1024
MAX_TASK_ROWS = 20000
MAX_PATH_BYTES = 4096
MAX_WALK = 64
WORKTREES = ".worktrees"
BUSY_TIMEOUT_S = 0.25

Result = Tuple[Optional[str], str]


def normalize_cwd(cwd: Any) -> Optional[str]:
    """A lexically normalized absolute POSIX path, else None (no filesystem access, nothing resolved)."""
    if not isinstance(cwd, str):
        return None
    text = cwd.strip()
    if not text or "\x00" in text or "\\" in text or len(text.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        return None
    if not text.startswith("/"):
        return None
    path = posixpath.normpath(text)
    if path.startswith("//"):  # normpath keeps exactly two leading slashes
        path = "/" + path.lstrip("/")
    return path if path != "/" else None


def _below(path: str, prefix: str) -> Optional[List[str]]:
    """Components of ``path`` strictly below ``prefix`` (both normalized), or None."""
    base = prefix.rstrip("/") + "/"
    if not path.startswith(base):
        return None
    return [p for p in path[len(base):].split("/") if p]


class Board:
    """One enumerated board: its slug and the repository roots it is known to work on."""

    __slots__ = ("slug", "repo", "roots")

    def __init__(self, slug: str, repo: Optional[str], roots: Tuple[str, ...]):
        self.slug = slug
        self.repo = repo  # the repository root cwd workspaces of this board resolve to
        self.roots = roots  # every repository root whose ``.worktrees/*`` belong to this board


def _read_board_json(directory: Path) -> Optional[dict]:
    path = directory / "board.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BOARD_JSON_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            value = json.loads(handle.read(MAX_BOARD_JSON_BYTES + 1).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    return value if isinstance(value, dict) else None


def _task_roots(directory: Path, own_workspaces: str, trace: Optional[Callable[[str], None]]) -> List[str]:
    """Repository roots implied by the board's ``dir``/``worktree`` tasks (``kanban.db``, strictly read-only)."""
    path = directory / "kanban.db"
    try:
        info = os.lstat(path)
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode):
        return []
    uri = open_uri(path)
    if uri is None:
        return []
    conn = None
    out: List[str] = []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        if trace is not None:
            conn.set_trace_callback(trace)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if not {"workspace_kind", "workspace_path"} <= columns:
            return []
        sql = ("SELECT workspace_kind, workspace_path FROM tasks "
               "WHERE workspace_kind IN ('worktree', 'dir') AND workspace_path IS NOT NULL LIMIT ?")
        for kind, raw in conn.execute(sql, (MAX_TASK_ROWS,)):
            path_text = normalize_cwd(raw)
            if path_text is None or (own_workspaces and _below(path_text, own_workspaces) is not None):
                continue  # scratch-like dirs inside the board's own workspaces are not repositories
            if kind == "worktree":
                head, sep, _tail = path_text.partition("/" + WORKTREES + "/")
                if sep and head:
                    out.append(head)
            else:
                out.append(path_text)
    except (sqlite3.Error, OSError, ValueError, UnicodeError, OverflowError):
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return out


def load_boards(kanban_dir: Path, trace: Optional[Callable[[str], None]] = None) -> Dict[str, Board]:
    """Boards of ``<kanban_dir>/boards`` only. Unreadable/odd entries are skipped, never fatal."""
    boards_dir = Path(kanban_dir) / "boards"
    try:
        if boards_dir.is_symlink() or not boards_dir.is_dir():
            return {}
        entries = sorted(e.name for e in os.scandir(boards_dir)
                         if BOARD_NAME_RE.match(e.name) and e.is_dir(follow_symlinks=False))
    except OSError:
        return {}
    out: Dict[str, Board] = {}
    for name in entries[:MAX_BOARDS]:
        directory = boards_dir / name
        meta = _read_board_json(directory) or {}
        own = normalize_cwd(str(directory / "workspaces")) or ""
        tasks = _task_roots(directory, own, trace)
        configured = normalize_cwd(meta.get("default_workdir"))
        counted = Counter(tasks)
        repo = configured
        if repo is None and counted:
            repo = sorted(counted, key=lambda r: (-counted[r], r))[0]
        roots = tuple(sorted({r for r in ([configured] if configured else []) + list(counted)}))
        out[name] = Board(name, repo, roots)
    return out


class RepoResolver:
    """Resolves one session at a time; memoized per normalized ``cwd`` so a scan stats each directory once.

    ``kanban_dir`` is ``<hermes root>/kanban`` of the request-scoped root (None: rule 2 is off). Boards load
    lazily, at the first session that needs them. ``probe`` is the stat seam (``lstat``-like: path -> exists).
    """

    def __init__(self, kanban_dir: Optional[Path] = None, *, trace: Optional[Callable[[str], None]] = None,
                 probe: Optional[Callable[[str], bool]] = None):
        self._kanban = None if kanban_dir is None else Path(kanban_dir)
        self._trace = trace
        self._probe = probe or _has_git_entry
        self._boards: Optional[Dict[str, Board]] = None
        self._memo: Dict[str, Result] = {}
        self._dir_memo: Dict[str, Optional[str]] = {}
        self._prefix: Optional[str] = None
        if self._kanban is not None:
            base = normalize_cwd(str(self._kanban / "boards"))
            self._prefix = base

    @property
    def boards(self) -> Dict[str, Board]:
        if self._boards is None:
            self._boards = {} if self._kanban is None else load_boards(self._kanban, self._trace)
        return self._boards

    # -- the precedence ------------------------------------------------------------------------------
    def resolve(self, recorded: Any, cwd: Any) -> Result:
        """``(repository root path | None, source)``; the source is one of ``SOURCES``."""
        if isinstance(recorded, str) and _recorded_ok(recorded):
            return recorded, SOURCE_RECORDED
        path = normalize_cwd(cwd)
        if path is None:
            return None, SOURCE_NONE
        hit = self._memo.get(path)
        if hit is None:
            hit = self._memo[path] = self._derive(path)
        return hit

    def _derive(self, path: str) -> Result:
        root = self._from_kanban(path)
        if root is not None:
            return root, SOURCE_KANBAN
        root = self._from_git_marker(path)
        if root is not None:
            return root, SOURCE_CWD
        return None, SOURCE_NONE

    def _from_kanban(self, path: str) -> Optional[str]:
        if self._prefix is None:
            return None
        parts = _below(path, self._prefix)
        if parts is not None:  # a board's workspace: <slug>/workspaces/<task>[/...]
            if len(parts) < 3 or parts[1] != "workspaces":
                return None
            board = self.boards.get(parts[0])
            return None if board is None else board.repo
        if "/" + WORKTREES + "/" not in path:
            return None
        best: Optional[str] = None
        for board in self.boards.values():  # a worktree below the repository of any enumerated board
            for root in board.roots:
                inside = _below(path, root + "/" + WORKTREES)
                if inside and (best is None or len(root) > len(best)):
                    best = root
        return best

    def _from_git_marker(self, path: str) -> Optional[str]:
        walked: List[str] = []
        found: Optional[str] = None
        current = path
        for _ in range(MAX_WALK):
            if current in self._dir_memo:
                found = self._dir_memo[current]
                break
            walked.append(current)
            if self._probe(current + "/.git"):
                found = current
                break
            parent = posixpath.dirname(current)
            if parent == current or parent == "/":
                break
            current = parent
        if found is not None or len(walked) < MAX_WALK:
            for directory in walked:  # every directory below the hit shares its answer
                self._dir_memo[directory] = found
        return found


def _recorded_ok(text: str) -> bool:
    stripped = text.strip().rstrip("/\\")
    if not stripped:
        return False
    base = re.split(r"[\\/]", stripped)[-1]
    return bool(base.strip())


def _has_git_entry(path: str) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True
