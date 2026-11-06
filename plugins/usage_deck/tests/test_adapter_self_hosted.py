import http.server
import threading
import time
import unittest

from plugins.usage_deck.adapters.local import SelfHostedAdapter
from plugins.usage_deck.adapters.prometheus import parse_exposition
from plugins.usage_deck.adapters.transport import TransportError, UrllibTransport
from plugins.usage_deck.models.dto import to_plain

from .helpers import SENTINEL_KEY, FakeTransport, json_response, make_ctx, raw_response, scan_for

BASE = "http://gpu-box.lan:8000"
METRICS = """# HELP process_cpu_percent cpu
# TYPE process_cpu_percent gauge
process_cpu_percent 41.5
process_resident_memory_bytes 2147483648
"""


def adapter(routes, provider="custom:gpu", base=BASE + "/v1", credential=SENTINEL_KEY):
    transport = FakeTransport(routes)
    ctx = make_ctx(provider_id=provider, kind="self_hosted", endpoint=base, transport=transport, credential=credential)
    return SelfHostedAdapter(ctx), transport


class PrometheusParserTests(unittest.TestCase):
    def test_allowlisted_samples(self):
        m = parse_exposition(METRICS)
        self.assertEqual((m.cpu_percent, m.memory_bytes), (41.5, 2147483648))

    def test_per_core_cpu_is_averaged_not_summed(self):
        m = parse_exposition('cpu_usage_percent{core="0"} 10\ncpu_usage_percent{core="1"} 30\n')
        self.assertEqual(m.cpu_percent, 20.0)

    def test_valid_empty_exposition_gives_nulls(self):
        for text in ("", "# HELP x y\n", "other_metric 1\n"):
            m = parse_exposition(text)
            self.assertIsNotNone(m)
            self.assertEqual((m.cpu_percent, m.memory_bytes), (None, None))

    def test_invalid_and_hostile_values(self):
        self.assertIsNone(parse_exposition("<html>not metrics</html>"))
        self.assertIsNone(parse_exposition("process_cpu_percent abc\n"))
        for bad in ("process_cpu_percent NaN", "process_cpu_percent +Inf", "process_resident_memory_bytes -5",
                    "process_resident_memory_bytes Inf"):
            m = parse_exposition(bad + "\n")
            self.assertIsNotNone(m, bad)
            self.assertEqual((m.cpu_percent, m.memory_bytes), (None, None), bad)

    def test_ambiguous_memory_samples_stay_null(self):
        m = parse_exposition('memory_usage_bytes{a="1"} 5\nmemory_usage_bytes{a="2"} 6\n')
        self.assertIsNone(m.memory_bytes)


class SelfHostedHealthTests(unittest.TestCase):
    def test_healthy_with_metrics(self):
        a, t = adapter({BASE + "/metrics": raw_response(METRICS, latency=3.0),
                        BASE + "/v1/models": json_response({"data": []}, latency=9.0)})
        probe = a.probe()
        self.assertTrue(probe.supported)
        self.assertTrue(probe.reachable)
        self.assertIn("metrics", probe.capabilities)
        usage = a.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "self_hosted", "unsupported"))
        health = a.get_health()
        self.assertEqual((health.status, health.confidence, health.metrics_available, health.reason),
                         ("healthy", "self_hosted", True, None))
        self.assertEqual((health.latency_ms, health.cpu_percent, health.memory_bytes), (9.0, 41.5, 2147483648))
        self.assertEqual(a.get_confidence(), "self_hosted")
        # endpoint paths derive from the origin, not appended to /v1; metrics never get the inference credential
        self.assertEqual(t.urls(), [BASE + "/metrics", BASE + "/v1/models"])
        self.assertNotIn("Authorization", t.requests[0][1])
        self.assertEqual(t.requests[1][1]["Authorization"], "Bearer " + SENTINEL_KEY)

    def test_models_reachable_without_metrics_is_degraded_opaque(self):
        a, _ = adapter({BASE + "/metrics": raw_response("nope", status=404),
                        BASE + "/v1/models": json_response({"data": [{"id": "m"}]}, latency=7.0)})
        health = a.get_health()
        self.assertEqual((health.status, health.confidence, health.metrics_available, health.reason),
                         ("degraded", "opaque", False, "metrics_unavailable"))
        self.assertEqual((health.latency_ms, health.cpu_percent, health.memory_bytes), (7.0, None, None))
        self.assertEqual(a.get_confidence(), "opaque")
        self.assertEqual(a.get_usage(30).reason, "metrics_unavailable")
        self.assertFalse(a.probe().supported)
        self.assertTrue(a.probe().reachable)

    def test_ollama_uses_api_tags(self):
        o = "http://localhost:11434"
        a, t = adapter({o + "/api/tags": json_response({"models": []}, latency=4.0)}, provider="ollama", base=o)
        health = a.get_health()
        self.assertEqual(health.status, "degraded")
        self.assertEqual(health.latency_ms, 4.0)
        self.assertEqual(t.urls(), [o + "/metrics", o + "/api/tags"])

    def test_v1_falls_back_to_api_tags(self):
        a, t = adapter({BASE + "/v1/models": raw_response("nope", status=404),
                        BASE + "/api/tags": json_response({"models": []})})
        self.assertEqual(a.get_health().status, "degraded")
        self.assertIn(BASE + "/api/tags", t.urls())

    def test_metrics_ok_health_failed_is_degraded_self_hosted(self):
        a, _ = adapter({BASE + "/metrics": raw_response(METRICS, latency=3.0),
                        BASE + "/v1/models": raw_response("x", status=500),
                        BASE + "/api/tags": raw_response("x", status=500)})
        health = a.get_health()
        self.assertEqual((health.status, health.confidence), ("degraded", "self_hosted"))
        self.assertEqual(health.latency_ms, 3.0)  # falls back to the successful metrics request

    def test_offline_when_all_requests_fail_to_connect(self):
        a, t = adapter({})
        health = a.get_health()
        self.assertEqual((health.status, health.confidence, health.latency_ms), ("offline", "opaque", None))
        self.assertEqual(health.error.code, "upstream_unavailable")
        self.assertEqual(a.probe().reachable, False)
        self.assertEqual(a.get_usage(30).error.code, "upstream_unavailable")
        self.assertEqual(len(t.requests), 2)  # metrics + one health attempt; a dead host is not hammered

    def test_offline_timeout_code(self):
        a, _ = adapter({BASE + "/metrics": TransportError(TransportError.TIMEOUT),
                        BASE + "/v1/models": TransportError(TransportError.TIMEOUT)})
        self.assertEqual(a.get_health().error.code, "timeout")

    def test_unknown_when_responses_are_invalid_not_connection_failures(self):
        a, _ = adapter({BASE + "/metrics": raw_response("<html>", status=200),
                        BASE + "/v1/models": raw_response("<html>", status=200),
                        BASE + "/api/tags": raw_response("<html>", status=200)})
        health = a.get_health()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(health.error.code, "malformed_response")
        self.assertIsNone(health.latency_ms)

    def test_auth_failure_is_unknown_with_auth_error(self):
        a, _ = adapter({BASE + "/metrics": raw_response("x", status=404),
                        BASE + "/v1/models": raw_response("x", status=401),
                        BASE + "/api/tags": raw_response("x", status=401)})
        health = a.get_health()
        self.assertEqual((health.status, health.error.code), ("unknown", "auth_required"))

    def test_userinfo_endpoint_is_never_requested(self):
        a, t = adapter({}, base="http://user:" + SENTINEL_KEY + "@gpu-box.lan:8000/v1")
        health = a.get_health()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(t.requests, [])
        self.assertEqual(scan_for(to_plain(health), [SENTINEL_KEY, "user:"]), [])
        self.assertFalse(a.probe().supported)

    def test_unconfigured_endpoint_has_no_health_row(self):
        ctx = make_ctx(provider_id="custom:x", kind="self_hosted", endpoint=None)
        self.assertIsNone(SelfHostedAdapter(ctx).get_health())

    def test_invalid_metrics_values_remain_null(self):
        a, _ = adapter({BASE + "/metrics": raw_response("process_cpu_percent NaN\n"),
                        BASE + "/v1/models": json_response({"data": []})})
        h = a.get_health()
        self.assertEqual((h.status, h.metrics_available, h.cpu_percent, h.memory_bytes), ("healthy", True, None, None))


class UrllibTransportTests(unittest.TestCase):
    """Real sockets on loopback: redirects refused, size capped, timeouts finite."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                elif self.path == "/big":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 5000)
                elif self.path == "/slow":
                    time.sleep(1.0)
                    self.send_response(200)
                    self.end_headers()
                elif self.path == "/500":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"boom")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ok(self):
        r = UrllibTransport().get(self.base + "/ok")
        self.assertEqual((r.status, r.body), (200, b'{"ok": true}'))
        self.assertGreater(r.latency_ms, 0)

    def test_redirect_is_error_even_same_origin(self):
        with self.assertRaises(TransportError) as cm:
            UrllibTransport().get(self.base + "/redir")
        self.assertEqual(cm.exception.kind, TransportError.REDIRECT)

    def test_oversize(self):
        with self.assertRaises(TransportError) as cm:
            UrllibTransport().get(self.base + "/big", max_bytes=1000)
        self.assertEqual(cm.exception.kind, TransportError.OVERSIZE)

    def test_timeout(self):
        with self.assertRaises(TransportError) as cm:
            UrllibTransport().get(self.base + "/slow", timeout=0.2)
        self.assertEqual(cm.exception.kind, TransportError.TIMEOUT)

    def test_error_status_returned_not_raised(self):
        r = UrllibTransport().get(self.base + "/500")
        self.assertEqual(r.status, 500)

    def test_connection_refused(self):
        with self.assertRaises(TransportError) as cm:
            UrllibTransport().get("http://127.0.0.1:9/x", timeout=0.5)
        self.assertEqual(cm.exception.kind, TransportError.CONNECTION)


if __name__ == "__main__":
    unittest.main()
