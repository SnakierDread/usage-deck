#!/usr/bin/env python3
"""Seed synthetic session usage into a SCRATCH Hermes home's state.db (acceptance harness helper).

Run with the *scratch* environment (HERMES_HOME=<scratch root>), never against a real home:

    HERMES_HOME=<scratch> python seed_sessions.py <hermes-agent-checkout>

Uses the host's own ``SessionDB`` so the schema is the host's. Values are invented and fixed:
two sessions, 1,200/340 + 800/160 input/output tokens, 3 + 2 API calls. Refuses to run if
HERMES_HOME is not an existing directory containing no ``.hermes`` ancestry marker of the account home.
"""
import os
import sys
from pathlib import Path

home = Path(os.environ.get("HERMES_HOME", "")).resolve()
if not home.is_dir() or home == Path.home().resolve() / ".hermes":
    sys.exit("refusing to seed: HERMES_HOME must be an existing scratch directory")
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from hermes_state import SessionDB  # noqa: E402  (host module)

db = SessionDB(home / "state.db")
try:
    for sid, tokens_in, tokens_out, cost, calls in (("syn-session-1", 1200, 340, 0.0123, 3),
                                                    ("syn-session-2", 800, 160, 0.0045, 2)):
        db.create_session(sid, "cli", model="synthetic-model")
        db.update_token_counts(sid, input_tokens=tokens_in, output_tokens=tokens_out, model="synthetic-model",
                               estimated_cost_usd=cost, api_call_count=calls, source="cli")
finally:
    db.close()
print("seeded", home / "state.db")
