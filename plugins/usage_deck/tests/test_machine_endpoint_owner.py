"""Machine view probes a self-hosted endpoint that ONLY a secondary home's config defines (card t_793f0ed6).

Contract under test (C1-C4): an endpoint resolved from any enumerated home's config is probed with that owner
home's base_url (read-only ``/metrics`` + ``/v1/models``, ``/api/tags`` as the models fallback), anonymously unless
the OWNER home's own ``key_env`` supplies a key (no other home's value, and no process value, is ever sent);
credential-type items keep the selected-context rule (covered by ``test_machine_scope``/``test_codex_machine_scope``);
one card per normalized base_url even when the root mirrors the profile's definition; the health row says honestly
that the endpoint is owned by another profile's config (``profile_owned``).

It also pins the live regression: a box published through a CDN/WAF tunnel refuses the stock ``Python-urllib``
agent (403) while answering the same anonymous GET with 200, so every self-hosted probe carries the fixed
UsageDeck agent, and a probe that WAS sent never renders as "Not probed" (health ``unknown`` + reason
``source_unavailable`` + no error). Every value is synthetic and the transport is injected.
"""

import json
import os
import unittest
from pathlib import Path

from plugins.usage_deck.adapters.local import PROBE_USER_AGENT
from plugins.usage_deck.adapters.transport import (AllowedTarget, CrossProfileProbeTransport, GuardedTransport,
                                                   TransportError)
from plugins.usage_deck.discovery import FileInventorySource, discover_machine
from plugins.usage_deck.service import UsageDeckService

from .helpers import NOW, FakeAnalytics, ScratchHome, json_response, raw_response, scan_for

BOX = "https://llama-mac.example.net"
OTHER = "http://pgx.lan:8080"
METRICS = "process_cpu_percent 7.5\nprocess_resident_memory_bytes 2048\n"
OWNER_KEY = "sntl-owner-key-71c0d9a4e5b2"
VIEWED_KEY = "sntl-viewed-key-2f8b6e03a9d1"
ROOT_KEY = "sntl-root-key-b4e91c77d062"
PROC_KEY = "sntl-proc-key-0a3d5f8c1e94"
ALL_KEYS = [OWNER_KEY, VIEWED_KEY, ROOT_KEY, PROC_KEY]
FRONTEND_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "frontend" / "fixtures" / "machine_endpoint_owner_health.json"


class WafBox:
    """A tunnelled box: 403 (Cloudflare 1010) for a missing/stock-urllib agent, real answers otherwise.

    ``metrics``/``models`` choose each route's answer: ``"ok"`` | an HTTP status | ``"junk"`` (200, wrong body).
    Records every request's headers.
    """

    def __init__(self, origin=BOX, status=None, metrics="ok", models="ok", require=None, tags=None):
        self.origin = origin
        self.status = status  # force every answer to this status
        self.metrics = metrics
        self.models = models
        self.tags = tags  # an ollama-style /api/tags listing, if any
        self.require = require
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        if not url.startswith(self.origin + "/"):
            raise TransportError(TransportError.CONNECTION)
        agent = headers.get("User-Agent", "")
        if not agent or agent.lower().startswith("python-urllib"):
            return raw_response("error code: 1010", status=403)
        if self.status is not None:
            return raw_response("nope", status=self.status)
        if self.require is not None and headers.get("Authorization") != "Bearer " + self.require:
            return json_response({"error": "auth"}, status=401)
        if url.endswith("/metrics"):
            return self._answer(self.metrics, raw_response(METRICS, latency=3.0))
        if url.endswith("/v1/models"):
            return self._answer(self.models, json_response({"data": [{"id": "m"}]}, latency=9.0))
        if url.endswith("/api/tags") and self.tags is not None:
            return json_response({"models": self.tags}, latency=5.0)
        return raw_response("not found", status=404)

    @staticmethod
    def _answer(mode, good):
        if mode == "ok":
            return good
        if mode == "junk":
            return raw_response("<html>login</html>")
        return raw_response("nope", status=int(mode))

    def paths(self):
        return [u[len(self.origin):] for u, _h in self.requests if u.startswith(self.origin)]


class _Multi:
    """Routes a request to the box owning its origin; unknown origins are unreachable."""

    def __init__(self, *boxes):
        self.boxes = boxes

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        for box in self.boxes:
            if url.startswith(box.origin + "/"):
                return box.get(url, headers, timeout, max_bytes)
        raise TransportError(TransportError.CONNECTION)


class SecondaryHomeEndpoint(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("laptop", {"credential_pool": {}})
        # the viewed home ("work") defines only its own LAN box; the laptop profile defines the tunnel box
        self.viewed = {"model": {}, "custom_providers": [{"name": "pgx", "base_url": OTHER + "/v1"}]}
        self.laptop = {
            "model": {"provider": "custom:llama-mac", "base_url": BOX + "/v1"},
            "custom_providers": [{"name": "llama-mac", "base_url": BOX + "/v1", "model": "m"}],
        }
        self.configs = {"laptop": self.laptop}
        self.envs = {"laptop": {}}

    def source(self):
        def config_loader(directory):
            return self.configs.get(Path(directory).name)

        def env_loader(directory):
            return self.envs.get(Path(directory).name, {})

        return FileInventorySource(
            self.s.home, self.s.root,
            config_loader=lambda: self.viewed,
            custom_provider_loader=lambda cfg: cfg.get("custom_providers", []),
            env_lookup=lambda name: {"GPU_KEY": PROC_KEY}.get(name),
            home_config_loader=config_loader,
            home_env_loader=env_loader,
        )

    def service(self, transport):
        return UsageDeckService(
            inventory_factory=self.source,
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )

    def machine(self, transport):
        svc = self.service(transport)
        envelopes = [svc.handle(name, [("view", "machine")])[1] for name in ("subscriptions", "health")]
        cards = {c["provider_id"]: c for c in envelopes[0]["data"]["cards"]}
        health = {h["provider_id"]: h for h in envelopes[1]["data"]["entries"]}
        return cards, health, envelopes

    # -- C1 ------------------------------------------------------------------------------------------------

    def test_endpoint_defined_only_in_a_secondary_home_is_probed_with_the_owner_base_url(self):
        box = WafBox()
        cards, health, _envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        entry = health["custom:llama-mac"]
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"], entry["reason"]),
                         ("healthy", "self_hosted", True, None))
        self.assertEqual((entry["cpu_percent"], entry["memory_bytes"]), (7.5, 2048))
        self.assertEqual([(h["profile"], h["is_global"]) for h in cards["custom:llama-mac"]["holders"]],
                         [("laptop", False)])
        self.assertEqual(box.paths(), ["/metrics", "/v1/models"])  # read-only, owner origin, nothing else

    def test_models_up_and_metrics_absent_is_a_probe_result_not_not_probed(self):
        for metrics in (404, "junk"):
            with self.subTest(metrics=metrics):
                box = WafBox(metrics=metrics)
                _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
                entry = health["custom:llama-mac"]
                self.assertEqual(box.paths(), ["/metrics", "/v1/models"])
                self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"], entry["reason"]),
                                 ("degraded", "opaque", False, "metrics_unavailable"))
                self.assertIsNone(entry["error"])
                self.assertEqual(entry["latency_ms"], 9.0)  # the models probe is the latency evidence

    def test_ollama_style_tags_fallback_is_probed_too(self):
        box = WafBox(models=404, tags=[{"name": "m"}])
        _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(box.paths(), ["/metrics", "/v1/models", "/api/tags"])
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")

    def test_probe_is_anonymous_and_no_other_homes_value_is_ever_sent(self):
        box = WafBox()
        _cards, _health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        for _url, headers in box.requests:
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["User-Agent"], PROBE_USER_AGENT)
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    def test_the_owner_homes_own_key_env_authenticates_both_routes_alone(self):
        self.configs["laptop"] = {"custom_providers": [
            {"name": "llama-mac", "base_url": BOX + "/v1", "key_env": "GPU_KEY"}]}
        self.envs["laptop"] = {"GPU_KEY": OWNER_KEY}
        box = WafBox(require=OWNER_KEY)
        _cards, health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")
        self.assertEqual([h.get("Authorization") for _u, h in box.requests], ["Bearer " + OWNER_KEY] * 2)
        self.assertEqual(box.paths(), ["/metrics", "/v1/models"])
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    def test_an_inline_api_key_of_the_owner_home_is_never_sent(self):
        self.configs["laptop"] = {"custom_providers": [
            {"name": "llama-mac", "base_url": BOX + "/v1", "api_key": ROOT_KEY, "key_env": "GPU_KEY"}]}
        self.envs["laptop"] = {"GPU_KEY": OWNER_KEY}
        box = WafBox()
        _cards, _health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        # the inline key outranks key_env as the inference credential, and the key_env secret is then not a probe
        # credential either: both routes go out anonymously
        self.assertEqual({h.get("Authorization") for _u, h in box.requests}, {None})
        self.assertEqual(box.paths(), ["/metrics", "/v1/models"])
        self.assertEqual(scan_for(envelopes, [ROOT_KEY, OWNER_KEY]), [])

    def test_a_decoy_key_in_the_viewed_home_and_process_never_reaches_the_secondary_box(self):
        self.configs["laptop"] = {"custom_providers": [
            {"name": "llama-mac", "base_url": BOX + "/v1", "key_env": "GPU_KEY"}]}
        self.envs["laptop"] = {}  # the owner home does not define the variable; the process does
        box = WafBox()
        _cards, health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(box.requests, [])  # nothing sent: a missing key is never replaced by another home's value
        self.assertEqual(health["custom:llama-mac"]["reason"], "credentials_missing")
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    def test_the_cross_profile_ceiling_refuses_other_paths_and_verbs(self):
        # the guard the service builds for the card: only the three read-only routes of the owner origin pass
        box = WafBox()
        target = AllowedTarget("https", "llama-mac.example.net", None, ("/metrics", "/v1/models", "/api/tags"))
        guard = CrossProfileProbeTransport(GuardedTransport(box, [target]))
        for url in (BOX + "/v1/chat/completions", BOX + "/v1/models?x=1", BOX + "/admin", "https://evil.example/metrics"):
            with self.assertRaises(TransportError):
                guard.get(url)
        with self.assertRaises(TransportError):
            guard.request("POST", BOX + "/v1/models")
        self.assertEqual(box.requests, [])
        self.assertEqual(guard.get(BOX + "/v1/models", {"User-Agent": PROBE_USER_AGENT}).status, 200)

    # -- C3: honest failure + ownership --------------------------------------------------------------------

    def test_a_probe_the_box_refuses_is_not_reported_as_not_probed(self):
        for status, expected in ((403, ("degraded", "credentials_missing", None)),
                                 (500, ("unknown", "source_unavailable", "upstream_unavailable"))):
            with self.subTest(status=status):
                box = WafBox(status=status)
                _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
                entry = health["custom:llama-mac"]
                code = entry["error"]["code"] if entry["error"] else None
                self.assertEqual((entry["status"], entry["reason"], code), expected)
                # the UI shows "Not probed" only for unknown + source_unavailable + NO error: a sent probe has an error
                if entry["status"] == "unknown":
                    self.assertIsNotNone(entry["error"])
                self.assertIn("/metrics", box.paths())

    def test_an_offline_secondary_box_stays_quiet_opaque_and_owned(self):
        _cards, health, _env = self.machine(_Multi(WafBox("http://nowhere.invalid"), WafBox(OTHER)))
        entry = health["custom:llama-mac"]
        self.assertEqual((entry["status"], entry["confidence"], entry["reason"]), ("offline", "opaque", "box_offline"))
        self.assertIsNone(entry["error"])
        self.assertIs(entry["profile_owned"], True)

    def test_the_health_row_of_a_secondary_only_endpoint_is_profile_owned_the_selected_ones_are_not(self):
        _cards, health, _env = self.machine(_Multi(WafBox(), WafBox(OTHER)))
        self.assertIs(health["custom:llama-mac"]["profile_owned"], True)
        self.assertIs(health["custom:pgx"]["profile_owned"], False)  # the viewed home's own endpoint

    def test_the_timed_out_and_failed_health_rows_keep_the_ownership_flag(self):
        item = next(i for i in discover_machine(self.source()) if i.provider_id == "custom:llama-mac")
        svc = self.service(_Multi(WafBox()))
        self.assertIs(svc._offline_health(item, NOW, True)["profile_owned"], True)
        self.assertIs(svc._unknown_health(item, NOW, "timeout", True)["profile_owned"], True)
        self.assertIs(svc._offline_health(item, NOW, False)["profile_owned"], False)  # never outside the machine view

    def test_profile_view_never_reads_a_secondary_home_and_flags_nothing(self):
        box = WafBox()
        svc = self.service(_Multi(box, WafBox(OTHER)))
        health = svc.handle("health", [("view", "profile")])[1]["data"]["entries"]
        self.assertEqual([h["provider_id"] for h in health], ["custom:pgx"])
        self.assertEqual([h["profile_owned"] for h in health], [False])
        self.assertEqual(box.requests, [])

    def test_an_unsafe_secondary_endpoint_is_still_never_requested(self):
        self.configs["laptop"] = {"custom_providers": [{"name": "evil", "base_url": "http://user:pw@" + "x.lan:9/v1"}]}
        box = WafBox("http://x.lan:9")
        self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(box.requests, [])

    # -- C4 ------------------------------------------------------------------------------------------------

    def test_a_root_mirror_of_the_profile_entry_is_still_one_card_and_one_health_row(self):
        mirrored = {"model": {}, "custom_providers": self.viewed["custom_providers"] + [
            {"name": "llama-mac", "base_url": BOX + "/v1/", "model": "m"}]}  # trailing slash: same normalized url
        self.viewed = mirrored
        box = WafBox()
        cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(sorted(cards), ["custom:llama-mac", "custom:pgx"])
        self.assertEqual(len([h for h in health.values() if h["provider_id"] == "custom:llama-mac"]), 1)
        holders = sorted(h["profile"] for h in cards["custom:llama-mac"]["holders"])
        self.assertEqual(holders, ["laptop", "work"])
        items = [i for i in discover_machine(self.source()) if i.provider_id == "custom:llama-mac"]
        self.assertEqual(len(items), 1)
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")
        # the selected (mirroring) home owns the endpoint: not profile-owned, and one probe set, not two
        self.assertIs(health["custom:llama-mac"]["profile_owned"], False)
        self.assertEqual(sorted(box.paths()), ["/metrics", "/v1/models"])

    def test_the_selected_homes_definition_wins_the_health_row_over_the_secondary_one(self):
        self.viewed = {"model": {}, "custom_providers": [{"name": "llama-mac", "base_url": BOX + "/v1"}]}
        box = WafBox()
        _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")
        self.assertIs(health["custom:llama-mac"]["profile_owned"], False)
        # selected profile path: models probe + metrics, one health row, no duplicate cross-profile scrape
        self.assertEqual(sorted(box.paths()), ["/metrics", "/v1/models"])

    # -- service -> frontend ---------------------------------------------------------------------------------

    def scenario_rows(self):
        """The ``custom:llama-mac`` health row the real service produces in each ownership scenario."""
        rows = {}
        rows["secondary_only"] = self.machine(_Multi(WafBox(), WafBox(OTHER)))[1]["custom:llama-mac"]
        rows["models_only"] = self.machine(_Multi(WafBox(metrics=404), WafBox(OTHER)))[1]["custom:llama-mac"]
        rows["offline"] = self.machine(_Multi(WafBox("http://nowhere.invalid"), WafBox(OTHER)))[1]["custom:llama-mac"]
        self.viewed = {"model": {}, "custom_providers": self.viewed["custom_providers"] + [
            {"name": "llama-mac", "base_url": BOX + "/v1/", "model": "m"}]}
        rows["selected_mirror"] = self.machine(_Multi(WafBox(), WafBox(OTHER)))[1]["custom:llama-mac"]
        return rows

    def test_frontend_fixture_is_the_services_own_output(self):
        """``tests/frontend/machine.test.js`` renders these exact rows (both UI variants): they must be what the
        service emits today. Regenerate with ``USAGE_DECK_REGEN_FIXTURES=1`` after a deliberate wire change."""
        rows = self.scenario_rows()
        if os.environ.get("USAGE_DECK_REGEN_FIXTURES"):
            FRONTEND_FIXTURE.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", "utf-8")
        self.assertEqual(json.loads(FRONTEND_FIXTURE.read_text("utf-8")), rows)
        self.assertEqual({k: v["profile_owned"] for k, v in rows.items()},
                         {"secondary_only": True, "models_only": True, "offline": True, "selected_mirror": False})


if __name__ == "__main__":
    unittest.main()
