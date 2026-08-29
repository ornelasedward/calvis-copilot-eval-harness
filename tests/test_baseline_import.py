"""Baseline import and eval smoke tests (zero API spend)."""

from __future__ import annotations

from pathlib import Path

from harness.adapters.replay import iter_baseline_turns
from harness.engine import import_baseline_as_run
from harness.evals import Assertion, evaluate_assertion, summarize_turns
from harness.loader import load_shift

BUNDLE = Path(__file__).resolve().parents[1]


def test_iter_baseline_turns_covers_schedule():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    turns = iter_baseline_turns(shift)
    assert len(turns) == 41
    assert turns[0].trigger == "session_start"
    # Turn 1 should include bootstrap actions that precede turn_start.
    assert turns[0].tool_calls or turns[0].messages


def test_import_baseline_counts_for_56370():
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    results = import_baseline_as_run(shift, BUNDLE / "prompts", "test_baseline")
    assert len(results) == 41
    summary = summarize_turns([r.to_dict() for r in results])
    assert summary["dms"] == 31
    assert summary["escalations"].get("human", 0) == 2
    assert summary["escalations"].get("ops", 0) == 1


def test_import_quiet_shift_55252():
    shift = load_shift(BUNDLE / "shifts" / "55252.json")
    results = import_baseline_as_run(shift, BUNDLE / "prompts", "test_quiet")
    summary = summarize_turns([r.to_dict() for r in results])
    assert summary["turns"] == 11
    assert summary["dms"] == 6
    # Silent guard — no guard_message turns.
    assert summary["guard_message_turns"] == 0


def test_assertion_guard_reply_rate():
    """Baseline itself is not perfect — reply rate is a measured reference."""
    shift = load_shift(BUNDLE / "shifts" / "56370.json")
    results = import_baseline_as_run(shift, BUNDLE / "prompts", "test_assert")
    turns = [r.to_dict() for r in results]
    summary = summarize_turns(turns)
    # Document the real baseline rate; assertion checks it's computable.
    assert summary["guard_message_turns"] == 23
    assert 0.8 <= summary["guard_reply_rate"] <= 1.0
    a = Assertion(
        id="always-answer",
        description="Guard reply rate stays at or above baseline",
        trigger="guard_message",
        metric="guard_reply_rate",
        expect={"gte_baseline": True},
    )
    r = evaluate_assertion(a, turns, turns)
    assert r.passed, r.detail


def test_mvp_shift_set_importable():
    for sid in ("56370", "55252", "50737"):
        shift = load_shift(BUNDLE / "shifts" / f"{sid}.json")
        results = import_baseline_as_run(shift, BUNDLE / "prompts", f"t_{sid}")
        assert results
