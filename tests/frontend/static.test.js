"use strict";
// Static guarantees on the shipped bundle: no hardcoded colours, no forbidden APIs,
// manifest matches DESIGN.md §7.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const DIR = path.resolve(__dirname, "../../plugins/usage_deck");
const read = (p) => fs.readFileSync(path.join(DIR, p), "utf8");

test("style.css has no hardcoded colours (tokens / currentColor only)", () => {
  const css = read("dist/style.css").replace(/\/\*[\s\S]*?\*\//g, "");
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css), "hex colour");
  assert.ok(!/\b(rgb|rgba|hsl|hsla|hwb|lab|lch|oklch|oklab)\(/i.test(css), "functional colour");
  const named = css.match(/(?:color|background|background-color|border(?:-[a-z]+)?|fill|stroke|outline)\s*:\s*([^;{}]+);/gi) || [];
  const NAMED = /\b(red|green|blue|black|white|gray|grey|orange|yellow|purple|pink|teal|cyan|navy|maroon|lime|aqua|silver|olive|brown)\b/i;
  named.forEach((d) => assert.ok(!NAMED.test(d.replace(/var\([^)]*\)/g, "")), "named colour in: " + d));
  assert.ok(/var\(--color-[a-z-]+/.test(css), "uses host design tokens");
  // every rule is scoped
  css.replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "").replace(/@media[^{]*\{([\s\S]*?\})\s*\}/g, "$1")
    .split("}").map((s) => s.split("{")[0].trim()).filter(Boolean)
    .forEach((sel) => assert.ok(sel.split(",").every((x) => x.trim().startsWith(".usage-deck")), "unscoped selector: " + sel));
});

test("index.js: namespaced API only, no innerHTML / storage / network escapes / dependencies", () => {
  const js = read("dist/index.js");
  const code = js.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/\s\/\/ .*$/gm, "");
  assert.ok(js.includes('"/api/plugins/usage-deck"'));
  assert.ok(!/\/api\/usage-deck/.test(code), "old namespace absent");
  // The namespace is a single constant; comments mention it for humans only.
  assert.equal((code.match(/\/api\/plugins\/usage-deck/g) || []).length, 1, "API_BASE defined once");
  for (const bad of ["innerHTML", "dangerouslySetInnerHTML", "localStorage", "sessionStorage", "document.cookie",
    "XMLHttpRequest", "eval(", "new Function", "importScripts", "https://", "http://", "type: \"password\"", "type: \"text\""]) {
    assert.ok(!code.includes(bad), "forbidden token in bundle: " + bad);
  }
  assert.ok(!/\bfetch\(/.test(code.replace(/SDK\.fetchJSON/g, "")), "only SDK.fetchJSON");
  assert.ok(!/\brequire\(|\bimport\s/.test(code), "no module loading");
});

test("manifest.json matches DESIGN.md §7 canonical fields", () => {
  const m = JSON.parse(read("manifest.json"));
  assert.deepEqual(m, {
    name: "usage-deck", label: "UsageDeck", description: m.description, icon: "Activity", version: "0.1.0",
    tab: { path: "/usage-deck", position: "after:models" },
    entry: "dist/index.js", css: "dist/style.css", api: "plugin_api.py",
  });
  assert.ok(m.description && !/[<>]/.test(m.description));
  assert.ok(fs.existsSync(path.join(DIR, m.entry)) && fs.existsSync(path.join(DIR, m.css)));
});

test("plugin.yaml declares identity and no dependencies", () => {
  const y = read("plugin.yaml");
  assert.match(y, /^name: usage-deck$/m);
  assert.match(y, /^version: 0\.1\.0$/m);
  assert.match(y, /^description:/m);
  assert.match(y, /^author:/m);
  assert.ok(!/requires|dependencies|pip_/i.test(y));
});
