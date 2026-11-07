"""Opt-in agent-side observer for Anthropic quota headers (DESIGN.md §12.1–§12.3).

Hermes publishes the in-process ``provider_ratelimit_headers`` hook once per actual native Anthropic
OAuth response (sanitized ``anthropic-ratelimit-unified-*`` headers plus request-local provenance).
This module is the only subscriber that persists anything, and only when the profile that issued the
request has opted in::

    usage_deck:
      capture_anthropic_headers: true      # absent / false / any other value = off (default)

Consent is read through the host's config loader for the *originating* agent profile (the process /
context home the hook fires in), checked against the payload's ``execution_profile`` -- never the
holder of the credential row, never the profile the dashboard happens to display. Installing the
plugin or opening the dashboard enables nothing. With the setting off, on an unknown host, on any
validation doubt, or on any failure the observer does nothing: no write, no request, no retry, no
refresh, no log line carrying a path, header or exception text. Normal inference is never affected
(every error is swallowed at this boundary).

The observer imports nothing from the dashboard service, never touches ``auth.json``/``.env``/config
(it reads the config through the host loader only) and writes only the plugin-data capture store via
:func:`captures.record_headers`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Optional, Tuple

from . import captures

log = logging.getLogger(__name__)

HOOK_NAME = "provider_ratelimit_headers"
PAYLOAD_SCHEMA_VERSION = 1
PROVIDER = "anthropic"
CONFIG_SECTION = "usage_deck"
CONFIG_KEY = "capture_anthropic_headers"
GLOBAL_PROFILE = captures.GLOBAL_PROFILE
_PAYLOAD_KEYS = frozenset({
    "schema_version", "provider", "hermes_root", "execution_profile", "holder",
    "credential_id", "observed_at", "headers",
})


class HostContext(NamedTuple):
    """The originating agent's home as the host resolves it, plus its effective config mapping."""

    home: Path
    root: Path
    config: Mapping[str, Any]


def host_context() -> HostContext:
    """Resolve the current (originating) home, its Hermes root and its config through host helpers.

    The hook fires inside the originating agent's own process/context, so ``get_hermes_home()`` is that
    agent's home; ``load_config_readonly()`` reads that home's ``config.yaml`` (no process-env
    mutation, no write). Raises when the host helpers are unavailable (an unknown host): the caller
    then skips.
    """
    from hermes_constants import get_default_hermes_root, get_hermes_home  # type: ignore
    from hermes_cli.config import load_config_readonly  # type: ignore

    home = Path(os.path.realpath(get_hermes_home()))
    root = Path(os.path.realpath(get_default_hermes_root(home=home)))
    config = load_config_readonly()
    return HostContext(home, root, config if isinstance(config, Mapping) else {})


def _profile_of(home: Path, root: Path) -> Optional[Tuple[str, bool]]:
    """``(profile, is_global)`` of ``home`` relative to ``root``; None when it is neither the root nor a
    direct ``<root>/profiles/<name>`` home."""
    if home == root:
        return GLOBAL_PROFILE, True
    if home.parent == root / "profiles" and captures._PROFILE_RE.match(home.name):
        return home.name, False
    return None


def capture_enabled(config: Mapping[str, Any]) -> bool:
    """Only an explicit boolean ``true`` under ``usage_deck.capture_anthropic_headers`` opts in."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    return isinstance(section, Mapping) and section.get(CONFIG_KEY) is True


def _scope(value: Any) -> Optional[Tuple[str, bool]]:
    if not isinstance(value, Mapping) or set(value) != {"profile", "is_global"}:
        return None
    profile, is_global = value["profile"], value["is_global"]
    if not isinstance(profile, str) or not isinstance(is_global, bool):
        return None
    return profile, is_global


class Observer:
    """Validates one hook payload against the host and, when opted in, records it. Never raises."""

    def __init__(self, context: Optional[Callable[[], HostContext]] = None,
                 writer: Optional[Callable[..., Any]] = None):
        self._context = context  # None: the host's own resolution, looked up at call time
        self._writer = writer

    def __call__(self, **payload: Any) -> None:
        try:
            self._observe(payload)
        except Exception:  # noqa: BLE001 - telemetry boundary: fail closed for telemetry, open for inference
            log.debug("usage_deck observer: skipped")  # fixed text: never a path, header or exception

    def _observe(self, payload: Mapping[str, Any]) -> None:
        # The host adds envelope metadata of its own (e.g. ``telemetry_schema_version``) to every hook call and
        # payloads evolve additively: every required field must be present, extra fields are ignored unread.
        if not _PAYLOAD_KEYS <= set(payload):
            return
        version = payload["schema_version"]
        if type(version) is not int or version != PAYLOAD_SCHEMA_VERSION or payload["provider"] != PROVIDER:
            return  # unknown / newer payload shape: skip, never guess
        execution = _scope(payload["execution_profile"])
        holder = _scope(payload["holder"])
        credential_id, observed, hermes_root = payload["credential_id"], payload["observed_at"], payload["hermes_root"]
        if execution is None or holder is None or not all(
                isinstance(v, str) for v in (credential_id, observed, hermes_root)):
            return
        if not _scope_ok(execution) or not _scope_ok(holder):
            return

        context = (self._context or host_context)()
        # The supplied root and execution profile must be exactly the host's own view of the home this
        # hook is running in; a mismatch means we cannot attribute consent, so we skip.
        if not (context.root.is_absolute() and str(context.root) == os.path.realpath(hermes_root)):
            return
        if _profile_of(context.home, context.root) != execution:
            return
        # The credential row is owned by the execution profile's own store or by the global fallback store.
        if holder != execution and holder != (GLOBAL_PROFILE, True):
            return
        if not capture_enabled(context.config):  # consent of the ORIGINATING profile, default off
            return

        headers = captures.sanitize_headers(payload["headers"])
        if not headers:
            return
        stamp: datetime = captures._parse_ts(observed)  # receipt time from core; never the read time
        try:
            (self._writer or captures.record_headers)(
                captures.default_store_path(context.root), PROVIDER, credential_id, headers, stamp,
                holder=(holder[1], holder[0]))
        except captures.CaptureError:
            log.debug("usage_deck observer: observation dropped")


def _scope_ok(scope: Tuple[str, bool]) -> bool:
    profile, is_global = scope
    return (profile == GLOBAL_PROFILE) if is_global else bool(captures._PROFILE_RE.match(profile))


_default = Observer()


def observe(**payload: Any) -> None:
    """The hook subscriber (a plain function: hosts introspect callbacks by name/signature).

    Stateless: every call re-validates the payload against the host and re-reads consent.
    """
    _default(**payload)


def host_has_hook() -> bool:
    """True only on a Hermes whose hook registry knows ``provider_ratelimit_headers``. Unknown or older
    hosts (or no host at all) skip: no subscription, no SDK monkeypatching, no log scraping."""
    try:
        from hermes_cli.plugins import VALID_HOOKS  # type: ignore
    except Exception:  # noqa: BLE001
        return False
    return HOOK_NAME in VALID_HOOKS


def register(ctx: Any) -> None:
    """Subscribe to the hook when this Hermes version has it. Registration alone captures nothing."""
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook) or not host_has_hook():
        return
    try:
        register_hook(HOOK_NAME, observe)
    except Exception:  # noqa: BLE001 - never let a registration problem stop the plugin from loading
        log.debug("usage_deck observer: hook registration unavailable")
