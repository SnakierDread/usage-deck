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
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from .adapters import is_self_hosted_provider
from .adapters.base import SecretValue
from .adapters.cloud import CODEX_PROVIDER_ID, codex_routing
from .captures import default_store_path
from .models.dto import Holder
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
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_AUTH_BYTES = 4 * 1024 * 1024
MAX_PROFILES = 64
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
GLOBAL_PROFILE = "global"

log = logging.getLogger(__name__)


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
class StoreRead:
    """One auth store (the global root or one profile) read on its own: no shadowing, no config."""

    profile: str
    is_global: bool
    raw: RawInventory


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
    #: ``pool`` | ``legacy`` | ``config``: where the inventory row came from.
    origin: str = "pool"
    #: Public (non-secret) stored pool row ids behind this item; used to look up captures.
    row_ids: Tuple[str, ...] = ()
    #: Private merge key for the machine view (never serialized, never shown): derived from a JWT
    #: account identity, a self-hosted endpoint or a public row id; None = never merged across homes.
    identity_key: Optional[str] = field(default=None, repr=False)
    holders: Tuple[Holder, ...] = ()
    # The entry points at an env var (``key_env``) that holds no value: a credential is
    # expected but absent, so nothing may be probed anonymously on its behalf.
    credential_expected: bool = False

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

    # -- machine view helpers (DESIGN.md §10) ---------------------------------
    def root_dir(self) -> Path:
        """Hermes root that owns ``auth.json`` and ``profiles/*`` (the home itself if no root is known)."""
        return self._global if self._global is not None else self._home

    def profile_name(self) -> Tuple[str, bool]:
        """(display name, is_global) of the home this source reads."""
        home = self._home.resolve()
        root = self.root_dir().resolve()
        if home == root:
            return GLOBAL_PROFILE, True
        if home.parent.name == "profiles" and home.parent.parent == root and _PROFILE_NAME_RE.match(home.name):
            return home.name, False
        return "current", False

    def captures_path(self) -> Path:
        return default_store_path(self.root_dir())

    def read_stores(self) -> List[StoreRead]:
        """Read global ``auth.json`` and every ``profiles/<name>/auth.json`` on their own.

        Only real directories under ``<root>/profiles`` whose names are valid profile names, and only
        non-symlink ``auth.json`` files, are read, so nothing outside the Hermes root is ever opened.
        An unreadable or malformed store is skipped (logged without path or content) except the
        store of the home being viewed, which fails like the profile view does.
        """
        root = self.root_dir()
        current = self._home.resolve()
        candidates: List[Tuple[str, bool, Path]] = [(GLOBAL_PROFILE, True, root)]
        try:
            names = sorted(p.name for p in (root / "profiles").iterdir()
                           if _PROFILE_NAME_RE.match(p.name) and p.is_dir() and not p.is_symlink())
        except OSError:
            names = []
        candidates += [(name, False, root / "profiles" / name) for name in names[:MAX_PROFILES]]
        stores: List[StoreRead] = []
        for name, is_global, directory in candidates:
            auth = directory / "auth.json"
            try:
                if auth.is_symlink() or not auth.is_file():
                    continue
                data = _load_json(auth)
            except (InventoryUnavailable, OSError):
                if directory.resolve() == current:
                    raise InventoryUnavailable("auth store unreadable") from None
                log.warning("usage_deck: skipped an unreadable auth store (%s)", name)
                continue
            if data is None:
                continue
            pool: Dict[str, List[Dict[str, Any]]] = {}
            for provider, entries in _dict_section(data, "credential_pool").items():
                if isinstance(entries, list):
                    rows = [e for e in entries if isinstance(e, dict)]
                    if rows:
                        pool[str(provider)] = rows
            legacy = {str(provider): entry for provider, entry in _dict_section(data, "providers").items()
                      if isinstance(entry, dict) and str(provider) not in pool}
            stores.append(StoreRead(name, is_global, RawInventory(str(directory.resolve()), pool, legacy, [], None)))
        return stores

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


def _jwt_payload(token: Optional[str]) -> Optional[dict]:
    """Decoded (unverified) JWT payload, or None. Local only: never validated, never sent anywhere."""
    if not token or token.count(".") != 2 or len(token) > 16384:
        return None
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def plan_from_token(provider_id: str, token: Optional[str]) -> Optional[str]:
    """Allowlisted, unverified plan claim from an existing JWT; never network, never verified."""
    if provider_id.lower() not in PLAN_PROVIDERS:
        return None
    payload = _jwt_payload(token)
    if payload is None:
        return None
    containers = [payload] + [v for k, v in payload.items()
                              if isinstance(k, str) and k.startswith("https://api.openai.com/") and isinstance(v, dict)]
    for container in containers:
        for key in _PLAN_CLAIM_KEYS:
            claim = container.get(key)
            if isinstance(claim, str) and _PLAN_RE.match(claim):
                return claim
    return None


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


def account_identity(provider_id: str, token: Optional[str]) -> Optional[str]:
    """Stable account identity from an existing JWT's account claims, or None.

    Used only as a private merge key for the machine view (it is hashed into the card id and never
    displayed or serialized). The account claim alone names a *workspace*, so the user claim is
    included: two seats of one business workspace are different accounts.
    """
    payload = _jwt_payload(token)
    if payload is None:
        return None
    claims = payload.get(_OPENAI_AUTH_CLAIM)
    if not isinstance(claims, dict):
        return None
    account = claims.get("chatgpt_account_id")
    if not isinstance(account, str) or not _IDENTITY_RE.match(account):
        return None
    user = None
    for name in ("chatgpt_user_id", "user_id", "chatgpt_account_user_id"):
        candidate = claims.get(name)
        if isinstance(candidate, str) and _IDENTITY_RE.match(candidate):
            user = candidate
            break
    return f"jwt:{provider_id}:{account}:{user or ''}"


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
    origin: str = "pool"
    key_env: Optional[str] = None


def _key_env_of(entry: Dict[str, Any]) -> Optional[str]:
    """Name of the env var a ``custom_providers`` entry points at (never its value)."""
    for name in ("key_env", "api_key_env"):
        value = entry.get(name)
        if isinstance(value, str) and _ENV_NAME_RE.match(value.strip()):
            return value.strip()
    return None


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
        drafts.append(_Draft(provider_id, endpoint, None, 1, _credential_of(entry), secrets, origin="legacy"))

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
        key_env = _key_env_of(entry)
        existing = drafts_matching(provider_id)
        covered = False
        for draft in existing:
            if key_env and draft.credential is None and draft.key_env is None:
                draft.key_env = key_env
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
                             credential, secrets, origin="config", key_env=key_env))

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
                drafts.append(_Draft(provider_id, endpoint, None, 1, None, frozenset(), origin="config"))

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
        # Precedence: an inline credential (api_key/token) beats the key_env pointer.
        if credential is None and draft.key_env:
            credential = source.env_secret(draft.key_env)
        if credential:
            secrets.add(credential)
            if provider_id.strip().lower() == CODEX_PROVIDER_ID:
                routing = codex_routing(credential)  # private routing claims join the masking context
                if routing is not None:
                    secrets |= routing.masking_values()
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
        jwt_identity = account_identity(provider_id, credential) if credential else None
        if jwt_identity:
            identity = jwt_identity
        elif draft.row_id is not None:
            identity = f"row:{provider_id}:{draft.row_id}"
        elif draft.origin == "config" and normalized:
            identity = f"cfgep:{provider_id}:{normalized}"
        elif draft.origin == "config" and not credential:
            identity = f"cfg:{provider_id}"
        elif kind == "self_hosted" and normalized:
            identity = f"ep:{provider_id}:{normalized}"
        else:
            identity = None
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
            origin=draft.origin,
            row_ids=(draft.row_id,) if draft.row_id is not None else (),
            identity_key=identity,
            credential_expected=bool(draft.key_env) and credential is None,
        ))
    items.sort(key=lambda i: (i.provider_id, i.account_id))
    return items


# ------------------------------------------------------------ machine view


class _StaticSource:
    """Adapts an already-read RawInventory to the ``discover`` source protocol."""

    def __init__(self, raw: RawInventory, env_lookup: Optional[Callable[[str], Optional[str]]] = None):
        self._raw = raw
        self._env = env_lookup

    def read(self) -> RawInventory:
        return self._raw

    def env_secret(self, name: str) -> Optional[str]:
        return self._env(name) if self._env else None


def discover_machine(source: Any) -> List[InventoryItem]:
    """Machine-wide inventory: global + every profile credential pool, deduped by account identity.

    Each store is discovered on its own (a profile that merely falls back to the global pool does not
    "hold" those accounts), then items are merged when they share a private identity key: the JWT
    account + user claims, or an identical public pool row id for a provider (so two api-key rows to
    one endpoint stay two accounts, as in the profile view). Anything without such an identity
    (anonymous rows) is never merged across homes. The viewed profile's config-derived endpoints
    (custom/primary) join the union; they are dropped when a pooled account already covers the same
    provider (cloud) or provider + endpoint (self-hosted).
    Raises ``InventoryUnavailable`` like the profile view; secrets never leave ``InventoryItem``.
    """
    env = getattr(source, "env_secret", None)
    groups: Dict[Any, List[Tuple[Holder, InventoryItem]]] = {}

    def add(holder: Holder, item: InventoryItem) -> None:
        key = item.identity_key or ("anonymous", holder.profile, holder.is_global, item.account_id)
        groups.setdefault(key, []).append((holder, item))

    for store in source.read_stores():
        holder = Holder(store.profile, store.is_global)
        for item in discover(_StaticSource(store.raw, env)):
            add(holder, item)

    raw = source.read()
    if raw.custom_endpoints or raw.primary:
        name, is_global = source.profile_name()
        config_only = RawInventory(raw.home_key, {}, {}, raw.custom_endpoints, raw.primary)
        for item in discover(_StaticSource(config_only, env)):
            add(Holder(name, is_global), item)

    def is_config(key: Any) -> bool:
        return isinstance(key, str) and key.startswith(("cfg:", "cfgep:"))

    held_providers = {members[0][1].provider_id for key, members in groups.items() if not is_config(key)}
    held_endpoints = {(members[0][1].provider_id, members[0][1].endpoint.normalized)
                      for key, members in groups.items()
                      if not is_config(key) and members[0][1].endpoint is not None}
    merged: List[InventoryItem] = []
    per_provider: Dict[str, int] = {}
    seen_ids: set = set()
    for key, members in groups.items():
        first = members[0][1]
        if isinstance(key, str) and key.startswith("cfg:") and first.provider_id in held_providers:
            continue  # a configured cloud provider with no credential: some home already holds an account
        if isinstance(key, str) and key.startswith("cfgep:") and first.endpoint is not None \
                and (first.provider_id, first.endpoint.normalized) in held_endpoints:
            continue  # the configured endpoint is already a pooled account
        holders: List[Holder] = []
        for holder, _item in members:
            if holder not in holders:
                holders.append(holder)
        items = [item for _holder, item in members]
        rep = next((i for i in items if i.credential is not None), first)
        plan = next((i for i in items if i.plan_name), None)
        secrets = frozenset().union(*(i.secrets for i in items))
        row_ids = tuple(dict.fromkeys(r for i in items for r in i.row_ids))
        anchor = first.identity_key or f"{holders[0].profile}:{first.account_id}"
        normalized = first.endpoint.normalized if first.endpoint else None
        account_id = make_account_id(first.provider_id, normalized, "machine:" + anchor)
        if account_id in seen_ids:
            continue
        seen_ids.add(account_id)
        per_provider[first.provider_id] = per_provider.get(first.provider_id, 0) + 1
        merged.append(replace(
            rep,
            account_id=account_id,
            account_label=f"Account {per_provider[first.provider_id]}",
            auth_status="available" if any(i.credential is not None for i in items) else "missing",
            plan_name=plan.plan_name if plan else None,
            plan_source=plan.plan_source if plan else "none",
            secrets=secrets,
            credential_expected=rep.credential is None and any(i.credential_expected for i in items),
            row_ids=row_ids,
            identity_key=first.identity_key,
            holders=tuple(holders),
        ))
    merged.sort(key=lambda i: (i.provider_id, i.account_id))
    return merged
