"""Wake schedule replay.

The deliberation gate is out of scope (per the bundle), so the baseline's
recorded `turn_start` entries are the authoritative wake schedule: same turns,
same triggers, same timestamps. Turn numbers are NOT assumed contiguous --
shift 53658 skips turn 11 in the record -- and `turn_skipped` entries (absent
from this bundle but emitted on shifts recorded after 2026-08-24) are carried
through as non-executing wakes so the harness outlives the bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .loader import Shift, parse_ts


@dataclass(frozen=True)
class Wake:
    ts: datetime
    turn: int
    trigger: str
    skipped: bool = False
    skip_reason: str | None = None


def build_schedule(shift: Shift) -> list[Wake]:
    wakes: list[Wake] = []
    for entry in shift.baseline:
        if entry.get("type") == "turn_start":
            wakes.append(
                Wake(
                    ts=parse_ts(entry["ts"]),
                    turn=entry["turn"],
                    trigger=entry["trigger"],
                )
            )
        elif entry.get("type") == "turn_skipped":
            wakes.append(
                Wake(
                    ts=parse_ts(entry["ts"]),
                    turn=entry.get("turn", -1),
                    trigger=entry.get("trigger", "unknown"),
                    skipped=True,
                    skip_reason=entry.get("reason"),
                )
            )
    wakes.sort(key=lambda w: w.ts)
    return wakes
