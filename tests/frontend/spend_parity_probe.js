// C1 (UI): the spend display, captured from the dashboard tab.
//
// `collect()` renders the usage views (All / by-profile / by-model) from one fixed legacy-shaped payload (no
// API-equivalent fields; wire v2 exactly as the pre-feature backend served it) and returns every SPEND-related
// string the user sees: the totals card "Est. USD" value, the "Est. USD" column of every table, and the spend
// chart labels. tests/frontend/fixtures/spend_golden_v1.json was produced by running
//   node tests/frontend/spend_parity_probe.js --write tests/frontend/fixtures/spend_golden_v1.json
// on the PRE-FEATURE tree (origin/main c4a662d); spend_parity.test.js replays it on the feature branch.
// This module only depends on harness.js/fixtures.js, which exist unchanged on both trees.
"use strict";
const fs = require("node:fs");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;
const rowId = (k) => "row_" + k.padEnd(64, "0");

// id, day, profile, global?, model, provider, in, out, est. spend (null = not recorded)
const LEAVES = [
  ["r1", "2026-11-05", "global", true, "gpt-6-luna", "openrouter", 1000, 10, 1.25],
  ["r2", "2026-11-05", "alpha", false, "gpt-6.1-sol", "custom:lane", 250000, 12000, 0],
  ["r3", "2026-11-04", "alpha", false, "claude-sonnet-5-5", "anthropic", 40000, 900, 0.3125],
  ["r4", "2026-11-04", "global", true, "qwen3.8-flash-next", "custom:lane", 1000000, 200000, 0],
  ["r5", "2026-11-03", "alpha", false, "mystery-model", "custom:x", 77, 7, 9.99],
  ["r6", "2026-11-03", "global", true, "qwen3.6:35b", "custom:lane", null, null, 0],
  ["r7", "2026-11-02", "global", true, "gpt-6-luna", "openrouter", 3, 1, 0.0000125],
  ["r8", "2026-11-02", "alpha", false, "cyber-tiel-coder:35b", "custom:lane", 5000, 50, 1234.5678],
];

function sumKnown(values) {
  const known = values.filter((v) => v !== null);
  return known.length ? known.reduce((a, b) => a + b, 0) : null;
}

function metricsOf(ls) {
  return F.metrics({
    input_count: sumKnown(ls.map((l) => l[6])), output_count: sumKnown(ls.map((l) => l[7])),
    estimated_usd: sumKnown(ls.map((l) => l[8])), sessions: ls.length, api_calls: ls.length,
  });
}

function seriesOf(id, ls) {
  const days = [...new Set(ls.map((l) => l[1]))].sort();
  return F.series(days.map((day) => {
    const m = metricsOf(ls.filter((l) => l[1] === day));
    return { day, input_count: m.input_count, output_count: m.output_count, estimated_usd: m.estimated_usd,
      sessions: m.sessions, api_calls: m.api_calls, confidence: "estimated" };
  }), { id, scope: "machine" });
}

function groupOf(id, label, ls, extra) {
  return Object.assign({ id, label, series_id: id, model_id: null, provider_ids: [], profiles: [],
    row_ids: ls.map((l) => rowId(l[0])).sort(), totals: metricsOf(ls), is_other: false }, extra || {});
}

function legacyPayload(dimension) {
  let groups;
  if (dimension === "all") groups = [groupOf("all", "All", LEAVES)];
  else if (dimension === "profile") {
    groups = ["global", "alpha"].map((p) => groupOf("profile:" + JSON.stringify([p === "global", p]), p, LEAVES.filter((l) => l[2] === p)));
  } else {
    const other = LEAVES.filter((l) => l[4] === "gpt-6-luna");
    groups = LEAVES.filter((l) => !other.includes(l)).map((l) => groupOf("model:" + JSON.stringify(l[4]), l[4] + " (Custom)", [l], { model_id: l[4] }));
    groups.push(groupOf("other", "Other", other, { is_other: true }));
  }
  const rows = LEAVES.map((l) => ({
    id: rowId(l[0]), day: l[1], profile: l[2], is_global: l[3], source: "state.db", model_id: l[4], provider_id: l[5],
    attribution_source: "session_provider", metrics: metricsOf([l]),
  }));
  return {
    window_days: 30, mode: "machine", dimension, provider_id: null,
    series: groups.map((g) => seriesOf(g.series_id, LEAVES.filter((l) => g.row_ids.includes(rowId(l[0]))))),
    groups, rows, totals: metricsOf(LEAVES),
    providers: [{ id: "openrouter", label: "OpenRouter" }, { id: "anthropic", label: "Anthropic" }, { id: "custom:lane", label: "Custom: lane" },
      { id: "custom:x", label: "Custom: x" }],
    sources: [{ profile: "global", is_global: true, state: "ok" }], complete: true, top_n: dimension === "model" ? 8 : null, reason: null,
  };
}

// The same payload as a feature-aware backend serves it: every metrics object gains the additive fields.
// Per-leaf API-equivalent pico-USD (null = unpriced); r5 (mystery-model) and r6 are unpriced, the rest priced.
const LEAF_PICO = { r1: "370000000000", r2: "740000000000", r3: "1110000000000", r4: "1480000000000", r5: null, r6: null,
  r7: "2590000000", r8: "5550000000000" };

function enrichedPayload(dimension) {
  const data = legacyPayload(dimension);
  const leafOf = {};  // row id -> leaf key
  const dayOf = {};
  LEAVES.forEach((l) => { leafOf[rowId(l[0])] = l[0]; dayOf[rowId(l[0])] = l[1]; });
  // consistent additive annotation of a metrics-like object from the leaves it covers
  const annotate = (m, keys) => {
    let total = null, priced = 0, unpriced = 0;
    for (const k of keys) {
      if (LEAF_PICO[k] === null) { unpriced += 1; continue; }
      total = (total === null ? BigInt(0) : total) + BigInt(LEAF_PICO[k]);
      priced += 1;
    }
    m.api_equivalent_usd = total === null ? null : Number(total) / 1e12;
    m.api_equivalent = { amount_pico_usd: total === null ? null : total.toString(), priced_rows: priced, unpriced_rows: unpriced,
      proxy_rows: 0, partial: unpriced > 0, stale: false };
  };
  const all = LEAVES.map((l) => l[0]);
  annotate(data.totals, all);
  data.groups.forEach((g) => annotate(g.totals, g.row_ids.map((id) => leafOf[id])));
  data.rows.forEach((r) => {
    annotate(r.metrics, [leafOf[r.id]]);
    const priced = LEAF_PICO[leafOf[r.id]] !== null;
    r.pricing = { catalog_key: priced ? "litellm:x/" + r.model_id : null, method: priced ? "alias" : "none", alias_kind: priced ? "equivalent" : null,
      note: priced ? "n" : null, reason: priced ? null : "no_catalog_match", source: priced ? "litellm" : null,
      fetched_at: priced ? "2026-11-05T12:00:00.000000Z" : null, stale: false };
  });
  data.series.forEach((s) => {
    const g = data.groups.find((x) => x.series_id === s.id);
    s.points.forEach((p) => annotate(p, g.row_ids.filter((id) => dayOf[id] === p.day).map((id) => leafOf[id])));
  });
  data.pricing = { catalog_revision: "a".repeat(64), origin: "seed", warnings: [],
    unknown_row_ids: data.rows.filter((r) => LEAF_PICO[leafOf[r.id]] === null).map((r) => r.id).sort() };
  return data;
}

const norm = (el) => el.textContent.replace(/\s+/g, " ").trim();

function routesFor(make) {
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return envelope({ cards: [], inventory_state: "empty", view: "machine" });
    if (url.startsWith(B + "/health")) return envelope({ entries: [] });
    if (url.startsWith(B + "/usage")) return envelope(make(new URL(url, "http://x").searchParams.get("dimension")));
    throw new Error("404: unexpected url " + url);
  };
}

/** Strings only: no DOM node ever reaches an assertion. */
function spendOf(m) {
  const out = { totals_est_usd: null, tables: [], charts: [] };
  const totals = m.$("[data-usage-totals]");
  for (const div of totals.querySelectorAll("dl > div")) {
    if (norm(div.querySelector("dt")) === "Est. USD") out.totals_est_usd = norm(div.querySelector("dd"));
  }
  const tables = m.$$("table");
  tables.forEach((t) => {
    const heads = Array.from(t.querySelectorAll("thead th")).map((th) => th.textContent);
    const col = heads.indexOf("Est. USD");
    if (col < 0) return;
    const cells = Array.from(t.querySelectorAll("tbody tr")).map((tr) => norm(tr.children[col]));
    out.tables.push({ table: t.getAttribute("data-rows-table") || t.getAttribute("data-summary-table") || "daily#" + out.tables.length, column: "Est. USD", cells });
  });
  for (const svg of m.$$("svg[aria-label]")) {
    const label = svg.getAttribute("aria-label");
    if (/^Estimated spend/.test(label)) out.charts.push(label);
  }
  return out;
}

async function collectWith(make) {
  const out = {};
  const m = await mount({ routes: routesFor(make) });
  out.all = spendOf(m);
  // data tables are inside <details>; they are in the DOM whether or not they are open
  await m.click(m.$("[data-dimension=profile]"));
  await m.flush(0);
  out.profile = spendOf(m);
  await m.click(m.$("[data-dimension=model]"));
  await m.flush(0);
  out.model = spendOf(m);
  await m.unmount();
  return out;
}

const collect = () => collectWith(legacyPayload);
const collectEnriched = () => collectWith(enrichedPayload);

module.exports = { collect, collectEnriched, legacyPayload, enrichedPayload, routesFor, spendOf, LEAVES, rowId };

if (require.main === module) {
  const i = process.argv.indexOf("--write");
  if (i < 0 || !process.argv[i + 1]) { console.error("usage: node spend_parity_probe.js --write <path>"); process.exit(2); }
  collect().then((data) => {
    fs.mkdirSync(require("node:path").dirname(process.argv[i + 1]), { recursive: true });
    fs.writeFileSync(process.argv[i + 1], JSON.stringify(data, null, 2) + "\n");
    console.log("wrote " + process.argv[i + 1]);
    process.exit(0);
  });
}
