"""Masking must preserve the DESIGN.md §2 wire schema exactly (not merely hide secrets).

Regression for: ``sanitize({'label': 'Safe', 'confidence': 'opaque'}, {'el'})`` used to return
``{'lab[REDACTED]': 'Safe', ...}``. Every assertion here compares the full key set / enum values
against an independent, hand-written copy of the contract rather than only scanning for the secret.
"""

import json
import re
import unittest
from unittest import mock

from plugins.usage_deck.adapters.cloud import OPENROUTER_KEY_URL
from plugins.usage_deck.models import dto
from plugins.usage_deck.models.dto import DtoError, check_envelope, check_wire
from plugins.usage_deck.models.redaction import REDACTED, is_sensitive_name, sanitize

from .helpers import json_response, make_jwt, raw_response, scan_for
from .test_service import GPU, METRICS, ServiceBase

# Independent copy of DESIGN.md §2 field lists (deliberately not derived from dto.py).
CARD_KEYS = {"id", "provider_id", "account_id", "provider_label", "account_label", "kind", "plan_name",
             "plan_source", "auth_status", "endpoint_id", "state", "confidence", "reason", "meters",
             "checked_at", "stale", "error", "debug", "holders", "last_seen_at"}
HOLDER_KEYS = {"profile", "is_global"}
METER_KEYS = {"id", "label", "value", "limit", "remaining", "unit", "scope", "period", "confidence",
              "source", "observed_at", "resets_at"}
PERIOD_KEYS = {"start", "end", "label"}
ERROR_KEYS = {"code", "message", "retryable", "provider_id", "account_id"}
DEBUG_KEYS = {"adapter", "capabilities", "source", "sanitized", "skipped_slots"}
POINT_KEYS = {"day", "input_count", "output_count", "estimated_usd", "sessions", "api_calls", "confidence",
              "api_equivalent_usd", "api_equivalent"}
SERIES_KEYS = {"id", "provider_id", "account_id", "scope", "confidence", "source", "period", "window_days",
               "bucket", "timezone", "coverage", "points", "checked_at", "stale", "error"}
# Usage v2 (analytics-dimensions-breakdown §4): exact key sets, deliberately independent of dto.py.
USAGE_KEYS = {"window_days", "mode", "dimension", "provider_id", "series", "groups", "rows", "totals", "providers",
              "sources", "complete", "top_n", "reason", "pricing",
              # analytics-depth additions (analytics-depth-breakdown §2): always emitted, null when unavailable
              "durations", "duration_samples", "heatmap", "cache_metrics", "repo_rollup", "model_rollup",
              "depth_totals", "depth_status"}
DEPTH_KEYS = {"durations", "duration_samples", "heatmap", "cache_metrics", "repo_rollup", "model_rollup",
              "depth_totals", "depth_status"}
DURATION_KEYS = {"p50", "p90", "p99", "mean", "max"}
HEATMAP_KEYS = {"grid", "starts"}
HEATCELL_KEYS = {"sessions", "total_tokens"}
HEATSTART_KEYS = {"started_at", "sessions", "total_tokens"}
CACHE_ROW_KEYS = {"model", "provider_id", "hit_rate_pct", "cached_input_tokens", "uncached_input_tokens",
                  "reasoning_tokens", "effective_cost_per_mtok", "cache_write_tokens", "output_tokens",
                  "reasoning_share_pct", "discount_multiplier", "rate_source", "reason"}
CACHE_REASONS = {None, "missing_usage", "missing_cost", "unknown_discount", "zero_denominator", "incomplete_source"}
REPO_ROW_KEYS = {"repo_basename", "full_path", "sessions", "total_tokens", "total_cost", "full_paths"}
MODEL_ROW_KEYS = {"model", "provider_id", "sessions", "total_tokens", "total_cost"}
DEPTH_TOTALS_KEYS = {"sessions", "total_tokens", "total_cost"}
DEPTH_STATUS_KEYS = {"partial", "excluded_started_at", "missing"}
DEPTH_MISSING_KEYS = {"input_tokens", "output_tokens", "estimated_cost_usd", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens", "duration"}
#: Token-counter names are admitted only inside the depth objects (never listed as global wire keys).
DEPTH_COUNTER_NAMES = {"total_tokens", "cached_input_tokens", "uncached_input_tokens", "reasoning_tokens",
                       "cache_write_tokens", "output_tokens", "input_tokens", "cache_read_tokens"}
METRICS_KEYS = {"input_count", "output_count", "estimated_usd", "sessions", "api_calls", "missing_fields",
                "api_equivalent_usd", "api_equivalent"}
# API-equivalent pricing annotation (docs/design/api-equivalent-pricing.md §7), independent of dto.py.
API_EQUIVALENT_KEYS = {"amount_pico_usd", "priced_rows", "unpriced_rows", "proxy_rows", "partial", "stale"}
ROW_PRICING_KEYS = {"catalog_key", "method", "alias_kind", "note", "reason", "source", "fetched_at", "stale"}
DATA_PRICING_KEYS = {"catalog_revision", "origin", "warnings", "unknown_row_ids"}
ROW_KEYS = {"id", "day", "profile", "is_global", "source", "model_id", "provider_id", "attribution_source", "metrics",
            "pricing", "repo_basename", "repo_paths"}
GROUP_KEYS = {"id", "label", "series_id", "model_id", "provider_ids", "profiles", "row_ids", "totals", "is_other",
              "durations", "duration_samples"}
SOURCE_KEYS = {"profile", "is_global", "state"}
PROVIDER_KEYS = {"id", "label"}
HEALTH_KEYS = {"endpoint_id", "provider_id", "display_name", "status", "confidence", "metrics_available",
               "latency_ms", "cpu_percent", "memory_bytes", "checked_at", "stale", "reason", "error", "last_seen_at",
               "profile_owned"}
META_KEYS = {"schema_version", "request_id", "generated_at", "partial", "errors"}
ENUMS = {
    "confidence": {"exact", "estimated", "opaque", "self_hosted"},
    "state": {"ready", "error", "opaque", "empty"},
    "reason": {"unsupported", "credentials_missing", "permission_denied", "attribution_unavailable",
               "no_observations", "metrics_unavailable", "source_unavailable", "box_offline", None},
    "kind": {"cloud", "self_hosted"},
    "plan_source": {"unverified_claim", "provider", "none"},
    "auth_status": {"available", "missing", "rejected"},
    "unit": {"usd", "count", "percent", "bytes", "credit"},
    "scope": {"api_credential", "provider_account", "endpoint", "profile", "provider", "account", "machine"},
    "source": {"openrouter_key", "session_analytics", "prometheus", "anthropic_headers", "anthropic_oauth_usage",
                "chatgpt_wham_usage", "none"},
    "view": {"machine", "profile"},
    "status": {"healthy", "degraded", "offline", "unknown"},
    "inventory_state": {"empty", "detected"},
    "bucket": {"day"}, "timezone": {"server_local"}, "coverage": {"partial", "complete"},
    "code": {"invalid_query", "provider_not_found", "auth_required", "permission_denied", "timeout",
             "upstream_unavailable", "malformed_response", "inventory_unavailable",
             "analytics_unavailable", "internal_error"},
}
FREE_TEXT_KEYS = ("provider_label", "account_label", "plan_name", "display_name")
ISO_Z = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def walk(obj, path=()):
    """Yield (path, key, value) for every mapping entry, recursing through lists."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield path, key, value
            yield from walk(value, path + (key,))
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item, path)


def shape(obj):
    """Structure signature: sorted key sets, list element shapes; leaf values ignored."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(v) for v in obj]
    return None


class SchemaAsserts(unittest.TestCase):
    """Exact-schema assertions (no tests of its own; mixed into the service tests below)."""

    def assert_exact_card(self, c):
        self.assertEqual(set(c), CARD_KEYS)
        self.assertEqual(set(c["debug"]), DEBUG_KEYS)
        for m in c["meters"]:
            self.assertEqual(set(m), METER_KEYS)
            self.assertEqual(set(m["period"]), PERIOD_KEYS)
            self.assertTrue(m["resets_at"] is None or re.match(ISO_Z, m["resets_at"]))
        for holder in c["holders"]:
            self.assertEqual(set(holder), HOLDER_KEYS)
            self.assertIsInstance(holder["is_global"], bool)
        if c["error"] is not None:
            self.assertEqual(set(c["error"]), ERROR_KEYS)
        for k in ("kind", "plan_source", "auth_status", "state", "confidence", "reason"):
            self.assertIn(c[k], ENUMS[k], k)
        self.assertRegex(c["checked_at"], ISO_Z)
        self.assertRegex(c["account_id"], r"^acct_[0-9a-f]{24}$")
        self.assertEqual(c["id"], c["account_id"])
        for m in c["meters"]:
            for k in ("unit", "scope", "confidence", "source"):
                self.assertIn(m[k], ENUMS[k], k)
        check_wire("card", c)

    def assert_exact_health(self, h):
        self.assertEqual(set(h), HEALTH_KEYS)
        for k in ("status", "confidence", "reason"):
            self.assertIn(h[k], ENUMS[k], k)
        self.assertRegex(h["endpoint_id"], r"^ep_[0-9a-f]{24}$")
        self.assertRegex(h["checked_at"], ISO_Z)
        check_wire("health", h)

    def assert_exact_series(self, s):
        self.assertEqual(set(s), SERIES_KEYS)
        self.assertEqual(set(s["period"]), PERIOD_KEYS)
        for p in s["points"]:
            self.assertEqual(set(p), POINT_KEYS)
            self.assertRegex(p["day"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn(p["confidence"], ENUMS["confidence"])
            self.assert_exact_api_equivalent(p)
        for k in ("scope", "confidence", "source", "bucket", "timezone", "coverage"):
            self.assertIn(s[k], ENUMS[k], k)
        self.assertRegex(s["period"]["label"], r"^rolling_(7|30|90)d$")
        check_wire("series", s)

    def assert_exact_api_equivalent(self, m):
        """``api_equivalent_usd`` is a finite nonnegative number or null; the summary is exactly six fields."""
        usd, api = m["api_equivalent_usd"], m["api_equivalent"]
        self.assertTrue(usd is None or (isinstance(usd, (int, float)) and not isinstance(usd, bool) and usd >= 0))
        self.assertEqual(set(api), API_EQUIVALENT_KEYS)
        self.assertTrue(api["amount_pico_usd"] is None or re.match(r"^(0|[1-9][0-9]*)$", api["amount_pico_usd"]))
        self.assertEqual(usd is None, api["amount_pico_usd"] is None)
        for k in ("priced_rows", "unpriced_rows", "proxy_rows"):
            self.assertTrue(type(api[k]) is int and api[k] >= 0, k)
        self.assertIs(api["partial"], api["unpriced_rows"] > 0)
        self.assertIsInstance(api["stale"], bool)

    def assert_exact_metrics(self, m):
        self.assertEqual(set(m), METRICS_KEYS)
        for k in ("input_count", "output_count", "sessions", "api_calls"):
            self.assertTrue(m[k] is None or (type(m[k]) is int and m[k] >= 0), k)
        self.assertTrue(m["estimated_usd"] is None or (isinstance(m["estimated_usd"], (int, float)) and m["estimated_usd"] >= 0))
        self.assertEqual(m["missing_fields"], sorted(set(m["missing_fields"])))
        self.assertTrue(set(m["missing_fields"]) <= {"input_count", "output_count", "estimated_usd", "sessions", "api_calls"})
        self.assert_exact_api_equivalent(m)

    def assert_exact_usage_objects(self, data):
        self.assert_exact_metrics(data["totals"])
        self.assertEqual(set(data["pricing"]), DATA_PRICING_KEYS)
        self.assertRegex(data["pricing"]["catalog_revision"], r"^[0-9a-f]{64}$")
        self.assertIn(data["pricing"]["origin"], ("seed", "cache", "network"))
        self.assertEqual(data["pricing"]["unknown_row_ids"],
                         sorted(r["id"] for r in data["rows"] if r["metrics"]["api_equivalent"]["amount_pico_usd"] is None))
        for r in data["rows"]:
            self.assertEqual(set(r), ROW_KEYS)
            self.assertEqual(set(r["pricing"]), ROW_PRICING_KEYS)
            self.assertIn(r["pricing"]["method"], ("alias", "heuristic", "none"))
            self.assertRegex(r["id"], r"^row_[0-9a-f]{64}$")
            self.assertEqual(r["source"], "state.db")
            self.assertIn(r["attribution_source"], ("session_provider", "billing_provider", "models_json", "unknown"))
            self.assertIsInstance(r["is_global"], bool)
            self.assert_exact_metrics(r["metrics"])
        for g in data["groups"]:
            self.assertEqual(set(g), GROUP_KEYS)
            self.assertIsInstance(g["is_other"], bool)
            for h in g["profiles"]:
                self.assertEqual(set(h), HOLDER_KEYS)
            self.assert_exact_metrics(g["totals"])
        for o in data["providers"]:
            self.assertEqual(set(o), PROVIDER_KEYS)
        for src in data["sources"]:
            self.assertEqual(set(src), SOURCE_KEYS)
            self.assertIn(src["state"], ("ok", "absent", "unavailable", "excluded"))
        self.assertEqual([g["series_id"] for g in data["groups"]], [s["id"] for s in data["series"]])
        grouped = sorted(rid for g in data["groups"] for rid in g["row_ids"])
        self.assertEqual(grouped, sorted(r["id"] for r in data["rows"]))  # every row in exactly one group
        self.assert_exact_depth(data)

    def assert_exact_depth(self, data):
        """Independent copy of the analytics-depth contract (design §2): exact keys, types, null doctrine."""
        def count(v, nullable=True):
            return (v is None and nullable) or (type(v) is int and v >= 0)

        def num(v):
            return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 and v == v
                                 and v not in (float("inf"),))

        def stats_ok(d, n):
            self.assertEqual(set(d), DURATION_KEYS)
            self.assertTrue(all(num(v) for v in d.values()))
            self.assertTrue(count(n))
            if n == 0:
                self.assertTrue(all(v is None for v in d.values()))
            else:
                self.assertTrue(all(v is not None for v in d.values()))

        for g in data["groups"]:
            self.assertEqual(g["durations"] is None, g["duration_samples"] is None)
            if g["durations"] is not None:
                stats_ok(g["durations"], g["duration_samples"])
        for r in data["rows"]:
            self.assertTrue(r["repo_basename"] is None or isinstance(r["repo_basename"], str))
            self.assertTrue(r["repo_paths"] is None or all(isinstance(p, str) for p in r["repo_paths"]))
        if data["durations"] is None:
            self.assertEqual({k: data[k] for k in ("duration_samples", "heatmap", "cache_metrics", "repo_rollup",
                                                   "model_rollup", "depth_totals")},
                             dict.fromkeys(("duration_samples", "heatmap", "cache_metrics", "repo_rollup",
                                            "model_rollup", "depth_totals")))
            self.assertEqual(set(data["depth_status"]), DEPTH_STATUS_KEYS)
            self.assertIs(data["depth_status"]["partial"], True)
            return
        stats_ok(data["durations"], data["duration_samples"])
        heat = data["heatmap"]
        self.assertEqual(set(heat), HEATMAP_KEYS)
        self.assertEqual((len(heat["grid"]), {len(row) for row in heat["grid"]}), (7, {24}))
        cells = [c for row in heat["grid"] for c in row]
        for c in cells:
            self.assertEqual(set(c), HEATCELL_KEYS)
            self.assertTrue(count(c["sessions"], False) and count(c["total_tokens"]))
        self.assertEqual(sum(s["sessions"] for s in heat["starts"]), sum(c["sessions"] for c in cells))
        self.assertEqual([s["started_at"] for s in heat["starts"]], sorted({s["started_at"] for s in heat["starts"]}))
        for s in heat["starts"]:
            self.assertEqual(set(s), HEATSTART_KEYS)
        for row in data["cache_metrics"]:
            self.assertEqual(set(row), CACHE_ROW_KEYS)
            self.assertIn(row["reason"], CACHE_REASONS)
            for k in ("hit_rate_pct", "reasoning_share_pct"):
                self.assertTrue(row[k] is None or 0 <= row[k] <= 100, k)
            self.assertTrue(num(row["effective_cost_per_mtok"]) and num(row["discount_multiplier"]))
        for row in data["repo_rollup"]:
            self.assertEqual(set(row), REPO_ROW_KEYS)
            self.assertTrue(row["full_path"] is None or row["full_paths"] == [row["full_path"]])
        for row in data["model_rollup"]:
            self.assertEqual(set(row), MODEL_ROW_KEYS)
        self.assertEqual(set(data["depth_totals"]), DEPTH_TOTALS_KEYS)
        status = data["depth_status"]
        self.assertEqual(set(status), DEPTH_STATUS_KEYS)
        self.assertEqual(set(status["missing"]), DEPTH_MISSING_KEYS)
        # Conservation (design §6) restated independently of the code under test.
        tot = data["depth_totals"]
        for rows in (data["repo_rollup"], data["model_rollup"]):
            self.assertEqual(sum(r["sessions"] for r in rows), tot["sessions"])
            toks = [r["total_tokens"] for r in rows if r["total_tokens"] is not None]
            self.assertEqual(sum(toks) if toks else None, tot["total_tokens"])

    def assert_exact_envelope(self, endpoint, status, body):
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"ok", "data", "error", "meta"})
        self.assertEqual(set(body["meta"]), META_KEYS)
        self.assertEqual(body["meta"]["schema_version"], 2 if endpoint == "usage" else 1)  # usage only is v2
        self.assertRegex(body["meta"]["generated_at"], ISO_Z)
        self.assertRegex(body["meta"]["request_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        data = body["data"]
        if endpoint == "subscriptions":
            self.assertEqual(set(data), {"cards", "inventory_state", "view"})
            self.assertIn(data["inventory_state"], ENUMS["inventory_state"])
            self.assertIn(data["view"], ENUMS["view"])
            for c in data["cards"]:
                self.assert_exact_card(c)
        elif endpoint == "health":
            self.assertEqual(set(data), {"entries"})
            for h in data["entries"]:
                self.assert_exact_health(h)
        else:
            self.assertEqual(set(data), USAGE_KEYS)
            self.assertIn(data["window_days"], (7, 30, 90))
            self.assertIn(data["reason"], (None, "no_observations", "source_unavailable"))
            self.assertIn(data["mode"], ENUMS["view"])
            self.assertIn(data["dimension"], ("all", "profile", "model", "repo"))
            self.assertIsInstance(data["complete"], bool)
            self.assertEqual(data["top_n"], 8 if data["dimension"] == "model" else None)
            for s in data["series"]:
                self.assert_exact_series(s)
            self.assert_exact_usage_objects(data)
        check_envelope(body)


class SanitizeSchemaTests(unittest.TestCase):
    def test_reported_repro_keeps_canonical_keys(self):
        out = sanitize({"label": "Safe", "confidence": "opaque"}, {"el"})
        self.assertEqual(out, {"label": "Safe", "confidence": "opaque"})
        self.assertEqual(list(out), ["label", "confidence"])

    def test_free_text_values_are_still_masked(self):
        out = sanitize({"label": "Selfish", "plan_name": "el-plan", "account_label": "el"}, {"el"})
        self.assertEqual(list(out), ["label", "plan_name", "account_label"])
        self.assertEqual(out["label"], f"S{REDACTED}fish")
        self.assertEqual(out["plan_name"], f"{REDACTED}-plan")
        self.assertEqual(out["account_label"], REDACTED)

    def test_private_raw_keys_are_not_exempt(self):
        raw = {"private_el_name": "x", "label": "v", "nested": {"zzel": 1, "api_key": "k", "state": "elsewhere"}}
        out = sanitize(raw, {"el"})
        self.assertNotIn("private_el_name", out)
        self.assertEqual(out[f"private_{REDACTED}_name"], "x")
        self.assertEqual(out["nested"][f"zz{REDACTED}"], 1)
        self.assertEqual(out["nested"]["api_key"], REDACTED)  # sensitive names still redacted
        self.assertEqual(out["nested"]["state"], f"{REDACTED}sewhere")  # non-vocabulary value under a schema key
        self.assertEqual(out["label"], "v")

    def test_explicit_schema_keys_override(self):
        out = sanitize({"label": "x", "other_el": "y"}, {"el"}, schema_keys=())
        self.assertEqual(set(out), {"lab" + REDACTED, f"other_{REDACTED}"})

    def test_wire_keys_cover_contract_and_none_are_sensitive(self):
        contract = (CARD_KEYS | METER_KEYS | PERIOD_KEYS | ERROR_KEYS | DEBUG_KEYS | POINT_KEYS | SERIES_KEYS
                    | HEALTH_KEYS | META_KEYS | HOLDER_KEYS | METRICS_KEYS | ROW_KEYS | GROUP_KEYS | SOURCE_KEYS
                    | PROVIDER_KEYS | USAGE_KEYS | API_EQUIVALENT_KEYS | ROW_PRICING_KEYS | DATA_PRICING_KEYS
                    | ((DURATION_KEYS | HEATMAP_KEYS | HEATCELL_KEYS | HEATSTART_KEYS | CACHE_ROW_KEYS | REPO_ROW_KEYS
                        | MODEL_ROW_KEYS | DEPTH_TOTALS_KEYS | DEPTH_STATUS_KEYS | DEPTH_MISSING_KEYS)
                       - DEPTH_COUNTER_NAMES)
                    | {"ok", "data", "error", "meta", "cards", "inventory_state", "view",
                       "entries", "window_days", "series", "reason", "provider_id"})
        self.assertEqual(set(dto.WIRE_KEYS), contract)
        self.assertEqual([k for k in dto.WIRE_KEYS if is_sensitive_name(k)], [])
        # The counters containing "token" stay redacted in raw mappings; only an exact depth-object shape keeps them.
        self.assertEqual(sanitize({"total_tokens": 5})["total_tokens"], REDACTED)
        self.assertEqual(sanitize({"sessions": 1, "total_tokens": 5}), {"sessions": 1, "total_tokens": 5})
        self.assertEqual(sanitize({"sessions": 1, "total_tokens": 5, "extra": 1})["total_tokens"], REDACTED)
        self.assertEqual(sanitize({n: 1 for n in DEPTH_MISSING_KEYS}), {n: 1 for n in DEPTH_MISSING_KEYS})

    def test_enum_and_format_values_survive_every_colliding_secret(self):
        sample = {"confidence": "self_hosted", "state": "opaque", "reason": "unsupported", "kind": "self_hosted",
                  "plan_source": "unverified_claim", "auth_status": "available", "status": "degraded",
                  "unit": "usd", "scope": "api_credential", "source": "openrouter_key",
                  "inventory_state": "detected", "view": "machine", "bucket": "day", "timezone": "server_local",
                  "resets_at": "2026-11-06T17:00:00Z",
                  "holders": [{"profile": "verifier", "is_global": False}, {"profile": "global", "is_global": True}],
                  "coverage": "partial", "code": "upstream_unavailable",
                  "message": dto.ERROR_MESSAGES["upstream_unavailable"],
                  "checked_at": "2026-11-06T12:00:00Z", "observed_at": "2026-11-06T12:00:00Z",
                  "day": "2026-11-06", "account_id": "acct_0123456789abcdef01234567",
                  "endpoint_id": "ep_0123456789abcdef01234567", "id": "api_spend",
                  "request_id": "0a1b2c3d-0000-4000-8000-0123456789ab",
                  "period": {"start": "2026-11-06T00:00:00Z", "end": "2026-11-06T12:00:00Z", "label": "rolling_30d"}}
        secrets = {c for value in json.dumps(sample) for c in value} | {"el", "ex", "ready", "opaque", "self_hosted",
                                                                          "2026", "rolling", "acct_", "api_spend"}
        for secret in sorted(secrets):
            with self.subTest(secret=secret):
                self.assertEqual(sanitize(sample, {secret}), sample)

    def test_error_variant_messages_survive_every_colliding_secret(self):
        # DESIGN.md §9.1.1/§9.1.2: the 401 / 404 / 429 / re-auth card messages are fixed wire vocabulary.
        for variant, (code, message, _retryable) in dto.ERROR_VARIANTS.items():
            sample = {"code": code, "message": message}
            for secret in sorted({c for c in message} | {"hermes", "Anthropic", "login", "401", "retry"}):
                with self.subTest(variant=variant, secret=secret):
                    self.assertEqual(sanitize(sample, {secret}), sample)

    def test_free_text_label_in_non_period_mapping_is_masked(self):
        out = sanitize({"label": "rolling_30d", "id": "m"}, {"rolling"})  # not Period-shaped: free text
        self.assertEqual(out["label"], f"{REDACTED}_30d")


class SchemaGuardTests(unittest.TestCase):
    def test_check_wire_rejects_garbled_shapes(self):
        card = {"id": "acct_" + "0" * 24, "provider_id": "p", "account_id": "acct_" + "0" * 24, "provider_label": "P",
                "account_label": "Account 1", "kind": "cloud", "plan_name": None, "plan_source": "none",
                "auth_status": "missing", "endpoint_id": None, "state": "opaque", "confidence": "opaque",
                "reason": "unsupported", "meters": [], "checked_at": "2026-11-06T12:00:00Z", "stale": False,
                "error": None, "debug": {"adapter": "a", "capabilities": [], "source": "s", "sanitized": True, "skipped_slots": 0},
                "holders": [{"profile": "work", "is_global": False}], "last_seen_at": None}
        self.assertIs(check_wire("card", card), card)
        for mutate in (
            lambda c: c.pop("reason"),                        # omitted != null
            lambda c: c.update(extra=1),                      # unknown field
            lambda c: c.__setitem__("lab[REDACTED]", c.pop("provider_label")),  # renamed key
            lambda c: c.update(confidence="opa[REDACTED]e"),  # invalid enum
            lambda c: c.update(checked_at="[REDACTED]-11-06T12:00:00Z"),
            lambda c: c["debug"].update(sanitized=False),
            lambda c: c.pop("holders"),
            lambda c: c["holders"].append({"profile": "p"}),   # holder missing is_global
            lambda c: c["holders"].append({"profile": "[REDACTED]", "is_global": False}),
        ):
            bad = json.loads(json.dumps(card))
            mutate(bad)
            with self.assertRaises(DtoError):
                check_wire("card", bad)


class ServiceSchemaTests(ServiceBase, SchemaAsserts):
    SHORT_SECRET_KEY_COLLISIONS = ("el", "ab", "id", "at", "e", "a", "_", "ac", "ce", "co", "ou")
    ENUM_COLLISIONS = ("ex", "act", "exact", "opaque", "ready", "self_hosted", "unsupported", "cloud", "none",
                       "available", "server_local", "day", "usd", "Z", "T", "0", "2", "-", ":")

    def seed_for(self, secret):
        """The fleet of ServiceBase.seed_fleet, but every credential equals ``secret``."""
        self.s.write_auth({"credential_pool": {
            "openrouter": [{"id": "or1", "access_token": secret, "label": "mine"}],
            "openai-codex": [{"id": "c1", "access_token": secret, "refresh_token": secret}],
            "nous": [{"id": "n1", "access_token": secret, "agent_key": secret}],
            "mystery-cloud": [{"id": "m1", "access_token": secret}],
        }})
        self.custom = [{"name": "gpu", "base_url": GPU + "/v1", "api_key": secret}]
        self.transport.routes.update({
            OPENROUTER_KEY_URL: json_response({"data": {"usage": 1.5, "limit": 5, "limit_remaining": 3.5}}),
            GPU + "/metrics": raw_response(METRICS, latency=2.0),
            GPU + "/v1/models": json_response({"data": []}, latency=6.0),
        })

    VIEW = "machine"

    def fetch_all(self):
        svc = self.make()
        return {e: self.call(svc, e, [("view", self.VIEW)]) for e in ("subscriptions", "health", "usage")}

    def test_control_run_matches_the_contract_exactly(self):
        self.seed_for("ctl-0000-none")
        for endpoint, (status, body) in self.fetch_all().items():
            self.assert_exact_envelope(endpoint, status, body)
        cards = self.fetch_all()["subscriptions"][1]["data"]["cards"]
        self.assertEqual(len(cards), 5)

    def run_collision(self, secret):
        self.seed_for("ctl-0000-none")
        control = self.fetch_all()
        self.seed_for(secret)  # same scratch home; auth.json rewritten, fresh service (no shared cache)
        masked = self.fetch_all()
        for endpoint in ("subscriptions", "health", "usage"):
            c_status, c_body = control[endpoint]
            status, body = masked[endpoint]
            self.assert_exact_envelope(endpoint, status, body)
            # Same structure as the no-collision run: no key renamed, added or dropped, same list lengths.
            self.assertEqual(shape(body["data"]), shape(c_body["data"]), (secret, endpoint))
            self.assertEqual(shape(body["meta"]), shape(c_body["meta"]))
            # Every enum/format value is identical to the control (never garbled, never invalid);
            # debug.source/adapter are free-text strings in the contract, so they are excluded.
            enum_keys = set(ENUMS) | {"checked_at", "observed_at", "day", "account_id", "endpoint_id"}
            masked_pairs = [(p, k, v) for p, k, v in walk(body["data"]) if k in enum_keys and p[-1:] != ("debug",)]
            control_pairs = [(p, k, v) for p, k, v in walk(c_body["data"]) if k in enum_keys and p[-1:] != ("debug",)]
            self.assertEqual(masked_pairs, control_pairs, (secret, endpoint))
            # Numeric values survive.
            if endpoint == "usage" and self.VIEW == "profile":  # profile mode serves the baseline reader's numbers
                self.assertEqual([p["input_count"] for p in body["data"]["series"][0]["points"]], [100, 0])
        return masked

    def test_short_secret_that_is_a_substring_of_canonical_keys_keeps_exact_schema(self):
        for secret in self.SHORT_SECRET_KEY_COLLISIONS:
            with self.subTest(secret=secret):
                masked = self.run_collision(secret)
                # Structure is intact AND the private value is still masked wherever it is free text.
                for endpoint, (_, body) in masked.items():
                    leaked = [(k, v) for _, k, v in walk(body["data"])
                              if k in FREE_TEXT_KEYS and isinstance(v, str) and secret in v]
                    self.assertEqual(leaked, [], (secret, endpoint))

    def test_secret_colliding_with_fixed_enum_vocabulary_never_yields_invalid_enum(self):
        for secret in self.ENUM_COLLISIONS:
            with self.subTest(secret=secret):
                self.run_collision(secret)

    def test_label_collision_is_masked_but_schema_intact(self):
        self.s.write_auth({"credential_pool": {"nous": [{"id": "n1", "access_token": "el"}]}})
        status, body = self.call(self.make(), "subscriptions")
        self.assert_exact_envelope("subscriptions", status, body)
        card = body["data"]["cards"][0]
        self.assertEqual(set(card), CARD_KEYS)
        self.assertEqual((card["provider_label"], card["account_label"]), ("Nous Portal", "Account 1"))
        self.assertEqual(scan_for(body["data"]["cards"], ["lab[REDACTED]"]), [])

    def test_regression_in_masking_becomes_internal_error_not_malformed_200(self):
        """If masking ever corrupted the schema again, the response must be the existing 500 model."""
        self.seed_for("ctl-0000-none")

        def garbling(value, secrets=(), *a, **kw):
            return {k.replace("label", "lab[REDACTED]"): v for k, v in value.items()} if isinstance(value, dict) else value

        with mock.patch("plugins.usage_deck.service.sanitize", side_effect=garbling):
            status, body = self.call(self.make(), "subscriptions")
        self.assertEqual(status, 500)
        self.assertEqual((body["ok"], body["data"], body["error"]["code"]), (False, None, "internal_error"))
        self.assertEqual(set(body["error"]), ERROR_KEYS)
        self.assertEqual(set(body), {"ok", "data", "error", "meta"})
        self.assertNotIn("lab[REDACTED]", json.dumps(body))

    def test_plan_claim_survives_with_secret_collision(self):
        self.s.write_auth({"credential_pool": {"openai-codex": [
            {"id": "c1", "access_token": make_jwt({"plan": "business"}), "refresh_token": "ss"}]}})
        status, body = self.call(self.make(), "subscriptions")
        self.assert_exact_envelope("subscriptions", status, body)
        self.assertEqual(body["data"]["cards"][0]["plan_source"], "unverified_claim")


class ServiceSchemaProfileViewTests(ServiceSchemaTests):
    """The same exact-schema and masking-collision checks against the profile-scoped view."""

    VIEW = "profile"

    def test_profile_view_cards_have_no_holders(self):
        self.seed_for("ctl-0000-none")
        _, body = self.fetch_all()["subscriptions"]
        self.assertEqual(body["data"]["view"], "profile")
        self.assertTrue(body["data"]["cards"])
        self.assertTrue(all(c["holders"] == [] for c in body["data"]["cards"]))


if __name__ == "__main__":
    unittest.main()
