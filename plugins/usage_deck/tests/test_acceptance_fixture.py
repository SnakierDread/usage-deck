"""Offline guards for the committed scratch_home fixture and the host acceptance harness.

The real host run (``python tests/acceptance/host_acceptance.py``) needs the ``hermes`` CLI and is not a unit
test; these tests keep its inputs honest without any network, host process or credential:

* the fixture is exactly 3 synthetic cloud accounts + 2 self-hosted endpoints and only carries visibly
  synthetic secrets (so a real credential can never be committed there by accident);
* the plugin's own discovery turns the fixture into that inventory (profile pool shadowing included);
* the harness's independent schema/source-binding validators accept contract-exact data and reject drift.
"""

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "plugins" / "usage_deck" / "tests" / "fixtures" / "scratch_home"
HARNESS = REPO / "tests" / "acceptance" / "host_acceptance.py"

_spec = importlib.util.spec_from_file_location("usage_deck_host_acceptance", HARNESS)
assert _spec is not None and _spec.loader is not None
harness = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness
_spec.loader.exec_module(harness)


def fixture_config(gpu="http://127.0.0.1:41001/v1", ollama="http://127.0.0.1:41002"):
    """What the host's config loader would hand the plugin for the rendered fixture config.yaml."""
    text = (FIXTURE / "config.yaml.template").read_text().replace("@@GPU_BASE_URL@@", gpu).replace(
        "@@OLLAMA_BASE_URL@@", ollama)
    match = re.search(r'api_key:\s*"([^"]+)"', text)
    assert match is not None
    api_key = match.group(1)
    return {"model": {"provider": "ollama", "base_url": ollama},
            "custom": [{"name": "gpu-box", "base_url": gpu, "api_key": api_key}]}


class FixtureTests(unittest.TestCase):
    def test_fixture_files_are_committed_and_parse(self):
        for rel in ("auth.json", "config.yaml.template", "profiles/work/auth.json", "profiles/work/config.yaml",
                    "profiles/verifier/auth.json"):
            self.assertTrue((FIXTURE / rel).is_file(), rel)
        json.loads((FIXTURE / "auth.json").read_text())
        json.loads((FIXTURE / "profiles" / "work" / "auth.json").read_text())
        json.loads((FIXTURE / "profiles" / "verifier" / "auth.json").read_text())

    def test_every_seeded_secret_is_visibly_synthetic(self):
        found = set()
        for rel in ("auth.json", "profiles/work/auth.json", "profiles/verifier/auth.json"):
            harness.collect_seeded_secrets(json.loads((FIXTURE / rel).read_text()), found)
        found |= harness.config_secrets((FIXTURE / "config.yaml.template").read_text())
        self.assertGreaterEqual(len(found), 8)
        for secret in found:
            self.assertTrue(secret.startswith(("UDSYN-", "sk-or-v1-udsyn", "eyJ")) or secret == "el", secret[:10])

    def test_discovery_yields_three_cloud_accounts_and_two_endpoints(self):
        cfg = fixture_config()
        src = FileInventorySource(
            FIXTURE, FIXTURE,
            config_loader=lambda: cfg,
            custom_provider_loader=lambda c: c["custom"],
            env_lookup=lambda name: None,
        )
        items = discover(src)
        self.assertEqual(sorted(i.provider_id for i in items),
                         ["custom:gpu-box", "nous", "ollama", "openai-codex", "openrouter"])
        by = {i.provider_id: i for i in items}
        self.assertEqual({k: v.kind for k, v in by.items()},
                         {"custom:gpu-box": "self_hosted", "nous": "cloud", "ollama": "self_hosted",
                          "openai-codex": "cloud", "openrouter": "cloud"})
        self.assertEqual((by["openai-codex"].plan_name, by["openai-codex"].plan_source), ("plus", "unverified_claim"))
        self.assertEqual(by["ollama"].auth_status, "missing")
        self.assertEqual(by["custom:gpu-box"].auth_status, "available")
        # the 2-char secret is part of the private context, so masking must run with it
        self.assertIn("el", by["nous"].secrets)

    def test_secondary_profile_pool_shadows_the_root_per_provider(self):
        cfg = {"model": {}, "custom": []}
        src = FileInventorySource(FIXTURE / "profiles" / "work", FIXTURE, config_loader=lambda: cfg,
                                  custom_provider_loader=lambda c: c["custom"], env_lookup=lambda n: None)
        items = {i.provider_id: i for i in discover(src)}
        self.assertEqual(sorted(items), ["nous", "openai-codex", "openrouter"])  # verifier's rows are not read here
        root = {i.provider_id: i for i in discover(FileInventorySource(
            FIXTURE, FIXTURE, config_loader=lambda: cfg, custom_provider_loader=lambda c: c["custom"],
            env_lookup=lambda n: None))}
        self.assertNotEqual(items["openrouter"].account_id, root["openrouter"].account_id)  # work's own row
        self.assertEqual(items["nous"].account_id, root["nous"].account_id)                 # fell back to root

    def test_machine_view_of_the_fixture_merges_codex_and_adds_verifier_only_anthropic(self):
        """The fixture is also the machine-view scratch fixture the real-host harness drives."""
        cfg = fixture_config()
        src = FileInventorySource(FIXTURE, FIXTURE, config_loader=lambda: cfg,
                                  custom_provider_loader=lambda c: c["custom"], env_lookup=lambda n: None)
        items = discover_machine(src)
        self.assertEqual(sorted(i.provider_id for i in items),
                         ["anthropic", "custom:gpu-box", "nous", "ollama", "openai-codex", "openrouter", "openrouter"])
        by = {}
        for item in items:
            by.setdefault(item.provider_id, []).append(item)
        codex = by["openai-codex"][0]
        self.assertEqual([(h.profile, h.is_global) for h in codex.holders], [("global", True), ("verifier", False)])
        self.assertEqual(codex.credential_refs, ((True, "global", "syn-codex-1"), (False, "verifier", "syn-codex-verifier-1")))
        self.assertEqual(sorted([(h.profile, h.is_global) for h in i.holders] for i in by["openrouter"]),
                         [[("global", True)], [("work", False)]])
        self.assertEqual([(h.profile, h.is_global) for h in by["anthropic"][0].holders], [("verifier", False)])
        self.assertEqual([(h.profile, h.is_global) for h in by["custom:gpu-box"][0].holders], [("global", True)])

    def test_no_real_hostnames_or_userinfo_in_fixture(self):
        blob = "".join(p.read_text() for p in FIXTURE.rglob("*") if p.is_file())
        self.assertNotRegex(blob, r"://[^/\s]*@")
        self.assertNotRegex(blob.replace("@@", ""), r"https?://(?!127\.0\.0\.1)")


class HarnessValidatorTests(unittest.TestCase):
    def test_harness_self_check_passes(self):
        results = harness.Results()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            harness.self_check(results)
        self.assertEqual(results.failed, [])
        self.assertGreaterEqual(len(results.items), 16)

    def test_validator_is_independent_of_the_plugin_and_flags_the_reported_defect(self):
        card = {"id": "acct_" + "0" * 24, "provider_id": "p", "account_id": "acct_" + "0" * 24, "provider_label": "P",
                "account_label": "Account 1", "kind": "cloud", "plan_name": None, "plan_source": "none",
                "auth_status": "missing", "endpoint_id": None, "state": "opaque", "confidence": "opaque",
                "reason": "unsupported", "meters": [], "checked_at": "2026-11-06T12:00:00Z", "stale": False,
                "error": None, "debug": {"adapter": "a", "capabilities": [], "source": "s", "sanitized": True},
                "holders": []}
        errs = []
        harness.validate_card(errs, "card", card)
        self.assertEqual(errs, [])
        garbled = {("provider_lab[REDACTED]" if k == "provider_label" else k): v for k, v in card.items()}
        errs = []
        harness.validate_card(errs, "card", garbled)
        self.assertTrue(any("keys differ" in e for e in errs), errs)

    def test_source_binding_flags_unbound_confidence(self):
        meter = {"id": "api_spend", "label": "x", "value": 1.0, "limit": None, "remaining": None, "unit": "usd",
                 "scope": "api_credential", "period": {"start": None, "end": None, "label": "unknown"},
                 "confidence": "exact", "source": "openrouter_key", "observed_at": "2026-11-06T12:00:00Z"}
        card = {"provider_id": "openrouter", "account_label": "Account 1", "meters": [meter], "confidence": "exact",
                "state": "ready", "kind": "cloud", "endpoint_id": None, "auth_status": "available", "error": None}
        self.assertEqual(harness.source_binding_violations([card], [], []), [])
        self.assertTrue(harness.source_binding_violations([dict(card, provider_id="nous")], [], []))
        series = [{"scope": "profile", "provider_id": None, "account_id": None, "confidence": "exact",
                   "source": "session_analytics"}]
        self.assertTrue(harness.source_binding_violations([], [], series))


if __name__ == "__main__":
    unittest.main()
