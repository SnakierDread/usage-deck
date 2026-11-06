"""Endpoint normalization and stable, secret-free identifiers (DESIGN.md §1.1)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from .redaction import is_sensitive_name


@dataclass(frozen=True)
class Endpoint:
    """A parsed http(s) endpoint.

    ``normalized`` is used for identity and display: lowercase scheme/host,
    no userinfo/query/fragment/trailing slash. ``safe`` is False when the raw
    URL carried userinfo or credential-looking query/fragment, in which case
    transport must refuse it (the raw URL is never logged or kept).
    """

    normalized: str
    origin: str
    path: str
    scheme: str
    host: str
    port: Optional[int]
    safe: bool

    def url(self, path: str) -> str:
        return self.origin + path


def parse_endpoint(raw: object) -> Optional[Endpoint]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 2048 or any(ord(c) < 32 for c in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    safe = "@" not in parts.netloc
    for blob in (parts.query, parts.fragment):
        if blob and (is_sensitive_name(blob) or re.search(r"[A-Za-z0-9_-]{24,}", blob)):
            safe = False
    shown_host = f"[{host}]" if ":" in host else host
    netloc = shown_host + (f":{port}" if port else "")
    path = parts.path.rstrip("/")
    origin = f"{scheme}://{netloc}"
    return Endpoint(origin + path, origin, path, scheme, host, port, safe)


def normalize_endpoint(raw: object) -> Optional[str]:
    endpoint = parse_endpoint(raw)
    return endpoint.normalized if endpoint else None


def _digest(prefix: str, parts: list) -> str:
    blob = json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return prefix + hashlib.sha256(blob).hexdigest()[:24]


def make_account_id(provider_id: str, normalized_endpoint: Optional[str], public_row_id_or_ordinal) -> str:
    return _digest("acct_", [provider_id, normalized_endpoint, public_row_id_or_ordinal])


def make_endpoint_id(provider_id: str, normalized_endpoint: str) -> str:
    return _digest("ep_", [provider_id, normalized_endpoint])
