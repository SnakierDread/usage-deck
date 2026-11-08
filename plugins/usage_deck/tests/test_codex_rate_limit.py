"""Codex wham/usage plan rate-limit meters (DESIGN.md §9.2.1).

Plans without a workspace spend control (e.g. ``go``) return the same top-level schema as the
spend-controlled ones but fill ``rate_limit.primary_window`` instead of
``spend_control.individual_limit``. Fixtures come from the sanitized two-account comparison
(``docs/evidence/codex-wham-usage-compare.sanitized.json``). Everything runs on injected transports.
"""

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from plugins.usage_deck.adapters.cloud import codex_meters, codex_rate_limit_meters, codex_spend_meter
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.dto import check_wire

from .helpers import NOW, json_response
from .test_codex_wham import CODEX_USAGE_URL, FUTURE, NOW_EPOCH, EMAIL, USER, ACCOUNT, codex_token, limit_body, usage_of
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

COMPARE = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "codex-wham-usage-compare.sanitized.json"
GO_RESET = 1796208479
BUSINESS_RESET = 1796083200


def fixtures():
    data = json.loads(COMPARE.read_text())
    return copy.deepcopy(data["go"]), copy.deepcopy(data["business"])


def rate_body(window=None, **rate):
    """A minimal plan rate-limit body; ``window`` overrides the primary window fields."""
    primary = {"used_percent": 40, "limit_window_seconds": 18000, "reset_after_seconds": 100, "reset_at": FUTURE}
    primary.update(window or {})
    block = {"allowed": True, "limit_reached": False, "primary_window": primary, "secondary_window": None}
    block.update(rate)
    return {"plan_type": "go", "rate_limit": block, "spend_control": {"reached": False, "individual_limit": None}}


def ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fixtures(unittest.TestCase):
    def test_fixtures_carry_no_identity(self):
        text = COMPARE.read_text()
        for banned in ("@", "user-", "account-"):
            self.assertNotIn(banned, text)
        go, business = fixtures()
        for body in (go, business):
            self.assertEqual((body["user_id"], body["account_id"], body["email"]), ("[REDACTED]",) * 3)

    def test_the_two_plans_fill_disjoint_shapes_of_one_schema(self):
        go, business = fixtures()
        self.assertEqual(set(go), set(business))
        self.assertIsNone(go["spend_control"]["individual_limit"])
        self.assertIsNotNone(go["rate_limit"]["primary_window"])
        self.assertIsNone(business["rate_limit"])
        self.assertEqual(business["spend_control"]["individual_limit"]["unit"], "credit")


class GoShape(unittest.TestCase):
    def test_go_body_maps_to_one_percent_meter_with_exact_reset_math(self):
        go, _ = fixtures()
        usage, adapter, transport = usage_of(go)
        self.assertEqual((usage.confidence, usage.reason, usage.error, usage.series), ("exact", None, None, ()))
        (m,) = usage.meters
        self.assertEqual(
            dto.to_plain(m),
            {"id": "rate_limit_primary", "label": "Plan usage limit (30-day window)", "value": 62.0, "limit": 100.0,
             "remaining": None, "unit": "percent", "scope": "provider_account",
             "period": {"start": None, "end": None, "label": "rolling_30d"}, "confidence": "exact",
             "source": "chatgpt_wham_usage", "observed_at": "2026-11-06T12:00:00Z",
             "resets_at": "2026-12-02T10:47:59Z"})
        self.assertEqual(adapter.source_label, "chatgpt_wham_usage")
        self.assertEqual(len(transport.requests), 1)

    def test_reset_is_the_reported_epoch_not_derived_from_reset_after_seconds(self):
        go, _ = fixtures()
        window = go["rate_limit"]["primary_window"]
        self.assertEqual(ts(GO_RESET), "2026-12-02T10:47:59Z")
        (m,) = codex_meters(go, NOW)
        self.assertEqual(m.resets_at, ts(window["reset_at"]))
        window["reset_after_seconds"] = 5  # contradicting relative hint: ignored
        (m,) = codex_meters(go, NOW)
        self.assertEqual(m.resets_at, ts(GO_RESET))
        self.assertNotEqual(m.resets_at, ts(NOW_EPOCH + 5))
        window["reset_at"] = None  # no absolute instant: null, never computed from the relative hint
        (m,) = codex_meters(go, NOW)
        self.assertIsNone(m.resets_at)

    def test_window_length_names_the_period(self):
        for seconds, label, phrase in ((2592000, "rolling_30d", "30-day"), (604800, "rolling_7d", "7-day"),
                                       (18000, "rolling_5h", "5-hour"), (900, "rolling_15m", "15-minute"),
                                       (90, "rolling_90s", "90-second")):
            with self.subTest(seconds=seconds):
                (m,) = codex_meters(rate_body({"limit_window_seconds": seconds}), NOW)
                self.assertEqual((m.period.label, m.label), (label, f"Plan usage limit ({phrase} window)"))
                self.assertEqual((m.period.start, m.period.end), (None, None))
        (m,) = codex_meters(rate_body({"limit_window_seconds": None}), NOW)
        self.assertEqual((m.period.label, m.label), ("unknown", "Plan usage limit"))

    def test_used_percent_is_never_clamped_or_rounded_and_zero_is_a_reading(self):
        for used, expected in ((0, 0.0), (0.5, 0.5), ("12.5", 12.5), (130, 130.0)):
            with self.subTest(used=used):
                (m,) = codex_meters(rate_body({"used_percent": used}), NOW)
                self.assertEqual((m.value, m.limit, m.remaining), (expected, 100.0, None))

    def test_no_used_percent_is_no_meter_not_zero(self):
        for body in (rate_body({"used_percent": None}), rate_body({}, primary_window={"reset_at": FUTURE})):
            self.assertEqual(codex_meters(body, NOW), [])

    def test_a_window_that_already_reset_is_not_presented_as_current(self):
        for reset in (NOW_EPOCH, NOW_EPOCH - 3600):
            self.assertEqual(codex_meters(rate_body({"reset_at": reset}), NOW), [])
        usage = usage_of(rate_body({"reset_at": NOW_EPOCH}))[0]
        self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error), ((), "opaque", "source_unavailable", None))

    def test_secondary_window_adds_a_second_meter(self):
        body = rate_body(secondary_window={"used_percent": 7, "limit_window_seconds": 604800, "reset_at": FUTURE + 5})
        first, second = codex_meters(body, NOW)
        self.assertEqual((first.id, first.period.label, first.value), ("rate_limit_primary", "rolling_5h", 40.0))
        self.assertEqual((second.id, second.label, second.period.label, second.value, second.resets_at),
                         ("rate_limit_secondary", "Secondary plan usage limit (7-day window)", "rolling_7d", 7.0,
                          ts(FUTURE + 5)))
        self.assertEqual((second.unit, second.scope, second.source, second.confidence),
                         ("percent", "provider_account", "chatgpt_wham_usage", "exact"))

    def test_secondary_only_is_still_mapped(self):
        body = rate_body(primary_window=None, secondary_window={"used_percent": 7, "reset_at": FUTURE})
        (m,) = codex_meters(body, NOW)
        self.assertEqual((m.id, m.value), ("rate_limit_secondary", 7.0))


class LimitReached(unittest.TestCase):
    def test_reached_marks_the_primary_meter_and_never_estimates_the_value(self):
        for used in (100, 97):
            with self.subTest(used=used):
                (m,) = codex_meters(rate_body({"used_percent": used}, allowed=False, limit_reached=True), NOW)
                self.assertEqual((m.value, m.limit, m.remaining, m.confidence), (float(used), 100.0, 0.0, "exact"))

    def test_not_reached_leaves_remaining_unreported(self):
        (m,) = codex_meters(rate_body(allowed=False, limit_reached=False), NOW)
        self.assertIsNone(m.remaining)

    def test_reached_without_a_window_still_reports_a_reached_meter(self):
        (m,) = codex_meters(rate_body(primary_window=None, allowed=False, limit_reached=True), NOW)
        self.assertEqual((m.id, m.value, m.limit, m.remaining, m.period.label, m.resets_at),
                         ("rate_limit_primary", None, 100.0, 0.0, "unknown", None))

    def test_reached_only_marks_the_primary_window(self):
        body = rate_body(allowed=False, limit_reached=True,
                         secondary_window={"used_percent": 3, "limit_window_seconds": 604800, "reset_at": FUTURE})
        first, second = codex_meters(body, NOW)
        self.assertEqual((first.remaining, second.remaining), (0.0, None))

    def test_a_reached_window_that_already_reset_is_not_shown(self):
        self.assertEqual(codex_meters(rate_body({"reset_at": NOW_EPOCH}, allowed=False, limit_reached=True), NOW), [])


class Precedence(unittest.TestCase):
    def test_business_shape_is_unchanged(self):
        _, business = fixtures()
        usage, _, _ = usage_of(business)
        (m,) = usage.meters
        self.assertEqual(
            dto.to_plain(m),
            {"id": "workspace_spend", "label": "Workspace spend limit (monthly)", "value": 960.6287800073624,
             "limit": 1500.0, "remaining": 539.3712199926376, "unit": "credit", "scope": "provider_account",
             "period": {"start": None, "end": None, "label": "monthly_workspace"}, "confidence": "exact",
             "source": "chatgpt_wham_usage", "observed_at": "2026-11-06T12:00:00Z",
             "resets_at": "2026-12-01T00:00:00Z"})
        self.assertEqual(ts(BUSINESS_RESET), "2026-12-01T00:00:00Z")
        self.assertEqual(codex_meters(business, NOW), [codex_spend_meter(business, NOW)])

    def test_spend_control_comes_first_and_rate_limit_is_the_secondary_meter(self):
        go, business = fixtures()
        business["rate_limit"] = go["rate_limit"]
        usage, _, _ = usage_of(business)
        self.assertEqual([m.id for m in usage.meters], ["workspace_spend", "rate_limit_primary"])
        self.assertEqual([m.unit for m in usage.meters], ["credit", "percent"])
        self.assertEqual(usage.meters[0], codex_spend_meter(business, NOW))
        self.assertEqual(usage.meters[1].value, 62.0)

    def test_an_expired_spend_meter_does_not_hide_the_rate_limit_meter(self):
        go, business = fixtures()
        business["rate_limit"] = go["rate_limit"]
        business["spend_control"]["individual_limit"]["reset_at"] = NOW_EPOCH
        self.assertEqual([m.id for m in codex_meters(business, NOW)], ["rate_limit_primary"])

    def test_a_malformed_rate_limit_never_discards_a_good_spend_meter(self):
        _, business = fixtures()
        for bad in ([], "x", {"allowed": "yes"}, {"primary_window": []}, {"primary_window": {"used_percent": "abc"}}):
            with self.subTest(bad=bad):
                body = copy.deepcopy(business)
                body["rate_limit"] = bad
                self.assertEqual([m.id for m in codex_meters(body, NOW)], ["workspace_spend"])

    def test_neither_shape_is_source_unavailable_as_before(self):
        go, business = fixtures()
        go["rate_limit"] = None
        business["spend_control"]["individual_limit"] = None
        cases = {
            "go without rate_limit": go, "business without individual_limit": business,
            "both null": {"rate_limit": None, "spend_control": {"individual_limit": None}},
            "empty rate_limit": {"rate_limit": {}, "spend_control": None},
            "rate_limit with only unknown keys": {"rate_limit": {"primary": {"used_percent": 99}}},
            "both windows null": {"rate_limit": {"allowed": True, "limit_reached": False, "primary_window": None,
                                                 "secondary_window": None}},
            "empty body": {},
        }
        for name, body in cases.items():
            with self.subTest(name):
                usage, adapter, _ = usage_of(body)
                self.assertEqual((usage.meters, usage.confidence, usage.reason, usage.error),
                                 ((), "opaque", "source_unavailable", None), name)
                self.assertEqual(adapter.source_label, "local_inventory")

    def test_unrecognised_spend_shape_with_a_rate_limit_still_maps_the_rate_limit(self):
        go, _ = fixtures()
        go["spend_control"]["individual_limit"] = {"source": "other", "unit": "usd", "limit": "5", "used": "1"}
        usage, _, _ = usage_of(go)
        self.assertEqual([m.id for m in usage.meters], ["rate_limit_primary"])


class Malformed(unittest.TestCase):
    def test_wrong_shaped_rate_limit_without_a_spend_meter_is_malformed(self):
        cases = {
            "list": {"rate_limit": []}, "string": {"rate_limit": "x"}, "true": {"rate_limit": True},
            "allowed string": rate_body(allowed="yes"), "reached number": rate_body(limit_reached=1),
            "window list": rate_body(primary_window=[]), "window string": rate_body(primary_window="x"),
            "secondary list": rate_body(secondary_window=[]),
            "used bool": rate_body({"used_percent": True}), "used text": rate_body({"used_percent": "abc"}),
            "used negative": rate_body({"used_percent": -1}), "used nan": rate_body({"used_percent": float("nan")}),
            "window zero": rate_body({"limit_window_seconds": 0}), "window negative": rate_body({"limit_window_seconds": -5}),
            "window bool": rate_body({"limit_window_seconds": True}), "window text": rate_body({"limit_window_seconds": "30d"}),
            "window fractional": rate_body({"limit_window_seconds": 1.5}),
            "window huge": rate_body({"limit_window_seconds": 10 ** 12}),
            "reset bool": rate_body({"reset_at": True}), "reset text": rate_body({"reset_at": "soon"}),
            "reset negative": rate_body({"reset_at": -1}),
        }
        for name, body in cases.items():
            with self.subTest(name):
                usage = usage_of(body)[0]
                self.assertEqual((usage.error.code, usage.meters, usage.confidence), ("malformed_response", (), "opaque"), name)

    def test_rate_limit_parsers_raise_valueerror_directly(self):
        with self.assertRaises(ValueError):
            codex_rate_limit_meters({"rate_limit": []}, NOW)
        self.assertEqual(codex_rate_limit_meters({}, NOW), [])
        self.assertEqual(codex_rate_limit_meters({"rate_limit": None}, NOW), [])


class RateLimitService(ServiceBase, SchemaAsserts):
    """End to end through the service: card state, wire schema, masking."""

    def seed(self, *rows):
        self.s.write_auth({"credential_pool": {"openai-codex": list(rows)}}, where="profile")

    def codex(self):
        status, body = self.make().handle("subscriptions", [("view", "profile")])
        self.assert_exact_envelope("subscriptions", status, body)
        return [c for c in body["data"]["cards"] if c["provider_id"] == "openai-codex"]

    def test_go_card_is_ready_with_one_percent_meter(self):
        go, _ = fixtures()
        go["rate_limit"]["primary_window"]["reset_at"] = FUTURE
        self.seed({"id": "c1", "access_token": codex_token(plan="go"), "refresh_token": "udsyn-refresh-not-for-sending"})
        self.transport.routes[CODEX_USAGE_URL] = json_response(go)
        (card,) = self.codex()
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["error"]), ("ready", "exact", None, None))
        self.assertEqual((card["auth_status"], card["debug"]["source"]), ("available", "chatgpt_wham_usage"))
        (m,) = card["meters"]
        self.assertEqual((m["id"], m["unit"], m["value"], m["limit"], m["remaining"], m["source"]),
                         ("rate_limit_primary", "percent", 62.0, 100.0, None, "chatgpt_wham_usage"))
        self.assertEqual(m["period"], {"start": None, "end": None, "label": "rolling_30d"})
        check_wire("card", card)
        blob = json.dumps(card)
        for leaked in (EMAIL, USER, ACCOUNT, "REDACTED", "refresh"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(len(self.transport.requests), 1)

    def test_go_reached_card_is_ready_and_flags_the_meter(self):
        self.seed({"id": "c1", "access_token": codex_token(plan="go")})
        self.transport.routes[CODEX_USAGE_URL] = json_response(rate_body({"used_percent": 100}, allowed=False, limit_reached=True))
        (card,) = self.codex()
        self.assertEqual((card["state"], card["confidence"]), ("ready", "exact"))
        (m,) = card["meters"]
        self.assertEqual((m["value"], m["remaining"]), (100.0, 0.0))
        check_wire("card", card)

    def test_go_without_any_window_stays_opaque(self):
        self.seed({"id": "c1", "access_token": codex_token(plan="go")})
        self.transport.routes[CODEX_USAGE_URL] = json_response({"rate_limit": None, "spend_control": {"individual_limit": None}})
        (card,) = self.codex()
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["meters"], card["error"]),
                         ("opaque", "opaque", "source_unavailable", [], None))
        self.assertEqual(card["debug"]["source"], "local_inventory")

    def test_business_card_with_both_shapes_lists_spend_first(self):
        go, business = fixtures()
        business["rate_limit"] = go["rate_limit"]
        business["rate_limit"]["primary_window"]["reset_at"] = FUTURE
        business["spend_control"]["individual_limit"]["reset_at"] = FUTURE
        self.seed({"id": "c1", "access_token": codex_token()})
        self.transport.routes[CODEX_USAGE_URL] = json_response(business)
        (card,) = self.codex()
        self.assertEqual([m["id"] for m in card["meters"]], ["workspace_spend", "rate_limit_primary"])
        check_wire("card", card)


if __name__ == "__main__":
    unittest.main()
