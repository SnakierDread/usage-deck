"use strict";
// Card t_793f0ed6 (C3): the machine-view health row of an endpoint defined only in ANOTHER profile's config says so,
// in BOTH UI variants (dashboard dist/index.js and desktop/plugin.js). The rows rendered here are the exact rows the
// real service emitted (tests/frontend/fixtures/machine_endpoint_owner_health.json, kept equal to the live service
// output by plugins/usage_deck/tests/test_machine_endpoint_owner.py), not hand-written payloads.
// Assertions compare strings/booleans only (never a jsdom node).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { mod, mount, envelope } = require("./harness");
const F = require("./fixtures");
const D = require("./desktop_harness");

const ROWS = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "machine_endpoint_owner_health.json"), "utf8"));
const B = mod.API_BASE;
const norm = (el) => el.textContent.replace(/\s+/g, " ").trim();

function routes(rows) {
  return (url) => {
    if (url.startsWith(B + "/subscriptions")) {
      return envelope({ cards: [], inventory_state: "detected", view: new URL(url, "http://x").searchParams.get("view") });
    }
    if (url.startsWith(B + "/usage")) {
      return envelope(F.usage(Number(new URL(url, "http://x").searchParams.get("window")), [F.point("2026-11-01")]));
    }
    if (url.startsWith(B + "/health")) return envelope({ entries: rows });
    throw new Error("404: unexpected url " + url);
  };
}

const desktop = D.load(["HealthRow"]);
const dashboardRow = async (row, view) => {
  const m = await mount({ routes: routes([row]) });
  if (view === "profile") { await m.click(m.$("[data-view=profile]")); await m.flush(5); }
  return m;
};
const owned = (m) => m.$("[data-endpoint-id] [data-profile-owned]");

test("the fixture holds the six real service scenarios", () => {
  assert.deepEqual(Object.keys(ROWS).sort(), ["missing_key", "models_only", "offline", "pool_only", "secondary_only", "selected_mirror"]);
  assert.deepEqual(Object.entries(ROWS).map(([k, r]) => [k, r.profile_owned]).sort(),
    [["missing_key", true], ["models_only", true], ["offline", true], ["pool_only", true], ["secondary_only", true], ["selected_mirror", false]]);
  // the service's own states behind the two provenance-sensitive rows
  assert.deepEqual([ROWS.missing_key.status, ROWS.missing_key.reason], ["unknown", "credentials_missing"]);
  assert.deepEqual([ROWS.pool_only.status, ROWS.pool_only.confidence], ["healthy", "self_hosted"]);
});

// The note is ownership/policy copy: it must not claim a completed probe or a config-file origin, because the same
// flag is set for a zero-request missing-key row and for a credential-pool-only endpoint with no config at all.
const assertOwnershipOnly = (title) => {
  assert.match(title, /belongs to another profile's inventory \(its config or its credential pool\)/);
  assert.match(title, /read-only/);
  assert.match(title, /when that key is unset, no request is sent/);
  assert.match(title, /No other profile's or the process's credential is sent/);
  assert.ok(!/was probed|defined only in/.test(title), "no execution or config-origin claim: " + title);
};

// ------------------------------------------------------------------ dashboard variant

test("dashboard: a profile-owned endpoint shows the ownership badge and a truthful note, never 'Not probed'", async () => {
  const m = await dashboardRow(ROWS.secondary_only);
  const badge = owned(m);
  assert.ok(badge !== null);
  assert.equal(badge.textContent, "Profile endpoint");
  assertOwnershipOnly(badge.getAttribute("title"));
  assert.ok(m.$("[data-machine-only-note]") === null);
  assert.ok(!/Not probed|No live source/.test(m.text()));
  assert.match(norm(m.$("[data-endpoint-id]")), /Healthy/);
  await m.unmount();
});

test("dashboard: a zero-request missing-key row and a pool-only row are owned, with copy that stays true for both", async () => {
  const missing = await dashboardRow(ROWS.missing_key);
  const badge = owned(missing);
  assert.ok(badge !== null);
  assertOwnershipOnly(badge.getAttribute("title"));
  assert.ok(missing.$("[data-machine-only-note]") === null, "owned rows are never 'Not probed'");
  assert.ok(!/was probed/.test(missing.text()));
  await missing.unmount();

  const pool = await dashboardRow(ROWS.pool_only);
  assert.ok(owned(pool) !== null);
  assertOwnershipOnly(owned(pool).getAttribute("title"));
  assert.match(norm(pool.$("[data-endpoint-id]")), /Healthy/);
  await pool.unmount();
});

test("dashboard: models up, metrics absent is a degraded probe result with the ownership badge", async () => {
  const m = await dashboardRow(ROWS.models_only);
  assert.ok(owned(m) !== null);
  assert.ok(m.$("[data-machine-only-note]") === null);
  assert.match(norm(m.$("[data-endpoint-id]")), /Degraded/);
  assert.match(norm(m.$("[data-endpoint-id]")), /Metrics unavailable/);
  await m.unmount();
});

test("dashboard: an offline profile-owned box stays quiet (no error, last-seen line) and still says who owns it", async () => {
  const m = await dashboardRow(ROWS.offline);
  assert.ok(owned(m) !== null);
  assert.ok(m.$("[data-endpoint-id] [data-field=last-seen]") !== null);
  assert.ok(m.$("[data-endpoint-id] [role=note]") === null, "offline is not an error card");
  assert.ok(m.$("[data-machine-only-note]") === null);
  assert.match(norm(m.$("[data-endpoint-id]")), /Offline/);
  await m.unmount();
});

test("dashboard: an endpoint the selected profile also defines (root mirror) carries no ownership badge", async () => {
  const m = await dashboardRow(ROWS.selected_mirror);
  assert.ok(owned(m) === null);
  assert.match(norm(m.$("[data-endpoint-id]")), /Healthy/);
  await m.unmount();
});

test("dashboard: the profile view never shows the badge, even for a flagged row", async () => {
  const m = await dashboardRow(ROWS.secondary_only, "profile");
  assert.ok(m.$("[data-endpoint-id]") !== null);
  assert.ok(owned(m) === null);
  await m.unmount();
});

test("dashboard: 'Not probed' still marks a never-sent machine row that is not profile-owned", async () => {
  const row = Object.assign({}, ROWS.selected_mirror, { status: "unknown", confidence: "opaque", metrics_available: false,
    latency_ms: null, cpu_percent: null, memory_bytes: null, reason: "source_unavailable", profile_owned: false });
  const m = await dashboardRow(row);
  assert.match(m.$("[data-endpoint-id] [data-machine-only-note]").textContent, /^Not probed$/);
  assert.ok(owned(m) === null);
  await m.unmount();
});

// ------------------------------------------------------------------ desktop variant

const desktopRow = async (row, machine) => D.render(desktop.HealthRow, { entry: row, machine, zoneStale: false });

test("desktop: a profile-owned endpoint shows the ownership badge and the same truthful note, never 'Not probed'", async () => {
  const r = await desktopRow(ROWS.secondary_only, true);
  const badge = r.container.querySelector("[data-profile-owned]");
  assert.ok(badge !== null);
  assert.equal(badge.textContent, "Profile endpoint");
  assertOwnershipOnly(badge.getAttribute("title"));
  assert.ok(r.container.querySelector("[data-machine-only-note]") === null);
  assert.ok(!/Not probed/.test(r.container.textContent));
  await r.unmount();
});

test("desktop: a zero-request missing-key row and a pool-only row are owned, with copy that stays true for both", async () => {
  const missing = await desktopRow(ROWS.missing_key, true);
  const badge = missing.container.querySelector("[data-profile-owned]");
  assert.ok(badge !== null);
  assertOwnershipOnly(badge.getAttribute("title"));
  assert.ok(missing.container.querySelector("[data-machine-only-note]") === null);
  assert.ok(!/was probed/.test(missing.container.textContent));
  await missing.unmount();

  const pool = await desktopRow(ROWS.pool_only, true);
  const poolBadge = pool.container.querySelector("[data-profile-owned]");
  assert.ok(poolBadge !== null);
  assertOwnershipOnly(poolBadge.getAttribute("title"));
  await pool.unmount();
});

test("desktop: models-only, offline and mirrored rows", async () => {
  const modelsOnly = await desktopRow(ROWS.models_only, true);
  assert.ok(modelsOnly.container.querySelector("[data-profile-owned]") !== null);
  assert.ok(modelsOnly.container.querySelector("[data-machine-only-note]") === null);
  await modelsOnly.unmount();

  const offline = await desktopRow(ROWS.offline, true);
  assert.ok(offline.container.querySelector("[data-profile-owned]") !== null);
  assert.ok(offline.container.querySelector("[data-field=last-seen]") !== null);
  assert.ok(offline.container.querySelector("[role=note]") === null, "offline stays quiet");
  await offline.unmount();

  const mirror = await desktopRow(ROWS.selected_mirror, true);
  assert.ok(mirror.container.querySelector("[data-profile-owned]") === null);
  await mirror.unmount();
});

test("desktop: no badge outside the machine view; 'Not probed' still marks a never-sent non-owned row", async () => {
  const profile = await desktopRow(ROWS.secondary_only, false);
  assert.ok(profile.container.querySelector("[data-profile-owned]") === null);
  await profile.unmount();

  const row = Object.assign({}, ROWS.selected_mirror, { status: "unknown", confidence: "opaque", metrics_available: false,
    latency_ms: null, cpu_percent: null, memory_bytes: null, reason: "source_unavailable", profile_owned: false });
  const r = await desktopRow(row, true);
  assert.match(r.container.querySelector("[data-machine-only-note]").textContent, /^Not probed$/);
  await r.unmount();
});

test("both variants carry byte-identical ownership copy", () => {
  const grab = (file, name) => {
    const src = fs.readFileSync(path.resolve(__dirname, "../../plugins/usage_deck", file), "utf8");
    const m = src.match(new RegExp("PROFILE_OWNED_" + name + "\\s*=\\s*\\n?\\s*([\"'])(.*)\\1"));
    assert.ok(m, file + " " + name);
    return m[2];
  };
  for (const name of ["BADGE", "NOTE"]) assert.equal(grab("dist/index.js", name), grab("desktop/plugin.js", name));
});
