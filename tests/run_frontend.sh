#!/bin/sh
# Run the frontend + packaging tests. React/jsdom come from the Hermes checkout's
# node_modules (override with HERMES_NODE_MODULES); they are test-only, never plugin deps.
set -e
cd "$(dirname "$0")/.."
export HERMES_NODE_MODULES="${HERMES_NODE_MODULES:-$HOME/.hermes/hermes-agent/node_modules}"
# --test-force-exit: jsdom keeps timers alive after the last test.
node --test --test-force-exit --test-timeout=60000 tests/frontend/*.test.js
python3 -m unittest discover -s tests/packaging -v
