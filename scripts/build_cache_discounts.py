#!/usr/bin/env python3
"""Rebuild ``plugins/usage_deck/pricing_data/cache-discounts-v1.json`` from Hermes core's pricing snapshot.

Design: docs/design/analytics-depth-breakdown.md §5. The file is a LOCAL PROJECTION of the official-docs
snapshot rows of ``agent/usage_pricing.py`` at one pinned core commit: for every exact ``(provider, model)``
key of ``_OFFICIAL_DOCS_PRICING`` (tuple aliases already expanded by core's own loader) it keeps the pricing
version, the input rate and the cache-read rate. Rates are decimal strings; a missing cache-read rate is null.
Nothing is fetched at runtime and the runtime never imports Hermes core.

    python3 scripts/build_cache_discounts.py --core-checkout ~/.hermes/hermes-agent          # rewrite
    python3 scripts/build_cache_discounts.py --core-checkout ~/.hermes/hermes-agent --check  # verify bytes
    python3 scripts/build_cache_discounts.py --core-file usage_pricing.py --core-commit <sha> # no git needed

The core module is executed with stubbed ``agent.model_metadata`` / ``utils`` imports (it only needs them for
network helpers this script never calls), so any plain ``python3`` works. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from decimal import Decimal
from pathlib import Path

PIN = "3b421578588243f920886e6fe79a6faa87ea026e"
OUT_REL = Path("plugins") / "usage_deck" / "pricing_data" / "cache-discounts-v1.json"
CORE_REL = "agent/usage_pricing.py"


def load_core(source: str):
    """Execute the core pricing module source with its heavy imports stubbed; return the module."""
    stubs = {
        "agent": types.ModuleType("agent"),
        "agent.model_metadata": types.ModuleType("agent.model_metadata"),
        "utils": types.ModuleType("utils"),
    }
    for name in ("fetch_endpoint_model_metadata", "fetch_model_metadata", "strip_codex_context_variant_suffix"):
        setattr(stubs["agent.model_metadata"], name, lambda *a, **k: None)
    for name in ("base_url_host_matches", "base_url_hostname", "base_url_origin"):
        setattr(stubs["utils"], name, lambda *a, **k: None)
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    name = "_core_usage_pricing_snapshot"
    module = types.ModuleType(name)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    try:
        exec(compile(source, CORE_REL, "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
    return module


def _rate(value) -> "str | None":
    return None if value is None else format(Decimal(value), "f")


def project(module, commit: str) -> dict:
    table = module._OFFICIAL_DOCS_PRICING
    entries = []
    for (provider, model), entry in sorted(table.items()):
        entries.append({
            "provider_id": provider,
            "model": model,
            "pricing_version": entry.pricing_version,
            "input_cost_per_million": _rate(entry.input_cost_per_million),
            "cache_read_cost_per_million": _rate(entry.cache_read_cost_per_million),
        })
    return {"core_commit": commit, "entries": entries}


def render(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild cache-discounts-v1.json from the pinned core snapshot")
    parser.add_argument("--core-checkout", help="Hermes core git checkout (the pinned commit is read with git show)")
    parser.add_argument("--core-file", help="a saved copy of agent/usage_pricing.py at --core-commit")
    parser.add_argument("--core-commit", default=PIN)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / OUT_REL)
    parser.add_argument("--check", action="store_true", help="exit 1 when the committed file differs")
    args = parser.parse_args(argv)
    if bool(args.core_checkout) == bool(args.core_file):
        parser.error("give exactly one of --core-checkout / --core-file")
    if args.core_file:
        source = Path(args.core_file).read_text(encoding="utf-8")
    else:
        source = subprocess.run(["git", "-C", args.core_checkout, "show", f"{args.core_commit}:{CORE_REL}"],
                                check=True, capture_output=True, text=True).stdout
    data = render(project(load_core(source), args.core_commit))
    if args.check:
        current = args.out.read_bytes() if args.out.is_file() else b""
        if current != data:
            print(f"{args.out} differs from the pinned snapshot projection", file=sys.stderr)
            return 1
        print(f"{args.out}: OK ({args.core_commit[:10]})")
        return 0
    args.out.write_bytes(data)
    print(f"wrote {args.out} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
