"""Read-only, profile-scoped provider inventory (DESIGN.md §1.1).

The profile ``auth.json`` pool shadows the global one *per provider*, only when
the profile has zero entries for that provider (mirrors Hermes
``read_credential_pool``, hermes_cli/auth.py:934-958). Hermes' own
``_load_auth_store`` is deliberately NOT used: on corrupt JSON it writes a
``.corrupt`` backup into the home, and this plugin never mutates anything.

Nothing here refreshes OAuth, rotates credentials or talks to the network.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from .adapters import is_self_hosted_provider
from .adapters.base import SecretValue
from .models.identity import Endpoint, make_account_id, make_endpoint_id, parse_endpoint
from .models.redaction import collect_secret_values, contains_secret, is_sensitive_name

PROVIDER_LABELS = {
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "openai-codex": "OpenAI Codex",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "custom": "Custom endpoint",
}
PLAN_PROVIDERS = ("openai-codex", "anthropic")
_PLAN_CLAIM_KEYS = ("plan", "plan_type", "subscription_plan", "chatgpt_plan_type")
_PLAN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,31}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:/@+-]{0,63}$")
_CRED_FIELDS = ("access_token", "agent_key", "api_key")
MAX_AUTH_BYTES = 4 * 1024 * 1024


class InventoryUnavailable(Exception):
    """auth.json / config could not be read or is malformed."""


@dataclass(frozen=True)
class RawInventory:
    home_key: str
    pool: Dict[str, List[Dict[str, Any]]]
    legacy: Dict[str, Dict[str, Any]]
    custom_endpoints: List[Dict[str, Any]]
    primary: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class InventoryItem:
    provider_id: str
    provider_label: str
    account_id: str
    account_label: str
    kind: str
    endpoint: Optional[Endpoint]
    endpoint_id: Optional[str]
    auth_status: str
    credential: Optional[SecretValue]
    plan_name: Optional[str]
    plan_source: str
    secrets: FrozenSet[str]
    health_credential: Optional[SecretValue] = None

    def __repr__(self) -> str:
        return f"InventoryItem({self.provider_id!r}, {self.account_id!r})"


# ------------------------------------------------------------------ sources


def _load_json(path: Path, *, required_dict: bool = True) -> Optional[dict]:
    """None when missing; InventoryUnavailable (no path/content) when unreadable/malformed."""
    try:
        if not path.exists():
            return None
        if path.stat().st_size > MAX_AUTH_BYTES:
            raise InventoryUnavailable("auth store too large")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except InventoryUnavailable:
        raise
    except (OSError, ValueError, UnicodeDecodeError):
        raise InventoryUnavailable("auth store unreadable") from None
    if required_dict and not isinstance(data, dict):
        raise InventoryUnavailable("auth store malformed")
    return data


def _dict_section(store: Optional[dict], key: str) -> Dict[str, Any]:
    section = store.get(key) if store else None
    return section if isinstance(section, dict) else {}


class FileInventorySource:
    """Inventory from ``<home>/auth.json`` (+ optional global root) and a config loader.

    ``config_loader`` returns the effective, env-expanded configuration mapping
    (the host loader in production; tests inject a dict). ``env_lookup``
    resolves a single named credential reference.
    """

    def __init__(
        self,
        home: Path,
        global_home: Optional[Path] = None,
        config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
        custom_provider_loader: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
        env_lookup: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self._home = Path(home)
        self._global = Path(global_home) if global_home else None
        self._config_loader = config_loader
        self._custom_loader = custom_provider_loader
        self._env = env_lookup

    def home_key(self) -> str:
        return str(self._home.resolve())

    def env_secret(self, name: str) -> Optional[str]:
        if not self._env:
            return None
        try:
            value = self._env(name)
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, str) and value else None

    def read(self) -> RawInventory:
        profile = _load_json(self._home / "auth.json")
        global_store = None
        if self._global is not None and self._global.resolve() != self._home.resolve():
            try:
                global_store = _load_json(self._global / "auth.json")
            except InventoryUnavailable:
                global_store = None  # a malformed global store never breaks profile reads

        pool: Dict[str, List[Dict[str, Any]]] = {}
        for provider, entries in _dict_section(profile, "credential_pool").items():
            if isinstance(entries, list):
                rows = [e for e in entries if isinstance(e, dict)]
                if rows:
                    pool[str(provider)] = rows
        for provider, entries in _dict_section(global_store, "credential_pool").items():
            if str(provider) in pool or not isinstance(entries, list):
                continue
            rows = [e for e in entries if isinstance(e, dict)]
            if rows:
                pool[str(provider)] = rows  # profile has zero entries: global fallback

        legacy: Dict[str, Dict[str, Any]] = {}
        for provider, entry in _dict_section(global_store, "providers").items():
            if isinstance(entry, dict):
                legacy[str(provider)] = entry
        for provider, entry in _dict_section(profile, "providers").items():
            if isinstance(entry, dict):
                legacy[str(provider)] = entry

        custom: List[Dict[str, Any]] = []
        primary = None
        config: Dict[str, Any] = {}
        if self._config_loader is not None:
            try:
                loaded = self._config_loader()
            except Exception:  # noqa: BLE001
                raise InventoryUnavailable("config unreadable") from None
            config = loaded if isinstance(loaded, dict) else {}
        if config:
            try:
                if self._custom_loader is not None:
                    custom = [dict(e) for e in self._custom_loader(config) if isinstance(e, dict)]
            except Exception:  # noqa: BLE001
                raise InventoryUnavailable("config unreadable") from None
            model = config.get("model")
            if isinstance(model, dict):
                provider = model.get("provider")
                if isinstance(provider, str) and provider.strip():
                    primary = {"provider": provider.strip(), "base_url": model.get("base_url")}
        return RawInventory(str(self._home.resolve()), pool, legacy, custom, primary)


def host_inventory_source() -> FileInventorySource:
    """Build a source from the host's request-scoped helpers (lazy imports)."""
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home  # type: ignore
        from hermes_cli.config import get_compatible_custom_providers, get_env_value, load_config  # type: ignore
    except Exception:  # noqa: BLE001
        raise InventoryUnavailable("host helpers unavailable") from None
    try:
        home = Path(get_hermes_home())
        root = Path(get_default_hermes_root(home=home))  # root of the request-scoped home, not of the env
    except Exception:  # noqa: BLE001
        raise InventoryUnavailable("home unresolved") from None
    return FileInventorySource(
        home,
        root,
        config_loader=load_config,
        custom_provider_loader=lambda cfg: get_compatible_custom_providers(cfg),
        env_lookup=get_env_value,
    )


# ---------------------------------------------------------------- discovery


def _credential_of(entry: Dict[str, Any]) -> Optional[str]:
    for name in _CRED_FIELDS:
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tokens = entry.get("tokens")
    if isinstance(tokens, dict):
        value = tokens.get("access_token")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def plan_from_token(provider_id: str, token: Optional[str]) -> Optional[str]:
    """Allowlisted, unverified plan claim from an existing JWT; never network, never verified."""
    if provider_id.lower() not in PLAN_PROVIDERS or not token or token.count(".") != 2 or len(token) > 16384:
        return None
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    containers = [payload] + [v for k, v in payload.items()
                              if isinstance(k, str) and k.startswith("https://api.openai.com/") and isinstance(v, dict)]
    for container in containers:
        for key in _PLAN_CLAIM_KEYS:
            claim = container.get(key)
            if isinstance(claim, str) and _PLAN_RE.match(claim):
                return claim
    return None


def provider_label_for(provider_id: str, secrets: FrozenSet[str]) -> str:
    lowered = provider_id.lower()
    if lowered in PROVIDER_LABELS:
        return PROVIDER_LABELS[lowered]
    name = provider_id.split(":", 1)[1] if lowered.startswith("custom:") else provider_id
    if _LABEL_RE.match(name) and not contains_secret(name, secrets):
        return name
    return "Provider"


def _public_row_id(entry: Dict[str, Any], secrets: FrozenSet[str]) -> Optional[str]:
    row_id = entry.get("id")
    if isinstance(row_id, (str, int)) and not isinstance(row_id, bool):
        text = str(row_id)
        if 0 < len(text) <= 64 and not contains_secret(text, secrets) and not text.startswith(("eyJ", "sk-")):
            return text
    return None


@dataclass
class _Draft:
    provider_id: str
    endpoint: Optional[Endpoint]
    row_id: Optional[str]
    ordinal: int
    credential: Optional[str]
    secrets: FrozenSet[str]
    health_credential: Optional[str] = None
    from_pool: bool = False


def _endpoint_from_entry(entry: Dict[str, Any]) -> Optional[Endpoint]:
    for name in ("base_url", "inference_base_url", "url"):
        endpoint = parse_endpoint(entry.get(name))
        if endpoint is not None:
            return endpoint
    return None


def discover(source: Any) -> List[InventoryItem]:
    """Return one InventoryItem per effective pool account / configured endpoint."""
    raw: RawInventory = source.read()
    drafts: List[_Draft] = []
    seen_rows: set = set()

    for provider_id, entries in raw.pool.items():
        for ordinal, entry in enumerate(entries, start=1):
            secrets = collect_secret_values(entry)
            row_id = _public_row_id(entry, secrets)
            if row_id is not None:
                key = (provider_id, row_id)
                if key in seen_rows:
                    continue  # exact duplicate stored row id
                seen_rows.add(key)
            endpoint = _endpoint_from_entry(entry) if is_self_hosted_provider(provider_id) else None
            drafts.append(_Draft(provider_id, endpoint, row_id, ordinal, _credential_of(entry), secrets, None, True))

    pooled_ids = {d.provider_id for d in drafts}
    for provider_id, entry in raw.legacy.items():
        if provider_id in pooled_ids:
            continue
        secrets = collect_secret_values(entry)
        endpoint = _endpoint_from_entry(entry) if is_self_hosted_provider(provider_id) else None
        drafts.append(_Draft(provider_id, endpoint, None, 1, _credential_of(entry), secrets))

    def drafts_matching(provider_id: str) -> List[_Draft]:
        exact = [d for d in drafts if d.provider_id == provider_id]
        if exact:
            return exact
        folded = {d.provider_id for d in drafts if d.provider_id.lower() == provider_id.lower()}
        return [d for d in drafts if d.provider_id in folded] if len(folded) == 1 else []

    # Configured custom endpoints (possibly credential-less).
    for entry in raw.custom_endpoints:
        name = entry.get("name") or entry.get("provider_key")
        endpoint = parse_endpoint(entry.get("base_url"))
        if not isinstance(name, str) or not name.strip() or endpoint is None:
            continue
        provider_id = name.strip() if name.strip().lower().startswith("custom:") else f"custom:{name.strip()}"
        secrets = collect_secret_values(entry)
        existing = drafts_matching(provider_id)
        covered = False
        for draft in existing:
            if draft.endpoint is None:
                draft.endpoint = endpoint
                covered = True
            elif draft.endpoint.normalized == endpoint.normalized:
                covered = True
        if existing and covered:
            continue
        api_key = entry.get("api_key")
        credential = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        drafts.append(_Draft(existing[0].provider_id if existing else provider_id, endpoint, None, 1,
                             credential, secrets))

    # Primary configured provider, even without credentials.
    primary = raw.primary
    if primary:
        provider_id = primary["provider"]
        if provider_id.lower() != "auto":
            existing = drafts_matching(provider_id)
            endpoint = parse_endpoint(primary.get("base_url")) if is_self_hosted_provider(provider_id) else None
            if existing:
                for draft in existing:
                    if draft.endpoint is None and endpoint is not None:
                        draft.endpoint = endpoint
            else:
                drafts.append(_Draft(provider_id, endpoint, None, 1, None, frozenset()))

    # Ollama-style pool entries with no endpoint of their own use the primary's endpoint.
    items: List[InventoryItem] = []
    per_provider: Dict[str, int] = {}
    seen_ids: set = set()
    for draft in drafts:
        provider_id = draft.provider_id
        credential = draft.credential
        secrets = set(draft.secrets)
        if credential is None and provider_id.lower() == "openrouter":
            credential = source.env_secret("OPENROUTER_API_KEY")
        if credential:
            secrets.add(credential)
        kind = "self_hosted" if is_self_hosted_provider(provider_id) else "cloud"
        endpoint = draft.endpoint if kind == "self_hosted" else None
        normalized = endpoint.normalized if endpoint else None
        row_ref = draft.row_id if draft.row_id is not None else draft.ordinal
        account_id = make_account_id(provider_id, normalized, row_ref)
        if account_id in seen_ids:
            continue  # exact duplicate normalized inventory item
        seen_ids.add(account_id)
        per_provider[provider_id] = per_provider.get(provider_id, 0) + 1
        plan = plan_from_token(provider_id, credential)
        frozen = frozenset(s for s in secrets if s)
        items.append(InventoryItem(
            provider_id=provider_id,
            provider_label=provider_label_for(provider_id, frozen),
            account_id=account_id,
            account_label=f"Account {per_provider[provider_id]}",
            kind=kind,
            endpoint=endpoint,
            endpoint_id=make_endpoint_id(provider_id, normalized) if normalized else None,
            auth_status="available" if credential else "missing",
            credential=SecretValue(credential) if credential else None,
            plan_name=plan,
            plan_source="unverified_claim" if plan else "none",
            secrets=frozen,
        ))
    items.sort(key=lambda i: (i.provider_id, i.account_id))
    return items
