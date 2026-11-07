"""Injected, stdlib-only HTTP transport (DESIGN.md §1.5).

GET only; no redirects (3xx is an error, same-origin or not); TLS verified;
finite timeout; 1 MiB response cap. A ``GuardedTransport`` wraps any transport
and refuses URLs outside the allowlisted origins/paths so adapters can never
send credentials to another origin.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Protocol, Tuple
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_S = 2.0
MAX_RESPONSE_BYTES = 1024 * 1024


class TransportError(Exception):
    """Transport-level failure. ``kind`` is one of the constants below.

    The message never contains the URL, headers or upstream text.
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    REDIRECT = "redirect"
    OVERSIZE = "oversize"
    FORBIDDEN_URL = "forbidden_url"
    TLS = "tls"

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    latency_ms: float
    content_type: Optional[str] = None


class HttpTransport(Protocol):
    def get(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = DEFAULT_TIMEOUT_S,
            max_bytes: int = MAX_RESPONSE_BYTES) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class UrllibTransport:
    """Default transport. Non-2xx statuses are returned, not raised."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def get(self, url, headers=None, timeout=DEFAULT_TIMEOUT_S, max_bytes=MAX_RESPONSE_BYTES) -> HttpResponse:
        request = urllib.request.Request(url, method="GET", headers=dict(headers or {}))
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=timeout) as resp:
                body = resp.read(max_bytes + 1)
                status = resp.status
                ctype = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                if 300 <= status < 400:
                    raise TransportError(TransportError.REDIRECT) from None
                try:
                    body = exc.read(max_bytes + 1)
                except Exception:  # noqa: BLE001 - body of an error response is irrelevant
                    body = b""
                ctype = exc.headers.get("Content-Type") if exc.headers else None
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise TransportError(TransportError.TIMEOUT) from None
            if "CERTIFICATE" in str(reason).upper() or "SSL" in type(reason).__name__.upper():
                raise TransportError(TransportError.TLS) from None
            raise TransportError(TransportError.CONNECTION) from None
        except TimeoutError:
            raise TransportError(TransportError.TIMEOUT) from None
        except (OSError, ValueError):
            raise TransportError(TransportError.CONNECTION) from None
        if len(body) > max_bytes:
            raise TransportError(TransportError.OVERSIZE)
        latency = (time.monotonic() - started) * 1000.0
        return HttpResponse(status, body, round(latency, 3), ctype)


@dataclass(frozen=True)
class AllowedTarget:
    """``(scheme, host, port)`` plus either exact paths or any path (None)."""

    scheme: str
    host: str
    port: Optional[int]
    paths: Optional[Tuple[str, ...]] = None  # None = any path on this origin


def _effective_port(scheme: str, port: Optional[int]) -> int:
    # Absent (None) takes the scheme default; an explicit 0 stays 0 and never matches a target.
    if port is None:
        return 443 if scheme == "https" else 80
    return port


class GuardedTransport:
    def __init__(self, inner: HttpTransport, allowed: Iterable[AllowedTarget]):
        self._inner = inner
        self._allowed = tuple(allowed)

    def get(self, url, headers=None, timeout=DEFAULT_TIMEOUT_S, max_bytes=MAX_RESPONSE_BYTES) -> HttpResponse:
        timeout = min(float(timeout), DEFAULT_TIMEOUT_S) if timeout and timeout > 0 else DEFAULT_TIMEOUT_S
        max_bytes = min(int(max_bytes), MAX_RESPONSE_BYTES)
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise TransportError(TransportError.FORBIDDEN_URL) from None
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if port == 0 or "@" in parts.netloc or "#" in url or scheme not in ("http", "https") or not host:
            raise TransportError(TransportError.FORBIDDEN_URL)
        for target in self._allowed:
            if (target.scheme, target.host) != (scheme, host):
                continue
            if _effective_port(scheme, target.port) != _effective_port(scheme, port):
                continue
            if target.paths is not None and parts.path not in target.paths:
                continue
            if (parts.query or "?" in url) and target.paths is not None:
                continue  # exact-path targets take no query (not even an empty one)
            return self._inner.get(url, headers, timeout, max_bytes)
        raise TransportError(TransportError.FORBIDDEN_URL)
