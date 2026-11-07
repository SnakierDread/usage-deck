"""Packaging tests for scripts/stage_dashboard.py (stdlib unittest)."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("stage_dashboard", REPO / "scripts" / "stage_dashboard.py")
assert _spec is not None and _spec.loader is not None
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


def _make_plugin(root: Path, backend: bool) -> Path:
    p = root / "plugins" / "usage_deck"
    (p / "dist").mkdir(parents=True)
    (p / "manifest.json").write_text('{"name":"usage-deck"}')
    (p / "dist" / "index.js").write_text("js")
    (p / "dist" / "style.css").write_text("css")
    if backend:
        (p / "__init__.py").write_text("")
        (p / "plugin_api.py").write_text("router = None\n")
        (p / "discovery.py").write_text("d")
        (p / "captures.py").write_text("c")
        (p / "service.py").write_text("s")
        for d in ("adapters", "models"):
            (p / d).mkdir()
            (p / d / "__init__.py").write_text("")
            (p / d / "mod.py").write_text(d)
            (p / d / "__pycache__").mkdir()
            (p / d / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"\0")
        (p / "tests").mkdir()
        (p / "tests" / "test_x.py").write_text("t")
    return p


class StageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_frontend_only_stage_then_check_clean(self):
        p = _make_plugin(self.tmp, backend=False)
        staged = sd.stage(p)
        self.assertEqual(staged, ["dist/index.js", "dist/style.css", "manifest.json"])
        self.assertEqual(sd.check(p), [])
        self.assertEqual((p / "dashboard" / "manifest.json").read_bytes(), (p / "manifest.json").read_bytes())

    def test_backend_copied_without_tests_or_bytecode(self):
        p = _make_plugin(self.tmp, backend=True)
        staged = sd.stage(p, require_backend=True)
        self.assertIn("plugin_api.py", staged)
        self.assertIn("captures.py", staged)
        self.assertIn("adapters/mod.py", staged)
        self.assertIn("models/__init__.py", staged)
        self.assertFalse(any("tests" in s or "__pycache__" in s or s.endswith(".pyc") for s in staged))
        self.assertEqual(sd.check(p, require_backend=True), [])

    def test_require_backend_fails_when_absent(self):
        p = _make_plugin(self.tmp, backend=False)
        with self.assertRaises(sd.StageError):
            sd.stage(p, require_backend=True)

    def test_deterministic_and_removes_stale_files(self):
        p = _make_plugin(self.tmp, backend=True)
        sd.stage(p)
        (p / "dashboard" / "stale.py").write_text("old")
        first = {r: (p / "dashboard" / r).read_bytes() for r in sd.plan(p)}
        self.assertTrue(any("stale.py" in x for x in sd.check(p)))
        sd.stage(p)
        self.assertFalse((p / "dashboard" / "stale.py").exists())
        self.assertEqual(first, {r: (p / "dashboard" / r).read_bytes() for r in sd.plan(p)})
        self.assertEqual(sorted(sd.plan(p)), list(sd.plan(p)))
        mode = (p / "dashboard" / "dist" / "index.js").stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_check_detects_hand_edit_and_missing(self):
        p = _make_plugin(self.tmp, backend=False)
        sd.stage(p)
        (p / "dashboard" / "dist" / "index.js").write_text("tampered")
        self.assertEqual(sd.check(p), ["content differs from canonical: dist/index.js"])
        (p / "dashboard" / "dist" / "style.css").unlink()
        self.assertIn("missing from dashboard/: dist/style.css", sd.check(p))

    def test_refuses_symlinks(self):
        p = _make_plugin(self.tmp, backend=True)
        os.symlink("/etc/passwd", p / "adapters" / "evil.py")
        with self.assertRaises(sd.StageError):
            sd.plan(p)


class RepoPackagingTests(unittest.TestCase):
    """Run against the real repository tree."""

    def test_staged_dashboard_matches_canonical_sources(self):
        plugin = REPO / "plugins" / "usage_deck"
        self.assertEqual(sd.check(plugin), [], "run: python3 scripts/stage_dashboard.py")

    def test_manifest_and_yaml_agree_on_identity(self):
        plugin = REPO / "plugins" / "usage_deck"
        m = json.loads((plugin / "dashboard" / "manifest.json").read_text())
        self.assertEqual((m["name"], m["version"]), ("usage-deck", "0.1.0"))
        y = (plugin / "plugin.yaml").read_text()
        self.assertIn("name: usage-deck", y)
        self.assertIn("version: 0.1.0", y)
        for rel in (m["entry"], m["css"]):
            self.assertTrue((plugin / "dashboard" / rel).is_file(), rel)

    def test_no_symlinks_in_package(self):
        plugin = REPO / "plugins" / "usage_deck"
        for dirpath, dirs, files in os.walk(plugin):
            for n in dirs + files:
                self.assertFalse((Path(dirpath) / n).is_symlink(), n)


if __name__ == "__main__":
    unittest.main()
