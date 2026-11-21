"""Synthetic fixture for the analytics-depth tests and the baseline-parity goldens (design §9).

Deliberately free of any import from the depth implementation (``analytics_depth``) so the SAME module can
drive the baseline (pre-depth) service when the C6 golden hashes are regenerated. Every database is a
temporary SQLite file written with plain ``sqlite3``; expected values come from the specs, never from the code.
"""

import json
import sqlite3
from datetime import datetime, timezone

from .test_analytics_dimensions import DAY, NOW, NOW_TS, Machine

DEPTH_COLUMNS = ("id TEXT, source TEXT, model TEXT, provider TEXT, billing_provider TEXT, started_at REAL, "
                 "input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL, api_call_count INTEGER, "
                 "ended_at REAL, last_activity_at REAL, git_repo_root TEXT, cache_read_tokens INTEGER, "
                 "cache_write_tokens INTEGER, reasoning_tokens INTEGER")


def D(sid, days_ago, model, provider, inp, out, cost, repo=None, *, hour=0, dur=None, billing=None, calls=1,
      cr=None, cw=None, rs=None, started=None, ended="auto", want=None):
    """A session spec. ``dur`` seconds after start = ended_at (None = open); ``want`` = attributed provider."""
    start = NOW_TS - days_ago * DAY - hour * 3600 if started is None else started
    end = (None if dur is None else start + dur) if ended == "auto" else ended
    return {"id": sid, "started_at": start, "model": model, "provider": provider, "billing_provider": billing,
            "input_tokens": inp, "output_tokens": out, "estimated_cost_usd": cost, "api_call_count": calls,
            "ended_at": end, "last_activity_at": start + 99999 if end is None else end + 5, "git_repo_root": repo,
            "cache_read_tokens": cr, "cache_write_tokens": cw, "reasoning_tokens": rs,
            "want": want or (provider or billing or "unknown")}


ROOT = [
    D("r1", 1, "claude-sonnet-5.5", "anthropic", 1000, 500, 0.5, "/work/usage-deck", dur=600, cr=4000, cw=500, rs=100),
    D("r2", 2, "claude-sonnet-5.5", "anthropic", 2000, 700, 1.0, "/work/usage-deck/", dur=120, cr=0, cw=0, rs=0),
    D("r3", 2, "local-x", "custom:box", 300, 100, 0.0, "/home/u/other/usage-deck", dur=None),  # open session
    D("r4", 3, "claude-sonnet-5.5", "anthropic", 10, 5, 0.01, "/srv/Unscoped", dur=0),  # zero length, literal Unscoped
    D("r5", 3, "claude-sonnet-5.5", "anthropic", None, None, None, None, dur=-30),  # negative interval, all null
    D("r6", 4, "claude-sonnet-5.5", "anthropic", 0, 0, 0.0, "", dur=5, cr=0, cw=0, rs=0),  # recorded zeros
    D("r7", 4, "claude-sonnet-5.5", "anthropic", 5, 5, None, "   ", dur=10),  # whitespace-only root
    D("r8", 5, "gpt-6-luna", "openai", 7, 3, 0.02, "/", dur=20, cr=2, cw=1, rs=1),  # separator-only root
    D("r9", 5, None, None, 11, 7, 0.0, "C:\\Users\\dev\\usage-deck", dur=7, billing="auto", want="unknown"),
    D("r10", 6, "claude-sonnet-5.5", "anthropic", 1, 1, 0.5, "/work/usage-deck", dur=1.5, cr=1, cw=1, rs=1),
    D("r11", 50, "claude-sonnet-5.5", "anthropic", 10 ** 9, 10 ** 9, 99.0, "/old/repo", dur=1),  # outside 30d
]
A = [
    D("a1", 1, "gpt-6-luna", "openai", 4000, 1500, 2.0, "/work/usage-deck", hour=2, dur=3600, cr=100, cw=0, rs=500),
    D("a2", 3, "claude-sonnet-5.5", None, 800, 200, 0.2, "/work/alpha", billing="nous", dur=60, cr=10, cw=10, rs=10),
    D("a3", 2, "mapped-m", None, 50, 25, 0.01, "/work/alpha", billing="custom", dur=30, want="custom:gpu"),
    D("a4", 4, None, None, 70, 30, 0.02, None, billing="auto", dur=None, want="unknown"),
    # more than eight models in total (model Other) and several providers for one model
    D("a5", 1, "m-01", "openai", 100, 10, 0.1, "/work/alpha", dur=10),
    D("a6", 1, "m-02", "openai", 90, 10, 0.1, "/work/beta", dur=20),
    D("a7", 1, "m-03", "openai", 80, 10, 0.1, "/work/beta", dur=30),
    D("a8", 1, "m-04", "openai", 70, 10, 0.1, "/work/gamma", dur=40),
    D("a9", 1, "m-05", "openai", 60, 10, 0.1, "/work/gamma", dur=50),
    D("a10", 1, "m-06", "openai", 50, 10, 0.1, "/work/delta", dur=60),
    D("a11", 1, "m-07", "openai", 40, 10, 0.1, "/work/delta", dur=70),
    D("a12", 1, "m-08", "openai", 30, 10, 0.1, "/work/epsilon", dur=80),
    D("a13", 1, "m-09", "openai", 20, 10, 0.1, "/work/epsilon", dur=90),
    D("a14", 1, "gpt-6-luna", "openrouter", 25, 5, 0.05, "/work/usage-deck", dur=5, cr=5, cw=5, rs=5),
    D("a15", 2, "Claude-Sonnet-5.5", "anthropic", 3, 3, 0.03, "/work/Usage-Deck", dur=2),  # exact id, case kept
]
B = [
    D("b1", 1, "claude-sonnet-5.5", "anthropic", 123, 45, 0.1, "/work/usage-deck", dur=100, cr=7, cw=3, rs=2),
    D("b2", 1, "claude-sonnet-5.5", "anthropic", None, None, 0.3, "/work/usage-deck", hour=1, dur=200),
    D("b9", 40, "claude-sonnet-5.5", "anthropic", 10 ** 9, 10 ** 9, 99.0, "/old/repo", dur=1),  # outside 30d
]


def seed(machine, columns=DEPTH_COLUMNS):
    machine.write_db(None, ROOT, columns=columns)
    machine.write_db("a", A, columns=columns)
    machine.write_models("a", [{"id": "mapped-m", "provider": "custom:gpu"}])
    machine.write_db("b", B, columns=columns)
    # an unbucketable start: TEXT in a REAL column compares > any number, so the window predicate selects it
    conn = sqlite3.connect(machine.home("b") / "state.db")
    conn.execute("INSERT INTO sessions (id, started_at, model, provider) VALUES ('bad', 'not-a-time', 'x', 'openai')")
    conn.commit()
    conn.close()
    return machine


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


def project_baseline(data):
    """Project a usage ``data`` object onto the pre-depth key set (design §9.5/C6): additions removed only."""
    keep = {"window_days", "mode", "dimension", "provider_id", "series", "groups", "rows", "totals", "providers",
            "sources", "complete", "top_n", "reason", "pricing"}
    out = {k: v for k, v in data.items() if k in keep}
    out["groups"] = [{k: v for k, v in g.items() if k not in ("durations", "duration_samples")}
                     for g in data["groups"]]
    out["rows"] = [{k: v for k, v in r.items() if k not in ("repo_basename", "repo_paths")} for r in data["rows"]]
    return out


def canonical(data):
    return json.dumps(project_baseline(data), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def golden_cases():
    """``(label, bound home or None, query items)`` of the C6 parity matrix (baseline dimensions only)."""
    cases = []
    for window in ("7", "30", "90"):
        for dim in ("all", "profile", "model"):
            for prov in (None, "anthropic", "custom"):
                items = [("mode", "machine"), ("dimension", dim), ("window", window)]
                cases.append((f"machine/{dim}/{window}/{prov}", None, items + ([("provider", prov)] if prov else [])))
        for home in ("root", "a", "b"):
            for dim in ("all", "model"):
                for prov in (None, "anthropic"):
                    items = [("mode", "profile"), ("dimension", dim), ("window", window)]
                    cases.append((f"profile-{home}/{dim}/{window}/{prov}", None if home == "root" else home,
                                  items + ([("provider", prov)] if prov else [])))
    return cases


def run_golden(machine, service_for_home):
    """``{label: sha256(canonical baseline projection)}`` for every parity case (needs TZ=UTC for local days)."""
    import hashlib
    out = {}
    for label, home, items in golden_cases():
        status, body = service_for_home(home).handle("usage", list(items))
        assert status == 200, (label, status, body)
        out[label] = hashlib.sha256(canonical(body["data"]).encode()).hexdigest()
    return out


__all__ = ["DAY", "NOW", "NOW_TS", "Machine", "D", "ROOT", "A", "B", "seed", "utc", "project_baseline", "canonical"]
