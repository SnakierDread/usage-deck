"""Anthropic OAuth token resolution through Hermes core (DESIGN.md §9.1.1 credential lifecycle, §9.1.2).

The usage pull must never send a token it read straight off disk: an access token expires (``expires_at_ms``)
and, on a topology where no agent talks to Anthropic directly (builder profiles routed through a passthrough,
pool ``request_count == 0``), NOTHING else would ever rotate it. The meter therefore resolves its bearer through
the same credential pool the ``hermes auth refresh`` command drives:

* the row is looked up in ``agent.credential_pool.load_pool("anthropic")`` (core's view, which already merges
  peer rotations and singleton files);
* an expired / expiring row (``expires_at_ms`` within core's 2-minute skew) is refreshed with
  ``CredentialPool.try_refresh_matching(credential_id=...)``, the exact call ``hermes auth refresh`` makes, so the
  rotated pair is persisted, locked and written back by core. This module never implements the OAuth refresh
  grant, never writes ``auth.json`` and never touches a token file;
* a refresh that fails terminally (core marks the row DEAD / drops it, or the row cannot be refreshed at all) is
  a ``reauth`` verdict, a transient failure is ``unavailable`` (retry later, never "rejected").

Only a row the SELECTED profile owns is ever refreshed. A sibling profile's credential (the §13 cross-profile
exemption) is read-only: a still-valid stored token is used as is, an expired one is reported ``unavailable``
instead of being sent (it could only earn a misleading 401) or rotated behind its owner's back.

Hermes is imported lazily and only here; the module has no import-time dependency on the host.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Protocol

from .base import SecretValue

log = logging.getLogger("usage_deck.credentials")

OK = "ok"
REAUTH = "reauth"  # the stored login is dead: only `hermes auth add anthropic` repairs it
UNAVAILABLE = "unavailable"  # cannot resolve a bearer right now (transient / not permitted): retry later

#: Core refreshes an expiry-stamped row this long before ``expires_at_ms`` (``agent.credential_pool_plugin``
#: ``_EXPIRY_SKEW_MS``); mirrored so the meter and the agents agree on "expiring".
EXPIRY_SKEW_MS = 120_000
#: After a transient refresh failure the row is left alone this long (one snapshot per 30 s must not hammer the
#: token endpoint or keep benching the row in the shared pool).
FAILURE_BACKOFF_SECONDS = 60.0
_STATUS_DEAD = "dead"  # agent.credential_pool.STATUS_DEAD
_PROVIDER = "anthropic"


@dataclass(frozen=True)
class StoredRow:
    """What discovery saw: the bearer is carried opaquely and revealed only by a resolver."""

    token: SecretValue
    expires_at: float  # seconds since the epoch, 0 = unknown
    row_id: Optional[str]  # public pool row id (the key ``load_pool`` rows carry)
    may_refresh: bool  # the row belongs to the selected profile's effective pool


@dataclass(frozen=True)
class TokenResolution:
    status: str
    token: Optional[SecretValue] = None
    refreshed: bool = False


class TokenResolver(Protocol):
    def resolve(self, row: StoredRow, now: datetime) -> TokenResolution: ...


def _expiring(expires_at_seconds: float, now: datetime) -> bool:
    if not expires_at_seconds or expires_at_seconds <= 0:
        return False  # unknown expiry: core treats such a row as not expiring too
    return expires_at_seconds * 1000.0 <= now.timestamp() * 1000.0 + EXPIRY_SKEW_MS


def _core_load_pool() -> Any:
    from agent.credential_pool import load_pool  # type: ignore  # host module, lazy

    return load_pool(_PROVIDER)


class CoreTokenResolver:
    """Resolve the Anthropic bearer through Hermes' credential pool (see the module docstring)."""

    def __init__(self, pool_loader: Optional[Callable[[], Any]] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 backoff_seconds: float = FAILURE_BACKOFF_SECONDS) -> None:
        self._load = pool_loader or _core_load_pool
        self._monotonic = monotonic
        self._backoff = backoff_seconds
        self._lock = threading.Lock()
        self._failed_at: Dict[str, float] = {}

    # -- public ----------------------------------------------------------------------------------
    def resolve(self, row: StoredRow, now: datetime) -> TokenResolution:
        if not row.may_refresh or not row.row_id:
            return self._stored(row, now)
        if self._backed_off(row.row_id):
            return TokenResolution(UNAVAILABLE)
        try:
            # Core reads (and may heal) the store itself; discovery has already read auth.json with stdlib json,
            # so a corrupt store never reaches this point.
            pool = self._load()
            entry = self._find(pool, row.row_id)
        except Exception as exc:  # noqa: BLE001 - host missing/changed: degrade, never crash the card
            log.warning("usage_deck: core credential pool unavailable (%s)", type(exc).__name__)
            return self._stored(row, now)
        if entry is None or getattr(entry, "auth_type", None) != "oauth":
            return self._stored(row, now)
        if getattr(entry, "last_status", None) == _STATUS_DEAD:
            return TokenResolution(REAUTH)  # core already judged the grant dead
        token = getattr(entry, "access_token", None)
        expires_ms = getattr(entry, "expires_at_ms", None)
        expiring = False
        if isinstance(expires_ms, (int, float)) and not isinstance(expires_ms, bool):
            expiring = int(expires_ms) <= int(now.timestamp() * 1000) + EXPIRY_SKEW_MS
        if token and not expiring:
            return TokenResolution(OK, SecretValue(token))
        if not getattr(entry, "refresh_token", None):
            return TokenResolution(REAUTH)  # expired and nothing to refresh with
        return self._refresh(pool, row.row_id)

    # -- internals -------------------------------------------------------------------------------
    @staticmethod
    def _find(pool: Any, row_id: str) -> Any:
        return next((e for e in pool.entries() if getattr(e, "id", None) == row_id), None)

    @staticmethod
    def _stored(row: StoredRow, now: datetime) -> TokenResolution:
        if _expiring(row.expires_at, now):
            return TokenResolution(UNAVAILABLE)
        return TokenResolution(OK, row.token)

    def _backed_off(self, row_id: str) -> bool:
        with self._lock:
            failed = self._failed_at.get(row_id)
            return failed is not None and self._monotonic() - failed < self._backoff

    def _remember_failure(self, row_id: str) -> None:
        with self._lock:
            self._failed_at[row_id] = self._monotonic()

    def _refresh(self, pool: Any, row_id: str) -> TokenResolution:
        try:
            # The call `hermes auth refresh` makes: one forced, lock-protected rotation that core persists.
            refreshed = pool.try_refresh_matching(credential_id=row_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("usage_deck: anthropic token refresh raised %s", type(exc).__name__)
            self._remember_failure(row_id)
            return TokenResolution(UNAVAILABLE)
        token = getattr(refreshed, "access_token", None) if refreshed is not None else None
        if token:
            with self._lock:
                self._failed_at.pop(row_id, None)
            return TokenResolution(OK, SecretValue(token), refreshed=True)
        try:
            after = self._find(pool, row_id)
        except Exception:  # noqa: BLE001
            after = None
        if after is None or getattr(after, "last_status", None) == _STATUS_DEAD:
            return TokenResolution(REAUTH)  # core dropped / killed the row: the grant is gone
        self._remember_failure(row_id)
        return TokenResolution(UNAVAILABLE)


_default_factory: Callable[[], TokenResolver] = CoreTokenResolver


def default_resolver() -> TokenResolver:
    """The production resolver (core-backed). Tests install a hermetic factory via ``set_default_factory``."""
    return _default_factory()


def set_default_factory(factory: Callable[[], TokenResolver]) -> None:
    global _default_factory
    _default_factory = factory
