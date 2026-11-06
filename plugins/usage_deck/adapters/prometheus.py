"""Bounded Prometheus text-exposition reader (DESIGN.md §1.5).

Only simple numeric samples are understood. Labels, exemplars, timestamps and
the raw exposition are never exposed; only an allowlist of CPU-percent and
memory-bytes metric names produces output.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

MAX_LINES = 50_000

# Highest priority first. Percent samples (possibly one per core) are averaged.
CPU_PERCENT_NAMES = (
    "process_cpu_percent",
    "cpu_usage_percent",
    "node_cpu_usage_percent",
    "system_cpu_percent",
    "cpu_percent",
)
MEMORY_BYTES_NAMES = (
    "process_resident_memory_bytes",
    "memory_usage_bytes",
    "node_memory_used_bytes",
    "system_memory_used_bytes",
)

_SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)(?:\s+-?\d+)?$"
)


@dataclass(frozen=True)
class Metrics:
    cpu_percent: Optional[float]
    memory_bytes: Optional[int]


def _value(text: str) -> Optional[float]:
    lowered = text.lower()
    if lowered in ("nan", "+inf", "-inf", "inf"):
        return float("nan")  # marker for rejection
    try:
        return float(text)
    except ValueError:
        return None


def parse_exposition(text: str) -> Optional[Metrics]:
    """Return ``Metrics`` for a valid exposition, ``None`` when it is not one.

    A valid exposition with no allowlisted samples yields null CPU/memory.
    """
    samples: Dict[str, List[float]] = {}
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            return None
        name, value_text = match.group(1), match.group(3)
        value = _value(value_text)
        if value is None:
            return None  # unparseable number -> not an exposition
        if name in CPU_PERCENT_NAMES or name in MEMORY_BYTES_NAMES:
            if not math.isfinite(value) or value < 0:
                # NaN/Infinity/negative on an allowlisted sample: reject the sample.
                continue
            samples.setdefault(name, []).append(value)
    cpu = None
    for name in CPU_PERCENT_NAMES:
        values = samples.get(name)
        if values:
            cpu = round(sum(values) / len(values), 3)
            break
    memory = None
    for name in MEMORY_BYTES_NAMES:
        values = samples.get(name)
        if values:
            # Several labelled samples cannot be proven disjoint: leave unknown.
            memory = int(values[0]) if len(values) == 1 else None
            break
    return Metrics(cpu, memory)
