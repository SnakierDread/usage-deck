"""Self-hosted /metrics + health probes authenticate with the endpoint's resolved ``key_env`` secret.

Regression: a llama-swap box that demands a key on EVERY route (incl. /metrics) was reported
``degraded: metrics_unavailable`` because only /v1/models carried the resolved key_env secret.
All synthetic: no real HERMES_HOME, no real credentials; the socket test only binds 127.0.0.1.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from plugins.usage_deck.adapters.transport import TransportError, UrllibTransport
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine
from plugins.usage_deck.service import UsageDeckService

from .helpers import (
    NOW,
    SENTINEL_KEY,
    SENTINEL_TOKEN,
    FakeAnalytics,
    ScratchHome,
    json_response,
    raw_response,
    scan_for,
)

BOX = "http://desktop.lan:8080"
ENV_NAME = "HERMES_CUSTOM_DESKTOP_8080_API_KEY"
ENV_VALUE = "sntl-env-a7c3e91d05b248f6"
METRICS = "llamaswap_cpu_util_percent 12.5\nllamaswap_memory_total_bytes 1000\n"
ANON_TEXT = "The provider rejected the stored credential."


class StrictBoxTransport:
    """llama-swap with ``apiKeys``: EVERY route (incl. /metrics) answers 401 without the exact Bearer."""

    def __init__(self, good=ENV_VALUE):
        self.good = good
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        if headers.get("Authorization") != "Bearer " + self.good:
            return json_response({"error": {"type": "authentication_error"}}, status=401)
        if url == BOX + "/metrics":
            return raw_response(METRICS, latency=2.0)
        if url == BOX + "/v1/models":
            return json_response({"data": [{"id": "m"}]}, latency=6.0)
        if url == BOX + "/api/tags":
            return raw_response("not found", status=404)
        raise TransportError(TransportError.CONNECTION)

    def auth_for(self, path):
        return [h.get("Authorization") for u, h in self.requests if u == BOX + path]


class Base(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.env = {}
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]

    def source(self):
        return FileInventorySource(
            self.s.home, self.s.root,
            config_loader=lambda: {"model": {}},
            custom_provider_loader=lambda c: self.custom,
            env_lookup=lambda name: self.env.get(name),
        )

    def deck(self, transport, view=None):
        src = self.source()
        svc = UsageDeckService(
            inventory_factory=lambda: src,
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )
        params = [("view", view)] if view else []
        _, subs = svc.handle("subscriptions", params)
        _, health = svc.handle("health", params)
        return subs["data"]["cards"], health["data"]["entries"], (subs, health)


class KeyEnvReachesProbes(Base):
    def test_resolved_key_env_is_attached_to_metrics_and_health(self):
        self.env[ENV_NAME] = ENV_VALUE
        box = StrictBoxTransport()
        cards, health, envelopes = self.deck(box)
        bearer = "Bearer " + ENV_VALUE
        self.assertEqual(box.auth_for("/metrics"), [bearer])
        self.assertTrue(box.auth_for("/v1/models") and all(a == bearer for a in box.auth_for("/v1/models")))
        (entry,) = health
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"]),
                         ("healthy", "self_hosted", True))
        self.assertEqual((entry["reason"], entry["error"]), (None, None))
        self.assertEqual(cards[0]["auth_status"], "available")
        self.assertEqual(cards[0]["confidence"], "self_hosted")
        self.assertEqual(scan_for(envelopes, [ENV_VALUE]), [])

    def test_api_key_env_alias_reaches_metrics(self):
        self.env[ENV_NAME] = ENV_VALUE
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key_env": ENV_NAME}]
        box = StrictBoxTransport()
        _cards, health, _ = self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), ["Bearer " + ENV_VALUE])
        self.assertEqual(health[0]["status"], "healthy")

    def test_machine_view_attaches_the_selected_homes_key_env(self):
        self.env[ENV_NAME] = ENV_VALUE
        box = StrictBoxTransport()
        _cards, health, _ = self.deck(box, view="machine")
        self.assertEqual(box.auth_for("/metrics"), ["Bearer " + ENV_VALUE])
        self.assertEqual(health[0]["status"], "healthy")

    def test_discovery_exposes_it_as_health_credential_only_from_key_env(self):
        self.env[ENV_NAME] = ENV_VALUE
        (item,) = discover(self.source())
        (machine,) = discover_machine(self.source())
        for found in (item, machine):
            assert found.health_credential is not None
            self.assertEqual(found.health_credential.reveal(), ENV_VALUE)

    def test_inline_api_key_never_reaches_metrics(self):
        """Egress rule: an inline/pool inference key is not a metrics credential."""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key": SENTINEL_KEY, "key_env": ENV_NAME}]
        self.env[ENV_NAME] = ENV_VALUE
        (item,) = discover(self.source())
        self.assertEqual((item.credential.reveal(), item.health_credential), (SENTINEL_KEY, None))
        box = StrictBoxTransport(good=SENTINEL_KEY)
        _cards, health, _ = self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), [None])
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("degraded", "metrics_unavailable"))

    def test_pool_row_credential_never_reaches_metrics(self):
        self.custom = []
        self.s.write_auth({"credential_pool": {"custom:desktop": [
            {"id": "r1", "base_url": BOX + "/v1", "access_token": SENTINEL_TOKEN}]}})
        (item,) = discover(self.source())
        self.assertIsNone(item.health_credential)
        box = StrictBoxTransport(good=SENTINEL_TOKEN)
        self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), [None])


class HonestTaxonomyWithoutAKey(Base):
    def test_no_key_env_configured_is_credentials_missing_not_rejected(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        box = StrictBoxTransport()
        cards, health, envelopes = self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), [None])
        self.assertTrue(all(a is None for a in box.auth_for("/v1/models")))
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "missing")
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"]),
                         ("unknown", "opaque", False))
        self.assertEqual((entry["reason"], entry["error"]), ("credentials_missing", None))
        self.assertIsNone(entry["cpu_percent"])  # no estimation from an unauthenticated probe
        self.assertNotIn(ANON_TEXT, json.dumps(envelopes))

    def test_unresolved_key_env_sends_nothing(self):
        box = StrictBoxTransport()
        cards, health, _ = self.deck(box)
        self.assertEqual(box.requests, [])
        self.assertEqual(cards[0]["auth_status"], "missing")
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("unknown", "credentials_missing"))

    def test_wrong_key_env_value_is_rejected_with_no_metrics(self):
        self.env[ENV_NAME] = SENTINEL_TOKEN
        box = StrictBoxTransport()
        cards, health, envelopes = self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), ["Bearer " + SENTINEL_TOKEN])
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "rejected")
        self.assertEqual((entry["status"], entry["metrics_available"], entry["error"]["code"]),
                         ("unknown", False, "auth_required"))
        self.assertEqual(entry["confidence"], "opaque")
        self.assertEqual(scan_for(envelopes, [SENTINEL_TOKEN]), [])

    def test_machine_only_member_makes_no_request(self):
        """Another profile's key_env/credentials are inventory only: the viewed home alone supplies them."""
        self.custom = []
        self.s.write_profile("other", {"credential_pool": {"custom:desktop": [
            {"id": "o1", "base_url": BOX + "/v1", "access_token": SENTINEL_TOKEN}]}})
        box = StrictBoxTransport(good=SENTINEL_TOKEN)
        self.deck(box, view="machine")
        self.assertEqual(box.requests, [])


class _Handler(BaseHTTPRequestHandler):
    key = ENV_VALUE
    seen = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + type(self).key:
            body, code, ctype = b'{"error":{"type":"authentication_error"}}', 401, "application/json"
        elif self.path == "/metrics":
            body, code, ctype = METRICS.encode(), 200, "text/plain"
        elif self.path == "/v1/models":
            body, code, ctype = b'{"data":[{"id":"m"}]}', 200, "application/json"
        else:
            body, code, ctype = b"{}", 404, "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


class RealSocketTests(Base):
    """The real urllib transport against a loopback server that demands the key on every route."""

    def setUp(self):
        super().setUp()
        handler = type("H", (_Handler,), {"seen": []})
        self.handler = handler
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.custom = [{"name": "desktop", "base_url": self.base + "/v1", "key_env": ENV_NAME}]

    def test_authed_probe_is_self_hosted_and_unauthed_is_missing(self):
        self.env[ENV_NAME] = ENV_VALUE
        _cards, health, _ = self.deck(UrllibTransport())
        self.assertEqual((health[0]["status"], health[0]["confidence"], health[0]["metrics_available"]),
                         ("healthy", "self_hosted", True))
        self.assertIn(("/metrics", "Bearer " + ENV_VALUE), self.handler.seen)

        self.handler.seen.clear()
        self.custom = [{"name": "desktop", "base_url": self.base + "/v1"}]
        cards, health, _ = self.deck(UrllibTransport())
        self.assertEqual(cards[0]["auth_status"], "missing")
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("unknown", "credentials_missing"))
        self.assertTrue(all(auth is None for _path, auth in self.handler.seen))


if __name__ == "__main__":
    unittest.main()
