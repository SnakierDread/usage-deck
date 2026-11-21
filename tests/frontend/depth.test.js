"use strict";
// Analytics depth in the dashboard tab (docs/design/analytics-depth-breakdown.md §4-§7, §9): By repo, the local
// start-time heatmap (DST / fractional-offset correct), the cache-economics table, duration stats, null-vs-zero,
// and one shared helper block in dist/index.js and desktop/plugin.js. All fixture data is synthetic.
// The zone is pinned BEFORE any Date is formatted: the heatmap is browser-local by contract.
process.env.TZ = "America/New_York";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");

const B = mod.API_BASE;
const ROOT = path.resolve(__dirname, "../../plugins/usage_deck");
const BEGIN = "// <depth-helpers>";
const END = "// </depth-helpers>";
const txt = (el) => el.textContent.replace(/\s+/g, " ").trim();

function helperBlock(file) {
  const text = fs.readFileSync(path.join(ROOT, file), "utf8");
  const a = text.indexOf(BEGIN), b = text.indexOf(END);
  assert.ok(a >= 0 && b > a, "helper markers in " + file);
  return text.slice(a, b + END.length);
}

const NAMES = "decodeDepth, fmtDuration, durationItems, localHeat, localCellOf, heatLevel, heatMax, heatCellLabel, heatDetail, " +
  "localOffset, providerList, cacheRowView, depthStatusLine, repoRowFor, repoPathSummary, repoCoverage, DEPTH_DAYS, DEPTH_FIELDS";

function load(source) {
  // Date is shared with this process on purpose: the helpers must honour the process (browser) zone.
  return vm.runInNewContext(source + "\n;({ " + NAMES + " })", { Date, Intl, JSON, Object, Array, Math, String, Number, isFinite, isNaN });
}
const plain = (v) => JSON.parse(JSON.stringify(v));
const utc = (y, mo, d, h, mi) => Date.UTC(y, mo - 1, d, h, mi || 0) / 1000;

// ------------------------------------------------------------------ pure helpers

test("dist and desktop share one byte-identical depth helper block and behave identically", () => {
  const dist = helperBlock("dist/index.js"), desktop = helperBlock("desktop/plugin.js");
  assert.equal(desktop, dist);
  assert.doesNotMatch(dist, /fetch\(|fetchJSON|XMLHttpRequest|localStorage|innerHTML|eval\(|new Function/);
  const a = load(dist), b = load(desktop);
  const starts = [{ t: utc(2026, 11, 5, 14, 5), n: 2, tokens: 9 }];
  assert.deepEqual(plain(a.localHeat(starts)), plain(b.localHeat(starts)));
  assert.equal(a.fmtDuration(125), b.fmtDuration(125));
});

test("both halves wire the same additions: By repo, the three cards' data hooks and the decode", () => {
  for (const file of ["dist/index.js", "desktop/plugin.js"]) {
    const src = fs.readFileSync(path.join(ROOT, file), "utf8");
    assert.match(src, /DIMENSIONS = \[[^\]]*['"]repo['"]\]/, file);
    assert.match(src, /By repo/, file);
    assert.match(src, /decodeDepth\(d\)/, file);
    for (const hook of ["heatmap", "cache", "duration-summary", "repo-paths"]) {
      assert.ok(src.includes("data-" + hook) || src.includes("'data-" + hook) || src.includes("\"data-" + hook) || src.includes("depth-card"), hook + " in " + file);
    }
  }
});

test("decodeDepth: an older server (no members) is unsupported; a partial set is unsupported; nulls stay null", () => {
  const H = load(helperBlock("dist/index.js"));
  assert.equal(H.decodeDepth({}).supported, false);
  assert.equal(H.decodeDepth(null).supported, false);
  const full = { durations: null, duration_samples: null, heatmap: null, cache_metrics: null, repo_rollup: null, model_rollup: null,
    depth_totals: null, depth_status: null };
  const d = H.decodeDepth(full);
  assert.equal(d.supported, true);
  assert.deepEqual(plain([d.durations, d.heatmap, d.cache, d.repos, d.totals, d.status]), [null, null, null, null, null, null]);
  const partial = Object.assign({}, full);
  delete partial.heatmap;
  assert.equal(H.decodeDepth(partial).supported, false);
  const bad = H.decodeDepth(Object.assign({}, full, { heatmap: { grid: [], starts: [{ started_at: "x", sessions: 1, total_tokens: 1 }] } }));
  assert.equal(bad.heatmap.starts, null);  // an untrustworthy start list is dropped, never partially trusted
});

test("durations: null is a dash and never 0; exact seconds in the title; units", () => {
  const H = load(helperBlock("dist/index.js"));
  assert.equal(H.fmtDuration(null), "—");
  assert.equal(H.fmtDuration(0), "0 s");
  assert.equal(H.fmtDuration(42.5), "42.5 s");
  assert.equal(H.fmtDuration(125), "2m 5s");
  assert.equal(H.fmtDuration(3 * 3600 + 7 * 60 + 30), "3h 7m");
  for (const bad of [-1, NaN, Infinity, "5", true, undefined]) assert.equal(H.fmtDuration(bad), "—", String(bad));
  const items = plain(H.durationItems({ p50: 60, p90: null, p99: null, mean: 61.5, max: 120 }, 0));
  assert.deepEqual(items.map((i) => [i.id, i.text]), [["p50", "1m 0s"], ["p90", "—"], ["p99", "—"], ["mean", "1m 2s"], ["max", "2m 0s"], ["samples", "0"]]);
  assert.equal(items[0].title, "60 seconds");
  assert.equal(items[1].title, "Not recorded.");
  assert.equal(plain(H.durationItems(null, null)).every((i) => i.text === "—"), true);
});

test("heatmap C3: browser-local rebucketing, one bucket per session, DST fold and gap (America/New_York)", () => {
  const H = load(helperBlock("dist/index.js"));
  // midnight-spanning session: it started Thu 23:59 local and counts ONCE, in Thu 23 (never split across midnight)
  const span = [{ t: utc(2026, 11, 6, 4, 59), n: 1, tokens: 42 }];  // 2026-11-05 23:59 EST
  const cells = plain(H.localHeat(span));
  const hot = cells.map((c, i) => [i, c]).filter(([, c]) => c.sessions);
  assert.deepEqual(hot, [[3 * 24 + 23, { sessions: 1, tokens: 42 }]]);
  // the UTC grid would call this Fri 04: the local view must not
  assert.notEqual(hot[0][0], 4 * 24 + 4);
  // fall back (2026-11-01): 05:30Z = 01:30 EDT and 06:30Z = 01:30 EST are the SAME local cell, told apart by offset
  const fold = [{ t: utc(2026, 11, 1, 5, 30), n: 1, tokens: 10 }, { t: utc(2026, 11, 1, 6, 30), n: 2, tokens: null }];
  const f = plain(H.localHeat(fold));
  assert.deepEqual(f[6 * 24 + 1], { sessions: 3, tokens: 10 });  // Sunday 01:00, known tokens only
  const detail = plain(H.heatDetail(fold, 6, 1));
  assert.deepEqual(detail.map((e) => [e.offset, e.sessions, e.tokens]), [["-04:00", 1, 10], ["-05:00", 2, null]]);
  assert.deepEqual(detail.map((e) => e.iso), ["2026-11-01T05:30:00.000Z", "2026-11-01T06:30:00.000Z"]);
  // spring forward (2026-03-08): 06:59Z = 01:59 EST, 07:00Z = 03:00 EDT; local hour 02 never holds a session
  const gap = plain(H.localHeat([{ t: utc(2026, 3, 8, 6, 59), n: 1, tokens: 1 }, { t: utc(2026, 3, 8, 7, 0), n: 1, tokens: 1 }]));
  const gapHot = gap.map((c, i) => [i, c.sessions]).filter(([, s]) => s);
  assert.deepEqual(gapHot, [[6 * 24 + 1, 1], [6 * 24 + 3, 1]]);
  // every start is counted exactly once, wherever it lands
  const many = Array.from({ length: 200 }, (_, i) => ({ t: utc(2026, 1, 1, 0) + i * 5400, n: 1 + (i % 3), tokens: i % 5 === 0 ? null : i }));
  assert.equal(plain(H.localHeat(many)).reduce((n, c) => n + c.sessions, 0), many.reduce((n, s) => n + s.n, 0));
});

test("heatmap C3: fractional-offset zone (Asia/Kolkata) and a zone east of UTC move the cell, not the count", () => {
  const prev = process.env.TZ;
  process.env.TZ = "Asia/Kolkata";
  try {
    const H = load(helperBlock("dist/index.js"));
    const t = utc(2026, 11, 5, 18, 30);  // Thu 18:30 UTC = Fri 00:00 IST
    assert.deepEqual(plain(H.localCellOf(t)), { row: 4, col: 0 });
    assert.equal(H.localOffset(t), "+05:30");
    const cells = plain(H.localHeat([{ t, n: 1, tokens: 7 }]));
    assert.deepEqual(cells[4 * 24 + 0], { sessions: 1, tokens: 7 });
  } finally {
    process.env.TZ = prev;
  }
});

test("heat level: null tokens are 'not recorded', never an empty-looking zero; sessions metric is unaffected", () => {
  const H = load(helperBlock("dist/index.js"));
  const cells = [{ sessions: 3, tokens: null }, { sessions: 0, tokens: null }, { sessions: 2, tokens: 0 }, { sessions: 4, tokens: 400 }];
  assert.deepEqual(plain(H.heatLevel(cells[0], "tokens", 400)), { level: 0, na: true });
  assert.deepEqual(plain(H.heatLevel(cells[1], "tokens", 400)), { level: 0, na: false });  // no session: empty, not "n/a"
  assert.deepEqual(plain(H.heatLevel(cells[2], "tokens", 400)), { level: 0, na: false });  // a real zero
  assert.deepEqual(plain(H.heatLevel(cells[3], "tokens", 400)), { level: 4, na: false });
  assert.equal(plain(H.heatLevel(cells[0], "sessions", 4)).level, 3);
  assert.equal(H.heatMax(cells, "tokens"), 400);
  assert.equal(H.heatMax(cells, "sessions"), 4);
});

test("cacheRowView: values are the backend's, missing is a dash with a reason, no money is recomputed", () => {
  const H = load(helperBlock("dist/index.js"));
  const full = plain(H.cacheRowView({ model: "gpt-6-luna", provider_id: "openai", hit_rate_pct: 28.5714, cached_input_tokens: 200000,
    uncached_input_tokens: 500000, cache_write_tokens: 0, reasoning_tokens: 0, reasoning_share_pct: 0, effective_cost_per_mtok: 6.778169014084507,
    output_tokens: 700000, discount_multiplier: 0.1, rate_source: "openai-gpt-6-tiers-2026-09", reason: null }));
  assert.equal(full.hit, "28.57%");
  assert.equal(full.cached, "200,000");
  assert.equal(full.effective, "$6.7782");
  assert.match(full.effectiveTitle, /6\.778169014084507/);
  assert.equal(full.writes, "0");  // an exact zero is shown as 0
  assert.equal(full.share, "0.00%");
  const gone = plain(H.cacheRowView({ model: null, provider_id: null, hit_rate_pct: null, cached_input_tokens: null, uncached_input_tokens: null,
    cache_write_tokens: null, reasoning_tokens: null, reasoning_share_pct: null, effective_cost_per_mtok: null, output_tokens: null,
    discount_multiplier: null, rate_source: null, reason: "unknown_discount" }));
  assert.equal(gone.model, "Unknown model");
  for (const k of ["hit", "cached", "uncached", "writes", "reasoning", "share", "effective", "output", "multiplier", "rateSource"]) assert.equal(gone[k], "—", k);
  assert.match(gone.reasonText, /No published cache discount/);
  assert.equal(plain(H.cacheRowView({ reason: "<b>x</b>" })).reasonText, null);  // an unknown code never becomes text
});

test("depthStatusLine and repo helpers", () => {
  const H = load(helperBlock("dist/index.js"));
  assert.equal(H.depthStatusLine(null), null);
  const clean = { partial: false, excluded: 0, missing: { input_tokens: 0, duration: 0 } };
  assert.equal(H.depthStatusLine(clean), null);
  const line = H.depthStatusLine({ partial: true, excluded: 2, missing: { input_tokens: 1, duration: 3, estimated_cost_usd: 0 } });
  assert.match(line, /partial data; 2 sessions without a valid start time left out; 1 without input tokens; 3 without a completed duration/);
  const roll = [
    { repo_basename: "Unscoped", full_path: null, full_paths: [], sessions: 4 },
    { repo_basename: "Unscoped", full_path: "/srv/Unscoped", full_paths: ["/srv/Unscoped"], sessions: 1 },
    { repo_basename: "deck", full_path: null, full_paths: ["/a/deck", "/b/deck"], sessions: 5 },
  ];
  assert.equal(plain(H.repoRowFor({ id: "repo:null", label: "Unscoped", totals: { sessions: 4 } }, roll)).sessions, 4);
  assert.equal(plain(H.repoRowFor({ id: 'repo:"Unscoped"', label: "Unscoped", totals: { sessions: 1 } }, roll)).full_path, "/srv/Unscoped");
  assert.deepEqual(plain(H.repoPathSummary(roll[2])), { count: 2, text: "2 paths", paths: ["/a/deck", "/b/deck"] });
  assert.deepEqual(plain(H.repoPathSummary(roll[0])), { count: 0, text: null, paths: [] });
  assert.equal(H.repoPathSummary(roll[1]).text, "/srv/Unscoped");
});

// ------------------------------------------------------------------ rendering (dashboard half)

const DAYPOINT = (day, inp, out, sessions) => F.point({ day, input_count: inp, output_count: out, estimated_usd: null, sessions, api_calls: sessions });

function depthPayload(dimension, over) {
  const points = [DAYPOINT("2026-11-05", 600, 400, 3)];
  const base = F.usage(30, points, { dimension });
  const groupTotals = (s, i, o) => F.metrics({ input_count: i, output_count: o, sessions: s, api_calls: s });
  const groups = dimension === "repo" ? [
    { id: 'repo:"deck"', label: "deck", series_id: "g1", model_id: null, provider_ids: [], profiles: [], row_ids: [], totals: groupTotals(2, 500, 300), is_other: false,
      durations: { p50: 60, p90: 100, p99: 120, mean: 70, max: 120 }, duration_samples: 2 },
    { id: 'repo:"solo"', label: "solo", series_id: "g2", model_id: null, provider_ids: [], profiles: [], row_ids: [], totals: groupTotals(1, 100, 100), is_other: false,
      durations: { p50: null, p90: null, p99: null, mean: null, max: null }, duration_samples: 0 },
  ] : base.groups;
  const series = dimension === "repo"
    ? [F.series([DAYPOINT("2026-11-05", 500, 300, 2)], { id: "g1", scope: "machine" }), F.series([DAYPOINT("2026-11-05", 100, 100, 1)], { id: "g2", scope: "machine" })]
    : base.series;
  const grid = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => ({ sessions: 0, total_tokens: null })));
  const depth = {
    durations: { p50: 90, p90: 114, p99: 119, mean: 90, max: 120 }, duration_samples: 2,
    heatmap: { grid, starts: [{ started_at: utc(2026, 11, 6, 4, 59), sessions: 2, total_tokens: 800 }, { started_at: utc(2026, 11, 5, 15, 0), sessions: 1, total_tokens: null }] },
    cache_metrics: [
      { model: "gpt-6-luna", provider_id: "openai", cached_input_tokens: 200000, uncached_input_tokens: 500000, cache_write_tokens: 0,
        reasoning_tokens: 0, output_tokens: 700000, hit_rate_pct: 28.5714, reasoning_share_pct: 0, effective_cost_per_mtok: 6.778169014084507,
        discount_multiplier: 0.1, rate_source: "openai-gpt-6-tiers-2026-09", reason: null },
      { model: "mystery", provider_id: null, cached_input_tokens: null, uncached_input_tokens: null, cache_write_tokens: null,
        reasoning_tokens: null, output_tokens: null, hit_rate_pct: null, reasoning_share_pct: null, effective_cost_per_mtok: null,
        discount_multiplier: null, rate_source: null, reason: "missing_usage" },
    ],
    repo_rollup: [
      { repo_basename: "deck", full_path: null, full_paths: ["/work/deck", "/other/deck"], sessions: 2, total_tokens: 800, total_cost: null },
      { repo_basename: "solo", full_path: "/work/solo", full_paths: ["/work/solo"], sessions: 1, total_tokens: 200, total_cost: null },
    ],
    model_rollup: [], depth_totals: { sessions: 3, total_tokens: 1000, total_cost: null },
    depth_status: { partial: false, excluded_started_at: 0, missing: { input_tokens: 0, output_tokens: 0, estimated_cost_usd: 3, cache_read_tokens: 0, cache_write_tokens: 0, reasoning_tokens: 0, duration: 1 } },
  };
  return Object.assign({}, base, { groups, series, rows: [] }, depth, over || {});
}

function routes(make) {
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) return envelope({ cards: [], inventory_state: "empty", view: "machine" });
    if (url.startsWith(B + "/health")) return envelope({ entries: [] });
    if (url.startsWith(B + "/usage")) return envelope(make(new URL(url, "http://x").searchParams.get("dimension")));
    throw new Error("404: unexpected url " + url);
  };
}

test("By repo is offered in machine mode; the cards, duration line and repo paths render from the payload", async () => {
  const m = await mount({ routes: routes((dim) => depthPayload(dim)) });
  await m.flush(0);
  assert.deepEqual(m.$$("[data-dimension-toggle] [data-dimension]").map((e) => e.dataset.dimension), ["all", "profile", "model", "repo"]);
  assert.equal(txt(m.$("[data-dimension=repo]")), "By repo");
  await m.click(m.$("[data-dimension=repo]"));
  await m.flush(0);
  assert.ok(m.calls.some((u) => u.includes("dimension=repo")));
  assert.ok(m.$("[data-dimension-view=repo]"));
  const table = m.$("[data-summary-table=repo]");
  assert.equal(txt(table.querySelector("th")), "Repository");
  const rows = m.$$("[data-summary-table=repo] tbody tr");
  assert.deepEqual(rows.map((r) => r.dataset.groupId), ['repo:"deck"', 'repo:"solo"']);
  assert.equal(txt(rows[0].querySelector("td")), "deck2 paths");
  assert.equal(txt(rows[1].querySelector("td")), "solo/work/solo");
  const paths = m.$("[data-repo-paths]");
  assert.match(txt(paths), /deck — 2 paths/);
  assert.ok(paths.textContent.includes("/other/deck") && paths.textContent.includes("/work/deck"));
  // durations: the overall line sits in the totals area, per group in a table, nulls are dashes
  const line = m.$("[data-usage-totals] [data-duration-summary=true]");
  assert.match(txt(line), /^Completed session duration: p50 1m 30s · p90 1m 54s · p99 1m 59s · mean 1m 30s · max 2m 0s · samples 2$/);
  const gd = m.$$("[data-group-durations] tbody tr");
  assert.equal(gd.length, 2);
  assert.deepEqual(gd[1].querySelectorAll("td").length, 7);
  assert.equal(gd[1].querySelector("[data-duration=p50]").textContent, "—");
  assert.equal(gd[1].querySelector("[data-duration=samples]").textContent, "0");
  // cache table
  const cache = m.$("[data-depth-card=cache]");
  const luna = cache.querySelector('[data-cache-row="gpt-6-luna|openai"]');
  assert.equal(luna.querySelector("[data-field=hit_rate_pct]").textContent, "28.57%");
  assert.equal(luna.querySelector("[data-field=effective_cost_per_mtok]").textContent, "$6.7782");
  assert.equal(luna.querySelector("[data-field=cache_write_tokens]").textContent, "0");
  const gone = cache.querySelector('[data-cache-row="mystery|"]');
  assert.equal(gone.dataset.cacheReason, "missing_usage");
  assert.equal(gone.querySelector("[data-field=hit_rate_pct]").textContent, "—");
  assert.equal(gone.querySelector("[data-field=effective_cost_per_mtok]").textContent, "—");
  assert.match(txt(cache), /Recorded-cost basis/);
  assert.match(txt(cache), /Not a tariff|not a tariff/);
  await m.unmount();
});

test("cache table: one row per model; resolved providers are listed in that row, Unknown only when nothing resolved", async () => {
  const row = (model, provider_id) => ({ model, provider_id, cached_input_tokens: 10, uncached_input_tokens: 90, cache_write_tokens: 0,
    reasoning_tokens: 0, output_tokens: 5, hit_rate_pct: 10, reasoning_share_pct: 0, effective_cost_per_mtok: 1.5,
    discount_multiplier: 0.1, rate_source: null, reason: null });
  const cache = [row("alpha", "anthropic,openai"), row("beta", "openai"), row("gamma", "unknown"), row("delta", null)];
  const m = await mount({ routes: routes((dim) => depthPayload(dim, { cache_metrics: cache })) });
  await m.flush(0);
  const card = m.$("[data-depth-card=cache]");
  const rows = Array.from(card.querySelectorAll("[data-cache-row]"));
  assert.deepEqual(rows.map((r) => r.dataset.cacheRow.split("|")[0]), ["alpha", "beta", "gamma", "delta"]);  // no model repeated
  const cell = (r) => txt(r.querySelector("[data-field=provider_id]"));
  assert.equal(cell(rows[0]), "anthropic, openai");
  assert.equal(cell(rows[1]), "openai");
  assert.equal(cell(rows[3]), "Unknown");
  assert.ok(!/unknown/i.test(cell(rows[0])), "never alongside a resolved label");
  await m.unmount();
});

test("providerList splits the backend's comma list and ignores blanks", () => {
  const H = load(helperBlock("dist/index.js"));
  assert.deepEqual(plain(H.providerList("anthropic,custom:pgx,openai")), ["anthropic", "custom:pgx", "openai"]);
  assert.deepEqual(plain(H.providerList(null)), []);
  assert.deepEqual(plain(H.providerList("openai,")), ["openai"]);
});

test("heatmap card: browser-local grid from exact epochs, tokens toggle, null tokens are '?', detail by offset", async () => {
  const m = await mount({ routes: routes((dim) => depthPayload(dim)) });
  await m.flush(0);
  const card = m.$("[data-depth-card=heatmap]");
  assert.match(txt(card.querySelector("[data-heat-caption]")), /Time zone: America\/New_York\./);
  assert.match(txt(card.querySelector("[data-heat-caption]")), /counted once, in the hour it started/);
  const cell = (r, c) => card.querySelector('[data-heat-cell="' + r + "-" + c + '"]');
  // 2026-11-06T04:59Z = Thu 23:59 EST; 2026-11-05T15:00Z = Thu 10:00 EST
  assert.equal(cell(3, 23).dataset.sessions, "2");
  assert.equal(cell(3, 23).dataset.tokens, "800");
  assert.equal(cell(3, 10).dataset.sessions, "1");
  assert.equal(cell(3, 10).dataset.tokens, "null");
  assert.equal(card.querySelectorAll("[data-heat-cell]").length, 7 * 24);
  assert.equal(card.querySelector('[data-heat-cell][data-sessions="2"]'), cell(3, 23));  // nothing leaked into the UTC bucket (Fri 04)
  assert.equal(cell(4, 4).dataset.sessions, "0");
  assert.match(cell(3, 23).getAttribute("aria-label"), /^Thu 23:00, 2 sessions, 800 tokens$/);
  assert.match(cell(3, 10).getAttribute("aria-label"), /^Thu 10:00, 1 session, tokens not recorded$/);
  await m.click(card.querySelector("[data-heat-toggle=tokens]"));
  assert.equal(card.querySelector("[data-heat-grid]").dataset.heatMetric, "tokens");
  assert.equal(cell(3, 10).dataset.heatNa, "true");  // not recorded, distinct from an empty cell
  assert.equal(cell(3, 10).textContent, "?");
  assert.equal(cell(0, 0).dataset.heatNa, undefined);
  await m.click(cell(3, 23));
  const detail = card.querySelector("[data-heat-detail]");
  assert.ok(detail);
  assert.match(txt(detail), /2026-11-06T04:59:00\.000Z.*-05:00.*2.*800/);
  await m.click(cell(3, 23));
  assert.equal(card.querySelector("[data-heat-detail]"), null);
  await m.unmount();
});

test("an older server (no depth members) renders no depth cards and no duration line; By repo still requests", async () => {
  const m = await mount({ routes: routes(() => F.usage(30, [DAYPOINT("2026-11-05", 1, 1, 1)])) });
  await m.flush(0);
  assert.equal(m.$("[data-depth-card]"), null);
  assert.equal(m.$("[data-duration-summary]"), null);
  assert.ok(m.$("[data-usage-totals]"));
  await m.unmount();
});

test("an unreadable projection shows 'unavailable' states, never zeros", async () => {
  const dead = { durations: null, duration_samples: null, heatmap: null, cache_metrics: null, repo_rollup: null, model_rollup: null,
    depth_totals: null, depth_status: { partial: true, excluded_started_at: null, missing: { input_tokens: null, output_tokens: null, estimated_cost_usd: null, cache_read_tokens: null, cache_write_tokens: null, reasoning_tokens: null, duration: null } } };
  const m = await mount({ routes: routes((dim) => depthPayload(dim, dead)) });
  await m.flush(0);
  assert.match(txt(m.$("[data-duration-summary]")), /unavailable/);
  assert.match(txt(m.$("[data-depth-card=heatmap]")), /Activity heatmap unavailable/);
  assert.match(txt(m.$("[data-depth-card=cache]")), /Cache economics unavailable/);
  assert.equal(m.$("[data-heat-cell]"), null);
  assert.equal(m.$("[data-cache-table]"), null);
  await m.unmount();
});

test("hostile repo names and paths render as text only", async () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const over = depthPayload("repo");
  over.groups[0].label = hostile;
  over.repo_rollup[0].repo_basename = hostile;
  over.repo_rollup[0].full_paths = ["/w/" + hostile];
  over.repo_rollup[0].full_path = "/w/" + hostile;
  const m = await mount({ routes: routes((dim) => (dim === "repo" ? over : depthPayload(dim))) });
  await m.click(m.$("[data-dimension=repo]"));
  await m.flush(0);
  assert.equal(m.$$("img").length, 0);
  assert.ok(m.text().includes(hostile));
  assert.ok(m.$("[data-repo-paths]").textContent.includes("/w/" + hostile));
  await m.unmount();
});

test("read-only: viewing the depth cards and toggling issues only GETs", async () => {
  const m = await mount({ routes: routes((dim) => depthPayload(dim)) });
  await m.flush(0);
  await m.click(m.$("[data-depth-card=heatmap] [data-heat-toggle=tokens]"));
  await m.click(m.$("[data-dimension=repo]"));
  await m.flush(0);
  assert.ok(m.calls.every((u) => typeof u === "string" && u.startsWith(B + "/")));
  await m.unmount();
});

// ------------------------------------------------------------------ attribution coverage (t_ff59364f, C4)

test("repoCoverage: attributed share of known tokens, Unscoped is the empty-path row only, floored to one decimal", () => {
  const H = load(helperBlock("dist/index.js"));
  const row = (name, paths, tokens) => ({ repo_basename: name, full_path: paths.length === 1 ? paths[0] : null, full_paths: paths,
    sessions: 1, total_tokens: tokens, total_cost: null });
  const roll = [row("deck", ["/w/deck"], 7500), row("Unscoped", ["/srv/Unscoped"], 500), row("Unscoped", [], 2000)];
  const c = plain(H.repoCoverage(roll));
  assert.deepEqual([c.attributed, c.unscoped, c.total, c.pct], [8000, 2000, 10000, 80]);  // the literal repo counts as attributed
  assert.equal(c.text, "80.0% of tokens attributed to a repository (8,000 of 10,000 known tokens; 2,000 Unscoped)");
  assert.equal(H.repoCoverage([row("deck", ["/w/deck"], 3), row("Unscoped", [], 997)]).pct, 0.3);
  assert.equal(H.repoCoverage([row("deck", ["/w/deck"], 2999), row("Unscoped", [], 1)]).pct, 99.9);  // floored: 100.0 only when complete
  assert.equal(H.repoCoverage([row("deck", ["/w/deck"], 5)]).pct, 100);
  assert.equal(H.repoCoverage([row("Unscoped", [], 5)]).pct, 0);
  // unknown token counts are skipped, never zero-filled; nothing known -> no caption
  assert.equal(H.repoCoverage([row("deck", ["/w/deck"], null), row("Unscoped", [], null)]), null);
  assert.equal(H.repoCoverage([row("Unscoped", [], 0)]), null);
  assert.equal(H.repoCoverage(null), null);
  assert.equal(H.repoCoverage([null, "x", row("deck", ["/w/deck"], null), row("Unscoped", [], 10)]).pct, 0);
});

test("By repo shows the Unscoped row as its own row and a '% of tokens attributed' caption; other dimensions show none", async () => {
  const roll = [
    { repo_basename: "deck", full_path: "/w/deck", full_paths: ["/w/deck"], sessions: 3, total_tokens: 750, total_cost: null },
    { repo_basename: "Unscoped", full_path: null, full_paths: [], sessions: 1, total_tokens: 250, total_cost: null },
  ];
  const m = await mount({ routes: routes((dim) => depthPayload(dim, dim === "repo" ? { repo_rollup: roll } : {})) });
  await m.flush(0);
  assert.ok(m.$("[data-repo-coverage]") === null);  // dimension all
  await m.click(m.$("[data-dimension=repo]"));
  await m.flush(0);
  const cap = m.$("[data-repo-coverage]");
  assert.ok(cap !== null);
  assert.equal(cap.dataset.repoCoverage, "75");
  assert.equal(txt(cap), "75.0% of tokens attributed to a repository (750 of 1,000 known tokens; 250 Unscoped)");
  await m.unmount();
});
