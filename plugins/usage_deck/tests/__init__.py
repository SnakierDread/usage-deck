"""UsageDeck backend tests.

Hermetic by construction: the service's default Anthropic token resolver drives Hermes' REAL credential pool
(refresh + auth-store writes), so every test here gets the pass-through double unless it injects its own.
"""

from plugins.usage_deck.adapters import credentials as _credentials

from .helpers import PassthroughResolver as _PassthroughResolver

_credentials.set_default_factory(_PassthroughResolver)
