"""Adapter registry (DESIGN.md §1.2): dict keyed by provider id -> adapter class.

Resolution order: exact built-in id -> configured custom/ollama endpoint
adapter -> SessionAnalyticsAdapter. IDs are lookup keys only; no dynamic
imports, no remote discovery, never a crash for an unknown provider.
"""

from __future__ import annotations

from typing import Dict, Type

from .base import AdapterContext, BaseAdapter, ProviderAdapter
from .cloud import AnthropicAdapter, NousAdapter, OpenAICodexAdapter, OpenRouterAdapter
from .fallback import SessionAnalyticsAdapter
from .local import SelfHostedAdapter

SELF_HOSTED_KEY = "self_hosted"
UNKNOWN_KEY = "unknown"

REGISTRY: Dict[str, Type[BaseAdapter]] = {
    "nous": NousAdapter,
    "openrouter": OpenRouterAdapter,
    "openai-codex": OpenAICodexAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": SelfHostedAdapter,
    SELF_HOSTED_KEY: SelfHostedAdapter,
    UNKNOWN_KEY: SessionAnalyticsAdapter,
}

_SENTINELS = {SELF_HOSTED_KEY, UNKNOWN_KEY}


def is_self_hosted_provider(provider_id: str) -> bool:
    lowered = provider_id.strip().lower()
    return lowered in ("ollama", "custom") or lowered.startswith("custom:")


def resolve_adapter_class(provider_id: str) -> Type[BaseAdapter]:
    """Pick the adapter class for a stored provider id (lookup-only aliasing)."""
    lowered = provider_id.strip().lower()
    if lowered in REGISTRY and lowered not in _SENTINELS:
        return REGISTRY[lowered]
    if is_self_hosted_provider(provider_id):
        return REGISTRY[SELF_HOSTED_KEY]
    return REGISTRY[UNKNOWN_KEY]


def create_adapter(ctx: AdapterContext) -> BaseAdapter:
    return resolve_adapter_class(ctx.provider_id)(ctx)


__all__ = [
    "REGISTRY",
    "AdapterContext",
    "ProviderAdapter",
    "SessionAnalyticsAdapter",
    "SelfHostedAdapter",
    "NousAdapter",
    "OpenRouterAdapter",
    "OpenAICodexAdapter",
    "AnthropicAdapter",
    "resolve_adapter_class",
    "create_adapter",
    "is_self_hosted_provider",
]
