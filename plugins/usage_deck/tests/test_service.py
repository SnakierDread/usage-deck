import json
import threading
import time
import unittest

from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL
from plugins.usage_deck.adapters.transport import TransportError
from plugins.usage_deck.discovery import FileInventorySource
from plugins.usage_deck.service import UsageDeckService, parse_query

from .helpers import (
    ALL_SENTINELS,
    NOW,
    SENTINEL_KEY,
    SENTINEL_REFRESH,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
    FakeAnalytics,
    FakeTransport,
    ScratchHome,
    day_row,
    json_response,
    make_jwt,
    raw_response,
    scan_for,
)

GPU = "http://gpu.lan:8000"
METRICS = "process_cpu_percent 12.5\nprocess_resident_memory_bytes 1000\n"


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.analytics = FakeAnalytics([day_row("2026-11-05"), day_row("2026-11-06", 0, 0, 0.0, 1, 0)])
        self.transport = FakeTransport()
        self.config = {"model": {}}
        self.custom = []
        self.clock_calls = 0

    def make(self, **kw):
        src = FileInventorySource(
            self.s.home, self.s.root,
            config_loader=lambda: self.config,
            custom_provider_loader=lambda c: self.custom,
            env_lookup=lambda n: None,
        )
        return UsageDeckService(
            inventory_factory=lambda: src,
            analytics_factory=lambda profile: self.analytics,
            transport=self.transport,
            clock=lambda: NOW,
            **kw,
        )

    def call(self, svc, endpoint, params=()):
        return svc.handle(endpoint, list(params))

    def seed_fleet(self):
        """3 cloud accounts (+ opaque + unknown) and 1 self-hosted endpoint, with sentinel secrets everywhere."""
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or1", "access_token": SENTINEL_KEY, "refresh_token": SENTINEL_REFRESH,
                            "label": "mine"}],
            "openai-codex": [{"id": "c1", "access_token": make_jwt({"plan": "business"}), "refresh_token": SENTINEL_REFRESH},
                             {"id": "c2", "access_token": make_jwt({"plan": "go"})}],
            "nous": [{"id": "n1", "access_token": SENTINEL_TOKEN, "agent_key": SENTINEL_SECRET}],
            "mystery-cloud": [{"id": "m1", "access_token": SENTINEL_TOKEN}],
        }})
        self.custom = [{"name": "gpu", "base_url": GPU + "/v1", "api_key": SENTINEL_SECRET}]
        self.transport.routes.update({
            OPENROUTER_KEY_URL: json_response({"data": {"usage": 1.5, "limit": 5, "limit_remaining": 3.5,
                                                         "label": SENTINEL_KEY, "hash": SENTINEL_TOKEN}}),
            GPU + "/metrics": raw_response(METRICS, latency=2.0),
            GPU + "/v1/models": json_response({"data": []}, latency=6.0),
        })


class SubscriptionsTests(ServiceBase):
    def test_empty_inventory_is_200_empty(self):
        status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], {"cards": [], "inventory_state": "empty", "view": "machine"})
        self.assertEqual(body["meta"]["partial"], False)
        self.assertEqual(body["meta"]["schema_version"], 1)
        self.assertTrue(body["ok"] and body["error"] is None)

    def test_fleet_cards_confidence_and_state(self):
        self.seed_fleet()
        status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(status, 200)
        cards = body["data"]["cards"]
        self.assertEqual(body["data"]["inventory_state"], "detected")
        self.assertEqual(len(cards), 6)  # or1, c1, c2, n1, m1, custom:gpu
        self.assertEqual([(c["provider_id"], c["account_id"]) for c in cards],
                         sorted((c["provider_id"], c["account_id"]) for c in cards))
        by = {}
        for c in cards:
            by.setdefault(c["provider_id"], []).append(c)
        orc = by["openrouter"][0]
        self.assertEqual((orc["state"], orc["confidence"], orc["reason"]), ("ready", "exact", None))
        self.assertEqual(orc["meters"][0]["value"], 1.5)
        self.assertEqual(orc["id"], orc["account_id"])
        codex = sorted(by["openai-codex"], key=lambda c: c["account_label"])
        self.assertEqual([c["account_label"] for c in codex], ["Account 1", "Account 2"])
        # These test tokens carry a plan claim but no account claim: no usable routing, so no request (§9.2).
        self.assertEqual([(c["state"], c["confidence"], c["reason"], c["meters"]) for c in codex],
                         [("opaque", "opaque", "source_unavailable", [])] * 2)
        self.assertEqual(sorted(c["plan_name"] for c in codex), ["business", "go"])
        self.assertEqual(by["mystery-cloud"][0]["state"], "opaque")
        self.assertEqual(by["mystery-cloud"][0]["debug"]["adapter"], "SessionAnalyticsAdapter")
        gpu = by["custom:gpu"][0]
        self.assertEqual((gpu["kind"], gpu["state"], gpu["confidence"], gpu["reason"]),
                         ("self_hosted", "opaque", "self_hosted", "unsupported"))
        self.assertEqual(gpu["meters"], [])
        # every card has every field (null != omitted)
        for c in cards:
            self.assertEqual(set(c), {"id", "provider_id", "account_id", "provider_label", "account_label", "kind",
                                      "plan_name", "plan_source", "auth_status", "endpoint_id", "state", "confidence",
                                      "reason", "meters", "checked_at", "stale", "error", "debug", "holders"})
        self.assertFalse(body["meta"]["partial"])

    def test_no_secret_substrings_anywhere(self):
        self.seed_fleet()
        svc = self.make()
        for endpoint in ("subscriptions", "health"):
            status, body = self.call(svc, endpoint)
            self.assertEqual(status, 200)
            self.assertEqual(scan_for(body, ALL_SENTINELS), [], endpoint)
            self.assertNotIn(self.s.root.as_posix(), json.dumps(body))
            self.assertNotIn("eyJ", json.dumps(body))
        status, body = self.call(svc, "usage")
        self.assertEqual(scan_for(body, ALL_SENTINELS), [])

    def test_short_secrets_never_reach_responses_and_counts_survive(self):
        short = "zq"  # synthetic 2-character private value; DESIGN.md §2.1 covers every nonempty secret
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or1", "access_token": short}],
            "nous": [{"id": short, "access_token": short}],
            "custom:" + short: [{"id": "gp", "access_token": short}],
        }})
        self.custom = [{"name": "gpu", "base_url": GPU + "/v1", "api_key": short}]
        self.transport.routes.update({
            OPENROUTER_KEY_URL: json_response({"data": {"usage": 1.5, "limit": 5, "limit_remaining": 3.5,
                                                         "label": short}}),
            GPU + "/metrics": raw_response(METRICS, latency=2.0),
            GPU + "/v1/models": json_response({"data": []}, latency=6.0),
        })
        svc = self.make()
        for endpoint in ("subscriptions", "health", "usage"):
            status, body = self.call(svc, endpoint)
            self.assertEqual(status, 200, endpoint)
            self.assertEqual(scan_for(body, (short,)), [], endpoint)
        _, subs = self.call(svc, "subscriptions")
        cards = subs["data"]["cards"]
        self.assertEqual(len(cards), 4)
        self.assertTrue(all(c["provider_label"] != short and c["account_label"].startswith("Account") for c in cards))
        # Numeric values are not strings: safe counts survive scrubbing.
        _, usage = self.call(svc, "usage")
        counts = [p for p in usage["data"]["series"][0]["points"] if p["input_count"] is not None]
        self.assertEqual([p["input_count"] for p in counts], [100, 0])
        self.assertEqual([p["output_count"] for p in counts], [50, 0])

    def test_failure_isolation_and_partial_meta(self):
        self.seed_fleet()
        self.transport.routes[OPENROUTER_KEY_URL] = json_response({"error": SENTINEL_KEY}, status=401)
        status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(status, 200)  # per-provider failure never fails the grid
        cards = {c["provider_id"]: c for c in body["data"]["cards"]}
        self.assertEqual(cards["openrouter"]["state"], "error")
        self.assertEqual(cards["openrouter"]["error"]["code"], "auth_required")
        self.assertEqual(cards["openrouter"]["error"]["message"], "The provider rejected the stored credential.")
        self.assertEqual(cards["nous"]["state"], "opaque")  # sibling unaffected
        self.assertTrue(body["meta"]["partial"])
        self.assertEqual(body["meta"]["errors"], [cards["openrouter"]["error"]])
        self.assertEqual(scan_for(body, ALL_SENTINELS), [])

    def test_unsupported_alone_does_not_make_partial(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "a", "access_token": "tok-aaaaaa"}]}})
        _, body = self.call(self.make(), "subscriptions")
        self.assertFalse(body["meta"]["partial"])
        self.assertEqual(body["meta"]["errors"], [])

    def test_analytics_down_does_not_break_cards(self):
        self.seed_fleet()
        self.analytics.fail = True
        status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(status, 200)
        mystery = [c for c in body["data"]["cards"] if c["provider_id"] == "mystery-cloud"][0]
        self.assertEqual((mystery["state"], mystery["error"]), ("opaque", None))

    def test_unknown_provider_never_crashes(self):
        self.s.write_auth({"credential_pool": {"Brand New/Provider": [{"id": "z", "access_token": "tok-zzzzzz"}]}})
        status, body = self.call(self.make(), "subscriptions")
        card = body["data"]["cards"][0]
        self.assertEqual((status, card["provider_id"], card["state"], card["confidence"]),
                         (200, "Brand New/Provider", "opaque", "opaque"))

    def test_internal_adapter_error_is_sanitized_card_error(self):
        self.seed_fleet()

        class Exploding(FakeTransport):
            def get(self, url, *a, **k):
                raise RuntimeError("secret " + SENTINEL_KEY)

        self.transport = Exploding()
        with self.assertLogs("plugins.usage_deck.service", level="ERROR") as logs:
            status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(scan_for(logs.output, ALL_SENTINELS), [])  # logs carry the exception type only
        self.assertEqual(status, 200)
        cards = {c["provider_id"]: c for c in body["data"]["cards"]}
        self.assertEqual(cards["openrouter"]["error"]["code"], "internal_error")
        self.assertEqual(cards["custom:gpu"]["error"]["code"], "internal_error")
        self.assertEqual(scan_for(body, ALL_SENTINELS), [])
        self.assertTrue(body["meta"]["partial"])
        h = self.call(self.make(), "health")[1]
        self.assertEqual(h["data"]["entries"][0]["status"], "unknown")

    def test_malformed_inventory_is_503_without_leaks(self):
        self.s.write_auth("{not json " + SENTINEL_KEY)
        status, body = self.call(self.make(), "subscriptions")
        self.assertEqual((status, body["ok"], body["data"], body["error"]["code"]), (503, False, None, "inventory_unavailable"))
        self.assertEqual(scan_for(body, ALL_SENTINELS), [])
        self.assertNotIn(str(self.s.root), json.dumps(body))
        for endpoint in ("health",):
            self.assertEqual(self.call(self.make(), endpoint)[0], 503)

    def test_deadline_turns_pending_into_timeout_and_late_workers_cannot_overwrite(self):
        self.seed_fleet()
        release = threading.Event()
        calls = []

        class Slow(FakeTransport):
            def get(inner, url, *a, **k):
                if "gpu.lan" in url:
                    calls.append(url)
                    release.wait(5)
                return FakeTransport.get(inner, url, *a, **k)

        self.transport = Slow(self.transport.routes)
        svc = self.make(deadline=0.3)
        started = time.monotonic()
        status, body = self.call(svc, "subscriptions")
        self.assertLess(time.monotonic() - started, 3)
        gpu = [c for c in body["data"]["cards"] if c["provider_id"] == "custom:gpu"][0]
        self.assertEqual((gpu["state"], gpu["error"]["code"], gpu["error"]["retryable"]), ("error", "timeout", True))
        orc = [c for c in body["data"]["cards"] if c["provider_id"] == "openrouter"][0]
        self.assertEqual(orc["state"], "ready")  # finished siblings are kept
        _, hbody = self.call(svc, "health")  # cached snapshot, still the timeout one
        self.assertEqual(hbody["data"]["entries"][0]["status"], "unknown")
        self.assertEqual(hbody["data"]["entries"][0]["error"]["code"], "timeout")
        release.set()
        time.sleep(0.3)
        _, again = self.call(svc, "subscriptions")
        self.assertEqual([c for c in again["data"]["cards"] if c["provider_id"] == "custom:gpu"][0]["state"], "error")

    def test_snapshot_cache_30s_and_expiry(self):
        self.seed_fleet()
        t = [1000.0]
        svc = self.make(monotonic=lambda: t[0])
        self.call(svc, "subscriptions")
        n = len(self.transport.requests)
        self.assertGreater(n, 0)
        self.call(svc, "subscriptions")
        self.call(svc, "health")  # health shares the same deck snapshot
        self.assertEqual(len(self.transport.requests), n)
        t[0] += 31
        self.call(svc, "subscriptions")
        self.assertEqual(len(self.transport.requests), 2 * n)

    def test_cache_is_not_shared_across_homes(self):
        self.seed_fleet()
        svc = self.make()
        self.call(svc, "subscriptions")
        other = ScratchHome()
        self.addCleanup(other.cleanup)
        src2 = FileInventorySource(other.home, None)
        svc._inventory_factory = lambda: src2
        status, body = self.call(svc, "subscriptions")
        self.assertEqual(body["data"]["cards"], [])


class HealthTests(ServiceBase):
    def test_health_rows_one_per_endpoint(self):
        self.seed_fleet()
        self.s.write_auth({"credential_pool": {"custom:gpu": [
            {"id": "g1", "access_token": SENTINEL_KEY, "base_url": GPU + "/v1"},
            {"id": "g2", "access_token": SENTINEL_TOKEN, "base_url": GPU + "/v1"}]}})
        self.transport.routes.update({GPU + "/metrics": raw_response(METRICS), GPU + "/v1/models": json_response({"data": []})})
        self.custom = []
        svc = self.make()
        _, subs = self.call(svc, "subscriptions")
        self.assertEqual(len([c for c in subs["data"]["cards"] if c["provider_id"] == "custom:gpu"]), 2)
        status, body = self.call(svc, "health")
        entries = body["data"]["entries"]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual((e["status"], e["confidence"], e["metrics_available"], e["cpu_percent"], e["memory_bytes"]),
                         ("healthy", "self_hosted", True, 12.5, 1000))
        self.assertEqual(self.transport.urls().count(GPU + "/metrics"), 1)

    def test_offline_endpoint_is_partial_with_matching_errors(self):
        self.s.write_auth({"credential_pool": {}})
        self.custom = [{"name": "dead", "base_url": "http://dead.lan:1/v1"}]
        status, body = self.call(self.make(), "health")
        e = body["data"]["entries"][0]
        self.assertEqual((status, e["status"], e["latency_ms"], e["error"]["code"]), (200, "offline", None, "upstream_unavailable"))
        self.assertTrue(body["meta"]["partial"])
        self.assertEqual(body["meta"]["errors"], [e["error"]])

    def test_no_self_hosted_means_empty_entries(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n", "access_token": "t-123456"}]}})
        _, body = self.call(self.make(), "health")
        self.assertEqual(body["data"], {"entries": []})


class UsageTests(ServiceBase):
    def test_unfiltered_profile_series_once(self):
        self.seed_fleet()
        status, body = self.call(self.make(), "usage", [("window", "7")])
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual((data["window_days"], data["provider_id"], data["reason"]), (7, None, None))
        self.assertEqual(len(data["series"]), 1)  # never cloned per provider/account
        s = data["series"][0]
        self.assertEqual((s["scope"], s["confidence"], s["provider_id"], s["account_id"], s["timezone"]),
                         ("profile", "estimated", None, None, "server_local"))
        self.assertEqual([p["day"] for p in s["points"]], ["2026-11-05", "2026-11-06"])
        self.assertEqual(s["points"][1]["input_count"], 0)
        self.assertEqual(set(s["points"][0]), {"day", "input_count", "output_count", "estimated_usd", "sessions",
                                               "api_calls", "confidence"})
        self.assertEqual(self.analytics.calls, [7])

    def test_default_window_is_30(self):
        _, body = self.call(self.make(), "usage")
        self.assertEqual(body["data"]["window_days"], 30)

    def test_filtered_by_known_provider_is_attribution_unavailable(self):
        self.seed_fleet()
        for provider in ("openrouter", "mystery-cloud", "custom:gpu"):
            status, body = self.call(self.make(), "usage", [("provider", provider)])
            self.assertEqual(status, 200)
            self.assertEqual((body["data"]["series"], body["data"]["reason"], body["data"]["provider_id"]),
                             ([], "attribution_unavailable", provider))
        self.assertEqual(self.analytics.calls, [])  # no fake provider totals, no pointless read

    def test_unknown_provider_filter_is_404(self):
        self.seed_fleet()
        status, body = self.call(self.make(), "usage", [("provider", "nope")])
        self.assertEqual((status, body["ok"], body["error"]["code"]), (404, False, "provider_not_found"))

    def test_empty_analytics_is_no_observations(self):
        self.analytics.rows = ()
        _, body = self.call(self.make(), "usage")
        self.assertEqual((body["data"]["series"][0]["points"], body["data"]["reason"]), ([], "no_observations"))

    def test_analytics_unavailable_is_503_only_for_usage(self):
        self.seed_fleet()
        self.analytics.fail = True
        svc = self.make()
        status, body = self.call(svc, "usage")
        self.assertEqual((status, body["error"]["code"]), (503, "analytics_unavailable"))
        self.assertEqual(self.call(svc, "subscriptions")[0], 200)
        self.assertEqual(self.call(svc, "health")[0], 200)


class QueryValidationTests(ServiceBase):
    def test_invalid_queries_are_400(self):
        svc = self.make()
        bad = [
            ("usage", [("window", "14")]), ("usage", [("window", "07")]), ("usage", [("window", "30.0")]),
            ("usage", [("window", "")]), ("usage", [("window", "7"), ("window", "30")]),
            ("usage", [("provider", "")]), ("usage", [("provider", "x" * 257)]), ("usage", [("provider", "a\x00b")]),
            ("usage", [("provider", "a"), ("provider", "b")]), ("usage", [("nonsense", "1")]),
            ("subscriptions", [("window", "7")]), ("subscriptions", [("provider", "x")]),
            ("health", [("foo", "bar")]), ("health", [("profile", "../x")]), ("health", [("profile", "a/b")]),
            ("subscriptions", [("profile", "a"), ("profile", "b")]), ("subscriptions", [("profile", "")]),
        ]
        for endpoint, params in bad:
            status, body = self.call(svc, endpoint, params)
            self.assertEqual((status, body["ok"], body["error"]["code"]), (400, False, "invalid_query"), (endpoint, params))
            self.assertIsNone(body["data"])

    def test_valid_queries(self):
        for window in ("7", "30", "90"):
            self.assertEqual(parse_query("usage", [("window", window)]).window, int(window))
        q = parse_query("usage", [("provider", "custom:a b/c"), ("profile", "work"), ("window", "90")])
        self.assertEqual((q.provider, q.profile, q.window), ("custom:a b/c", "work", 90))
        self.assertEqual(parse_query("health", [("profile", "current")]).profile, "current")

    def test_envelope_shape(self):
        status, body = self.call(self.make(), "health")
        self.assertEqual(list(body), ["ok", "data", "error", "meta"])
        self.assertEqual(list(body["meta"]), ["schema_version", "request_id", "generated_at", "partial", "errors"])
        self.assertEqual(body["meta"]["generated_at"], "2026-11-06T12:00:00Z")
        self.assertRegex(body["meta"]["request_id"], r"^[0-9a-f-]{36}$")
        json.dumps(body, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
