// Frontend test harness: real React + react-dom + jsdom, fake host SDK.
// React/jsdom are resolved from the Hermes checkout's node_modules (HERMES_NODE_MODULES);
// nothing here is a dependency of the plugin itself.
"use strict";
const path = require("node:path");
const { createRequire } = require("node:module");

const NM = process.env.HERMES_NODE_MODULES || "/root/.hermes/hermes-agent/node_modules";
const nmRequire = createRequire(path.join(NM, "noop.js"));
const { JSDOM } = nmRequire("jsdom");
const React = nmRequire("react");
const { createRoot } = nmRequire("react-dom/client");

const dom = new JSDOM("<!doctype html><html><body><div id=\"root\"></div></body></html>", { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const act = React.act;

const PLUGIN = path.resolve(__dirname, "../../plugins/usage_deck/dist/index.js");
const mod = require(PLUGIN);

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function envelope(data, extra) {
  return Object.assign({
    ok: true, data, error: null,
    meta: { schema_version: 1, request_id: "r", generated_at: "2026-11-06T00:00:00Z", partial: false, errors: [] },
  }, extra || {});
}

function errEnvelope(code, message, retryable) {
  return { ok: false, data: null, error: { code, message, retryable: !!retryable, provider_id: null, account_id: null },
    meta: { schema_version: 1, request_id: "r", generated_at: "2026-11-06T00:00:00Z", partial: false, errors: [] } };
}

/**
 * mount({ routes, pollMs, profile }) — routes: (url) => value | Promise | throws.
 * Returns { container, calls, unmount, flush, $, $$, click }.
 */
async function mount(opts) {
  const calls = [];
  const SDK = {
    React,
    hooks: { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback,
      useMemo: React.useMemo, useRef: React.useRef },
    fetchJSON: (url, init) => {
      calls.push(url);
      try { return Promise.resolve(opts.routes(url, init)); } catch (e) { return Promise.reject(e); }
    },
    api: opts.profile ? { getManagementProfile: () => opts.profile.current } : {},
  };
  const { UsageDeckPage } = mod.createUsageDeck(SDK, { pollMs: opts.pollMs });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(React.createElement(UsageDeckPage)); });
  const api = {
    container, calls, root,
    $: (sel) => container.querySelector(sel),
    $$: (sel) => Array.from(container.querySelectorAll(sel)),
    text: () => container.textContent,
    flush: async (ms) => { await act(async () => { await new Promise((r) => setTimeout(r, ms || 0)); }); },
    click: async (el) => { await act(async () => { el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })); }); },
    rerender: async () => { await act(async () => { root.render(React.createElement(UsageDeckPage)); }); },
    unmount: async () => { await act(async () => { root.unmount(); }); container.remove(); },
  };
  return api;
}

module.exports = { mod, mount, deferred, envelope, errEnvelope, act, dom, React };
