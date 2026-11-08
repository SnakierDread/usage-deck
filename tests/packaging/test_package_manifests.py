"""Guards for the git-installable layout (stdlib unittest).

The desktop app stages the desktop half of ``plugins/usage_deck`` when the package is installed from
Git. These tests pin the layout rules that install depends on (see README "Desktop half" and the PR
that introduced them): the two metadata manifests agree with the real package, the entry files exist,
the tree is symlink-free and well under the 64 MB cap, and nothing at the repository root can turn
the root into a second package or win the desktop-entry search.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "plugins" / "usage_deck"
DESKTOP = PKG / "desktop"
MAX_BYTES = 64 * 1024 * 1024  # desktop-plugin-sdk.md "One package, both SDKs"


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def _plugin_yaml_field(field: str) -> str:
    m = re.search(rf"^{field}:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", (PKG / "plugin.yaml").read_text("utf-8"), re.M)
    assert m, field
    return m.group(1)


def _tracked_tree():
    """Everything the installer could copy: the repo minus .git, bytecode and untracked scratch dirs."""
    skip = {".git", "__pycache__", ".worktrees"}
    stack = [REPO]
    while stack:
        d = stack.pop()
        for p in d.iterdir():
            if p.name in skip:
                continue
            yield p
            if p.is_dir() and not p.is_symlink():
                stack.append(p)


class ManifestAgreementTests(unittest.TestCase):
    def test_root_manifest_describes_the_package(self):
        m = _load(REPO / "manifest.json")
        self.assertEqual(m["name"], _plugin_yaml_field("name"))
        self.assertEqual(m["version"], _plugin_yaml_field("version"))
        self.assertEqual(m["package"], "plugins/usage_deck")
        self.assertTrue((REPO / m["entry"]).is_file(), m["entry"])
        self.assertEqual((REPO / m["entry"]).resolve(), (DESKTOP / "plugin.js").resolve())

    def test_desktop_manifest_mirrors_root_manifest(self):
        root, desk = _load(REPO / "manifest.json"), _load(DESKTOP / "manifest.json")
        self.assertEqual(desk["name"], root["name"])
        self.assertEqual(desk["version"], root["version"])
        self.assertEqual((DESKTOP / desk["entry"]).resolve(), (REPO / root["entry"]).resolve())

    def test_versions_match_dashboard_manifest(self):
        self.assertEqual(_load(REPO / "manifest.json")["version"], _load(PKG / "manifest.json")["version"])

    def test_manifests_are_not_mistaken_for_dashboard_manifests(self):
        # The host's dashboard scan keys on an "api"/"tab" manifest; ours must carry neither.
        for p in (REPO / "manifest.json", DESKTOP / "manifest.json"):
            self.assertFalse({"api", "tab", "css"} & set(_load(p)), p)

    def test_desktop_manifest_names_the_plugin_id_of_plugin_js(self):
        js = (DESKTOP / "plugin.js").read_text("utf-8")
        m = re.search(r"const PLUGIN_ID = '([^']+)'", js)
        assert m is not None
        self.assertEqual(m.group(1), _load(DESKTOP / "manifest.json")["name"])


class InstallerLayoutTests(unittest.TestCase):
    """The installer's rules, restated: desktop-plugins-root.ts `desktopHalfSource`
    (root plugin.js wins, then desktop/plugin.js) and desktop-plugin-install.ts `detectPluginComponents`."""

    def test_no_root_plugin_js(self):
        # A root plugin.js would win and publish the WHOLE repository as the desktop payload.
        self.assertFalse((REPO / "plugin.js").exists())

    def test_desktop_entry_is_found_under_the_package(self):
        self.assertTrue((DESKTOP / "plugin.js").is_file())
        self.assertFalse((PKG / "plugin.js").exists())  # same reason, one level down

    def test_root_is_not_an_agent_package(self):
        # No root plugin.yaml/plugin.json/__init__.py/dashboard/: the repo root must stay "not a
        # package" so the only package the installer can pick is plugins/usage_deck.
        for name in ("plugin.yaml", "plugin.yml", "plugin.json", "__init__.py", "dashboard"):
            self.assertFalse((REPO / name).exists(), name)

    def test_package_is_an_agent_package(self):
        self.assertTrue((PKG / "plugin.yaml").is_file())
        self.assertTrue((PKG / "__init__.py").is_file())
        self.assertTrue((PKG / "dashboard" / "manifest.json").is_file())

    def test_desktop_payload_is_exactly_plugin_js_and_manifest(self):
        files = sorted(p.name for p in DESKTOP.iterdir())
        self.assertEqual(files, ["manifest.json", "plugin.js"])

    def test_no_symlinks_anywhere(self):
        links = [str(p.relative_to(REPO)) for p in _tracked_tree() if p.is_symlink()]
        self.assertEqual(links, [])

    def test_tree_is_far_under_the_size_cap(self):
        total = sum(p.stat().st_size for p in _tracked_tree() if p.is_file())
        self.assertLess(total, MAX_BYTES // 8, total)


if __name__ == "__main__":
    unittest.main()
