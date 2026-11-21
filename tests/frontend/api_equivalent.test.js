"use strict";
// API-equivalent ($) in the dashboard tab: totals card, All / by-model / by-profile tables, provenance rows,
// the explicit unpriced bucket, null-vs-zero, proxy / stale / partial qualifiers, and byte-identical spend.
// Also pins the shared pure-helper block of dist/index.js and desktop/plugin.js to one implementation.
// All fixture data is synthetic.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;
const ROOT = path.resolve(__dirname, "../../plugins/usage_deck");
const BEGIN = "// <api-equivalent-helpers>";
const END = "// </api-equivalent-helpers>";

function helperBlock(file) {
  const text = fs.readFileSync(path.join(ROOT, file), "utf8");
  const a = text.indexOf(BEGIN), b = text.indexOf(END);
  assert.ok(a >= 0 && b > a, "helper markers in " + file);
  return text.slice(a, b + END.length);
}

function loadHelpers(source) {
  return vm.runInNewContext(source + "\n;({ apiPico, picoToUsd, fmtApi, apiView, apiFlags, apiTitle, unpricedModels, pricingNotes, " +
    "API_LABEL, API_TIP, API_FOOTNOTE })", { BigInt, JSON, Object, Array, Math, String, isFinite });
}

// ------------------------------------------------------------------ fixture builder (BigInt, independent of the UI)

const NOTE = "Prices as a cataloged tariff; not subscription spend.";
const LEAVES = [
  // id, day, profile, global?, model, pico (string|null), kind, reason, stale, est. spend
  ["r1", "2026-11-05", "global", true, "gpt-6-luna", "123456780000", "equivalent", null, false, 0],
  ["r2", "2026-11-05", "alpha", false, "gpt-6.1-sol", "5000000000000", "equivalent", null, false, 1.25],
  ["r3", "2026-11-04", "alpha", false, "claude-sonnet-5-5", "1234567890123456", "equivalent", null, false, 0],
  ["r4", "2026-11-04", "global", true, "qwen3.8-flash-next", "2500000000", "proxy", null, false, 0],
  ["r5", "2026-11-03", "alpha", false, "qwen3.6:35b", "0", "proxy", null, false, 0],
  ["r6", "2026-11-03", "alpha", false, "cyber-tiel-coder:35b", "7000000000000", "proxy", null, false, 0],
  ["r7", "2026-11-02", "global", true, "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k", "9999999999", "proxy", null, true, 0],
  ["r8", "2026-11-02", "global", true, "mystery-model", null, null, "no_catalog_match", false, 0.5],
];

const rowId = (k) => "row_" + k.padEnd(64, "0");

function leaf(l) {
  const [k, day, profile, isGlobal, model, pico, kind, reason, stale, est] = l;
  const priced = pico !== null;
  return {
    id: rowId(k), day, profile, is_global: isGlobal, source: "state.db", model_id: model, provider_id: "custom:lane",
    attribution_source: "session_provider",
    metrics: Object.assign(F.metrics({ input_count: 1000, output_count: 100, estimated_usd: est, sessions: 1, api_calls: 1 }),
      agg([l])),
    pricing: {
      catalog_key: priced ? "litellm:vendor/" + model : null, method: priced ? "alias" : "none", alias_kind: kind,
      note: priced ? NOTE : null, reason, source: priced ? "litellm" : null,
      fetched_at: priced ? "2026-11-05T12:00:00.831243Z" : null, stale,
    },
  };
}

function agg(leaves) {
  let total = null, priced = 0, unpriced = 0, proxy = 0, stale = false;
  for (const l of leaves) {
    if (l[5] === null) { unpriced += 1; continue; }
    total = (total === null ? BigInt(0) : total) + BigInt(l[5]);
    priced += 1;
    proxy += l[6] === "proxy" ? 1 : 0;
    stale = stale || l[8];
  }
  return {
    api_equivalent_usd: total === null ? null : Number(total) / 1e12,
    api_equivalent: { amount_pico_usd: total === null ? null : total.toString(), priced_rows: priced, unpriced_rows: unpriced,
      proxy_rows: proxy, partial: unpriced > 0, stale },
  };
}

function sums(leaves) {
  const s = (i) => leaves.reduce((n, l) => n + (i === 9 ? l[9] : 1000), 0);
  return Object.assign(F.metrics({ input_count: s(0), output_count: leaves.length * 100, estimated_usd: s(9),
    sessions: leaves.length, api_calls: leaves.length }), agg(leaves));
}

function groupSeries(id, leaves) {
  const days = [...new Set(leaves.map((l) => l[1]))].sort();
  return F.series(days.map((day) => {
    const m = sums(leaves.filter((l) => l[1] === day));
    return { day, input_count: m.input_count, output_count: m.output_count, estimated_usd: m.estimated_usd, sessions: m.sessions,
      api_calls: m.api_calls, confidence: "estimated", api_equivalent_usd: m.api_equivalent_usd, api_equivalent: m.api_equivalent };
  }), { id, scope: "machine" });
}

function group(id, label, leaves, extra) {
  return Object.assign({ id, label, series_id: id, model_id: null, provider_ids: ["custom:lane"], profiles: [], row_ids: leaves.map((l) => rowId(l[0])).sort(),
    totals: sums(leaves), is_other: false }, extra || {});
}

function payload(dimension, opts) {
  const o = opts || {};
  const leaves = o.leaves || LEAVES;
  const rows = leaves.map(leaf);
  let groups;
  if (dimension === "all") groups = [group("all", "All", leaves)];
  else if (dimension === "profile") {
    groups = ["global", "alpha"].map((p) => group("profile:" + JSON.stringify([p === "global", p]), p, leaves.filter((l) => l[2] === p)));
  } else {
    const other = leaves.filter((l) => l[4] === "gpt-6-luna" || l[4] === "qwen3.8-flash-next");
    groups = leaves.filter((l) => !other.includes(l)).map((l) => group("model:" + JSON.stringify(l[4]), l[4] + " (Custom)", [l], { model_id: l[4] }));
    groups.push(group("other", "Other", other, { is_other: true }));
  }
  const unknown = rows.filter((r) => r.metrics.api_equivalent.amount_pico_usd === null).map((r) => r.id).sort();
  return Object.assign({
    window_days: 30, mode: "machine", dimension, provider_id: null,
    series: groups.map((g) => groupSeries(g.series_id, leaves.filter((l) => g.row_ids.includes(rowId(l[0]))))),
    groups, rows, totals: sums(leaves), providers: [{ id: "custom:lane", label: "Custom: lane" }],
    sources: [{ profile: "global", is_global: true, state: "ok" }], complete: true, top_n: dimension === "model" ? 8 : null, reason: null,
    pricing: { catalog_revision: "a".repeat(64), origin: "seed", warnings: o.warnings || [], unknown_row_ids: unknown },
  }, o.over || {});
}

function routes(make) {
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return envelope({ cards: [], inventory_state: "empty", view: "machine" });
    if (url.startsWith(B + "/health")) return envelope({ entries: [] });
    if (url.startsWith(B + "/usage")) {
      const q = new URL(url, "http://x").searchParams;
      return envelope(make(q.get("dimension"), q));
    }
    throw new Error("404: unexpected url " + url);
  };
}

const SUM = LEAVES.filter((l) => l[5] !== null).reduce((n, l) => n + BigInt(l[5]), BigInt(0));
const txt = (el) => el.textContent.replace(/\s+/g, " ").trim();

// ------------------------------------------------------------------ pure helpers

test("fmtApi: null is a dash, exact zero is $0.00, sub-cent is <$0.01, rounding is on exact integers", () => {
  const H = loadHelpers(helperBlock("dist/index.js"));
  const cases = [
    [null, "—"], ["0", "$0.00"], ["1", "<$0.01"], ["9999999999", "<$0.01"], ["10000000000", "$0.01"],
    ["14999999999", "$0.01"], ["15000000000", "$0.02"], ["1000000000000", "$1.00"], ["1005000000000", "$1.01"],
    ["1234567890123456", "$1,234.57"], ["123456789012345678901234", "$123,456,789,012.35"],
  ];
  for (const [pico, want] of cases) assert.equal(H.fmtApi(pico), want, String(pico));
  assert.equal(H.picoToUsd("1234567"), "0.000001234567");
  assert.equal(H.picoToUsd("1000000000000"), "1");
  assert.equal(H.picoToUsd("0"), "0");
});

test("apiView keeps null, zero and value apart programmatically and never reads a float or a malformed string", () => {
  const H = loadHelpers(helperBlock("dist/index.js"));
  const mk = (pico, over) => ({ api_equivalent_usd: 1, api_equivalent: Object.assign({ amount_pico_usd: pico, priced_rows: 1, unpriced_rows: 0, proxy_rows: 0, partial: false, stale: false }, over) });
  assert.equal(H.apiView(mk(null)).state, "null");
  assert.equal(H.apiView(mk("0")).state, "zero");
  assert.equal(H.apiView(mk("5")).state, "value");
  for (const bad of ["05", "-1", "1.5", "abc", 5, true, {}]) assert.equal(H.apiView(mk(bad)).state, "null", String(bad));
  assert.equal(H.apiView({}).state, "null");  // older backend: unreported is a dash, not a fabricated zero
  assert.equal(H.apiView(null).text, "—");
  assert.equal(H.apiView(mk("0")).text, "$0.00");
  assert.equal(H.apiView({ api_equivalent_usd: 0, api_equivalent: null }).state, "null");  // a float 0 alone is not zero
});

test("apiFlags: partial subtotal, unpriced-only, price proxy, stale prices", () => {
  const H = loadHelpers(helperBlock("dist/index.js"));
  const v = (over) => H.apiView({ api_equivalent: Object.assign({ amount_pico_usd: "5", priced_rows: 3, unpriced_rows: 0, proxy_rows: 0, partial: false, stale: false }, over) });
  const texts = (view) => JSON.parse(JSON.stringify(H.apiFlags(view).map((f) => f.text)));  // vm-realm arrays -> plain
  assert.deepEqual(texts(v({})), []);
  assert.deepEqual(texts(v({ unpriced_rows: 2, partial: true })), ["Partial · 2 unpriced"]);
  assert.deepEqual(texts(v({ amount_pico_usd: null, priced_rows: 0, unpriced_rows: 2, partial: true })), ["2 unpriced"]);
  assert.deepEqual(texts(v({ proxy_rows: 1 })), ["Price proxy · 1 of 3"]);
  assert.deepEqual(texts(v({ priced_rows: 1, proxy_rows: 1 })), ["Price proxy"]);
  assert.deepEqual(texts(v({ stale: true })), ["Stale prices"]);
  assert.deepEqual(texts(v({ unpriced_rows: 1, partial: true, proxy_rows: 3, stale: true })),
    ["Partial · 1 unpriced", "Price proxy · 3 of 3", "Stale prices"]);
});

test("dist and desktop share one byte-identical helper block (no independent pricing logic in either)", () => {
  const dist = helperBlock("dist/index.js");
  const desktop = helperBlock("desktop/plugin.js");
  assert.equal(desktop, dist);
  assert.ok(!/https?:\/\//.test(dist.replace(/\/\*[\s\S]*?\*\//g, "")), "no URL in the helper block");
  assert.ok(!/fetch\(|XMLHttpRequest|litellm|openrouter/i.test(dist.replace(/\/\/.*$|\/\*[\s\S]*?\*\//gm, "").replace(/"[^"\n]*"/g, '""')),
    "helpers format backend strings only: no fetch, no catalog, no vendor");
  // the helpers must behave identically when loaded from the desktop source
  const a = loadHelpers(dist), b = loadHelpers(desktop);
  for (const pico of [null, "0", "1", "10000000000", "1234567890123456"]) assert.equal(a.fmtApi(pico), b.fmtApi(pico));
});

// ------------------------------------------------------------------ rendered tabs

test("All: totals card shows API-equivalent beside spend, with footnote, partial, proxy and stale qualifiers", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim, { warnings: ["pricing_stale"] })) });
  const totals = m.$("[data-usage-totals]");
  const labels = m.$$("[data-usage-totals] dt").map((e) => e.textContent);
  assert.deepEqual(labels, ["Input", "Output", "Total tokens", "Est. USD", "API-equivalent ($)", "Sessions", "API calls"]);
  const cell = totals.querySelector("[data-field=api_equivalent_usd]");
  assert.equal(cell.dataset.apiState, "value");
  assert.equal(cell.dataset.apiPico, SUM.toString());
  const { fmtApi } = mod;
  assert.ok(txt(cell).startsWith(fmtApi(SUM.toString())), txt(cell));
  assert.match(txt(cell), /Partial · 1 unpriced/);
  assert.match(txt(cell), /Price proxy · 4 of 7/);
  assert.match(txt(cell), /Stale prices/);
  assert.equal(totals.querySelector("dt[title]").title.startsWith("Hypothetical cost at current base text API rates; not actual spend."), true);
  assert.match(totals.querySelector("[data-api-footnote]").textContent, /hypothetical figure, not actual spend/);
  assert.ok(cell.title.includes("Exact: $" + mod.picoToUsd(SUM.toString()) + " (" + SUM + " pico-USD)"), cell.title);
  assert.match(cell.title, /7 priced, 1 unpriced, 4 on a price proxy of 8 daily rows\./);
  assert.match(m.$("[data-pricing-warning=pricing_stale]").textContent, /Stale prices: some catalog prices are more than 7 days old/);
  // existing spend is untouched, byte for byte
  const spend = m.$$("[data-usage-totals] dd")[3];
  assert.equal(txt(spend), "$1.75");
  await m.unmount();
});

test("All: provenance rows carry the column; null renders a dash with the reason, zero renders $0.00", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim)) });
  const table = m.$("[data-rows-table=provenance]");
  const heads = Array.from(table.querySelectorAll("th")).map((e) => e.textContent);
  assert.deepEqual(heads, ["Day", "Profile DB", "Model", "Provider", "Input", "Output", "Est. USD", "API-equivalent ($)", "Sessions", "API calls"]);
  assert.equal(table.querySelector("th[title]").title.startsWith("Hypothetical cost"), true);
  const cellOf = (k) => table.querySelector("[data-row-id=" + rowId(k) + "] [data-field=api_equivalent_usd]");
  assert.equal(cellOf("r8").dataset.apiState, "null");
  assert.equal(txt(cellOf("r8").querySelector(".ud-api-amount")), "—");
  assert.match(cellOf("r8").title, /Not priced: no price known for this model id\. Excluded from every API-equivalent sum; not zero\./);
  assert.equal(cellOf("r5").dataset.apiState, "zero");
  assert.equal(txt(cellOf("r5").querySelector(".ud-api-amount")), "$0.00");
  assert.equal(cellOf("r5").dataset.apiPico, "0");
  assert.equal(cellOf("r3").dataset.apiState, "value");
  assert.equal(txt(cellOf("r3").querySelector(".ud-api-amount")), "$1,234.57");
  assert.equal(txt(cellOf("r7").querySelector(".ud-api-amount")), "<$0.01");  // sub-cent with exact value on hover
  assert.match(cellOf("r7").title, /Exact: \$0\.009999999999 \(9999999999 pico-USD\)/);
  // resolved key / method / proxy / note / fetched_at / stale are exposed per row
  assert.match(cellOf("r6").title, /Model: cyber-tiel-coder:35b\nPriced as litellm:vendor\/cyber-tiel-coder:35b \(alias, proxy\)\nPrices as a cataloged tariff; not subscription spend\./);
  assert.match(cellOf("r6").title, /Prices fetched 2026-11-05T12:00:00\.831243Z\n/);
  assert.match(cellOf("r7").title, /stale \(older than 7 days\)/);
  assert.match(txt(cellOf("r6")), /Price proxy/);
  assert.match(txt(cellOf("r7")), /Stale prices/);
  assert.match(txt(cellOf("r1")), /^\$0\.12$/);  // equivalent rows carry no qualifier
  await m.unmount();
});

test("All: the daily data table has the column too, null days stay a dash", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim)) });
  const rows = m.$$("details.ud-table-wrap:not([data-provenance]) tbody tr");
  assert.ok(rows.length >= 3);
  const byDay = Object.fromEntries(rows.map((r) => [r.firstChild.textContent, r.querySelector("[data-field=api_equivalent_usd]")]));
  assert.equal(byDay["2026-11-02"].dataset.apiState, "value");  // r7 priced + r8 unpriced = partial subtotal
  assert.match(txt(byDay["2026-11-02"]), /Partial · 1 unpriced/);
  assert.equal(byDay["2026-11-05"].dataset.apiState, "value");
  await m.unmount();
});

test("Unpriced models: explicit bucket lists exact model ids with reasons and is never summed", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim)) });
  const box = m.$("[data-unpriced-models]");
  assert.ok(box);
  assert.match(txt(box.querySelector("summary")), /^Unpriced models \(1 daily row\)$/);
  const li = box.querySelectorAll("li");
  assert.equal(li.length, 1);
  assert.equal(li[0].dataset.unpricedModel, "mystery-model");
  assert.equal(li[0].dataset.reason, "no_catalog_match");
  assert.match(txt(li[0]), /^mystery-model — no price known for this model id \(1 daily row\)$/);
  assert.match(txt(box), /excluded from every sum; they are not zero/);
  await m.unmount();
});

test("no unpriced rows: no bucket, no partial flag; all priced and no warnings is quiet", async () => {
  const leaves = LEAVES.filter((l) => l[5] !== null && !l[8]);
  const m = await mount({ routes: routes((dim) => payload(dim, { leaves })) });
  assert.ok(m.$("[data-unpriced-models]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  assert.ok(m.$("[data-pricing-warning]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  const cell = m.$("[data-usage-totals] [data-field=api_equivalent_usd]");
  assert.ok(cell.querySelector("[data-api-flag=partial]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  assert.ok(cell.querySelector("[data-api-flag=stale]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  await m.unmount();
});

test("By model: every group row has the column (incl. Other); unknown model is a dash; sums conserve in the DOM", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim)) });
  await m.click(m.$("[data-dimension=model]"));
  await m.flush(0);
  assert.ok(m.calls.some((u) => u.includes("dimension=model")));
  const table = m.$("[data-summary-table=model]");
  const heads = Array.from(table.querySelectorAll("th")).map((e) => e.textContent);
  assert.deepEqual(heads, ["Group", "Input", "Output", "Total tokens", "Est. USD", "API-equivalent ($)", "Sessions", "API calls"]);
  const cells = Array.from(table.querySelectorAll("tbody tr")).map((tr) => ({ id: tr.dataset.groupId, c: tr.querySelector("[data-field=api_equivalent_usd]") }));
  assert.equal(cells.length, 7);  // 6 model groups + Other
  const mystery = cells.find((x) => x.id === 'model:"mystery-model"');
  assert.equal(mystery.c.dataset.apiState, "null");
  assert.equal(txt(mystery.c.querySelector(".ud-api-amount")), "—");
  assert.equal(cells.find((x) => x.id === 'model:"qwen3.6:35b"').c.dataset.apiState, "zero");
  const other = cells.find((x) => x.id === "other");
  assert.equal(other.c.dataset.apiPico, (BigInt("123456780000") + BigInt("2500000000")).toString());
  assert.match(other.c.title, /2 priced, 0 unpriced, 1 on a price proxy of 2 daily rows\./);
  const sum = cells.reduce((n, x) => (x.c.dataset.apiPico ? n + BigInt(x.c.dataset.apiPico) : n), BigInt(0));
  assert.equal(sum.toString(), m.$("[data-usage-totals] [data-field=api_equivalent_usd]").dataset.apiPico);
  assert.equal(sum, SUM);
  // spend column unchanged
  const spend = Array.from(table.querySelectorAll("tbody tr [data-field=estimated_usd]")).map((e) => txt(e));
  assert.deepEqual(spend.sort(), ["$0.00", "$0.00", "$0.00", "$0.00", "$0.50", "$1.25", "$0.00"].sort());
  assert.ok(m.$("[data-unpriced-models]"));  // the bucket is shown in grouped views too
  await m.unmount();
});

test("By profile: the same column; per-profile partial flag; profiles sum to the grand total", async () => {
  const m = await mount({ routes: routes((dim) => payload(dim)) });
  await m.click(m.$("[data-dimension=profile]"));
  await m.flush(0);
  const table = m.$("[data-summary-table=profile]");
  const rows = Array.from(table.querySelectorAll("tbody tr"));
  assert.equal(rows.length, 2);
  const alpha = rows.find((r) => r.dataset.groupId === 'profile:[false,"alpha"]').querySelector("[data-field=api_equivalent_usd]");
  const global = rows.find((r) => r.dataset.groupId === 'profile:[true,"global"]').querySelector("[data-field=api_equivalent_usd]");
  assert.ok(alpha.querySelector("[data-api-flag=partial]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");  // alpha has no unpriced row
  assert.match(txt(global), /Partial · 1 unpriced/);
  assert.equal((BigInt(alpha.dataset.apiPico) + BigInt(global.dataset.apiPico)).toString(), SUM.toString());
  assert.equal(m.$("[data-usage-totals] [data-field=api_equivalent_usd]").dataset.apiPico, SUM.toString());
  await m.unmount();
});

test("an older backend without the new fields renders dashes, never zero, and keeps spend", async () => {
  const strip = (o) => JSON.parse(JSON.stringify(o, (k, v) => (k === "api_equivalent_usd" || k === "api_equivalent" || k === "pricing" ? undefined : v)));
  const m = await mount({ routes: routes((dim) => strip(payload(dim))) });
  const cell = m.$("[data-usage-totals] [data-field=api_equivalent_usd]");
  assert.equal(cell.dataset.apiState, "null");
  assert.equal(txt(cell.querySelector(".ud-api-amount")), "—");
  assert.equal(txt(m.$$("[data-usage-totals] dd")[3]), "$1.75");
  assert.ok(m.$("[data-unpriced-models]") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  await m.unmount();
});

test("alias notes and model ids are rendered as text, never markup", async () => {
  const evil = "<img src=x onerror=alert(1)>";
  const m = await mount({ routes: routes((dim) => {
    const d = payload(dim);
    d.rows[0].pricing.note = evil;
    return d;
  }) });
  assert.ok(m.$("img") === null, "selector must not match (no assert.equal on DOM nodes: a failing diff walks the whole jsdom tree)");
  assert.ok(m.$("[data-row-id=" + rowId("r1") + "] [data-field=api_equivalent_usd]").title.includes(evil));
  await m.unmount();
});
