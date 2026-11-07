#!/usr/bin/env python3
"""UsageDeck actual-host acceptance harness (DESIGN.md §3 "verified in integration QA", §8).

Unlike ``plugins/usage_deck/tests/test_plugin_api.py`` (a standalone FastAPI mount) this drives the
**real Hermes dashboard host**:

1. builds an isolated scratch Hermes home from the committed synthetic fixture
   ``plugins/usage_deck/tests/fixtures/scratch_home/`` (3 cloud accounts + 2 self-hosted endpoints);
2. installs the plugin from the *committed* repo HEAD with ``hermes plugins install file://<repo>#plugins/usage_deck``
   (uncommitted changes are rejected, installed bytes are compared to git blobs);
3. starts ``hermes dashboard`` against that home and exercises ``/api/plugins/usage-deck/{subscriptions,usage,health}``
   through the host's own auth (session token), plugin-consent (``plugins.enabled``) and profile (``?profile=``) gates;
4. validates, with an independent hard-coded copy of the DESIGN.md §2/§3 schema, the exact wire shape, source-bound
   confidence labels, seeded-secret masking (responses + dashboard log) and config/auth immutability.

Isolation (no real-credential fallback). Hermes derives the *global* credential root from ``HERMES_HOME``'s ancestry:
a scratch home below ``~/.hermes`` resolves to the real ``~/.hermes`` and the plugin's per-provider global fallback would
read real credentials. Every child process therefore gets a **clean environment** (no inherited variables) with
``HOME`` and ``HERMES_HOME`` both inside the managed work directory, so the global root *is* the scratch root. The
harness proves this (``isolation`` checks: naive env resolves to the real root, harness env to the scratch root) and
asserts the real ``~/.hermes`` credential/config files are byte-identical before and after.

Network: all HTTPS is forced through a loopback proxy that refuses everything but openrouter.ai:443. Fake loopback endpoints serve the two self-hosted endpoints, and a loopback CONNECT
proxy + throw-away CA terminates TLS for ``openrouter.ai:443`` so the OpenRouter adapter's real HTTPS path runs against a
synthetic payload. Everything the fixture produces is **synthetic**; the harness makes no claim about any live pool.

Usage (host interpreter, from the repo root):

    python tests/acceptance/host_acceptance.py                 # full run, exit 0 only if every check passes
    python tests/acceptance/host_acceptance.py --keep          # keep the work dir (logs, JSON report)
    python tests/acceptance/host_acceptance.py --self-check    # offline: fixture + validators only (no host)

Stdlib only; needs the ``hermes`` CLI, ``git`` and ``openssl`` on PATH (or ``--hermes``).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import http.server
import json
import math
import os
import re
import secrets as _secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_REL = "plugins/usage_deck"
FIXTURE = REPO / PLUGIN_REL / "tests" / "fixtures" / "scratch_home"
API = "/api/plugins/usage-deck"
PLUGIN_NAME = "usage-deck"
OPENROUTER_HOST = "openrouter.ai"

# --------------------------------------------------------------------------- DESIGN.md §2/§3 (independent copy)

CARD_KEYS = {"id", "provider_id", "account_id", "provider_label", "account_label", "kind", "plan_name", "plan_source",
             "auth_status", "endpoint_id", "state", "confidence", "reason", "meters", "checked_at", "stale", "error",
             "debug", "holders"}
HOLDER_KEYS = {"profile", "is_global"}
METER_KEYS = {"id", "label", "value", "limit", "remaining", "unit", "scope", "period", "confidence", "source",
              "observed_at", "resets_at"}
PERIOD_KEYS = {"start", "end", "label"}
ERROR_KEYS = {"code", "message", "retryable", "provider_id", "account_id"}
DEBUG_KEYS = {"adapter", "capabilities", "source", "sanitized"}
POINT_KEYS = {"day", "input_count", "output_count", "estimated_usd", "sessions", "api_calls", "confidence"}
SERIES_KEYS = {"id", "provider_id", "account_id", "scope", "confidence", "source", "period", "window_days", "bucket",
               "timezone", "coverage", "points", "checked_at", "stale", "error"}
HEALTH_KEYS = {"endpoint_id", "provider_id", "display_name", "status", "confidence", "metrics_available", "latency_ms",
               "cpu_percent", "memory_bytes", "checked_at", "stale", "reason", "error"}
META_KEYS = {"schema_version", "request_id", "generated_at", "partial", "errors"}
ENVELOPE_KEYS = {"ok", "data", "error", "meta"}
DATA_KEYS = {"subscriptions": {"cards", "inventory_state", "view"}, "health": {"entries"},
             "usage": {"window_days", "provider_id", "series", "reason"}}
REASONS = {"unsupported", "credentials_missing", "permission_denied", "attribution_unavailable", "no_observations",
           "metrics_unavailable", "source_unavailable"}
ENUMS = {
    "confidence": {"exact", "estimated", "opaque", "self_hosted"},
    "state": {"ready", "error", "opaque", "empty"},
    "kind": {"cloud", "self_hosted"},
    "plan_source": {"unverified_claim", "provider", "none"},
    "auth_status": {"available", "missing"},
    "unit": {"usd", "count", "percent", "bytes"},
    "meter_scope": {"api_credential", "provider_account", "endpoint", "profile"},
    "series_scope": {"profile", "provider", "account"},
    "meter_source": {"openrouter_key", "session_analytics", "prometheus", "anthropic_headers", "none"},
    "view": {"machine", "profile"},
    "status": {"healthy", "degraded", "offline", "unknown"},
    "error_code": {"invalid_query", "provider_not_found", "auth_required", "permission_denied", "timeout",
                   "upstream_unavailable", "malformed_response", "inventory_unavailable", "analytics_unavailable",
                   "internal_error"},
    "capability": {"quota", "usage", "metrics", "session_analytics"},
}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACCT_RE = re.compile(r"^acct_[0-9a-f]{24}$")
EP_RE = re.compile(r"^ep_[0-9a-f]{24}$")
SENSITIVE_FRAGMENTS = ("key", "token", "secret", "password", "authorization", "cookie", "credentials", "bearer",
                       "jwt", "access", "refresh")


def _num(v, nullable=True):
    if v is None:
        return nullable
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _keys(errs, where, obj, expected):
    if not isinstance(obj, dict):
        errs.append(f"{where}: not an object")
        return False
    missing, extra = sorted(expected - set(obj)), sorted(set(obj) - expected)
    if missing or extra:
        errs.append(f"{where}: keys differ from schema (missing={missing} extra={extra})")
        return False
    return True


def _enum(errs, where, value, allowed, nullable=False):
    if not ((nullable and value is None) or value in allowed):
        errs.append(f"{where}: {value!r} not in {sorted(allowed)}")


def validate_error(errs, where, err):
    if err is None:
        return
    if _keys(errs, where, err, ERROR_KEYS):
        _enum(errs, where + ".code", err["code"], ENUMS["error_code"])
        if not isinstance(err["message"], str) or not isinstance(err["retryable"], bool):
            errs.append(f"{where}: bad message/retryable")


def validate_period(errs, where, p):
    if _keys(errs, where, p, PERIOD_KEYS):
        for k in ("start", "end"):
            if p[k] is not None and not (isinstance(p[k], str) and TS_RE.match(p[k])):
                errs.append(f"{where}.{k}: bad timestamp {p[k]!r}")
        if (p["start"] is None) != (p["end"] is None):
            errs.append(f"{where}: one bound null")
        if not (isinstance(p["label"], str) and re.match(r"^[a-z0-9_]{1,32}$", p["label"])):
            errs.append(f"{where}.label: {p['label']!r}")


def validate_meter(errs, where, m):
    if not _keys(errs, where, m, METER_KEYS):
        return
    for k in ("value", "limit", "remaining"):
        if not _num(m[k]):
            errs.append(f"{where}.{k}: not a finite number/null")
    _enum(errs, where + ".unit", m["unit"], ENUMS["unit"])
    _enum(errs, where + ".scope", m["scope"], ENUMS["meter_scope"])
    _enum(errs, where + ".confidence", m["confidence"], ENUMS["confidence"])
    _enum(errs, where + ".source", m["source"], ENUMS["meter_source"])
    validate_period(errs, where + ".period", m["period"])
    if m["observed_at"] is not None and not TS_RE.match(str(m["observed_at"])):
        errs.append(f"{where}.observed_at")
    if m["resets_at"] is not None and not TS_RE.match(str(m["resets_at"])):
        errs.append(f"{where}.resets_at")


def validate_card(errs, where, c):
    if not _keys(errs, where, c, CARD_KEYS):
        return
    for k, enum in (("kind", "kind"), ("plan_source", "plan_source"), ("auth_status", "auth_status"),
                    ("state", "state"), ("confidence", "confidence")):
        _enum(errs, f"{where}.{k}", c[k], ENUMS[enum])
    _enum(errs, where + ".reason", c["reason"], REASONS, nullable=True)
    if not ACCT_RE.match(str(c["account_id"])) or c["id"] != c["account_id"]:
        errs.append(f"{where}: id/account_id shape")
    if c["endpoint_id"] is not None and not EP_RE.match(str(c["endpoint_id"])):
        errs.append(f"{where}.endpoint_id")
    if not TS_RE.match(str(c["checked_at"])) or not isinstance(c["stale"], bool):
        errs.append(f"{where}: checked_at/stale")
    for k in ("provider_id", "provider_label", "account_label"):
        if not isinstance(c[k], str):
            errs.append(f"{where}.{k}: not a string")
    if c["plan_name"] is not None and not isinstance(c["plan_name"], str):
        errs.append(f"{where}.plan_name")
    if c["plan_name"] is None and c["plan_source"] == "unverified_claim":
        errs.append(f"{where}: plan_source without plan")
    if not isinstance(c["meters"], list):
        errs.append(f"{where}.meters: not a list")
    else:
        for i, m in enumerate(c["meters"]):
            validate_meter(errs, f"{where}.meters[{i}]", m)
    validate_error(errs, where + ".error", c["error"])
    if not isinstance(c["holders"], list):
        errs.append(f"{where}.holders: not a list")
    else:
        for i, holder in enumerate(c["holders"]):
            if _keys(errs, f"{where}.holders[{i}]", holder, HOLDER_KEYS):
                if not (isinstance(holder["profile"], str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", holder["profile"])) \
                        or not isinstance(holder["is_global"], bool):
                    errs.append(f"{where}.holders[{i}]: bad profile/is_global")
    if _keys(errs, where + ".debug", c["debug"], DEBUG_KEYS):
        if c["debug"]["sanitized"] is not True:
            errs.append(f"{where}.debug.sanitized")
        for cap in c["debug"]["capabilities"]:
            _enum(errs, where + ".debug.capabilities", cap, ENUMS["capability"])


def validate_health(errs, where, h):
    if not _keys(errs, where, h, HEALTH_KEYS):
        return
    _enum(errs, where + ".status", h["status"], ENUMS["status"])
    _enum(errs, where + ".confidence", h["confidence"], ENUMS["confidence"])
    _enum(errs, where + ".reason", h["reason"], REASONS, nullable=True)
    if not EP_RE.match(str(h["endpoint_id"])) or not TS_RE.match(str(h["checked_at"])):
        errs.append(f"{where}: endpoint_id/checked_at")
    if not isinstance(h["metrics_available"], bool) or not isinstance(h["stale"], bool):
        errs.append(f"{where}: bools")
    for k in ("latency_ms", "cpu_percent"):
        if not _num(h[k]) or (h[k] is not None and h[k] < 0):
            errs.append(f"{where}.{k}")
    if h["memory_bytes"] is not None and (isinstance(h["memory_bytes"], bool) or not isinstance(h["memory_bytes"], int)):
        errs.append(f"{where}.memory_bytes")
    validate_error(errs, where + ".error", h["error"])


def validate_series(errs, where, s):
    if not _keys(errs, where, s, SERIES_KEYS):
        return
    _enum(errs, where + ".scope", s["scope"], ENUMS["series_scope"])
    _enum(errs, where + ".confidence", s["confidence"], ENUMS["confidence"])
    _enum(errs, where + ".window_days", s["window_days"], {7, 30, 90})
    for k, v in (("source", "session_analytics"), ("bucket", "day"), ("timezone", "server_local")):
        if s[k] != v:
            errs.append(f"{where}.{k}: {s[k]!r}")
    _enum(errs, where + ".coverage", s["coverage"], {"partial", "complete"})
    validate_period(errs, where + ".period", s["period"])
    days = []
    for i, p in enumerate(s["points"]):
        if _keys(errs, f"{where}.points[{i}]", p, POINT_KEYS):
            if not DAY_RE.match(str(p["day"])):
                errs.append(f"{where}.points[{i}].day")
            days.append(p["day"])
            _enum(errs, f"{where}.points[{i}].confidence", p["confidence"], ENUMS["confidence"])
    if days != sorted(set(days)):
        errs.append(f"{where}: points not unique+ascending")
    validate_error(errs, where + ".error", s["error"])


def validate_envelope(endpoint, body, status=200):
    """Return a list of schema violations for a plugin-owned response."""
    errs = []
    if not _keys(errs, "envelope", body, ENVELOPE_KEYS):
        return errs
    meta = body["meta"]
    if _keys(errs, "meta", meta, META_KEYS):
        if meta["schema_version"] != 1 or not UUID_RE.match(str(meta["request_id"])) \
                or not TS_RE.match(str(meta["generated_at"])):
            errs.append("meta: schema_version/request_id/generated_at")
        if not isinstance(meta["errors"], list) or meta["partial"] != bool(meta["errors"]):
            errs.append("meta: partial/errors")
        else:
            for i, e in enumerate(meta["errors"]):
                validate_error(errs, f"meta.errors[{i}]", e)
    if status != 200 or body["ok"] is False:
        if body["ok"] is not False or body["data"] is not None or body["error"] is None:
            errs.append("failure envelope: ok/data/error")
        validate_error(errs, "error", body["error"])
        return errs
    if body["ok"] is not True or body["error"] is not None:
        errs.append("success envelope: ok/error")
    data = body["data"]
    if not _keys(errs, "data", data, DATA_KEYS[endpoint]):
        return errs
    if endpoint == "subscriptions":
        _enum(errs, "data.inventory_state", data["inventory_state"], {"empty", "detected"})
        _enum(errs, "data.view", data["view"], ENUMS["view"])
        if (data["inventory_state"] == "empty") != (not data["cards"]):
            errs.append("data.inventory_state disagrees with cards")
        for i, c in enumerate(data["cards"]):
            validate_card(errs, f"cards[{i}]", c)
        keys = [(c["provider_id"], c["account_id"]) for c in data["cards"]]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            errs.append("cards not sorted/unique by (provider_id, account_id)")
    elif endpoint == "health":
        for i, h in enumerate(data["entries"]):
            validate_health(errs, f"entries[{i}]", h)
        keys = [(h["provider_id"], h["endpoint_id"]) for h in data["entries"]]
        if keys != sorted(keys) or len(set(h["endpoint_id"] for h in data["entries"])) != len(keys):
            errs.append("entries not sorted / one per endpoint")
    else:
        _enum(errs, "data.window_days", data["window_days"], {7, 30, 90})
        _enum(errs, "data.reason", data["reason"], REASONS, nullable=True)
        for i, s in enumerate(data["series"]):
            validate_series(errs, f"series[{i}]", s)
    return errs


def source_binding_violations(cards, health, series):
    """Confidence labels must be bound to the source that justifies them (DESIGN.md §5)."""
    v = []
    health_by_ep = {h["endpoint_id"]: h for h in health}
    for c in cards:
        tag = f"{c['provider_id']}/{c['account_label']}"
        for m in c["meters"]:
            openrouter_ok = (m["source"] == "openrouter_key" and c["provider_id"] == "openrouter"
                             and m["unit"] == "usd" and m["scope"] == "api_credential" and m["observed_at"])
            headers_ok = (m["source"] == "anthropic_headers" and c["provider_id"] == "anthropic"
                          and m["unit"] == "percent" and m["scope"] == "provider_account" and m["observed_at"]
                          and m["period"]["label"] in ("rolling_5h", "rolling_7d")
                          and m["period"]["start"] is None and m["period"]["end"] is None)
            if m["confidence"] == "exact" and not (openrouter_ok or headers_ok):
                v.append(f"{tag}: exact meter without a verified source (openrouter_key / anthropic_headers)")
            if m["source"] == "openrouter_key" and c["provider_id"] != "openrouter":
                v.append(f"{tag}: openrouter_key source on another provider")
            if m["source"] == "anthropic_headers" and c["provider_id"] != "anthropic":
                v.append(f"{tag}: anthropic_headers source on another provider")
            if m["confidence"] == "self_hosted":
                v.append(f"{tag}: self_hosted confidence on a billing meter")
        if c["confidence"] == "exact" and not any(m["confidence"] == "exact" for m in c["meters"]):
            v.append(f"{tag}: card exact without an exact meter")
        if c["state"] == "ready" and not any(m["value"] is not None for m in c["meters"]):
            v.append(f"{tag}: ready without a measured value")
        if c["confidence"] == "self_hosted":
            h = health_by_ep.get(c["endpoint_id"])
            if c["kind"] != "self_hosted" or h is None or not h["metrics_available"]:
                v.append(f"{tag}: self_hosted card without successful metrics")
        if c["kind"] == "cloud" and c["provider_id"] not in ("openrouter", "anthropic") and c["confidence"] != "opaque":
            v.append(f"{tag}: cloud card without a verified quota source must be opaque")
        if c["provider_id"] == "anthropic" and c["confidence"] != "opaque" and not c["meters"]:
            v.append(f"{tag}: anthropic card above opaque without a header meter")
        if c["auth_status"] == "available" and c["state"] == "error" and c["error"] is None:
            v.append(f"{tag}: error state without error")
    for h in health:
        if h["confidence"] == "self_hosted" and not h["metrics_available"]:
            v.append(f"health {h['provider_id']}: self_hosted without metrics")
        if h["metrics_available"] is False and (h["cpu_percent"] is not None or h["memory_bytes"] is not None):
            v.append(f"health {h['provider_id']}: cpu/memory without metrics")
    for s in series:
        if s["scope"] == "profile" and (s["provider_id"] is not None or s["account_id"] is not None):
            v.append("profile series carries provider/account")
        if s["confidence"] == "exact" or s["source"] != "session_analytics":
            v.append("series must be session_analytics and never exact")
    return v


# --------------------------------------------------------------------------- secrets

def _is_sensitive(name):
    low = str(name).lower()
    return any(f in low for f in SENSITIVE_FRAGMENTS)


def collect_seeded_secrets(obj, found=None, under_sensitive=False):
    """Every string stored under a sensitive field name anywhere in ``obj`` (and the label copies of them)."""
    found = set() if found is None else found
    if isinstance(obj, dict):
        for k, v in obj.items():
            collect_seeded_secrets(v, found, under_sensitive or _is_sensitive(k))
    elif isinstance(obj, list):
        for v in obj:
            collect_seeded_secrets(v, found, under_sensitive)
    elif isinstance(obj, str) and under_sensitive and obj:
        found.add(obj)
    return found


def jwt_parts(token):
    parts = token.split(".")
    return [p for p in parts if len(p) >= 8] if token.startswith("eyJ") else []


def config_secrets(text):
    return set(re.findall(r'api_key:\s*"([^"]+)"', text))


# --------------------------------------------------------------------------- results

class Results:
    def __init__(self):
        self.items = []
        self.scenario = "-"
        self.record = {}

    def check(self, name, ok, detail=""):
        self.items.append({"scenario": self.scenario, "check": name, "ok": bool(ok), "detail": str(detail)[:600]})
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {self.scenario}: {name}" + (f"  -- {detail}" if (not ok and detail) else ""), flush=True)
        return bool(ok)

    @property
    def failed(self):
        return [i for i in self.items if not i["ok"]]


# --------------------------------------------------------------------------- fake network

class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []

    def add(self, **kw):
        with self.lock:
            self.requests.append(kw)


class FakeEndpoint:
    """Loopback HTTP endpoint with fixed routes; every request (any method) is recorded."""

    def __init__(self, routes):
        rec = self.rec = _Recorder()
        table = routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def _serve(self, method):
                auth = self.headers.get("Authorization")
                rec.add(method=method, path=self.path, authorization=auth)
                if method != "GET":
                    self.send_response(405)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, ctype, body = table.get(self.path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            do_PUT = do_DELETE = do_PATCH = do_HEAD = do_POST

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OpenRouterInterceptor:
    """CONNECT proxy: terminates TLS for openrouter.ai:443 with a throw-away CA; refuses every other target."""

    PAYLOAD_USAGE, PAYLOAD_LIMIT, PAYLOAD_REMAINING = 1.5, 5, 3.5

    def __init__(self, workdir: Path):
        self.rec = _Recorder()
        self.connects = []
        self.ca_pem = workdir / "ca.pem"
        srv_pem, srv_key = workdir / "srv.pem", workdir / "srv.key"
        self._mkcerts(workdir, srv_pem, srv_key)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(srv_pem), str(srv_key))
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _mkcerts(self, wd, srv_pem, srv_key):
        def sh(*args):
            subprocess.run(["openssl", *args], cwd=wd, check=True, capture_output=True)

        (wd / "ext.cnf").write_text("subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n"
                                    "extendedKeyUsage=serverAuth\n" % OPENROUTER_HOST)
        sh("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key",
           "-out", "ca.pem", "-days", "2", "-subj", "/CN=usage-deck-acceptance-throwaway-ca",
           "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        sh("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", srv_key.name,
           "-out", "srv.csr", "-subj", "/CN=" + OPENROUTER_HOST)
        sh("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
           srv_pem.name, "-days", "2", "-extfile", "ext.cnf")

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_head(conn):
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _handle(self, conn):
        try:
            conn.settimeout(10)
            head = self._read_head(conn).decode("latin-1")
            first = head.split("\r\n", 1)[0].split()
            if len(first) < 2 or first[0] != "CONNECT":
                self.connects.append(("non-CONNECT:" + (first[0] if first else "?"), False))
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            target = first[1]
            allowed = target == f"{OPENROUTER_HOST}:443"
            self.connects.append((target, allowed))
            if not allowed:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tls = self.ctx.wrap_socket(conn, server_side=True)
            request = self._read_head(tls).decode("latin-1")
            line = request.split("\r\n", 1)[0].split()
            headers = {k.lower(): v for k, v in (h.split(": ", 1) for h in request.split("\r\n")[1:] if ": " in h)}
            auth = headers.get("authorization", "")
            self.rec.add(method=line[0], path=line[1], authorization=auth)
            if line[0] == "GET" and line[1] == "/api/v1/key":
                body = json.dumps({"data": {
                    "label": auth.replace("Bearer ", ""),            # upstream text echoing the credential
                    "usage": self.PAYLOAD_USAGE, "limit": self.PAYLOAD_LIMIT,
                    "limit_remaining": self.PAYLOAD_REMAINING, "is_free_tier": False,
                    "hash": "UDSYN-upstream-echo-91ac4e"}}).encode()
                status = b"200 OK"
            else:
                body, status = b"{}", b"404 Not Found"
            tls.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            tls.close()
        except (OSError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def close(self):
        self._stop = True
        with contextlib.suppress(OSError):
            self.sock.close()


# --------------------------------------------------------------------------- host process + http

class Resp:
    def __init__(self, status, headers, raw):
        self.status, self.raw = status, raw
        self.headers = {str(k).lower(): v for k, v in headers.items()}   # HTTP header names are case-insensitive
        self.text = raw.decode("utf-8", "replace")
        try:
            self.json = json.loads(self.text)
        except ValueError:
            self.json = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def http_get(url, token=None, headers=None, timeout=20, method="GET"):
    hdrs = dict(headers or {})
    if token:
        hdrs["X-Hermes-Session-Token"] = token
    req = urllib.request.Request(url, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, dict(e.headers), e.read())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stat_sig(path):
    p = Path(path)
    if not p.exists():
        return None
    st = p.stat()
    return {"sha256": sha256_file(p), "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "size": st.st_size}


class Scratch:
    """One isolated Hermes home (+ HOME, TMPDIR, XDG dirs) under the managed work dir."""

    def __init__(self, base: Path, name: str, proxy_port: int, ca_pem: Path, hermes: str):
        self.name = name
        self.base = base / name
        self.home = self.base / "home"
        self.root = self.base / "hermes_root"        # HERMES_HOME == global root (no ancestry games)
        self.tmp = self.base / "tmp"
        self.logs = self.base / "logs"
        for d in (self.home, self.root, self.tmp, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.proxy_port, self.ca_pem, self.hermes = proxy_port, ca_pem, hermes
        self.token = _secrets.token_urlsafe(24)
        self.port = free_port()

    def env(self):
        # Clean environment: nothing is inherited from the caller (no API keys, no real HOME/HERMES_HOME).
        proxy = f"http://127.0.0.1:{self.proxy_port}"
        e = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": str(self.home), "HERMES_HOME": str(self.root), "TMPDIR": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "HERMES_DASHBOARD_SESSION_TOKEN": self.token,
            "HTTPS_PROXY": proxy, "https_proxy": proxy, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "SSL_CERT_FILE": str(self.ca_pem), "REQUESTS_CA_BUNDLE": str(self.ca_pem),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1",
        }
        return e

    def cli(self, *args, timeout=180, home=None):
        env = self.env()
        if home is not None:
            env["HERMES_HOME"] = str(home)  # act as another profile of the same scratch root
        proc = subprocess.run([self.hermes, *args], env=env, cwd=str(self.base), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return proc

    def link_runtime(self, runtime_root: Path):
        """Let the scratch root *borrow* the machine's committed Hermes dependency environment, read-only.

        Hermes scopes dependency state per data root (``<root>/installs/<install_key>``); a fresh temporary
        ``HERMES_HOME`` would otherwise download a private environment from PyPI. The scratch root therefore
        gets its own ``installs/<key>/`` directory holding a copy of ``facts.json`` plus links to the machine's
        ``environments/``, ``pm-runtime/`` and ``inputs/``. Keeping ``installs/<key>`` a *distinct* directory is
        deliberate: it makes the host treat this root as a borrower of the checkout (launchers, install stamp and
        owner state are never rebound or published from here). Do NOT symlink ``installs`` or ``tools`` wholesale:
        that makes the host believe the scratch root owns the checkout and it rewrites the shared ``hermes``
        launchers to point into the (later deleted) scratch tree. No credentials live in any of these paths.
        """
        installs = Path(runtime_root) / "installs"
        if not installs.is_dir():
            return
        for key_dir in sorted(p for p in installs.iterdir() if p.is_dir()):
            dest = self.root / "installs" / key_dir.name
            dest.mkdir(parents=True, exist_ok=True)
            if (key_dir / "facts.json").is_file():
                shutil.copy2(key_dir / "facts.json", dest / "facts.json")
            for name in ("environments", "pm-runtime", "inputs"):
                if (key_dir / name).exists() and not (dest / name).exists():
                    (dest / name).symlink_to(key_dir / name, target_is_directory=True)

    def render_fixture(self, fixture: Path, gpu_url: str, ollama_url: str):
        shutil.copytree(fixture, self.root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.template", "README*"))
        text = (fixture / "config.yaml.template").read_text()
        text = text.replace("@@GPU_BASE_URL@@", gpu_url).replace("@@OLLAMA_BASE_URL@@", ollama_url)
        (self.root / "config.yaml").write_text(text)

    def watched(self):
        files = ["auth.json", "config.yaml", ".env", "profiles/work/auth.json", "profiles/work/config.yaml",
                 "profiles/verifier/auth.json"]
        return {rel: stat_sig(self.root / rel) for rel in files}

    def corrupt_backups(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*.corrupt*"))


class Host:
    """``hermes dashboard`` on a scratch home. Never uses ``--stop`` (would touch other dashboards)."""

    def __init__(self, scratch: Scratch):
        self.s = scratch
        self.proc = None
        self.log_path = scratch.logs / "dashboard.log"
        self.url = f"http://127.0.0.1:{scratch.port}"

    def start(self, timeout=90):
        self.s.port = free_port()
        self.url = f"http://127.0.0.1:{self.s.port}"
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [self.s.hermes, "dashboard", "--host", "127.0.0.1", "--port", str(self.s.port), "--no-open", "--isolated",
             "--skip-build"],
            env=self.s.env(), cwd=str(self.s.base), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"dashboard exited early (rc={self.proc.returncode}); see {self.log_path}")
            try:
                if http_get(self.url + "/api/status", timeout=3).status == 200:
                    return
            except (urllib.error.URLError, OSError, ConnectionError):
                pass
            time.sleep(0.5)
        raise RuntimeError(f"dashboard not ready after {timeout}s; see {self.log_path}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=10)
        self.proc = None

    def get(self, path, token=True, **kw):
        return http_get(self.url + path, token=self.s.token if token is True else (token or None), **kw)

    def api(self, endpoint, query="", view="profile", **kw):
        """GET a plugin endpoint. The pre-machine-view scenarios assert profile-scoped inventories, so they
        default to ``view=profile``; machine scenarios pass ``view=machine`` (or put view= in the query)."""
        if view and "view=" not in query:
            query = (query + "&" if query else "") + f"view={view}"
        return self.get(f"{API}/{endpoint}" + (("?" + query) if query else ""), **kw)

    def log_text(self):
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""


# --------------------------------------------------------------------------- git / install

def git(*args, cwd=REPO):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def committed_head():
    head = git("rev-parse", "HEAD").strip()
    dirty = [l for l in git("status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
    return head, dirty


def blob_hash(path: Path):
    return subprocess.run(["git", "hash-object", str(path)], capture_output=True, text=True, check=True).stdout.strip()


def committed_files(head):
    out = git("ls-tree", "-r", head, "--", PLUGIN_REL)
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        files[path[len(PLUGIN_REL) + 1:]] = meta.split()[2]
    return files


def install_plugin(s: Scratch, R: Results, head: str, enable: bool, home=None, label=""):
    """Install the COMMITTED plugin (file:// clone of the repo) into ``home`` (default: the scratch root)."""
    home = Path(home) if home else s.root
    proc = s.cli("plugins", "install", f"file://{REPO}#{PLUGIN_REL}", "--no-enable", home=home)
    R.check(f"hermes plugins install file://<repo>#plugins/usage_deck{label}", proc.returncode == 0,
            (proc.stdout + proc.stderr)[-500:])
    installed = home / "plugins" / PLUGIN_NAME
    R.check(f"plugin directory installed in the scratch home{label}", installed.is_dir(), str(installed))
    if installed.is_dir():
        want = committed_files(head)
        got = {}
        for p in sorted(installed.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(installed).parts:
                got[str(p.relative_to(installed))] = blob_hash(p)
        mismatched = sorted(k for k in got if k in want and got[k] != want[k])
        # the installer ships the package root (plugins/usage_deck): sources, staged dashboard/, dist/
        required = {"plugin.yaml", "dashboard/manifest.json", "dashboard/plugin_api.py", "dashboard/service.py",
                    "dashboard/dist/index.js", "dashboard/models/redaction.py"}
        R.check(f"installed files are byte-identical to committed HEAD blobs{label}", not mismatched and got,
                f"mismatched={mismatched[:5]}")
        R.check(f"installed package contains the staged dashboard files{label}", required <= set(got),
                f"missing={sorted(required - set(got))}")
        R.check(f"installed files exist in committed HEAD (nothing extra injected){label}",
                all(k in want for k in got), f"extra={sorted(k for k in got if k not in want)[:5]}")
    if enable:
        proc = s.cli("plugins", "enable", PLUGIN_NAME, home=home)
        R.check(f"hermes plugins enable usage-deck (consent granted in scratch config){label}", proc.returncode == 0,
                (proc.stdout + proc.stderr)[-500:])


# --------------------------------------------------------------------------- scenario plumbing

def account_home():
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


class Context:
    def __init__(self, args, workdir: Path, R: Results):
        self.args, self.workdir, self.R = args, workdir, R
        self.head, _ = committed_head()
        self.fixture_text = {p.name: p.read_text() for p in FIXTURE.glob("*") if p.is_file()}
        self.fixture_auth = json.loads((FIXTURE / "auth.json").read_text())
        self.fixture_work_auth = json.loads((FIXTURE / "profiles" / "work" / "auth.json").read_text())
        self.fixture_verifier_auth = json.loads((FIXTURE / "profiles" / "verifier" / "auth.json").read_text())
        self.long_secrets, self.short_secrets = split_secrets(
            collect_seeded_secrets(self.fixture_auth) | collect_seeded_secrets(self.fixture_work_auth)
            | collect_seeded_secrets(self.fixture_verifier_auth)
            | config_secrets(self.fixture_text["config.yaml.template"]))
        self.icp = OpenRouterInterceptor(workdir)
        self.gpu = FakeEndpoint({
            "/metrics": (200, "text/plain", b"# synthetic\nprocess_cpu_percent 12.5\nprocess_resident_memory_bytes 1000\n"),
            "/v1/models": (200, "application/json", b'{"data": []}')})
        self.ollama = FakeEndpoint({"/api/tags": (200, "application/json", b'{"models": []}')})
        self.gpu_url = f"http://127.0.0.1:{self.gpu.port}/v1"
        self.ollama_url = f"http://127.0.0.1:{self.ollama.port}"

    def scratch(self, name, variant=None):
        s = Scratch(self.workdir, name, self.icp.port, self.icp.ca_pem, self.args.hermes)
        s.link_runtime(Path(self.args.runtime_root))
        s.render_fixture(FIXTURE, self.gpu_url, self.ollama_url)
        if variant:
            variant(s)
        return s

    def seed_sessions(self, s: Scratch):
        proc = subprocess.run([sys.executable, str(REPO / "tests" / "acceptance" / "seed_sessions.py"),
                               str(self.args.hermes_agent)], env={**s.env(), "HERMES_HOME": str(s.root)},
                              capture_output=True, text=True, timeout=120)
        self.R.check("seed synthetic sessions into the scratch state.db", proc.returncode == 0,
                     (proc.stdout + proc.stderr)[-400:])

    def close(self):
        self.icp.close()
        self.gpu.close()
        self.ollama.close()


def split_secrets(found):
    """Short secrets (< 4 chars) cannot be substring-scanned (they occur inside field names); long ones can."""
    return {s for s in found if len(s) >= 4}, {s for s in found if len(s) < 4}


def scan_texts(R, label, texts, secrets):
    hits = sorted({s for s in secrets for t in texts if s in t})
    # a JWT can leak piecewise: scan its header/payload segments too
    for s in secrets:
        for part in jwt_parts(s):
            if any(part in t for t in texts):
                hits.append("jwt-segment:" + part[:6])
    R.check(f"{label}: no seeded secret appears", not hits, f"leaked={hits}")


def check_envelope_resp(R, label, endpoint, resp, status=200):
    ok = resp.status == status and resp.json is not None
    R.check(f"{label}: HTTP {status} + JSON", ok, f"status={resp.status} body={resp.text[:200]!r}")
    if not ok:
        return None
    errs = validate_envelope(endpoint, resp.json, status)
    R.check(f"{label}: exact DESIGN.md §2/§3 schema (keys, enums, formats, ordering)", not errs, "; ".join(errs[:6]))
    R.check(f"{label}: Cache-Control: no-store", "no-store" in resp.headers.get("cache-control", ""),
            resp.headers.get("cache-control"))
    return resp.json


def by_provider(cards):
    out = {}
    for c in cards:
        out.setdefault(c["provider_id"], []).append(c)
    return out


def free_text_has(card, needle):
    return [k for k in ("provider_label", "account_label", "plan_name") if card.get(k) and needle in card[k]]


# --------------------------------------------------------------------------- scenarios

def scenario_isolation(ctx: Context):
    R = ctx.R
    R.scenario = "isolation"
    real_root = account_home() / ".hermes"
    probe = ctx.workdir / "root_probe.py"
    probe.write_text("import sys\nsys.path.insert(0, sys.argv[1])\n"
                     "from hermes_constants import get_default_hermes_root\nprint(get_default_hermes_root())\n")

    def resolve(env):
        proc = subprocess.run([sys.executable, str(probe), str(ctx.args.hermes_agent)], env=env, capture_output=True,
                              text=True, timeout=60)
        return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None

    naive_home = real_root / "profiles" / "usage-deck-acceptance-naive"      # path only, never created
    naive = resolve({"PATH": os.environ.get("PATH", ""), "HOME": str(account_home()), "HERMES_HOME": str(naive_home)})
    R.check("negative control: HERMES_HOME under ~/.hermes with the real HOME resolves the REAL global root "
            "(the credential-fallback hazard this harness defends against)",
            naive == real_root.resolve(), f"naive={naive} real={real_root}")
    s = Scratch(ctx.workdir, "isolation", ctx.icp.port, ctx.icp.ca_pem, ctx.args.hermes)
    safe = resolve(s.env())
    R.check("harness environment: global root == the scratch root (isolated global root injected)",
            safe == s.root.resolve(), f"resolved={safe} scratch={s.root}")
    env = s.env()
    R.check("child environment is clean (HOME/HERMES_HOME/TMPDIR inside the managed work dir; no inherited API keys)",
            all(str(Path(env[k]).resolve()).startswith(str(ctx.workdir.resolve()))
                for k in ("HOME", "HERMES_HOME", "TMPDIR")) and not any(
                re.search(r"(API_KEY|TOKEN|SECRET|PASSWORD)", k) and k != "HERMES_DASHBOARD_SESSION_TOKEN" for k in env),
            sorted(env))


def real_credential_files():
    roots = {account_home() / ".hermes"}
    if os.environ.get("HERMES_HOME"):
        roots.add(Path(os.environ["HERMES_HOME"]))
    return [r / n for r in sorted(roots) for n in ("auth.json", ".env", "config.yaml")]


def snapshot_real():
    """Hashes of the real credential/config files AND of the shared launchers / bootstrap records.

    The launcher/bootstrap part guards the host's shared install state: a scratch run must never rebind the
    ``hermes`` launchers or add per-profile bootstrap records to the machine's install (a symlinked scratch
    ``installs/`` once made Hermes rewrite them to point into the deleted scratch tree).
    """
    snap = {str(p): (stat_sig(p) or {}).get("sha256") for p in real_credential_files()}
    for d in (account_home() / ".hermes" / "hermes-agent" / ".hermes" / "bin", account_home() / ".local" / "bin"):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file():
                    snap[str(p)] = sha256_file(p)
    boot = account_home() / ".hermes" / "installs"
    if boot.is_dir():
        for p in sorted(boot.glob("*/bootstrap/*.json")):
            snap[str(p)] = "present"   # presence only: bootstrap records are rewritten by every real profile launch
    return snap


def fmt_epoch(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def write_synthetic_capture(s):
    """A header capture for the fixture's anthropic row, written by this harness with INVENTED values.

    Nothing in Hermes records ``anthropic-ratelimit-unified-*`` headers today, so the plugin's capture file is
    produced here, in the scratch root only. Times are relative to now so the windows are still open when the
    host reads them. Returns the expected values for the meter checks.
    """
    now = int(time.time())
    observed = now - 90
    fives, sevens = now + 3 * 3600, now + 3 * 86400
    path = s.root / "plugin-data" / "usage-deck" / "anthropic-ratelimit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "captures": [{
        "provider": "anthropic", "credential_id": "syn-anthropic-1", "observed_at": fmt_epoch(observed),
        "headers": {
            "anthropic-ratelimit-unified-5h-utilization": "0.25", "anthropic-ratelimit-unified-5h-reset": str(fives),
            "anthropic-ratelimit-unified-7d-utilization": "0.5", "anthropic-ratelimit-unified-7d-reset": str(sevens),
        }}]}))
    return {"observed_at": fmt_epoch(observed), "session_reset": fmt_epoch(fives), "weekly_reset": fmt_epoch(sevens)}


def machine_checks(R, host, bodies, capture):
    msub = host.api("subscriptions", "view=machine")
    mhealth = host.api("health", "view=machine")
    musage = host.api("usage", "provider=anthropic&view=machine")
    bodies += [msub.text, mhealth.text, musage.text]
    mj = check_envelope_resp(R, "machine view /subscriptions", "subscriptions", msub)
    mh = check_envelope_resp(R, "machine view /health", "health", mhealth)
    mu = check_envelope_resp(R, "machine view /usage?provider=anthropic", "usage", musage)
    mcards = mj["data"]["cards"] if mj else []
    mentries = mh["data"]["entries"] if mh else []
    pv = by_provider(mcards)

    def held(c):
        return [(h["profile"], h["is_global"]) for h in c["holders"]]

    R.check("machine view: data.view == 'machine' and it is the default when no view is given",
            bool(mj) and mj["data"]["view"] == "machine"
            and (host.api("subscriptions", view=None).json or {}).get("data", {}).get("view") == "machine")
    R.check("machine inventory is the union of root + profiles: codex merged to ONE card, openrouter stays two accounts "
            "(root's and work's), anthropic only via profile 'verifier', both endpoints from the launch config",
            sorted(c["provider_id"] for c in mcards) == [
                "anthropic", "custom:gpu-box", "nous", "ollama", "openai-codex", "openrouter", "openrouter"],
            str(sorted(c["provider_id"] for c in mcards)))
    if len(mcards) == 7:
        codex = pv["openai-codex"][0]
        R.check("machine dedupe: root + verifier hold the same JWT account identity under different tokens -> one "
                "card listing both holders (global first); plan stays an unverified claim; still opaque",
                held(codex) == [("global", True), ("verifier", False)]
                and (codex["plan_name"], codex["plan_source"], codex["state"], codex["confidence"]) ==
                ("plus", "unverified_claim", "opaque", "opaque"), json.dumps(codex)[:300])
        R.check("machine view: the two openrouter rows stay separate accounts, each held by exactly its own home",
                sorted(held(c) for c in pv["openrouter"]) == [[("global", True)], [("work", False)]]
                and all((c["state"], c["confidence"]) == ("ready", "exact") for c in pv["openrouter"])
                and len({c["account_id"] for c in pv["openrouter"]}) == 2)
        ant = pv["anthropic"][0]
        meters = {m["id"]: m for m in ant["meters"]}
        R.check("anthropic: exact session_5h / weekly_7d meters from the captured headers (synthetic values), "
                "freshness == capture time, provider-reported resets, never extrapolated",
                held(ant) == [("verifier", False)] and (ant["state"], ant["confidence"], ant["reason"]) == ("ready", "exact", None)
                and sorted(meters) == ["session_5h", "weekly_7d"]
                and (meters["session_5h"]["value"], meters["weekly_7d"]["value"]) == (25.0, 50.0)
                and meters["session_5h"]["resets_at"] == capture["session_reset"]
                and meters["weekly_7d"]["resets_at"] == capture["weekly_reset"]
                and all(m["observed_at"] == capture["observed_at"] and m["source"] == "anthropic_headers"
                        and m["unit"] == "percent" and m["limit"] == 100 and m["remaining"] is None
                        and m["period"]["start"] is None and m["period"]["end"] is None for m in ant["meters"])
                and ant["debug"]["source"] == "anthropic_headers", json.dumps(ant)[:400])
        R.check("machine view: config-derived endpoints are held by the launch home ('global')",
                held(pv["custom:gpu-box"][0]) == [("global", True)] and held(pv["ollama"][0]) == [("global", True)])
        R.check("machine view: card ids are unique (one per merged account)",
                len({c["account_id"] for c in mcards}) == 7)
    R.check("machine view /health: still one row per endpoint (gpu-box healthy, ollama degraded)",
            sorted(e["provider_id"] for e in mentries) == ["custom:gpu-box", "ollama"])
    R.check("machine /usage?provider=anthropic: provider known only via another profile -> attribution unavailable, "
            "empty series (profile totals never cloned)",
            bool(mu) and mu["data"]["series"] == [] and mu["data"]["reason"] == "attribution_unavailable")
    R.check("machine view: source binding holds (exact only with a verified source; self_hosted only with metrics)",
            bool(mcards) and not source_binding_violations(mcards, mentries, []),
            "; ".join(source_binding_violations(mcards, mentries, [])))
    bad = host.api("subscriptions", "view=everything")
    bodies.append(bad.text)
    j = check_envelope_resp(R, "subscriptions?view=everything", "subscriptions", bad, status=400)
    R.check("view=everything: invalid_query", bool(j) and j["error"]["code"] == "invalid_query")


def scenario_main(ctx: Context):
    R = ctx.R
    R.scenario = "main"
    s = ctx.scratch("main")
    ctx.seed_sessions(s)
    capture = write_synthetic_capture(s)
    install_plugin(s, R, ctx.head, enable=False)

    # ---- consent gate: installed but NOT enabled -> the host must not serve the plugin API
    host = Host(s)
    host.start()
    try:
        for ep in ("subscriptions", "usage", "health"):
            r = host.api(ep)
            R.check(f"consent gate: installed-but-not-enabled /{ep} is refused by the host (404, not a plugin response)",
                    r.status == 404 and not (r.json and "meta" in r.json), f"status={r.status} body={r.text[:160]!r}")
    finally:
        host.stop()

    proc = s.cli("plugins", "enable", PLUGIN_NAME)
    R.check("hermes plugins enable usage-deck (consent granted in the scratch config)", proc.returncode == 0,
            (proc.stdout + proc.stderr)[-300:])
    # The secondary profile is a separate consent/profile scope: it needs its own installed + enabled copy.
    install_plugin(s, R, ctx.head, enable=True, home=s.root / "profiles" / "work", label=" [profile work]")
    before_start = s.watched()
    host.start()
    after_start = s.watched()
    R.check("host startup did not rewrite fixture auth/config/.env (plugin enable only touches config.yaml consent)",
            all(before_start[k] == after_start[k] for k in before_start), json.dumps({
                k: (before_start[k] or {}).get("sha256", "")[:8] + "->" + (after_start[k] or {}).get("sha256", "")[:8]
                for k in before_start if before_start[k] != after_start[k]}))
    bodies = []
    try:
        # ---- who is serving? the real host, with this scratch home
        st = host.get("/api/status", token=False)
        R.check("actual host answers /api/status (Hermes dashboard, not a standalone mount)",
                st.status == 200 and isinstance(st.json, dict) and "version" in st.json, st.text[:120])
        index = host.get("/", token=False)
        R.check("the session token the harness uses is the host's real gate token (injected into the SPA)",
                s.token in index.text, "token not found in /")
        listing = host.get("/api/dashboard/plugins", token=False)
        entry = next((p for p in (listing.json or []) if p.get("name") == PLUGIN_NAME), None)
        R.check("host plugin registry lists usage-deck (user source, has_api, tab /usage-deck after:models)",
                bool(entry) and entry.get("has_api") is True and entry.get("source") == "user"
                and entry.get("tab") == {"path": "/usage-deck", "position": "after:models"}, json.dumps(entry)[:300])

        # ---- staged static route: the bundle the host serves is the committed canonical bundle
        committed = {p: sha256_file(REPO / PLUGIN_REL / p) for p in ("dist/index.js", "dist/style.css", "manifest.json")}
        for rel in ("dist/index.js", "dist/style.css"):
            r = host.get(f"/dashboard-plugins/{PLUGIN_NAME}/{rel}", token=False)
            R.check(f"staged route /dashboard-plugins/usage-deck/{rel} serves the committed bytes",
                    r.status == 200 and hashlib.sha256(r.raw).hexdigest() == committed[rel],
                    f"status={r.status}")
        staged_js = REPO / PLUGIN_REL / "dashboard" / "dist" / "index.js"
        R.check("staged dashboard/ bundle == canonical dist/ bundle (committed)",
                sha256_file(staged_js) == committed["dist/index.js"])

        # ---- auth gate
        for ep in ("subscriptions", "usage", "health"):
            r0 = host.api(ep, token=False)
            r1 = host.api(ep, token="not-the-token")
            R.check(f"auth gate: /{ep} without a session token -> 401 from the host",
                    r0.status == 401 and not (r0.json and "meta" in r0.json), f"{r0.status} {r0.text[:80]!r}")
            R.check(f"auth gate: /{ep} with a wrong session token -> 401", r1.status == 401, str(r1.status))
        r = host.get("/api/usage-deck/subscriptions")
        R.check("the original /api/usage-deck/* namespace is NOT exposed (DESIGN.md §0/§3)",
                r.status == 404 or not (r.json and r.json.get("ok")), f"{r.status} {r.text[:80]!r}")

        # ---- happy path through the host
        plug_before = len(ctx.icp.connects)
        gpu_before, ollama_before = len(ctx.gpu.rec.requests), len(ctx.ollama.rec.requests)
        sub = host.api("subscriptions")
        health = host.api("health")
        usage = host.api("usage")
        bodies += [sub.text, health.text, usage.text]
        subs = check_envelope_resp(R, "subscriptions", "subscriptions", sub)
        hlt = check_envelope_resp(R, "health", "health", health)
        use = check_envelope_resp(R, "usage", "usage", usage)
        cards = subs["data"]["cards"] if subs else []
        entries = hlt["data"]["entries"] if hlt else []
        series = use["data"]["series"] if use else []

        R.check("inventory is exactly the fixture's 3 cloud accounts + 2 self-hosted endpoints (no real-pool fallback)",
                sorted(c["provider_id"] for c in cards) ==
                ["custom:gpu-box", "nous", "ollama", "openai-codex", "openrouter"], [c["provider_id"] for c in cards])
        pv = by_provider(cards)
        if len(cards) == 5:
            gpu, nous, oll, codex, orc = (pv[k][0] for k in ("custom:gpu-box", "nous", "ollama", "openai-codex",
                                                              "openrouter"))
            R.check("openrouter: ready / exact, single api_spend meter bound to source openrouter_key (synthetic payload)",
                    (orc["state"], orc["confidence"], orc["reason"], orc["auth_status"]) == ("ready", "exact", None, "available")
                    and len(orc["meters"]) == 1 and {k: orc["meters"][0][k] for k in (
                        "id", "label", "value", "limit", "remaining", "unit", "scope", "confidence", "source")} == {
                        "id": "api_spend", "label": "API credential spend", "value": 1.5, "limit": 5, "remaining": 3.5,
                        "unit": "usd", "scope": "api_credential", "confidence": "exact", "source": "openrouter_key"}
                    and orc["meters"][0]["period"] == {"start": None, "end": None, "label": "unknown"}
                    and orc["meters"][0]["observed_at"] == orc["checked_at"], json.dumps(orc["meters"])[:300])
            for name, c in (("nous", nous), ("openai-codex", codex)):
                R.check(f"{name}: opaque / unsupported, no meters (no verified quota source; credential presence != quota)",
                        (c["state"], c["confidence"], c["reason"], c["meters"], c["kind"], c["auth_status"]) ==
                        ("opaque", "opaque", "unsupported", [], "cloud", "available"), json.dumps(c)[:300])
            R.check("openai-codex plan 'plus' is an unverified local JWT claim (never an entitlement)",
                    (codex["plan_name"], codex["plan_source"]) == ("plus", "unverified_claim")
                    and (nous["plan_name"], nous["plan_source"]) == (None, "none"))
            R.check("custom:gpu-box: self_hosted confidence from successful /metrics only; no token quota meter",
                    (gpu["kind"], gpu["state"], gpu["confidence"], gpu["reason"], gpu["meters"], gpu["auth_status"]) ==
                    ("self_hosted", "opaque", "self_hosted", "unsupported", [], "available"), json.dumps(gpu)[:300])
            R.check("ollama (no credential, no /metrics): opaque confidence even though models are reachable",
                    (oll["kind"], oll["state"], oll["confidence"], oll["reason"], oll["meters"], oll["auth_status"]) ==
                    ("self_hosted", "opaque", "opaque", "metrics_unavailable", [], "missing"), json.dumps(oll)[:300])
            R.check("card ids: id == account_id, endpoint ids tie cards to health rows",
                    {c["endpoint_id"] for c in cards if c["endpoint_id"]} == {h["endpoint_id"] for h in entries}
                    and len({c["account_id"] for c in cards}) == 5)
            R.check("short secret 'el' (nous agent_key) did not alter any canonical key or enum on any card",
                    all(set(c) == CARD_KEYS for c in cards) and not free_text_has(nous, "el"))
        R.check("health: 2 rows; gpu-box healthy/self_hosted with measured cpu+memory, ollama degraded/opaque with nulls",
                len(entries) == 2 and {
                    e["provider_id"]: (e["status"], e["confidence"], e["metrics_available"], e["cpu_percent"],
                                       e["memory_bytes"], e["reason"], e["error"]) for e in entries} == {
                    "custom:gpu-box": ("healthy", "self_hosted", True, 12.5, 1000, None, None),
                    "ollama": ("degraded", "opaque", False, None, None, "metrics_unavailable", None)},
                json.dumps(entries)[:400])
        R.check("envelope meta: partial=false, no errors (fixture is self-consistent)",
                bool(subs) and subs["meta"]["partial"] is False and subs["meta"]["errors"] == []
                and bool(hlt) and hlt["meta"]["partial"] is False)
        R.check("usage: one profile-scoped session_analytics series, estimated, never cloned per account",
                len(series) == 1 and (series[0]["scope"], series[0]["confidence"], series[0]["source"],
                                      series[0]["provider_id"], series[0]["account_id"], series[0]["window_days"],
                                      series[0]["period"]["label"], series[0]["id"]) ==
                ("profile", "estimated", "session_analytics", None, None, 30, "rolling_30d", "profile_session_analytics"),
                json.dumps(series)[:300])
        if series and series[0]["points"]:
            pt = series[0]["points"][-1]
            R.check("usage point carries the seeded counts under input_count/output_count (no *_tokens wire names)",
                    (pt["input_count"], pt["output_count"], pt["sessions"], pt["api_calls"]) == (2000, 500, 2, 5)
                    and abs(pt["estimated_usd"] - 0.0168) < 1e-9 and pt["confidence"] == "estimated"
                    and "tokens" not in json.dumps(series[0]), json.dumps(pt))
        else:
            R.check("usage point carries the seeded counts", False, json.dumps(series)[:200])
        R.check("source binding: exact only with openrouter_key; self_hosted only with metrics; series never exact",
                bool(cards) and not source_binding_violations(cards, entries, series),
                "; ".join(source_binding_violations(cards, entries, series)))
        R.check("no 'estimated'/'exact' label on any subscription card except the openrouter meter",
                {c["confidence"] for c in cards if c["provider_id"] != "openrouter"} <= {"opaque", "self_hosted"})

        for w in ("7", "90"):
            r = host.api("usage", f"window={w}")
            bodies.append(r.text)
            j = check_envelope_resp(R, f"usage?window={w}", "usage", r)
            R.check(f"usage?window={w}: window_days and period label follow the query",
                    bool(j) and j["data"]["window_days"] == int(w)
                    and all(x["window_days"] == int(w) and x["period"]["label"] == f"rolling_{w}d" for x in j["data"]["series"]))
        for pid in ("openrouter", "custom%3Agpu-box"):
            r = host.api("usage", f"provider={pid}")
            bodies.append(r.text)
            j = check_envelope_resp(R, f"usage?provider={pid}", "usage", r)
            R.check(f"usage?provider={pid}: attribution unavailable, empty series (profile totals not cloned)",
                    bool(j) and j["data"]["series"] == [] and j["data"]["reason"] == "attribution_unavailable"
                    and j["data"]["provider_id"] == urllib_unquote(pid))
        r = host.api("usage", "provider=ghost-provider")
        bodies.append(r.text)
        j = check_envelope_resp(R, "usage?provider=<unknown>", "usage", r, status=404)
        R.check("unknown provider -> 404 provider_not_found failure envelope",
                bool(j) and j["ok"] is False and j["error"]["code"] == "provider_not_found" and j["data"] is None)
        bad = [("usage", "window=07"), ("usage", "window=14"), ("usage", "window=7&window=30"), ("usage", "provider="),
               ("usage", "bogus=1"), ("subscriptions", "window=7"), ("health", "provider=x")]
        for ep, q in bad:
            r = host.api(ep, q)
            bodies.append(r.text)
            j = check_envelope_resp(R, f"{ep}?{q}", ep, r, status=400)
            R.check(f"{ep}?{q}: invalid_query", bool(j) and j["error"]["code"] == "invalid_query")

        # ---- profile gate (host-side profile validation / scoping happens before plugin code runs)
        probes = (("current", "profile=current"), ("unknown profile", "profile=does-not-exist"),
                  ("path-traversal profile", "profile=..%2Fescape"), ("NUL in profile", "profile=a%00b"),
                  ("repeated profile", "profile=work&profile=root"), ("empty profile", "profile="))
        observed = {}
        for label, q in probes:
            r = host.api("subscriptions", q)
            bodies.append(r.text)
            observed[label] = {"query": q, "status": r.status, "body": r.text[:160]}
            if label == "current":
                R.check("profile gate: ?profile=current == the launch profile (served, 5 cards)",
                        r.status == 200 and bool(r.json) and len(r.json["data"]["cards"]) == 5, f"{r.status}")
            else:
                inventory = bool(r.json) and r.json.get("ok") is True and bool(r.json["data"]["cards"])
                R.check(f"profile gate: {label} ({q}) is refused (4xx) and never returns another home's inventory",
                        400 <= r.status < 500 and not inventory, f"{r.status} {r.text[:120]!r}")
        # The 'work' profile has its own pool + its own installed/enabled copy of the plugin (set up above).
        r = host.api("subscriptions", "profile=work")
        bodies.append(r.text)
        observed["work (plugin installed+enabled in that profile)"] = {"query": "profile=work", "status": r.status,
                                                                      "body": r.text[:160]}
        j = check_envelope_resp(R, "profile=work /subscriptions (served via the host's profile gate)", "subscriptions", r)
        if j:
            wcards = by_provider(j["data"]["cards"])
            orw = wcards.get("openrouter", [])
            R.check("profile 'work': its own openrouter pool shadows the root's (distinct account id, own token); "
                    "nous / openai-codex fall back to the isolated root pool (per provider, never merged); endpoints are "
                    "per-profile config, and this profile configures none",
                    len(orw) == 1 and orw[0]["account_id"] != by_provider(cards)["openrouter"][0]["account_id"]
                    and sorted(wcards) == ["nous", "openai-codex", "openrouter"]
                    and all(len(v) == 1 for v in wcards.values()), json.dumps(sorted(wcards))[:200])
        R.record = observed

        # ---- machine view (DESIGN.md §10): global + every profile, deduped by JWT account identity
        machine_checks(R, host, bodies, capture)

        # ---- egress: what did the plugin talk to?
        new_connects = ctx.icp.connects[plug_before:]
        R.check("plugin egress: only CONNECT openrouter.ai:443 (no other origin, no refresh/billing endpoints)",
                {t for t, _ in new_connects} <= {"openrouter.ai:443"} and any(t == "openrouter.ai:443" for t, _ in new_connects),
                str(new_connects))
        or_reqs = ctx.icp.rec.requests
        root_token = ctx.fixture_auth["credential_pool"]["openrouter"][0]["access_token"]
        work_token = ctx.fixture_work_auth["credential_pool"]["openrouter"][0]["access_token"]
        R.check("openrouter.ai saw only GET /api/v1/key, with the root pool's bearer for the root profile and the "
                "work pool's bearer for profile=work (profile-first pool, never combined)",
                bool(or_reqs) and all((q["method"], q["path"]) == ("GET", "/api/v1/key") for q in or_reqs)
                and {q["authorization"] for q in or_reqs} == {"Bearer " + root_token, "Bearer " + work_token},
                str([(q["method"], q["path"]) for q in or_reqs]))
        for name, srv, base in (("gpu-box", ctx.gpu, gpu_before), ("ollama", ctx.ollama, ollama_before)):
            reqs = srv.rec.requests[base:]
            R.check(f"{name}: GET-only, only the configured origin's /metrics, /v1/models, /api/tags",
                    bool(reqs) and all(q["method"] == "GET" and q["path"] in ("/metrics", "/v1/models", "/api/tags")
                                       for q in reqs), str(reqs)[:200])
            R.check(f"{name}: inference credential never sent to /metrics",
                    all(q["authorization"] is None for q in reqs if q["path"] == "/metrics"), str(reqs)[:200])
        R.check("no refresh/billing/inference traffic to the fake endpoints (no POST/PUT/DELETE anywhere)",
                all(q["method"] == "GET" for srv in (ctx.gpu, ctx.ollama) for q in srv.rec.requests))
    finally:
        host.stop()

    # ---- secrets + immutability + real-home tripwire
    log = host.log_text()
    scan_texts(R, "responses (subscriptions/usage/health/errors/profile probes)", bodies, ctx.long_secrets
               | {"UDSYN-upstream-echo-91ac4e"})
    scan_texts(R, "dashboard process log", [log], ctx.long_secrets)
    final = s.watched()
    R.check("config/auth immutability: auth.json, config.yaml, .env, profile files unchanged (hash, mtime, inode) "
            "across all plugin requests", final == after_start,
            json.dumps({k: (after_start[k], final[k]) for k in final if final[k] != after_start[k]})[:300])
    R.check("plugin requests created no *.corrupt backup or stray lock beside auth stores",
            s.corrupt_backups() == [], str(s.corrupt_backups()))
    return s


def urllib_unquote(v):
    import urllib.parse
    return urllib.parse.unquote(v)


def scenario_variant(ctx: Context, name, mutate, checks, seed=False):
    R = ctx.R
    R.scenario = name
    s = ctx.scratch(name, mutate)
    if seed:
        ctx.seed_sessions(s)
    install_plugin(s, R, ctx.head, enable=True)
    corrupt_pre = s.corrupt_backups()
    host = Host(s)
    host.start()
    after_start = s.watched()
    corrupt_started = s.corrupt_backups()
    if corrupt_started != corrupt_pre:
        R.record[f"{name}: host-originated at startup (before any plugin request)"] = {
            "new_backups": sorted(set(corrupt_started) - set(corrupt_pre))}
    bodies = []
    try:
        checks(host, bodies)
    finally:
        host.stop()
    end = s.watched()
    R.check("auth/config files unchanged by plugin requests (hash, mtime, inode)", end == after_start,
            json.dumps({k: (after_start[k], end[k]) for k in end if end[k] != after_start[k]})[:300])
    R.check("plugin requests created no *.corrupt backup (the plugin never uses the host auth-store loader that "
            "writes one)", s.corrupt_backups() == corrupt_started,
            f"at-startup={corrupt_started} after-requests={s.corrupt_backups()}")
    scan_texts(R, "responses", bodies, ctx.long_secrets | {"UDSYN-mystery-token-2a6d1b9c04f7"})
    scan_texts(R, "dashboard log", [host.log_text()], ctx.long_secrets | {"UDSYN-mystery-token-2a6d1b9c04f7"})
    for body in bodies:
        R.check("no scratch path / file content in any response", str(s.root) not in body and "credential_pool" not in body,
                body[:120])


def _set_auth(s, content):
    (s.root / "auth.json").write_text(content)


def _minimal_config(s):
    (s.root / "config.yaml").write_text("nous:\n  keepalive_interval_seconds: 0\n")
    shutil.rmtree(s.root / "profiles", ignore_errors=True)


def variant_empty(s):
    _minimal_config(s)
    (s.root / "auth.json").unlink()


def variant_malformed_json(s):
    _minimal_config(s)
    _set_auth(s, '{"credential_pool": {"nous": [ {"access_token": "UDSYN-broken-json-token-77c1e0"')


def variant_malformed_shape(s):
    _minimal_config(s)
    _set_auth(s, '["not", "an", "object"]')


def variant_unknown_provider(s):
    _minimal_config(s)
    _set_auth(s, json.dumps({"version": 1, "credential_pool": {"mystery-cloud": [
        {"id": "m1", "access_token": "UDSYN-mystery-token-2a6d1b9c04f7"}]}}))


def checks_empty(R):
    def run(host, bodies):
        for ep, key, empty in (("subscriptions", "cards", "empty"), ("health", "entries", None)):
            r = host.api(ep)
            bodies.append(r.text)
            j = check_envelope_resp(R, f"empty home /{ep}", ep, r)
            R.check(f"empty home /{ep}: 200 with an empty list (not 404/503, DESIGN.md §3)",
                    bool(j) and j["data"][key] == [] and (empty is None or j["data"]["inventory_state"] == empty))
        r = host.api("usage")
        bodies.append(r.text)
        j = check_envelope_resp(R, "empty home /usage (no session store)", "usage", r, status=503)
        R.check("empty home /usage: analytics_unavailable 503 only for /usage; inventory/health stay usable",
                bool(j) and j["error"]["code"] == "analytics_unavailable" and j["ok"] is False)
    return run


def checks_malformed(R):
    def run(host, bodies):
        for ep in ("subscriptions", "health"):
            r = host.api(ep)
            bodies.append(r.text)
            j = check_envelope_resp(R, f"malformed auth store /{ep}", ep, r, status=503)
            R.check(f"malformed auth store /{ep}: 503 inventory_unavailable, fixed safe message, no content/path",
                    bool(j) and j["error"]["code"] == "inventory_unavailable" and j["data"] is None
                    and j["error"]["message"] == "The provider inventory could not be read.")
        r = host.api("usage", "provider=nous")
        bodies.append(r.text)
        R.check("malformed auth store: filtered /usage fails closed (503, never a guessed 200/404)",
                r.status == 503 and bool(r.json) and r.json["error"]["code"] == "inventory_unavailable",
                f"{r.status} {r.text[:120]!r}")
        r = host.get("/api/status", token=False)
        R.check("host stays healthy while the plugin reports an unreadable inventory", r.status == 200)
    return run


def checks_unknown(R):
    def run(host, bodies):
        r = host.api("subscriptions")
        bodies.append(r.text)
        j = check_envelope_resp(R, "unknown provider /subscriptions", "subscriptions", r)
        cards = j["data"]["cards"] if j else []
        R.check("unknown cloud provider id is a normal opaque card via the SessionAnalytics fallback (not an error)",
                len(cards) == 1 and (cards[0]["provider_id"], cards[0]["kind"], cards[0]["state"], cards[0]["confidence"],
                                     cards[0]["reason"], cards[0]["meters"], cards[0]["error"]) ==
                ("mystery-cloud", "cloud", "opaque", "opaque", "unsupported", [], None)
                and cards[0]["debug"]["adapter"] == "SessionAnalyticsAdapter"
                and cards[0]["provider_label"] in ("Provider", "mystery-cloud"),
                json.dumps(cards)[:300])
        R.check("unknown provider: partial=false (unsupported is not an error)",
                bool(j) and j["meta"]["partial"] is False)
        r = host.api("health")
        bodies.append(r.text)
        j = check_envelope_resp(R, "unknown provider /health", "health", r)
        R.check("unknown provider: no health row for a non-local adapter", bool(j) and j["data"]["entries"] == [])
        r = host.api("usage", "provider=mystery-cloud")
        bodies.append(r.text)
        j = check_envelope_resp(R, "usage?provider=<discovered unknown adapter>", "usage", r)
        R.check("discovered-but-unknown adapter filter is 200 attribution_unavailable (NOT 404)",
                bool(j) and j["data"]["series"] == [] and j["data"]["reason"] == "attribution_unavailable")
        r = host.api("usage", "provider=ghost")
        bodies.append(r.text)
        check_envelope_resp(R, "usage?provider=<not in inventory>", "usage", r, status=404)
        r = host.api("usage")
        bodies.append(r.text)
        j = check_envelope_resp(R, "unknown provider /usage (profile series)", "usage", r)
        R.check("profile series is estimated/session_analytics and carries no provider attribution",
                bool(j) and len(j["data"]["series"]) == 1 and j["data"]["series"][0]["confidence"] == "estimated"
                and j["data"]["series"][0]["provider_id"] is None)
    return run


# --------------------------------------------------------------------------- offline self-check

def self_check(R):
    R.scenario = "self-check"
    auth = json.loads((FIXTURE / "auth.json").read_text())
    pool = auth["credential_pool"]
    R.check("fixture: 3 synthetic cloud accounts (nous, openrouter, openai-codex)",
            sorted(pool) == ["nous", "openai-codex", "openrouter"] and all(len(v) == 1 for v in pool.values()))
    cfg = (FIXTURE / "config.yaml.template").read_text()
    R.check("fixture: 2 self-hosted endpoints (primary ollama + custom gpu-box) with loopback placeholders",
            "@@GPU_BASE_URL@@" in cfg and "@@OLLAMA_BASE_URL@@" in cfg and "provider: ollama" in cfg
            and "name: gpu-box" in cfg and "keepalive_interval_seconds: 0" in cfg)
    secrets_all = collect_seeded_secrets(auth) | config_secrets(cfg)
    synthetic = ("UDSYN-", "sk-or-v1-udsyn", "eyJ", "el")
    R.check("fixture: every seeded secret is visibly synthetic", all(s.startswith(synthetic) for s in secrets_all),
            sorted(s[:12] for s in secrets_all if not s.startswith(synthetic)))
    jwt = pool["openai-codex"][0]["access_token"]
    payload = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
    ident = payload.get("https://api.openai.com/auth", {})
    R.check("fixture: codex JWT is unsigned/synthetic: plan claim 'plus' plus invented udsyn-* account identity claims",
            payload.get("iss") == "synthetic.invalid" and payload.get("plan_type") == "plus"
            and jwt.split(".")[2].startswith("udsyn")
            and set(ident) == {"chatgpt_account_id", "chatgpt_user_id", "chatgpt_plan_type"}
            and all(str(v).startswith("udsyn-") or k == "chatgpt_plan_type" for k, v in ident.items()))
    vauth = json.loads((FIXTURE / "profiles" / "verifier" / "auth.json").read_text())
    vjwt = vauth["credential_pool"]["openai-codex"][0]["access_token"]
    vclaims = json.loads(base64.urlsafe_b64decode(vjwt.split(".")[1] + "=="))["https://api.openai.com/auth"]
    R.check("fixture: profile 'verifier' holds the SAME synthetic codex account identity under a different token "
            "(machine-view merge) and an anthropic row only it has",
            vclaims == ident and vjwt != jwt and sorted(vauth["credential_pool"]) == ["anthropic", "openai-codex"])
    R.check("fixture: a short (2-char) secret and a secret copied under a generic field exist",
            "el" in secrets_all and pool["nous"][0]["label"] == pool["nous"][0]["access_token"])
    blob = json.dumps(auth) + cfg
    R.check("fixture: no URL userinfo, no real hostnames (only loopback placeholders)",
            not re.search(r"://[^/\s]*@", blob) and not re.search(r"https?://(?!127\.0\.0\.1)", cfg.replace("@@", "")))
    # validators accept a contract-exact sample and reject drift
    now = "2026-11-06T12:00:00Z"
    card = {"id": "acct_" + "0" * 24, "provider_id": "nous", "account_id": "acct_" + "0" * 24, "provider_label": "Nous Portal",
            "account_label": "Account 1", "kind": "cloud", "plan_name": None, "plan_source": "none",
            "auth_status": "available", "endpoint_id": None, "state": "opaque", "confidence": "opaque",
            "reason": "unsupported", "meters": [], "checked_at": now, "stale": False, "error": None,
            "debug": {"adapter": "NousAdapter", "capabilities": ["session_analytics"], "source": "local_inventory",
                      "sanitized": True}, "holders": [{"profile": "global", "is_global": True}]}
    env = {"ok": True, "data": {"cards": [card], "inventory_state": "detected", "view": "machine"}, "error": None,
           "meta": {"schema_version": 1, "request_id": "0a1b2c3d-0000-4000-8000-0123456789ab", "generated_at": now,
                    "partial": False, "errors": []}}
    R.check("validator: accepts a contract-exact subscriptions envelope", validate_envelope("subscriptions", env) == [])
    mutations = {
        "renamed key (the reported defect: label -> lab[REDACTED])": lambda e: e["data"]["cards"][0].__setitem__(
            "provider_lab[REDACTED]", e["data"]["cards"][0].pop("provider_label")),
        "dropped field": lambda e: e["data"]["cards"][0].pop("reason"),
        "extra field": lambda e: e["data"]["cards"][0].update(raw={}),
        "invalid enum": lambda e: e["data"]["cards"][0].update(confidence="opa[REDACTED]e"),
        "garbled timestamp": lambda e: e["data"]["cards"][0].update(checked_at="[REDACTED]-11-06T12:00:00Z"),
        "debug.sanitized false": lambda e: e["data"]["cards"][0]["debug"].update(sanitized=False),
        "meta.partial mismatch": lambda e: e["meta"].update(partial=True),
        "holders dropped": lambda e: e["data"]["cards"][0].pop("holders"),
        "holder without is_global": lambda e: e["data"]["cards"][0]["holders"].append({"profile": "x"}),
        "data.view dropped": lambda e: e["data"].pop("view"),
        "invalid data.view": lambda e: e["data"].update(view="all"),
    }
    for label, mutate in mutations.items():
        bad = copy.deepcopy(env)
        mutate(bad)
        R.check(f"validator rejects: {label}", validate_envelope("subscriptions", bad) != [])
    meter = {"id": "api_spend", "label": "x", "value": 1.0, "limit": None, "remaining": None, "unit": "usd",
             "scope": "api_credential", "period": {"start": None, "end": None, "label": "unknown"},
             "confidence": "exact", "source": "none", "observed_at": now, "resets_at": None}
    exact_wrong = dict(card, provider_id="nous", meters=[meter], confidence="exact", state="ready")
    R.check("source-binding validator rejects an exact meter whose source is not openrouter_key",
            bool(source_binding_violations([exact_wrong], [], [])))
    R.check("source-binding validator rejects a self_hosted card without successful metrics",
            bool(source_binding_violations([dict(card, kind="self_hosted", confidence="self_hosted", endpoint_id="ep_" + "1" * 24)],
                                           [], [])))


# --------------------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hermes", default=shutil.which("hermes") or "hermes", help="hermes CLI to drive")
    ap.add_argument("--workdir", help="parent directory for the managed scratch tree (default: $HERMES_KANBAN_WORKSPACE or $TMPDIR)")
    ap.add_argument("--runtime-root", default=str(account_home() / ".hermes"),
                    help="where the machine's Hermes runtime (installs/, tools/) lives; linked read-only into each scratch root")
    ap.add_argument("--hermes-agent", help="hermes-agent checkout (default: located from hermes_constants / runtime root)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch tree (logs, report.json)")
    ap.add_argument("--allow-dirty", action="store_true", help="skip the committed-HEAD check (installed bytes still compared to HEAD)")
    ap.add_argument("--self-check", action="store_true", help="offline: validate fixture + validators only, no host")
    ap.add_argument("--only", action="append", choices=("isolation", "main", "empty", "malformed", "unknown"),
                    help="run only these scenarios")
    ap.add_argument("--json-out", help="write the machine-readable report here")
    args = ap.parse_args(argv)
    if not args.hermes_agent:
        import importlib.util
        spec = importlib.util.find_spec("hermes_constants")
        args.hermes_agent = str(Path(spec.origin).parent) if spec and spec.origin else str(Path(args.runtime_root) / "hermes-agent")
    return args


def main(argv=None):
    args = parse_args(argv)
    R = Results()
    if args.self_check:
        self_check(R)
        print(f"\nself-check: {len(R.items) - len(R.failed)}/{len(R.items)} passed")
        return 1 if R.failed else 0

    parent = args.workdir or os.environ.get("HERMES_KANBAN_WORKSPACE") or os.environ.get("TMPDIR")
    if not parent:
        print("refusing to guess a temp location: set TMPDIR / HERMES_KANBAN_WORKSPACE or pass --workdir", file=sys.stderr)
        return 2
    for tool in (args.hermes, "git", "openssl"):
        if not shutil.which(tool):
            print(f"missing required tool: {tool}", file=sys.stderr)
            return 2
    head, dirty = committed_head()
    if dirty and not args.allow_dirty:
        print("tracked files differ from HEAD; the harness installs the COMMITTED plugin. Commit or pass --allow-dirty:\n  "
              + "\n  ".join(dirty), file=sys.stderr)
        return 2
    workdir = Path(tempfile.mkdtemp(prefix="usage-deck-acceptance-", dir=parent))
    print(f"UsageDeck host acceptance @ {head}\n  workdir: {workdir}\n  hermes:  {args.hermes}", flush=True)
    real_before = snapshot_real()
    ctx = Context(args, workdir, R)
    wanted = set(args.only or ("isolation", "main", "empty", "malformed", "unknown"))

    def guarded(label, fn, *a, **kw):
        """A crash inside one scenario is a FAILED check, never a reason to skip the remaining scenarios."""
        try:
            fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001 - report, never hide
            R.scenario = label
            R.check(f"scenario aborted: {type(exc).__name__}", False, str(exc)[:500])

    try:
        if "isolation" in wanted:
            guarded("isolation", scenario_isolation, ctx)
        if "main" in wanted:
            guarded("main", scenario_main, ctx)
        if "empty" in wanted:
            guarded("empty-home", scenario_variant, ctx, "empty-home", variant_empty, checks_empty(R))
        if "malformed" in wanted:
            guarded("malformed-auth-json", scenario_variant, ctx, "malformed-auth-json", variant_malformed_json,
                    checks_malformed(R))
            guarded("malformed-auth-shape", scenario_variant, ctx, "malformed-auth-shape", variant_malformed_shape,
                    checks_malformed(R))
        if "unknown" in wanted:
            guarded("unknown-provider", scenario_variant, ctx, "unknown-provider", variant_unknown_provider,
                    checks_unknown(R), seed=True)
    except Exception as exc:  # noqa: BLE001 - report, never hide
        R.scenario = "harness"
        R.check(f"harness aborted: {type(exc).__name__}", False, str(exc)[:500])
    finally:
        ctx.close()
        R.scenario = "real-home tripwire"
        after_real = snapshot_real()
        R.check("real ~/.hermes credential/config files, the shared hermes launchers and bootstrap records are unchanged "
                "after the run (a concurrently running real Hermes could also write auth/config)",
                after_real == real_before,
                json.dumps(sorted(k for k in set(after_real) | set(real_before) if after_real.get(k) != real_before.get(k))))
        passed = len(R.items) - len(R.failed)
        report = {"head": head, "workdir": str(workdir), "passed": passed, "total": len(R.items),
                  "host_gate_observations": R.record, "checks": R.items,
                  "scope": "synthetic fixture only; no live-pool, real-credential or real-browser claim"}
        out = Path(args.json_out) if args.json_out else workdir / "report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
        print(f"\nRESULT: {passed}/{len(R.items)} checks passed @ {head}\nreport: {out}", flush=True)
        if R.failed:
            print("FAILED:\n  " + "\n  ".join(f"[{i['scenario']}] {i['check']} -- {i['detail'][:160]}" for i in R.failed))
        if not args.keep and not R.failed:
            shutil.rmtree(workdir, ignore_errors=True)
        elif R.failed:
            print(f"(work dir kept for inspection: {workdir})")
    return 1 if R.failed else 0


if __name__ == "__main__":
    sys.exit(main())
