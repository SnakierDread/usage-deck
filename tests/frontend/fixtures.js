// Shared DTO fixtures matching DESIGN.md §2 (wire version 1).
"use strict";

const T = "2026-11-06T00:00:00Z";
const PERIOD_UNKNOWN = { start: null, end: null, label: "unknown" };
const DEBUG = { adapter: "x", capabilities: [], source: "x", sanitized: true };

function card(over) {
  return Object.assign({
    id: "acct_a", provider_id: "openrouter", account_id: "acct_a",
    provider_label: "OpenRouter", account_label: "Account 1", kind: "cloud",
    plan_name: null, plan_source: "none", auth_status: "available", endpoint_id: null,
    state: "opaque", confidence: "opaque", reason: "unsupported", meters: [],
    checked_at: T, stale: false, error: null, debug: DEBUG,
  }, over);
}

function meter(over) {
  return Object.assign({
    id: "api_spend", label: "API credential spend", value: 12.5, limit: null, remaining: null,
    unit: "usd", scope: "api_credential", period: PERIOD_UNKNOWN, confidence: "exact",
    source: "openrouter_key", observed_at: T,
  }, over);
}

const readyOpenRouter = card({
  id: "acct_or", account_id: "acct_or", state: "ready", confidence: "exact", reason: null,
  meters: [meter({ value: 12.5, limit: 50, remaining: 37.5 })],
});
const opaqueNous = card({
  id: "acct_nous", account_id: "acct_nous", provider_id: "nous", provider_label: "Nous Portal",
  state: "opaque", confidence: "opaque", reason: "unsupported", meters: [],
});
const errorAnthropic = card({
  id: "acct_an", account_id: "acct_an", provider_id: "anthropic", provider_label: "Anthropic",
  state: "error", confidence: "opaque", reason: null,
  error: { code: "timeout", message: "The provider did not answer in time.", retryable: true, provider_id: "anthropic", account_id: "acct_an" },
});
const selfHosted = card({
  id: "acct_ol", account_id: "acct_ol", provider_id: "ollama", provider_label: "Ollama",
  kind: "self_hosted", endpoint_id: "ep_1", state: "opaque", confidence: "opaque", reason: "metrics_unavailable",
});

function point(day, over) {
  return Object.assign({ day, input_count: 1000, output_count: 400, estimated_usd: 0.25, sessions: 3, api_calls: 10, confidence: "estimated" }, over);
}
function series(points, over) {
  return Object.assign({
    id: "profile", provider_id: null, account_id: null, scope: "profile", confidence: "estimated",
    source: "session_analytics", period: { start: "2026-10-07T00:00:00Z", end: T, label: "rolling_30d" },
    window_days: 30, bucket: "day", timezone: "server_local", coverage: "partial",
    points, checked_at: T, stale: false, error: null,
  }, over);
}
function usage(windowDays, points, over) {
  return Object.assign({ window_days: windowDays, provider_id: null,
    series: [series(points, { window_days: windowDays })], reason: null }, over);
}
function healthEntry(over) {
  return Object.assign({
    endpoint_id: "ep_1", provider_id: "ollama", display_name: "Local Ollama", status: "healthy",
    confidence: "self_hosted", metrics_available: true, latency_ms: 42.4, cpu_percent: 12.5,
    memory_bytes: 2 * 1024 * 1024 * 1024, checked_at: T, stale: false, reason: null, error: null,
  }, over);
}

module.exports = { T, card, meter, point, series, usage, healthEntry, readyOpenRouter, opaqueNous, errorAnthropic, selfHosted, PERIOD_UNKNOWN };
