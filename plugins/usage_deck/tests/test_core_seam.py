"""The real core hook -> real plugin loader -> observer -> schema-2 store, in a subprocess on a scratch home.

Needs a Hermes checkout that already carries the ``provider_ratelimit_headers`` hook (core card t_3a6bf27f,
independent PASS t_67c019db): point ``USAGE_DECK_CORE_CHECKOUT`` at it. Skipped otherwise (the installed host has
no such hook yet). The callback is a synthetic test call into the core dispatch function; nothing is requested
from any provider and nothing here is a live observation.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

CORE = os.environ.get("USAGE_DECK_CORE_CHECKOUT", "")
PLUGIN_SRC = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("seam_probe.py")
HAVE_CORE = bool(CORE) and (Path(CORE) / "agent" / "provider_ratelimit_headers.py").is_file()


def run_probe(scenario):
    env = {"PATH": os.environ.get("PATH", ""), "HERMES_RUNTIME_DIR": os.environ.get("HERMES_RUNTIME_DIR", "")}
    proc = subprocess.run([sys.executable, str(PROBE), CORE, str(PLUGIN_SRC), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=120, env=env, cwd="/tmp")
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if proc.returncode != 0 or not lines:
        raise AssertionError("probe failed: " + proc.stderr[-400:])
    return json.loads(lines[-1][len("RESULT "):])


@unittest.skipUnless(HAVE_CORE, "set USAGE_DECK_CORE_CHECKOUT to a Hermes checkout with provider_ratelimit_headers")
class CoreHookToStore(unittest.TestCase):
    def test_opt_in_profile_records_through_the_real_dispatch(self):
        result = run_probe({"setting": "true"})
        self.assertTrue(result["has_hook"])  # the real loader registered the plugin's subscriber
        (entry,) = result["store"]["captures"]
        self.assertEqual(result["store"]["schema_version"], 2)
        self.assertEqual(entry["holder"], {"profile": "work", "is_global": False})
        self.assertEqual(entry["credential_id"], "ant-row-1")
        self.assertEqual(entry["headers"], {"anthropic-ratelimit-unified-5h-reset": "4102444000",
                                            "anthropic-ratelimit-unified-5h-utilization": "0.25",
                                            "anthropic-ratelimit-unified-7d-utilization": "0.5"})
        text = json.dumps(result["store"])
        for leak in ("must-not-persist", "cookie", "hermes_root", "execution"):
            self.assertNotIn(leak, text)

    def test_default_and_explicit_opt_out_write_nothing(self):
        for setting in (None, "false", "'true'", "1"):
            with self.subTest(setting=setting):
                result = run_probe({"setting": setting})
                self.assertTrue(result["has_hook"])
                self.assertIsNone(result["store"])
                self.assertEqual([p for p in result["tree"] if "plugin-data" in p], [])

    def test_global_fallback_row_is_attributed_to_the_global_holder(self):
        result = run_probe({"setting": "true", "holder": ["global", True], "row": "g-row"})
        (entry,) = result["store"]["captures"]
        self.assertEqual((entry["holder"], entry["credential_id"]), ({"profile": "global", "is_global": True}, "g-row"))

    def test_root_agent_uses_its_own_config(self):
        result = run_probe({"setting": "true", "home": "root", "execution": "global", "holder": ["global", True]})
        (entry,) = result["store"]["captures"]
        self.assertEqual(entry["holder"], {"profile": "global", "is_global": True})


if __name__ == "__main__":
    unittest.main()
