"""Pre-feature spend snapshot (C1): fixture + collector + generator.

The golden file ``fixtures/spend_golden_v1.json`` was produced by running ``python -m
plugins.usage_deck.tests.spend_golden --write <path>`` on the PRE-FEATURE tree (origin/main c4a662d, which has no
pricing module at all). ``test_api_equivalent_qa.SpendGoldenTests`` replays the identical fixture on the feature
branch and requires every pre-feature field to be byte-identical (canonical JSON) once the additive API-equivalent
annotation keys are removed.

This module must keep importing ONLY pre-feature code so the generator runs unmodified on the old tree.
"""

import contextlib
import json
import os
import sys
import time
from pathlib import Path

from plugins.usage_deck.discovery import FileInventorySource
from plugins.usage_deck.service import UsageDeckService

from .helpers import FakeTransport
from .test_analytics_dimensions import NOW, S, DirectSqlAnalytics, Machine

# Additive keys of the API-equivalent feature (docs/design/api-equivalent-pricing.md §7) and of analytics depth
# (docs/design/analytics-depth-breakdown.md §2, §6): the declared additions, whitelisted by name so every other
# pre-feature field must stay byte-identical.
ANNOTATION_KEYS = frozenset({
    "api_equivalent_usd", "api_equivalent", "pricing",
    "durations", "duration_samples", "heatmap", "cache_metrics", "repo_rollup", "model_rollup", "depth_totals",
    "depth_status", "repo_basename", "repo_paths",
})

SEVEN = ["gpt-6-luna", "gpt-6.1-sol", "claude-sonnet-5-5", "qwen3.8-flash-next", "qwen3.6:35b",
         "cyber-tiel-coder:35b", "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k"]

# (name, sessions). Host-estimated spend is a mix of real dollars, exact 0.0 and NULL so the sum-known /
# null-vs-0 behaviour of ``estimated_usd`` is part of the snapshot.
GLOBAL = [
    S("g1", 1, "gpt-6-luna", "openrouter", None, 1000, 10, 1.25, 1, "openrouter"),
    S("g2", 2, "gpt-6.1-sol", "custom:lane", None, 250_000, 12_000, 0.0, 4, "custom:lane"),
    S("g3", 2, "claude-sonnet-5-5", "anthropic", None, 40_000, 900, 0.3125, 2, "anthropic"),
    S("g4", 3, "qwen3.8-flash-next", "custom:lane", None, 1_000_000, 200_000, 0.0, 9, "custom:lane"),
    S("g5", 3, "mystery-model", "custom:x", None, 77, 7, None, 2, "custom:x"),
    S("g6", 4, "qwen3.6:35b", "custom:lane", None, None, None, 0.0, 1, "custom:lane"),
    S("g7", 5, "gpt-6-luna", "openrouter", None, 3, 1, 0.0000125, 1, "openrouter"),
]
ALPHA = [
    S("a1", 1, "cyber-tiel-coder:35b", "custom:lane", None, 5000, 50, 0.0, 1, "custom:lane"),
    S("a2", 2, "qwen3.8-hauhau-aggressive-27b-iq3-gpu-96k-b8k", "custom:lane", None, 123_456, 7_890, 0.0, 3,
      "custom:lane"),
    S("a3", 2, "gpt-6-luna", "openrouter", None, 9_999, 99, 0.5, 5, "openrouter"),
    S("a4", 9, "claude-sonnet-5-5", "anthropic", None, 1_000_000, 100_000, 7.5, 12, "anthropic"),
]

# (mode, dimension, window, provider) combinations snapshotted.
REQUESTS = [
    ("machine", "all", 30, None), ("machine", "profile", 30, None), ("machine", "model", 30, None),
    ("machine", "model", 7, None), ("machine", "all", 90, None), ("machine", "model", 30, "openrouter"),
    ("profile", "all", 30, None), ("profile", "model", 30, None),
]


def strip(obj):
    """A copy without any annotation key (what the payload looked like before this feature)."""
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in ANNOTATION_KEYS}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


@contextlib.contextmanager
def pinned_utc():
    """Daily buckets use SQLite 'localtime': pin the zone so the snapshot is host-independent."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def collect(cleanup, **service_kwargs):
    """Run every REQUESTS combination against the fixture; returns {request-key: payload ``data``}.

    ``cleanup`` is a callable registering a teardown (``TestCase.addCleanup`` or a ``list.append`` shim).
    """
    class _T:
        addCleanup = staticmethod(cleanup)

    with pinned_utc():
        return _collect(_T, **service_kwargs)


def _collect(_T, **service_kwargs):
    machine = Machine(_T)
    machine.write_db(None, GLOBAL)
    machine.write_db("alpha", ALPHA)
    out = {}
    for mode, dimension, window, provider in REQUESTS:
        home = machine.home(None)
        src = FileInventorySource(home, machine.root, config_loader=lambda: {"model": {}},
                                  custom_provider_loader=lambda c: [], env_lookup=lambda n: None)

        def analytics(profile, _home=home):
            if profile is None or profile.lower() == "current":
                return DirectSqlAnalytics(_home)
            return DirectSqlAnalytics(machine.root if profile == "default" else machine.home(profile))

        svc = UsageDeckService(inventory_factory=lambda s=src: s, analytics_factory=analytics,
                               transport=FakeTransport(), clock=lambda: NOW, **service_kwargs)
        params = [("mode", mode), ("dimension", dimension), ("window", str(window))]
        if provider:
            params.append(("provider", provider))
        status, body = svc.handle("usage", params)
        if status != 200:
            raise SystemExit(f"usage request failed: {status} {body}")
        out[f"{mode}/{dimension}/{window}/{provider}"] = body["data"]
    return out


def main(argv):
    if len(argv) != 3 or argv[1] != "--write":
        raise SystemExit("usage: python -m plugins.usage_deck.tests.spend_golden --write <path>")
    cleanups = []
    data = collect(cleanups.append)
    Path(argv[2]).write_text(canonical(strip(data)) + "\n")
    for fn in reversed(cleanups):
        fn()
    print(f"wrote {argv[2]}: {len(data)} payloads")


if __name__ == "__main__":
    main(sys.argv)
