"""Packaging tests for scripts/stage_root_entry.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("stage_root_entry", REPO / "scripts" / "stage_root_entry.py")
assert _spec is not None and _spec.loader is not None
sre = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sre)

SOURCE = b"// desktop half\nimport * as ui from '@hermes/plugin-sdk'\nexport default { id: 'x' }\n"


def _make_repo(root: Path) -> Path:
    src = root / "plugins" / "usage_deck" / "desktop" / "plugin.js"
    src.parent.mkdir(parents=True)
    src.write_bytes(SOURCE)
    return root


class StageRootEntryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = _make_repo(Path(self._tmp.name))

    def test_stage_writes_a_byte_exact_copy_and_check_is_clean(self):
        digest = sre.stage(self.root)
        self.assertEqual((self.root / "plugin.js").read_bytes(), SOURCE)
        self.assertEqual(len(digest), 64)
        self.assertEqual(sre.check(self.root), [])
        self.assertEqual((self.root / "plugin.js").stat().st_mode & 0o777, 0o644)

    def test_check_reports_missing_and_drift(self):
        self.assertIn("does not exist", sre.check(self.root)[0])
        sre.stage(self.root)
        (self.root / "plugin.js").write_bytes(SOURCE + b"// hand edit\n")
        self.assertIn("differs", sre.check(self.root)[0])
        # one changed byte is drift: the app's adoption rule compares bytes
        (self.root / "plugin.js").write_bytes(SOURCE.replace(b"x", b"y"))
        self.assertTrue(sre.check(self.root))

    def test_stage_repairs_drift(self):
        (self.root / "plugin.js").write_text("stale")
        sre.stage(self.root)
        self.assertEqual(sre.check(self.root), [])

    def test_missing_or_symlinked_source_is_an_error(self):
        src = self.root / "plugins" / "usage_deck" / "desktop" / "plugin.js"
        src.unlink()
        with self.assertRaises(sre.StageError):
            sre.stage(self.root)
        os.symlink("/etc/passwd", src)
        with self.assertRaises(sre.StageError):
            sre.check(self.root)

    def test_symlinked_target_is_refused(self):
        os.symlink("/etc/passwd", self.root / "plugin.js")
        with self.assertRaises(sre.StageError):
            sre.stage(self.root)
        self.assertEqual(sre.check(self.root), ["root plugin.js is a symlink"])

    def test_main_exit_codes(self):
        self.assertEqual(sre.main(["--root", str(self.root), "--check"]), 1)
        self.assertEqual(sre.main(["--root", str(self.root)]), 0)
        self.assertEqual(sre.main(["--root", str(self.root), "--check"]), 0)


class RepoRootEntryTests(unittest.TestCase):
    """Run against the real repository tree."""

    def test_root_plugin_js_matches_canonical_source(self):
        self.assertEqual(sre.check(REPO), [], "run: python3 scripts/stage_root_entry.py")


if __name__ == "__main__":
    unittest.main()
