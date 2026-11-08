"""Anthropic session/weekly meters from captured unified rate-limit headers (DESIGN.md §9).

All header data here is SYNTHETIC: ``fixtures/synthetic_anthropic_capture.json`` is invented (and flagged
``"synthetic": true``) or built inline. Nothing was captured from a real Anthropic response. Capture schema 2
(scoped by holder, DESIGN.md §12.3); the writer/reader safety cases live in ``test_capture_store.py``.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from plugins.usage_deck.adapters import create_adapter
from plugins.usage_deck.adapters.cloud import AnthropicAdapter, OpenAICodexAdapter, anthropic_meters
from plugins.usage_deck.captures import (
    ALLOWED_HEADERS,
    CaptureError,
    CaptureStore,
    default_store_path,
    parse_reset_epoch,
    parse_utilization,
    record_headers,
)
from plugins.usage_deck.models.dto import check_wire

from .helpers import NOW, FakeTransport, make_ctx
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_anthropic_capture.json"
P5 = "anthropic-ratelimit-unified-5h-"
P7 = "anthropic-ratelimit-unified-7d-"


def store(allow_synthetic=True, path=FIXTURE):
    return CaptureStore(path, allow_synthetic=allow_synthetic)


def refs(*row_ids, profile="work", is_global=False):
    """Scoped capture references ``(is_global, profile, row id)`` of one auth store."""
    return [(is_global, profile, row_id) for row_id in row_ids]


def adapter_for(row_ids, captures, profile="work", **kw):
    ctx = make_ctx("anthropic", credential="sk-ant-oat-synthetic-0000", credential_refs=tuple(refs(*row_ids, profile=profile)),
                   captures=captures, **kw)
    return create_adapter(ctx), ctx


class FixtureIsLabelledSynthetic(unittest.TestCase):
    def test_fixture_declares_itself_synthetic_and_is_refused_by_default(self):
        data = json.loads(FIXTURE.read_text())
        self.assertIs(data["synthetic"], True)
        self.assertIn("SYNTHETIC", data["_note"])
        self.assertIsNone(store(allow_synthetic=False).latest("anthropic", refs("ant-syn-fresh")))
        self.assertIsNotNone(store().latest("anthropic", refs("ant-syn-fresh")))

    def test_per_capture_synthetic_flag_is_also_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"schema_version": 2, "captures": [{
                "provider": "anthropic", "holder": {"profile": "work", "is_global": False}, "credential_id": "x1",
                "observed_at": "2026-11-06T11:00:00Z", "synthetic": True, "headers": {P5 + "utilization": "0.1"}}]}))
            self.assertIsNone(CaptureStore(path).latest("anthropic", refs("x1")))
            self.assertIsNotNone(CaptureStore(path, allow_synthetic=True).latest("anthropic", refs("x1")))


class MeterMapping(unittest.TestCase):
    def test_fresh_capture_maps_both_windows_exactly(self):
        adapter, ctx = adapter_for(["ant-syn-fresh"], store())
        usage = adapter.get_usage(30)
        self.assertEqual((usage.confidence, usage.reason, usage.error), ("exact", None, None))
        session, weekly = usage.meters
        self.assertEqual((session.id, weekly.id), ("session_5h", "weekly_7d"))
        self.assertEqual(session.value, 1.8417)  # 0.018416969696969696 reported as a percentage, 4 dp
        self.assertEqual(weekly.value, 43.21)
        for meter in (session, weekly):
            self.assertEqual((meter.unit, meter.limit, meter.remaining), ("percent", 100.0, None))
            self.assertEqual((meter.scope, meter.confidence, meter.source), ("provider_account", "exact", "anthropic_headers"))
        self.assertEqual((session.period.label, weekly.period.label), ("rolling_5h", "rolling_7d"))
        self.assertEqual((session.period.start, session.period.end), (None, None))  # never computed
        self.assertEqual(session.resets_at, "2026-11-06T15:00:00Z")
        self.assertEqual(weekly.resets_at, "2026-11-10T09:00:00Z")

    def test_freshness_is_the_capture_call_time_not_the_read_time(self):
        adapter, ctx = adapter_for(["ant-syn-fresh"], store())
        for meter in adapter.get_usage(30).meters:
            self.assertEqual(meter.observed_at, "2026-11-06T11:58:30Z")
            self.assertNotEqual(meter.observed_at, ctx.now_ts())  # NOW is 12:00:00

    def test_no_network_request_is_ever_made(self):
        transport = FakeTransport()
        adapter, _ = adapter_for(["ant-syn-fresh"], store(), transport=transport)
        adapter.probe()
        adapter.get_usage(30)
        adapter.get_confidence()
        self.assertEqual(transport.requests, [])

    def test_over_limit_utilization_is_kept_not_clamped(self):
        adapter, _ = adapter_for(["ant-syn-over"], store())
        (meter,) = adapter.get_usage(30).meters
        self.assertEqual(meter.value, 107.0)
        self.assertEqual(meter.limit, 100.0)

    def test_window_whose_reset_has_passed_yields_no_meter(self):
        adapter, _ = adapter_for(["ant-syn-session-rolled"], store())
        usage = adapter.get_usage(30)
        self.assertEqual([m.id for m in usage.meters], ["weekly_7d"])  # session window already reset: no guess
        self.assertEqual(usage.meters[0].value, 25.0)

    def test_all_windows_rolled_is_opaque_not_zero(self):
        adapter, _ = adapter_for(["ant-syn-both-rolled"], store())
        usage = adapter.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "opaque", "source_unavailable"))

    def test_missing_reset_keeps_meter_with_null_resets_at(self):
        adapter, _ = adapter_for(["ant-syn-no-reset"], store())
        (meter,) = adapter.get_usage(30).meters
        self.assertEqual((meter.id, meter.value, meter.resets_at), ("session_5h", 20.0, None))

    def test_invalid_utilization_values_are_skipped(self):
        for bad in ("nan", "inf", "-0.1", "abc", "", "1e999"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_utilization(bad))
        self.assertEqual(parse_utilization("0"), 0.0)  # an observed zero is a value
        self.assertEqual(parse_utilization(" 1.5 "), 1.5)
        for bad in ("nan", "12", "-5", "99999999999999", "x", None):
            with self.subTest(reset=bad):
                self.assertIsNone(parse_reset_epoch(bad))
        self.assertEqual(parse_reset_epoch("1793977200"), datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc))

    def test_meter_mapping_is_pure_over_a_capture(self):
        capture = store().latest("anthropic", refs("ant-syn-fresh"))
        self.assertEqual([m.id for m in anthropic_meters(capture, NOW)], ["session_5h", "weekly_7d"])
        later = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)  # after the 5h reset, before the 7d one
        self.assertEqual([m.id for m in anthropic_meters(capture, later)], ["weekly_7d"])


class OpaqueFallbacks(unittest.TestCase):
    def assert_opaque(self, adapter):
        usage = adapter.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "opaque", "unsupported"))
        self.assertEqual(adapter.get_confidence(), "opaque")
        self.assertFalse(adapter.probe().supported)
        self.assertEqual(adapter.source_label, "local_inventory")

    def test_no_store_unknown_row_or_no_row_ids_stay_opaque(self):
        self.assert_opaque(adapter_for(["ant-syn-fresh"], None)[0])
        self.assert_opaque(adapter_for(["someone-else"], store())[0])
        self.assert_opaque(adapter_for([], store())[0])

    def test_capture_for_a_different_provider_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"schema_version": 2, "captures": [{
                "provider": "openrouter", "holder": {"profile": "work", "is_global": False}, "credential_id": "r1",
                "observed_at": "2026-11-06T11:00:00Z", "headers": {P5 + "utilization": "0.1"}}]}))
            self.assert_opaque(adapter_for(["r1"], CaptureStore(path))[0])

    def test_capture_present_marks_quota_capability_and_source(self):
        adapter, _ = adapter_for(["ant-syn-fresh"], store())
        probe = adapter.probe()
        self.assertTrue(probe.supported)
        self.assertIn("quota", probe.capabilities)
        self.assertEqual(adapter.source_label, "anthropic_headers")

    def test_codex_without_usable_routing_claims_makes_no_request(self):
        # DESIGN.md §9.2: Codex reads one ratified route, but a credential whose token carries no
        # account claim (here: not a JWT at all) is opaque/source_unavailable with no request.
        transport = FakeTransport()
        ctx = make_ctx("openai-codex", credential="syn.codex.jwt", transport=transport)
        adapter = create_adapter(ctx)
        self.assertIsInstance(adapter, OpenAICodexAdapter)
        usage = adapter.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "opaque", "source_unavailable"))
        self.assertEqual(transport.requests, [])
        self.assertIsInstance(create_adapter(make_ctx("anthropic")), AnthropicAdapter)


class AnthropicCardThroughService(ServiceBase, SchemaAsserts):
    """End to end through the service: scratch home + capture file at the Hermes-root location."""

    def seed(self, capture_rows=("ant-syn-fresh",)):
        self.s.write_auth({"credential_pool": {"anthropic": [
            {"id": capture_rows[0], "access_token": "sk-ant-oat-synthetic-1111", "refresh_token": "sk-ant-ort-synthetic-2222"}]}})
        target = default_store_path(self.s.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(FIXTURE.read_text())
        data["synthetic"] = False  # the service reads production-style files: model a real-looking capture file
        data.pop("_note", None)
        target.write_text(json.dumps(data))
        return target

    def cards(self, view):
        status, body = self.call(self.make(), "subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return body["data"]["cards"]

    def test_card_is_ready_exact_with_two_meters_in_both_views(self):
        self.seed()
        for view in ("machine", "profile"):
            (card,) = self.cards(view)
            self.assertEqual((card["state"], card["confidence"], card["reason"]), ("ready", "exact", None), view)
            self.assertEqual([m["id"] for m in card["meters"]], ["session_5h", "weekly_7d"])
            self.assertEqual(card["debug"]["source"], "anthropic_headers")
            self.assertIn("quota", card["debug"]["capabilities"])
            self.assertEqual(card["meters"][0]["observed_at"], "2026-11-06T11:58:30Z")
            self.assertEqual(card["meters"][0]["resets_at"], "2026-11-06T15:00:00Z")
            check_wire("card", card)

    def test_card_without_a_capture_for_its_row_is_opaque(self):
        self.seed()
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "no-capture", "access_token": "sk-ant-oat-synthetic-3333"}]}})
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["confidence"], card["reason"], card["meters"]), ("opaque", "opaque", "unsupported", []))
        self.assertEqual(card["debug"]["source"], "local_inventory")

    def test_default_service_ignores_synthetic_flagged_capture_files(self):
        target = self.seed()
        data = json.loads(target.read_text())
        data["synthetic"] = True
        target.write_text(json.dumps(data))
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["meters"]), ("opaque", []))

    def test_no_secret_or_raw_header_reaches_the_response(self):
        self.seed()
        status, body = self.call(self.make(), "subscriptions")
        text = json.dumps(body)
        for needle in ("sk-ant-oat-synthetic-1111", "sk-ant-ort-synthetic-2222", "anthropic-ratelimit", "1793977200"):
            self.assertNotIn(needle, text)

    def test_capture_from_another_profile_row_is_found_for_the_machine_card(self):
        self.s.write_profile("verifier", {"credential_pool": {"anthropic": [
            {"id": "ant-syn-fresh", "access_token": "sk-ant-oat-synthetic-1111"}]}})
        self.s.write_auth({"credential_pool": {}})
        target = default_store_path(self.s.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(FIXTURE.read_text())
        data["synthetic"] = False
        target.write_text(json.dumps(data))
        (card,) = self.cards("machine")
        self.assertEqual((card["state"], card["confidence"]), ("ready", "exact"))
        self.assertEqual(card["holders"], [{"profile": "verifier", "is_global": False}])


if __name__ == "__main__":
    unittest.main()


class SinkRoundtripWindows(unittest.TestCase):
    """record_headers -> capture file -> reader -> AnthropicAdapter, for each shape of header set.

    Header values are invented (SYNTHETIC). The writer is the real sink; nothing is estimated for a window whose
    headers are absent (partial sanitizing): the card shows exactly the windows the row carries.
    """

    SESSION_RESET = int(datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc).timestamp())
    WEEKLY_RESET = int(datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc).timestamp())
    OBSERVED = datetime(2026, 11, 6, 11, 58, 30, tzinfo=timezone.utc)

    BOTH = {
        P5 + "utilization": "0.25", P5 + "reset": str(SESSION_RESET), P5 + "status": "allowed",
        P7 + "utilization": "0.5", P7 + "reset": str(WEEKLY_RESET), P7 + "status": "allowed",
    }
    FIVE_ONLY = {k: v for k, v in BOTH.items() if k.startswith(P5)}
    WEEK_ONLY = {k: v for k, v in BOTH.items() if k.startswith(P7)}

    def roundtrip(self, headers, row="row-rt"):
        tmp = tempfile.TemporaryDirectory(prefix="usage-deck-rt-")
        self.addCleanup(tmp.cleanup)
        path = default_store_path(Path(tmp.name))
        stored = record_headers(path, "anthropic", row, headers, self.OBSERVED, holder=(False, "work"),
                                clock=lambda: NOW)
        self.assertIsNotNone(stored)
        adapter, _ = adapter_for([row], CaptureStore(path, clock=lambda: NOW))
        return adapter.get_usage(30)

    def shape(self, meter):
        return (meter.id, meter.label, meter.value, meter.limit, meter.remaining, meter.unit, meter.scope,
                meter.period.start, meter.period.end, meter.period.label, meter.confidence, meter.source,
                meter.observed_at, meter.resets_at)

    SESSION = ("session_5h", "Session (5-hour window)", 25.0, 100.0, None, "percent", "provider_account",
               None, None, "rolling_5h", "exact", "anthropic_headers", "2026-11-06T11:58:30Z", "2026-11-06T15:00:00Z")
    WEEKLY = ("weekly_7d", "Weekly (7-day window)", 50.0, 100.0, None, "percent", "provider_account",
              None, None, "rolling_7d", "exact", "anthropic_headers", "2026-11-06T11:58:30Z", "2026-11-10T09:00:00Z")

    def test_both_windows_are_emitted_session_first_with_exact_shapes(self):
        usage = self.roundtrip(self.BOTH)
        self.assertEqual((usage.confidence, usage.reason), ("exact", None))
        self.assertEqual([self.shape(m) for m in usage.meters], [self.SESSION, self.WEEKLY])

    def test_five_hour_only_renders_just_the_session_meter(self):
        usage = self.roundtrip(self.FIVE_ONLY)
        self.assertEqual((usage.confidence, usage.reason), ("exact", None))
        self.assertEqual([self.shape(m) for m in usage.meters], [self.SESSION])

    def test_week_only_renders_just_the_weekly_meter(self):
        usage = self.roundtrip(self.WEEK_ONLY)
        self.assertEqual((usage.confidence, usage.reason), ("exact", None))
        self.assertEqual([self.shape(m) for m in usage.meters], [self.WEEKLY])

    def test_session_meter_follows_the_same_staleness_rule_as_weekly(self):
        # Observed before the 5h reset, read after it: the session window no longer describes the current window
        # (no guess, not zero) while the weekly one is still current. This is the live 2026-11-08 situation.
        headers = dict(self.BOTH)
        headers[P5 + "reset"] = str(int(datetime(2026, 11, 6, 11, 59, tzinfo=timezone.utc).timestamp()))
        usage = self.roundtrip(headers)
        self.assertEqual([m.id for m in usage.meters], ["weekly_7d"])

    def test_missing_utilization_with_reset_present_yields_no_meter_for_that_window(self):
        headers = {k: v for k, v in self.BOTH.items() if k != P5 + "utilization"}
        usage = self.roundtrip(headers)
        self.assertEqual([m.id for m in usage.meters], ["weekly_7d"])
