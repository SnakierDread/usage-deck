"use strict";
// Codex workspace spend meter UI (DESIGN.md §5, §9.2, §11): credits unit, exact per-meter badge, source ceiling,
// ready / limit-only / opaque / error states, and the removal of the superseded blanket Codex-opaque explanation.
// All fixture data is synthetic.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;
const MONTHLY = { start: null, end: null, label: "monthly_workspace" };
const spend = (over) => F.meter(Object.assign({
  id: "workspace_spend", label: "Workspace spend limit (monthly)", value: 864.4848500490189, limit: 1500,
  remaining: 635.5151499509811, unit: "credit", scope: "provider_account", period: MONTHLY, confidence: "exact",
  source: "chatgpt_wham_usage", observed_at: "2026-11-06T12:00:00Z", resets_at: "2026-12-01T00:00:01Z",
}, over));
const codex = (over) => F.card(Object.assign({
  id: "acct_cx", account_id: "acct_cx", provider_id: "openai-codex", provider_label: "OpenAI Codex",
  plan_name: "business", plan_source: "unverified_claim", state: "ready", confidence: "exact", reason: null,
  meters: [spend()], debug: { adapter: "OpenAICodexAdapter", capabilities: ["quota"], source: "chatgpt_wham_usage", sanitized: true },
}, over));
const err = (code, retryable) => ({ code, message: "x", retryable, provider_id: "openai-codex", account_id: "acct_cx" });

function routes(cards) {
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return envelope({ cards, inventory_state: "detected", view: "machine" });
    if (url.startsWith(B + "/usage")) return envelope(F.usage(30, [F.point("2026-11-01")]));
    if (url.startsWith(B + "/health")) return envelope({ entries: [] });
    throw new Error("404: unexpected url " + url);
  };
}

test("credit unit is displayed as 'credits', never as $, count or tokens", () => {
  assert.equal(mod.fmtNumber(864.4848500490189, "credit"), "864.48 credits");
  assert.equal(mod.fmtNumber(1500, "credit"), "1,500 credits");
  assert.equal(mod.fmtNumber(0, "credit"), "0 credits");
  assert.equal(mod.fmtNumber(null, "credit"), "—");
  assert.ok(!/\$/.test(mod.fmtNumber(12, "credit")));
  assert.equal(mod.fmtNumber(1234567, "count"), "1,234,567");
});

test("chatgpt_wham_usage has an exact ceiling and a fixed label; claims above it are lowered, unknown sources opaque", () => {
  assert.equal(mod.meterConfidence(spend()), "exact");
  assert.equal(mod.meterConfidence(spend({ confidence: "estimated" })), "estimated");
  assert.equal(mod.meterConfidence(spend({ confidence: "opaque" })), "opaque");
  assert.equal(mod.meterConfidence(spend({ source: "chatgpt_wham_usagex" })), "opaque");
  assert.equal(mod.meterConfidence(spend({ confidence: "self_hosted" })), "opaque");
});

test("ready Codex card: exact badge, credits, provider reset, as-of, bar over a known monthly period", async () => {
  const m = await mount({ routes: routes([codex()]) });
  const card = m.$("[data-card-id=acct_cx]");
  assert.equal(card.dataset.view, "meters");
  const row = card.querySelector("[data-meter-id=workspace_spend]");
  const badge = row.querySelector(".ud-badge");
  assert.equal(badge.dataset.confidence, "exact");
  const tip = badge.getAttribute("title");
  assert.match(tip, /Source: ChatGPT workspace spend control \(wham\/usage\)/);
  assert.match(tip, /Scope: Provider account/);
  assert.match(tip, /Observed: 2026-11-06 12:00 UTC/);
  assert.match(tip, /Reset reported by the provider: 2026-12-01 00:00 UTC/);
  assert.match(tip, /not USD, not messages, not a fleet total/);
  assert.match(row.textContent, /Workspace spend limit \(monthly\)/);
  assert.match(row.textContent, /Used864\.48 credits/);
  assert.match(row.textContent, /Limit1,500 credits/);
  assert.match(row.textContent, /Remaining635\.52 credits/);
  assert.match(row.textContent, /Resets2026-12-01 00:00 UTC/);
  assert.match(row.textContent, /As of2026-11-06 12:00 UTC/);
  assert.equal(row.querySelector("[data-resets-at]").dataset.resetsAt, "2026-12-01T00:00:01Z");
  assert.ok(!/\$|USD/.test(row.textContent), "no currency");
  const bar = row.querySelector("[role=progressbar]");
  assert.ok(bar, "monthly_workspace is a known period");
  assert.equal(bar.getAttribute("aria-valuetext"), "57.6% of limit");
  assert.equal(card.querySelector(".ud-card-head .ud-badge").dataset.confidence, "exact");
  assert.match(card.textContent, /Credential present \(not validated\)/);
  await m.unmount();
});

test("limit-only, zero and overspend readings render without invented numbers", async () => {
  const limitOnly = codex({ id: "acct_lo", account_id: "acct_lo", meters: [spend({ value: null, remaining: null })] });
  const zero = codex({ id: "acct_z", account_id: "acct_z", meters: [spend({ value: 0, remaining: 1500 })] });
  const noLimit = codex({ id: "acct_nl", account_id: "acct_nl", meters: [spend({ limit: null, remaining: null, resets_at: null })] });
  const zeroLimit = codex({ id: "acct_zl", account_id: "acct_zl", meters: [spend({ value: 3, limit: 0, remaining: 0 })] });
  const m = await mount({ routes: routes([limitOnly, zero, noLimit, zeroLimit]) });
  const lo = m.$("[data-card-id=acct_lo]");
  assert.equal(lo.dataset.view, "meters", "any non-null observation is a reading");
  assert.match(lo.textContent, /UsedNot reported/);
  assert.match(lo.textContent, /Limit1,500 credits/);
  assert.match(lo.textContent, /RemainingNot reported/);
  assert.equal(lo.querySelectorAll("[role=progressbar]").length, 0, "no bar without a used value");
  const z = m.$("[data-card-id=acct_z]");
  assert.match(z.textContent, /Used0 credits/);
  assert.equal(z.querySelector("[role=progressbar]").getAttribute("aria-valuenow"), "0");
  const nl = m.$("[data-card-id=acct_nl]");
  assert.match(nl.textContent, /LimitNot reported/);
  assert.match(nl.textContent, /ResetsNot reported/);
  assert.equal(nl.querySelectorAll("[role=progressbar]").length, 0, "no bar for a null limit");
  assert.equal(m.$("[data-card-id=acct_zl]").querySelectorAll("[role=progressbar]").length, 0, "no bar for a zero limit");
  await m.unmount();
});

test("opaque Codex cards (unsupported, expired, no credential, forbidden) show no number, bar, zero or stale recon text", async () => {
  const cards = ["source_unavailable", "credentials_missing", "permission_denied"].map((reason, i) => codex({
    id: "acct_o" + i, account_id: "acct_o" + i, state: "opaque", confidence: "opaque", reason, meters: [],
    debug: { adapter: "OpenAICodexAdapter", capabilities: [], source: "local_inventory", sanitized: true },
  }));
  const m = await mount({ routes: routes(cards) });
  for (let i = 0; i < 3; i++) {
    const c = m.$("[data-card-id=acct_o" + i + "]");
    assert.equal(c.dataset.view, "unavailable");
    assert.equal(c.querySelectorAll("[role=progressbar], .ud-meter").length, 0);
    assert.equal(c.querySelector(".ud-card-head .ud-badge").dataset.confidence, "opaque");
    assert.match(c.querySelector("[data-opaque-note=openai-codex]").textContent, /Nothing is estimated/);
    assert.doesNotMatch(c.textContent, /recon|t_7dce2222|no live response|1\.3/);
    assert.ok(!/\b\d+ credits\b/.test(c.textContent), "no number: " + c.textContent);
  }
  assert.match(m.$("[data-card-id=acct_o1]").textContent, /No credential was sent for this account \(none was found\)/);
  assert.match(m.$("[data-card-id=acct_o2]").textContent, /does not permit reading this data/);
  await m.unmount();
});

test("a 401 shows the safe auth_required error, a retry, and no meter", async () => {
  const c = codex({
    state: "error", confidence: "opaque", reason: "permission_denied", meters: [], error: {
      code: "auth_required", message: "The provider rejected the stored credential.", retryable: false,
      provider_id: "openai-codex", account_id: "acct_cx",
    },
  });
  const m = await mount({ routes: routes([c, codex({ id: "acct_t", account_id: "acct_t", state: "error", confidence: "opaque",
    meters: [], error: { code: "timeout", message: "The provider did not answer in time.", retryable: true, provider_id: "openai-codex", account_id: "acct_t" } })]) });
  const card = m.$("[data-card-id=acct_cx]");
  assert.equal(card.dataset.view, "error");
  assert.match(card.textContent, /The provider rejected the stored credential\./);
  assert.match(card.textContent, /Code: auth_required/);
  assert.equal(card.querySelectorAll(".ud-meter, [role=progressbar]").length, 0);
  assert.equal(card.querySelector(".ud-card-head .ud-badge").dataset.confidence, "opaque");
  assert.match(card.textContent, /Credential present \(not validated\)/, "presence only; never 'rejected'");
  assert.doesNotMatch(card.textContent, /recon|no live response/);
  assert.match(m.$("[data-card-id=acct_t]").textContent, /Code: timeout/);
  await m.unmount();
});

test("the shipped bundle carries no superseded blanket Codex-opaque wording", () => {
  const js = fs.readFileSync(path.resolve(__dirname, "../../plugins/usage_deck/dist/index.js"), "utf8");
  assert.doesNotMatch(js, /no live response was verified/);
  assert.doesNotMatch(js, /sending Codex OAuth there is not permitted/);
  assert.doesNotMatch(js, /OPAQUE_NOTES[\s\S]{0,400}recon t_7dce2222[\s\S]{0,200}openai-codex/);
  assert.match(js, /chatgpt_wham_usage/);
});
