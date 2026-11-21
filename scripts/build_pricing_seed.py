#!/usr/bin/env python3
"""Rebuild plugins/usage_deck/pricing_data/seed-v1.json from two captured upstream payloads (design §3).

    python3 scripts/build_pricing_seed.py --litellm L.json --openrouter O.json \\
        --litellm-fetched-at 2026-11-21T00:57:39.000000Z --openrouter-fetched-at 2026-11-21T00:57:40.000000Z

The seed is a *real* snapshot: every usable entry of both payloads, normalized by ``pricing.normalize_*`` (the
same code the runtime refresh uses), never hand-edited. Retrieval times are the ORIGINAL fetch times (seed age
is never install/build time). The build fails when any curated alias target is missing from the result.
``--check`` verifies the committed seed instead of writing it. Stdlib only; no network.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.usage_deck import pricing  # noqa: E402

SEED = ROOT / "plugins" / "usage_deck" / "pricing_data" / pricing.SEED_FILE
ALIASES = ROOT / "plugins" / "usage_deck" / "pricing_data" / pricing.ALIASES_FILE


def build(litellm: bytes, openrouter: bytes, litellm_at, openrouter_at) -> bytes:
    sources, entries = {}, {}
    for sid, payload, at in ((pricing.PRIMARY, litellm, litellm_at), (pricing.SECONDARY, openrouter, openrouter_at)):
        got, skipped = pricing._NORMALIZERS[sid](payload, at)
        print(f"{sid}: {len(got)} usable entries, {skipped} skipped", file=sys.stderr)
        entries.update(got)
        sources[sid] = pricing.SourceMeta(at, at + timedelta(hours=24), hashlib.sha256(payload).hexdigest())
    aliases = pricing.load_aliases_strict(ALIASES)
    missing = sorted({a.catalog_key for a in aliases.values()} - set(entries))
    if missing:
        raise SystemExit("seed build failed: alias targets missing from catalog: " + ", ".join(missing))
    return pricing._serialize_envelope(sources, entries)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--litellm", type=Path, required=True)
    ap.add_argument("--openrouter", type=Path, required=True)
    ap.add_argument("--litellm-fetched-at", required=True)
    ap.add_argument("--openrouter-fetched-at", required=True)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed seed differs from a rebuild")
    args = ap.parse_args(argv)
    out = build(args.litellm.read_bytes(), args.openrouter.read_bytes(),
                pricing.parse_ts(args.litellm_fetched_at), pricing.parse_ts(args.openrouter_fetched_at))
    if args.check:
        if SEED.read_bytes() != out:
            print("seed differs from rebuild", file=sys.stderr)
            return 1
        return 0
    SEED.write_bytes(out)
    print(f"wrote {SEED.relative_to(ROOT)} ({len(out)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
