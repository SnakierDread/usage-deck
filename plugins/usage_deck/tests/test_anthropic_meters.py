"""Anthropic session/weekly meters from captured unified rate-limit headers (DESIGN.md §9).

All header data here is SYNTHETIC: ``fixtures/synthetic_anthropic_capture.json`` is invented (and flagged
``"synthetic": true``) or built inline. Nothing was captured from a real Anthropic response.
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


def adapter_for(row_ids, captures, **kw):
    ctx = make_ctx("anthropic", credential="sk-ant-oat-synthetic-0000", row_ids=tuple(row_ids), captures=captures, **kw)
    return create_adapter(ctx), ctx


class FixtureIsLabelledSynthetic(unittest.TestCase):
    def test_fixture_declares_itself_synthetic_and_is_refused_by_default(self):
        data = json.loads(FIXTURE.read_text())
        self.assertIs(data["synthetic"], True)
        self.assertIn("SYNTHETIC", data["_note"])
        self.assertIsNone(store(allow_synthetic=False).latest("anthropic", ["ant-syn-fresh"]))
        self.assertIsNotNone(store().latest("anthropic", ["ant-syn-fresh"]))

    def test_per_capture_synthetic_flag_is_also_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"schema_version": 1, "captures": [{
                "provider": "anthropic", "credential_id": "x1", "observed_at": "2026-11-06T11:00:00Z",
                "synthetic": True, "headers": {P5 + "utilization": "0.1"}}]}))
            self.assertIsNone(CaptureStore(path).latest("anthropic", ["x1"]))
            self.assertIsNotNone(CaptureStore(path, allow_synthetic=True).latest("anthropic", ["x1"]))


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
        capture = store().latest("anthropic", ["ant-syn-fresh"])
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
            path.write_text(json.dumps({"schema_version": 1, "captures": [{
                "provider": "openrouter", "credential_id": "r1", "observed_at": "2026-11-06T11:00:00Z",
                "headers": {P5 + "utilization": "0.1"}}]}))
            self.assert_opaque(adapter_for(["r1"], CaptureStore(path))[0])

    def test_capture_present_marks_quota_capability_and_source(self):
        adapter, _ = adapter_for(["ant-syn-fresh"], store())
        probe = adapter.probe()
        self.assertTrue(probe.supported)
        self.assertIn("quota", probe.capabilities)
        self.assertEqual(adapter.source_label, "anthropic_headers")

    def test_codex_stays_opaque_and_never_calls_out(self):
        transport = FakeTransport()
        ctx = make_ctx("openai-codex", credential="syn.codex.jwt", transport=transport)
        adapter = create_adapter(ctx)
        self.assertIsInstance(adapter, OpenAICodexAdapter)
        usage = adapter.get_usage(30)
        self.assertEqual((usage.meters, usage.confidence, usage.reason), ((), "opaque", "unsupported"))
        self.assertEqual(transport.requests, [])
        self.assertIsInstance(create_adapter(make_ctx("anthropic")), AnthropicAdapter)


class CaptureFileHandling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "plugin-data" / "usage-deck" / "anthropic-ratelimit.json"

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text if isinstance(text, str) else json.dumps(text))

    def entry(self, **kw):
        base = {"provider": "anthropic", "credential_id": "row1", "observed_at": "2026-11-06T11:00:00Z",
                "headers": {P5 + "utilization": "0.1"}}
        base.update(kw)
        return base

    def test_missing_corrupt_oversize_or_wrong_schema_files_mean_no_capture(self):
        self.assertIsNone(CaptureStore(self.path).latest("anthropic", ["row1"]))
        self.assertIsNone(CaptureStore(None).latest("anthropic", ["row1"]))
        for text in ("not json", "[]", json.dumps({"schema_version": 2, "captures": [self.entry()]}),
                     json.dumps({"schema_version": 1, "captures": "nope"}), "x" * (256 * 1024 + 1)):
            self.write(text)
            self.assertIsNone(CaptureStore(self.path).latest("anthropic", ["row1"]), text[:20])

    def test_one_bad_entry_does_not_hide_the_good_ones(self):
        self.write({"schema_version": 1, "captures": [
            self.entry(credential_id="eyJhbGciOi.secretlooking"), self.entry(credential_id="sk-ant-abc"),
            self.entry(observed_at="yesterday", credential_id="bad-ts"),
            self.entry(headers={P5 + "utilization": "x" * 100}, credential_id="long"), "junk", self.entry()]})
        found = CaptureStore(self.path)
        self.assertIsNotNone(found.latest("anthropic", ["row1"]))
        for rid in ("eyJhbGciOi.secretlooking", "sk-ant-abc", "bad-ts", "long"):
            self.assertIsNone(found.latest("anthropic", [rid]), rid)

    def test_only_allowlisted_headers_are_loaded(self):
        self.write({"schema_version": 1, "captures": [self.entry(headers={
            P5 + "utilization": "0.1", "authorization": "Bearer should-never-be-here", "x-other": "1",
            "Anthropic-RateLimit-Unified-Status": "allowed"})]})
        capture = CaptureStore(self.path).latest("anthropic", ["row1"])
        self.assertEqual(sorted(capture.headers), [P5 + "utilization", "anthropic-ratelimit-unified-status"])

    def test_newest_capture_wins_across_credential_ids(self):
        self.write({"schema_version": 1, "captures": [
            self.entry(credential_id="a", observed_at="2026-11-06T10:00:00Z"),
            self.entry(credential_id="b", observed_at="2026-11-06T11:00:00Z")]})
        found = CaptureStore(self.path).latest("anthropic", ["a", "b"])
        self.assertEqual(found.credential_id, "b")

    def test_record_headers_round_trip_filters_and_replaces(self):
        when = datetime(2026, 11, 6, 11, 0, 5, tzinfo=timezone.utc)
        headers = {P5 + "utilization": "0.31", P5 + "reset": "1793977200", "Authorization": "Bearer leak-me-not",
                   "set-cookie": "s=1", "content-type": "application/json"}
        record_headers(self.path, "anthropic", "row1", headers, when)
        record_headers(self.path, "anthropic", "row2", {P7 + "utilization": "0.5"}, when)
        record_headers(self.path, "anthropic", "row1", {P5 + "utilization": "0.40"}, when.replace(minute=30))
        text = self.path.read_text()
        for leak in ("leak-me-not", "Authorization", "cookie", "content-type"):
            self.assertNotIn(leak, text)
        data = json.loads(text)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual([c["credential_id"] for c in data["captures"]], ["row1", "row2"])
        found = CaptureStore(self.path).latest("anthropic", ["row1"])
        self.assertEqual(found.headers[P5 + "utilization"], "0.40")
        self.assertEqual(found.observed_at, when.replace(minute=30))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])  # no temp leftovers

    def test_record_headers_rejects_bad_input(self):
        when = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)
        with self.assertRaises(CaptureError):
            record_headers(self.path, "anthropic", "row1", {"x-other": "1"}, when)  # no unified headers
        with self.assertRaises(CaptureError):
            record_headers(self.path, "anthropic", "row1", {P5 + "utilization": "0.1"}, when.replace(tzinfo=None))
        with self.assertRaises(CaptureError):
            record_headers(self.path, "anthropic", "sk-ant-secret", {P5 + "utilization": "0.1"}, when)
        self.assertFalse(self.path.exists())

    def test_allowlist_matches_recon_header_names(self):
        for name in ("status", "reset", "5h-status", "5h-reset", "5h-utilization", "7d-status", "7d-reset",
                     "7d-utilization", "representative-claim", "fallback-percentage", "overage-status",
                     "overage-disabled-reason"):
            self.assertIn("anthropic-ratelimit-unified-" + name, ALLOWED_HEADERS)
        self.assertEqual(default_store_path(Path("/r")), Path("/r/plugin-data/usage-deck/anthropic-ratelimit.json"))


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
