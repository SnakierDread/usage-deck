"""UsageDeck dashboard plugin API (thin router; DESIGN.md §3, §6).

The host mounts this router at ``/api/plugins/usage-deck``, so the router
exports only the relative suffixes ``/subscriptions``, ``/usage`` and
``/health`` (no prefix, no alias).

Importing this module needs neither FastAPI nor Hermes: ``create_router()``
imports FastAPI lazily and ``router`` is ``None`` in a bare environment.
The dashboard loads this file under a synthetic module name with no package,
so when ``__package__`` is empty a private package rooted at this directory is
bootstrapped with stdlib importlib (no ``sys.path`` mutation, no cwd imports).
"""

import importlib
import importlib.machinery
import importlib.util
import os
import sys
import threading

_BUNDLE = "_usage_deck_bundle"


def _sibling(name):
    package = __package__
    if not package:
        package = _BUNDLE
        if package not in sys.modules:
            here = os.path.dirname(os.path.abspath(__file__))
            spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
            spec.submodule_search_locations = [here]
            module = importlib.util.module_from_spec(spec)
            sys.modules[package] = module
    return importlib.import_module(package + "." + name)


_service_mod = _sibling("service")

API_BASE = _service_mod.API_BASE
UsageDeckService = _service_mod.UsageDeckService

_service = None
_service_lock = threading.Lock()


def get_service():
    """Process-wide service (owns the 30 s snapshot cache)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = UsageDeckService()
        return _service


def create_router(service=None):
    """Build the relative router. Raises ImportError when FastAPI is absent."""
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    api = APIRouter()

    def _respond(endpoint, request: Request):
        svc = service or get_service()
        status, body = svc.handle(endpoint, list(request.query_params.multi_items()))
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    # Sync handlers: the host runs them in a worker thread with the request's
    # profile scope (context variables) copied in.
    @api.get("/subscriptions")
    def subscriptions(request: Request):
        return _respond("subscriptions", request)

    @api.get("/usage")
    def usage(request: Request):
        return _respond("usage", request)

    @api.get("/health")
    def health(request: Request):
        return _respond("health", request)

    return api


try:
    router = create_router()
except ImportError:  # bare environment: the host never treats this as integration success
    router = None

__all__ = ["API_BASE", "UsageDeckService", "create_router", "get_service", "router"]
