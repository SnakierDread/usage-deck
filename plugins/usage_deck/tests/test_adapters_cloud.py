import unittest

from plugins.usage_deck.adapters import REGISTRY, create_adapter, resolve_adapter_class
from plugins.usage_deck.adapters.cloud import (
    OPENROUTER_KEY_URL,
    AnthropicAdapter,
    NousAdapter,
    OpenAICodexAdapter,
    OpenRouterAdapter,
)
from plugins.usage_deck.adapters.fallback import SessionAnalyticsAdapter
from plugins.usage_deck.adapters.local import SelfHostedAdapter
from plugins.usage_deck.adapters.transport import GuardedTransport, AllowedTarget, TransportError
from plugins.usage_deck.adapters.cloud import OPENROUTER_TARGET

from .helpers import (
    NOW,
    SENTINEL_KEY,
    FakeAnalytics,
    FakeTransport,
    day_row,
    json_response,
    make_ctx,
    raw_response,
    scan_for,
)

from plugins.usage_deck.models.dto import to_plain


class RegistryTests(unittest.TestCase):
    def test_registry_is_dict_keyed_by_provider_id(self):
        self.assertIsInstance(REGISTRY, dict)
        for key in ("nous", "openrouter", "openai-codex", "anthropic", "self_hosted", "unknown"):
            self.assertIn(key, REGISTRY)

    def test_builtin_resolution(self):
        self.assertIs(resolve_adapter_class("nous"), NousAdapter)
        self.assertIs(resolve_adapter_class("openrouter"), OpenRouterAdapter)
        self.assertIs(resolve_adapter_class("openai-codex"), OpenAICodexAdapter)
        self.assertIs(resolve_adapter_class("anthropic"), AnthropicAdapter)

    def test_self_hosted_resolution(self):
        for pid in ("custom:Lab-Box", "custom", "ollama", "Ollama"):
            self.assertIs(resolve_adapter_class(pid), SelfHostedAdapter, pid)

    def test_unknown_provider_falls_back_and_never_raises(self):
        for pid in ("mystery-cloud", "", "x" * 500, "../../etc/passwd", "customx"):
            self.assertIs(resolve_adapter_class(pid), SessionAnalyticsAdapter, pid)
        adapter = create_adapter(make_ctx(provider_id="mystery-cloud"))
        self.assertEqual(adapter.probe().supported, False)

    def test_sentinel_keys_are_not_provider_ids(self):
        # "self_hosted"/"unknown" are registry slots, not stored provider ids
        self.assertIs(resolve_adapter_class("self_hosted"), SessionAnalyticsAdapter)


class OpaqueCloudTests(unittest.TestCase):
    def test_opaque_providers_happy_path_make_no_network_calls(self):
        for cls, pid in ((NousAdapter, "nous"), (OpenAICodexAdapter, "openai-codex"), (AnthropicAdapter, "anthropic")):
            transport = FakeTransport()
            adapter = cls(make_ctx(provider_id=pid, transport=transport))
            probe = adapter.probe()
            self.assertFalse(probe.supported)
            self.assertEqual(probe.reason, "unsupported")
            self.assertIn("session_analytics", probe.capabilities)
            usage = adapter.get_usage(30)
            self.assertEqual(usage.meters, ())
            self.assertEqual(usage.confidence, "opaque")
            self.assertEqual(usage.reason, "unsupported")
            self.assertEqual(adapter.get_confidence(), "opaque")
            self.assertIsNone(adapter.get_health())
            self.assertEqual(transport.requests, [], pid)

    def test_opaque_error_path_broken_analytics_reader_does_not_crash(self):
        class Boom(FakeAnalytics):
            def available(self):
                raise RuntimeError("boom")

        adapter = NousAdapter(make_ctx(provider_id="nous", analytics=Boom()))
        probe = adapter.probe()
        self.assertNotIn("session_analytics", probe.capabilities)

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            NousAdapter(make_ctx(provider_id="nous")).get_usage(14)


class OpenRouterTests(unittest.TestCase):
    def adapter(self, outcome, credential=SENTINEL_KEY):
        transport = FakeTransport({OPENROUTER_KEY_URL: outcome})
        return OpenRouterAdapter(make_ctx(transport=transport, credential=credential)), transport

    def test_happy_path_exact_meter(self):
        adapter, transport = self.adapter(json_response({"data": {"usage": 3.5, "limit": 10, "limit_remaining": 6.5}}))
        probe = adapter.probe()
        self.assertTrue(probe.supported)
        self.assertEqual(transport.requests, [])  # probe makes no request
        usage = adapter.get_usage(30)
        self.assertEqual(len(usage.meters), 1)
        meter = usage.meters[0]
        self.assertEqual((meter.id, meter.unit, meter.scope, meter.source, meter.confidence),
                         ("api_spend", "usd", "api_credential", "openrouter_key", "exact"))
        self.assertEqual((meter.value, meter.limit, meter.remaining), (3.5, 10, 6.5))
        self.assertEqual((meter.period.start, meter.period.end, meter.period.label), (None, None, "unknown"))
        self.assertEqual(meter.observed_at, "2026-11-06T12:00:00Z")
        self.assertEqual(adapter.get_confidence(), "exact")
        # one request only, credential sent only to the fixed URL
        self.assertEqual(transport.urls(), [OPENROUTER_KEY_URL])
        self.assertEqual(transport.requests[0][1]["Authorization"], "Bearer " + SENTINEL_KEY)
        self.assertEqual(scan_for(to_plain(usage), [SENTINEL_KEY]), [])

    def test_explicit_zero_and_null_limit_are_observed_values(self):
        adapter, _ = self.adapter(json_response({"data": {"usage": 0, "limit": None, "limit_remaining": None}}))
        meter = adapter.get_usage(30).meters[0]
        self.assertEqual((meter.value, meter.limit, meter.remaining), (0, None, None))

    def test_negative_remaining_is_overspend_but_negative_usage_rejected(self):
        adapter, _ = self.adapter(json_response({"data": {"usage": 12, "limit": 10, "limit_remaining": -2}}))
        self.assertEqual(adapter.get_usage(30).meters[0].remaining, -2)
        adapter, _ = self.adapter(json_response({"data": {"usage": -1, "limit": 10}}))
        self.assertEqual(adapter.get_usage(30).error.code, "malformed_response")
        adapter, _ = self.adapter(json_response({"data": {"usage": 1, "limit": -10}}))
        self.assertEqual(adapter.get_usage(30).error.code, "malformed_response")

    def test_all_values_absent_is_opaque_not_zero(self):
        adapter, _ = self.adapter(json_response({"data": {"label": "x"}}))
        usage = adapter.get_usage(30)
        self.assertEqual(usage.meters, ())
        self.assertEqual(usage.confidence, "opaque")
        self.assertIsNone(usage.error)

    def test_credentials_missing_makes_no_request(self):
        adapter, transport = self.adapter(json_response({"data": {}}), credential=None)
        usage = adapter.get_usage(30)
        self.assertEqual(usage.reason, "credentials_missing")
        self.assertEqual(usage.meters, ())
        self.assertEqual(transport.requests, [])

    def test_http_error_paths(self):
        cases = {401: "auth_required", 429: "upstream_unavailable", 500: "upstream_unavailable",
                 503: "upstream_unavailable", 400: "malformed_response"}
        for status, code in cases.items():
            adapter, _ = self.adapter(json_response({"error": SENTINEL_KEY}, status=status))
            usage = adapter.get_usage(30)
            self.assertEqual(usage.error.code, code, status)
            self.assertEqual(usage.meters, ())
            self.assertEqual(scan_for(to_plain(usage), [SENTINEL_KEY]), [], status)
        for status in (403, 404):
            adapter, _ = self.adapter(json_response({}, status=status))
            usage = adapter.get_usage(30)
            self.assertEqual(usage.reason, "permission_denied")
            self.assertIsNone(usage.error)

    def test_transport_failures(self):
        cases = {TransportError.TIMEOUT: "timeout", TransportError.CONNECTION: "upstream_unavailable",
                 TransportError.TLS: "upstream_unavailable", TransportError.REDIRECT: "malformed_response",
                 TransportError.OVERSIZE: "malformed_response"}
        for kind, code in cases.items():
            adapter, _ = self.adapter(TransportError(kind))
            self.assertEqual(adapter.get_usage(30).error.code, code, kind)

    def test_malformed_bodies(self):
        bodies = ["not json", '{"data": []}', '[]', '{"data": {"usage": "3"}}',
                  '{"data": {"usage": NaN}}', '{"data": {"usage": Infinity}}', '{"data": {"usage": true}}']
        for body in bodies:
            adapter, _ = self.adapter(raw_response(body))
            self.assertEqual(adapter.get_usage(30).error.code, "malformed_response", body)

    def test_snapshot_reused(self):
        adapter, transport = self.adapter(json_response({"data": {"usage": 1}}))
        adapter.probe()
        adapter.get_usage(30)
        adapter.get_usage(30)
        adapter.get_confidence()
        self.assertEqual(len(transport.requests), 1)


class GuardedTransportTests(unittest.TestCase):
    def test_refuses_other_origins_paths_and_userinfo(self):
        inner = FakeTransport({OPENROUTER_KEY_URL: json_response({})})
        guard = GuardedTransport(inner, [OPENROUTER_TARGET])
        guard.get(OPENROUTER_KEY_URL)
        for bad in ("https://evil.example/api/v1/key", "https://openrouter.ai/api/v1/credits",
                    "http://openrouter.ai/api/v1/key", "https://user:pw@openrouter.ai/api/v1/key",
                    "https://openrouter.ai/api/v1/key?x=1", "https://openrouter.ai/api/v1/key#f", "file:///etc/passwd"):
            with self.assertRaises(TransportError, msg=bad) as ctx:
                guard.get(bad)
            self.assertEqual(ctx.exception.kind, TransportError.FORBIDDEN_URL)
        self.assertEqual(len(inner.requests), 1)

    def test_clamps_timeout_and_size(self):
        inner = FakeTransport({"http://h:1/x": json_response({})})
        GuardedTransport(inner, [AllowedTarget("http", "h", 1, None)]).get("http://h:1/x", timeout=999, max_bytes=10**9)
        self.assertLessEqual(inner.requests[0][2], 2.0)


class SessionAnalyticsAdapterTests(unittest.TestCase):
    def test_happy_path_profile_series_estimated(self):
        analytics = FakeAnalytics([day_row("2026-11-05"), day_row("2026-11-04", usd=None, calls=None)])
        adapter = SessionAnalyticsAdapter(make_ctx(provider_id="mystery", credential=None, analytics=analytics))
        probe = adapter.probe()
        self.assertEqual((probe.supported, probe.capabilities, probe.reason), (False, ("session_analytics",), "unsupported"))
        usage = adapter.get_usage(7)
        self.assertEqual(usage.meters, ())
        self.assertEqual(usage.confidence, "opaque")
        self.assertEqual(analytics.calls, [7])
        series = usage.series[0]
        self.assertEqual((series.scope, series.confidence, series.source, series.provider_id, series.account_id),
                         ("profile", "estimated", "session_analytics", None, None))
        self.assertEqual([p.day for p in series.points], ["2026-11-04", "2026-11-05"])
        self.assertIsNone(series.points[0].estimated_usd)  # null stays null
        self.assertEqual((series.timezone, series.bucket, series.coverage), ("server_local", "day", "partial"))
        self.assertEqual((series.period.start, series.period.end, series.period.label),
                         ("2026-10-30T12:00:00Z", "2026-11-06T12:00:00Z", "rolling_7d"))

    def test_rows_without_numeric_observations_are_not_zeros(self):
        from plugins.usage_deck.adapters.base import AnalyticsDay
        analytics = FakeAnalytics([AnalyticsDay("2026-11-05", None, None, None, None, None), day_row("2026-11-06", 0, 0, 0.0, 1, 0)])
        series = SessionAnalyticsAdapter(make_ctx(provider_id="m", analytics=analytics)).get_usage(30).series[0]
        self.assertEqual([p.day for p in series.points], ["2026-11-06"])
        self.assertEqual(series.points[0].input_count, 0)  # observed zero stays zero

    def test_error_path_analytics_unavailable(self):
        adapter = SessionAnalyticsAdapter(make_ctx(provider_id="m", analytics=FakeAnalytics(fail=True)))
        usage = adapter.get_usage(30)
        self.assertEqual(usage.error.code, "analytics_unavailable")
        self.assertEqual(usage.series, ())
        self.assertFalse(SessionAnalyticsAdapter(make_ctx(provider_id="m", analytics=FakeAnalytics(available=False))).probe().capabilities)

    def test_unexpected_reader_exception_is_contained(self):
        class Weird(FakeAnalytics):
            def read_usage(self, days):
                raise ValueError("with secret " + SENTINEL_KEY)

        usage = SessionAnalyticsAdapter(make_ctx(provider_id="m", analytics=Weird())).get_usage(30)
        self.assertEqual(usage.error.code, "analytics_unavailable")
        self.assertEqual(scan_for(to_plain(usage), [SENTINEL_KEY]), [])


if __name__ == "__main__":
    unittest.main()
