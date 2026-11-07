"""Router integration: relative suffixes, host-style file loading, scratch HERMES_HOME.

Skipped when FastAPI/Hermes are not importable (the bare stdlib environment).
Run with the Hermes dashboard interpreter to exercise them.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    HAVE_FASTAPI = False

try:
    import hermes_cli.config  # noqa: F401
    import hermes_constants  # noqa: F401
    HAVE_HOST = True
except Exception:  # noqa: BLE001
    HAVE_HOST = False

from plugins.usage_deck import plugin_api
from plugins.usage_deck.service import API_BASE, UsageDeckService
from plugins.usage_deck.discovery import FileInventorySource

from .helpers import (
    ALL_SENTINELS,
    NOW,
    SENTINEL_KEY,
    SENTINEL_REFRESH,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
    FakeAnalytics,
    FakeTransport,
    ScratchHome,
    day_row,
    json_response,
    make_jwt,
    scan_for,
)

PLUGIN_API_PATH = Path(plugin_api.__file__)


class BareImportTests(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(API_BASE, "/api/plugins/usage-deck")
        self.assertEqual(sorted(plugin_api.__all__), ["API_BASE", "UsageDeckService", "create_router", "get_service", "router"])

    def test_file_loaded_under_synthetic_name_like_the_host(self):
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_usage-deck", PLUGIN_API_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(spec.name, None)
        self.assertEqual(mod.API_BASE, API_BASE)
        self.assertEqual(mod.router is None, not HAVE_FASTAPI)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class RouterTests(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or1", "access_token": SENTINEL_KEY, "refresh_token": SENTINEL_REFRESH}],
            "anthropic": [{"id": "an1", "access_token": make_jwt({"plan_type": "max"})}]}})
        self.transport = FakeTransport({
            "https://openrouter.ai/api/v1/key": json_response({"data": {"usage": 2, "limit": 4, "limit_remaining": 2}})})
        src = FileInventorySource(self.s.home, None, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [])
        self.service = UsageDeckService(
            inventory_factory=lambda: src,
            analytics_factory=lambda p: FakeAnalytics([day_row("2026-11-06")]),
            transport=self.transport, clock=lambda: NOW)
        app = FastAPI()
        app.include_router(plugin_api.create_router(self.service), prefix=API_BASE)  # as the host mounts it
        self.client = TestClient(app)

    def test_router_paths_are_relative_suffixes(self):
        self.assertEqual([r.path for r in plugin_api.create_router(self.service).routes],
                         ["/subscriptions", "/usage", "/health"])

    def test_three_routes_valid_json_no_secrets_no_store(self):
        for suffix in ("subscriptions", "usage?window=7", "health"):
            r = self.client.get(f"{API_BASE}/{suffix}")
            self.assertEqual(r.status_code, 200, suffix)
            self.assertEqual(r.headers["cache-control"], "no-store")
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertEqual(scan_for(body, ALL_SENTINELS), [], suffix)
        cards = self.client.get(f"{API_BASE}/subscriptions").json()["data"]["cards"]
        self.assertEqual(sorted(c["provider_id"] for c in cards), ["anthropic", "openrouter"])

    def test_old_namespace_is_not_exposed(self):
        self.assertEqual(self.client.get("/api/usage-deck/subscriptions").status_code, 404)
        self.assertEqual(self.client.get("/subscriptions").status_code, 404)

    def test_query_errors_use_plugin_envelope(self):
        for url in (f"{API_BASE}/usage?window=14", f"{API_BASE}/usage?window=7&window=30",
                    f"{API_BASE}/health?x=1", f"{API_BASE}/usage?provider="):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 400, url)
            self.assertEqual(r.json()["error"]["code"], "invalid_query")
        r = self.client.get(f"{API_BASE}/usage?provider=nope")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "provider_not_found"))
        r = self.client.get(f"{API_BASE}/usage?provider=openrouter")
        self.assertEqual(r.json()["data"]["reason"], "attribution_unavailable")

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(f"{API_BASE}/subscriptions").status_code, 405)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class ProfileRestrictedPrincipalTests(unittest.TestCase):
    """DESIGN.md §12.5: the machine view is a full-trust machine-owner inventory. A host principal bound to one
    profile (a native grant: ``request.state.native_http_principal``) stays profile-only; ``?view=`` cannot widen it."""

    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)
        self.s.write_auth({"credential_pool": {}})
        self.s.write_profile("verifier", {"credential_pool": {"nous": [{"id": "n1", "access_token": SENTINEL_TOKEN}]}})
        src = FileInventorySource(self.s.home, self.s.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)
        service = UsageDeckService(inventory_factory=lambda: src, analytics_factory=lambda p: FakeAnalytics(),
                                   transport=FakeTransport(), clock=lambda: NOW)
        app = FastAPI()

        @app.middleware("http")
        async def stamp_principal(request, call_next):  # what the host's native-grant seam does
            if request.headers.get("x-test-native-principal"):
                request.state.native_http_principal = {"profile_id": str(self.s.home)}
            return await call_next(request)

        app.include_router(plugin_api.create_router(service), prefix=API_BASE)
        self.client = TestClient(app)

    def get(self, suffix, restricted):
        headers = {"x-test-native-principal": "1"} if restricted else {}
        return self.client.get(f"{API_BASE}/{suffix}", headers=headers)

    def test_machine_owner_session_enumerates_other_homes(self):
        body = self.get("subscriptions?view=machine", False).json()
        self.assertEqual((body["data"]["view"], [c["provider_id"] for c in body["data"]["cards"]]), ("machine", ["nous"]))
        self.assertEqual(self.get("subscriptions", False).json()["data"]["view"], "machine")  # default stays machine

    def test_profile_restricted_principal_is_forced_to_the_profile_view(self):
        for suffix in ("subscriptions", "subscriptions?view=machine", "subscriptions?view=profile"):
            body = self.get(suffix, True).json()
            self.assertEqual((body["data"]["view"], body["data"]["cards"]), ("profile", []), suffix)
        self.assertEqual(self.get("health?view=machine", True).json()["data"]["entries"], [])
        r = self.get("usage?provider=nous&view=machine", True)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "provider_not_found"))
        r = self.get("subscriptions?view=bogus", True)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_query"))
        self.assertEqual(scan_for(self.get("subscriptions?view=machine", True).json(), [SENTINEL_TOKEN]), [])


@unittest.skipUnless(HAVE_FASTAPI and HAVE_HOST, "needs the Hermes dashboard interpreter")
class HostScratchHomeTests(unittest.TestCase):
    """The real host helpers (home resolution, config loader, auth) on a scratch HERMES_HOME."""

    def setUp(self):
        from hermes_constants import get_default_hermes_root, reset_hermes_home_override, set_hermes_home_override

        # Outside any real ~/.hermes tree, so the host's root rules cannot fall back to real credentials.
        self.s = ScratchHome(base="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self.s.cleanup)
        (self.s.home / "config.yaml").write_text(
            "model:\n  provider: openrouter\ncustom_providers:\n  - name: gpu-box\n    base_url: http://gpu.lan:8000/v1\n"
            f"    api_key: {SENTINEL_SECRET}\n")
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or1", "access_token": SENTINEL_KEY}],
            "openai-codex": [{"id": "c1", "access_token": make_jwt({"plan": "business"}), "refresh_token": SENTINEL_REFRESH},
                             {"id": "c2", "access_token": SENTINEL_TOKEN}]}})
        token = set_hermes_home_override(str(self.s.home))
        self.addCleanup(reset_hermes_home_override, token)
        self.assertNotEqual(get_default_hermes_root(home=self.s.home), Path.home() / ".hermes")
        self.before = {p.name: p.read_bytes() for p in self.s.home.iterdir() if p.is_file()}

    def test_host_usage_series_from_real_session_db(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="s1", source="cli", model="m")
        finally:
            db.close()
        service = UsageDeckService(transport=FakeTransport(), clock=lambda: NOW)
        app = FastAPI()
        app.include_router(plugin_api.create_router(service), prefix=API_BASE)
        r = TestClient(app).get(f"{API_BASE}/usage?window=7")
        self.assertEqual(r.status_code, 200, r.text)
        series = r.json()["data"]["series"]
        self.assertEqual(len(series), 1)
        self.assertEqual((series[0]["scope"], series[0]["confidence"], series[0]["provider_id"]), ("profile", "estimated", None))
        self.assertEqual(len(series[0]["points"]), 1)
        self.assertEqual(series[0]["points"][0]["sessions"], 1)
        # a missing sessions db is analytics_unavailable (503), not a crash
        for name in ("state.db", "state.db-shm", "state.db-wal"):
            (self.s.home / name).unlink(missing_ok=True)
        service2 = UsageDeckService(transport=FakeTransport(), clock=lambda: NOW)
        app2 = FastAPI()
        app2.include_router(plugin_api.create_router(service2), prefix=API_BASE)
        r2 = TestClient(app2).get(f"{API_BASE}/usage?window=7")
        self.assertEqual((r2.status_code, r2.json()["error"]["code"]), (503, "analytics_unavailable"))

    def test_host_sources_through_router(self):
        transport = FakeTransport({
            "https://openrouter.ai/api/v1/key": json_response({"data": {"usage": 1, "limit": None, "limit_remaining": None}}),
            "http://gpu.lan:8000/metrics": json_response({}, status=404),
            "http://gpu.lan:8000/v1/models": json_response({"data": []})})
        service = UsageDeckService(transport=transport, clock=lambda: NOW)  # host inventory + host analytics reader
        app = FastAPI()
        app.include_router(plugin_api.create_router(service), prefix=API_BASE)
        client = TestClient(app)
        subs = client.get(f"{API_BASE}/subscriptions")
        self.assertEqual(subs.status_code, 200, subs.text)
        cards = subs.json()["data"]["cards"]
        self.assertEqual(sorted(c["provider_id"] for c in cards), ["custom:gpu-box", "openai-codex", "openai-codex", "openrouter"])
        health = client.get(f"{API_BASE}/health").json()["data"]["entries"]
        self.assertEqual([(e["status"], e["metrics_available"]) for e in health], [("degraded", False)])
        usage = client.get(f"{API_BASE}/usage?window=7")
        self.assertIn(usage.status_code, (200, 503))  # scratch home has no sessions db; both are valid envelopes
        for r in (subs, usage, client.get(f"{API_BASE}/health")):
            self.assertEqual(scan_for(r.json(), ALL_SENTINELS), [])
            json.loads(r.text)
        after = {p.name: p.read_bytes() for p in self.s.home.iterdir() if p.is_file() and p.name in self.before}
        self.assertEqual(after, self.before)  # read-only: credentials and config untouched
        self.assertFalse((self.s.home / "auth.json.corrupt").exists())


if __name__ == "__main__":
    unittest.main()
