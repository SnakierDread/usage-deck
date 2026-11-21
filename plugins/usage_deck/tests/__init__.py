"""UsageDeck backend tests.

Hermetic by construction: the service's default Anthropic token resolver drives Hermes' REAL credential pool
(refresh + auth-store writes), so every test here gets the pass-through double unless it injects its own.
"""

from plugins.usage_deck import pricing as _pricing
from plugins.usage_deck.adapters import credentials as _credentials

from .helpers import PassthroughResolver as _PassthroughResolver

_credentials.set_default_factory(_PassthroughResolver)


def _offline_fetch(url, timeout_s, max_bytes):
    """The pricing catalog manager the service builds by default would refresh from the network in the background."""
    raise _pricing.FetchError("network disabled in tests")


_pricing.default_fetch = _offline_fetch
