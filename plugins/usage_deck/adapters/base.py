"""Adapter protocol, private context and shared helpers (DESIGN.md §1.2)."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, FrozenSet, Optional, Protocol, Tuple

from ..captures import CaptureStore, CredentialRef
from ..models.dto import (
    DeckError,
    HealthStripEntry,
    ProbeResult,
    UsageResult,
    fmt_ts,
    make_error,
)
from ..models.identity import Endpoint
from .transport import HttpResponse, HttpTransport, TransportError


class SecretValue:
    """Holds one credential; never reveals itself via repr/str/pickle/json."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<SecretValue [REDACTED]>"

    __str__ = __repr__

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("SecretValue cannot be serialized")

    def __setattr__(self, name, value):  # immutable
        raise AttributeError("immutable")

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class AnalyticsDay:
    """One normalized analytics row. Wire names are mapped to ``*_count`` by the reader."""

    day: str
    input_count: Optional[int]
    output_count: Optional[int]
    estimated_usd: Optional[float]
    sessions: Optional[int]
    api_calls: Optional[int]


class AnalyticsUnavailable(Exception):
    """Session analytics could not be read (missing/corrupt/import failure)."""


class AnalyticsReader(Protocol):
    def available(self) -> bool: ...

    def read_usage(self, days: int) -> Tuple[AnalyticsDay, ...]: ...


class EndpointCache:
    """Per-refresh memo so accounts sharing one endpoint cause one set of requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._values: Dict[str, object] = {}

    def get_or_compute(self, key: str, compute: Callable[[], object]) -> object:
        with self._lock:
            key_lock = self._locks.setdefault(key, threading.Lock())
        with key_lock:
            if key not in self._values:
                self._values[key] = compute()
            return self._values[key]


@dataclass(frozen=True)
class AdapterContext:
    """Immutable private input for one adapter. Never serialized (repr hidden)."""

    provider_id: str
    account_id: str
    provider_label: str
    account_label: str
    kind: str  # cloud | self_hosted
    analytics: AnalyticsReader
    transport: HttpTransport
    clock: Callable[[], datetime]
    endpoint: Optional[Endpoint] = None
    endpoint_id: Optional[str] = None
    credential: Optional[SecretValue] = None
    auth_status: str = "missing"
    plan_name: Optional[str] = None
    plan_source: str = "none"
    secrets: FrozenSet[str] = frozenset()
    health_credential: Optional[SecretValue] = None  # separately configured metrics credential only
    endpoint_cache: Optional[EndpointCache] = None
    #: Scoped ``(is_global, profile, public row id)`` capture references (never secret, never a DTO key).
    credential_refs: Tuple[CredentialRef, ...] = ()
    captures: Optional[CaptureStore] = None  # read-only header captures, never a network call
    #: False for machine-only inventory (no member belongs to the selected profile's effective
    #: credentials/config): adapters must make NO outbound request (DESIGN.md §12.5).
    live_source: bool = True

    def __repr__(self) -> str:
        return f"AdapterContext(provider_id={self.provider_id!r}, account_id={self.account_id!r})"

    def now(self) -> datetime:
        return self.clock()

    def now_ts(self) -> str:
        return fmt_ts(self.clock())


class ProviderAdapter(Protocol):
    def probe(self) -> ProbeResult: ...

    def get_usage(self, window: int) -> UsageResult: ...

    def get_health(self) -> Optional[HealthStripEntry]: ...

    def get_confidence(self) -> str: ...


def error_for_status(status: int, provider_id: str, account_id: str) -> Optional[DeckError]:
    """Map an HTTP status to a safe DeckError (None for 2xx)."""
    if 200 <= status < 300:
        return None
    if status == 401:
        code = "auth_required"
    elif status in (403, 404):
        # 404 on a documented endpoint means the credential/route is not entitled here.
        code = "permission_denied"
    elif status == 429 or status >= 500:
        code = "upstream_unavailable"
    else:
        code = "malformed_response"
    return make_error(code, provider_id, account_id)


def error_for_transport(exc: TransportError, provider_id: str, account_id: str) -> DeckError:
    if exc.kind == TransportError.TIMEOUT:
        code = "timeout"
    elif exc.kind in (TransportError.CONNECTION, TransportError.TLS):
        code = "upstream_unavailable"
    elif exc.kind in (TransportError.REDIRECT, TransportError.OVERSIZE):
        code = "malformed_response"
    else:
        code = "internal_error"
    return make_error(code, provider_id, account_id)


def parse_json_body(response: HttpResponse):
    """Parse a bounded JSON body; reject NaN/Infinity. Raises ValueError."""

    def _reject(_name):
        raise ValueError("non-finite constant")

    return json.loads(response.body.decode("utf-8"), parse_constant=_reject)


def finite_number(value) -> Optional[float]:
    """Return a finite int/float (bool excluded) else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class BaseAdapter:
    """Shared snapshot behavior: probe once, reuse for usage/health/confidence."""

    adapter_name = "base"
    source_label = "none"

    def __init__(self, ctx: AdapterContext):
        self.ctx = ctx
        self._probe: Optional[ProbeResult] = None
        self._usage: Dict[int, UsageResult] = {}
        self._confidence = "opaque"

    # -- hooks -----------------------------------------------------------
    def _do_probe(self) -> ProbeResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def _do_usage(self, window: int) -> UsageResult:
        return UsageResult(confidence="opaque", reason="unsupported")

    # -- protocol --------------------------------------------------------
    def probe(self) -> ProbeResult:
        if self._probe is None:
            self._probe = self._do_probe()
        return self._probe

    def get_usage(self, window: int) -> UsageResult:
        if window not in (7, 30, 90):
            raise ValueError("window must be 7, 30 or 90")
        if window not in self._usage:
            self.probe()
            result = self._do_usage(window)
            self._confidence = result.confidence
            self._usage[window] = result
        return self._usage[window]

    def get_health(self) -> Optional[HealthStripEntry]:
        return None

    def get_confidence(self) -> str:
        if not self._usage:
            self.get_usage(30)  # callable independently: reuse/produce the snapshot
        return self._confidence

    # -- helpers ---------------------------------------------------------
    def _probe_result(self, supported, reachable=None, capabilities=(), reason=None, error=None) -> ProbeResult:
        return ProbeResult(supported, reachable, self.ctx.now_ts(), tuple(capabilities), reason, error)

    def _capabilities(self, *extra: str) -> Tuple[str, ...]:
        caps = list(extra)
        try:
            if self.ctx.analytics.available():
                caps.append("session_analytics")
        except Exception:  # noqa: BLE001 - a broken reader is "unavailable", not a crash
            pass
        return tuple(caps)
