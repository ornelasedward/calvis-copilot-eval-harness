"""Shift bundle loading: time-indexed events and exact-match tool fixtures.

Everything the replay engine knows about a shift comes through here. Two hard
rules enforced at this layer:

* Reads are only answerable "as of" a timestamp -- no future data can leak.
* Fixture lookups are exact-match on (tool, canonical input). There is no
  nearest-match fallback; a miss is a miss and the caller must surface it.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def parse_ts(value: str) -> datetime:
    """Bundle timestamps are ISO-8601 with explicit offsets (always +00:00)."""
    return datetime.fromisoformat(value)


def canonical_input(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Event:
    ts: datetime
    type: str
    data: dict

    @classmethod
    def from_raw(cls, raw: dict) -> "Event":
        return cls(ts=parse_ts(raw["ts"]), type=raw["type"], data=raw)


class EventStore:
    """All shift events, sorted by time, queryable only up to a cutoff."""

    def __init__(self, raw_events: Iterable[dict]):
        self._events = sorted(
            (Event.from_raw(e) for e in raw_events), key=lambda e: e.ts
        )
        self._ts_index = [e.ts for e in self._events]

    def __len__(self) -> int:
        return len(self._events)

    def as_of(self, ts: datetime, types: set[str] | None = None) -> list[Event]:
        cut = bisect_right(self._ts_index, ts)
        events = self._events[:cut]
        if types is not None:
            events = [e for e in events if e.type in types]
        return events

    def between(
        self,
        start: datetime | None,
        end: datetime,
        types: set[str] | None = None,
    ) -> list[Event]:
        lo = 0 if start is None else bisect_right(self._ts_index, start)
        hi = bisect_right(self._ts_index, end)
        events = self._events[lo:hi]
        if types is not None:
            events = [e for e in events if e.type in types]
        return events


@dataclass(frozen=True)
class FixtureCall:
    ts: datetime
    tool: str
    input: dict
    output: Any


class FixtureStore:
    """Recorded production tool calls, indexed for exact-match replay."""

    def __init__(self, baseline: Iterable[dict]):
        self._calls: list[FixtureCall] = [
            FixtureCall(
                ts=parse_ts(e["ts"]),
                tool=e["tool"],
                input=e.get("input") or {},
                output=e.get("output"),
            )
            for e in baseline
            if e.get("type") == "tool_call"
        ]
        self._by_key: dict[tuple[str, str], list[FixtureCall]] = {}
        self._by_tool: dict[str, list[FixtureCall]] = {}
        for call in self._calls:
            key = (call.tool, canonical_input(call.input))
            self._by_key.setdefault(key, []).append(call)
            self._by_tool.setdefault(call.tool, []).append(call)

    def __len__(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[FixtureCall]:
        return list(self._calls)

    def tools(self) -> set[str]:
        return set(self._by_tool)

    def exact(
        self, tool: str, tool_input: dict, as_of: datetime | None = None
    ) -> FixtureCall | None:
        """Exact-match lookup. With `as_of`, only calls recorded at or before
        that moment are eligible (latest such call wins); this keeps fixture
        replay time-safe for tools whose answers evolve over the shift."""
        matches = self._by_key.get((tool, canonical_input(tool_input)), [])
        if as_of is not None:
            matches = [c for c in matches if c.ts <= as_of]
        return matches[-1] if matches else None

    def session_id(self) -> str | None:
        """The production session id, as recorded in tool-call inputs. Replays
        reuse it so exact-match fixture lookups keyed on input succeed."""
        for call in self._calls:
            sid = call.input.get("session_id")
            if sid:
                return sid
        return None

    def guard_roster(self) -> list[dict]:
        """Guard identities are absent from the `shift` object; recover them
        from recorded tool outputs (get_site_history / get_guard_locations)."""
        roster: dict[int, str] = {}
        for call in self._calls:
            output = call.output
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except (ValueError, TypeError):
                    continue
            if not isinstance(output, dict):
                continue
            for entry in output.get("returning_guards") or []:
                if entry.get("guard_id") is not None:
                    roster.setdefault(entry["guard_id"], entry.get("name", ""))
            for entry in output.get("guards") or []:
                if entry.get("guard_id") is not None:
                    roster.setdefault(entry["guard_id"], entry.get("guard_name", ""))
        return [{"id": gid, "name": name} for gid, name in roster.items()]


@dataclass
class Shift:
    id: str
    path: Path
    context: dict          # the `shift` object: briefing material
    events: EventStore
    baseline: list[dict]   # raw baseline entries, in recorded order
    fixtures: FixtureStore
    script: dict | None = None  # scripted-guard scenario (experiments/fixtures)
    start: datetime = field(init=False)
    end: datetime = field(init=False)
    timezone: str = field(init=False)

    def __post_init__(self) -> None:
        self.start = parse_ts(self.context["start"])
        self.end = parse_ts(self.context["end"])
        self.timezone = self.context["timezone"]

    def baseline_entries(self, type_: str) -> list[dict]:
        return [e for e in self.baseline if e.get("type") == type_]


def load_shift(path: str | Path) -> Shift:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    baseline = raw.get("baseline") or []
    return Shift(
        id=str(raw["shift"]["id"]),
        path=path,
        context=raw["shift"],
        events=EventStore(raw.get("events") or []),
        baseline=baseline,
        fixtures=FixtureStore(baseline),
        script=raw.get("script"),
    )


def load_all_shifts(shifts_dir: str | Path) -> dict[str, Shift]:
    shifts = {}
    for path in sorted(Path(shifts_dir).glob("*.json")):
        shift = load_shift(path)
        shifts[shift.id] = shift
    return shifts
