"""Self-hosted /metrics + health probes authenticate with the endpoint's resolved ``key_env`` secret.

Regression: a llama-swap box that demands a key on EVERY route (incl. /metrics) was reported
``degraded: metrics_unavailable`` because only /v1/models carried the resolved key_env secret.
All synthetic: no real HERMES_HOME, no real credentials; the socket test only binds 127.0.0.1.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

try:
    import hermes_cli.config  # noqa: F401
    import hermes_constants  # noqa: F401
    HAVE_HOST = True
except Exception:  # noqa: BLE001
    HAVE_HOST = False

from plugins.usage_deck.adapters.transport import TransportError, UrllibTransport
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine, host_inventory_source
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

    def __init__(self, good=ENV_VALUE, deny=401, open_models=False):
        self.good = good
        self.deny = deny  # status answered to a request without the exact Bearer
        self.open_models = open_models  # /v1/models answers anonymously; only /metrics is guarded
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        guarded = not (self.open_models and url == BOX + "/v1/models")
        if guarded and headers.get("Authorization") != "Bearer " + self.good:
            return json_response({"error": {"type": "authentication_error"}}, status=self.deny)
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
    def test_no_key_env_configured_is_degraded_credentials_missing_not_rejected(self):
        """Anonymous 401 AND 403: degraded + credentials_missing; never rejected, never an outage."""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        for deny in (401, 403):
            with self.subTest(status=deny):
                box = StrictBoxTransport(deny=deny)
                cards, health, envelopes = self.deck(box)
                self.assertTrue(box.requests)
                self.assertTrue(all(h.get("Authorization") is None for _u, h in box.requests))
                (card,), (entry,) = cards, health
                self.assertEqual(card["auth_status"], "missing")
                self.assertEqual((card["reason"], card["error"]), ("credentials_missing", None))
                self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"]),
                                 ("degraded", "opaque", False))
                self.assertEqual((entry["reason"], entry["error"]), ("credentials_missing", None))
                # no estimation from an unauthenticated probe, no fabricated numbers
                self.assertEqual((entry["cpu_percent"], entry["memory_bytes"], entry["latency_ms"]), (None, None, None))
                self.assertNotIn(ANON_TEXT, json.dumps(envelopes))

    def test_anonymous_metrics_only_denial_is_credentials_missing(self):
        """/v1/models is open but /metrics is guarded and no key_env exists: say so, send no credential."""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        for deny in (401, 403):
            with self.subTest(status=deny):
                box = StrictBoxTransport(deny=deny, open_models=True)
                _cards, health, _ = self.deck(box)
                self.assertEqual(box.auth_for("/metrics"), [None])
                self.assertEqual((health[0]["status"], health[0]["reason"], health[0]["metrics_available"]),
                                 ("degraded", "credentials_missing", False))
                self.assertIsNone(health[0]["cpu_percent"])

    def test_anonymous_404_stays_unknown_not_credentials_missing(self):
        """Only an access refusal means 'missing credential'; a 404 is not evidence about credentials."""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        box = StrictBoxTransport(deny=404)
        _cards, health, _ = self.deck(box)
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("unknown", "source_unavailable"))

    def test_rejected_key_with_403_is_not_reclassified_as_missing(self):
        self.env[ENV_NAME] = SENTINEL_TOKEN
        box = StrictBoxTransport(deny=403)
        cards, health, _ = self.deck(box)
        self.assertEqual(box.auth_for("/metrics"), ["Bearer " + SENTINEL_TOKEN])
        self.assertEqual(cards[0]["auth_status"], "available")  # presence only; a 403 is not a 401 rejection
        self.assertNotEqual(health[0]["reason"], "credentials_missing")

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

    def test_machine_only_member_scrapes_metrics_anonymously_only(self):
        """DESIGN.md §13 (user authority 2026-11-07) supersedes the old no-request rule for the MACHINE view:
        another profile's endpoint gets ONE read-only ``/metrics`` GET, and its inference credential (here an
        inline pool token) is never attached to it nor sent to any other route."""
        self.custom = []
        self.s.write_profile("other", {"credential_pool": {"custom:desktop": [
            {"id": "o1", "base_url": BOX + "/v1", "access_token": SENTINEL_TOKEN}]}})
        box = StrictBoxTransport(good=SENTINEL_TOKEN)
        _cards, _health, envelopes = self.deck(box, view="machine")
        self.assertEqual([u for u, _h in box.requests], [BOX + "/metrics"])
        self.assertEqual(box.auth_for("/metrics"), [None])
        self.assertEqual(scan_for(envelopes, [SENTINEL_TOKEN]), [])

    def test_machine_only_member_makes_no_request_in_the_profile_view(self):
        """The profile/worker view never touches another profile's endpoint or credential."""
        self.custom = []
        self.s.write_profile("other", {"credential_pool": {"custom:desktop": [
            {"id": "o1", "base_url": BOX + "/v1", "access_token": SENTINEL_TOKEN}]}})
        box = StrictBoxTransport(good=SENTINEL_TOKEN)
        self.deck(box, view="profile")
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
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("degraded", "credentials_missing"))
        self.assertFalse(health[0]["metrics_available"])
        self.assertTrue(self.handler.seen and all(auth is None for _path, auth in self.handler.seen))


OTHER_BOX = "http://other.lan:9090"
OTHER_VALUE = "sntl-other-home-5e19b0c7d3a84f26"
ROOT_VALUE = "sntl-root-env-31d8c6a9f0e24b57"


@unittest.skipUnless(HAVE_HOST, "needs the Hermes host interpreter (real credential helper)")
class HostCredentialHelperDiskOnly(unittest.TestCase):
    """key_env -> Bearer through the REAL host path (``host_inventory_source`` -> ``get_env_value``).

    The synthetic key lives ONLY in the scratch home's ``.env`` on disk (never in ``os.environ``,
    never in an injected dict); the plugin has no ``.env`` parser of its own.
    """

    def setUp(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        # Under /tmp, outside any real ~/.hermes tree, so the host cannot fall back to real credentials.
        self.s = ScratchHome(base="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self.s.cleanup)
        env = {k: v for k, v in os.environ.items() if k != ENV_NAME}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertNotIn(ENV_NAME, os.environ)
        (self.s.home / "config.yaml").write_text(
            "custom_providers:\n  - name: desktop\n    base_url: " + BOX + "/v1\n    key_env: " + ENV_NAME + "\n")
        (self.s.home / ".env").write_text(ENV_NAME + "=" + ENV_VALUE + "\n")
        # Egress-scope sentinels: another profile of the machine and the global root each hold a DIFFERENT
        # value for the same variable, and the other profile has its own authed box.
        (self.s.root / ".env").write_text(ENV_NAME + "=" + ROOT_VALUE + "\n")
        other = self.s.root / "profiles" / "other"
        other.mkdir(parents=True, exist_ok=True)
        (other / ".env").write_text(ENV_NAME + "=" + OTHER_VALUE + "\n")
        (other / "config.yaml").write_text(
            "custom_providers:\n  - name: elsewhere\n    base_url: " + OTHER_BOX + "/v1\n    key_env: " + ENV_NAME + "\n")
        self.s.write_profile("other", {"credential_pool": {"custom:elsewhere": [
            {"id": "o1", "base_url": OTHER_BOX + "/v1", "access_token": OTHER_VALUE}]}})
        token = set_hermes_home_override(str(self.s.home))
        self.addCleanup(reset_hermes_home_override, token)

    def deck(self, transport, view=None):
        svc = UsageDeckService(
            inventory_factory=host_inventory_source,  # the real host helpers: get_env_value, load_config
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )
        params = [("view", view)] if view else []
        _, subs = svc.handle("subscriptions", params)
        _, health = svc.handle("health", params)
        return subs["data"]["cards"], health["data"]["entries"], (subs, health)

    def assert_only_this_home(self, box):
        sent = {h.get("Authorization") for _u, h in box.requests}
        self.assertEqual(sent, {"Bearer " + ENV_VALUE})
        for foreign in (OTHER_VALUE, ROOT_VALUE):
            self.assertNotIn("Bearer " + foreign, sent)
        self.assertTrue(all(u.startswith(BOX + "/") for u, _h in box.requests))  # nothing to the other home's box

    def test_disk_only_env_key_is_sent_as_bearer_to_metrics_and_health(self):
        box = StrictBoxTransport()
        cards, health, envelopes = self.deck(box, view="profile")
        self.assertEqual(box.auth_for("/metrics"), ["Bearer " + ENV_VALUE])
        self.assertTrue(all(a == "Bearer " + ENV_VALUE for a in box.auth_for("/v1/models")))
        self.assert_only_this_home(box)
        (entry,) = health
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"]),
                         ("healthy", "self_hosted", True))
        self.assertEqual(cards[0]["auth_status"], "available")
        self.assertEqual(scan_for(envelopes, [ENV_VALUE, OTHER_VALUE, ROOT_VALUE]), [])

    def test_machine_view_never_borrows_another_homes_env_for_the_running_homes_box(self):
        """The running home's own box carries only its own key. (DESIGN.md §13: the OTHER home's box is
        also scraped now, but only on ``/metrics`` and anonymously here because its pool row's inline
        token outranks its ``key_env``; the other home's token never leaves its own origin.)"""
        box = StrictBoxTransport()
        _cards, health, envelopes = self.deck(box, view="machine")
        own = [(u, h) for u, h in box.requests if u.startswith(BOX + "/")]
        other = [(u, h) for u, h in box.requests if u.startswith(OTHER_BOX + "/")]
        self.assertEqual({h.get("Authorization") for _u, h in own}, {"Bearer " + ENV_VALUE})
        self.assertEqual([(u, h.get("Authorization")) for u, h in other], [(OTHER_BOX + "/metrics", None)])
        self.assertEqual(len(own) + len(other), len(box.requests))
        self.assertEqual([e["status"] for e in health if e["endpoint_id"] and "desktop" in e["provider_id"]],
                         ["healthy"])
        self.assertEqual(scan_for(envelopes, [ENV_VALUE, OTHER_VALUE, ROOT_VALUE]), [])

    def test_key_only_in_another_home_is_never_borrowed(self):
        (self.s.home / ".env").write_text("UNRELATED=1\n")  # this home no longer holds the key
        box = StrictBoxTransport(good=OTHER_VALUE)
        cards, health, _ = self.deck(box, view="profile")
        self.assertEqual(box.requests, [])  # unresolved key_env: nothing is sent, anywhere
        self.assertEqual(cards[0]["auth_status"], "missing")
        self.assertEqual((health[0]["status"], health[0]["reason"]), ("unknown", "credentials_missing"))
        self.assertEqual(health[0]["metrics_available"], False)


if __name__ == "__main__":
    unittest.main()
