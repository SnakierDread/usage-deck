"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { mod, mount, deferred, envelope, errEnvelope, act, dom } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;

function routesFor(over) {
  const o = Object.assign({
    subs: () => envelope({ cards: [F.readyOpenRouter, F.opaqueNous, F.errorAnthropic, F.selfHosted], inventory_state: "detected" }),
    usage: (u) => {
      const w = Number(new URL(u, "http://x").searchParams.get("window"));
      return envelope(F.usage(w, [F.point("2026-11-01"), F.point("2026-11-02"), F.point("2026-11-04")]));
    },
    health: () => envelope({ entries: [F.healthEntry()] }),
  }, over || {});
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return o.subs(url);
    if (url.startsWith(B + "/usage")) return o.usage(url);
    if (url.startsWith(B + "/health")) return o.health(url);
    throw new Error("404: unexpected url " + url);
  };
}

test("renders all three zones from the namespaced API and never shows a credential input", async () => {
  const m = await mount({ routes: routesFor() });
  assert.deepEqual(m.$$("[data-zone]").map((z) => z.dataset.zone), ["subscriptions", "usage", "health"]);
  assert.ok(m.calls.includes(B + "/subscriptions"));
  assert.ok(m.calls.includes(B + "/usage?window=30"));
  assert.ok(m.calls.includes(B + "/health"));
  assert.ok(m.calls.every((u) => u.startsWith("/api/plugins/usage-deck/")), "only the ratified namespace");
  assert.equal(m.$$("input, textarea, [type=password]").length, 0, "no credential wall");
  assert.equal(m.$(".usage-deck").dataset.phase, "connected");
  assert.equal(m.$$("article.ud-card").length, 4);
  await m.unmount();
});

test("opaque card says unavailable honestly: no number, no bar, no zero", async () => {
  const m = await mount({ routes: routesFor() });
  const nous = m.$("[data-card-id=acct_nous]");
  assert.equal(nous.dataset.view, "unavailable");
  assert.match(nous.textContent, /Usage unavailable/);
  assert.match(nous.textContent, /Data unavailable/);
  assert.match(nous.textContent, /Session analytics only/);
  assert.equal(nous.querySelectorAll("[role=progressbar]").length, 0);
  assert.ok(!/\$0|\b0%|\b0\b/.test(nous.textContent.replace(/Account 1/, "")), "no fake zero: " + nous.textContent);
  assert.equal(nous.querySelector(".ud-card-head .ud-badge").dataset.confidence, "opaque");
  await m.unmount();
});

test("ready meter carries its own exact badge with source/scope/time; OpenRouter limit gets no bar", async () => {
  const m = await mount({ routes: routesFor() });
  const or = m.$("[data-card-id=acct_or]");
  assert.equal(or.dataset.view, "meters");
  const badge = or.querySelector(".ud-meter .ud-badge");
  assert.equal(badge.dataset.confidence, "exact");
  assert.equal(badge.textContent, "Exact");
  assert.match(badge.getAttribute("title"), /Source: OpenRouter key endpoint\. Scope: API credential\. Observed: 2026-11-06 00:00 UTC/);
  assert.match(or.textContent, /\$12\.50/);
  assert.match(or.textContent, /\$50\.00/);
  assert.equal(or.querySelectorAll("[role=progressbar]").length, 0, "period unknown -> no quota bar");
  assert.match(or.textContent, /No progress bar/);
  await m.unmount();
});

test("known-period meter draws a bar; over-limit shows overspend text with capped fill", async () => {
  const known = { start: null, end: null, label: "rolling_30d" };
  const over = F.card({ id: "acct_over", state: "ready", confidence: "exact", reason: null,
    meters: [F.meter({ value: 150, limit: 100, remaining: -50, period: known })] });
  const zero = F.card({ id: "acct_zero", state: "ready", confidence: "exact", reason: null,
    meters: [F.meter({ value: 0, limit: 100, remaining: 100, period: known })] });
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [over, zero], inventory_state: "detected" }) }) });
  const o = m.$("[data-card-id=acct_over]");
  const bar = o.querySelector("[role=progressbar]");
  assert.ok(bar);
  assert.equal(bar.getAttribute("aria-valuenow"), "100");
  assert.match(bar.getAttribute("aria-valuetext"), /150% of limit \(over limit\)/);
  assert.equal(bar.firstChild.style.width, "100%");
  assert.match(o.textContent, /Over limit: 150% of limit/);
  assert.match(o.textContent, /Overspent\$50\.00/);
  const z = m.$("[data-card-id=acct_zero]");
  assert.equal(z.dataset.view, "meters");
  assert.match(z.textContent, /\$0\.00/);              // observed zero stays zero
  assert.equal(z.querySelector("[role=progressbar]").getAttribute("aria-valuenow"), "0");
  await m.unmount();
});

test("meter whose source cannot support its claim is badged down, not up", async () => {
  const c = F.card({ id: "acct_s", state: "ready", confidence: "exact", reason: null,
    meters: [F.meter({ confidence: "exact", source: "session_analytics", scope: "profile" })] });
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [c], inventory_state: "detected" }) }) });
  assert.equal(m.$(".ud-meter .ud-badge").dataset.confidence, "estimated");
  assert.equal(m.$(".ud-card-head .ud-badge").dataset.confidence, "estimated");
  await m.unmount();
});

test("error card shows safe message and retry; sibling cards unaffected", async () => {
  const m = await mount({ routes: routesFor() });
  const e = m.$("[data-card-id=acct_an]");
  assert.equal(e.dataset.view, "error");
  assert.match(e.textContent, /The provider did not answer in time\./);
  assert.match(e.textContent, /Code: timeout/);
  assert.ok([...e.querySelectorAll("button")].some((b) => b.textContent === "Retry"));
  assert.equal(m.$("[data-card-id=acct_or]").dataset.view, "meters");
  await m.unmount();
});

test("empty card state", async () => {
  const c = F.card({ id: "acct_e", state: "empty", confidence: "estimated", reason: "no_observations" });
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [c], inventory_state: "detected" }) }) });
  assert.match(m.$("[data-card-id=acct_e]").textContent, /No observations for this period\./);
  await m.unmount();
});

test("loading skeleton shows no numbers or badges before data arrives", async () => {
  const d = deferred();
  const m = await mount({ routes: routesFor({ subs: () => d.promise }) });
  const zone = m.$("[data-zone=subscriptions]");
  assert.ok(zone.querySelector(".ud-skel"));
  assert.equal(zone.querySelectorAll(".ud-badge").length, 0);
  assert.ok(!/\d/.test(zone.textContent.replace("Loading subscriptions…", "")));
  assert.equal(m.$(".usage-deck").dataset.phase, "first_run");
  await act(async () => { d.resolve(envelope({ cards: [F.opaqueNous], inventory_state: "detected" })); });
  assert.equal(m.$(".usage-deck").dataset.phase, "connected");
  await m.unmount();
});

test("empty inventory: first-run guidance with Refresh, no credential input", async () => {
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [], inventory_state: "empty" }), health: () => envelope({ entries: [] }) }) });
  const zone = m.$("[data-zone=subscriptions]");
  assert.match(zone.textContent, /No providers detected in this profile/);
  assert.ok([...zone.querySelectorAll("button")].some((b) => b.textContent === "Refresh"));
  assert.equal(m.$$("input, textarea, [type=password]").length, 0);
  assert.equal(m.$(".usage-deck").dataset.phase, "first_run");
  assert.match(m.$("[data-zone=health]").textContent, /No self-hosted endpoints/);
  // session chart may still render in first_run
  assert.ok(m.$("[data-zone=usage] .ud-chart"));
  await m.unmount();
});

test("host 403 shows dashboard access error, not a credential prompt, and has no retry", async () => {
  const deny = () => { throw new Error("403: Forbidden"); };
  const m = await mount({ routes: routesFor({ subs: deny, usage: deny, health: deny }) });
  const errs = m.$$("[role=alert]");
  assert.equal(errs.length, 3);
  assert.ok(errs.every((e) => /Dashboard access error/.test(e.textContent)));
  assert.ok(errs.every((e) => e.dataset.kind === "access"));
  assert.equal(m.$$("input, [type=password]").length, 0);
  assert.equal(m.$(".usage-deck").dataset.phase, "first_run");
  await m.unmount();
});

test("a failing zone never erases its siblings", async () => {
  const m = await mount({ routes: routesFor({
    usage: () => { throw new Error("503: " + JSON.stringify(errEnvelope("analytics_unavailable", "Session analytics are unavailable.", true))); },
  }) });
  assert.equal(m.$$("article.ud-card").length, 4);
  assert.equal(m.$$("[data-zone=health] .ud-health-row").length, 1);
  const u = m.$("[data-zone=usage] [role=alert]");
  assert.match(u.textContent, /Session analytics are unavailable\./);
  assert.match(u.textContent, /Code: analytics_unavailable/);
  assert.equal(m.$("[data-zone=usage] .ud-chart"), null);
  await m.unmount();
});

test("malformed envelope becomes a malformed error card", async () => {
  const m = await mount({ routes: routesFor({ health: () => ({ ok: true, data: { entries: "nope" }, meta: { schema_version: 1 } }) }) });
  const e = m.$("[data-zone=health] [role=alert]");
  assert.equal(e.dataset.kind, "malformed");
  await m.unmount();
});

test("JSON drawer: aria-expanded, sanitized DTO text only, Escape restores focus", async () => {
  const evil = F.card({ id: "acct_x", provider_label: "<img src=x onerror=alert(1)>", account_label: "<b>bold</b>" });
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [evil], inventory_state: "detected" }) }) });
  assert.equal(m.$$("img").length, 0, "labels are text, not markup");
  assert.equal(m.$$("b").length, 0);
  assert.match(m.$("[data-card-id=acct_x]").textContent, /<img src=x onerror=alert\(1\)>/);
  const toggle = m.$(".ud-card-foot .ud-btn");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(m.$(".ud-drawer"), null);
  await m.click(toggle);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  const drawer = m.$(".ud-drawer");
  assert.equal(drawer.id, toggle.getAttribute("aria-controls"));
  assert.deepEqual(JSON.parse(m.$(".ud-drawer pre").textContent), evil);
  assert.equal(m.$$("img").length, 0);
  await m.unmount();
});

const key = (el, k, extra) => act(async () => {
  el.dispatchEvent(new dom.window.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, extra)));
});

test("JSON viewer is a modal dialog: focus moves in on open, Escape closes from anywhere, focus restored", async () => {
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [F.readyOpenRouter, F.opaqueNous], inventory_state: "detected" }) }) });
  const toggle = m.$$(".ud-card-foot .ud-btn")[1];
  toggle.focus();
  await m.click(toggle);
  const dlg = m.$("[role=dialog]");
  assert.ok(dlg, "role=dialog element exists");
  assert.equal(dlg.tagName, "DIV", "not a bare <pre>");
  assert.equal(dlg.getAttribute("aria-modal"), "true");
  assert.match(dlg.getAttribute("aria-label"), /Sanitized JSON for Nous Portal/);
  assert.equal(toggle.getAttribute("aria-controls"), dlg.id);
  assert.ok(dlg.contains(document.activeElement), "focus moved into the dialog on open");
  assert.equal(document.activeElement.textContent, "Close");
  // Escape with focus anywhere (here: body, as after clicking outside) still closes.
  document.activeElement.blur();
  assert.equal(document.activeElement, document.body);
  await key(document.body, "Escape");
  assert.equal(m.$("[role=dialog]"), null);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(document.activeElement, toggle, "focus restored to the opening button");
  // Escape with focus inside the dialog, and the Close button, both close and restore too.
  await m.click(toggle);
  await key(document.activeElement, "Escape");
  assert.equal(m.$("[role=dialog]"), null);
  assert.equal(document.activeElement, toggle);
  await m.click(toggle);
  await m.click(m.$("[role=dialog] button"));
  assert.equal(m.$("[role=dialog]"), null);
  assert.equal(document.activeElement, toggle);
  await m.unmount();
});

test("JSON viewer traps Tab / Shift+Tab inside the dialog", async () => {
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [F.readyOpenRouter], inventory_state: "detected" }) }) });
  await m.click(m.$(".ud-card-foot .ud-btn"));
  const dlg = m.$("[role=dialog]");
  const closeBtn = dlg.querySelector("button");
  const pre = dlg.querySelector("pre");
  assert.equal(document.activeElement, closeBtn);
  let ev;
  // Tab on the last focusable wraps to the first; Shift+Tab on the first wraps to the last.
  pre.focus();
  await act(async () => { ev = new dom.window.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }); pre.dispatchEvent(ev); });
  assert.ok(ev.defaultPrevented);
  assert.equal(document.activeElement, closeBtn);
  await act(async () => { ev = new dom.window.KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }); closeBtn.dispatchEvent(ev); });
  assert.ok(ev.defaultPrevented);
  assert.equal(document.activeElement, pre);
  // Tab from a middle position is left to the browser (no preventDefault).
  closeBtn.focus();
  await act(async () => { ev = new dom.window.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }); closeBtn.dispatchEvent(ev); });
  assert.ok(!ev.defaultPrevented);
  await m.unmount();
});

test("only one JSON dialog is open at a time and Escape listener is removed on close", async () => {
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [F.readyOpenRouter, F.opaqueNous], inventory_state: "detected" }) }) });
  const [t1, t2] = m.$$(".ud-card-foot .ud-btn");
  await m.click(t1);
  await m.click(t2);
  assert.equal(m.$$("[role=dialog]").length, 1);
  assert.equal(t1.getAttribute("aria-expanded"), "false");
  assert.equal(t2.getAttribute("aria-expanded"), "true");
  await key(document.body, "Escape");
  assert.equal(m.$$("[role=dialog]").length, 0);
  t1.focus();
  await key(document.body, "Escape"); // no dialog: must not steal focus
  assert.equal(document.activeElement, t1);
  await m.unmount();
});

test("window picker refetches; out-of-order completion keeps the latest selection", async () => {
  const pending = {};
  const m = await mount({ routes: routesFor({
    usage: (u) => {
      const w = Number(new URL(u, "http://x").searchParams.get("window"));
      if (w === 30) return envelope(F.usage(30, [F.point("2026-11-01")]));
      pending[w] = deferred();
      return pending[w].promise;
    },
  }) });
  const btn = (n) => m.$$(".ud-seg-btn").find((b) => b.textContent === n + " days");
  assert.equal(btn(30).getAttribute("aria-pressed"), "true");
  await m.click(btn(7));
  await m.click(btn(90));
  assert.ok(m.calls.includes(B + "/usage?window=7"));
  assert.ok(m.calls.includes(B + "/usage?window=90"));
  // 90 answers first, then the stale 7 arrives late
  await act(async () => { pending[90].resolve(envelope(F.usage(90, [F.point("2026-08-01"), F.point("2026-08-02")]))); });
  await act(async () => { pending[7].resolve(envelope(F.usage(7, [F.point("2026-11-05")]))); });
  assert.equal(btn(90).getAttribute("aria-pressed"), "true");
  assert.equal(m.$(".usage-deck").dataset.window, "90");
  const days = m.$$("[data-zone=usage] tbody tr td:first-child").map((td) => td.textContent);
  assert.deepEqual(days, ["2026-08-01", "2026-08-02"]);
  await m.unmount();
});

test("window response for a different window is rejected as malformed", async () => {
  const m = await mount({ routes: routesFor({ usage: () => envelope(F.usage(7, [F.point("2026-11-01")])) }) });
  assert.equal(m.$("[data-zone=usage] [role=alert]").dataset.kind, "malformed");
  await m.unmount();
});

test("profile-wide chart: honest subtitle, badge, separate token and USD charts, gaps not bridged", async () => {
  const m = await mount({ routes: routesFor() });
  const z = m.$("[data-zone=usage]");
  assert.match(z.textContent, /This Hermes profile only — not subscription allowance\./);
  assert.equal(z.querySelector(".ud-series .ud-badge").dataset.confidence, "estimated");
  const charts = z.querySelectorAll("figure.ud-chart");
  assert.deepEqual([...charts].map((c) => c.dataset.chart), ["Tokens (count)", "Estimated spend (USD)"]);
  // 11-01, 11-02 contiguous -> one polyline; 11-04 after a missing day -> isolated point, not joined
  const tokenInput = [...charts[0].querySelectorAll("polyline.ud-line-a")];
  assert.equal(tokenInput.length, 1);
  assert.equal(tokenInput[0].getAttribute("points").split(" ").length, 2);
  assert.equal(charts[0].querySelectorAll("circle.ud-dot.ud-line-a").length, 3);
  assert.match(z.textContent, /Days in server local time/);
  assert.match(z.textContent, /partial coverage/);
  await m.unmount();
});

test("chart with only null observations shows no fake zero line", async () => {
  const nulls = [F.point("2026-11-01", { estimated_usd: null }), F.point("2026-11-02", { estimated_usd: null })];
  const m = await mount({ routes: routesFor({ usage: () => envelope(F.usage(30, nulls)) }) });
  const usd = m.$("[data-chart-empty='Estimated spend (USD)']");
  assert.ok(usd, "USD chart replaced by empty note");
  assert.match(usd.textContent, /No observations for this period\./);
  assert.ok(m.$("figure[data-chart='Tokens (count)']"));
  await m.unmount();
});

test("provider filter requests ?provider= and shows attribution-unavailable honestly", async () => {
  const m = await mount({ routes: routesFor({
    usage: (u) => {
      const p = new URL(u, "http://x").searchParams;
      const w = Number(p.get("window"));
      if (p.get("provider")) return envelope({ window_days: w, provider_id: p.get("provider"), series: [], reason: "attribution_unavailable" });
      return envelope(F.usage(w, [F.point("2026-11-01")]));
    },
  }) });
  const sel = m.$("select[aria-label='Provider filter']");
  assert.deepEqual([...sel.options].map((o) => o.value), ["", "openrouter", "nous", "anthropic", "ollama"]); // inventory order preserved
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value").set;
    setter.call(sel, "openrouter");
    sel.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
  assert.ok(m.calls.includes(B + "/usage?window=30&provider=openrouter"));
  const z = m.$("[data-zone=usage]");
  assert.match(z.textContent, /Provider attribution unavailable from session analytics\./);
  assert.equal(z.querySelectorAll("figure.ud-chart").length, 0, "no profile totals cloned onto the provider");
  assert.equal(z.querySelectorAll("polyline, circle").length, 0);
  await m.unmount();
});

test("provider filter narrows the rendered subscription cards (DOM count), and clearing restores all", async () => {
  const cards = [
    F.readyOpenRouter,
    F.opaqueNous,
    F.errorAnthropic,
    Object.assign({}, F.opaqueNous, { id: "acct_an2", account_id: "acct_an2", provider_id: "anthropic", provider_label: "Anthropic", account_label: "Account 2" }),
    F.selfHosted,
    Object.assign({}, F.opaqueNous, { id: "acct_or2", account_id: "acct_or2", provider_id: "openrouter", provider_label: "OpenRouter", account_label: "Account 2" }),
  ];
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards, inventory_state: "detected" }) }) });
  const count = () => m.$$("[data-zone=subscriptions] .ud-card").length;
  const ids = () => m.$$("[data-zone=subscriptions] .ud-card").map((c) => c.dataset.cardId);
  const pick = async (v) => act(async () => {
    const sel = m.$("select[aria-label='Provider filter']");
    Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value").set.call(sel, v);
    sel.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
  assert.equal(count(), 6);
  await pick("anthropic");
  assert.equal(count(), 2, "only the Anthropic cards remain in the DOM");
  assert.deepEqual(ids(), ["acct_an", "acct_an2"]);
  assert.match(m.$("[data-zone=subscriptions] [data-filter-note]").textContent, /Showing 2 of 6/);
  await pick("ollama");
  assert.deepEqual(ids(), ["acct_ol"]);
  await pick("");
  assert.equal(count(), 6);
  assert.equal(m.$("[data-filter-note]"), null);
  await m.unmount();
});

test("changing the provider filter closes an open JSON dialog instead of leaving it dangling", async () => {
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [F.readyOpenRouter, F.errorAnthropic], inventory_state: "detected" }) }) });
  await m.click(m.$$(".ud-card-foot .ud-btn")[0]);
  assert.ok(m.$("[role=dialog]"));
  await act(async () => {
    const sel = m.$("select[aria-label='Provider filter']");
    Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value").set.call(sel, "anthropic");
    sel.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
  assert.equal(m.$("[role=dialog]"), null);
  assert.equal(m.$$("[data-zone=subscriptions] .ud-card").length, 1);
  await m.unmount();
});

test("health strip: status text+icon, latency, measured CPU/memory only when non-null, badge per row", async () => {
  const entries = [
    F.healthEntry(),
    F.healthEntry({ endpoint_id: "ep_2", display_name: "Bare vLLM", status: "degraded", confidence: "opaque",
      metrics_available: false, latency_ms: null, cpu_percent: null, memory_bytes: null, reason: "metrics_unavailable" }),
    F.healthEntry({ endpoint_id: "ep_3", display_name: "Down box", status: "offline", confidence: "self_hosted",
      metrics_available: false, latency_ms: null, cpu_percent: null, memory_bytes: null,
      error: { code: "timeout", message: "The endpoint did not answer in time.", retryable: true, provider_id: "x", account_id: null } }),
  ];
  const m = await mount({ routes: routesFor({ health: () => envelope({ entries }) }) });
  const rows = m.$$(".ud-health-row");
  assert.equal(rows.length, 3);
  assert.match(rows[0].textContent, /● Healthy/);
  assert.match(rows[0].textContent, /42 ms/);
  assert.match(rows[0].textContent, /CPU 12\.5%/);
  assert.match(rows[0].textContent, /Memory 2\.0 GiB/);
  assert.equal(rows[0].querySelector(".ud-badge").dataset.confidence, "self_hosted");
  assert.equal(rows[0].querySelector(".ud-badge").textContent, "Self-hosted");
  assert.match(rows[1].textContent, /▲ Degraded/);
  assert.match(rows[1].textContent, /Latency —/);
  assert.ok(!/CPU|Memory|\b0 ms/.test(rows[1].textContent));
  assert.match(rows[1].textContent, /Metrics unavailable/);
  assert.equal(rows[1].querySelector(".ud-badge").dataset.confidence, "opaque");
  assert.match(rows[2].textContent, /✕ Offline/);
  // offline row claims self_hosted but has no metrics -> demoted
  assert.equal(rows[2].querySelector(".ud-badge").dataset.confidence, "opaque");
  assert.match(rows[2].textContent, /The endpoint did not answer in time\./);
  assert.ok(!/\b0 ms/.test(rows[2].textContent));
  await m.unmount();
});

test("failed refresh keeps prior data, visibly stale, and the card goes to error banner", async () => {
  let fail = false;
  const m = await mount({ routes: routesFor({
    subs: () => { if (fail) throw new Error("500: boom"); return envelope({ cards: [F.readyOpenRouter], inventory_state: "detected" }); },
  }) });
  assert.equal(m.$$("article.ud-card").length, 1);
  fail = true;
  const refresh = m.$$(".ud-header-actions .ud-btn")[0];
  await m.click(refresh);
  await m.flush(5);
  const zone = m.$("[data-zone=subscriptions]");
  assert.ok(zone.querySelector("[role=alert]"), "error shown");
  assert.equal(zone.querySelectorAll("article.ud-card").length, 1, "prior data kept");
  assert.ok(zone.querySelector(".ud-stale"), "stale tag");
  assert.ok(zone.querySelector(".ud-card-stale"));
  // recovery
  fail = false;
  await m.click(m.$$(".ud-header-actions .ud-btn")[0]);
  await m.flush(5);
  assert.equal(m.$("[data-zone=subscriptions] [role=alert]"), null);
  assert.equal(m.$("[data-zone=subscriptions] .ud-stale"), null);
  await m.unmount();
});

test("profile change discards prior-profile data immediately", async () => {
  const profile = { current: "a" };
  const d = deferred();
  let n = 0;
  const m = await mount({ profile, routes: routesFor({
    subs: () => { n += 1; return n === 1 ? envelope({ cards: [F.readyOpenRouter], inventory_state: "detected" }) : d.promise; },
  }) });
  assert.equal(m.$$("article.ud-card").length, 1);
  profile.current = "b";
  await m.rerender();
  assert.equal(m.$$("article.ud-card").length, 0, "prior-profile cards gone");
  assert.ok(m.$("[data-zone=subscriptions] .ud-skel"));
  assert.equal(m.$(".usage-deck").dataset.phase, "first_run");
  await act(async () => { d.resolve(envelope({ cards: [F.opaqueNous], inventory_state: "detected" })); });
  assert.equal(m.$$("article.ud-card").length, 1);
  assert.ok(m.$("[data-card-id=acct_nous]"));
  await m.unmount();
});

function setVisibility(state) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new dom.window.Event("visibilitychange"));
}

test("polls while visible and stops while hidden", async () => {
  const m = await mount({ routes: routesFor(), pollMs: 30 });
  const subsCalls = () => m.calls.filter((u) => u === B + "/subscriptions").length;
  assert.equal(subsCalls(), 1);
  await m.flush(80);
  const visibleCount = subsCalls();
  assert.ok(visibleCount >= 2, "polled while visible, got " + visibleCount);
  await act(async () => { setVisibility("hidden"); });
  const atHide = subsCalls();
  await m.flush(120);
  assert.equal(subsCalls(), atHide, "no polling while hidden");
  await act(async () => { setVisibility("visible"); });
  await m.flush(80);
  assert.ok(subsCalls() > atHide, "polling resumed on visible");
  await m.unmount();
  const afterUnmount = subsCalls();
  await new Promise((r) => setTimeout(r, 90));
  assert.equal(subsCalls(), afterUnmount, "timers cleared on unmount");
});

test("manual Refresh re-requests all three zones and explains the cache", async () => {
  const m = await mount({ routes: routesFor() });
  const before = m.calls.length;
  const btn = m.$$(".ud-header-actions .ud-btn")[0];
  assert.match(btn.getAttribute("title"), /30-second cache/);
  await m.click(btn);
  await m.flush(5);
  const added = m.calls.slice(before);
  assert.deepEqual(added.sort(), [B + "/health", B + "/subscriptions", B + "/usage?window=30"].sort());
  await m.unmount();
});

test("card shows missing vs rejected credential state with a tooltip", async () => {
  const missing = F.card({ id: "acct_m", account_id: "acct_m", auth_status: "missing" });
  const rejected = F.card({ id: "acct_r", account_id: "acct_r", auth_status: "rejected" });
  const m = await mount({ routes: routesFor({ subs: () => envelope({ cards: [missing, rejected], inventory_state: "detected" }) }) });
  const mSpan = m.$("[data-card-id=acct_m] [data-auth-status]");
  const rSpan = m.$("[data-card-id=acct_r] [data-auth-status]");
  assert.equal(mSpan.dataset.authStatus, "missing");
  assert.equal(mSpan.textContent, "No credential found");
  assert.match(mSpan.getAttribute("title"), /none was sent/);
  assert.equal(rSpan.dataset.authStatus, "rejected");
  assert.equal(rSpan.textContent, "Credential rejected");
  assert.match(rSpan.getAttribute("title"), /answered 401/);
  await m.unmount();
});
