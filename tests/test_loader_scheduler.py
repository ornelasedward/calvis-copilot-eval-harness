"""Loader and scheduler tests pinned to facts verified directly from the
bundle (turn counts, event counts, the missing turn 11 on shift 53658, the
1:1 copilot_message/request_copilot_dm correspondence)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.loader import load_all_shifts, load_shift, parse_ts
from harness.scheduler import build_schedule

BUNDLE = Path(__file__).resolve().parents[1]
SHIFTS = BUNDLE / "shifts"

# Verified by direct count of turn_start entries.
EXPECTED_TURNS = {
    "46116": 133,
    "50340": 24,
    "50737": 41,
    "50833": 16,
    "50837": 13,
    "53658": 13,
    "55252": 11,
    "56212": 22,
    "56370": 41,
    "58349": 102,
}


@pytest.fixture(scope="module")
def shifts():
    return load_all_shifts(SHIFTS)


def test_all_ten_shifts_load(shifts):
    assert set(shifts) == set(EXPECTED_TURNS)


def test_wake_schedule_counts(shifts):
    for shift_id, expected in EXPECTED_TURNS.items():
        schedule = build_schedule(shifts[shift_id])
        assert len(schedule) == expected, shift_id
        assert all(not w.skipped for w in schedule)


def test_schedule_is_time_ordered(shifts):
    for shift in shifts.values():
        schedule = build_schedule(shift)
        assert schedule == sorted(schedule, key=lambda w: w.ts)


def test_shift_53658_tolerates_missing_turn_number(shifts):
    turns = [w.turn for w in build_schedule(shifts["53658"])]
    assert 11 not in turns
    assert max(turns) == 14 and len(turns) == 13


def test_event_store_time_travel():
    shift = load_shift(SHIFTS / "56370.json")
    assert len(shift.events) == 2007
    # No future leakage: as_of at shift start must exclude later events.
    at_start = shift.events.as_of(shift.start)
    assert at_start and all(e.ts <= shift.start for e in at_start)
    everything = shift.events.as_of(shift.end)
    assert len(everything) <= len(shift.events)


def test_fixture_store_exact_match_only():
    shift = load_shift(SHIFTS / "56370.json")
    calls = shift.fixtures.calls
    assert calls, "expected recorded tool calls"
    sample = next(c for c in calls if c.tool == "mcp__calvis__get_site_history")
    hit = shift.fixtures.exact(sample.tool, sample.input)
    assert hit is not None and hit.output == sample.output
    # A perturbed input must miss -- no nearest-match behavior.
    perturbed = dict(sample.input, days=99)
    assert shift.fixtures.exact(sample.tool, perturbed) is None
    # Time-scoped lookup: nothing recorded before the first call's moment.
    first_ts = min(c.ts for c in calls)
    assert shift.fixtures.exact(
        sample.tool, sample.input, as_of=parse_ts("2026-08-04T00:00:00+00:00")
    ) is None or first_ts <= parse_ts("2026-08-04T00:00:00+00:00")


def test_session_id_recovered_from_fixtures():
    shift = load_shift(SHIFTS / "56370.json")
    assert shift.fixtures.session_id() == "16e10cc0-7f46-41c5-83e0-15c49e07deff"


def test_guard_roster_recovered_from_fixtures():
    shift = load_shift(SHIFTS / "56370.json")
    roster = shift.fixtures.guard_roster()
    assert {"id": 9674, "name": "Hector Nguyen"} in roster


def test_copilot_messages_mirror_dm_calls():
    for path in sorted(SHIFTS.glob("*.json")):
        shift = load_shift(path)
        dm_calls = [
            c for c in shift.fixtures.calls
            if c.tool == "mcp__calvis__request_copilot_dm"
        ]
        messages = shift.baseline_entries("copilot_message")
        assert len(dm_calls) == len(messages), shift.id
