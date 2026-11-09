"""Anthropic meters as a LIVE pull (DESIGN.md §9.1.2, card t_1cc46bf4).

``GET https://api.anthropic.com/api/oauth/usage`` with the persisted OAuth access token is the primary source;
captured unified headers (§9.1) are the fallback. Bodies below are synthetic but follow the shape recorded from
the real endpoint on 2026-11-09 (``five_hour`` / ``seven_day`` windows, ``utilization`` already in PERCENT,
``resets_at`` ISO-8601 with offset). Tokens are invented; the transport is injected, nothing leaves the box.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugins.usage_deck.adapters import create_adapter
from plugins.usage_deck.adapters.cloud import (
    ANTHROPIC_OAUTH_BETA,
    ANTHROPIC_TARGET,
    ANTHROPIC_USAGE_URL,
    AnthropicAdapter,
    anthropic_usage_meters,
)
from plugins.usage_deck.adapters.transport import (
    CrossProfileProbeTransport,
    GuardedTransport,
    TransportError,
    probe_allowed,
)
from plugins.usage_deck.captures import CaptureStore, default_store_path
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine

from .helpers import NOW, FakeTransport, PassthroughResolver, json_response, make_ctx, raw_response, scan_for
from .test_anthropic_meters import FIXTURE, refs, store
from .test_cross_profile_meters import tree
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

TOKEN = "sk-ant-oat01-udsyn-live-token-A"
OTHER_TOKEN = "sk-ant-oat01-udsyn-live-token-B"
ROW = "ant-syn-fresh"  # a row id the synthetic capture fixture carries (holder work, observed 11:58:30Z)


def iso(moment):
    return moment.isoformat().replace("+00:00", "+00:00")


FIVE_RESET = NOW + timedelta(hours=3, microseconds=439963)
WEEK_RESET = NOW + timedelta(days=4)


def window(utilization, reset):
    return {"utilization": utilization, "resets_at": None if reset is None else iso(reset),
            "limit_dollars": None, "used_dollars": None, "remaining_dollars": None, "locked_reason": None}


def usage_body(five=(42.5, FIVE_RESET), week=(7.0, WEEK_RESET), **extra):
    body = {"five_hour": None if five is None else window(*five),
            "seven_day": None if week is None else window(*week),
            "seven_day_opus": None, "extra_usage": {"is_enabled": True, "monthly_limit": 0, "used_credits": 0.0},
            "limits": [{"kind": "session", "percent": 99, "resets_at": iso(FIVE_RESET)}]}
    body.update(extra)
    return body


def shape(meter):
    return (meter.id, meter.label, meter.value, meter.limit, meter.remaining, meter.unit, meter.scope,
            meter.period.start, meter.period.end, meter.period.label, meter.confidence, meter.source,
            meter.observed_at, meter.resets_at)


SESSION_LIVE = ("session_5h", "Session (5-hour window)", 42.5, 100.0, None, "percent", "provider_account",
                None, None, "rolling_5h", "exact", "anthropic_oauth_usage", "2026-11-06T12:00:00Z", "2026-11-06T15:00:00Z")
WEEKLY_LIVE = ("weekly_7d", "Weekly (7-day window)", 7.0, 100.0, None, "percent", "provider_account",
               None, None, "rolling_7d", "exact", "anthropic_oauth_usage", "2026-11-06T12:00:00Z", "2026-11-10T12:00:00Z")


def live_ctx(transport, captures=None, row=ROW, token=TOKEN, **kw):
    kw.setdefault("oauth_credential", True)
    kw.setdefault("token_resolver", PassthroughResolver())
    return make_ctx("anthropic", credential=token, transport=transport, captures=captures,
                    credential_refs=tuple(refs(row)) if captures is not None else (), **kw)


def adapter(transport, captures=None, **kw):
    ctx = live_ctx(transport, captures, **kw)
    return create_adapter(ctx)


def routed(outcome):
    return FakeTransport({ANTHROPIC_USAGE_URL: outcome})


class LiveMeterMapping(unittest.TestCase):
    def test_active_windows_map_to_exact_meters_session_first(self):
        transport = routed(json_response(usage_body()))
        a = adapter(transport)
        self.assertIsInstance(a, AnthropicAdapter)
        usage = a.get_usage(30)
        self.assertEqual((usage.confidence, usage.reason, usage.error), ("exact", None, None))
        self.assertEqual([shape(m) for m in usage.meters], [SESSION_LIVE, WEEKLY_LIVE])
        self.assertEqual((a.get_confidence(), a.source_label, a.credential_rejected()), ("exact", "anthropic_oauth_usage", False))
        probe = a.probe()
        self.assertTrue(probe.supported)
        self.assertIn("quota", probe.capabilities)

    def test_value_is_the_reported_percent_not_a_fraction(self):
        # The header capture reports a fraction (0.07); this endpoint reports percent (7.0). Never rescaled.
        (meter,) = anthropic_usage_meters({"seven_day": window(7.0, WEEK_RESET)}, NOW)
        self.assertEqual((meter.value, meter.limit), (7.0, 100.0))
        (meter,) = anthropic_usage_meters({"five_hour": window(1, FIVE_RESET)}, NOW)
        self.assertEqual(meter.value, 1.0)

    def test_zero_usage_fresh_window_is_a_reading_not_a_hole(self):
        transport = routed(json_response(usage_body(five=(0.0, FIVE_RESET), week=(0, WEEK_RESET))))
        usage = adapter(transport).get_usage(30)
        self.assertEqual([(m.id, m.value) for m in usage.meters], [("session_5h", 0.0), ("weekly_7d", 0.0)])
        self.assertEqual(usage.confidence, "exact")

    def test_observed_at_is_the_poll_time_and_resets_at_the_providers_instant(self):
        usage = adapter(routed(json_response(usage_body()))).get_usage(30)
        for meter in usage.meters:
            self.assertEqual(meter.observed_at, "2026-11-06T12:00:00Z")
        self.assertEqual([m.resets_at for m in usage.meters], ["2026-11-06T15:00:00Z", "2026-11-10T12:00:00Z"])

    def test_over_limit_is_kept_not_clamped(self):
        usage = adapter(routed(json_response(usage_body(five=(107.25, FIVE_RESET))))).get_usage(30)
        self.assertEqual(usage.meters[0].value, 107.25)

    def test_reset_offsets_z_suffix_and_naive_are_all_read_as_instants(self):
        for text in ("2026-11-06T15:00:00Z", "2026-11-06T17:00:00+02:00", "2026-11-06T15:00:00",
                     "2026-11-06T15:00:00.439963+00:00"):
            with self.subTest(text=text):
                body = {"five_hour": {"utilization": 3.0, "resets_at": text}}
                (meter,) = anthropic_usage_meters(body, NOW)
                self.assertEqual(meter.resets_at, "2026-11-06T15:00:00Z")

    def test_null_window_or_null_utilization_yields_no_meter_never_a_zero(self):
        self.assertEqual([m.id for m in anthropic_usage_meters(usage_body(five=None), NOW)], ["weekly_7d"])
        self.assertEqual([m.id for m in anthropic_usage_meters(usage_body(week=None), NOW)], ["session_5h"])
        body = usage_body(five=(None, FIVE_RESET))
        self.assertEqual([m.id for m in anthropic_usage_meters(body, NOW)], ["weekly_7d"])
        self.assertEqual(anthropic_usage_meters(usage_body(five=None, week=None), NOW), [])

    def test_window_without_reset_keeps_its_meter_with_null_resets_at(self):
        (meter,) = anthropic_usage_meters(usage_body(five=(12.0, None), week=None), NOW)
        self.assertEqual((meter.id, meter.value, meter.resets_at), ("session_5h", 12.0, None))

    def test_week_is_accepted_as_an_alias_of_seven_day(self):
        (meter,) = anthropic_usage_meters({"week": window(9.5, WEEK_RESET)}, NOW)
        self.assertEqual((meter.id, meter.value), ("weekly_7d", 9.5))

    def test_every_other_field_of_the_body_is_ignored(self):
        body = usage_body(extra_usage={"used_credits": 99.0}, spend={"percent": 77}, pace="x", indicator="y")
        self.assertEqual([m.id for m in anthropic_usage_meters(body, NOW)], ["session_5h", "weekly_7d"])

    def test_invalid_present_fields_raise(self):
        bad_windows = ["x", 5, [1], True]
        bad_values = ["12", True, -1.0, float("inf"), [], {}]
        for bad in bad_windows:
            with self.subTest(window=bad), self.assertRaises(ValueError):
                anthropic_usage_meters({"five_hour": bad}, NOW)
        for bad in bad_values:
            with self.subTest(utilization=bad), self.assertRaises(ValueError):
                anthropic_usage_meters({"five_hour": {"utilization": bad, "resets_at": iso(FIVE_RESET)}}, NOW)
        for bad in (123, "tomorrow", "", True, "2026-13-45T00:00:00Z"):
            with self.subTest(resets_at=bad), self.assertRaises(ValueError):
                anthropic_usage_meters({"five_hour": {"utilization": 1.0, "resets_at": bad}}, NOW)


class ResetJustPassed(unittest.TestCase):
    def test_window_whose_reset_is_at_or_before_now_is_hidden_not_zero(self):
        for delta in (timedelta(seconds=-1), timedelta(0), timedelta(minutes=-30)):
            with self.subTest(delta=delta):
                transport = routed(json_response(usage_body(five=(88.0, NOW + delta))))
                usage = adapter(transport).get_usage(30)
                self.assertEqual([m.id for m in usage.meters], ["weekly_7d"])

    def test_one_second_before_the_reset_the_window_is_still_current(self):
        usage = adapter(routed(json_response(usage_body(five=(88.0, NOW + timedelta(seconds=1)))))).get_usage(30)
        self.assertEqual([m.id for m in usage.meters], ["session_5h", "weekly_7d"])

    def test_nothing_current_is_opaque_source_unavailable_and_never_overridden_by_an_older_capture(self):
        body = usage_body(five=(88.0, NOW - timedelta(seconds=1)), week=(5.0, NOW - timedelta(hours=1)))
        a = adapter(routed(json_response(body)), store(), row=ROW)  # a fresh capture exists...
        usage = a.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error), ((), "opaque", "source_unavailable", None))
        self.assertFalse(a.credential_rejected())  # ...but the live answer is definitive: no stale reading in its place


class Request(unittest.TestCase):
    def test_exactly_one_guarded_get_with_the_fixed_header_set(self):
        transport = routed(json_response(usage_body()))
        a = adapter(transport)
        a.probe()
        for window_days in (7, 30, 90):  # every window request reuses the one snapshot
            a.get_usage(window_days)
        a.get_confidence()
        self.assertEqual(transport.urls(), ["https://api.anthropic.com/api/oauth/usage"])
        (_url, headers, _timeout) = transport.requests[0]
        self.assertEqual(sorted(headers), ["Accept", "Authorization", "User-Agent", "anthropic-beta"])
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(ANTHROPIC_OAUTH_BETA, "oauth-2025-04-20")

    def test_probe_alone_makes_no_request(self):
        transport = routed(json_response(usage_body()))
        a = adapter(transport)
        a.probe()
        self.assertEqual(transport.requests, [])

    def test_no_request_without_an_oauth_credential_a_live_source_or_a_credential(self):
        cases = {"api key (not oauth)": dict(oauth_credential=False),
                 "machine-only inventory": dict(live_source=False),
                 "no credential": dict(token=None)}
        for name, kw in cases.items():
            with self.subTest(name):
                transport = routed(json_response(usage_body()))
                a = adapter(transport, **kw)
                usage = a.get_usage(30)
                self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "opaque", "unsupported"))
                self.assertFalse(a.probe().supported)
                self.assertEqual((transport.requests, a.source_label, a.credential_rejected()), ([], "local_inventory", False))

    def test_headers_only_card_without_oauth_credential_still_uses_the_capture(self):
        transport = routed(json_response(usage_body()))
        a = adapter(transport, store(), oauth_credential=False)
        usage = a.get_usage(30)
        self.assertEqual([m.source for m in usage.meters], ["anthropic_headers", "anthropic_headers"])
        self.assertEqual(transport.requests, [])


class AuthRejected(unittest.TestCase):
    def test_401_is_auth_required_and_credential_rejected_with_a_single_request(self):
        transport = routed(raw_response("{}", status=401))
        a = adapter(transport, store())  # a fresh capture exists: still no fallback over the credential's verdict
        usage = a.get_usage(30)
        a.get_usage(7)
        self.assertEqual((usage.meters, usage.error.code, usage.reason), ((), "auth_required", "permission_denied"))
        self.assertTrue(a.credential_rejected())
        self.assertEqual(len(transport.requests), 1)  # never a retry, never a refresh, never another route

    def test_a_credential_that_was_never_sent_is_never_rejected(self):
        a = adapter(routed(raw_response("{}", status=401)), oauth_credential=False)
        a.get_usage(30)
        self.assertFalse(a.credential_rejected())


# Endpoint failures that allow the header fallback: (name, outcome)
UNAVAILABLE = (
    ("timeout", TransportError(TransportError.TIMEOUT), "timeout"),
    ("connection", TransportError(TransportError.CONNECTION), "upstream_unavailable"),
    ("tls", TransportError(TransportError.TLS), "upstream_unavailable"),
    ("redirect", TransportError(TransportError.REDIRECT), "malformed_response"),
    ("oversize", TransportError(TransportError.OVERSIZE), "malformed_response"),
    ("429", raw_response("{}", status=429), "upstream_unavailable"),
    ("500", raw_response("{}", status=500), "upstream_unavailable"),
    ("503", raw_response("{}", status=503), "upstream_unavailable"),
    ("418", raw_response("{}", status=418), "malformed_response"),
    ("not json", raw_response("<html>", status=200), "malformed_response"),
    ("not an object", json_response([1, 2]), "malformed_response"),
    ("wrong shape", json_response({"five_hour": "x"}), "malformed_response"),
    ("bad utilization", json_response({"five_hour": {"utilization": "7", "resets_at": iso(FIVE_RESET)}}), "malformed_response"),
)


class FallbackMatrix(unittest.TestCase):
    """endpoint unavailable x capture state -> exact meters or an honest error; never an estimate."""

    def test_unavailable_endpoint_with_a_fresh_capture_falls_back_to_the_capture_meters(self):
        for name, outcome, _code in UNAVAILABLE:
            with self.subTest(name):
                a = adapter(routed(outcome), store())
                usage = a.get_usage(30)
                self.assertEqual((usage.confidence, usage.reason, usage.error), ("exact", None, None))
                self.assertEqual([(m.id, m.source, m.observed_at) for m in usage.meters],
                                 [("session_5h", "anthropic_headers", "2026-11-06T11:58:30Z"),
                                  ("weekly_7d", "anthropic_headers", "2026-11-06T11:58:30Z")])  # capture time, not poll time
                self.assertEqual((a.source_label, a.credential_rejected()), ("anthropic_headers", False))

    def test_403_and_404_also_fall_back_and_say_permission_denied_without_a_capture(self):
        for status in (403, 404):
            with self.subTest(status=status):
                fallback = adapter(routed(raw_response("{}", status=status)), store()).get_usage(30)
                self.assertEqual([m.source for m in fallback.meters], ["anthropic_headers"] * 2)
                bare = adapter(routed(raw_response("{}", status=status))).get_usage(30)
                self.assertEqual((bare.meters, bare.reason), ((), "permission_denied"))
                if status == 403:  # no profile scope: a quiet opaque card
                    self.assertIsNone(bare.error)
                else:  # 404 is a credential/endpoint ERROR (§9.1.2)
                    self.assertEqual(bare.error.code, "permission_denied")

    def test_unavailable_endpoint_and_no_capture_is_an_error_with_no_meters(self):
        for name, outcome, code in UNAVAILABLE:
            with self.subTest(name):
                a = adapter(routed(outcome))
                usage = a.get_usage(30)
                self.assertEqual((usage.meters, usage.error.code), ((), code))
                self.assertEqual(a.source_label, "local_inventory")

    def test_unavailable_endpoint_and_a_stale_capture_hides_every_window(self):
        for name, outcome, code in UNAVAILABLE:
            with self.subTest(name):
                a = adapter(routed(outcome), store(), row="ant-syn-both-rolled")
                usage = a.get_usage(30)
                self.assertEqual((usage.meters, usage.error.code), ((), code))  # never an estimate, never a zero

    def test_a_partly_stale_capture_contributes_only_its_current_window(self):
        a = adapter(routed(TransportError(TransportError.TIMEOUT)), store(), row="ant-syn-session-rolled")
        usage = a.get_usage(30)
        self.assertEqual([(m.id, m.source) for m in usage.meters], [("weekly_7d", "anthropic_headers")])

    def test_live_meters_always_beat_a_fresher_looking_capture(self):
        a = adapter(routed(json_response(usage_body())), store())
        self.assertEqual([m.source for m in a.get_usage(30).meters], ["anthropic_oauth_usage"] * 2)

    def test_sources_are_never_mixed_inside_one_card(self):
        # The endpoint answers with the weekly window only: the 5-hour meter is NOT borrowed from the capture.
        a = adapter(routed(json_response(usage_body(five=None))), store())
        self.assertEqual([(m.id, m.source) for m in a.get_usage(30).meters], [("weekly_7d", "anthropic_oauth_usage")])


class EgressAllowlist(unittest.TestCase):
    ORIGIN = "https://api.anthropic.com"

    def guard(self):
        inner = FakeTransport()
        return GuardedTransport(inner, [ANTHROPIC_TARGET]), inner

    def test_target_is_exactly_the_one_get_route(self):
        self.assertEqual((ANTHROPIC_TARGET.scheme, ANTHROPIC_TARGET.host, ANTHROPIC_TARGET.port, ANTHROPIC_TARGET.paths),
                         ("https", "api.anthropic.com", 443, ("/api/oauth/usage",)))
        self.assertEqual(ANTHROPIC_USAGE_URL, self.ORIGIN + "/api/oauth/usage")

    def test_only_the_usage_path_egresses(self):
        guard, inner = self.guard()
        inner.routes[ANTHROPIC_USAGE_URL] = inner.routes[self.ORIGIN + ":443/api/oauth/usage"] = json_response({})
        guard.get(ANTHROPIC_USAGE_URL)
        guard.get(self.ORIGIN + ":443/api/oauth/usage")
        self.assertEqual(len(inner.requests), 2)

    def test_no_other_anthropic_path_origin_scheme_or_decoration_egresses(self):
        guard, inner = self.guard()
        for url in (
            self.ORIGIN + "/v1/messages",
            self.ORIGIN + "/v1/models",
            self.ORIGIN + "/v1/organizations/usage_report/messages",
            self.ORIGIN + "/api/oauth/profile",
            self.ORIGIN + "/api/oauth/usage/",
            self.ORIGIN + "/api/oauth/usage/extra",
            self.ORIGIN + "/api/oauth/usage?beta=1",
            self.ORIGIN + "/api/oauth/usage?",
            self.ORIGIN + "/api/oauth/usage#frag",
            self.ORIGIN + "/api/oauth/Usage",
            self.ORIGIN + "/",
            self.ORIGIN,
            "http://api.anthropic.com/api/oauth/usage",
            "https://api.anthropic.com:8443/api/oauth/usage",
            "https://console.anthropic.com/api/oauth/usage",
            "https://claude.ai/api/oauth/usage",
            "https://anthropic.com/api/oauth/usage",
            "https://api.anthropic.com.evil.example/api/oauth/usage",
            "https://evil@api.anthropic.com/api/oauth/usage",
            "https://chatgpt.com/backend-api/wham/usage",
        ):
            with self.subTest(url=url), self.assertRaises(TransportError) as ctx:
                guard.get(url)
            self.assertEqual(ctx.exception.kind, TransportError.FORBIDDEN_URL)
        self.assertEqual(inner.requests, [])

    def test_cross_profile_ceiling_has_the_get_only_anthropic_entry_and_refuses_everything_else(self):
        self.assertTrue(probe_allowed("GET", ANTHROPIC_USAGE_URL))
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            self.assertFalse(probe_allowed(method, ANTHROPIC_USAGE_URL), method)
        for path in ("/v1/messages", "/api/oauth/profile", "/api/oauth/usage/", "/api/oauth/usage?x=1"):
            self.assertFalse(probe_allowed("GET", self.ORIGIN + path), path)
        inner = FakeTransport({ANTHROPIC_USAGE_URL: json_response({})})
        cross = CrossProfileProbeTransport(GuardedTransport(inner, [ANTHROPIC_TARGET]))
        for method in ("POST", "PUT", "DELETE"):
            with self.assertRaises(TransportError):
                cross.request(method, ANTHROPIC_USAGE_URL)
        with self.assertRaises(TransportError):
            cross.get(self.ORIGIN + "/v1/messages")
        self.assertEqual(inner.requests, [])
        cross.get(ANTHROPIC_USAGE_URL)
        self.assertEqual(inner.urls(), [ANTHROPIC_USAGE_URL])


# --------------------------------------------------------------------- through the service

def oauth_row(row_id, token=TOKEN, **extra):
    row = {"id": row_id, "auth_type": "oauth", "source": "manual:hermes_pkce", "access_token": token,
           "refresh_token": "sntl-ref-anthropic-1"}
    row.update(extra)
    return row


def pool(*rows):
    return {"credential_pool": {"anthropic": list(rows)}}


class LiveCardThroughService(ServiceBase, SchemaAsserts):
    def setUp(self):
        super().setUp()
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body())

    def cards(self, view="machine", svc=None):
        status, body = (svc or self.make()).handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return body["data"]["cards"]

    def seed_capture(self, rows=(ROW,)):
        target = default_store_path(self.s.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(FIXTURE.read_text())
        data["synthetic"] = False
        data.pop("_note", None)
        target.write_text(json.dumps(data))

    def test_card_is_ready_exact_with_the_two_live_meters_in_both_views(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        for view in ("machine", "profile"):
            self.transport.requests.clear()
            (card,) = self.cards(view)
            self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"]), ("ready", "exact", None, None), view)
            self.assertEqual([m["id"] for m in card["meters"]], ["session_5h", "weekly_7d"])
            self.assertEqual([m["source"] for m in card["meters"]], ["anthropic_oauth_usage"] * 2)
            self.assertEqual(card["debug"]["source"], "anthropic_oauth_usage")
            self.assertEqual((card["meters"][0]["value"], card["meters"][0]["observed_at"]), (42.5, "2026-11-06T12:00:00Z"))
            self.assertEqual(card["auth_status"], "available")
            self.assertEqual(self.transport.urls(), [ANTHROPIC_USAGE_URL], view)
            self.assertEqual(self.transport.requests[0][1]["Authorization"], "Bearer " + TOKEN)

    def test_fresh_zero_percent_window_during_idle_renders_the_zero(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body(five=(0.0, FIVE_RESET)))
        (card,) = self.cards()
        self.assertEqual((card["state"], [(m["id"], m["value"]) for m in card["meters"]]),
                         ("ready", [("session_5h", 0.0), ("weekly_7d", 7.0)]))

    def test_live_read_per_snapshot_and_no_request_before_a_subscriptions_call(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        svc = self.make()
        self.assertEqual(self.transport.requests, [])  # constructing the service starts nothing: no timer, no poll
        self.cards(svc=svc)
        self.cards(svc=svc)  # inside the 30 s snapshot TTL: the existing UI cadence coalesces, no extra request
        self.assertEqual(len(self.transport.requests), 1)
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body(five=(60.0, FIVE_RESET)))
        later = self.make(cache_ttl=0.0)  # a Refresh after the TTL reads live again
        self.assertEqual(self.cards(svc=later)[0]["meters"][0]["value"], 60.0)

    def test_401_marks_the_card_rejected_with_an_auth_error_and_no_meters(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        self.seed_capture()
        self.transport.routes[ANTHROPIC_USAGE_URL] = raw_response("{}", status=401)
        (card,) = self.cards()
        self.assertEqual((card["state"], card["auth_status"], card["meters"]), ("error", "rejected", []))
        self.assertEqual(card["error"]["code"], "auth_required")
        self.assertEqual(len(self.transport.requests), 1)

    def test_unreachable_endpoint_falls_back_to_the_fresh_capture_with_the_capture_time(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        self.seed_capture()
        self.transport.routes[ANTHROPIC_USAGE_URL] = TransportError(TransportError.CONNECTION)
        (card,) = self.cards()
        self.assertEqual((card["state"], card["confidence"], card["error"], card["auth_status"]),
                         ("ready", "exact", None, "available"))
        self.assertEqual([(m["source"], m["observed_at"]) for m in card["meters"]],
                         [("anthropic_headers", "2026-11-06T11:58:30Z")] * 2)
        self.assertEqual(card["debug"]["source"], "anthropic_headers")

    def test_unreachable_endpoint_without_a_current_capture_is_an_error_card_without_meters(self):
        self.s.write_auth(pool(oauth_row("no-capture-row")))
        self.seed_capture()
        self.transport.routes[ANTHROPIC_USAGE_URL] = TransportError(TransportError.TIMEOUT)
        (card,) = self.cards()
        self.assertEqual((card["state"], card["meters"], card["error"]["code"]), ("error", [], "timeout"))

    def test_api_key_rows_and_rows_without_auth_type_make_no_request(self):
        for row in ({"id": ROW, "auth_type": "api_key", "access_token": TOKEN},
                    {"id": ROW, "access_token": TOKEN}):
            with self.subTest(row=sorted(row)):
                self.transport.requests.clear()
                self.s.write_auth(pool(row))
                (card,) = self.cards()
                self.assertEqual((card["state"], card["reason"], card["meters"]), ("opaque", "unsupported", []))
                self.assertEqual(self.transport.requests, [])

    def test_only_the_usage_route_is_ever_requested_and_the_token_never_leaks(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        before = tree(self.s.root)
        status, body = self.make().handle("subscriptions", [("view", "machine")])
        self.assertEqual(self.transport.urls(), [ANTHROPIC_USAGE_URL])
        self.assertEqual(scan_for(body, (TOKEN, "sntl-ref-anthropic-1", "Bearer", "oauth-2025-04-20")), [])
        self.assertEqual(tree(self.s.root), before)  # no sink file, cache file or credential write

    def test_a_malicious_body_cannot_redirect_the_request_or_leak_through_the_card(self):
        self.s.write_auth(pool(oauth_row(ROW)))
        evil = usage_body(next_url="https://evil.example/steal", error={"message": TOKEN})
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(evil)
        body = self.make().handle("subscriptions", [("view", "machine")])[1]
        self.assertEqual(self.transport.urls(), [ANTHROPIC_USAGE_URL])
        self.assertEqual(scan_for(body, (TOKEN, "evil.example")), [])


class CrossProfileLive(ServiceBase, SchemaAsserts):
    """DESIGN.md §13 + §9.1.2: the machine view polls a sibling-held OAuth row; nothing else does."""

    def setUp(self):
        super().setUp()
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body())
        self.s.write_auth({"credential_pool": {}})  # 'work' and the root hold no Anthropic row

    def cards(self, view):
        status, body = self.make().handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]

    def test_sibling_held_oauth_row_is_polled_in_the_machine_view_with_its_own_token(self):
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN)))
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["confidence"]), ("ready", "exact"))
        self.assertEqual([m["source"] for m in card["meters"]], ["anthropic_oauth_usage"] * 2)
        self.assertEqual([(h["profile"], h["is_global"]) for h in card["holders"]], [("verifier", False)])
        self.assertEqual(self.transport.urls(), [ANTHROPIC_USAGE_URL])
        self.assertEqual(self.transport.requests[0][1]["Authorization"], "Bearer " + OTHER_TOKEN)

    def test_the_profile_view_never_spends_a_sibling_credential(self):
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN)))
        self.assertEqual(self.cards("profile"), [])
        self.assertEqual(self.transport.requests, [])

    def test_a_restricted_principal_is_clamped_to_the_profile_view_and_polls_nothing_foreign(self):
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN)))
        status, body = self.make().handle("subscriptions", [("view", "machine")], profile_only=True)
        self.assertEqual(body["data"]["cards"], [])
        self.assertEqual(self.transport.requests, [])

    def test_sibling_held_api_key_stays_inventory_with_no_request(self):
        self.s.write_profile("verifier", pool({"id": "v1", "auth_type": "api_key", "access_token": OTHER_TOKEN}))
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["meters"]), ("opaque", []))
        self.assertEqual(self.transport.requests, [])

    def test_sibling_401_is_rejected_on_the_card(self):
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN)))
        self.transport.routes[ANTHROPIC_USAGE_URL] = raw_response("{}", status=401)
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["auth_status"], card["error"]["code"]), ("error", "rejected", "auth_required"))

    def test_latest_expiring_holder_of_a_seat_is_discovered_first(self):
        # Two rows of ONE merged card cannot occur for opaque tokens (ids differ), so check the ordering hint itself.
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN, expires_at_ms=1_700_000_000_000)))
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        (item,) = [i for i in discover_machine(src) if i.provider_id == "anthropic"]
        self.assertTrue(item.oauth_credential and item.cross_profile and item.live_source)
        self.assertEqual(item.expires_at, 1_700_000_000.0)

    def test_oauth_flag_is_private_never_in_the_response(self):
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN)))
        status, body = self.make().handle("subscriptions", [("view", "machine")])
        self.assertEqual(scan_for(body, (OTHER_TOKEN, "oauth_credential", "auth_type", "expires_at")), [])


class DiscoveryOauthFlag(ServiceBase):
    def test_flag_follows_the_pool_row_auth_type(self):
        self.s.write_auth(pool(oauth_row("a1"), {"id": "a2", "auth_type": "api_key", "access_token": "sk-ant-api03-udsyn"},
                               {"id": "a3", "access_token": "sk-ant-oat01-udsyn-untyped"}))
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        flags = {i.row_ref: i.oauth_credential for i in discover(src)}
        self.assertEqual(flags, {"a1": True, "a2": False, "a3": False})


# ---------------------------------------------------- credential provenance (review round 1, defect 1)

API_KEY = "sk-ant...pikey"
AGENT_KEY = "sk-ant...gentk"


def key_only_oauth_rows(row_id=ROW):
    """OAuth-tagged pool rows that carry NO usable access_token, only a generic api/agent key."""
    rows = {}
    for field in ("api_key", "agent_key"):
        for label, access in (("missing", None), ("blank", ""), ("whitespace", "   "), ("non-string", 7)):
            row = {"id": row_id, "auth_type": "oauth", "source": "manual:hermes_pkce", field: API_KEY}
            if access is not None:
                row["access_token"] = access
            rows[f"{field}/{label}"] = row
    return rows


class OauthAccessTokenProvenance(ServiceBase, SchemaAsserts):
    def setUp(self):
        super().setUp()
        self.transport.routes[ANTHROPIC_USAGE_URL] = json_response(usage_body())

    def cards(self, view):
        status, body = self.make().handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        self.assertEqual(scan_for(body, (API_KEY, AGENT_KEY, TOKEN, OTHER_TOKEN)), [])
        return [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]

    def seed_capture(self):
        target = default_store_path(self.s.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(FIXTURE.read_text())
        data["synthetic"] = False
        data.pop("_note", None)
        target.write_text(json.dumps(data))

    def discovered(self, view="profile"):
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        return [i for i in (discover_machine(src) if view == "machine" else discover(src)) if i.provider_id == "anthropic"]

    def test_selected_profile_oauth_row_without_an_access_token_sends_nothing(self):
        for name, row in key_only_oauth_rows().items():
            for view in ("machine", "profile"):
                with self.subTest(row=name, view=view):
                    self.transport.requests.clear()
                    self.s.write_auth(pool(row))
                    self.assertFalse(any(i.oauth_credential for i in self.discovered(view)))
                    (card,) = self.cards(view)
                    self.assertEqual((card["state"], card["reason"], card["meters"]), ("opaque", "unsupported", []))
                    self.assertEqual(self.transport.requests, [])  # the api/agent key never egresses

    def test_header_fallback_still_serves_a_key_only_oauth_row_without_any_request(self):
        self.seed_capture()
        self.s.write_auth(pool({"id": ROW, "auth_type": "oauth", "api_key": API_KEY}))
        for view in ("machine", "profile"):
            with self.subTest(view=view):
                (card,) = self.cards(view)
                self.assertEqual([(m["source"], m["observed_at"]) for m in card["meters"]],
                                 [("anthropic_headers", "2026-11-06T11:58:30Z")] * 2)
                self.assertEqual(self.transport.requests, [])

    def test_sibling_oauth_row_without_an_access_token_is_never_polled(self):
        for name, row in key_only_oauth_rows("v1").items():
            with self.subTest(row=name):
                self.transport.requests.clear()
                self.s.write_auth({"credential_pool": {}})
                self.s.write_profile("verifier", pool(row))
                self.assertFalse(any(i.oauth_credential for i in self.discovered("machine")))
                (card,) = self.cards("machine")
                self.assertEqual((card["state"], card["meters"]), ("opaque", []))
                self.assertEqual(self.transport.requests, [])

    def test_positive_control_the_access_token_is_the_only_bearer_even_beside_generic_keys(self):
        row = oauth_row(ROW, api_key=API_KEY, agent_key=AGENT_KEY)
        self.s.write_auth(pool(row))
        for view in ("machine", "profile"):
            with self.subTest(view=view):
                self.transport.requests.clear()
                (card,) = self.cards(view)
                self.assertEqual((card["state"], card["confidence"]), ("ready", "exact"))
                self.assertEqual([r[1]["Authorization"] for r in self.transport.requests], ["Bearer " + TOKEN])
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", pool(oauth_row("v1", token=OTHER_TOKEN, api_key=API_KEY)))
        self.transport.requests.clear()
        (card,) = self.cards("machine")
        self.assertEqual(card["state"], "ready")
        self.assertEqual([r[1]["Authorization"] for r in self.transport.requests], ["Bearer " + OTHER_TOKEN])

    def test_a_blank_access_token_beside_a_key_does_not_shadow_a_sibling_oauth_token(self):
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("a", pool({"id": "a1", "auth_type": "oauth", "access_token": "", "api_key": API_KEY}))
        self.s.write_profile("b", pool(oauth_row("b1", token=OTHER_TOKEN)))
        self.transport.requests.clear()
        self.cards("machine")
        self.assertEqual([r[1]["Authorization"] for r in self.transport.requests], ["Bearer " + OTHER_TOKEN])


# ------------------------------------------- live no-meter provenance (review round 1, defect 2)

NODE = shutil.which("node")
DIST = Path(__file__).resolve().parents[1] / "dist" / "index.js"
DESKTOP = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"
ROOT_ENTRY = Path(__file__).resolve().parents[3] / "plugin.js"
BRIDGE = """
const fs = require("fs");
const mod = require(process.argv[2]);
const cards = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(cards.map((c) => ({ machineOnly: mod.machineOnlyNote(c), opaque: mod.opaqueNote(c) }))));
"""
STALE_LIVE_CLAIMS = ("No live source for this account", "can only appear after an ordinary request returns eligible headers",
                     "no usable capture")


class LiveNoMeterProvenance(ServiceBase, SchemaAsserts):
    def setUp(self):
        super().setUp()
        self.s.write_auth(pool(oauth_row(ROW)))

    def seed_capture(self):
        target = default_store_path(self.s.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(FIXTURE.read_text())
        data["synthetic"] = False
        data.pop("_note", None)
        target.write_text(json.dumps(data))

    def card(self, outcome, capture=False, row=ROW, view="machine"):
        self.s.write_auth(pool(oauth_row(row)))
        default_store_path(self.s.root).unlink(missing_ok=True)  # each probe starts from a known capture state
        if capture:
            self.seed_capture()
        self.transport.requests.clear()
        self.transport.routes[ANTHROPIC_USAGE_URL] = outcome
        status, body = self.make().handle("subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        (card,) = [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]
        return card

    def frontend(self, *cards):
        if NODE is None:
            self.skipTest("node unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            data, script = Path(tmp, "cards.json"), Path(tmp, "bridge.js")
            data.write_text(json.dumps(list(cards)))
            script.write_text(BRIDGE)
            done = subprocess.run([NODE, str(script), str(DIST), str(data)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_200_with_no_current_window_keeps_the_endpoint_as_source_with_or_without_a_capture(self):
        empty = json_response(usage_body(five=None, week=None))
        passed = json_response(usage_body(five=(88.0, NOW - timedelta(seconds=1)), week=(5.0, NOW - timedelta(hours=1))))
        for name, outcome in (("null windows", empty), ("reset just passed", passed)):
            for capture in (False, True):
                for view in ("machine", "profile"):
                    with self.subTest(body=name, capture=capture, view=view):
                        card = self.card(outcome, capture=capture, view=view)
                        self.assertEqual((card["state"], card["confidence"], card["reason"], card["meters"]),
                                         ("opaque", "opaque", "source_unavailable", []))
                        self.assertEqual(card["debug"]["source"], "anthropic_oauth_usage")
                        self.assertEqual(len(self.transport.requests), 1)  # definitive: no estimate, no older fallback

    def test_service_produced_live_empty_cards_get_the_live_note_not_the_capture_only_guidance(self):
        cards = [self.card(json_response(usage_body(five=None, week=None)), capture=capture) for capture in (False, True)]
        cards.append(self.card(raw_response("{}", status=403)))
        for card, notes in zip(cards, self.frontend(*cards)):
            with self.subTest(reason=card["reason"]):
                self.assertIsNone(notes["machineOnly"])
                self.assertRegex(notes["opaque"], r"read live .* reported no current session or weekly window")
                self.assertRegex(notes["opaque"], r"no older captured reading is shown")
                for claim in STALE_LIVE_CLAIMS:
                    self.assertNotIn(claim, notes["opaque"])

    def test_permission_denied_without_a_capture_is_also_live_provenance(self):
        for status, state in ((403, "opaque"), (404, "error")):  # 404 is a credential/endpoint error card (§9.1.2)
            with self.subTest(status=status):
                card = self.card(raw_response("{}", status=status))
                self.assertEqual((card["state"], card["reason"], card["meters"]), (state, "permission_denied", []))
                self.assertEqual(card["debug"]["source"], "anthropic_oauth_usage")

    def test_a_403_with_a_current_capture_still_falls_back_to_the_header_meters(self):
        card = self.card(raw_response("{}", status=403), capture=True)
        self.assertEqual([m["source"] for m in card["meters"]], ["anthropic_headers"] * 2)
        self.assertEqual(card["debug"]["source"], "anthropic_headers")

    def test_401_card_carries_the_endpoint_source_and_stays_rejected(self):
        card = self.card(raw_response("{}", status=401), capture=True)
        self.assertEqual((card["state"], card["auth_status"], card["debug"]["source"]),
                         ("error", "rejected", "anthropic_oauth_usage"))

    def test_an_unreachable_endpoint_is_not_live_provenance(self):
        card = self.card(TransportError(TransportError.TIMEOUT))
        self.assertEqual((card["state"], card["debug"]["source"]), ("error", "local_inventory"))
        stale = self.card(TransportError(TransportError.TIMEOUT), capture=True, row="ant-syn-both-rolled")
        self.assertEqual((stale["state"], stale["meters"], stale["debug"]["source"]), ("error", [], "anthropic_headers"))

    def test_inventory_only_and_header_only_cards_keep_the_capture_guidance(self):
        self.s.write_auth(pool({"id": "plain", "auth_type": "api_key", "access_token": API_KEY}))
        self.transport.requests.clear()
        status, body = self.make().handle("subscriptions", [("view", "machine")])
        (inventory,) = [c for c in body["data"]["cards"] if c["provider_id"] == "anthropic"]
        self.assertEqual((inventory["reason"], inventory["debug"]["source"], self.transport.requests),
                         ("unsupported", "local_inventory", []))
        (notes,) = self.frontend(inventory)
        self.assertIn("no usable capture; enable the optional observer", notes["opaque"])
        self.assertIn("can only appear after an ordinary request returns eligible headers", notes["opaque"])

    def test_both_frontends_replaced_the_obsolete_capture_only_note(self):
        for path in (DIST, DESKTOP, ROOT_ENTRY):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertNotIn("no usable capture for this credential", text)
                self.assertIn("was read live for this credential and reported no current session or weekly window", text)
                self.assertIn("no live usage reading for this credential", text)
                self.assertIn("ANTHROPIC_LIVE_EMPTY_NOTE", text)


if __name__ == "__main__":
    unittest.main()
