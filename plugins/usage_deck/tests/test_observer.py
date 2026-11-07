"""Opt-in observer: consent, validation, zero-effect when off, the real schema-2 seam (DESIGN.md §12.1-§12.3).

Every payload below is a SYNTHETIC test callback: it exercises the observer -> ``record_headers`` ->
``CaptureStore`` -> card path, and is never evidence of a live Anthropic response.
"""

import contextlib
import functools
import hashlib
import importlib
import json
import logging
import os
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from plugins.usage_deck import captures, observer
from plugins.usage_deck.captures import CaptureStore, default_store_path, record_headers
from plugins.usage_deck.discovery import FileInventorySource
from plugins.usage_deck.observer import HostContext, Observer

from .helpers import NOW, FakeAnalytics, FakeTransport, ScratchHome
from .test_schema_preservation import SchemaAsserts
from .test_service import ServiceBase

P5 = "anthropic-ratelimit-unified-5h-"
P7 = "anthropic-ratelimit-unified-7d-"
FUTURE_RESET = "1793977200"  # 2026-11-06T15:00:00Z, after the fixed test clock
SEVEN_DAY_RESET = "1794301200"
FIXED = lambda: NOW  # noqa: E731 - the writer/reader clock of these tests (2026-11-06T12:00:00Z)
OBSERVED = "2026-11-06T11:59:30Z"
GOOD_HEADERS = {P5 + "utilization": "0.25", P5 + "reset": FUTURE_RESET, P7 + "utilization": "0.5",
                P7 + "reset": SEVEN_DAY_RESET, "anthropic-ratelimit-unified-status": "allowed"}
ENABLED = {"usage_deck": {"capture_anthropic_headers": True}}


class NetworkSpy:
    """Fails and records any attempt to open a connection while active."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        def refuse(*args, **kwargs):
            self.calls.append("connect")
            raise AssertionError("unexpected network access")

        self._patches = [mock.patch.object(socket.socket, "connect", refuse),
                         mock.patch.object(socket, "create_connection", refuse),
                         mock.patch.object(urllib.request.OpenerDirector, "open", refuse)]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()


def payload(**overrides):
    """What the Hermes core hook publishes (agent/provider_ratelimit_headers.py ``_dispatch``)."""
    base = {
        "schema_version": 1, "provider": "anthropic", "hermes_root": "/unset",
        "execution_profile": {"profile": "work", "is_global": False},
        "holder": {"profile": "work", "is_global": False},
        "credential_id": "ant-row-1", "observed_at": OBSERVED, "headers": dict(GOOD_HEADERS),
    }
    base.update(overrides)
    return base


def tree_digest(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return out


class ObserverBase(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.root = self.s.root
        self.home = self.s.home  # profiles/work
        self.store_path = default_store_path(self.root)
        self.writer_calls = []
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "tok-private-A"}]}})
        (self.home / "config.yaml").write_text("model:\n  provider: anthropic\n")
        (self.root / ".env").write_text("SECRET=1\n")
        self.config = ENABLED

    def context(self, home=None, config=None):
        return lambda: HostContext(Path(home or self.home), self.root, config if config is not None else self.config)

    def observer(self, home=None, config=None):
        def spy(*args, **kwargs):
            self.writer_calls.append((args, kwargs))
            return record_headers(*args, clock=FIXED, **kwargs)

        return Observer(self.context(home, config), spy)

    def fire(self, obs=None, **overrides):
        obs = obs or self.observer()
        obs(**payload(hermes_root=str(self.root), **overrides))

    def read(self, row="ant-row-1", scope=(False, "work")):
        return CaptureStore(self.store_path, clock=FIXED).latest("anthropic", [(scope[0], scope[1], row)], now=NOW)


class OptOutHasZeroEffects(ObserverBase):
    def test_absent_false_and_non_boolean_settings_do_nothing(self):
        before = tree_digest(self.root)
        for config in ({}, {"usage_deck": {}}, {"usage_deck": {"capture_anthropic_headers": False}},
                       {"usage_deck": {"capture_anthropic_headers": "true"}}, {"usage_deck": {"capture_anthropic_headers": 1}},
                       {"usage_deck": {"capture_anthropic_headers": None}}, {"usage_deck": "true"}, {"usage_deck": None},
                       {"capture_anthropic_headers": True}, {"usage_deck": {"capture_anthropic_headers ": True}}):
            with self.subTest(config=config), NetworkSpy() as spy:
                self.fire(self.observer(config=config))
                self.assertEqual(spy.calls, [])
        self.assertEqual(self.writer_calls, [])  # not even attempted
        self.assertFalse(self.store_path.exists())
        self.assertFalse((self.root / "plugin-data").exists())
        self.assertEqual(tree_digest(self.root), before)  # auth.json, config, .env and the tree: byte-identical

    def test_installation_alone_enables_nothing(self):
        """Registering the subscriber and importing the plugin make no capture without the setting."""
        ctx = RecordingContext()
        with mock.patch.object(observer, "host_has_hook", return_value=True):
            observer.register(ctx)
        self.assertEqual([name for name, _ in ctx.hooks], [observer.HOOK_NAME])
        callback = ctx.hooks[0][1]
        with mock.patch.object(observer, "host_context", return_value=HostContext(self.home, self.root, {})):
            callback(**payload(hermes_root=str(self.root)))
        self.assertFalse(self.store_path.exists())

    def test_opt_out_leaves_an_existing_store_untouched(self):
        self.fire()
        before = self.store_path.read_bytes()
        self.fire(self.observer(config={"usage_deck": {"capture_anthropic_headers": False}}),
                  headers={P5 + "utilization": "0.99"}, observed_at="2026-11-06T11:59:59Z")
        self.assertEqual(self.store_path.read_bytes(), before)  # existing observations simply stop updating

    def test_enabled_observer_itself_makes_no_network_request(self):
        with NetworkSpy() as spy:
            self.fire()
        self.assertEqual(spy.calls, [])
        self.assertIsNotNone(self.read())


class RecordingContext:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback, **kw):
        self.hooks.append((name, callback))


class OptInRecords(ObserverBase):
    def test_enabled_profile_records_a_scoped_schema_2_entry(self):
        self.fire()
        data = json.loads(self.store_path.read_text())
        self.assertEqual(data["schema_version"], 2)
        (entry,) = data["captures"]
        self.assertEqual(entry["provider"], "anthropic")
        self.assertEqual(entry["holder"], {"profile": "work", "is_global": False})
        self.assertEqual((entry["credential_id"], entry["observed_at"]), ("ant-row-1", OBSERVED))
        self.assertEqual(entry["headers"], GOOD_HEADERS)
        self.assertNotIn("hermes_root", json.dumps(data))  # the private routing root is never persisted
        self.assertNotIn(str(self.root), self.store_path.read_text())
        self.assertNotIn("execution", self.store_path.read_text())
        capture = self.read()
        self.assertEqual((capture.observed_at, capture.headers[P5 + "utilization"]), (NOW - timedelta(seconds=30), "0.25"))

    def test_registered_hook_callback_roundtrips_payload_to_sink_and_flag_gates_it(self):
        """The exact callable handed to ``register_hook`` (not a test-built Observer): flag off -> no file,
        flag on -> one scoped entry, and the routing root never reaches the file or the log."""
        ctx = RecordingContext()
        with mock.patch.object(observer, "host_has_hook", return_value=True):
            observer.register(ctx)
        ((name, callback),) = ctx.hooks
        self.assertEqual(name, "provider_ratelimit_headers")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        live = payload(hermes_root=str(self.root), observed_at=stamp)
        live["telemetry_schema_version"] = 1  # host envelope field, ignored
        for config, expect_file in (({"usage_deck": {"capture_anthropic_headers": False}}, False), (ENABLED, True)):
            with self.subTest(enabled=expect_file), mock.patch.object(
                    observer, "host_context", return_value=HostContext(self.home, self.root, config)):
                callback(**live)
            self.assertEqual(self.store_path.exists(), expect_file)
        (entry,) = json.loads(self.store_path.read_text())["captures"]
        self.assertEqual((entry["credential_id"], entry["holder"], entry["observed_at"]),
                         ("ant-row-1", {"profile": "work", "is_global": False}, stamp))
        self.assertEqual(entry["headers"], GOOD_HEADERS)
        text = self.store_path.read_text()
        for private in (str(self.root), "hermes_root", "execution_profile", "telemetry_schema_version"):
            self.assertNotIn(private, text)

    def test_global_fallback_row_is_attributed_to_the_global_holder_not_the_profile(self):
        self.fire(holder={"profile": "global", "is_global": True}, credential_id="g-row")
        (entry,) = json.loads(self.store_path.read_text())["captures"]
        self.assertEqual(entry["holder"], {"profile": "global", "is_global": True})
        self.assertIsNotNone(self.read("g-row", (True, "global")))
        self.assertIsNone(self.read("g-row", (False, "work")))

    def test_root_agent_records_for_the_global_holder(self):
        obs = self.observer(home=self.root)
        self.fire(obs, execution_profile={"profile": "global", "is_global": True},
                  holder={"profile": "global", "is_global": True})
        self.assertIsNotNone(self.read("ant-row-1", (True, "global")))

    def test_only_allow_listed_valid_headers_are_persisted(self):
        self.fire(headers={**GOOD_HEADERS, "authorization": "Bearer x", "set-cookie": "s=1",
                           P5 + "status": "sk-ant-LEAK", P7 + "utilization": "nan", "x-request-id": "r"})
        text = self.store_path.read_text()
        for leak in ("Bearer", "cookie", "sk-ant-LEAK", "x-request-id", "nan"):
            self.assertNotIn(leak, text)
        (entry,) = json.loads(text)["captures"]
        self.assertEqual(entry["headers"], {k: v for k, v in GOOD_HEADERS.items() if k != P7 + "utilization"})

    def test_out_of_order_delivery_does_not_regress(self):
        self.fire(observed_at="2026-11-06T11:59:50Z", headers={P5 + "utilization": "0.9"})
        self.fire(observed_at="2026-11-06T11:50:00Z", headers={P5 + "utilization": "0.1"})
        self.assertEqual(self.read().headers[P5 + "utilization"], "0.9")

    def test_auth_config_and_env_remain_unmodified(self):
        snapshot = {p: p.read_bytes() for p in (self.home / "auth.json", self.home / "config.yaml", self.root / ".env")}
        self.fire()
        self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".env", "plugin-data", "profiles"])


class ConsentFollowsTheOriginatingProfile(ObserverBase):
    def test_consent_is_read_for_the_execution_profile_context_only(self):
        """The host context the hook runs in IS the originating profile; its config alone decides."""
        self.fire(self.observer(config={}))  # work has not opted in ...
        self.assertFalse(self.store_path.exists())
        # ... even though the holder (global) or a displayed profile might have: nothing else is consulted.
        (self.root / "config.yaml").write_text("usage_deck:\n  capture_anthropic_headers: true\n")
        self.fire(self.observer(config={}), holder={"profile": "global", "is_global": True})
        self.assertFalse(self.store_path.exists())

    def test_payload_must_name_the_host_context_it_runs_in(self):
        before = tree_digest(self.root)
        for overrides in (
            dict(execution_profile={"profile": "other", "is_global": False}),     # not this agent's home
            dict(execution_profile={"profile": "global", "is_global": True}),     # root claimed from a profile home
            dict(execution_profile={"profile": "work", "is_global": True}),
            dict(holder={"profile": "other", "is_global": False}),                # holder is neither self nor root
            dict(holder={"profile": "work", "is_global": True}),
            dict(hermes_root="/somewhere/else"), dict(hermes_root="relative/root"), dict(hermes_root=""),
        ):
            with self.subTest(overrides=overrides):
                obs = self.observer()
                params = payload(**{"hermes_root": str(self.root), **overrides})
                obs(**params)
        self.assertEqual(self.writer_calls, [])
        self.assertEqual(tree_digest(self.root), before)

    def test_unresolvable_or_foreign_home_is_skipped(self):
        outside = Path(tempfile.mkdtemp(prefix="usage-deck-foreign-"))
        self.addCleanup(outside.rmdir)
        self.fire(self.observer(home=outside))  # a home that is neither the root nor <root>/profiles/<name>
        nested = self.home / "sub"
        nested.mkdir()
        self.fire(self.observer(home=nested))
        self.assertEqual(self.writer_calls, [])

    def test_unknown_host_without_context_helpers_skips_silently(self):
        def broken():
            raise ImportError("no host")

        Observer(broken, record_headers)(**payload(hermes_root=str(self.root)))
        self.assertFalse(self.store_path.exists())


class PayloadValidation(ObserverBase):
    def test_malformed_unknown_or_newer_payloads_are_skipped(self):
        good = payload(hermes_root=str(self.root))
        bad_payloads = [
            {k: v for k, v in good.items() if k != "holder"},
            {**good, "schema_version": 2}, {**good, "schema_version": True},
            {**good, "provider": "openai"}, {**good, "provider": None},
            {**good, "execution_profile": "work"}, {**good, "execution_profile": {"profile": "work"}},
            {**good, "holder": {"profile": "work", "is_global": "no"}},
            {**good, "holder": {"profile": 3, "is_global": False}},
            {**good, "credential_id": 5}, {**good, "credential_id": "eyJhbGciOi.x"},
            {**good, "credential_id": "sk-ant-secret"}, {**good, "credential_id": "bad id"},
            {**good, "observed_at": "yesterday"}, {**good, "observed_at": 12},
            {**good, "headers": None}, {**good, "headers": {}}, {**good, "headers": {"x-other": "1"}},
            {**good, "headers": {P5 + "utilization": "-1"}}, {**good, "headers": "x"},
            {**good, "hermes_root": 5},
        ]
        for bad in bad_payloads:
            with self.subTest(bad={k: bad[k] for k in bad if bad[k] != good.get(k)}):
                self.observer()(**bad)
                self.assertFalse(self.store_path.exists())

    def test_extra_host_envelope_fields_are_ignored_not_trusted(self):
        """The real host adds ``telemetry_schema_version`` to every hook call (found by the seam probe)."""
        self.observer()(**payload(hermes_root=str(self.root)), telemetry_schema_version=1, future_field={"x": 1})
        self.assertIsNotNone(self.read())
        self.assertNotIn("telemetry", self.store_path.read_text())
        self.assertNotIn("future_field", self.store_path.read_text())

    def test_future_observation_is_dropped_by_the_writer(self):
        stamp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        Observer(self.context(), record_headers)(**payload(hermes_root=str(self.root), observed_at=stamp))
        self.assertFalse(self.store_path.exists())


class FailuresFailClosedAndQuiet(ObserverBase):
    def test_any_writer_failure_is_swallowed_with_a_fixed_path_free_diagnostic(self):
        for error in (captures.CaptureError("lock timeout"), RuntimeError(f"boom {self.root} Bearer secret"),
                      OSError(f"disk {self.root}"), ValueError("x")):
            def raiser(*args, _error=error, **kwargs):
                raise _error

            with self.subTest(error=type(error).__name__), self.assertLogs(observer.log, level="DEBUG") as logs:
                Observer(self.context(), raiser)(**payload(hermes_root=str(self.root)))
            text = " ".join(logs.output)
            for needle in (str(self.root), "Bearer", "secret", "anthropic-ratelimit", "ant-row-1", "disk"):
                self.assertNotIn(needle, text)

    def test_lock_contention_drops_the_observation_without_raising(self):
        import fcntl
        self.fire()
        before = self.store_path.read_bytes()
        fd = os.open(self.store_path.parent / captures.LOCK_NAME, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.fire(observed_at="2026-11-06T11:59:59Z", headers={P5 + "utilization": "0.8"})
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_malformed_existing_store_is_not_replaced(self):
        self.store_path.parent.mkdir(parents=True)
        self.store_path.write_text("{corrupt PRIVATE")
        self.fire()
        self.assertEqual(self.store_path.read_text(), "{corrupt PRIVATE")

    def test_symlinked_parent_is_refused_without_following(self):
        outside = Path(tempfile.mkdtemp(prefix="usage-deck-outside-"))
        self.addCleanup(lambda: [p.unlink() for p in outside.rglob("*") if p.is_file()] or outside.rmdir())
        os.symlink(outside, self.root / "plugin-data")
        self.fire()
        self.assertEqual(list(outside.iterdir()), [])

    def test_hook_callback_never_raises_into_the_agent(self):
        raising = Observer(lambda: (_ for _ in ()).throw(RuntimeError("x")), record_headers)
        raising(**payload(hermes_root=str(self.root)))
        raising()  # even a call without any payload


class RegistrationConvention(unittest.TestCase):
    def test_package_import_has_no_side_effects_and_exposes_register(self):
        pkg = importlib.import_module("plugins.usage_deck")
        self.assertTrue(callable(pkg.register))
        self.assertNotIn("observer", pkg.__dict__.get("__dict__", {}))

    def test_register_subscribes_exactly_the_core_hook_once(self):
        pkg = importlib.import_module("plugins.usage_deck")
        ctx = RecordingContext()
        with mock.patch.object(observer, "host_has_hook", return_value=True):
            pkg.register(ctx)
        self.assertEqual(ctx.hooks, [("provider_ratelimit_headers", observer.observe)])

    def test_unknown_or_older_hosts_skip_without_error(self):
        pkg = importlib.import_module("plugins.usage_deck")
        ctx = RecordingContext()
        with mock.patch.object(observer, "host_has_hook", return_value=False):
            pkg.register(ctx)
        self.assertEqual(ctx.hooks, [])
        pkg.register(object())  # no register_hook at all
        with mock.patch.object(observer, "host_has_hook", return_value=True):
            class Exploding:
                def register_hook(self, *a, **k):
                    raise RuntimeError("old host")

            pkg.register(Exploding())  # swallowed: the plugin still loads

    def test_host_has_hook_reflects_the_registry(self):
        fake = mock.MagicMock()
        fake.VALID_HOOKS = {"provider_ratelimit_headers", "on_session_start"}
        with mock.patch.dict("sys.modules", {"hermes_cli.plugins": fake}):
            self.assertTrue(observer.host_has_hook())
            fake.VALID_HOOKS = {"on_session_start"}
            self.assertFalse(observer.host_has_hook())
        with mock.patch.dict("sys.modules", {"hermes_cli.plugins": None}):
            self.assertFalse(observer.host_has_hook())  # no host at all

    def test_observer_never_imports_dashboard_service_or_hits_the_core(self):
        source = Path(observer.__file__).read_text()
        for forbidden in ("from .service", "import service", "plugin_api", "urllib", "socket", "requests", "http."):
            self.assertNotIn(forbidden, source)


try:
    import hermes_cli.config  # noqa: F401
    import hermes_constants  # noqa: F401
    HAVE_HOST = True
except Exception:  # noqa: BLE001
    HAVE_HOST = False


@unittest.skipUnless(HAVE_HOST, "needs the Hermes dashboard interpreter")
class HostContextResolution(unittest.TestCase):
    """The real host helpers on scratch homes: consent comes from the ORIGINATING profile's own config."""

    def setUp(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        self.s = ScratchHome(base="/tmp" if os.path.isdir("/tmp") else None)  # never under a real ~/.hermes
        self.addCleanup(self.s.cleanup)
        self.work, self.other = self.s.home, self.s.root / "profiles" / "other"
        self.other.mkdir()
        (self.work / "config.yaml").write_text("usage_deck:\n  capture_anthropic_headers: true\n")
        (self.other / "config.yaml").write_text("model:\n  provider: anthropic\n")
        (self.s.root / "config.yaml").write_text("usage_deck:\n  capture_anthropic_headers: true\n")  # the root opted in
        self._set, self._reset = set_hermes_home_override, reset_hermes_home_override
        self.path = default_store_path(Path(os.path.realpath(self.s.root)))

    def deliver(self, home, **overrides):
        token = self._set(str(home))
        try:
            writer = functools.partial(record_headers, clock=FIXED)
            Observer(None, writer)(**payload(hermes_root=os.path.realpath(self.s.root), **overrides))
        finally:
            self._reset(token)

    def test_host_context_is_the_originating_home_and_its_config(self):
        token = self._set(str(self.work))
        try:
            ctx = observer.host_context()
        finally:
            self._reset(token)
        self.assertEqual((ctx.home, ctx.root), (Path(os.path.realpath(self.work)), Path(os.path.realpath(self.s.root))))
        self.assertTrue(observer.capture_enabled(ctx.config))

    def test_each_profile_decides_for_itself_and_the_holders_or_roots_opt_in_is_irrelevant(self):
        self.deliver(self.other, execution_profile={"profile": "other", "is_global": False},
                     holder={"profile": "global", "is_global": True})  # root opted in, 'other' did not: nothing recorded
        self.assertFalse(self.path.exists())
        self.deliver(self.work, holder={"profile": "global", "is_global": True}, credential_id="g-row")
        (entry,) = json.loads(self.path.read_text())["captures"]  # work opted in: the fallback row is the GLOBAL holder's
        self.assertEqual((entry["holder"], entry["credential_id"]), ({"profile": "global", "is_global": True}, "g-row"))

    def test_a_payload_naming_another_profile_than_the_running_home_is_skipped(self):
        self.deliver(self.work, execution_profile={"profile": "other", "is_global": False},
                     holder={"profile": "other", "is_global": False})
        self.assertFalse(self.path.exists())

    def test_root_agent_uses_the_root_config(self):
        self.deliver(self.s.root, execution_profile={"profile": "global", "is_global": True},
                     holder={"profile": "global", "is_global": True})
        self.assertTrue(self.path.is_file())


class SeamReachesCards(ServiceBase, SchemaAsserts):
    """Synthetic callback -> observer -> record_headers -> store -> card, through the real service."""

    def setUp(self):
        super().setUp()
        self.config_enabled = ENABLED
        self.written = []

    def callback(self, home=None, config=None):
        # exactly what the host calls: the subscriber registered through the package convention
        ctx = RecordingContext()
        with mock.patch.object(observer, "host_has_hook", return_value=True):
            importlib.import_module("plugins.usage_deck").register(ctx)
        real = ctx.hooks[0][1]
        context = HostContext(Path(home or self.s.home), self.s.root, config if config is not None else ENABLED)
        patches = [mock.patch.object(observer, "host_context", return_value=context),
                   mock.patch.object(captures, "_utc_now", FIXED)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return real

    def deliver(self, callback, **overrides):
        callback(**payload(hermes_root=str(self.s.root), **overrides))

    def cards(self, view, svc=None):
        status, body = self.call(svc or self.make(), "subscriptions", [("view", view)])
        self.assert_exact_envelope("subscriptions", status, body)
        return body["data"]["cards"]

    def anthropic_cards(self, view):
        return [c for c in self.cards(view) if c["provider_id"] == "anthropic"]

    def test_own_row_reaches_its_card_in_both_views(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "sk-ant-aaa1"}]}})
        self.deliver(self.callback())
        for view in ("profile", "machine"):
            (card,) = self.anthropic_cards(view)
            self.assertEqual((card["state"], card["confidence"], card["reason"]), ("ready", "exact", None), view)
            self.assertEqual([(m["id"], m["value"], m["observed_at"], m["resets_at"]) for m in card["meters"]], [
                ("session_5h", 25.0, OBSERVED, "2026-11-06T15:00:00Z"),
                ("weekly_7d", 50.0, OBSERVED, "2026-11-10T09:00:00Z")], view)

    def test_global_fallback_attribution_for_a_profile_without_its_own_rows(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "g-1", "access_token": "sk-ant-glob"}]}}, where="global")
        self.deliver(self.callback(), holder={"profile": "global", "is_global": True}, credential_id="g-1")
        (profile_card,) = self.anthropic_cards("profile")  # work falls back to the global pool: effective provenance
        self.assertEqual((profile_card["state"], profile_card["holders"]), ("ready", []))
        (machine_card,) = self.anthropic_cards("machine")
        self.assertEqual((machine_card["state"], machine_card["holders"]), ("ready", [{"profile": "global", "is_global": True}]))

    def test_profile_own_rows_shadow_global_so_a_global_capture_does_not_apply(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "same", "access_token": "sk-ant-work"}]}})
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "same", "access_token": "sk-ant-glob"}]}}, where="global")
        self.deliver(self.callback(), holder={"profile": "global", "is_global": True}, credential_id="same")
        (profile_card,) = self.anthropic_cards("profile")  # effective pool is work's own 'same', not global's
        self.assertEqual((profile_card["state"], profile_card["meters"]), ("opaque", []))
        cards = self.anthropic_cards("machine")
        by_holder = {tuple(h["profile"] for h in c["holders"]): c["state"] for c in cards}
        self.assertEqual(by_holder, {("global",): "ready", ("work",): "opaque"})

    def test_two_holders_with_the_same_row_id_never_share_a_capture(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "same", "access_token": "sk-ant-work"}]}})
        self.s.write_profile("verifier", {"credential_pool": {"anthropic": [{"id": "same", "access_token": "sk-ant-veri"}]}})
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "same", "access_token": "sk-ant-glob"}]}}, where="global")
        cb = self.callback()
        self.deliver(cb, credential_id="same", headers={P5 + "utilization": "0.11", P5 + "reset": FUTURE_RESET})
        self.deliver(cb, credential_id="same", headers={P5 + "utilization": "0.22", P5 + "reset": FUTURE_RESET},
                     holder={"profile": "global", "is_global": True})
        cards = self.anthropic_cards("machine")
        values = {c["holders"][0]["profile"]: [m["value"] for m in c["meters"]] for c in cards}
        self.assertEqual(values, {"work": [11.0], "global": [22.0], "verifier": []})  # verifier has no capture of its own
        self.assertEqual(len(json.loads(default_store_path(self.s.root).read_text())["captures"]), 2)

    def test_capture_for_a_row_that_exists_in_no_store_is_never_shown(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "mine", "access_token": "sk-ant-mine"}]}})
        self.deliver(self.callback(), credential_id="someone-else")
        (card,) = self.anthropic_cards("machine")
        self.assertEqual((card["state"], card["meters"], card["reason"]), ("opaque", [], "unsupported"))

    def test_opt_out_card_stays_opaque_and_nothing_is_written(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "sk-ant-aaa1"}]}})
        with NetworkSpy() as spy:
            self.deliver(self.callback(config={}))
            (card,) = self.anthropic_cards("profile")
        self.assertEqual(spy.calls, [])
        self.assertEqual((card["state"], card["meters"]), ("opaque", []))
        self.assertFalse(default_store_path(self.s.root).exists())

    def test_refused_unscoped_v1_file_leaves_cards_opaque_and_blocks_the_writer(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "sk-ant-aaa1"}]}})
        path = default_store_path(self.s.root)
        path.parent.mkdir(parents=True)
        v1 = json.dumps({"schema_version": 1, "captures": [{"provider": "anthropic", "credential_id": "ant-row-1",
                                                           "observed_at": OBSERVED, "headers": GOOD_HEADERS}]})
        path.write_text(v1)
        self.deliver(self.callback())
        self.assertEqual(path.read_text(), v1)  # never migrated, never overwritten
        (card,) = self.anthropic_cards("profile")
        self.assertEqual((card["state"], card["meters"]), ("opaque", []))

    def test_future_dated_capture_does_not_render(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "sk-ant-aaa1"}]}})
        path = default_store_path(self.s.root)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 2, "captures": [{
            "provider": "anthropic", "holder": {"profile": "work", "is_global": False}, "credential_id": "ant-row-1",
            "observed_at": "2026-11-06T13:00:00Z", "headers": {P5 + "utilization": "0.3", P5 + "reset": FUTURE_RESET}}]}))
        (card,) = self.anthropic_cards("profile")
        self.assertEqual((card["state"], card["meters"]), ("opaque", []))

    def test_no_secret_header_or_capture_reference_reaches_the_response(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"id": "ant-row-1", "access_token": "sk-ant-aaa1"}]}})
        self.deliver(self.callback())
        status, body = self.call(self.make(), "subscriptions", [("view", "machine")])
        text = json.dumps(body)
        for needle in ("sk-ant-aaa1", "anthropic-ratelimit", FUTURE_RESET, "plugin-data", "execution_profile", "credential_refs"):
            self.assertNotIn(needle, text)
        self.assertNotIn("ant-row-1", text)


if __name__ == "__main__":
    unittest.main()
