#!/usr/bin/env python3
"""Deterministically stage the repository-root ``plugin.js`` from the canonical desktop half.

Why a root ``plugin.js`` exists: the Hermes desktop app's *Install from Git* probe scans the clone root
of a bare ``https://github.com/owner/repo`` URL for ``plugin.yaml`` / ``plugin.json`` / ``plugin.js`` /
``desktop/plugin.js``. This monorepo keeps its package under ``plugins/usage_deck`` so the bare URL found
nothing ("No agent or desktop plugin artifacts found"). A root ``plugin.js`` makes the bare URL install
the **desktop half** (the whole repo tree is then published as that half, ``.git`` and ``__pycache__``
excluded).

It must be a byte-for-byte copy, never a shim: the desktop plugin loader accepts only the specifiers
``@hermes/plugin-sdk``, ``react`` and ``react/jsx-runtime`` (a relative re-export fails as an unsupported
import), and when the agent half is later installed the app adopts a marker-less folder as that package's
desktop half only if its ``plugin.js`` equals ``plugins/usage_deck/desktop/plugin.js`` byte for byte.

The canonical source is ``plugins/usage_deck/desktop/plugin.js`` (hand-edited); the root file is
*generated* and must never be edited by hand. Rebuild after every desktop-half change:

    python3 scripts/stage_root_entry.py            # rewrite ./plugin.js
    python3 scripts/stage_root_entry.py --check    # exit 1 if ./plugin.js drifted from the source

Stdlib only; the output is a pure function of the source bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

SOURCE_REL = Path("plugins") / "usage_deck" / "desktop" / "plugin.js"
TARGET_REL = Path("plugin.js")


class StageError(RuntimeError):
    pass


def _source(root: Path) -> Path:
    src = root / SOURCE_REL
    if src.is_symlink() or not src.is_file():
        raise StageError(f"missing canonical desktop entry (or it is a symlink): {SOURCE_REL.as_posix()}")
    return src


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(root: Path) -> str:
    """Write ``<root>/plugin.js`` from the canonical source; return its sha256."""
    data = _source(root).read_bytes()
    target = root / TARGET_REL
    if target.is_symlink():
        raise StageError("root plugin.js must not be a symlink")
    target.write_bytes(data)
    os.chmod(target, 0o644)
    return _sha(data)


def check(root: Path) -> list[str]:
    """Return drift problems (empty == the root entry is a byte-exact copy of the canonical source)."""
    data = _source(root).read_bytes()
    target = root / TARGET_REL
    if target.is_symlink():
        return ["root plugin.js is a symlink"]
    if not target.is_file():
        return ["root plugin.js does not exist (run scripts/stage_root_entry.py)"]
    if target.read_bytes() != data:
        return [f"root plugin.js differs from {SOURCE_REL.as_posix()} (run scripts/stage_root_entry.py)"]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify instead of rewriting")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: parent of scripts/)")
    args = ap.parse_args(argv)
    try:
        if args.check:
            problems = check(args.root)
            for p in problems:
                print("DRIFT:", p)
            if not problems:
                print("root plugin.js matches", SOURCE_REL.as_posix())
            return 1 if problems else 0
        digest = stage(args.root)
    except StageError as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    print(f"staged {TARGET_REL.as_posix()} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
