"""ThreadManager tests: chronological interleave, mode differences."""

from __future__ import annotations

from pathlib import Path

from harness.loader import load_shift, parse_ts
from harness.thread import ThreadManager

BUNDLE = Path(__file__).resolve().parents[1]


def test_history_interleaves_guard_and_baseline_copilot():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    tm = ThreadManager(shift)
    # After several guard messages exist.
    as_of = parse_ts("2026-08-05T03:00:00+00:00")
    hist = tm.history_as_of(as_of, mode="baseline")
    assert hist, "expected conversation by 03:00"
    assert any(m.role == "guard" for m in hist)
    assert any(m.role == "copilot" for m in hist)
    # Chronological.
    assert hist == sorted(hist, key=lambda m: (m.ts, 0 if m.role == "guard" else 1))


def test_shift_mode_starts_empty_of_variant_messages():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    tm = ThreadManager(shift)
    as_of = parse_ts("2026-08-05T03:00:00+00:00")
    hist = tm.history_as_of(as_of, mode="shift")
    assert all(m.role == "guard" for m in hist)


def test_shift_mode_accumulates_variant_messages():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    tm = ThreadManager(shift)
    ts = parse_ts("2026-08-05T02:30:00+00:00")
    tm.record_variant_message(ts, "Variant hello")
    hist = tm.history_as_of(parse_ts("2026-08-05T03:00:00+00:00"), mode="shift")
    assert any(m.role == "copilot" and m.text == "Variant hello" for m in hist)


def test_model_messages_roles():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    tm = ThreadManager(shift)
    msgs = tm.as_model_messages(parse_ts("2026-08-05T03:00:00+00:00"), mode="baseline")
    roles = {m["role"] for m in msgs}
    assert roles <= {"user", "assistant"}
    assert "user" in roles


def test_quiet_shift_has_no_guard_messages():
    shift = load_shift(BUNDLE / "shifts" / "55252.json")
    tm = ThreadManager(shift)
    hist = tm.history_as_of(shift.end, mode="baseline")
    assert all(m.role == "copilot" for m in hist)
