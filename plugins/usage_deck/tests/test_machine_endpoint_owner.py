"""Machine view probes a self-hosted endpoint that ONLY a secondary home's config defines (card t_793f0ed6).

Contract under test: an endpoint resolved from any enumerated home's config is probed with that owner home's
base_url, anonymously unless the OWNER home's own ``key_env`` supplies a key (no other home's value, and no
process value, is ever sent); credential-type items keep the selected-context rule (covered by
``test_machine_scope``/``test_codex_machine_scope``); one card per normalized base_url even when the root mirrors
the profile's definition.

It also pins the live regression: a box published through a CDN/WAF tunnel refuses the stock ``Python-urllib``
agent (403) while answering the same anonymous GET with 200, so every self-hosted probe carries the fixed
UsageDeck agent, and a probe that WAS sent never renders as "Not probed" (reason ``source_unavailable``).
Every value is synthetic and the transport is injected.
"""

import unittest
from pathlib import Path

from plugins.usage_deck.adapters.local import PROBE_USER_AGENT
from plugins.usage_deck.adapters.transport import TransportError
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


class WafBox:
    """A tunnelled box: 403 (Cloudflare 1010) for a missing/stock-urllib agent, 200 otherwise. Records headers."""

    def __init__(self, origin=BOX, status=None, metrics_body=METRICS, require=None):
        self.origin = origin
        self.status = status  # force every answer to this status
        self.metrics_body = metrics_body
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
            return raw_response(self.metrics_body, latency=3.0)
        if url.endswith("/v1/models"):
            return json_response({"data": [{"id": "m"}]}, latency=9.0)
        return raw_response("not found", status=404)

    def paths(self):
        return [u[len(self.origin):] for u, _h in self.requests if u.startswith(self.origin)]


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
        self.viewed_env = {"GPU_KEY": VIEWED_KEY}

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

    def machine(self, transport):
        svc = UsageDeckService(
            inventory_factory=self.source,
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )
        envelopes = [svc.handle(name, [("view", "machine")])[1] for name in ("subscriptions", "health")]
        cards = {c["provider_id"]: c for c in envelopes[0]["data"]["cards"]}
        health = {h["provider_id"]: h for h in envelopes[1]["data"]["entries"]}
        return cards, health, envelopes

    # -- C1 ------------------------------------------------------------------------------------------------

    def test_endpoint_defined_only_in_a_secondary_home_is_probed_with_the_owner_base_url(self):
        box = WafBox()
        cards, health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        entry = health["custom:llama-mac"]
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"], entry["reason"]),
                         ("healthy", "self_hosted", True, None))
        self.assertEqual((entry["cpu_percent"], entry["memory_bytes"]), (7.5, 2048))
        self.assertEqual([(h["profile"], h["is_global"]) for h in cards["custom:llama-mac"]["holders"]],
                         [("laptop", False)])
        self.assertEqual(box.paths(), ["/metrics"])  # read-only, the one cross-profile request

    def test_probe_is_anonymous_and_no_other_homes_value_is_ever_sent(self):
        box = WafBox()
        _cards, _health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        for _url, headers in box.requests:
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["User-Agent"], PROBE_USER_AGENT)
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    def test_the_owner_homes_own_key_env_authenticates_the_scrape_alone(self):
        self.configs["laptop"] = {"custom_providers": [
            {"name": "llama-mac", "base_url": BOX + "/v1", "key_env": "GPU_KEY"}]}
        self.envs["laptop"] = {"GPU_KEY": OWNER_KEY}
        box = WafBox(require=OWNER_KEY)
        _cards, health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")
        self.assertEqual([h.get("Authorization") for _u, h in box.requests], ["Bearer " + OWNER_KEY])
        self.assertEqual(box.paths(), ["/metrics"])
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    def test_a_decoy_key_in_the_viewed_home_and_process_never_reaches_the_secondary_box(self):
        self.configs["laptop"] = {"custom_providers": [
            {"name": "llama-mac", "base_url": BOX + "/v1", "key_env": "GPU_KEY"}]}
        self.envs["laptop"] = {}  # the owner home does not define the variable; the viewed home/process do
        box = WafBox()
        _cards, health, envelopes = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(box.requests, [])  # nothing sent: a missing key is never replaced by another home's value
        self.assertEqual(health["custom:llama-mac"]["reason"], "credentials_missing")
        self.assertEqual(scan_for(envelopes, ALL_KEYS), [])

    # -- C3 ------------------------------------------------------------------------------------------------

    def test_a_probe_the_box_refuses_is_not_reported_as_not_probed(self):
        for status, expected in ((403, ("degraded", "credentials_missing")),
                                 (500, ("unknown", "metrics_unavailable"))):
            with self.subTest(status=status):
                box = WafBox(status=status)
                _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
                entry = health["custom:llama-mac"]
                self.assertEqual((entry["status"], entry["reason"]), expected)
                self.assertNotEqual(entry["reason"], "source_unavailable")  # the UI's "Not probed" key
                self.assertEqual(box.paths(), ["/metrics"])

    def test_an_invalid_exposition_is_metrics_unavailable_not_not_probed(self):
        box = WafBox(metrics_body="<html>login</html>")
        _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual((health["custom:llama-mac"]["status"], health["custom:llama-mac"]["reason"]),
                         ("unknown", "metrics_unavailable"))

    def test_an_offline_secondary_box_stays_quiet_opaque(self):
        _cards, health, _env = self.machine(_Multi(WafBox("http://nowhere.invalid"), WafBox(OTHER)))
        entry = health["custom:llama-mac"]
        self.assertEqual((entry["status"], entry["confidence"], entry["reason"]), ("offline", "opaque", "box_offline"))
        self.assertIsNone(entry["error"])

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

    def test_the_selected_homes_definition_wins_the_health_row_over_the_secondary_one(self):
        self.viewed = {"model": {}, "custom_providers": [{"name": "llama-mac", "base_url": BOX + "/v1"}]}
        box = WafBox()
        _cards, health, _env = self.machine(_Multi(box, WafBox(OTHER)))
        self.assertEqual(health["custom:llama-mac"]["status"], "healthy")
        # selected profile path: models probe + metrics, one health row, no duplicate cross-profile scrape
        self.assertEqual(sorted(box.paths()), ["/metrics", "/v1/models"])


class _Multi:
    """Routes a request to the box owning its origin; unknown origins are unreachable."""

    def __init__(self, *boxes):
        self.boxes = boxes

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        for box in self.boxes:
            if url.startswith(box.origin + "/"):
                return box.get(url, headers, timeout, max_bytes)
        raise TransportError(TransportError.CONNECTION)


if __name__ == "__main__":
    unittest.main()
