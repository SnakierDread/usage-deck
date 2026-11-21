"""A faithful test double of the ``pricing.py`` resolver contract (docs/design/api-equivalent-pricing.md §6).

The UI/integration lane consumes the resolver only through ``resolve_api_equivalent(model_id, input_count,
output_count, *, catalog, aliases, now) -> PriceResolution`` plus ``CatalogSnapshot`` / ``Alias``. This module
implements exactly that arithmetic and reason vocabulary (alias lookup, tokens_missing/tokens_invalid,
alias_target_missing, model_unknown, quantize once per leaf to pico-USD with ROUND_HALF_EVEN, stale after 7 days)
over an in-memory catalog, so the integration tests never touch the network, the disk catalog or live prices.
Heuristic matching is deliberately NOT reimplemented here: that is the resolver card's own test surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Mapping, Optional, Tuple

FETCHED = datetime(2026, 11, 5, 12, 0, 0, 831243, tzinfo=timezone.utc)
STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    source_key: str
    input_usd_per_token: Decimal
    output_usd_per_token: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class Alias:
    catalog_key: str
    kind: str
    note: str


@dataclass(frozen=True)
class CatalogSnapshot:
    entries: Mapping[str, CatalogEntry]
    revision: str
    origin: str
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class PriceResolution:
    api_equivalent_usd: Optional[float]
    amount_pico_usd: Optional[int]
    catalog_key: Optional[str]
    method: str
    alias_kind: Optional[str]
    note: Optional[str]
    reason: Optional[str]
    source: Optional[str]
    fetched_at: Optional[datetime]
    stale: bool


def per_million(value: str) -> Decimal:
    """USD per million tokens (how tariffs are quoted) -> exact USD per token."""
    return Decimal(value) / Decimal(1_000_000)


def entry(key: str, input_per_million: str, output_per_million: str, fetched_at: datetime = FETCHED) -> CatalogEntry:
    source, source_key = key.split(":", 1)
    return CatalogEntry(source, source_key, per_million(input_per_million), per_million(output_per_million), fetched_at)


def snapshot(entries, origin: str = "seed", warnings: Tuple[str, ...] = ()) -> CatalogSnapshot:
    by_key = {f"{e.source}:{e.source_key}": e for e in entries}
    canonical = json.dumps({k: [str(e.input_usd_per_token), str(e.output_usd_per_token), e.fetched_at.isoformat()]
                            for k, e in sorted(by_key.items())}, sort_keys=True, separators=(",", ":"))
    return CatalogSnapshot(by_key, hashlib.sha256(canonical.encode()).hexdigest(), origin, tuple(warnings))


def _valid_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def resolve_api_equivalent(model_id, input_count, output_count, *, catalog: CatalogSnapshot,
                           aliases: Mapping[str, Alias], now: datetime) -> PriceResolution:
    alias = aliases.get(model_id) if isinstance(model_id, str) else None
    if alias is None:
        reason = "model_unknown" if not isinstance(model_id, str) else "no_catalog_match"
        return PriceResolution(None, None, None, "none", None, None, reason, None, None, False)
    target = catalog.entries.get(alias.catalog_key)
    if target is None:
        return PriceResolution(None, None, alias.catalog_key, "alias", alias.kind, alias.note,
                               "alias_target_missing", None, None, False)
    stale = now - target.fetched_at > STALE_AFTER
    identity = (alias.catalog_key, "alias", alias.kind, alias.note)
    if input_count is None or output_count is None:
        reason = "tokens_missing"
    elif not (_valid_count(input_count) and _valid_count(output_count)):
        reason = "tokens_invalid"
    else:
        total = Decimal(input_count) * target.input_usd_per_token + Decimal(output_count) * target.output_usd_per_token
        pico = int((total * 10 ** 12).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        return PriceResolution(float(Decimal(pico) / 10 ** 12), pico, *identity, None, target.source,
                               target.fetched_at, stale)
    return PriceResolution(None, None, *identity, reason, target.source, target.fetched_at, stale)
