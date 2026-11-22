// Loads the desktop plugin source (an ES module importing the host SDK) so one of its internal components can be
// rendered with real React + jsdom. Nothing here is a dependency of the plugin: the three imports are stripped
// and replaced by stand-ins, and the default export is renamed so the module body can be evaluated as a function.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const { React, act, dom } = require("./harness");

const NM = process.env.HERMES_NODE_MODULES || "/root/.hermes/hermes-agent/node_modules";
const nmRequire = createRequire(path.join(NM, "noop.js"));
const { jsx, jsxs } = nmRequire("react/jsx-runtime");

const SOURCE = path.resolve(__dirname, "../../plugins/usage_deck/desktop/plugin.js");

// Any host SDK component renders as a plain element carrying its children (StatusDot, Badge, Button, ...).
const ui = new Proxy({}, {
  get: (_t, name) => (typeof name === "string" && /^[A-Z]/.test(name)
    ? (props) => React.createElement("span", { "data-ui": name }, props && props.children)
    : undefined),
});

function load(names) {
  let src = fs.readFileSync(SOURCE, "utf8");
  src = src.replace(/^import \* as ui from .*$/m, "")
    .replace(/^import \{[^}]*\} from 'react'$/m, "")
    .replace(/^import \{ jsx, jsxs \} from 'react\/jsx-runtime'$/m, "")
    .replace(/^export default \{/m, "const __default = {");
  if (/^import /m.test(src) || !/^const __default = \{/m.test(src)) throw new Error("desktop plugin shape changed");
  src += "\nreturn { " + names.join(", ") + " }\n";
  const factory = new Function("ui", "useCallback", "useEffect", "useMemo", "useState", "jsx", "jsxs", src);
  return factory(ui, React.useCallback, React.useEffect, React.useMemo, React.useState, jsx, jsxs);
}

/** Render one component inside a <ul>; returns the container (textContent/querySelector only). */
async function render(Component, props) {
  const container = dom.window.document.createElement("div");
  dom.window.document.body.appendChild(container);
  const { createRoot } = nmRequire("react-dom/client");
  const root = createRoot(container);
  await act(async () => { root.render(React.createElement("ul", null, React.createElement(Component, props))); });
  return { container, unmount: async () => { await act(async () => { root.unmount(); }); container.remove(); } };
}

module.exports = { load, render };
