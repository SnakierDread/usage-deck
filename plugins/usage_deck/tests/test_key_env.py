"""key_env resolution for custom_providers + honest missing-vs-rejected credential labels.

All synthetic: no network, no real HERMES_HOME, no real credentials.
"""

import json
import unittest

from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL
from plugins.usage_deck.adapters.transport import HttpResponse, TransportError
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine
from plugins.usage_deck.service import UsageDeckService

from .helpers import (
    NOW,
    SENTINEL_KEY,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
    FakeAnalytics,
    ScratchHome,
    json_response,
    raw_response,
    scan_for,
)

BOX = "http://desktop.lan:8000"
METRICS = "process_cpu_percent 12.5\nprocess_resident_memory_bytes 1000\n"
ENV_NAME = "HERMES_CUSTOM_DESKTOP_API_KEY"
ENV_VALUE = "sntl-env-4c1d9e07ab35f862"
REJECTED_TEXT = "The provider rejected the stored credential."


class AuthBoxTransport:
    """A box whose /v1/models needs ``Authorization: Bearer <good>`` (401 without / with a wrong one).

    ``metrics`` controls /metrics (anonymous 200 by default, or 404 for a models-only-auth box).
    """

    def __init__(self, good=ENV_VALUE, metrics=True):
        self.good = good
        self.metrics = metrics
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        if url == BOX + "/metrics":
            return raw_response(METRICS, latency=2.0) if self.metrics else raw_response("nope", status=404)
        if url == BOX + "/v1/models":
            if headers.get("Authorization") == "Bearer " + self.good:
                return json_response({"data": [{"id": "m"}]}, latency=6.0)
            return json_response({"error": "unauthorized"}, status=401)
        if url == BOX + "/api/tags":
            return raw_response("not found", status=404)
        raise TransportError(TransportError.CONNECTION)

    def auth_headers_for(self, path):
        return [h.get("Authorization") for u, h in self.requests if u == BOX + path]

    def urls(self):
        return [u for u, _ in self.requests]


class Base(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.env = {}
        self.custom = []

    def source(self):
        return FileInventorySource(
            self.s.home, self.s.root,
            config_loader=lambda: {"model": {}},
            custom_provider_loader=lambda c: self.custom,
            env_lookup=lambda name: self.env.get(name),
        )

    def service(self, transport):
        src = self.source()
        return UsageDeckService(
            inventory_factory=lambda: src,
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )

    def deck(self, transport):
        svc = self.service(transport)
        _, subs = svc.handle("subscriptions", [])
        _, health = svc.handle("health", [])
        return subs["data"]["cards"], health["data"]["entries"], (subs, health)


class DiscoveryKeyEnvTests(Base):
    def test_key_env_only_resolves_to_the_env_value(self):
        self.env[ENV_NAME] = ENV_VALUE
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertEqual(item.credential.reveal(), ENV_VALUE)
        self.assertEqual((item.auth_status, item.credential_expected), ("available", False))
        self.assertIn(ENV_VALUE, item.secrets)  # registered for redaction

    def test_api_key_alias_is_resolved_too(self):
        self.env[ENV_NAME] = ENV_VALUE
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertEqual(item.credential.reveal(), ENV_VALUE)

    def test_inline_api_key_beats_key_env(self):
        self.env[ENV_NAME] = ENV_VALUE
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key": SENTINEL_KEY, "key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertEqual(item.credential.reveal(), SENTINEL_KEY)
        self.assertNotIn(ENV_VALUE, item.secrets)

    def test_unset_env_var_gives_no_credential_and_is_flagged_expected(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertIsNone(item.credential)
        self.assertEqual((item.auth_status, item.credential_expected), ("missing", True))

    def test_empty_env_value_is_unset(self):
        self.env[ENV_NAME] = ""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertEqual((item.auth_status, item.credential_expected), ("missing", True))

    def test_no_key_env_is_not_expected(self):
        self.custom = [{"name": "ollama-box", "base_url": BOX + "/v1"}]
        (item,) = discover(self.source())
        self.assertEqual((item.auth_status, item.credential_expected), ("missing", False))

    def test_malformed_key_env_names_are_ignored_and_never_looked_up(self):
        looked_up = []
        src = FileInventorySource(
            self.s.home, self.s.root, config_loader=lambda: {"model": {}},
            custom_provider_loader=lambda c: [
                {"name": "a", "base_url": BOX + "/v1", "key_env": "bad name; rm"},
                {"name": "b", "base_url": "http://b.lan:1/v1", "key_env": 5},
            ],
            env_lookup=lambda n: looked_up.append(n),
        )
        items = discover(src)
        self.assertEqual(looked_up, [])
        self.assertEqual([(i.auth_status, i.credential_expected) for i in items], [("missing", False)] * 2)

    def test_key_env_applies_to_a_pool_row_without_a_credential(self):
        self.env[ENV_NAME] = ENV_VALUE
        self.s.write_auth({"credential_pool": {"custom:desktop": [{"id": "r1", "base_url": BOX + "/v1"}]}})
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]
        (item,) = discover(self.source())
        self.assertEqual(item.credential.reveal(), ENV_VALUE)

    def test_machine_view_resolves_key_env_and_flags_unset(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]
        (unset,) = discover_machine(self.source())
        self.assertIsNone(unset.credential)
        self.assertEqual((unset.auth_status, unset.credential_expected), ("missing", True))
        self.env[ENV_NAME] = ENV_VALUE
        (item,) = discover_machine(self.source())
        self.assertEqual(item.credential.reveal(), ENV_VALUE)
        self.assertEqual((item.auth_status, item.credential_expected), ("available", False))


class KeyEnvServiceTests(Base):
    def setUp(self):
        super().setUp()
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "key_env": ENV_NAME}]

    def test_key_env_only_entry_sends_bearer_and_is_healthy(self):
        self.env[ENV_NAME] = ENV_VALUE
        box = AuthBoxTransport()
        cards, health, envelopes = self.deck(box)
        self.assertTrue(box.auth_headers_for("/v1/models"))
        self.assertTrue(all(a == "Bearer " + ENV_VALUE for a in box.auth_headers_for("/v1/models")))
        self.assertEqual(box.auth_headers_for("/metrics"), [None] * len(box.auth_headers_for("/metrics")))
        (card,) = cards
        (entry,) = health
        self.assertEqual((entry["status"], entry["error"], entry["reason"]), ("healthy", None, None))
        self.assertEqual((card["auth_status"], card["kind"]), ("available", "self_hosted"))
        self.assertEqual(card["confidence"], "self_hosted")
        self.assertEqual(scan_for(envelopes, [ENV_VALUE]), [])

    def test_models_only_auth_box_degrades_but_is_reachable_with_bearer(self):
        """401 without header / 200 with header on /v1/models; /metrics not exposed."""
        self.env[ENV_NAME] = ENV_VALUE
        box = AuthBoxTransport(metrics=False)
        cards, health, _ = self.deck(box)
        self.assertTrue(all(a == "Bearer " + ENV_VALUE for a in box.auth_headers_for("/v1/models")))
        (entry,) = health
        self.assertEqual((entry["status"], entry["reason"], entry["error"]), ("degraded", "metrics_unavailable", None))
        self.assertEqual(cards[0]["auth_status"], "available")

    def test_models_only_auth_box_without_a_credential_never_claims_rejection(self):
        """No key_env at all: the 401 is anonymous, so the truth is 'no credential was sent'."""
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        box = AuthBoxTransport(metrics=False)
        cards, health, envelopes = self.deck(box)
        self.assertEqual(box.auth_headers_for("/v1/models"), [None] * len(box.auth_headers_for("/v1/models")))
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "missing")
        self.assertEqual((card["reason"], card["error"]), ("credentials_missing", None))
        self.assertEqual((entry["status"], entry["reason"], entry["error"]), ("unknown", "credentials_missing", None))
        self.assertNotIn(REJECTED_TEXT, json.dumps(envelopes))

    def test_anonymous_401_with_working_metrics_is_credentials_missing_not_rejected(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1"}]
        box = AuthBoxTransport(metrics=True)
        cards, health, envelopes = self.deck(box)
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "missing")
        self.assertEqual((entry["status"], entry["reason"], entry["error"]), ("degraded", "credentials_missing", None))
        self.assertNotIn(REJECTED_TEXT, json.dumps(envelopes))

    def test_unset_env_var_is_missing_and_sends_no_probe(self):
        box = AuthBoxTransport()
        cards, health, envelopes = self.deck(box)
        self.assertEqual(box.requests, [])  # nothing sent anonymously on behalf of an unset key_env
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "missing")
        self.assertEqual((card["state"], card["reason"], card["error"]), ("opaque", "credentials_missing", None))
        self.assertEqual((entry["status"], entry["reason"], entry["error"]), ("unknown", "credentials_missing", None))
        self.assertNotIn(REJECTED_TEXT, json.dumps(envelopes))

    def test_wrong_credential_is_rejected(self):
        self.env[ENV_NAME] = SENTINEL_TOKEN  # resolves, but the box wants ENV_VALUE
        box = AuthBoxTransport()
        cards, health, envelopes = self.deck(box)
        self.assertTrue(all(a == "Bearer " + SENTINEL_TOKEN for a in box.auth_headers_for("/v1/models")))
        (card,), (entry,) = cards, health
        self.assertEqual(card["auth_status"], "rejected")
        self.assertEqual(entry["status"], "degraded")  # /metrics is anonymous and fine; /v1/models refused us
        self.assertEqual(entry["error"]["code"], "auth_required")
        self.assertEqual(entry["error"]["message"], REJECTED_TEXT)
        self.assertEqual(scan_for(envelopes, [SENTINEL_TOKEN]), [])

    def test_wrong_credential_on_models_only_auth_box_is_unknown_and_rejected(self):
        self.env[ENV_NAME] = SENTINEL_TOKEN
        cards, health, _ = self.deck(AuthBoxTransport(metrics=False))
        self.assertEqual(cards[0]["auth_status"], "rejected")
        self.assertEqual((health[0]["status"], health[0]["error"]["code"]), ("unknown", "auth_required"))

    def test_wrong_inline_api_key_is_rejected_too(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key": SENTINEL_SECRET}]
        cards, health, _ = self.deck(AuthBoxTransport())
        self.assertEqual(cards[0]["auth_status"], "rejected")
        self.assertEqual(health[0]["error"]["code"], "auth_required")

    def test_key_env_fixing_the_variable_flips_missing_to_available(self):
        box = AuthBoxTransport()
        self.assertEqual(self.deck(box)[0][0]["auth_status"], "missing")
        self.env[ENV_NAME] = ENV_VALUE
        cards, health, _ = self.deck(AuthBoxTransport())
        self.assertEqual((cards[0]["auth_status"], health[0]["status"]), ("available", "healthy"))

    def test_credentialed_200_endpoint_stays_available(self):
        self.custom = [{"name": "desktop", "base_url": BOX + "/v1", "api_key": ENV_VALUE}]
        cards, health, _ = self.deck(AuthBoxTransport())
        self.assertEqual((cards[0]["auth_status"], health[0]["status"]), ("available", "healthy"))


class OpenRouterRejectedTests(Base):
    def test_openrouter_401_with_a_key_is_rejected(self):
        self.s.write_auth({"credential_pool": {"openrouter": [{"id": "or1", "access_token": SENTINEL_KEY}]}})

        class T:
            requests = []

            def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
                self.requests.append((url, headers))
                return HttpResponse(401, b"{}", 3.0, "application/json")

        cards, _, _ = self.deck(T())
        (card,) = cards
        self.assertEqual(card["auth_status"], "rejected")
        self.assertEqual(card["error"]["code"], "auth_required")

    def test_openrouter_without_a_key_makes_no_request_and_stays_missing(self):
        self.s.write_auth({"credential_pool": {"openrouter": [{"id": "or1"}]}})
        box = AuthBoxTransport()
        cards, _, _ = self.deck(box)
        self.assertEqual(box.requests, [])
        self.assertEqual((cards[0]["auth_status"], cards[0]["reason"], cards[0]["error"]), ("missing", "credentials_missing", None))


if __name__ == "__main__":
    unittest.main()
