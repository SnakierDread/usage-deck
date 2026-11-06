"""UsageDeck plugin. Importing this package has no side effects.

The dashboard backend lives in ``plugin_api.py``. ``register`` is the agent-plugin entry point the
Hermes plugin loader calls: it only subscribes the opt-in quota-header observer (DESIGN.md §12.1),
which does nothing unless ``usage_deck.capture_anthropic_headers`` is ``true`` for the profile that
made the request.
"""


def register(ctx):
    from . import observer

    observer.register(ctx)
