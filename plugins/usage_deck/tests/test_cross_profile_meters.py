"""Machine view reads usage meters with ANY local profile's credential (DESIGN.md §13).

User-ratified 2026-11-07. Two fake homes: ``work`` (the viewed profile, no Codex row) and ``verifier`` (holds the
only Codex credential, the "GO" account). All tokens are synthetic; the transport is injected, nothing leaves the box.
"""

import logging
import os
import unittest
import urllib.request
from unittest import mock

from plugins.usage_deck.adapters.cloud import CODEX_USAGE_URL
from plugins.usage_deck.adapters.transport import (
    CROSS_PROFILE_PROBES,
    CrossProfileProbeTransport,
    TransportError,
    UrllibTransport,
    probe_allowed,
)
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine
from plugins.usage_deck.service import UsageDeckService

from .helpers import NOW, FakeAnalytics, FakeTransport, json_response, raw_response, scan_for
from .test_codex_wham import codex_token, limit_body
from .test_schema_preservation import SchemaAsserts
from .test_service import GPU, METRICS, ServiceBase

GO_TOKEN = codex_token(account="udsyn-acct-GO", user="udsyn-user-GO", plan="go", residency="udsyn-region-GO", jti="go")
WORK_TOKEN = codex_token(account="udsyn-acct-WK", user="udsyn-user-WK", plan="plus", residency="udsyn-region-WK", jti="wk")


def codex_pool(*rows):
    return {"credential_pool": {"openai-codex": [{"id": rid, "access_token": tok} for rid, tok in rows]}}


def tree(root):
    """Every file under ``root`` as {relative path: bytes}."""
    found = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                found[os.path.relpath(path, root)] = fh.read()
    return found


class LogCapture:
    def __init__(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append

    def __enter__(self):
        root = logging.getLogger()
        self._level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self._level)

    def text(self):
        return [r.getMessage() + repr(r.args) for r in self.records]


class MachineViewReadsAnyProfilesMeter(ServiceBase, SchemaAsserts):
    def setUp(self):
        super().setUp()
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        self.s.write_auth({"credential_pool": {}})                       # 'work' and the root hold no Codex row
        self.s.write_profile("verifier", codex_pool(("go1", GO_TOKEN)))  # the GO credential lives only here

    def subscriptions(self, view, **kw):
        status, body = self.make().handle("subscriptions", [("view", view)], **kw)
        self.assert_exact_envelope("subscriptions", status, body)
        return body

    def codex_cards(self, body):
        return [c for c in body["data"]["cards"] if c["provider_id"] == "openai-codex"]

    # ------------------------------------------------------------------ (1) + (4)

    def test_verifier_held_go_credential_yields_the_exact_meter_in_the_machine_view(self):
        (card,) = self.codex_cards(self.subscriptions("machine"))
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"]), ("ready", "exact", None, None))
        (meter,) = card["meters"]
        self.assertEqual((meter["id"], meter["unit"], meter["source"], meter["value"], meter["limit"], meter["remaining"]),
                         ("workspace_spend", "credit", "chatgpt_wham_usage", 864.5, 1500.0, 635.5))
        self.assertEqual(card["debug"]["source"], "chatgpt_wham_usage")   # no longer "local_inventory"
        # attribution: the card keeps its holder list, the meter shows regardless of holder
        self.assertEqual([(h["profile"], h["is_global"]) for h in card["holders"]], [("verifier", False)])
        self.assertEqual(card["auth_status"], "available")
        # exactly one GET, to the one ratified route, with the verifier's own token and routing claims
        self.assertEqual(self.transport.urls(), [CODEX_USAGE_URL])
        headers = self.transport.requests[0][1]
        self.assertEqual((headers["Authorization"], headers["ChatGPT-Account-ID"],
                          headers["x-openai-internal-codex-residency"]),
                         ("Bearer " + GO_TOKEN, "udsyn-acct-GO", "udsyn-region-GO"))

    def test_a_selected_profiles_own_credential_is_still_preferred_over_the_verifiers(self):
        self.s.write_auth(codex_pool(("w1", WORK_TOKEN)))
        self.s.write_profile("verifier", codex_pool(("go1", GO_TOKEN)))
        cards = self.codex_cards(self.subscriptions("machine"))
        self.assertEqual([c["state"] for c in cards], ["ready", "ready"])   # two different seats
        self.assertEqual(sorted(r[1]["Authorization"] for r in self.transport.requests),
                         sorted(["Bearer " + WORK_TOKEN, "Bearer " + GO_TOKEN]))

    def test_upstream_failure_with_the_cross_profile_credential_is_an_honest_error_not_inventory(self):
        self.transport.routes[CODEX_USAGE_URL] = json_response({}, status=401)
        (card,) = self.codex_cards(self.subscriptions("machine"))
        self.assertEqual(card["state"], "error")
        self.assertEqual(card["error"]["code"], "auth_required")
        self.assertEqual(card["auth_status"], "available")                  # presence-only enum, no refresh, no retry
        self.assertEqual(len(self.transport.requests), 1)

    # ------------------------------------------------------------------ (2) worker / profile view unchanged

    def test_profile_view_of_the_viewed_profile_never_touches_the_verifiers_credential(self):
        body = self.subscriptions("profile")
        self.assertEqual(body["data"]["cards"], [])
        self.assertEqual(self.transport.requests, [])

    def test_profile_view_of_the_verifier_uses_only_the_verifiers_own_pool(self):
        # a profile process (or ?profile=verifier) resolves ITS OWN effective credentials; nobody else's
        svc = UsageDeckService(
            inventory_factory=lambda: FileInventorySource(
                self.s.root / "profiles" / "verifier", self.s.root, config_loader=lambda: {"model": {}},
                custom_provider_loader=lambda c: [], env_lookup=lambda n: None),
            analytics_factory=lambda p: self.analytics, transport=self.transport, clock=lambda: NOW)
        self.s.write_auth(codex_pool(("w1", WORK_TOKEN)))                   # work holds a seat too
        status, body = svc.handle("subscriptions", [("view", "profile")])
        self.assertEqual(status, 200)
        (card,) = self.codex_cards(body)
        self.assertEqual(card["state"], "ready")
        self.assertEqual([r[1]["Authorization"] for r in self.transport.requests], ["Bearer " + GO_TOKEN])

    def test_restricted_principal_is_clamped_to_profile_view_and_makes_no_cross_profile_request(self):
        for query in ([], [("view", "machine")]):
            status, body = self.make().handle("subscriptions", query, profile_only=True)
            self.assertEqual((status, body["data"]["view"], body["data"]["cards"]), (200, "profile", []))
        self.assertEqual(self.transport.requests, [])

    def test_profile_view_discovery_never_carries_a_cross_profile_item(self):
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        self.assertEqual(discover(src), [])
        (item,) = [i for i in discover_machine(src) if i.provider_id == "openai-codex"]
        self.assertTrue(item.cross_profile and item.live_source)

    def test_cache_never_serves_a_machine_meter_to_a_profile_view_request(self):
        svc = self.make()
        self.assertEqual(len(self.codex_cards(svc.handle("subscriptions", [("view", "machine")])[1])), 1)
        self.assertEqual(svc.handle("subscriptions", [("view", "profile")])[1]["data"]["cards"], [])

    # ------------------------------------------------------------------ read-only allowlist, in code

    def test_the_allowlist_is_exactly_the_three_documented_read_only_probes(self):
        self.assertEqual(
            [(p.method, p.scheme, p.host, p.port, p.path) for p in CROSS_PROFILE_PROBES],
            [("GET", "https", "chatgpt.com", 443, "/backend-api/wham/usage"),
             ("GET", "https", "api.anthropic.com", 443, "/api/oauth/usage"),
             ("GET", None, None, None, "/metrics")])

    def test_method_origin_and_path_are_all_enforced(self):
        good = [("GET", "https://chatgpt.com/backend-api/wham/usage"),
                ("GET", "https://chatgpt.com:443/backend-api/wham/usage"),
                ("GET", "https://api.anthropic.com/api/oauth/usage"),
                ("GET", "http://gpu.lan:8000/metrics")]
        bad = [("POST", "https://chatgpt.com/backend-api/wham/usage"),
               ("PUT", "https://api.anthropic.com/api/oauth/usage"),
               ("DELETE", "http://gpu.lan:8000/metrics"),
               ("HEAD", "https://chatgpt.com/backend-api/wham/usage"),
               ("GET", "https://chatgpt.com/backend-api/usage"),
               ("GET", "https://chatgpt.com/backend-api/wham/usage/"),
               ("GET", "https://chatgpt.com/backend-api/wham/usage?x=1"),
               ("GET", "https://chatgpt.com/backend-api/wham/usage#f"),
               ("GET", "http://chatgpt.com/backend-api/wham/usage"),
               ("GET", "https://chatgpt.com.evil.example/backend-api/wham/usage"),
               ("GET", "https://evil@chatgpt.com/backend-api/wham/usage"),
               ("GET", "https://chatgpt.com:8443/backend-api/wham/usage"),
               ("GET", "https://api.openai.com/v1/usage"),
               ("GET", "https://api.anthropic.com/v1/messages"),
               ("GET", "https://openrouter.ai/api/v1/key"),
               ("GET", "http://gpu.lan:8000/v1/models"),
               ("GET", "http://gpu.lan:8000/metrics?name=x"),
               ("GET", "ftp://gpu.lan/metrics")]
        for method, url in good:
            self.assertTrue(probe_allowed(method, url), (method, url))
        for method, url in bad:
            self.assertFalse(probe_allowed(method, url), (method, url))

    def test_write_verbs_are_refused_before_anything_is_sent(self):
        inner = FakeTransport({CODEX_USAGE_URL: json_response(limit_body())})
        guard = CrossProfileProbeTransport(inner)
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            with self.assertRaises(TransportError):
                guard.request(method, CODEX_USAGE_URL, {})
        self.assertEqual(inner.requests, [])
        guard.request("GET", CODEX_USAGE_URL, {})
        self.assertEqual(len(inner.requests), 1)

    def test_the_real_transport_only_ever_issues_get_for_a_cross_profile_meter(self):
        seen = []

        def record(_opener, request, *args, **kwargs):
            seen.append((request.get_method(), request.full_url))
            raise AssertionError("no network in tests")

        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        svc = UsageDeckService(inventory_factory=lambda: src, analytics_factory=lambda p: FakeAnalytics(),
                               transport=UrllibTransport(), clock=lambda: NOW)
        with mock.patch.object(urllib.request.OpenerDirector, "open", record):
            svc.handle("subscriptions", [("view", "machine")])
        self.assertEqual(seen, [("GET", CODEX_USAGE_URL)])

    def test_providers_without_a_ratified_probe_stay_inventory_only(self):
        self.s.write_profile("verifier", {"credential_pool": {
            "openrouter": [{"id": "or-v", "access_token": "or-key-verifier-0001"}],
            "nous": [{"id": "n1", "access_token": "nous-verifier-0002"}],
            "anthropic": [{"id": "a1", "access_token": "sk-ant-verifier-0003"}],
        }})
        cards = self.subscriptions("machine")["data"]["cards"]
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(c["state"] == "opaque" and c["reason"] in ("source_unavailable", "unsupported") for c in cards))
        self.assertEqual(self.transport.requests, [])

    def test_self_hosted_cross_profile_endpoint_gets_one_anonymous_metrics_get_only(self):
        self.s.write_profile("verifier", {"credential_pool": {"custom:gpu": [
            {"id": "g1", "base_url": GPU + "/v1", "access_token": "sk-gpu-verifier-0004"}]}})
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS),
                                      GPU + "/v1/models": json_response({"data": []})})
        status, body = self.make().handle("health", [("view", "machine")])
        self.assertEqual(status, 200)
        self.assertEqual(self.transport.urls(), [GPU + "/metrics"])
        self.assertNotIn("Authorization", self.transport.requests[0][1])
        self.assertEqual([e["status"] for e in body["data"]["entries"]], ["healthy"])

    # ------------------------------------------------------------------ (3) ephemeral credentials

    def test_credentials_are_never_written_logged_returned_or_cached(self):
        before = tree(self.s.root)
        needles = [GO_TOKEN, "udsyn-acct-GO", "udsyn-user-GO", "udsyn-region-GO",
                   *[part for part in GO_TOKEN.split(".") if len(part) > 8]]
        svc = self.make()
        with LogCapture() as logs:
            _s, subs = svc.handle("subscriptions", [("view", "machine")])
            _h, health = svc.handle("health", [("view", "machine")])
            _u, usage = svc.handle("usage", [("view", "machine"), ("provider", "openai-codex")])
            _e, errors = self.make().handle("subscriptions", [("view", "everything")])
        self.assertEqual(self.codex_cards(subs)[0]["state"], "ready")
        # nothing leaks into any response, log record or the service's own cache
        for name, obj in (("subscriptions", subs), ("health", health), ("usage", usage), ("errors", errors),
                          ("cache", repr(svc._cache)), ("logs", logs.text())):
            self.assertEqual(scan_for(obj, needles), [], name)
        # no sink file was created or changed under the Hermes root (captures, caches, debug dumps, ...)
        after = tree(self.s.root)
        self.assertEqual(sorted(after), sorted(before))
        self.assertEqual(after, before)

    def test_a_failing_cross_profile_request_does_not_leak_the_credential_either(self):
        self.transport.routes[CODEX_USAGE_URL] = TransportError(TransportError.CONNECTION)
        needles = [GO_TOKEN, "udsyn-acct-GO", "udsyn-region-GO"]
        svc = self.make()
        with LogCapture() as logs:
            _s, subs = svc.handle("subscriptions", [("view", "machine")])
        (card,) = self.codex_cards(subs)
        self.assertEqual(card["state"], "error")
        for name, obj in (("subscriptions", subs), ("cache", repr(svc._cache)), ("logs", logs.text())):
            self.assertEqual(scan_for(obj, needles), [], name)

    def test_the_credential_object_never_reveals_itself_in_repr(self):
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        (item,) = [i for i in discover_machine(src) if i.provider_id == "openai-codex"]
        self.assertNotIn(GO_TOKEN, repr(item) + repr(item.credential) + str(item.credential))


if __name__ == "__main__":
    unittest.main()
