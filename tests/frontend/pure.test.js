"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { mod } = require("./harness");
const F = require("./fixtures");

test("confidence is never raised above the source ceiling", () => {
  assert.equal(mod.meterConfidence(F.meter({ confidence: "exact", source: "session_analytics" })), "estimated");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "exact", source: "none" })), "opaque");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "exact", source: "prometheus" })), "opaque");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "self_hosted", source: "prometheus" })), "self_hosted");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "exact", source: "openrouter_key" })), "exact");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "estimated", source: "openrouter_key" })), "estimated");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "bogus", source: "openrouter_key" })), "opaque");
  assert.equal(mod.meterConfidence(F.meter({ confidence: "exact", source: "made_up" })), "opaque");
});

test("card badge never exceeds measured meters / state", () => {
  assert.equal(mod.cardConfidence(F.card({ state: "opaque", confidence: "exact" })), "opaque");
  assert.equal(mod.cardConfidence(F.card({ state: "error", confidence: "exact" })), "opaque");
  // ready + exact claim but only an estimated meter -> estimated
  const c = F.card({ state: "ready", confidence: "exact", meters: [F.meter({ confidence: "estimated", source: "session_analytics" })] });
  assert.equal(mod.cardConfidence(c), "estimated");
  // ready with no measured value -> opaque
  assert.equal(mod.cardConfidence(F.card({ state: "ready", confidence: "exact", meters: [] })), "opaque");
  assert.equal(mod.cardConfidence(F.readyOpenRouter), "exact");
});

test("progress bar eligibility: null/zero/over-limit/unlimited/unknown period", () => {
  const known = { start: null, end: null, label: "rolling_30d" };
  assert.equal(mod.progressRatio(F.meter({ value: 10, limit: 40, period: known })), 0.25);
  assert.equal(mod.progressRatio(F.meter({ value: 60, limit: 40, period: known })), 1.5);        // raw ratio kept
  assert.equal(mod.progressRatio(F.meter({ value: 0, limit: 40, period: known })), 0);           // observed zero is a bar at 0
  assert.equal(mod.progressRatio(F.meter({ value: null, limit: 40, period: known })), null);
  assert.equal(mod.progressRatio(F.meter({ value: 5, limit: null, period: known })), null);      // unlimited / missing
  assert.equal(mod.progressRatio(F.meter({ value: 5, limit: 0, period: known })), null);         // zero limit
  assert.equal(mod.progressRatio(F.meter({ value: -1, limit: 10, period: known })), null);
  assert.equal(mod.progressRatio(F.meter({ value: 5, limit: 10 })), null);                       // period unknown (OpenRouter)
  assert.equal(mod.progressRatio(F.meter({ value: 5, limit: 10, period: known, confidence: "opaque" })), null);
  assert.equal(mod.progressRatio(F.meter({ value: NaN, limit: 10, period: known })), null);
});

test("number formatting never turns null into zero", () => {
  assert.equal(mod.fmtNumber(null, "usd"), "—");
  assert.equal(mod.fmtNumber(undefined, "count"), "—");
  assert.equal(mod.fmtNumber(NaN, "bytes"), "—");
  assert.equal(mod.fmtNumber(0, "usd"), "$0.00");
  assert.equal(mod.fmtNumber(12.5, "usd"), "$12.50");
  assert.equal(mod.fmtNumber(0.0123, "usd"), "$0.0123");
  assert.equal(mod.fmtNumber(1234567, "count"), "1,234,567");
  assert.equal(mod.fmtNumber(2 * 1024 ** 3, "bytes"), "2.0 GiB");
  assert.equal(mod.fmtNumber(12.34, "percent"), "12.3%");
});

test("fmtTime is deterministic UTC text", () => {
  assert.equal(mod.fmtTime("2026-11-06T12:30:00Z"), "2026-11-06 12:30 UTC");
  assert.equal(mod.fmtTime(null), "unknown time");
  assert.equal(mod.fmtTime("garbage"), "unknown time");
});

test("splitSegments breaks at nulls and missing days; never zero-fills", () => {
  const pts = [
    F.point("2026-11-01"), F.point("2026-11-02"),
    F.point("2026-11-04"),                                   // 11-03 missing -> break
    F.point("2026-11-05", { input_count: null }),            // null -> break
    F.point("2026-11-06"),
  ];
  const segs = mod.splitSegments(pts, "input_count");
  assert.deepEqual(segs.map((s) => s.map((p) => p.day)), [["2026-11-01", "2026-11-02"], ["2026-11-04"], ["2026-11-06"]]);
  assert.ok(segs.every((s) => s.every((p) => p.v === 1000)));
  assert.deepEqual(mod.splitSegments([F.point("2026-11-01", { input_count: null })], "input_count"), []);
});

test("classifyError: host auth failures, plugin envelopes, transport", () => {
  assert.equal(mod.classifyError(new Error("401: nope")).kind, "access");
  assert.equal(mod.classifyError(new Error("403: {\"detail\":\"x\"}")).kind, "access");
  const e503 = mod.classifyError(new Error("503: " + JSON.stringify(
    { ok: false, data: null, error: { code: "analytics_unavailable", message: "Analytics are unavailable.", retryable: true } })));
  assert.equal(e503.kind, "deck");
  assert.equal(e503.code, "analytics_unavailable");
  assert.equal(e503.retryable, true);
  const detail = mod.classifyError(new Error("400: " + JSON.stringify({ detail: { error: { code: "invalid_query", message: "bad", retryable: false } } })));
  assert.equal(detail.code, "invalid_query");
  const raw = mod.classifyError(new Error("502: <html>secret-looking upstream body</html>"));
  assert.equal(raw.kind, "transport");
  assert.ok(!raw.message.includes("secret"), "raw bodies are never surfaced");
  assert.equal(mod.classifyError(new TypeError("Failed to fetch")).kind, "transport");
});

test("unwrapEnvelope enforces wire version and shape", () => {
  assert.deepEqual(mod.unwrapEnvelope({ ok: true, data: { a: 1 }, error: null, meta: { schema_version: 1 } }).data, { a: 1 });
  for (const bad of [null, [], { ok: true }, { ok: true, data: {}, meta: { schema_version: 2 } }, { ok: true, data: [], meta: { schema_version: 1 } }, { ok: false }]) {
    assert.throws(() => mod.unwrapEnvelope(bad), (e) => e.uiError && e.uiError.kind === "malformed");
  }
  assert.throws(() => mod.unwrapEnvelope({ ok: false, error: { code: "timeout", message: "t", retryable: true } }),
    (e) => e.uiError.kind === "deck" && e.uiError.code === "timeout");
});

test("cardView: ready without a measured value is never a zero", () => {
  assert.equal(mod.cardView(F.card({ state: "ready", meters: [] })), "unavailable");
  assert.equal(mod.cardView(F.card({ state: "ready", meters: [F.meter({ value: null, limit: null, remaining: null })] })), "unavailable");
  assert.equal(mod.cardView(F.card({ state: "ready", meters: [F.meter({ value: 0 })] })), "meters");   // explicit zero is ready
  assert.equal(mod.cardView(F.card({ state: "empty" })), "empty");
  assert.equal(mod.cardView(F.card({ state: "error" })), "error");
  assert.equal(mod.cardView(F.card({ state: "weird" })), "error");
  assert.equal(mod.cardView(F.card({ state: "opaque", meters: [F.meter({ confidence: "opaque", source: "none" })] })), "unavailable");
});
