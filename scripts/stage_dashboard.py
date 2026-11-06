#!/usr/bin/env python3
"""Deterministically stage plugins/usage_deck/dashboard/ from the canonical sources.

DESIGN.md §7: the Hermes CLI installs agent packages that carry ``plugin.yaml`` plus
``dashboard/manifest.json``. The canonical, hand-edited files live at the plugin root
(``manifest.json``, ``dist/``, backend modules); ``dashboard/`` is *generated* from them and
must never be edited by hand. Rebuild after every backend or frontend commit.

    python3 scripts/stage_dashboard.py            # rebuild dashboard/
    python3 scripts/stage_dashboard.py --check    # exit 1 if dashboard/ drifted from sources
    python3 scripts/stage_dashboard.py --require-backend   # fail when backend files are absent

Stdlib only. Output is a pure function of the source bytes: the destination is rebuilt from
scratch, entries are copied in sorted order, file modes are normalised, and no symlinks are
created or followed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

PLUGIN_REL = Path("plugins") / "usage_deck"

# Frontend-owned canonical files: (source relative to plugin root, destination under dashboard/).
FRONTEND_FILES = (
    "manifest.json",
    "dist/index.js",
    "dist/style.css",
)

# Backend-owned canonical entries, copied only when present (backend merges separately).
BACKEND_FILES = ("__init__.py", "plugin_api.py", "discovery.py", "service.py")
BACKEND_DIRS = ("adapters", "models")

_SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


class StageError(RuntimeError):
    pass


def _iter_dir(src: Path):
    """Yield regular files below *src* in sorted order, refusing symlinks."""
    for entry in sorted(src.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            raise StageError(f"refusing symlink in canonical sources: {entry}")
        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            yield from _iter_dir(entry)
        elif entry.is_file() and not entry.name.endswith(_SKIP_SUFFIXES):
            yield entry


def plan(plugin_root: Path, require_backend: bool = False) -> dict[str, Path]:
    """Map destination-relative POSIX path -> source file, in deterministic order."""
    out: dict[str, Path] = {}
    for rel in FRONTEND_FILES:
        src = plugin_root / rel
        if src.is_symlink() or not src.is_file():
            raise StageError(f"missing canonical frontend file: {rel}")
        out[rel] = src
    missing = []
    for rel in BACKEND_FILES:
        src = plugin_root / rel
        if src.is_symlink():
            raise StageError(f"refusing symlink in canonical sources: {src}")
        if src.is_file():
            out[rel] = src
        else:
            missing.append(rel)
    for d in BACKEND_DIRS:
        src = plugin_root / d
        if src.is_symlink():
            raise StageError(f"refusing symlink in canonical sources: {src}")
        if src.is_dir():
            for f in _iter_dir(src):
                out[f.relative_to(plugin_root).as_posix()] = f
        else:
            missing.append(d + "/")
    if require_backend and missing:
        raise StageError("backend files absent: " + ", ".join(missing))
    return dict(sorted(out.items()))


def stage(plugin_root: Path, require_backend: bool = False) -> list[str]:
    """Rebuild ``dashboard/`` and return the staged relative paths."""
    files = plan(plugin_root, require_backend)
    dest = plugin_root / "dashboard"
    if dest.is_symlink():
        raise StageError("dashboard/ must not be a symlink")
    if dest.exists():
        shutil.rmtree(dest)
    for rel, src in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
        os.chmod(target, 0o644)
    for d in sorted({p.parent for p in (dest / r for r in files)} | {dest}):
        os.chmod(d, 0o755)
    return list(files)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(plugin_root: Path, require_backend: bool = False) -> list[str]:
    """Return a list of drift problems (empty == staged copy matches canonical sources)."""
    files = plan(plugin_root, require_backend)
    dest = plugin_root / "dashboard"
    problems: list[str] = []
    if not dest.is_dir() or dest.is_symlink():
        return ["dashboard/ does not exist (run scripts/stage_dashboard.py)"]
    staged: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                problems.append(f"symlink in staging: {p.relative_to(dest).as_posix()}")
        for name in filenames:
            p = Path(dirpath) / name
            staged[p.relative_to(dest).as_posix()] = p
    for rel, src in files.items():
        if rel not in staged:
            problems.append(f"missing from dashboard/: {rel}")
        elif _sha(staged[rel]) != _sha(src):
            problems.append(f"content differs from canonical: {rel}")
    for rel in sorted(set(staged) - set(files)):
        problems.append(f"unexpected file in dashboard/: {rel}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify instead of rebuilding")
    ap.add_argument("--require-backend", action="store_true", help="fail if backend files are absent")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: parent of scripts/)")
    args = ap.parse_args(argv)
    plugin_root = args.root / PLUGIN_REL
    try:
        if args.check:
            problems = check(plugin_root, args.require_backend)
            for p in problems:
                print("DRIFT:", p)
            if not problems:
                print("dashboard/ matches canonical sources")
            return 1 if problems else 0
        staged = stage(plugin_root, args.require_backend)
    except StageError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    for rel in staged:
        print("staged", rel)
    absent = [r for r in (*BACKEND_FILES, *(d + "/" for d in BACKEND_DIRS))
              if not (plugin_root / r.rstrip("/")).exists()]
    if absent:
        print("note: backend not present yet, restage after it lands:", ", ".join(absent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
