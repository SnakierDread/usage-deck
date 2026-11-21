"use strict";
// QA gates for the API-equivalent ($) feature, UI side:
//   C1  spend display is byte-identical to the pre-feature tab (golden captured from origin/main c4a662d)
//   C2  null renders distinctly from 0 in every table and in the totals card
//   RO  rendering is read-only: GET only, no storage writes, no beacons, no XHR, no window.fetch
// Assertions compare strings/booleans only (never a jsdom node: a failing diff walks the whole DOM tree).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { mod, mount, envelope, dom } = require("./harness");
const P = require("./spend_parity_probe");

const GOLDEN = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "spend_golden_v1.json"), "utf8"));
const B = mod.API_BASE;
const norm = (el) => el.textContent.replace(/\s+/g, " ").trim();

function same(actual, expected, what) {
  const a = JSON.stringify(actual, null, 1), e = JSON.stringify(expected, null, 1);
  assert.ok(a === e, what + " differs from the pre-feature snapshot:\n--- actual\n" + a + "\n--- expected\n" + e);
}

// ------------------------------------------------------------------ C1

test("C1: the golden snapshot is pre-feature and covers all three dimensions", () => {
  assert.deepEqual(Object.keys(GOLDEN), ["all", "profile", "model"]);
  const text = JSON.stringify(GOLDEN);
  assert.ok(!/API-equivalent|api_equivalent/i.test(text));
  for (const dim of Object.keys(GOLDEN)) {
    assert.equal(GOLDEN[dim].totals_est_usd, "$1,246.12", dim);  // 1.25+0.3125+9.99+1234.5678+1.25e-5 in the legacy fixture
    assert.ok(GOLDEN[dim].tables.length >= 1, dim);
    assert.ok(GOLDEN[dim].tables.every((t) => t.cells.length > 0), dim);
  }
});

test("C1: a legacy-shaped payload renders exactly the pre-feature spend strings (totals, every Est. USD column, chart labels)", async () => {
  same(await P.collect(), GOLDEN, "spend display");
});

test("C1: the same data enriched with API-equivalent fields leaves every spend string byte-identical", async () => {
  same(await P.collectEnriched(), GOLDEN, "spend display with API-equivalent fields present");
});

test("C1: exact host zeros, subtotals and sub-cent spend keep their pre-feature spelling next to the new column", async () => {
  const m = await mount({ routes: P.routesFor(P.enrichedPayload) });
  const spend = m.$$("[data-rows-table=provenance] [data-field=estimated_usd]").map(norm);
  assert.deepEqual(spend, ["$1,234.57", "$0.00", "$9.99", "$0.00", "$0.3125", "$0.00", "$0.00", "$1.25"]);
  assert.equal(norm(m.$$("[data-usage-totals] dd")[3]), "$1,246.12");
  await m.unmount();
});

// ------------------------------------------------------------------ C2

function withZeroAndNull(dimension) {
  const data = P.enrichedPayload(dimension);
  const set = (m, pico) => {
    m.api_equivalent_usd = pico === null ? null : Number(pico) / 1e12;
    m.api_equivalent = { amount_pico_usd: pico, priced_rows: pico === null ? 0 : 1, unpriced_rows: pico === null ? 1 : 0, proxy_rows: 0,
      partial: pico === null, stale: false };
  };
  // r1 -> an exact priced zero, r5 (mystery-model) -> unpriced; every other leaf keeps its enriched value.
  const byId = Object.fromEntries(data.rows.map((r) => [r.id, r]));
  set(byId[P.rowId("r1")].metrics, "0");
  set(byId[P.rowId("r5")].metrics, null);
  Object.assign(byId[P.rowId("r5")].pricing, { catalog_key: null, method: "none", alias_kind: null, note: null, reason: "no_catalog_match", source: null, fetched_at: null });
  return data;
}

test("C2: null is a dash and a priced zero is $0.00, distinguishable in the DOM, in every row table and the totals card", async () => {
  const m = await mount({ routes: P.routesFor(withZeroAndNull) });
  const provenance = m.$("[data-rows-table=provenance]");
  const cell = (id) => provenance.querySelector("[data-row-id=" + P.rowId(id) + "] [data-field=api_equivalent_usd]");
  const nullCell = cell("r5"), zeroCell = cell("r1");
  assert.equal(nullCell.dataset.apiState, "null");
  assert.equal(zeroCell.dataset.apiState, "zero");
  assert.equal(norm(nullCell.querySelector(".ud-api-amount")), "—");
  assert.equal(norm(zeroCell.querySelector(".ud-api-amount")), "$0.00");
  assert.ok(nullCell.dataset.apiPico === undefined, "a null cell must expose no amount");
  assert.equal(zeroCell.dataset.apiPico, "0");
  assert.ok(nullCell.title !== zeroCell.title);
  assert.match(nullCell.title, /Not priced/);
  assert.ok(!/\$0\.00/.test(norm(nullCell)), "null must never read as $0.00: " + norm(nullCell));
  assert.ok(!/—/.test(norm(zeroCell.querySelector(".ud-api-amount"))), "zero must never read as a dash");
  // every API cell in the whole tab agrees with its own state attribute
  let seen = 0;
  for (const el of m.$$("[data-field=api_equivalent_usd]")) {
    const amount = norm(el.querySelector(".ud-api-amount") || el);
    const state = el.dataset.apiState;
    assert.ok(["value", "zero", "null"].includes(state), "unknown api state " + state);
    if (state === "null") assert.ok(amount.startsWith("—"), "null cell rendered " + amount);
    if (state === "zero") assert.ok(amount.startsWith("$0.00"), "zero cell rendered " + amount);
    if (state === "value") assert.ok(amount.startsWith("$") || amount.startsWith("<$"), "value cell rendered " + amount);
    seen += 1;
  }
  assert.ok(seen >= 8, "expected the column in totals + daily + provenance, saw " + seen);
  await m.unmount();
});

test("C2: unpriced model ids are listed in the unknown bucket and are not in any sum shown", async () => {
  const m = await mount({ routes: P.routesFor(withZeroAndNull) });
  const bucket = m.$("[data-unpriced-models]");
  assert.ok(bucket !== null, "the unpriced bucket must be rendered");
  assert.ok(norm(bucket).includes("mystery-model"));
  assert.ok(norm(bucket).includes("qwen3.6:35b"));
  assert.ok(!norm(bucket).includes("gpt-6-luna"), "priced models are not in the bucket");
  assert.ok(!norm(bucket).includes("claude-sonnet-5-5"), "priced models are not in the bucket");
  const totals = m.$("[data-usage-totals] [data-field=api_equivalent_usd]");
  assert.equal(totals.dataset.apiState, "value");
  assert.match(norm(totals), /Partial · 2 unpriced/);
  // the total excludes the two unpriced leaves: r1..r4, r7, r8 only (r1 is the enriched $0.37 here; the zero edit is row-level)
  assert.equal(totals.dataset.apiPico, String(BigInt("370000000000") + BigInt("740000000000") + BigInt("1110000000000") +
    BigInt("1480000000000") + BigInt("2590000000") + BigInt("5550000000000")));
  await m.unmount();
});

test("C2: by-model group rows keep null apart from zero too", async () => {
  const m = await mount({ routes: P.routesFor(withZeroAndNull) });
  await m.click(m.$("[data-dimension=model]"));
  await m.flush(0);
  const table = m.$("[data-summary-table=model]");
  const states = Object.fromEntries(Array.from(table.querySelectorAll("tbody tr")).map((tr) =>
    [tr.dataset.groupId, tr.querySelector("[data-field=api_equivalent_usd]").dataset.apiState]));
  assert.equal(states['model:"mystery-model"'], "null");
  assert.ok(Object.values(states).every((s) => ["value", "zero", "null"].includes(s)));
  await m.unmount();
});

// ------------------------------------------------------------------ read-only

test("RO: rendering and switching every dimension issues only GETs to the plugin API and writes nothing client-side", async () => {
  const w = dom.window;
  const writes = [];
  const wrap = (obj, name, label) => {
    if (!obj || typeof obj[name] !== "function") return () => {};
    const orig = obj[name];
    obj[name] = function () { writes.push(label); return orig.apply(this, arguments); };
    return () => { obj[name] = orig; };
  };
  const undo = [
    wrap(w.Storage.prototype, "setItem", "localStorage/sessionStorage.setItem"),
    wrap(w.Storage.prototype, "removeItem", "storage.removeItem"),
    wrap(w.Storage.prototype, "clear", "storage.clear"),
    wrap(w.navigator, "sendBeacon", "sendBeacon"),
    wrap(w.XMLHttpRequest.prototype, "open", "XMLHttpRequest.open"),
    wrap(w.XMLHttpRequest.prototype, "send", "XMLHttpRequest.send"),
  ];
  const fetchCalls = [];
  const orig = P.routesFor(withZeroAndNull);
  const inits = [];
  const m = await mount({ routes: (url, init) => { inits.push(init === undefined ? null : init); return orig(url, init); } });
  try {
    await m.click(m.$("[data-dimension=profile]"));
    await m.flush(0);
    await m.click(m.$("[data-dimension=model]"));
    await m.flush(0);
    await m.click(m.$("[data-dimension=all]"));
    await m.flush(0);
    for (const url of m.calls) {
      fetchCalls.push(url);
      assert.ok(url.startsWith(B + "/"), "request outside the plugin API: " + url);
      assert.match(url, /^[^?]*\/(subscriptions|health|usage)(\?|$)/, url);
    }
    for (const init of inits) {
      const method = init && init.method ? String(init.method).toUpperCase() : "GET";
      assert.equal(method, "GET");
      assert.ok(!init || init.body === undefined, "a render-time request carried a body");
    }
    assert.ok(fetchCalls.some((u) => u.includes("/usage?")), "the usage endpoint was exercised");
    assert.deepEqual(writes, []);
  } finally {
    undo.forEach((u) => u());
    await m.unmount();
  }
});
