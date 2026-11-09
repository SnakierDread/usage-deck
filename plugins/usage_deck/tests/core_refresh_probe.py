"""Subprocess probe for ``test_anthropic_credentials.RealCorePool``: drives Hermes' REAL credential pool.

Runs in its own interpreter with HERMES_HOME pointed at a scratch dir under /tmp, so core's refresh + persist
machinery works on a throwaway ``auth.json`` and the process-wide Hermes home of the test run is never touched.
Only the network call (``refresh_anthropic_oauth_pure``) is replaced. Usage: probe.py <repo_root> <scenario>
(scenario: rotate | dead | transient). Prints one JSON object (no secrets are real).
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

repo_root, scenario = sys.argv[1], sys.argv[2]
home = tempfile.mkdtemp(prefix="ud-core-probe-", dir="/tmp")
os.environ["HERMES_HOME"] = home
sys.path.insert(0, repo_root)

AUTH = Path(home) / "auth.json"
AUTH.write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [{
    "id": "row-probe", "label": "probe", "auth_type": "oauth", "priority": 0, "source": "manual:probe",
    "access_token": "OLD-ACCESS", "refresh_token": "OLD-REFRESH",
    "expires_at_ms": int((time.time() - 3600) * 1000)}]}}))

import agent.anthropic_credentials as ac  # noqa: E402

posts = []


def fake_refresh(refresh_token, use_json=False):
    posts.append(refresh_token)
    if scenario == "rotate":
        return {"access_token": "NEW-ACCESS", "refresh_token": "NEW-REFRESH",
                "expires_at_ms": int((time.time() + 3600) * 1000)}
    if scenario == "dead":
        raise ac.AnthropicOAuthError(400, "invalid_grant", "Refresh token not found or invalid", what="refresh")
    raise ValueError("network is down")  # transient: not a terminal OAuth verdict


ac.refresh_anthropic_oauth_pure = fake_refresh

from plugins.usage_deck.adapters.base import SecretValue  # noqa: E402
from plugins.usage_deck.adapters.credentials import CoreTokenResolver, StoredRow  # noqa: E402

row = StoredRow(SecretValue("OLD-ACCESS"), time.time() - 3600, "row-probe", True)
resolver = CoreTokenResolver()
result = resolver.resolve(row, datetime.now(timezone.utc))
pool_rows = json.loads(AUTH.read_text()).get("credential_pool", {}).get("anthropic", [])
stored = pool_rows[0] if pool_rows else {}
print(json.dumps({
    "status": result.status, "refreshed": result.refreshed,
    "token": result.token.reveal() if result.token else None,
    "posts": posts, "stored_access": stored.get("access_token"), "stored_refresh": stored.get("refresh_token"),
    "stored_status": stored.get("last_status"), "rows": len(pool_rows),
}))
