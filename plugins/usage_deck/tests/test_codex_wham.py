"""Codex workspace-spend adapter (DESIGN.md §9.2, docs/decisions/codex-wham-usage.md).

Everything runs against injected transports and scratch homes: no live request, no refresh, no real
credential. Tokens are unsigned JWT-shaped strings built by ``make_jwt`` with invented claims.
"""

import copy
import hashlib
import http.server
import json
import logging
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from plugins.usage_deck.adapters import create_adapter
from plugins.usage_deck.adapters.cloud import (
    CODEX_ORIGINATOR,
    CODEX_TARGET,
    CODEX_USAGE_URL,
    CODEX_USER_AGENT,
    OPENROUTER_KEY_URL,
    OPENROUTER_TARGET,
    CodexRouting,
    OpenAICodexAdapter,
    codex_request_headers,
    codex_routing,
    codex_spend_meter,
)
from plugins.usage_deck.adapters.transport import (
    AllowedTarget,
    GuardedTransport,
    HttpResponse,
    TransportError,
    UrllibTransport,
)
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.dto import DtoError, check_wire

from .helpers import (
    NOW,
    SENTINEL_TOKEN,
    FakeTransport,
    json_response,
    make_ctx,
    make_jwt,
    raw_response,
    scan_for,
)
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

AUTH = "https://api.openai.com/auth"
ACCOUNT = "udsyn-acct-5c1d0f6e-0000-4000-8000-0123456789ab"
USER = "udsyn-user-7f2a9c3b-0000-4000-8000-ba9876543210"
EMAIL = "udsyn.person@example.invalid"
RESIDENCY = "udsyn-residency-region"  # >= 4 chars: also joins the masking context
EVIDENCE = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "codex-wham-usage-200.sanitized.json"
NOW_EPOCH = int(NOW.timestamp())
FUTURE = NOW_EPOCH + 20 * 86400
FIXED_HEADER_NAMES = {"Authorization", "Accept", "User-Agent", "originator", "ChatGPT-Account-ID",
                      "x-openai-internal-codex-residency"}


def codex_token(account=ACCOUNT, user=USER, plan="business", residency=RESIDENCY, jti="1", **auth_extra):
    claims = {"chatgpt_account_id": account, "chatgpt_user_id": user, "chatgpt_plan_type": plan}
    if residency is not None:
        claims["chatgpt_data_residency"] = residency
    claims.update(auth_extra)
    return make_jwt({AUTH: claims, "jti": jti})


def limit_body(**over):
    """The recorded workspace spend-control shape, with per-test overrides of individual_limit fields."""
    limit = {"source": "workspace_spend_controls", "unit": "credit", "limit": "1500", "used": "864.5",
             "remaining": "635.5", "used_percent": 58, "remaining_percent": 42,
             "reset_after_seconds": 1000, "reset_at": FUTURE}
    limit.update(over)
    return {"user_id": USER, "account_id": ACCOUNT, "email": EMAIL, "plan_type": "business",
            "spend_control": {"reached": False, "individual_limit": limit}}


def adapter_for(outcome, token=None, transport=None):
    token = codex_token() if token is None else token
    transport = transport or FakeTransport({CODEX_USAGE_URL: outcome} if outcome is not None else {})
    ctx = make_ctx("openai-codex", credential=token, transport=transport, secrets=[token])
    return create_adapter(ctx), transport


def usage_of(body, status=200):
    adapter, transport = adapter_for(json_response(body, status))
    return adapter.get_usage(30), adapter, transport


class RoutingClaims(unittest.TestCase):
    """The claims that route the request: valid ones are read from the selected token only."""

    def test_account_and_residency_from_the_auth_claim_object(self):
        routing = codex_routing(codex_token())
        self.assertEqual((routing.account_id, routing.residency), (ACCOUNT, RESIDENCY))

    def test_residency_falls_back_to_compute_residency_and_may_be_absent(self):
        compute = make_jwt({AUTH: {"chatgpt_account_id": "a1", "chatgpt_compute_residency": "eu-compute"}})
        self.assertEqual(codex_routing(compute).residency, "eu-compute")
        both = make_jwt({AUTH: {"chatgpt_account_id": "a1", "chatgpt_data_residency": "data-r",
                                "chatgpt_compute_residency": "compute-r"}})
        self.assertEqual(codex_routing(both).residency, "data-r")  # data residency wins
        for empty in (None, ""):
            token = make_jwt({AUTH: {"chatgpt_account_id": "a1", "chatgpt_data_residency": empty}})
            self.assertEqual(codex_routing(token), CodexRouting("a1", None))
        self.assertEqual(codex_routing(make_jwt({AUTH: {"chatgpt_account_id": "a1"}})), CodexRouting("a1", None))

    def test_hostile_or_missing_claims_yield_no_routing(self):
        good = {"chatgpt_account_id": "a1"}
        hostile = [
            {},                                                      # no account
            {"chatgpt_account_id": None}, {"chatgpt_account_id": ""}, {"chatgpt_account_id": 5},
            {"chatgpt_account_id": ["a1"]}, {"chatgpt_account_id": {"x": "a1"}}, {"chatgpt_account_id": True},
            {"chatgpt_account_id": "has space"}, {"chatgpt_account_id": " a1"}, {"chatgpt_account_id": "a1 "},
            {"chatgpt_account_id": "a1\r\nX-Evil: 1"}, {"chatgpt_account_id": "a1\n"}, {"chatgpt_account_id": "a\x00b"},
            {"chatgpt_account_id": "a\x7fb"}, {"chatgpt_account_id": "é"}, {"chatgpt_account_id": "a" * 257},
            {**good, "chatgpt_data_residency": "has space"}, {**good, "chatgpt_data_residency": "r\r\nX: y"},
            {**good, "chatgpt_data_residency": 5}, {**good, "chatgpt_data_residency": ["us"]},
            {**good, "chatgpt_data_residency": False}, {**good, "chatgpt_data_residency": " "},
            {**good, "chatgpt_data_residency": "x" * 257},
            {**good, "chatgpt_data_residency": None, "chatgpt_compute_residency": "bad value"},
        ]
        for claims in hostile:
            with self.subTest(claims=claims):
                self.assertIsNone(codex_routing(make_jwt({AUTH: claims})))
        self.assertEqual(codex_routing(make_jwt({AUTH: {"chatgpt_account_id": "a" * 256}})).account_id, "a" * 256)

    def test_claims_outside_the_auth_object_are_never_used(self):
        self.assertIsNone(codex_routing(make_jwt({"chatgpt_account_id": "a1"})))
        self.assertIsNone(codex_routing(make_jwt({AUTH: ["chatgpt_account_id", "a1"]})))
        self.assertIsNone(codex_routing(make_jwt({AUTH: "a1"})))
        self.assertIsNone(codex_routing(make_jwt({"https://evil.example/auth": {"chatgpt_account_id": "a1"}})))
        self.assertIsNone(codex_routing(make_jwt(["not", "an", "object"])))

    def test_malformed_tokens_yield_no_routing(self):
        def enc(raw: bytes):
            import base64
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        payload = enc(json.dumps({AUTH: {"chatgpt_account_id": "a1"}}).encode())
        for token in (None, "", 5, "a.b", "a.b.c.d", "syn.codex.jwt", f"h.{payload}", f"h.{payload}.s.x",
                      f"h.{payload}!.s", f"h.{payload} .s", f"h .{payload}.s", f"h.{payload}.s s",
                      "h." + enc(b"not json") + ".s", "h." + enc(b"\xff\xfe") + ".s", "h." + enc(b"[") + ".s",
                      "h." + enc(b"[" * 20000) + ".s", "h.a.s", "h." + "A" * 16400 + ".s",
                      "h." + enc(json.dumps({AUTH: {"chatgpt_account_id": "é"}}).encode()) + ".s"):
            with self.subTest(token=str(token)[:30]):
                self.assertIsNone(codex_routing(token))
        self.assertIsNotNone(codex_routing(f"h.{payload}."))  # an unsigned token has an empty signature segment

    def test_routing_never_reveals_itself(self):
        routing = codex_routing(codex_token())
        self.assertNotIn(ACCOUNT, repr(routing) + str(routing))
        self.assertEqual(routing.masking_values(), frozenset({ACCOUNT, RESIDENCY}))
        # a short region code is a hint, not a secret: masking it would corrupt e.g. plan "business"
        self.assertEqual(CodexRouting(ACCOUNT, "us").masking_values(), frozenset({ACCOUNT}))

    def test_request_headers_are_exactly_the_approved_set(self):
        token = codex_token()
        headers = codex_request_headers(token, codex_routing(token))
        self.assertEqual(headers, {
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "HermesAgent/usage-deck", "originator": "hermes-agent",
            "ChatGPT-Account-ID": ACCOUNT, "x-openai-internal-codex-residency": RESIDENCY})
        bare = codex_request_headers(token, CodexRouting(ACCOUNT, None))
        self.assertEqual(set(bare), FIXED_HEADER_NAMES - {"x-openai-internal-codex-residency"})
        self.assertEqual((CODEX_USER_AGENT, CODEX_ORIGINATOR), ("HermesAgent/usage-deck", "hermes-agent"))


class Egress(unittest.TestCase):
    """What leaves the process, and when nothing may."""

    def test_one_exact_get_with_the_approved_headers(self):
        token = codex_token()
        adapter, transport = adapter_for(json_response(limit_body()), token)
        adapter.get_usage(30)
        self.assertEqual(len(transport.requests), 1)
        url, headers, _timeout = transport.requests[0]
        self.assertEqual(url, "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(headers, {
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "HermesAgent/usage-deck", "originator": "hermes-agent",
            "ChatGPT-Account-ID": ACCOUNT, "x-openai-internal-codex-residency": RESIDENCY})

    def test_absent_residency_omits_the_residency_header(self):
        adapter, transport = adapter_for(json_response(limit_body()), codex_token(residency=None))
        adapter.get_usage(30)
        self.assertEqual(set(transport.requests[0][1]), FIXED_HEADER_NAMES - {"x-openai-internal-codex-residency"})

    def test_no_credential_no_request(self):
        transport = FakeTransport()
        adapter = create_adapter(make_ctx("openai-codex", credential=None, transport=transport))
        probe = adapter.probe()
        self.assertEqual((probe.supported, probe.reason), (False, "credentials_missing"))
        usage = adapter.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error), ((), "opaque", "credentials_missing", None))
        self.assertEqual(transport.requests, [])

    def test_unusable_routing_claims_no_request(self):
        for token in ("syn.codex.jwt", codex_token(account=" bad"), codex_token(account=None),
                      codex_token(residency="bad value"), codex_token(residency="r\nx"),
                      make_jwt({"plan": "business"}), "sk-not-a-jwt"):
            with self.subTest(token=token[:20]):
                transport = FakeTransport({CODEX_USAGE_URL: json_response(limit_body())})
                adapter = create_adapter(make_ctx("openai-codex", credential=token, transport=transport, secrets=[token]))
                probe = adapter.probe()
                self.assertEqual((probe.supported, probe.reason), (False, "source_unavailable"))
                usage = adapter.get_usage(30)
                self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error),
                                 ((), "opaque", "source_unavailable", None))
                self.assertEqual(adapter.source_label, "local_inventory")
                self.assertEqual(transport.requests, [])

    def test_probe_is_local_and_the_snapshot_is_fetched_once(self):
        adapter, transport = adapter_for(json_response(limit_body()))
        probe = adapter.probe()
        self.assertTrue(probe.supported)
        self.assertIn("quota", probe.capabilities)
        self.assertEqual(transport.requests, [])  # probing spends no request
        first = adapter.get_usage(30)
        adapter.get_usage(7)
        adapter.get_usage(90)
        self.assertEqual(adapter.get_confidence(), "exact")
        self.assertIs(adapter.get_usage(30), first)
        self.assertEqual(len(transport.requests), 1)  # one request per snapshot, whatever the window

    def test_a_failed_snapshot_is_not_retried(self):
        adapter, transport = adapter_for(json_response({}, 500))
        adapter.get_usage(30)
        adapter.get_usage(7)
        adapter.get_confidence()
        self.assertEqual(len(transport.requests), 1)  # no in-request retry

    def test_guard_allows_exactly_the_target_url(self):
        inner = FakeTransport({CODEX_USAGE_URL: json_response({}),
                               "https://chatgpt.com:443/backend-api/wham/usage": json_response({})})
        guard = GuardedTransport(inner, [CODEX_TARGET])
        self.assertEqual(CODEX_TARGET, AllowedTarget("https", "chatgpt.com", 443, ("/backend-api/wham/usage",)))
        self.assertEqual(guard.get(CODEX_USAGE_URL).status, 200)
        self.assertEqual(guard.get("https://chatgpt.com:443/backend-api/wham/usage").status, 200)
        denied = (
            "https://chatgpt.com/backend-api/wham/usage?x=1",       # query
            "https://chatgpt.com/backend-api/wham/usage?",          # even an empty query
            "https://chatgpt.com/backend-api/wham/usage#frag",      # fragment
            "https://user@chatgpt.com/backend-api/wham/usage",      # userinfo
            "https://user:pw@chatgpt.com/backend-api/wham/usage",
            "https://chatgpt.com:8443/backend-api/wham/usage",      # other port
            "http://chatgpt.com/backend-api/wham/usage",            # other scheme / implicit port 80
            "http://chatgpt.com:443/backend-api/wham/usage",
            "https://chatgpt.com/backend-api/usage",                # sibling path (the 404 route)
            "https://chatgpt.com/backend-api/wham/usage/",
            "https://chatgpt.com/backend-api/wham/usage/x",
            "https://chatgpt.com/backend-api/wham",
            "https://chatgpt.com/wham/usage",
            "https://chatgpt.com//backend-api/wham/usage",
            "https://chatgpt.com/backend-api/wham/usage;a=b",
            "https://chatgpt.com/backend-api/WHAM/usage",
            "https://chatgpt.com/",
            "https://www.chatgpt.com/backend-api/wham/usage",       # other hosts
            "https://chatgpt.com.evil.example/backend-api/wham/usage",
            "https://evilchatgpt.com/backend-api/wham/usage",
            "https://chatgpt.com.@evil.example/backend-api/wham/usage",
            "https://api.openai.com/backend-api/wham/usage",
            "https://openrouter.ai/api/v1/key",
            "https://chatgpt.com\\@evil.example/backend-api/wham/usage",
            "//chatgpt.com/backend-api/wham/usage",
            "file:///backend-api/wham/usage",
        )
        before = len(inner.requests)
        for url in denied:
            with self.subTest(url=url):
                with self.assertRaises(TransportError) as cm:
                    guard.get(url, headers={"Authorization": "Bearer x"})
                self.assertEqual(cm.exception.kind, TransportError.FORBIDDEN_URL)
        self.assertEqual(len(inner.requests), before)  # nothing was forwarded

    def test_the_codex_target_is_granted_to_no_other_provider_and_vice_versa(self):
        inner = FakeTransport({CODEX_USAGE_URL: json_response({}), OPENROUTER_KEY_URL: json_response({})})
        codex_guard = GuardedTransport(inner, [CODEX_TARGET])
        with self.assertRaises(TransportError):
            codex_guard.get(OPENROUTER_KEY_URL)
        router_guard = GuardedTransport(inner, [OPENROUTER_TARGET])
        with self.assertRaises(TransportError):
            router_guard.get(CODEX_USAGE_URL)
        self.assertEqual(inner.requests, [])

    def test_a_guard_without_the_target_turns_into_internal_error_not_a_request(self):
        inner = FakeTransport({CODEX_USAGE_URL: json_response(limit_body())})
        token = codex_token()
        ctx = make_ctx("openai-codex", credential=token, transport=GuardedTransport(inner, []), secrets=[token])
        usage = create_adapter(ctx).get_usage(30)
        self.assertEqual(usage.error.code, "internal_error")
        self.assertEqual(inner.requests, [])

    def test_redirects_are_errors_even_when_same_origin(self):
        adapter, _ = adapter_for(TransportError(TransportError.REDIRECT))
        self.assertEqual(adapter.get_usage(30).error.code, "malformed_response")
        for status in (301, 302, 307, 308):
            adapter, transport = adapter_for(HttpResponse(status, b"", 1.0, None))
            self.assertEqual(adapter.get_usage(30).error.code, "malformed_response", status)
            self.assertEqual(len(transport.requests), 1)  # the Location is never followed


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):  # noqa: D401
        return

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()


class StdlibTransportWire(unittest.TestCase):
    """The production transport is stdlib urllib: check what it actually puts on the wire."""

    def test_headers_reach_the_wire_and_a_redirect_is_not_followed(self):
        handler = type("H", (_Recorder,), {"seen": []})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = codex_token()
        headers = codex_request_headers(token, codex_routing(token))
        with self.assertRaises(TransportError) as cm:
            UrllibTransport().get(f"http://127.0.0.1:{server.server_address[1]}/backend-api/wham/usage", headers=headers)
        self.assertEqual(cm.exception.kind, TransportError.REDIRECT)
        self.assertEqual(len(handler.seen), 1)  # /elsewhere was never requested
        wire = {k.lower(): v for k, v in handler.seen[0][1].items()}
        self.assertEqual(wire["authorization"], "Bearer " + token)
        self.assertEqual(wire["user-agent"], "HermesAgent/usage-deck")  # not Python-urllib
        self.assertEqual(wire["originator"], "hermes-agent")
        self.assertEqual(wire["chatgpt-account-id"], ACCOUNT)
        self.assertEqual(wire["x-openai-internal-codex-residency"], RESIDENCY)
        self.assertEqual(wire["accept"], "application/json")
        self.assertNotIn("cookie", wire)
        self.assertEqual(wire.get("accept-encoding"), "identity")  # stdlib default: no compressed body to inflate


class SpendMeter(unittest.TestCase):
    def meter(self, **over):
        usage, _, _ = usage_of(limit_body(**over))
        self.assertEqual(usage.error, None, over)
        return usage

    def test_recorded_200_body_maps_to_one_exact_credit_meter(self):
        body = json.loads(EVIDENCE.read_text())
        self.assertEqual(body["spend_control"]["individual_limit"]["unit"], "credit")
        body = copy.deepcopy(body)
        body["spend_control"]["individual_limit"]["reset_at"] = FUTURE  # the recorded reset is in the real future
        usage, adapter, transport = usage_of(body)
        self.assertEqual((usage.confidence, usage.reason, usage.error, usage.series), ("exact", None, None, ()))
        (m,) = usage.meters
        self.assertEqual(
            dto.to_plain(m),
            {"id": "workspace_spend", "label": "Workspace spend limit (monthly)", "value": 864.4848500490189,
             "limit": 1500.0, "remaining": 635.5151499509811, "unit": "credit", "scope": "provider_account",
             "period": {"start": None, "end": None, "label": "monthly_workspace"}, "confidence": "exact",
             "source": "chatgpt_wham_usage", "observed_at": "2026-11-06T12:00:00Z",
             "resets_at": (NOW + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.assertEqual(adapter.source_label, "chatgpt_wham_usage")
        self.assertEqual(len(transport.requests), 1)

    def test_recorded_reset_converts_to_utc_rfc3339(self):
        m = codex_spend_meter(json.loads(EVIDENCE.read_text()), NOW)
        self.assertEqual(m.resets_at, "2026-12-01T00:00:01Z")  # 1796083201
        self.assertEqual(m.observed_at, "2026-11-06T12:00:00Z")

    def test_only_the_spend_control_is_read_nothing_else_is_emitted(self):
        body = limit_body()
        body.update({"credits": {"balance": "635.5", "has_credits": True}, "model_usage": {"gpt-x": {"available": True}},
                     "rate_limit": {"primary": {"used_percent": 99}}, "promo": {"x": 1}, "plan_type": "enterprise"})
        usage, _, _ = usage_of(body)
        self.assertEqual(len(usage.meters), 1)  # no credits-balance, rate-limit or model meter
        blob = json.dumps(dto.to_plain(usage))
        for leaked in (EMAIL, USER, ACCOUNT, "enterprise", "gpt-x", "balance"):
            self.assertNotIn(leaked, blob)

    def test_rounded_percentages_never_override_or_create_observations(self):
        usage = self.meter(used_percent=1, remaining_percent=99)
        (m,) = usage.meters
        self.assertEqual((m.value, m.limit, m.remaining), (864.5, 1500.0, 635.5))
        (m,) = self.meter(used=None, remaining=None, used_percent=58, remaining_percent=42).meters
        self.assertEqual((m.value, m.limit, m.remaining), (None, 1500.0, None))  # nothing synthesized from percent

    def test_numbers_may_be_json_numbers_or_decimal_strings(self):
        (m,) = self.meter(limit=1500, used=864.25, remaining="635.75").meters
        self.assertEqual((m.value, m.limit, m.remaining), (864.25, 1500.0, 635.75))

    def test_zero_is_an_observation_and_the_card_is_ready(self):
        (m,) = self.meter(used="0", remaining="1500").meters
        self.assertEqual((m.value, m.limit, m.remaining), (0.0, 1500.0, 1500.0))
        (m,) = self.meter(used=0, limit=0, remaining=0).meters
        self.assertEqual((m.value, m.limit, m.remaining), (0.0, 0.0, 0.0))
        (m,) = self.meter(used="-0").meters
        self.assertEqual(str(m.value), "0.0")  # no negative zero on the wire

    def test_null_stays_null_and_limit_only_is_still_a_reading(self):
        (m,) = self.meter(used=None, remaining=None).meters
        self.assertEqual((m.value, m.limit, m.remaining), (None, 1500.0, None))
        (m,) = self.meter(limit=None, remaining=None).meters
        self.assertEqual((m.value, m.limit, m.remaining), (864.5, None, None))  # no limit invented, remaining not derived
        (m,) = self.meter(limit=None, used=None).meters
        self.assertEqual((m.value, m.limit, m.remaining), (None, None, 635.5))
        body = limit_body()
        for key in ("used", "remaining"):
            del body["spend_control"]["individual_limit"][key]  # absent behaves as null
        usage, _, _ = usage_of(body)
        self.assertEqual((usage.meters[0].value, usage.meters[0].limit, usage.meters[0].remaining), (None, 1500.0, None))

    def test_overspend_is_not_clamped(self):
        (m,) = self.meter(used="1700", remaining="0").meters
        self.assertEqual((m.value, m.limit, m.remaining), (1700.0, 1500.0, 0.0))

    def test_all_numbers_absent_or_null_is_opaque_not_zero(self):
        usage = usage_of(limit_body(used=None, limit=None, remaining=None))[0]
        self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error), ((), "opaque", "source_unavailable", None))
        body = limit_body()
        for key in ("used", "limit", "remaining"):
            del body["spend_control"]["individual_limit"][key]
        usage = usage_of(body)[0]
        self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error), ((), "opaque", "source_unavailable", None))

    def test_invalid_present_numbers_are_malformed(self):
        bad = (True, False, "", " ", " 1", "1 ", "abc", "1,000", "1e3", "1E3", "+1", ".5", "5.", "0x10", "NaN", "inf",
               "Infinity", "-Infinity", "١٢٣", "1_000", "9" * 65, "--1", "1.2.3", -1, -0.5, "-1", "-0.01", [], [1], {}, {"v": 1})
        for field in ("used", "limit", "remaining"):
            for value in bad:
                with self.subTest(field=field, value=value):
                    usage = usage_of(limit_body(**{field: value}))[0]
                    self.assertEqual((usage.error.code, usage.meters), ("malformed_response", ()), (field, value))

    def test_nonfinite_json_constants_are_malformed(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            raw = ('{"spend_control": {"individual_limit": {"source": "workspace_spend_controls", "unit": "credit", '
                   f'"used": {literal}, "limit": 10}}}}')
            adapter, _ = adapter_for(raw_response(raw))
            self.assertEqual(adapter.get_usage(30).error.code, "malformed_response", literal)
        adapter, _ = adapter_for(raw_response(
            '{"spend_control": {"individual_limit": {"source": "workspace_spend_controls", "unit": "credit", "used": 1e999}}}'))
        self.assertEqual(adapter.get_usage(30).error.code, "malformed_response")  # overflows to infinity
        huge = "1" + "0" * 400
        adapter, _ = adapter_for(raw_response(
            '{"spend_control": {"individual_limit": {"source": "workspace_spend_controls", "unit": "credit", '
            f'"used": {huge}}}}}'))
        self.assertEqual(adapter.get_usage(30).error.code, "malformed_response")

    def test_reset_at_forms(self):
        expect = (NOW + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for value in (FUTURE, float(FUTURE), str(FUTURE), f"{FUTURE}.0", f"{FUTURE}.000"):
            with self.subTest(value=value):
                (m,) = self.meter(reset_at=value).meters
                self.assertEqual(m.resets_at, expect)
        for absent in (None,):
            (m,) = self.meter(reset_at=absent).meters
            self.assertIsNone(m.resets_at)
        body = limit_body()
        del body["spend_control"]["individual_limit"]["reset_at"]
        (m,) = usage_of(body)[0].meters
        self.assertIsNone(m.resets_at)  # a missing reset is allowed: nothing is computed from reset_after_seconds

    def test_invalid_reset_at_is_malformed(self):
        bad = (True, False, -1, -1.0, 1.5, FUTURE + 0.5, "", "abc", "-1", "1.5", "1e9", " 1", "1796083201.5", "0x10", "٣",
               [], {}, [FUTURE], 10 ** 30, 1e30, "9" * 25, 253402300800, 253402300800.0, "253402300800", float("1e308"))
        for value in bad:
            with self.subTest(value=value):
                usage = usage_of(limit_body(reset_at=value))[0]
                self.assertEqual((usage.error.code, usage.meters), ("malformed_response", ()), value)
        for literal in ("NaN", "Infinity", "-Infinity"):
            raw = ('{"spend_control": {"individual_limit": {"source": "workspace_spend_controls", "unit": "credit", '
                   f'"used": 1, "reset_at": {literal}}}}}')
            adapter, _ = adapter_for(raw_response(raw))
            self.assertEqual(adapter.get_usage(30).error.code, "malformed_response", literal)

    def test_reset_in_the_past_suppresses_the_meter_instead_of_resetting_to_zero(self):
        for value in (NOW_EPOCH, NOW_EPOCH - 1, 0, str(NOW_EPOCH - 5000), float(NOW_EPOCH - 1)):
            with self.subTest(value=value):
                usage, adapter, _ = usage_of(limit_body(reset_at=value, used="1400"))
                self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error),
                                 ((), "opaque", "source_unavailable", None))
                self.assertEqual(adapter.source_label, "local_inventory")
        (m,) = self.meter(reset_at=NOW_EPOCH + 1).meters  # one second left is still current
        self.assertEqual(m.resets_at, "2026-11-06T12:00:01Z")

    def test_unsupported_shapes_are_opaque_not_errors(self):
        cases = {
            "no spend_control": {"plan_type": "business", "credits": {"balance": "9"}},
            "null spend_control": {"spend_control": None},
            "empty spend_control": {"spend_control": {}},
            "no individual_limit": {"spend_control": {"reached": False}},
            "null individual_limit": {"spend_control": {"individual_limit": None}},
            "other source": limit_body(source="plan_rate_limit"),
            "missing source": limit_body(source=None),
            "other unit": limit_body(unit="usd"),
            "count unit": limit_body(unit="count"),
            "missing unit": limit_body(unit=None),
            "case differs": limit_body(unit="Credit"),
            "rate-limit plan": {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 40}},
                                "spend_control": None},
            "empty object": {},
        }
        for name, body in cases.items():
            with self.subTest(name):
                usage, adapter, _ = usage_of(body)
                self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error),
                                 ((), "opaque", "source_unavailable", None), name)
                self.assertEqual(adapter.source_label, "local_inventory")

    def test_wrong_shapes_are_malformed(self):
        cases = {
            "spend_control list": {"spend_control": []}, "spend_control string": {"spend_control": "x"},
            "spend_control number": {"spend_control": 5}, "spend_control true": {"spend_control": True},
            "individual_limit list": {"spend_control": {"individual_limit": []}},
            "individual_limit string": {"spend_control": {"individual_limit": "x"}},
            "individual_limit number": {"spend_control": {"individual_limit": 0}},
            "individual_limit false": {"spend_control": {"individual_limit": False}},
        }
        for name, body in cases.items():
            with self.subTest(name):
                usage = usage_of(body)[0]
                self.assertEqual((usage.error.code, usage.meters, usage.confidence), ("malformed_response", (), "opaque"), name)
        for raw in ("[]", "null", "5", '"x"', "true", "not json", "", "{", b"\xff\xfe\xfd"):
            adapter, _ = adapter_for(raw_response(raw))
            self.assertEqual(adapter.get_usage(30).error.code, "malformed_response", raw)

    def test_unit_and_source_are_checked_before_the_numbers(self):
        usage = usage_of(limit_body(unit="usd", used="not a number"))[0]
        self.assertEqual((usage.error, usage.reason), (None, "source_unavailable"))  # unknown shape: numbers are not parsed


class HttpOutcomes(unittest.TestCase):
    def outcome(self, response):
        adapter, transport = adapter_for(response)
        usage = adapter.get_usage(30)
        self.assertEqual(len(transport.requests), 1)  # never an alternate route, never a retry
        self.assertEqual(transport.urls(), [CODEX_USAGE_URL])
        return usage

    def test_401_is_auth_required_with_permission_denied_reason_and_no_refresh(self):
        usage = self.outcome(json_response({"detail": "token_expired " + SENTINEL_TOKEN}, 401))
        self.assertEqual((usage.error.code, usage.error.retryable, usage.reason, usage.meters, usage.confidence),
                         ("auth_required", False, "permission_denied", (), "opaque"))

    def test_403_and_404_are_opaque_permission_denied(self):
        for status in (403, 404):
            usage = self.outcome(json_response({"detail": "nope"}, status))
            self.assertEqual((usage.error, usage.reason, usage.meters, usage.confidence), (None, "permission_denied", (), "opaque"))

    def test_throttling_and_server_errors_are_upstream_unavailable(self):
        for status in (429, 500, 502, 503, 599):
            usage = self.outcome(json_response({}, status))
            self.assertEqual((usage.error.code, usage.error.retryable), ("upstream_unavailable", True), status)

    def test_other_statuses_are_malformed(self):
        for status in (204, 400, 402, 405, 410, 418):
            # a 2xx without a JSON body and any unlisted 4xx are both "unexpected format"
            usage = self.outcome(json_response(limit_body(), status) if status != 204 else HttpResponse(204, b"", 1.0, None))
            self.assertEqual((usage.error.code, usage.meters), ("malformed_response", ()), status)

    def test_transport_failures_use_the_fixed_error_table(self):
        table = {TransportError.TIMEOUT: "timeout", TransportError.CONNECTION: "upstream_unavailable",
                 TransportError.TLS: "upstream_unavailable", TransportError.REDIRECT: "malformed_response",
                 TransportError.OVERSIZE: "malformed_response", TransportError.FORBIDDEN_URL: "internal_error"}
        for kind, code in table.items():
            usage = self.outcome(TransportError(kind))
            self.assertEqual((usage.error.code, usage.meters), (code, ()), kind)

    def test_errors_carry_no_upstream_text_headers_or_identity(self):
        for response in (json_response({"detail": ACCOUNT + EMAIL + SENTINEL_TOKEN}, 401),
                         json_response({"detail": ACCOUNT}, 500), raw_response(ACCOUNT + EMAIL, 200)):
            usage = self.outcome(response)
            blob = json.dumps(dto.to_plain(usage))
            self.assertEqual(scan_for(dto.to_plain(usage), [ACCOUNT, EMAIL, SENTINEL_TOKEN, "Bearer"]), [], blob)


class DtoSchema(unittest.TestCase):
    """The additive enum members, in every canonical validator copy."""

    def test_enums_gain_credit_and_the_wham_source_only(self):
        self.assertEqual(dto.UNITS, ("usd", "count", "percent", "bytes", "credit"))
        self.assertEqual(dto.METER_SOURCES, ("openrouter_key", "session_analytics", "prometheus", "anthropic_headers",
                                             "chatgpt_wham_usage", "none"))
        self.assertEqual(dto.SCHEMA_VERSION, 1)
        self.assertEqual(dto.AUTH_STATUSES, ("available", "missing"))  # no "rejected"
        self.assertTrue(dto.preserves_wire_value("unit", "credit"))
        self.assertTrue(dto.preserves_wire_value("source", "chatgpt_wham_usage"))
        self.assertFalse(dto.preserves_wire_value("unit", "credits"))
        self.assertTrue(dto.preserves_wire_value("label", "monthly_workspace", ("start", "end", "label")))

    def test_meter_round_trips_through_the_wire_check(self):
        (m,) = usage_of(limit_body())[0].meters
        wire = dto.to_plain(m)
        self.assertEqual(dto.to_plain(dto._meter_from_wire(wire)), wire)
        for field, value in (("unit", "usd2"), ("source", "wham"), ("unit", "credits")):
            bad = dict(wire, **{field: value})
            with self.assertRaises(DtoError):
                dto._meter_from_wire(bad)


# ----------------------------------------------------------------------------------- service level


class SeatRoutedTransport(FakeTransport):
    """Answers the Codex route per ChatGPT-Account-ID header, and records every request."""

    def __init__(self, bodies, status=200):
        super().__init__()
        self.bodies = bodies  # account id -> body (dict) or HttpResponse
        self.status = status

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        self.requests.append((url, dict(headers or {}), timeout))
        if url != CODEX_USAGE_URL:
            raise TransportError(TransportError.CONNECTION)
        body = self.bodies.get((headers or {}).get("ChatGPT-Account-ID"))
        if body is None:
            return json_response({"detail": "unknown seat"}, 404)
        return body if isinstance(body, HttpResponse) else json_response(body, self.status)


class CodexService(ServiceBase, SchemaAsserts):
    def seed(self, *rows, where="profile"):
        self.s.write_auth({"credential_pool": {"openai-codex": list(rows)}}, where=where)

    def cards(self, svc=None, view="profile"):
        status, body = (svc or self.make()).handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return body["data"]["cards"], body

    def codex(self, **kw):
        cards, _ = self.cards(**kw)
        return [c for c in cards if c["provider_id"] == "openai-codex"]

    def test_ready_card_shows_the_exact_credit_meter_and_keeps_presence_semantics(self):
        token = codex_token()
        self.seed({"id": "c1", "access_token": token, "refresh_token": "udsyn-refresh-not-for-sending"})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        (card,) = self.codex()
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"]), ("ready", "exact", None, None))
        self.assertEqual((card["auth_status"], card["plan_name"], card["plan_source"]), ("available", "business", "unverified_claim"))
        self.assertEqual((card["debug"]["source"], card["debug"]["adapter"]), ("chatgpt_wham_usage", "OpenAICodexAdapter"))
        self.assertIn("quota", card["debug"]["capabilities"])
        (m,) = card["meters"]
        self.assertEqual((m["id"], m["unit"], m["scope"], m["source"], m["confidence"]),
                         ("workspace_spend", "credit", "provider_account", "chatgpt_wham_usage", "exact"))
        self.assertEqual((m["value"], m["limit"], m["remaining"]), (864.5, 1500.0, 635.5))
        self.assertEqual(m["observed_at"], "2026-11-06T12:00:00Z")  # the snapshot's single clock reading
        self.assertEqual(m["period"], {"start": None, "end": None, "label": "monthly_workspace"})
        self.assertEqual(m["resets_at"], (NOW + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        check_wire("card", card)

    def test_limit_only_and_zero_cards_are_ready(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        for over, expected in ((dict(used=None, remaining=None), (None, 1500.0, None)),
                               (dict(used="0", remaining="1500"), (0.0, 1500.0, 1500.0)),
                               (dict(limit=None, used=None, remaining="12"), (None, None, 12.0))):
            with self.subTest(over=over):
                self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body(**over))
                (card,) = self.codex()
                self.assertEqual((card["state"], card["confidence"], card["reason"]), ("ready", "exact", None))
                self.assertEqual((card["meters"][0]["value"], card["meters"][0]["limit"], card["meters"][0]["remaining"]), expected)

    def test_unsupported_and_expired_cards_are_opaque_without_meters(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        for body in ({"spend_control": None}, limit_body(unit="usd"), limit_body(used=None, limit=None, remaining=None),
                     limit_body(reset_at=NOW_EPOCH)):
            self.transport.routes[CODEX_USAGE_URL] = json_response(body)
            (card,) = self.codex()
            self.assertEqual((card["state"], card["confidence"], card["reason"], card["meters"], card["error"]),
                             ("opaque", "opaque", "source_unavailable", [], None))
            self.assertEqual(card["debug"]["source"], "local_inventory")  # an allowlisted route is not a promotion

    def test_http_failures_map_to_card_states(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        cases = ((401, ("error", "opaque", "permission_denied", "auth_required", False)),
                 (403, ("opaque", "opaque", "permission_denied", None, None)),
                 (404, ("opaque", "opaque", "permission_denied", None, None)),
                 (429, ("error", "opaque", None, "upstream_unavailable", True)),
                 (503, ("error", "opaque", None, "upstream_unavailable", True)),
                 (400, ("error", "opaque", None, "malformed_response", False)))
        for status, (state, conf, reason, code, retry) in cases:
            with self.subTest(status=status):
                self.transport.routes[CODEX_USAGE_URL] = json_response({"detail": "x"}, status)
                (card,) = self.codex()
                self.assertEqual((card["state"], card["confidence"], card["reason"], card["meters"]), (state, conf, reason, []))
                self.assertEqual(card["error"] and (card["error"]["code"], card["error"]["retryable"]), code and (code, retry))
                self.assertEqual(card["auth_status"], "available")  # credential presence, never "rejected"
        for kind, code in ((TransportError.TIMEOUT, "timeout"), (TransportError.CONNECTION, "upstream_unavailable")):
            self.transport.routes[CODEX_USAGE_URL] = TransportError(kind)
            (card,) = self.codex()
            self.assertEqual((card["state"], card["error"]["code"]), ("error", code))

    def test_malformed_body_is_an_error_card_without_meters(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        for body in (limit_body(used="abc"), {"spend_control": []}, limit_body(reset_at=True)):
            self.transport.routes[CODEX_USAGE_URL] = json_response(body)
            (card,) = self.codex()
            self.assertEqual((card["state"], card["error"]["code"], card["meters"]), ("error", "malformed_response", []))

    def test_missing_credential_and_hostile_claims_make_no_request(self):
        self.seed({"id": "c1"}, {"id": "c2", "access_token": codex_token(account="bad id")},
                  {"id": "c3", "access_token": "udsyn-not-a-jwt"}, {"id": "c4", "access_token": codex_token(residency="bad\nvalue")})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        cards = self.codex()
        self.assertEqual(len(cards), 4)
        by_status = sorted((c["auth_status"], c["reason"]) for c in cards)
        self.assertEqual(by_status, [("available", "source_unavailable")] * 3 + [("missing", "credentials_missing")])
        self.assertTrue(all(c["state"] == "opaque" and c["meters"] == [] for c in cards))
        self.assertEqual(self.transport.requests, [])

    def test_one_request_per_snapshot_and_no_history(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        svc = self.make()
        for endpoint in ("subscriptions", "health", "subscriptions", "usage"):
            status, body = svc.handle(endpoint, [("view", "profile")] if endpoint != "usage" else [])
            self.assertEqual(status, 200, body)
        self.assertEqual(self.transport.urls(), [CODEX_USAGE_URL])  # cards + health share one snapshot; usage reads analytics only
        status, body = svc.handle("usage", [("provider", "openai-codex"), ("view", "profile")])
        self.assertEqual((status, body["data"]["series"], body["data"]["reason"]), (200, [], "attribution_unavailable"))
        self.assertEqual(len(self.transport.requests), 1)  # no history is manufactured from the spend control

    def test_no_refresh_and_credential_files_unchanged(self):
        token = codex_token()
        path = self.s.write_auth({"credential_pool": {"openai-codex": [
            {"id": "c1", "access_token": token, "refresh_token": "udsyn-refresh-1", "id_token": make_jwt({"x": 1})}]}})
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, sorted(p.name for p in self.s.home.iterdir()))
        for status in (200, 401, 403, 429):
            self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body(), status)
            svc = self.make()
            self.cards(svc)
        after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, sorted(p.name for p in self.s.home.iterdir()))
        self.assertEqual(before, after)
        for url, headers, _ in self.transport.requests:  # only the one route; neither refresh token nor id token is ever sent
            self.assertEqual(url, CODEX_USAGE_URL)
            self.assertTrue(set(headers) <= FIXED_HEADER_NAMES, headers)
            self.assertNotIn("udsyn-refresh-1", json.dumps(headers))
            self.assertFalse(any(v.startswith("Bearer") and v != "Bearer " + token for v in headers.values()))

    def test_cross_seat_requests_are_routed_by_their_own_token_only(self):
        seat_a = codex_token(account="udsyn-acct-A", user="udsyn-user-A", residency="udsyn-region-A", jti="a")
        seat_b = codex_token(account="udsyn-acct-A", user="udsyn-user-B", residency="udsyn-region-A", jti="b")  # same workspace
        seat_c = codex_token(account="udsyn-acct-C", user="udsyn-user-C", residency="udsyn-region-C", jti="c")
        self.seed({"id": "a", "access_token": seat_a}, {"id": "c", "access_token": seat_c}, {"id": "b", "access_token": seat_b})
        self.transport = SeatRoutedTransport({"udsyn-acct-A": limit_body(used="10"), "udsyn-acct-C": limit_body(used="30")})
        cards = self.codex()
        self.assertEqual(len(cards), 3)
        sent = {h["Authorization"]: (h["ChatGPT-Account-ID"], h["x-openai-internal-codex-residency"])
                for _, h, _ in self.transport.requests}
        self.assertEqual(sent, {"Bearer " + seat_a: ("udsyn-acct-A", "udsyn-region-A"),
                                "Bearer " + seat_b: ("udsyn-acct-A", "udsyn-region-A"),
                                "Bearer " + seat_c: ("udsyn-acct-C", "udsyn-region-C")})
        self.assertEqual(len(self.transport.requests), 3)  # one request per card, never merged or shared
        self.assertEqual(sorted(c["meters"][0]["value"] for c in cards), [10.0, 10.0, 30.0])  # no seat aggregation

    def test_profile_view_uses_only_the_selected_profiles_credential(self):
        global_token = codex_token(account="udsyn-acct-GLOBAL", jti="g")
        profile_token = codex_token(account="udsyn-acct-PROFILE", jti="p")
        other_token = codex_token(account="udsyn-acct-OTHER", jti="o")
        self.seed({"id": "g", "access_token": global_token}, where="global")
        self.s.write_profile("other", {"credential_pool": {"openai-codex": [{"id": "o", "access_token": other_token}]}})
        self.transport = SeatRoutedTransport({a: limit_body() for a in ("udsyn-acct-GLOBAL", "udsyn-acct-PROFILE", "udsyn-acct-OTHER")})
        # 'work' has no codex row: the global pool is the effective one for this profile
        (card,) = self.codex()
        self.assertEqual([h["ChatGPT-Account-ID"] for _, h, _ in self.transport.requests], ["udsyn-acct-GLOBAL"])
        # now the profile gets its own pool: it shadows the global one, which must not be used at all
        self.seed({"id": "p", "access_token": profile_token})
        self.transport.requests.clear()
        (card,) = self.codex(svc=self.make())
        self.assertEqual([h["ChatGPT-Account-ID"] for _, h, _ in self.transport.requests], ["udsyn-acct-PROFILE"])
        self.assertEqual(self.transport.requests[0][1]["Authorization"], "Bearer " + profile_token)

    def test_machine_view_sends_each_merged_account_once_with_its_own_credential(self):
        seat_a = codex_token(account="udsyn-acct-A", user="u1", jti="1")
        seat_a_again = codex_token(account="udsyn-acct-A", user="u1", jti="2")  # refreshed token of the same seat
        seat_b = codex_token(account="udsyn-acct-A", user="u2", jti="3")
        self.seed({"id": "g", "access_token": seat_a}, where="global")
        self.s.write_profile("architect", {"credential_pool": {"openai-codex": [{"id": "x", "access_token": seat_a_again}]}})
        self.s.write_profile("verifier", {"credential_pool": {"openai-codex": [{"id": "y", "access_token": seat_b}]}})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        cards = self.codex(view="machine")
        self.assertEqual(len(cards), 2)  # two seats of one workspace stay two accounts
        self.assertEqual(len(self.transport.requests), 2)
        bearers = {h["Authorization"] for _, h, _ in self.transport.requests}
        self.assertEqual(len(bearers), 2)
        self.assertTrue(all(b in ("Bearer " + seat_a, "Bearer " + seat_a_again, "Bearer " + seat_b) for b in bearers))

    def test_other_providers_never_reach_the_codex_route(self):
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or", "access_token": "udsyn-or-key"}],
            "nous": [{"id": "n", "access_token": codex_token(), "agent_key": "udsyn-agent"}],
            "anthropic": [{"id": "an", "access_token": codex_token(account="udsyn-acct-ANT")}],
            "mystery-cloud": [{"id": "m", "access_token": codex_token(account="udsyn-acct-MYS")}]}})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        self.transport.routes[OPENROUTER_KEY_URL] = json_response({"data": {"usage": 1, "limit": 2}})
        cards, _ = self.cards()
        self.assertEqual(self.transport.urls(), [OPENROUTER_KEY_URL])  # nothing else spoke to the network
        self.assertTrue(all(c["meters"] == [] for c in cards if c["provider_id"] != "openrouter"))

    def test_the_service_grants_the_codex_target_only_to_codex_contexts(self):
        svc = self.make()
        src = FileInventorySource(self.s.home, self.s.root)
        for provider, expect_allowed in (("openai-codex", True), ("OpenAI-Codex", True), ("openrouter", False),
                                         ("nous", False), ("anthropic", False), ("custom:gpu", False), ("mystery", False)):
            self.s.write_auth({"credential_pool": {provider: [{"id": "r", "access_token": codex_token()}]}})
            (item,) = [i for i in discover(src) if i.provider_id == provider]
            ctx = svc._context(item, None, NOW)
            inner = FakeTransport({CODEX_USAGE_URL: json_response({})})
            ctx.transport._inner = inner  # same guard and allowlist, observable inner transport
            try:
                ctx.transport.get(CODEX_USAGE_URL)
                allowed = True
            except TransportError:
                allowed = False
            self.assertEqual(allowed, expect_allowed, provider)

    def test_secrets_and_upstream_identity_never_reach_dtos_logs_or_caches(self):
        token = codex_token()
        refresh = "udsyn-refresh-sentinel-77"
        self.seed({"id": "c1", "access_token": token, "refresh_token": refresh})
        body = limit_body()
        body["credits"] = {"balance": "123.456789"}
        self.transport.routes[CODEX_USAGE_URL] = json_response(body)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        svc = self.make()
        cards, envelope = self.cards(svc)
        status, health = svc.handle("health", [])
        needles = [token, token.split(".")[1], token.split(".")[2], ACCOUNT, USER, EMAIL, RESIDENCY, refresh, "Bearer", "123.456789"]
        for name, obj in (("subscriptions", envelope), ("health", health), ("cache", repr(svc._cache)),
                          ("logs", [r.getMessage() + repr(r.args) for r in records])):
            self.assertEqual(scan_for(obj, needles), [], name)
        self.assertNotIn("balance", json.dumps(envelope))  # no credits-balance meter

    def test_a_short_residency_does_not_corrupt_the_plan_claim(self):
        self.seed({"id": "c1", "access_token": codex_token(residency="us", plan="business")})
        self.transport.routes[CODEX_USAGE_URL] = json_response(limit_body())
        (card,) = self.codex()
        self.assertEqual((card["plan_name"], card["state"]), ("business", "ready"))

    def test_routing_values_join_the_masking_context(self):
        self.seed({"id": "c1", "access_token": codex_token()})
        (item,) = discover(FileInventorySource(self.s.home, self.s.root))
        self.assertTrue({ACCOUNT, RESIDENCY} <= item.secrets)
        machine = discover_machine(FileInventorySource(self.s.home, self.s.root))
        self.assertTrue({ACCOUNT, RESIDENCY} <= machine[0].secrets)
        # other providers' tokens never get Codex routing claims added to their masking context
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n", "access_token": codex_token()}]}})
        (nous,) = discover(FileInventorySource(self.s.home, self.s.root))
        self.assertNotIn(ACCOUNT, nous.secrets)


if __name__ == "__main__":
    unittest.main()
