"""Shared test doubles. Stdlib only (unittest); no network, no real HERMES_HOME."""

import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from plugins.usage_deck.adapters.base import (
    AdapterContext,
    AnalyticsDay,
    AnalyticsUnavailable,
    SecretValue,
)
from plugins.usage_deck.adapters.transport import HttpResponse, TransportError
from plugins.usage_deck.models.identity import make_endpoint_id, parse_endpoint

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)

# Unique sentinels: any occurrence in output means a leak.
SENTINEL_TOKEN = "sntl-tok-9f3a7c21d4b8e650"
SENTINEL_KEY = "sntl-key-51be0c9d77a2f483"
SENTINEL_SECRET = "sntl-sec-c04e8a1b2d6f9375"
SENTINEL_REFRESH = "sntl-ref-7d2e19a05c8b4f36"
ALL_SENTINELS = (SENTINEL_TOKEN, SENTINEL_KEY, SENTINEL_SECRET, SENTINEL_REFRESH)


class FakeTransport:
    """Maps URL -> HttpResponse | TransportError | callable. Records every request."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def get(self, url, headers=None, timeout=2.0, max_bytes=1048576):
        self.requests.append((url, dict(headers or {}), timeout))
        outcome = self.routes.get(url)
        if outcome is None:
            raise TransportError(TransportError.CONNECTION)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def urls(self):
        return [r[0] for r in self.requests]


def json_response(payload, status=200, latency=12.5):
    return HttpResponse(status, json.dumps(payload).encode(), latency, "application/json")


def raw_response(body, status=200, latency=5.0):
    data = body.encode() if isinstance(body, str) else body
    return HttpResponse(status, data, latency, "text/plain")


class FakeAnalytics:
    def __init__(self, rows=None, fail=False, available=True):
        self.rows = tuple(rows or ())
        self.fail = fail
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def read_usage(self, days):
        self.calls.append(days)
        if self.fail:
            raise AnalyticsUnavailable("down")
        return self.rows


def day_row(day, i=100, o=50, usd=0.25, sessions=2, calls=7):
    return AnalyticsDay(day, i, o, usd, sessions, calls)


def make_ctx(provider_id="openrouter", kind="cloud", credential=SENTINEL_KEY, endpoint=None,
             analytics=None, transport=None, account_id="acct_test", secrets=None, **extra):
    ep = parse_endpoint(endpoint) if endpoint else None
    return AdapterContext(
        provider_id=provider_id,
        account_id=account_id,
        provider_label="Provider",
        account_label="Account 1",
        kind=kind,
        analytics=analytics or FakeAnalytics(),
        transport=transport or FakeTransport(),
        clock=lambda: NOW,
        endpoint=ep,
        endpoint_id=make_endpoint_id(provider_id, ep.normalized) if ep else None,
        credential=SecretValue(credential) if credential else None,
        auth_status="available" if credential else "missing",
        secrets=frozenset(secrets or ([credential] if credential else [])),
        **extra,
    )


def make_jwt(claims):
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig{SENTINEL_TOKEN[-6:]}"


class ScratchHome:
    """Temporary profile home (+ optional global root); never touches the real one."""

    def __init__(self, base=None):
        self._tmp = tempfile.TemporaryDirectory(prefix="usage-deck-test-", dir=base)
        self.root = Path(self._tmp.name)
        self.home = self.root / "profiles" / "work"
        self.home.mkdir(parents=True)

    def write_auth(self, data, where="profile"):
        target = (self.home if where == "profile" else self.root) / "auth.json"
        target.write_text(data if isinstance(data, str) else json.dumps(data))
        return target

    def write_profile(self, name, data):
        """Write ``<root>/profiles/<name>/auth.json`` (another profile of the same machine)."""
        directory = self.root / "profiles" / name
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "auth.json"
        target.write_text(data if isinstance(data, str) else json.dumps(data))
        return target

    def cleanup(self):
        self._tmp.cleanup()


def scan_for(obj, needles):
    """Return the needles found anywhere in the JSON text of ``obj``."""
    text = json.dumps(obj)
    return [n for n in needles if n in text]


class PassthroughResolver:
    """Hermetic stand-in for core's credential machinery: hands back the stored token untouched.

    The production resolver drives Hermes' real credential pool (and would refresh/write the real auth
    store), so the test package installs this one as the service default (``tests/__init__.py``).
    """

    def __init__(self):
        self.calls = []

    def resolve(self, row, now):
        from plugins.usage_deck.adapters.credentials import OK, TokenResolution

        self.calls.append(row)
        return TokenResolution(OK, row.token)
