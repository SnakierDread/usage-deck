"""Capture schema 2 store: scoped keys, strict validation, bounded locked atomic writer, confined paths
(DESIGN.md §12.3). Every header value here is SYNTHETIC test data, never a live observation.
"""

import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from plugins.usage_deck import captures
from plugins.usage_deck.captures import (
    ALLOWED_HEADERS,
    MAX_CAPTURE_BYTES,
    MAX_CAPTURES,
    CaptureError,
    CaptureStore,
    default_store_path,
    record_headers,
)

P5 = "anthropic-ratelimit-unified-5h-"
P7 = "anthropic-ratelimit-unified-7d-"
CLOCK_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
PROFILE = (False, "work")
GLOBAL = (True, "global")
posix_only = unittest.skipUnless(captures._CONFINED_IO, "needs POSIX openat confinement")


def clock():
    return CLOCK_NOW


def at(minutes_ago=0, **kw):
    return CLOCK_NOW - timedelta(minutes=minutes_ago, **kw)


def util(value="0.1"):
    return {P5 + "utilization": value}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="usage-deck-capture-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        self.path = default_store_path(self.root)

    def record(self, row="row1", headers=None, when=None, holder=PROFILE, provider="anthropic"):
        return record_headers(self.path, provider, row, headers or util(), when or at(5), holder=holder, clock=clock)

    def read(self, allow_synthetic=False):
        return CaptureStore(self.path, allow_synthetic=allow_synthetic, clock=clock)

    def write_raw(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))

    def entry(self, **kw):
        base = {"provider": "anthropic", "holder": {"profile": "work", "is_global": False}, "credential_id": "row1",
                "observed_at": "2026-11-06T11:00:00Z", "headers": util()}
        base.update(kw)
        return base

    def doc(self, *entries, version=2, **extra):
        return {"schema_version": version, "captures": list(entries), **extra}

    def stored(self):
        return json.loads(self.path.read_text())


class RoundTrip(StoreCase):
    def test_round_trip_is_schema_2_with_a_scoped_entry(self):
        stored = self.record(headers={**util("0.31"), P5 + "reset": "1793977200", "Authorization": "Bearer leak-me-not",
                                      "set-cookie": "s=1", "content-type": "application/json"}, when=at(2))
        self.assertEqual((stored.holder_profile, stored.holder_is_global, stored.credential_id), ("work", False, "row1"))
        text = self.path.read_text()
        for leak in ("leak-me-not", "Authorization", "cookie", "content-type"):
            self.assertNotIn(leak, text)
        data = json.loads(text)
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["captures"], [{
            "provider": "anthropic", "holder": {"profile": "work", "is_global": False}, "credential_id": "row1",
            "observed_at": "2026-11-06T11:58:00Z",
            "headers": {P5 + "reset": "1793977200", P5 + "utilization": "0.31"}}])
        found = self.read().latest("anthropic", [(False, "work", "row1")])
        self.assertEqual((found.headers[P5 + "utilization"], found.observed_at), ("0.31", at(2)))

    def test_replacing_the_same_scoped_key_with_a_newer_observation(self):
        self.record(headers=util("0.1"), when=at(10))
        self.record(headers=util("0.4"), when=at(1))
        (entry,) = self.stored()["captures"]
        self.assertEqual(entry["headers"][P5 + "utilization"], "0.4")

    def test_no_temp_files_remain_and_only_expected_names_exist(self):
        self.record()
        self.record("row2")
        names = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(names, [captures.LOCK_NAME, self.path.name])

    @posix_only
    def test_created_directories_and_files_are_private(self):
        self.record()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.path.parent / captures.LOCK_NAME).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.parent.parent.stat().st_mode), 0o700)

    def test_allowlist_is_exactly_the_twelve_core_names(self):
        suffixes = ("status", "reset", "5h-status", "5h-reset", "5h-utilization", "7d-status", "7d-reset",
                    "7d-utilization", "representative-claim", "fallback-percentage", "overage-status",
                    "overage-disabled-reason")
        self.assertEqual(ALLOWED_HEADERS, frozenset("anthropic-ratelimit-unified-" + s for s in suffixes))
        self.assertEqual(default_store_path(Path("/r")), Path("/r/plugin-data/usage-deck/anthropic-ratelimit.json"))


class InputValidation(StoreCase):
    def test_bad_input_raises_without_creating_anything(self):
        cases = [
            dict(headers={"x-other": "1"}),                                  # no unified headers
            dict(headers=util("nan")), dict(headers=util("-1")), dict(headers=util("1e5")),
            dict(headers={P5 + "reset": "12"}),                               # reset outside 2000..2100
            dict(headers={P5 + "status": "sk-ant-abc"}),                      # credential-shaped atom
            dict(headers={P5 + "status": "has space"}), dict(headers=util("9" * 70)),
            dict(headers=util("0.1\u00e9")),
            dict(row="***"), dict(row="eyJhbGciOi.x"), dict(row="sk-ant-abc"), dict(row="x" * 65),
            dict(provider="Anthropic!"),
            dict(holder=(True, "work")),                                      # root holder must be literally 'global'
            dict(holder=(False, "bad name")), dict(holder=(False, "")), dict(holder="work"), dict(holder=None),
            dict(when=CLOCK_NOW + timedelta(minutes=5)),                      # future
        ]
        for kw in cases:
            with self.subTest(kw=kw), self.assertRaises(CaptureError):
                self.record(**kw)
        with self.assertRaises(CaptureError):  # naive timestamp
            record_headers(self.path, "anthropic", "row1", util(), at(1).replace(tzinfo=None), holder=PROFILE, clock=clock)
        self.assertFalse(self.path.parent.exists() and any(self.path.parent.iterdir()))

    def test_holder_keyword_is_mandatory(self):
        with self.assertRaises(TypeError):
            record_headers(self.path, "anthropic", "row1", util(), at(1))  # type: ignore[call-arg]

    def test_invalid_values_are_discarded_independently_valid_ones_kept(self):
        self.record(headers={P5 + "utilization": "0.2", P5 + "reset": "-5", P7 + "utilization": "x",
                             P7 + "status": "allowed", "anthropic-ratelimit-unified-overage-disabled-reason": "org_level"})
        (entry,) = self.stored()["captures"]
        self.assertEqual(entry["headers"], {P5 + "utilization": "0.2", P7 + "status": "allowed",
                                            "anthropic-ratelimit-unified-overage-disabled-reason": "org_level"})

    def test_header_names_are_case_insensitive_and_a_later_bad_duplicate_wins(self):
        self.record(headers={"Anthropic-RateLimit-Unified-5H-Utilization": "0.3"})
        self.assertEqual(self.stored()["captures"][0]["headers"], {P5 + "utilization": "0.3"})
        self.assertEqual(captures.sanitize_headers([("x", "1")]), {})  # not a mapping
        pairs = {"dup": 1}
        self.assertEqual(captures.sanitize_headers(pairs), {})

    def test_failures_never_carry_paths_headers_or_content(self):
        secret_headers = {P5 + "status": "sk-ant-SECRETSECRET", P5 + "utilization": "0.1"}
        self.record(headers=secret_headers, when=at(3))  # the bad atom is dropped, the number is kept
        self.assertNotIn("SECRETSECRET", self.path.read_text())
        self.path.write_text("not json PRIVATE-CONTENT")
        with self.assertRaises(CaptureError) as caught:
            self.record("row2")
        for needle in (str(self.root), "PRIVATE-CONTENT", "anthropic-ratelimit"):
            self.assertNotIn(needle, str(caught.exception))


class OutOfOrderAndKeys(StoreCase):
    def test_older_observation_never_regresses_and_equal_is_ignored(self):
        self.assertIsNotNone(self.record(headers=util("0.9"), when=at(1)))
        self.assertIsNone(self.record(headers=util("0.1"), when=at(20)))  # late delivery of an old response
        self.assertIsNone(self.record(headers=util("0.5"), when=at(1)))    # same second: first writer wins
        (entry,) = self.stored()["captures"]
        self.assertEqual((entry["headers"][P5 + "utilization"], entry["observed_at"]), ("0.9", "2026-11-06T11:59:00Z"))

    def test_same_row_id_in_two_holders_stays_two_entries_and_never_crosses(self):
        self.record("same", util("0.11"), at(3), holder=PROFILE)
        self.record("same", util("0.22"), at(2), holder=(False, "other"))
        self.record("same", util("0.33"), at(1), holder=GLOBAL)
        self.record("same", util("0.44"), at(4), holder=(False, "global"))  # a profile literally named global
        self.assertEqual(len(self.stored()["captures"]), 4)
        store = self.read()
        for ref, expected in (((False, "work", "same"), "0.11"), ((False, "other", "same"), "0.22"),
                              ((True, "global", "same"), "0.33"), ((False, "global", "same"), "0.44")):
            self.assertEqual(store.latest("anthropic", [ref]).headers[P5 + "utilization"], expected, ref)
        self.assertIsNone(store.latest("anthropic", [(False, "nobody", "same")]))
        self.assertIsNone(store.latest("anthropic", [(True, "work", "same")]))
        self.assertIsNone(store.latest("openrouter", [(False, "work", "same")]))

    def test_unscoped_or_malformed_references_match_nothing(self):
        self.record("row1")
        store = self.read()
        for refs in (["row1"], [("row1",)], [(False, "work")], [("False", "work", "row1")], [None], [123],
                     [(0, "work", "row1")], [[False, "work", "row1"]], None, []):
            self.assertIsNone(store.latest("anthropic", refs), refs)
        self.assertIsNotNone(store.latest("anthropic", [(False, "work", "row1")]))

    def test_newest_scoped_reference_wins_with_deterministic_ties(self):
        self.record("a", util("0.1"), at(10), holder=PROFILE)
        self.record("b", util("0.2"), at(5), holder=GLOBAL)
        refs = [(False, "work", "a"), (True, "global", "b")]
        self.assertEqual(self.read().latest("anthropic", refs).credential_id, "b")
        self.assertEqual(self.read().latest("anthropic", refs[::-1]).credential_id, "b")
        self.record("c", util("0.3"), at(5), holder=PROFILE)  # same second as b: larger key tuple wins, any order
        refs.append((False, "work", "c"))
        self.assertEqual(self.read().latest("anthropic", refs).key, self.read().latest("anthropic", refs[::-1]).key)


class Bounds(StoreCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(captures.os, "fsync")  # hundreds of writes: durability is not under test here
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_entry_count_is_bounded_newest_retained_with_deterministic_ties(self):
        for index in range(MAX_CAPTURES + 20):
            self.record(f"row{index:03d}", util(), at(minutes_ago=0 if index >= 5 else 60 - index))
        entries = self.stored()["captures"]
        self.assertEqual(len(entries), MAX_CAPTURES)
        kept = {e["credential_id"] for e in entries}
        # rows 0..4 are the only old (>= 55 minutes) observations: all evicted; the 20 overflow slots are
        # taken from the identical-timestamp rows by tuple order (smallest keys evicted first)
        for index in range(5):
            self.assertNotIn(f"row{index:03d}", kept)
        self.assertEqual(sum(1 for i in range(5, MAX_CAPTURES + 20) if f"row{i:03d}" in kept), MAX_CAPTURES)
        self.assertNotIn("row005", kept)  # smallest tie-break key among the equal timestamps goes first
        self.assertIn(f"row{MAX_CAPTURES + 19:03d}", kept)

    def test_eviction_is_independent_of_insertion_order(self):
        def run(order):
            scratch = tempfile.TemporaryDirectory()
            self.addCleanup(scratch.cleanup)
            path = default_store_path(Path(scratch.name))
            for index in order:
                record_headers(path, "anthropic", f"row{index:03d}", util(), at(), holder=PROFILE, clock=clock)
            return sorted(c["credential_id"] for c in json.loads(path.read_text())["captures"])

        indexes = list(range(MAX_CAPTURES + 8))
        self.assertEqual(run(indexes), run(indexes[::-1]))

    def test_serialized_store_never_exceeds_256_kib(self):
        big = {name: ("9" * 60 if name.endswith(("reset", "utilization", "percentage")) else "a" * 60)
               for name in ALLOWED_HEADERS}
        big = {k: v for k, v in big.items() if not k.endswith("reset")}
        for index in range(MAX_CAPTURES):
            self.record(f"row{index:03d}", big, at(minutes_ago=0, seconds=MAX_CAPTURES - index))
        self.assertLessEqual(self.path.stat().st_size, MAX_CAPTURE_BYTES)
        entries = self.stored()["captures"]
        self.assertLessEqual(len(entries), MAX_CAPTURES)
        self.assertIn(f"row{MAX_CAPTURES - 1:03d}", {e["credential_id"] for e in entries})  # newest kept
        self.assertIsNotNone(self.read().latest("anthropic", [(False, "work", f"row{MAX_CAPTURES - 1:03d}")]))


class WriterRefusesUnknownStores(StoreCase):
    def assert_refused_untouched(self, content):
        self.write_raw(content)
        before = self.path.read_bytes()
        with self.assertRaises(CaptureError):
            self.record("new-row")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [captures.LOCK_NAME, self.path.name])

    def test_malformed_oversize_wrong_schema_and_synthetic_stores_are_never_overwritten(self):
        good = self.entry()
        for content in (
            "not json", "[]", "null", "", json.dumps(self.doc(good, version=1)), json.dumps(self.doc(good, version=3)),
            json.dumps({"captures": [good]}), json.dumps({"schema_version": 2, "captures": "x"}),
            json.dumps(self.doc(good, "junk")), json.dumps(self.doc(self.entry(credential_id="eyJhbG.x"))),
            json.dumps(self.doc(self.entry(headers={P5 + "utilization": "bad"}))),
            json.dumps(self.doc(self.entry(synthetic=True))), json.dumps(self.doc(good, synthetic=True)),
            json.dumps(self.doc(self.entry(headers={}))),
            json.dumps(self.doc(self.entry(extra="field"))),
            json.dumps(self.doc(self.entry(holder={"profile": "work"}))),
            json.dumps(self.doc(*[self.entry(credential_id=f"r{i}") for i in range(MAX_CAPTURES + 1)])),
            " " * (MAX_CAPTURE_BYTES + 1),
        ):
            with self.subTest(content=content[:40]):
                self.assert_refused_untouched(content)

    def test_v1_unscoped_store_is_refused_not_migrated(self):
        v1 = {"schema_version": 1, "captures": [{"provider": "anthropic", "credential_id": "row1",
                                                 "observed_at": "2026-11-06T11:00:00Z", "headers": util()}]}
        self.assert_refused_untouched(json.dumps(v1))
        self.assertIsNone(self.read().latest("anthropic", [(False, "work", "row1")]))
        self.assertIsNone(self.read().latest("anthropic", [(True, "global", "row1")]))

    def test_existing_future_dated_entry_never_blocks_a_real_observation(self):
        future = self.entry(observed_at="2026-11-06T13:00:00Z", headers=util("0.99"))
        self.write_raw(self.doc(future))
        self.record("row1", util("0.2"), at(1))
        (entry,) = self.stored()["captures"]
        self.assertEqual((entry["headers"][P5 + "utilization"], entry["observed_at"]), ("0.2", "2026-11-06T11:59:00Z"))


class ReaderRefusals(StoreCase):
    REF = (False, "work", "row1")

    def latest(self, **kw):
        return self.read(**kw).latest("anthropic", [self.REF], now=CLOCK_NOW)

    def test_missing_and_none_path_mean_no_capture(self):
        self.assertIsNone(self.latest())
        self.assertIsNone(CaptureStore(None).latest("anthropic", [self.REF]))

    def test_corrupt_oversize_and_wrong_schema_files_mean_no_capture(self):
        for text in ("not json", "[]", "null", json.dumps({"schema_version": 1, "captures": [self.entry()]}),
                     json.dumps({"schema_version": 3, "captures": [self.entry()]}),
                     json.dumps({"schema_version": 2, "captures": "nope"}),
                     json.dumps({"schema_version": True, "captures": [self.entry()]}),
                     json.dumps(self.doc(*[self.entry(credential_id=f"r{i}") for i in range(MAX_CAPTURES + 1)])),
                     "x" * (MAX_CAPTURE_BYTES + 1), "\u00e9" * 10):
            self.write_raw(text)
            self.assertIsNone(self.latest(), text[:20])
        self.assertEqual(self.read()._load(), {})

    def test_v1_unscoped_entries_are_refused_for_every_scope(self):
        v1 = {"schema_version": 1, "captures": [{"provider": "anthropic", "credential_id": "row1",
                                                 "observed_at": "2026-11-06T11:00:00Z", "headers": util()}]}
        self.write_raw(v1)
        for ref in (self.REF, (True, "global", "row1"), (False, "verifier", "row1")):
            self.assertIsNone(self.read().latest("anthropic", [ref], now=CLOCK_NOW))

    def test_synthetic_file_or_entry_is_refused_unless_a_test_opts_in(self):
        self.write_raw(self.doc(self.entry(), synthetic=True))
        self.assertIsNone(self.latest())
        self.assertIsNotNone(self.latest(allow_synthetic=True))
        self.write_raw(self.doc(self.entry(synthetic=True)))
        self.assertIsNone(self.latest())
        self.assertIsNotNone(self.latest(allow_synthetic=True))
        self.write_raw(self.doc(self.entry(synthetic="yes")))
        self.assertIsNone(self.latest(allow_synthetic=True))

    def test_future_observations_are_refused_by_the_reader_clock(self):
        self.write_raw(self.doc(self.entry(observed_at="2026-11-06T12:05:00Z")))
        self.assertIsNone(self.latest())
        self.assertIsNotNone(self.read().latest("anthropic", [self.REF], now=CLOCK_NOW + timedelta(minutes=6)))
        self.write_raw(self.doc(self.entry(observed_at="2026-11-06T12:00:01Z")))  # one second of tolerance
        self.assertIsNotNone(self.latest())

    def test_one_bad_entry_does_not_hide_the_good_ones(self):
        self.write_raw(self.doc(
            self.entry(credential_id="eyJhbGciOi.secretlooking"), self.entry(credential_id="sk-ant-abc"),
            self.entry(observed_at="yesterday", credential_id="bad-ts"),
            self.entry(headers={P5 + "utilization": "x" * 100}, credential_id="long"), "junk",
            self.entry(holder={"profile": "work", "is_global": True}, credential_id="badholder"),
            self.entry(holder=None, credential_id="noholder"), self.entry(extra=1, credential_id="extra"),
            self.entry()))
        found = self.read()
        self.assertIsNotNone(found.latest("anthropic", [self.REF], now=CLOCK_NOW))
        for rid in ("eyJhbGciOi.secretlooking", "sk-ant-abc", "bad-ts", "long", "badholder", "noholder", "extra"):
            self.assertIsNone(found.latest("anthropic", [(False, "work", rid)], now=CLOCK_NOW), rid)
        self.assertIsNone(found.latest("anthropic", [(True, "work", "badholder")], now=CLOCK_NOW))

    def test_only_allowlisted_headers_are_loaded_and_bad_allowlisted_values_refuse_the_entry(self):
        self.write_raw(self.doc(self.entry(headers={
            P5 + "utilization": "0.1", "authorization": "Bearer should-never-be-here", "x-other": "1",
            "anthropic-ratelimit-unified-status": "allowed"})))
        capture = self.latest()
        self.assertEqual(sorted(capture.headers), [P5 + "utilization", "anthropic-ratelimit-unified-status"])
        self.write_raw(self.doc(self.entry(headers={P5 + "utilization": "0.1", P5 + "reset": "-3"})))
        self.assertIsNone(self.latest())
        self.write_raw(self.doc(self.entry(headers={P5 + "utilization": " 0.1"})))  # stored values are already clean
        self.assertIsNone(self.latest())

    def test_duplicate_keys_keep_the_newest(self):
        self.write_raw(self.doc(self.entry(observed_at="2026-11-06T10:00:00Z", headers=util("0.1")),
                                self.entry(observed_at="2026-11-06T11:00:00Z", headers=util("0.2")),
                                self.entry(observed_at="2026-11-06T09:00:00Z", headers=util("0.3"))))
        self.assertEqual(self.latest().headers[P5 + "utilization"], "0.2")

    @posix_only
    @unittest.skipIf(os.geteuid() == 0, "root bypasses file modes")
    def test_unreadable_file_is_opaque_not_zero(self):
        self.write_raw(self.doc(self.entry()))
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        self.assertIsNone(self.latest())


@posix_only
class SymlinkRefusals(StoreCase):
    def setUp(self):
        super().setUp()
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()

    def assert_nothing_written_outside(self):
        self.assertEqual(sorted(p.name for p in self.outside.rglob("*")), [])

    def test_symlinked_plugin_data_parent_is_refused_for_write_and_read(self):
        os.symlink(self.outside, self.root / "plugin-data")
        with self.assertRaises(CaptureError):
            self.record()
        self.assert_nothing_written_outside()
        (self.outside / "usage-deck").mkdir()
        (self.outside / "usage-deck" / "anthropic-ratelimit.json").write_text(json.dumps(self.doc(self.entry())))
        self.assertIsNone(self.read().latest("anthropic", [(False, "work", "row1")], now=CLOCK_NOW))

    def test_symlinked_usage_deck_directory_is_refused(self):
        (self.root / "plugin-data").mkdir()
        os.symlink(self.outside, self.root / "plugin-data" / "usage-deck")
        with self.assertRaises(CaptureError):
            self.record()
        self.assert_nothing_written_outside()

    def test_symlinked_store_file_is_refused_and_target_untouched(self):
        target = self.outside / "victim.json"
        target.write_text(json.dumps(self.doc(self.entry())))
        self.path.parent.mkdir(parents=True)
        os.symlink(target, self.path)
        before = target.read_bytes()
        with self.assertRaises(CaptureError):
            self.record("new-row")
        self.assertEqual(target.read_bytes(), before)
        self.assertIsNone(self.read().latest("anthropic", [(False, "work", "row1")], now=CLOCK_NOW))

    def test_symlinked_lock_file_is_refused(self):
        target = self.outside / "lock-target"
        target.write_text("keep")
        self.path.parent.mkdir(parents=True)
        os.symlink(target, self.path.parent / captures.LOCK_NAME)
        with self.assertRaises(CaptureError):
            self.record()
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(self.path.exists())

    def test_symlink_planted_at_the_temporary_path_is_refused(self):
        target = self.outside / "tmp-target"
        self.path.parent.mkdir(parents=True)
        os.symlink(target, self.path.parent / ".capture-planted.tmp")
        with mock.patch.object(captures.secrets, "token_hex", return_value="planted"), \
                mock.patch.object(captures.os, "getpid", return_value=0):
            planted = ".capture-0-planted.tmp"
            os.rename(self.path.parent / ".capture-planted.tmp", self.path.parent / planted)
            with self.assertRaises(CaptureError):
                self.record()
        self.assertFalse(target.exists())
        self.assertFalse(self.path.exists())

    def test_store_path_must_be_the_canonical_layout_for_writes(self):
        with self.assertRaises(CaptureError):
            record_headers(self.root / "elsewhere.json", "anthropic", "row1", util(), at(1), holder=PROFILE, clock=clock)
        self.assertFalse((self.root / "elsewhere.json").exists())

    def test_symlinked_root_ancestors_above_the_host_root_are_the_hosts_choice(self):
        link = Path(self._tmp.name) / "root-link"
        os.symlink(self.root, link)
        record_headers(default_store_path(link), "anthropic", "row1", util(), at(1), holder=PROFILE, clock=clock)
        self.assertTrue(self.path.is_file())


@posix_only
class LockTimeout(StoreCase):
    def test_writer_drops_the_observation_after_a_bounded_wait_and_recovers(self):
        import fcntl
        self.record("first")
        before = self.path.read_bytes()
        fd = os.open(self.path.parent / captures.LOCK_NAME, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            started = datetime.now()
            with self.assertRaises(CaptureError) as caught:
                self.record("second")
            waited = (datetime.now() - started).total_seconds()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(str(caught.exception), "lock timeout")
        self.assertGreaterEqual(waited, captures.LOCK_WAIT_S * 0.9)
        self.assertLess(waited, 1.5)  # bounded: the agent never stalls on telemetry
        self.assertEqual(self.path.read_bytes(), before)  # dropped, not partially applied
        self.record("second")
        self.assertEqual(len(self.stored()["captures"]), 2)

    def test_without_a_platform_lock_writes_are_skipped_not_thread_locked(self):
        with mock.patch.object(captures, "fcntl", None):
            with self.assertRaises(CaptureError):
                self.record()
        self.assertFalse(self.path.exists())

    def test_without_confined_io_writes_are_skipped(self):
        with mock.patch.object(captures, "_CONFINED_IO", False):
            with self.assertRaises(CaptureError):
                self.record()
        self.assertFalse(self.path.exists())


class ConcurrentFirstRead(StoreCase):
    def test_card_workers_sharing_one_store_all_see_the_same_complete_snapshot(self):
        import threading
        import time

        self.record("row1")
        store = self.read()
        original = store._read_snapshot

        def slow():
            time.sleep(0.05)  # widen the window in which a half-built snapshot could be observed
            return original()

        store._read_snapshot = slow
        results = []

        def worker():
            results.append(store.latest("anthropic", [(False, "work", "row1")], now=CLOCK_NOW))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r is not None for r in results))


def _contention_worker(root, writer, count, barrier, results):
    """Runs in a separate process: waits for the barrier, then records ``count`` distinct keys."""
    path = default_store_path(Path(root))
    barrier.wait(30)
    outcome = {"ok": [], "dropped": 0, "other": []}
    for index in range(count):
        row = f"w{writer}-r{index}"
        try:
            stored = record_headers(path, "anthropic", row, {P5 + "utilization": f"0.{writer}{index}"},
                                    CLOCK_NOW - timedelta(seconds=writer * 100 + index), holder=PROFILE, clock=clock)
            outcome["ok"].append(row if stored is not None else None)
        except CaptureError as exc:
            if str(exc) == "lock timeout":
                outcome["dropped"] += 1
            else:
                outcome["other"].append(str(exc))
    results.put((writer, outcome))


@posix_only
class MultiprocessContention(StoreCase):
    def test_concurrent_processes_never_corrupt_or_lose_acknowledged_writes(self):
        ctx = multiprocessing.get_context("spawn")
        writers, per_writer = 6, 12
        barrier, results = ctx.Barrier(writers), ctx.Queue()
        procs = [ctx.Process(target=_contention_worker, args=(str(self.root), w, per_writer, barrier, results))
                 for w in range(writers)]
        for proc in procs:
            proc.start()
        outcomes = dict(results.get(timeout=120) for _ in procs)
        for proc in procs:
            proc.join(30)
            self.assertEqual(proc.exitcode, 0)
        acknowledged = {row for o in outcomes.values() for row in o["ok"] if row}
        self.assertEqual([o["other"] for o in outcomes.values()], [[]] * writers)  # only lock timeouts may drop
        self.assertGreater(len(acknowledged), per_writer)  # real progress by several writers
        data = self.stored()  # complete, parseable JSON (never a partial write)
        self.assertEqual(data["schema_version"], 2)
        stored_rows = {e["credential_id"] for e in data["captures"]}
        self.assertEqual(stored_rows, acknowledged)  # no lost update, nothing unacknowledged
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [captures.LOCK_NAME, self.path.name])
        for entry in data["captures"]:  # every stored value is exactly what its writer sent
            writer, index = entry["credential_id"][1:].split("-r")
            self.assertEqual(entry["headers"][P5 + "utilization"], f"0.{writer}{index}")
        self.assertEqual(len(CaptureStore(self.path, clock=clock)._load()), len(acknowledged))


if __name__ == "__main__":
    unittest.main()
