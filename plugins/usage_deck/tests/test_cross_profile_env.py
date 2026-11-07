"""Machine view reads ANOTHER home's configured endpoint with that home's own ``key_env``/``.env`` (DESIGN.md §13).

User-ratified 2026-11-07. Two fake homes under ``/tmp``: ``work`` (the viewed profile, nothing configured) and
``verifier`` (an empty auth pool, a ``config.yaml`` custom GPU endpoint with ``key_env`` and a ``.env`` holding the
key). Every value is synthetic and the transport is injected, so nothing leaves the box.

The disk tests drive the REAL host helpers (``host_inventory_source``: the shared YAML reader and ``.env``
tokenizer); the injected-loader tests run anywhere.
"""

import json
import logging
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import hermes_cli.config  # noqa: F401
    import hermes_constants  # noqa: F401
    HAVE_HOST = True
except Exception:  # noqa: BLE001
    HAVE_HOST = False

from plugins.usage_deck.adapters.transport import TransportError
from plugins.usage_deck.discovery import FileInventorySource, discover, discover_machine, host_inventory_source
from plugins.usage_deck.service import UsageDeckService

from .helpers import NOW, FakeAnalytics, ScratchHome, json_response, raw_response, scan_for

GPU = "http://gpu.lan:8000"
ENV_NAME = "GPU_KEY"
GPU_VALUE = "sntl-gpu-key-9f1c44d7a02b"
WORK_VALUE = "sntl-work-key-31be88c0d5e7"
ROOT_VALUE = "sntl-root-key-6a02f9e1c733"
PROC_VALUE = "sntl-proc-key-c4d81a5be092"
INLINE_VALUE = "sntl-inline-key-0e5b7d3a912c"
METRICS = "process_cpu_percent 12.5\nprocess_resident_memory_bytes 1000\n"
ALL_VALUES = [GPU_VALUE, WORK_VALUE, ROOT_VALUE, PROC_VALUE, INLINE_VALUE]


def gpu_config(name="gpu", extra=""):
    return ("custom_providers:\n  - name: " + name + "\n    base_url: " + GPU + "/v1\n    key_env: " + ENV_NAME + "\n"
            + extra)


class GpuTransport:
    """A box that serves ``/metrics`` (and, if asked, ``/v1/models``) and records every request."""

    def __init__(self, require=None):
        self.require = require  # Bearer value demanded on every route, or None for an open box
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        if not url.startswith(GPU + "/"):
            raise TransportError(TransportError.CONNECTION)
        if self.require is not None and headers.get("Authorization") != "Bearer " + self.require:
            return json_response({"error": {"type": "authentication_error"}}, status=401)
        if url == GPU + "/metrics":
            return raw_response(METRICS, latency=2.0)
        if url == GPU + "/v1/models":
            return json_response({"data": [{"id": "m"}]}, latency=6.0)
        return raw_response("not found", status=404)

    def sent(self):
        return [(u, h.get("Authorization")) for u, h in self.requests]


def tree(root):
    found = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                found[os.path.relpath(path, root)] = b"->" + os.readlink(path).encode()
                continue
            with open(path, "rb") as fh:
                found[os.path.relpath(path, root)] = fh.read()
    return found


class LogCapture:
    def __init__(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append

    def __enter__(self):
        root = logging.getLogger()
        self._level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self._level)

    def text(self):
        return " ".join(r.getMessage() + repr(r.args) for r in self.records)


@unittest.skipUnless(HAVE_HOST, "needs the Hermes host interpreter (real YAML reader and .env tokenizer)")
class SiblingConfigAndEnvOnDisk(unittest.TestCase):
    def setUp(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        # Under /tmp, outside any real ~/.hermes tree, so the host cannot fall back to real credentials.
        self.s = ScratchHome(base="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self.s.cleanup)
        env = {k: v for k, v in os.environ.items() if k != ENV_NAME}
        env[ENV_NAME + "_PROC"] = PROC_VALUE  # a process-level secret that must never reach a sibling
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.s.write_auth({"credential_pool": {}})                       # the viewed 'work' profile: nothing
        self.verifier = self.s.root / "profiles" / "verifier"
        self.s.write_profile("verifier", {"credential_pool": {}})        # an EMPTY pool
        (self.verifier / "config.yaml").write_text(gpu_config())
        (self.verifier / ".env").write_text(ENV_NAME + "=" + GPU_VALUE + "\nUNRELATED_SECRET=sntl-unrelated-77aa\n")
        token = set_hermes_home_override(str(self.s.home))
        self.addCleanup(reset_hermes_home_override, token)

    def deck(self, transport, view="machine", profile_only=False, which=("subscriptions", "health")):
        svc = UsageDeckService(
            inventory_factory=host_inventory_source,
            analytics_factory=lambda profile: FakeAnalytics(),
            transport=transport,
            clock=lambda: NOW,
        )
        params = [("view", view)]
        out = [svc.handle(name, params, profile_only=profile_only)[1] for name in which]
        return out

    def machine(self, transport):
        subs, health = self.deck(transport)
        return subs["data"]["cards"], health["data"]["entries"], (subs, health)

    # ------------------------------------------------------------------ the required behaviour

    def test_verifier_only_configured_gpu_endpoint_is_scraped_with_its_own_key(self):
        box = GpuTransport(require=GPU_VALUE)
        cards, health, envelopes = self.machine(box)
        (card,) = cards
        self.assertEqual(card["provider_id"], "custom:gpu")
        self.assertEqual([(h["profile"], h["is_global"]) for h in card["holders"]], [("verifier", False)])
        self.assertEqual(card["auth_status"], "available")
        (entry,) = health
        self.assertEqual((entry["status"], entry["confidence"], entry["metrics_available"], entry["reason"]),
                         ("healthy", "self_hosted", True, None))
        self.assertEqual((entry["cpu_percent"], entry["memory_bytes"]), (12.5, 1000))
        # read-only ceiling: exactly one GET, to /metrics of the configured origin, nothing else (no models probe)
        self.assertEqual(box.sent(), [(GPU + "/metrics", "Bearer " + GPU_VALUE)])
        self.assertEqual(scan_for(envelopes, ALL_VALUES + ["sntl-unrelated-77aa"]), [])

    def test_open_box_is_scraped_the_same_way(self):
        box = GpuTransport()
        _cards, health, _ = self.machine(box)
        self.assertEqual([u for u, _a in box.sent()], [GPU + "/metrics"])
        self.assertEqual(health[0]["status"], "healthy")

    def test_missing_variable_sends_nothing_and_is_honest(self):
        (self.verifier / ".env").write_text("UNRELATED=1\n")
        box = GpuTransport()
        cards, health, _ = self.machine(box)
        self.assertEqual(box.requests, [])
        (card,) = cards
        self.assertEqual(card["auth_status"], "missing")
        self.assertEqual((health[0]["status"], health[0]["reason"], health[0]["metrics_available"]),
                         ("unknown", "credentials_missing", False))

    def test_inline_api_key_outranks_key_env_and_never_reaches_metrics(self):
        (self.verifier / "config.yaml").write_text(
            gpu_config(extra="    api_key: " + INLINE_VALUE + "\n"))
        box = GpuTransport()
        cards, _health, envelopes = self.machine(box)
        self.assertEqual(box.sent(), [(GPU + "/metrics", None)])     # anonymous: neither the inline key nor key_env
        self.assertEqual(cards[0]["auth_status"], "available")        # presence only
        self.assertEqual(scan_for(envelopes, ALL_VALUES), [])

    # ------------------------------------------------------------------ isolation

    def test_key_env_is_resolved_in_the_owning_home_only(self):
        (self.verifier / ".env").write_text("UNRELATED=1\n")                       # verifier does NOT hold the key
        (self.s.home / ".env").write_text(ENV_NAME + "=" + WORK_VALUE + "\n")       # the viewed home does
        (self.s.root / ".env").write_text(ENV_NAME + "=" + ROOT_VALUE + "\n")       # and so does the global root
        os.environ[ENV_NAME] = PROC_VALUE                                            # and the running process
        box = GpuTransport()
        cards, health, envelopes = self.machine(box)
        self.assertEqual(box.requests, [])                    # nothing borrowed from any other source
        self.assertEqual((cards[0]["auth_status"], health[0]["reason"]), ("missing", "credentials_missing"))
        self.assertEqual(scan_for(envelopes, ALL_VALUES), [])

    def test_sibling_values_are_never_loaded_into_the_process_environment(self):
        before = dict(os.environ)
        self.machine(GpuTransport())
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn(GPU_VALUE, os.environ.values())

    def test_the_global_root_is_a_home_too(self):
        (self.verifier / "config.yaml").unlink()
        (self.s.root / "config.yaml").write_text(gpu_config("rootbox"))
        (self.s.root / ".env").write_text(ENV_NAME + "=" + ROOT_VALUE + "\n")
        box = GpuTransport(require=ROOT_VALUE)
        cards, _health, _ = self.machine(box)
        (card,) = cards
        self.assertEqual([(h["profile"], h["is_global"]) for h in card["holders"]], [("global", True)])
        self.assertEqual(box.sent(), [(GPU + "/metrics", "Bearer " + ROOT_VALUE)])

    def test_a_viewed_homes_own_endpoint_keeps_the_profile_path_and_merges_holders(self):
        (self.s.home / "config.yaml").write_text(gpu_config())
        (self.s.home / ".env").write_text(ENV_NAME + "=" + WORK_VALUE + "\n")
        box = GpuTransport(require=WORK_VALUE)
        cards, health, envelopes = self.machine(box)
        (card,) = cards                                               # one box, two holders
        self.assertEqual(sorted(h["profile"] for h in card["holders"]), ["verifier", "work"])
        sent = {a for _u, a in box.sent()}
        self.assertEqual(sent, {"Bearer " + WORK_VALUE})              # the viewed home's key, never the verifier's
        self.assertTrue({u for u, _a in box.sent()} >= {GPU + "/metrics"})
        self.assertEqual(health[0]["status"], "healthy")
        self.assertEqual(scan_for(envelopes, ALL_VALUES), [])

    def test_the_profile_view_of_another_homes_endpoint_is_inventory_only(self):
        box = GpuTransport(require=GPU_VALUE)
        subs, health = self.deck(box, view="profile")
        self.assertEqual(box.requests, [])
        self.assertEqual(subs["data"]["cards"], [])                   # not even a card: that home is not this one
        self.assertEqual(health["data"]["entries"], [])

    def test_a_restricted_principal_never_gets_the_machine_view(self):
        box = GpuTransport(require=GPU_VALUE)
        subs, _health = self.deck(box, view="machine", profile_only=True)
        self.assertEqual(box.requests, [])
        self.assertEqual(subs["data"]["view"], "profile")
        self.assertEqual(subs["data"]["cards"], [])

    # ------------------------------------------------------------------ bounded + ephemeral

    def test_no_file_is_written_and_nothing_leaks_to_logs_or_sinks(self):
        def foreign(files):  # the viewed home is the host's own business (profile view); everything else is a sibling
            return {p: d for p, d in files.items() if not p.startswith(os.path.join("profiles", "work") + os.sep)}

        before = foreign(tree(self.s.root))
        box = GpuTransport(require=GPU_VALUE)
        with LogCapture() as logs:
            _cards, _health, envelopes = self.machine(box)
        after = tree(self.s.root)
        self.assertEqual(foreign(after), before)    # no auth.json.corrupt, backup, cache or sink file in a sibling
        self.assertEqual(scan_for(envelopes, ALL_VALUES), [])
        for value in ALL_VALUES:
            self.assertNotIn(value, logs.text())
        # the key sits only in the verifier's own .env, exactly where the owner put it
        self.assertEqual([p for p, data in after.items() if GPU_VALUE.encode() in data],
                         [os.path.join("profiles", "verifier", ".env")])

    def test_unsafe_sibling_files_are_never_read(self):
        outside = Path(self.s._tmp.name + "-outside")
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "secret.env").write_text(ENV_NAME + "=" + WORK_VALUE + "\n")
        (self.verifier / ".env").unlink()
        os.symlink(outside / "secret.env", self.verifier / ".env")     # a symlinked .env is not followed
        box = GpuTransport()
        cards, health, _ = self.machine(box)
        self.assertEqual(box.requests, [])
        self.assertEqual(health[0]["reason"], "credentials_missing")

        (self.verifier / ".env").unlink()
        (self.verifier / ".env").write_text(ENV_NAME + "=" + GPU_VALUE + "\n" + "#" + "x" * (2 * 1024 * 1024))  # oversize
        box = GpuTransport()
        self.machine(box)
        self.assertEqual(box.requests, [])                             # bounded: too large to read, so unresolved

    def test_a_malformed_sibling_config_is_skipped_and_never_breaks_the_view(self):
        (self.verifier / "config.yaml").write_text("custom_providers: [unclosed\n  - : : {\n")
        with LogCapture() as logs:
            cards, _health, _ = self.machine(GpuTransport())
        self.assertEqual(cards, [])
        self.assertNotIn(GPU_VALUE, logs.text())

    def test_an_unsafe_sibling_endpoint_is_never_requested(self):
        (self.verifier / "config.yaml").write_text(
            "custom_providers:\n  - name: gpu\n    base_url: http://user:pw@gpu.lan:8000/v1\n    key_env: " + ENV_NAME + "\n")
        box = GpuTransport()
        self.machine(box)
        self.assertEqual(box.requests, [])


class InjectedSiblingLoaders(unittest.TestCase):
    """The same behaviour through injected loaders (no host needed)."""

    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", {"credential_pool": {}})
        self.configs = {"verifier": {"custom_providers": [{"name": "gpu", "base_url": GPU + "/v1", "key_env": ENV_NAME}]}}
        self.envs = {"verifier": {ENV_NAME: GPU_VALUE, "UNRELATED_SECRET": "sntl-unrelated-77aa"}}
        self.calls = []

    def source(self, with_loaders=True):
        def config_loader(directory):
            self.calls.append(("config", Path(directory).name))
            return self.configs.get(Path(directory).name)

        def env_loader(directory):
            self.calls.append(("env", Path(directory).name))
            return self.envs.get(Path(directory).name, {})

        return FileInventorySource(
            self.s.home, self.s.root,
            config_loader=lambda: {"model": {}},
            custom_provider_loader=lambda cfg: cfg.get("custom_providers", []),
            env_lookup=lambda name: None,
            home_config_loader=config_loader if with_loaders else None,
            home_env_loader=env_loader if with_loaders else None,
        )

    def test_sibling_store_carries_the_owning_homes_endpoint_and_a_narrow_env(self):
        stores = {st.profile: st for st in self.source().read_stores()}
        store = stores["verifier"]
        self.assertEqual([e["name"] for e in store.raw.custom_endpoints], ["gpu"])
        self.assertEqual(store.env(ENV_NAME), GPU_VALUE)
        self.assertIsNone(store.env("UNRELATED_SECRET"))               # only variables an endpoint names are retained
        self.assertNotIn(GPU_VALUE, repr(store))
        self.assertEqual((stores["work"].raw.custom_endpoints, stores["work"].env), ([], None))  # viewed home: profile path
        self.assertNotIn(("config", "work"), self.calls)
        self.assertNotIn(("env", "work"), self.calls)

    def test_inventory_item_resolves_the_key_for_metrics_only(self):
        (item,) = discover_machine(self.source())
        self.assertTrue(item.cross_profile and item.live_source)
        self.assertEqual(item.health_credential.reveal(), GPU_VALUE)
        self.assertIsNone(item.credential)                               # the inference credential is never granted
        self.assertIn(GPU_VALUE, item.secrets)                           # but it is registered for redaction
        self.assertEqual(item.holders[0].profile, "verifier")

    def test_without_loaders_a_sibling_config_is_invisible(self):
        self.assertEqual(discover_machine(self.source(with_loaders=False)), [])

    def test_profile_view_discovery_ignores_siblings_entirely(self):
        self.assertEqual(discover(self.source()), [])
        self.assertEqual(self.calls, [])

    def test_a_raising_loader_is_skipped(self):
        def boom(directory):
            raise RuntimeError(GPU_VALUE)

        src = self.source()
        src._home_config_loader = boom
        with LogCapture() as logs:
            self.assertEqual(discover_machine(src), [])
        self.assertNotIn(GPU_VALUE, logs.text())

    def test_credential_free_cloud_primary_of_another_home_is_not_a_subscription(self):
        self.configs["verifier"] = {"model": {"provider": "anthropic"}}
        self.assertEqual(discover_machine(self.source()), [])

    def test_unresolved_key_marks_the_card_expected_and_unrequestable(self):
        self.envs["verifier"] = {}
        (item,) = discover_machine(self.source())
        self.assertTrue(item.credential_expected)
        self.assertIsNone(item.health_credential)
        self.assertEqual(item.auth_status, "missing")


if __name__ == "__main__":
    unittest.main()
