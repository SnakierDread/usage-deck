"use strict";
// Machine view, holders, Anthropic header meters and recon-cited opaque notes (DESIGN.md §9, §10).
// All fixture data is synthetic.
const test = require("node:test");
const assert = require("node:assert/strict");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;
const T = "2026-11-06T12:00:00Z";

const holders = (...names) => names.map((n) => ({ profile: n, is_global: n === "global" }));
const codexGo = F.card({
  id: "acct_go", account_id: "acct_go", provider_id: "openai-codex", provider_label: "OpenAI Codex",
  plan_name: "go", plan_source: "unverified_claim", holders: holders("verifier"),
});
const codexBiz = F.card({
  id: "acct_biz", account_id: "acct_biz", provider_id: "openai-codex", provider_label: "OpenAI Codex",
  account_label: "Account 1", plan_name: "business", plan_source: "unverified_claim", holders: holders("global", "architect"),
});
const rolling = (label) => ({ start: null, end: null, label });
const hdrMeter = (over) => F.meter(Object.assign({
  id: "session_5h", label: "Session (5-hour window)", value: 1.8417, limit: 100, remaining: null, unit: "percent",
  scope: "provider_account", period: rolling("rolling_5h"), confidence: "exact", source: "anthropic_headers",
  observed_at: "2026-11-06T11:58:30Z", resets_at: "2026-11-06T15:00:00Z",
}, over));
const anthropicReady = F.card({
  id: "acct_ant", account_id: "acct_ant", provider_id: "anthropic", provider_label: "Anthropic",
  state: "ready", confidence: "exact", reason: null, holders: holders("global"),
  meters: [hdrMeter(), hdrMeter({ id: "weekly_7d", label: "Weekly (7-day window)", value: 107, period: rolling("rolling_7d"), resets_at: "2026-11-10T09:00:00Z" })],
});
const anthropicOpaque = F.card({
  id: "acct_ant2", account_id: "acct_ant2", provider_id: "anthropic", provider_label: "Anthropic", holders: holders("work"),
});

function routes(over) {
  const o = Object.assign({
    subs: (url) => envelope({ cards: [anthropicReady, anthropicOpaque, codexBiz, codexGo], inventory_state: "detected",
      view: new URL(url, "http://x").searchParams.get("view") }),
    usage: (u) => envelope(F.usage(Number(new URL(u, "http://x").searchParams.get("window")), [F.point("2026-11-01")])),
    health: () => envelope({ entries: [] }),
  }, over || {});
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return o.subs(url);
    if (url.startsWith(B + "/usage")) return o.usage(url);
    if (url.startsWith(B + "/health")) return o.health(url);
    throw new Error("404: unexpected url " + url);
  };
}

test("machine view is the default and every card names the profiles holding it", async () => {
  const m = await mount({ routes: routes() });
  assert.equal(m.$("[data-view-toggle]").dataset.viewToggle, "machine");
  assert.equal(m.$("[data-view=machine]").getAttribute("aria-pressed"), "true");
  assert.match(m.$("[data-view-note]").textContent, /Machine view: the global Hermes home plus every profile/);
  const go = m.$("[data-card-id=acct_go] .ud-card-title");
  assert.equal(go.textContent, "OpenAI Codex [go] — verifier");
  const biz = m.$("[data-card-id=acct_biz] .ud-card-title");
  assert.equal(biz.textContent, "OpenAI Codex [business] — global (root), architect");
  assert.equal(biz.querySelector("[data-holders]").dataset.holders, "global (root),architect");
  assert.match(biz.querySelector(".ud-card-plan").getAttribute("title"), /not verified/);
  await m.unmount();
});

test("profile toggle re-requests with view=profile, drops machine data and holders", async () => {
  const profileCards = [F.readyOpenRouter];
  const m = await mount({ routes: routes({
    subs: (url) => {
      const view = new URL(url, "http://x").searchParams.get("view");
      return envelope({ cards: view === "profile" ? profileCards : [codexGo], inventory_state: "detected", view });
    },
  }) });
  assert.ok(m.$("[data-card-id=acct_go]"));
  await m.click(m.$("[data-view=profile]"));
  await m.flush(5);
  assert.ok(m.calls.includes(B + "/subscriptions?view=profile"));
  assert.ok(m.calls.includes(B + "/health?view=profile"));
  assert.ok(m.calls.includes(B + "/usage?window=30&view=profile"));
  assert.equal(m.$("[data-view=profile]").getAttribute("aria-pressed"), "true");
  assert.equal(m.$("[data-card-id=acct_go]"), null, "machine data never shown under the profile key");
  assert.ok(m.$("[data-card-id=acct_or]"));
  assert.equal(m.$$("[data-holders]").length, 0);
  assert.match(m.$("[data-view-note]").textContent, /Profile view: the dashboard's selected profile/);
  await m.click(m.$("[data-view=machine]"));
  await m.flush(5);
  assert.ok(m.$("[data-card-id=acct_go]"));
  await m.unmount();
});

test("Anthropic header meters: exact badge, source, observed time, provider reset, bar, over-limit kept", async () => {
  const m = await mount({ routes: routes() });
  const card = m.$("[data-card-id=acct_ant]");
  assert.equal(card.dataset.view, "meters");
  const rows = card.querySelectorAll(".ud-meter");
  assert.equal(rows.length, 2);
  const [session, weekly] = rows;
  const badge = session.querySelector(".ud-badge");
  assert.equal(badge.dataset.confidence, "exact");
  const tip = badge.getAttribute("title");
  assert.match(tip, /Source: Anthropic unified rate-limit headers captured from the last call/);
  assert.match(tip, /Scope: Provider account/);
  assert.match(tip, /Observed: 2026-11-06 11:58 UTC/);
  assert.match(tip, /Reset reported by the provider: 2026-11-06 15:00 UTC/);
  assert.match(tip, /not live; it is never extrapolated/);
  assert.match(session.textContent, /Used1\.8%/);
  assert.equal(session.querySelector("[data-resets-at]").dataset.resetsAt, "2026-11-06T15:00:00Z");
  assert.match(session.textContent, /Resets2026-11-06 15:00 UTC/);
  assert.match(session.textContent, /As of2026-11-06 11:58 UTC/);
  assert.ok(!/Remaining|Limit/.test(session.textContent), "no invented remaining/limit rows");
  assert.equal(session.querySelectorAll("[role=progressbar]").length, 1, "rolling window is a known period");
  const bar = weekly.querySelector("[role=progressbar]");
  assert.equal(bar.getAttribute("aria-valuenow"), "100");
  assert.match(bar.getAttribute("aria-valuetext"), /107% of limit \(over limit\)/);
  assert.equal(card.querySelector(".ud-card-head .ud-badge").dataset.confidence, "exact");
  await m.unmount();
});

test("a header meter can never display above what its source allows", () => {
  const lying = hdrMeter({ source: "none" });
  assert.equal(mod.meterConfidence(lying), "opaque");
  assert.equal(mod.meterConfidence(hdrMeter({ confidence: "estimated" })), "estimated");
  assert.equal(mod.meterConfidence(hdrMeter()), "exact");
});

test("opaque Codex/Anthropic cards explain why there is no meter; no number, bar or zero", async () => {
  const m = await mount({ routes: routes() });
  const codex = m.$("[data-card-id=acct_go]");
  assert.equal(codex.dataset.view, "unavailable");
  const note = codex.querySelector("[data-opaque-note=openai-codex]");
  // Source-aware (DESIGN.md §9.2): the superseded blanket "never read, no live response" text is gone.
  assert.match(note.textContent, /No Codex spend meter for this account/);
  assert.match(note.textContent, /Nothing is estimated/);
  assert.doesNotMatch(note.textContent, /t_7dce2222|no live response|§1\.3/);
  const badge = codex.querySelector(".ud-card-head .ud-badge");
  assert.equal(badge.dataset.confidence, "opaque");
  assert.doesNotMatch(badge.getAttribute("title"), /t_7dce2222|no live response/);
  assert.equal(codex.querySelectorAll("[role=progressbar], .ud-meter").length, 0);
  const ant = m.$("[data-card-id=acct_ant2]");
  assert.match(ant.querySelector("[data-opaque-note=anthropic]").textContent, /no unified rate-limit headers are captured/);
  assert.match(ant.querySelector("[data-opaque-note=anthropic]").textContent, /Nothing is estimated/);
  await m.unmount();
});

test("a provider without a note (Nous) and self-hosted cards get no note", async () => {
  assert.equal(mod.opaqueNote(F.opaqueNous), null);
  assert.equal(mod.opaqueNote(F.selfHosted), null);
  assert.equal(mod.opaqueNote(anthropicReady), null, "a card with measured meters needs no explanation");
  assert.match(mod.opaqueNote(codexGo), /No Codex spend meter/);
  assert.equal(mod.opaqueNote(null), null);
});

test("holderNames tolerates malformed holder data and never throws", () => {
  assert.deepEqual(mod.holderNames({ holders: [{ profile: "a", is_global: false }, { profile: "global", is_global: true }, null, { profile: 5 }, {}] }),
    ["a", "global (root)"]);
  assert.deepEqual(mod.holderNames({}), []);
  assert.deepEqual(mod.holderNames({ holders: "x" }), []);
  assert.deepEqual(mod.holderNames(null), []);
});

test("holder names are rendered as text, never as markup", async () => {
  const evil = F.card({ id: "acct_evil", account_id: "acct_evil", holders: [{ profile: "<img src=x onerror=alert(1)>", is_global: false }] });
  const m = await mount({ routes: routes({ subs: () => envelope({ cards: [evil], inventory_state: "detected", view: "machine" }) }) });
  assert.equal(m.$$("img").length, 0);
  assert.match(m.$("[data-card-id=acct_evil] .ud-card-title").textContent, /<img src=x/);
  await m.unmount();
});

test("machine empty inventory says so on the machine, with no credential input", async () => {
  const m = await mount({ routes: routes({ subs: () => envelope({ cards: [], inventory_state: "empty", view: "machine" }) }) });
  assert.match(m.$("[data-state=first_run]").textContent, /No providers detected on this machine/);
  assert.equal(m.$$("input, textarea, [type=password]").length, 0);
  await m.unmount();
});
