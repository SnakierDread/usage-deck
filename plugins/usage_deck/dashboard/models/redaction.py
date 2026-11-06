"""Secret masking shared by every DTO path (DESIGN.md §2.1).

One recursive sanitizer runs at the coordinator boundary, before anything is
cached, logged or serialized:

* any mapping key containing (case-insensitive) one of ``SENSITIVE_FRAGMENTS``
  has its value replaced by the literal ``[REDACTED]`` -- no suffix, no
  fingerprint;
* every string is scrubbed of exact private secret values (from the current
  context), credential-looking patterns (Bearer, JWT, ``sk-`` style prefixes)
  and URL userinfo/query/fragment;
* non-JSON values (and NaN/Infinity) are rejected.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

REDACTED = "[REDACTED]"

# Case-insensitive substrings of a *field name* that force redaction.
SENSITIVE_FRAGMENTS = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credentials",
    "bearer",
    "jwt",
    "access",
    "refresh",
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?")
_PREFIX_RE = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}")
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/?#\"'<>]*)([^\s\"'<>]*)")


class RedactionError(TypeError):
    """Raised for values that cannot be serialized safely (non-JSON, NaN)."""


def is_sensitive_name(name: Any) -> bool:
    lowered = str(name).lower()
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def _scrub_url(match: "re.Match[str]") -> str:
    scheme, authority, rest = match.group(1), match.group(2), match.group(3)
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    # Drop query and fragment: they may carry credentials.
    cut = len(rest)
    for marker in ("?", "#"):
        idx = rest.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return f"{scheme}{authority}{rest[:cut]}"


def scrub_string(value: str, secrets: Iterable[str] = ()) -> str:
    """Remove secrets and credential-shaped substrings from free text."""
    out = value
    # Longest first so a secret that contains another is replaced whole.
    for secret in sorted({s for s in secrets if isinstance(s, str)}, key=len, reverse=True):
        if secret and secret in out:  # DESIGN.md §2.1: every nonempty secret, any length
            out = out.replace(secret, REDACTED)
    out = _URL_RE.sub(_scrub_url, out)
    out = _BEARER_RE.sub(REDACTED, out)
    out = _JWT_RE.sub(REDACTED, out)
    out = _PREFIX_RE.sub(REDACTED, out)
    return out


def contains_secret(value: str, secrets: Iterable[str] = ()) -> bool:
    """True when ``scrub_string`` would change ``value`` (used to reject labels)."""
    return scrub_string(value, secrets) != value


def sanitize(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a deep, JSON-safe, redacted copy of ``value``."""
    secret_set = frozenset(s for s in secrets if isinstance(s, str) and s)
    return _walk(value, secret_set, 0)


def _walk(value: Any, secrets: frozenset, depth: int) -> Any:
    if depth > 32:
        raise RedactionError("structure too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RedactionError("non-finite number")
        return value
    if isinstance(value, str):
        return scrub_string(value, secrets)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RedactionError("non-string key")
            if is_sensitive_name(key):
                out[key] = REDACTED
            else:
                out[scrub_string(key, secrets)] = _walk(item, secrets, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_walk(item, secrets, depth + 1) for item in value]
    raise RedactionError(f"non-JSON value of type {type(value).__name__}")


def collect_secret_values(raw: Any, _depth: int = 0) -> frozenset:
    """Collect string values stored under sensitive field names in raw input.

    Used to seed the exact-value scrub for the private context, including
    values that later surface under generic names.
    """
    found: set = set()
    if _depth > 16:
        return frozenset()
    if isinstance(raw, dict):
        for key, item in raw.items():
            if is_sensitive_name(key):
                _collect_strings(item, found, 0)
            else:
                found |= collect_secret_values(item, _depth + 1)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            found |= collect_secret_values(item, _depth + 1)
    return frozenset(s for s in found if s)


def _collect_strings(value: Any, found: set, depth: int) -> None:
    if depth > 16:
        return
    if isinstance(value, str):
        if value:
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, found, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, found, depth + 1)
