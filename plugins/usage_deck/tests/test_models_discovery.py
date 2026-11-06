import json
import unittest

from plugins.usage_deck.discovery import (
    FileInventorySource,
    InventoryUnavailable,
    discover,
    plan_from_token,
    provider_label_for,
)
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.identity import make_account_id, make_endpoint_id, normalize_endpoint, parse_endpoint
from plugins.usage_deck.models.redaction import (
    REDACTED,
    collect_secret_values,
    contains_secret,
    sanitize,
    scrub_string,
)

from .helpers import (
    SENTINEL_KEY,
    SENTINEL_REFRESH,
    SENTINEL_SECRET,
    SENTINEL_TOKEN,
    ScratchHome,
    make_jwt,
    scan_for,
)


class RedactionTests(unittest.TestCase):
    def test_sensitive_field_names_redacted_case_insensitive_and_nested(self):
        payload = {
            "API_KEY": "a-1234567",
            "Authorization": "Bearer zzz",
            "nested": [{"Refresh_Token": "r-123456", "ok": 1}, {"deep": {"clientSecret": "s-123456", "PassWord": "p"}}],
            "x_access_thing": "v",
            "jwt": "eyJabc",
            "cookie": "c",
            "credentials": {"a": "b"},
            "plain": "visible",
        }
        out = sanitize(payload)
        self.assertEqual(out["API_KEY"], REDACTED)
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["nested"][0]["Refresh_Token"], REDACTED)
        self.assertEqual(out["nested"][0]["ok"], 1)
        self.assertEqual(out["nested"][1]["deep"]["clientSecret"], REDACTED)
        self.assertEqual(out["nested"][1]["deep"]["PassWord"], REDACTED)
        for k in ("x_access_thing", "jwt", "cookie", "credentials"):
            self.assertEqual(out[k], REDACTED, k)
        self.assertEqual(out["plain"], "visible")

    def test_input_tokens_wire_name_is_redacted_so_counts_must_be_mapped(self):
        self.assertEqual(sanitize({"input_tokens": 5})["input_tokens"], REDACTED)
        self.assertEqual(sanitize({"input_count": 5})["input_count"], 5)

    def test_exact_secret_values_scrubbed_under_generic_names(self):
        out = sanitize({"label": f"hello {SENTINEL_KEY}!", "list": [SENTINEL_TOKEN], "n": {"msg": SENTINEL_SECRET}},
                       [SENTINEL_KEY, SENTINEL_TOKEN, SENTINEL_SECRET])
        self.assertEqual(scan_for(out, [SENTINEL_KEY, SENTINEL_TOKEN, SENTINEL_SECRET]), [])
        self.assertIn(REDACTED, out["label"])

    def test_short_secrets_of_any_length_are_scrubbed(self):
        # DESIGN.md §2.1: every nonempty private secret, however short (synthetic sentinels only).
        for short in ("abc", "qx", "z"):
            with self.subTest(secret=short):
                secrets = collect_secret_values({"access_token": short})
                self.assertEqual(secrets, frozenset({short}))
                out = sanitize({"label": short, "msg": f"x {short} y", "list": [short], "n": {"v": short}}, secrets)
                self.assertEqual(out["label"], REDACTED)
                self.assertNotIn(short, out["msg"])
                self.assertEqual(out["list"], [REDACTED])
                self.assertEqual(out["n"]["v"], REDACTED)
                self.assertTrue(contains_secret(short, secrets))
                self.assertEqual(scrub_string(f"a{short}b", secrets), f"a{REDACTED}b")
        self.assertEqual(collect_secret_values({"access_token": "", "api_key": ["", "qx"]}), frozenset({"qx"}))
        self.assertEqual(scrub_string("plain text", [""]), "plain text")  # empty secret never matches

    def test_short_secret_keeps_numeric_counts(self):
        out = sanitize({"input_count": 7, "output_count": 70, "ratio": 0.7, "ok": True, "none": None, "label": "7"}, ["7"])
        self.assertEqual((out["input_count"], out["output_count"], out["ratio"]), (7, 70, 0.7))
        self.assertIs(out["ok"], True)
        self.assertIsNone(out["none"])
        self.assertEqual(out["label"], REDACTED)

    def test_patterns_and_urls(self):
        text = scrub_string("Authorization: Bearer abc.def-ghi and sk-abcdefghijkl and " + make_jwt({"a": 1})
                            + " at https://user:pw@host.example:8080/p?token=abc#frag end")
        self.assertNotIn("abc.def-ghi", text)
        self.assertNotIn("sk-abcdefghijkl", text)
        self.assertNotIn("eyJ", text)
        self.assertNotIn("user:pw", text)
        self.assertNotIn("token=abc", text)
        self.assertNotIn("#frag", text)
        self.assertIn("https://host.example:8080/p", text)

    def test_exception_text_cannot_carry_secret(self):
        try:
            raise RuntimeError(f"auth failed for {SENTINEL_KEY}")
        except RuntimeError as exc:
            out = sanitize({"detail": str(exc)}, [SENTINEL_KEY])
        self.assertNotIn(SENTINEL_KEY, json.dumps(out))

    def test_rejects_non_json_and_non_finite(self):
        for bad in (object(), {1: 2}, float("nan"), float("inf"), {"a": {"b": {1, 2}}}):
            with self.assertRaises(TypeError):
                sanitize(bad)

    def test_collect_secret_values(self):
        found = collect_secret_values({"access_token": SENTINEL_TOKEN, "meta": {"api_key": [SENTINEL_KEY]}, "id": "abcdef"})
        self.assertEqual(found, frozenset({SENTINEL_TOKEN, SENTINEL_KEY}))


class DtoTests(unittest.TestCase):
    def test_errors_use_fixed_text(self):
        err = dto.make_error("timeout", "p", "a")
        self.assertEqual((err.message, err.retryable), (dto.ERROR_MESSAGES["timeout"], True))
        with self.assertRaises(dto.DtoError):
            dto.make_error("raw_upstream_text")

    def test_validation(self):
        with self.assertRaises(dto.DtoError):
            dto.UsageMeter("i", "l", float("nan"), None, None, "usd", "api_credential", dto.UNKNOWN_PERIOD, "exact", "none", None)
        with self.assertRaises(dto.DtoError):
            dto.UsageMeter("i", "l", -1, None, None, "usd", "api_credential", dto.UNKNOWN_PERIOD, "exact", "none", None)
        with self.assertRaises(dto.DtoError):
            dto.UsageMeter("i", "l", 1, None, None, "eur", "api_credential", dto.UNKNOWN_PERIOD, "exact", "none", None)
        with self.assertRaises(dto.DtoError):
            dto.UsagePoint("2026-1-1", None, None, None, None, None, "estimated")
        with self.assertRaises(dto.DtoError):
            dto.UsagePoint("2026-01-01", True, None, None, None, None, "estimated")

    def test_every_field_is_serialized_including_nulls(self):
        entry = dto.HealthStripEntry("ep", "p", "n", "unknown", "opaque", False, None, None, None,
                                     "2026-11-06T00:00:00Z", False, None, None)
        self.assertEqual(set(dto.to_plain(entry)), {
            "endpoint_id", "provider_id", "display_name", "status", "confidence", "metrics_available",
            "latency_ms", "cpu_percent", "memory_bytes", "checked_at", "stale", "reason", "error"})

    def test_series_points_must_ascend(self):
        a = dto.UsagePoint("2026-01-02", 1, 1, 0.0, 1, 1, "estimated")
        b = dto.UsagePoint("2026-01-01", 1, 1, 0.0, 1, 1, "estimated")
        with self.assertRaises(dto.DtoError):
            dto.UsageTimeSeries("s", None, None, "profile", "estimated", "session_analytics", dto.UNKNOWN_PERIOD,
                                30, "day", "server_local", "partial", (a, b), "2026-11-06T00:00:00Z", False, None)


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_endpoint("HTTP://User:pw@Host.Example:8000/v1/?q=1#f"), "http://host.example:8000/v1")
        self.assertEqual(normalize_endpoint("https://[::1]:9/"), "https://[::1]:9")
        for bad in ("ftp://x", "", None, "host:80", "http://", "http://h:99999"):
            self.assertIsNone(normalize_endpoint(bad), bad)

    def test_unsafe_urls_flagged(self):
        self.assertFalse(parse_endpoint("http://u:p@h/v1").safe)
        self.assertFalse(parse_endpoint("http://h/v1?api_key=abc").safe)
        self.assertTrue(parse_endpoint("http://h/v1?x=1").safe)

    def test_ids_are_stable_and_secret_free(self):
        a = make_account_id("openai-codex", None, "ab12")
        self.assertRegex(a, r"^acct_[0-9a-f]{24}$")
        self.assertEqual(a, make_account_id("openai-codex", None, "ab12"))
        self.assertNotEqual(a, make_account_id("openai-codex", None, "ab13"))
        self.assertNotEqual(make_account_id("openai-codex", None, 1), make_account_id("openai-codex", None, "1"))
        self.assertRegex(make_endpoint_id("ollama", "http://h:1"), r"^ep_[0-9a-f]{24}$")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.s = ScratchHome()
        self.addCleanup(self.s.cleanup)

    def source(self, config=None, custom=None, env=None, with_global=True):
        return FileInventorySource(
            self.s.home, self.s.root if with_global else None,
            config_loader=(lambda: config) if config is not None else None,
            custom_provider_loader=(lambda c: custom or []) if config is not None else None,
            env_lookup=(lambda n: (env or {}).get(n)),
        )

    def test_missing_files_are_empty_inventory(self):
        self.assertEqual(discover(self.source()), [])

    def test_two_accounts_one_provider_one_card_each(self):
        self.s.write_auth({"credential_pool": {"openai-codex": [
            {"id": "aa11", "access_token": make_jwt({"plan": "business"})},
            {"id": "bb22", "access_token": make_jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "go"}})}]}})
        items = discover(self.source())
        self.assertEqual(len(items), 2)
        self.assertEqual(len({i.account_id for i in items}), 2)
        self.assertEqual(sorted(i.account_label for i in items), ["Account 1", "Account 2"])
        self.assertEqual(sorted(i.plan_name for i in items), ["business", "go"])
        self.assertTrue(all(i.plan_source == "unverified_claim" and i.auth_status == "available" for i in items))
        self.assertTrue(all(i.endpoint_id is None and i.kind == "cloud" for i in items))

    def test_profile_pool_shadows_global_per_provider_only(self):
        self.s.write_auth({"credential_pool": {"openrouter": [{"id": "p1", "access_token": SENTINEL_KEY}]}})
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "g1", "access_token": "g-key-aaaa"}, {"id": "g2", "access_token": "g-key-bbbb"}],
            "anthropic": [{"id": "g3", "access_token": "g-key-cccc"}]}}, where="global")
        items = discover(self.source())
        providers = sorted(i.provider_id for i in items)
        self.assertEqual(providers, ["anthropic", "openrouter"])  # profile openrouter replaces BOTH global rows
        self.assertEqual(len([i for i in items if i.provider_id == "openrouter"]), 1)
        no_global = discover(self.source(with_global=False))
        self.assertEqual([i.provider_id for i in no_global], ["openrouter"])

    def test_legacy_providers_only_without_pool(self):
        self.s.write_auth({"providers": {"nous": {"access_token": SENTINEL_TOKEN}, "openrouter": {"api_key": "legacy-key-1"}},
                           "credential_pool": {"openrouter": [{"id": "r1", "access_token": SENTINEL_KEY}]}})
        items = discover(self.source())
        self.assertEqual(sorted(i.provider_id for i in items), ["nous", "openrouter"])
        self.assertEqual(len(items), 2)

    def test_malformed_auth_json_is_inventory_unavailable_without_leaking(self):
        self.s.write_auth('{"credential_pool": {"x": ' + SENTINEL_KEY)
        with self.assertRaises(InventoryUnavailable) as cm:
            discover(self.source())
        self.assertNotIn(SENTINEL_KEY, str(cm.exception))
        self.assertNotIn(str(self.s.home), str(cm.exception))
        self.s.write_auth("[1, 2]")
        with self.assertRaises(InventoryUnavailable):
            discover(self.source())
        # no corrupt backup was written by the read
        self.assertEqual([p.name for p in self.s.home.iterdir()], ["auth.json"])

    def test_malformed_global_store_is_ignored(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n", "access_token": "t-123456"}]}})
        self.s.write_auth("{broken", where="global")
        self.assertEqual(len(discover(self.source())), 1)

    def test_config_only_custom_endpoint_and_primary_without_credentials(self):
        cfg = {"model": {"provider": "openrouter"}}
        custom = [{"name": "Lab-Box", "base_url": "http://gpu.lan:8000/v1/"}]
        items = discover(self.source(cfg, custom))
        by = {i.provider_id: i for i in items}
        self.assertEqual(set(by), {"openrouter", "custom:Lab-Box"})
        self.assertEqual((by["openrouter"].auth_status, by["openrouter"].kind), ("missing", "cloud"))
        lab = by["custom:Lab-Box"]
        self.assertEqual((lab.kind, lab.auth_status, lab.provider_label), ("self_hosted", "missing", "Lab-Box"))
        self.assertEqual(lab.endpoint.normalized, "http://gpu.lan:8000/v1")
        self.assertRegex(lab.endpoint_id, r"^ep_")

    def test_pool_row_and_config_endpoint_are_one_item(self):
        self.s.write_auth({"credential_pool": {"custom:lab-box": [
            {"id": "c1", "access_token": SENTINEL_KEY, "base_url": "http://GPU.lan:8000/v1"}]}})
        custom = [{"name": "Lab-Box", "base_url": "http://gpu.lan:8000/v1"}]
        items = discover(self.source({"model": {}}, custom))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].provider_id, "custom:lab-box")  # stored id preserved literally

    def test_pool_row_without_endpoint_adopts_unique_config_endpoint(self):
        self.s.write_auth({"credential_pool": {"custom:lab": [{"id": "c1", "access_token": SENTINEL_KEY}]}})
        items = discover(self.source({"model": {}}, [{"name": "lab", "base_url": "http://gpu.lan:8000"}]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].endpoint.normalized, "http://gpu.lan:8000")

    def test_duplicate_stored_row_ids_deduped_not_same_provider_accounts(self):
        self.s.write_auth({"credential_pool": {"anthropic": [
            {"id": "same", "access_token": "t-aaaaaa"}, {"id": "same", "access_token": "t-bbbbbb"},
            {"id": "other", "access_token": "t-cccccc"}]}})
        self.assertEqual(len(discover(self.source())), 2)

    def test_ollama_primary_endpoint(self):
        items = discover(self.source({"model": {"provider": "ollama", "base_url": "http://localhost:11434/"}}))
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].kind, items[0].endpoint.normalized), ("self_hosted", "http://localhost:11434"))

    def test_openrouter_env_credential_resolved_for_adapter_only(self):
        items = discover(self.source({"model": {"provider": "openrouter"}}, env={"OPENROUTER_API_KEY": SENTINEL_KEY}))
        self.assertEqual(items[0].auth_status, "available")
        self.assertEqual(items[0].credential.reveal(), SENTINEL_KEY)
        self.assertNotIn(SENTINEL_KEY, repr(items[0]))
        self.assertIn(SENTINEL_KEY, items[0].secrets)

    def test_secret_looking_provider_names_get_safe_labels(self):
        self.s.write_auth({"credential_pool": {"custom:" + SENTINEL_TOKEN: [{"id": "x1", "access_token": SENTINEL_TOKEN}]}})
        items = discover(self.source())
        self.assertEqual(items[0].provider_label, "Provider")

    def test_short_secret_provider_names_and_row_ids_are_rejected(self):
        # Labels equal to a short private secret fall back to the fixed "Provider"/"Account N" text.
        self.s.write_auth({"credential_pool": {"custom:abc": [{"id": "abc", "access_token": "abc"}]}})
        items = discover(self.source())
        self.assertEqual(items[0].provider_label, "Provider")
        self.assertEqual(items[0].account_label, "Account 1")
        self.assertNotIn("abc", items[0].account_id)
        self.assertEqual(provider_label_for("custom:qx", frozenset({"qx"})), "Provider")
        self.assertEqual(provider_label_for("custom:lab", frozenset({"zq"})), "lab")

    def test_ids_never_derive_from_tokens(self):
        self.s.write_auth({"credential_pool": {"anthropic": [{"access_token": "tok-one-111111"}]}})
        first = discover(self.source())[0].account_id
        self.s.write_auth({"credential_pool": {"anthropic": [{"access_token": "tok-two-222222"}]}})
        self.assertEqual(first, discover(self.source())[0].account_id)  # anonymous row: ordinal-based

    def test_config_loader_failure_is_inventory_unavailable(self):
        def boom():
            raise ValueError(SENTINEL_KEY)

        src = FileInventorySource(self.s.home, None, config_loader=boom)
        with self.assertRaises(InventoryUnavailable) as cm:
            discover(src)
        self.assertNotIn(SENTINEL_KEY, str(cm.exception))


class PlanClaimTests(unittest.TestCase):
    def test_allowlist_and_providers(self):
        self.assertEqual(plan_from_token("anthropic", make_jwt({"plan_type": "max"})), "max")
        self.assertIsNone(plan_from_token("openrouter", make_jwt({"plan_type": "max"})))
        self.assertIsNone(plan_from_token("anthropic", make_jwt({"plan_type": "<script>alert(1)</script>"})))
        self.assertIsNone(plan_from_token("anthropic", make_jwt({"plan_type": "x" * 40})))
        self.assertIsNone(plan_from_token("anthropic", make_jwt({"sub": "someone@example.com"})))

    def test_malformed_jwt(self):
        for tok in ("", None, "abc", "a.b.c", "eyJ.eyJ.x", "a." + "!!!" + ".c", "x" * 20000):
            self.assertIsNone(plan_from_token("openai-codex", tok), tok)


if __name__ == "__main__":
    unittest.main()
