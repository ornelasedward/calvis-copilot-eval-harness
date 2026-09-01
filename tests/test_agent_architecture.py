"""Architecture locks for the eval-loop agent. No live API, no miner yet."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.agent.catalog import CLASS_CATALOG, assert_catalog_complete
from harness.agent.diagnose import diagnose, diagnose_deterministic
from harness.agent.errors import SessionTodo
from harness.agent.evaluate import evaluate_diagnosis
from harness.agent.mine import mine_shift
from harness.agent.orchestrator import run_loop
from harness.agent.patch import apply_patch
from harness.agent.policy import assess_lift, decide
from harness.agent.types import (
    PROBLEM_CLASSES,
    Evidence,
    LoopConfig,
    PatchPlan,
    ProblemCard,
    ScoreCard,
)


def _card(problem_class="unverified_claim", severity="lift", shift="50737", turns=None):
    return ProblemCard(
        id=f"{shift}-{problem_class}",
        shift_id=shift,
        turns=turns or [6],
        problem_class=problem_class,
        severity=severity,
        evidence=Evidence(
            guard_text="everything is clear",
            missing_tools=["get_guard_locations"],
            event_indexes=[167],
        ),
        policy_files=["instructions/guard_response.md"],
        source="json",
    )


def test_catalog_covers_every_problem_class():
    assert_catalog_complete()
    assert set(CLASS_CATALOG) == set(PROBLEM_CLASSES)


def test_card_rejects_persona_source_and_empty_evidence():
    with pytest.raises(ValueError, match="json"):
        ProblemCard(
            id="x",
            shift_id="1",
            turns=[1],
            problem_class="unverified_claim",
            severity="lift",
            evidence=Evidence(guard_text="hi"),
            policy_files=[],
            source="persona",  # type: ignore[arg-type]
        ).validate()
    with pytest.raises(ValueError, match="evidence"):
        ProblemCard(
            id="x",
            shift_id="1",
            turns=[1],
            problem_class="unverified_claim",
            severity="lift",
            evidence=Evidence(),
            policy_files=[],
        ).validate()


def test_diagnose_skip_llm_picks_safety_before_lift():
    cards = [
        _card("unverified_claim", "lift", "50737", [6]),
        _card("under_escalation", "safety", "55252", [5]),
    ]
    d = diagnose(cards, skip_llm=True)
    assert d.problem_class == "under_escalation"
    assert d.scorer == "escalation_focus"
    assert d.card_id == "55252-under_escalation"
    assert d.spec.require_escalation is True


def test_diagnose_llm_path_is_session_b():
    with pytest.raises(SessionTodo) as ei:
        diagnose([_card()], skip_llm=False)
    assert ei.value.session == "B"


def test_mine_is_implemented_and_patch_and_evaluate_are_session_todos():
    # Session A is live: cards come from the shift JSON, no API call.
    cards = mine_shift("50737")
    assert cards and all(c.source == "json" for c in cards)
    for c in cards:
        c.validate()
    d = diagnose_deterministic([_card()])
    with pytest.raises(SessionTodo) as c:
        apply_patch(d)
    assert c.value.session == "C"
    with pytest.raises(SessionTodo) as ev:
        evaluate_diagnosis(d, PatchPlan(variant_dir="x", changed_file="y", diff=""))
    assert ev.value.session == "D"


def test_decide_keep_requires_preserve_and_not_failed_holdout():
    keep = decide(
        ScoreCard(targeted_pass=True, preserve_pass=True, holdout_pass=True),
        iteration=1,
        max_iterations=3,
    )
    assert keep.action == "keep"
    revert_holdout = decide(
        ScoreCard(targeted_pass=True, preserve_pass=True, holdout_pass=False),
        iteration=1,
        max_iterations=3,
    )
    assert revert_holdout.action == "revert"
    next_card = decide(
        ScoreCard(targeted_pass=False, preserve_pass=True, holdout_pass=None),
        iteration=1,
        max_iterations=3,
    )
    assert next_card.action == "next_card"
    stop = decide(
        ScoreCard(targeted_pass=False, preserve_pass=True, holdout_pass=None),
        iteration=3,
        max_iterations=3,
    )
    assert stop.action == "stop"


def test_spec_rate_is_process_on_frozen_turns_not_guard_outcomes():
    from harness.agent.catalog import process_spec_for
    from harness.agent.spec import spec_rate

    spec = process_spec_for("unverified_claim")
    control = [
        {
            "tools_used": [{"tool": "request_copilot_dm"}],
            "messages": [{"body": "Great work"}],
            "escalations": [],
        }
    ]
    variant = [
        {
            "tools_used": [{"tool": "mcp__calvis__get_guard_locations"}],
            "messages": [{"body": "Thanks, can you walk the north lot?"}],
            "escalations": [],
        }
    ]
    c_rate = spec_rate(control, spec)
    v_rate = spec_rate(variant, spec)
    assert c_rate == 0.0
    assert v_rate == 1.0
    lift = assess_lift(c_rate, v_rate)
    assert lift["targeted_pass"] is True
    assert lift["headroom"] is True


def test_decide_uses_spec_rates_for_lift_regress_and_no_headroom():
    keep = decide(
        ScoreCard(
            targeted_pass=False,
            preserve_pass=True,
            holdout_pass=True,
            control_spec_rate=0.5,
            variant_spec_rate=1.0,
        ),
        iteration=1,
        max_iterations=3,
    )
    assert keep.action == "keep"
    revert = decide(
        ScoreCard(
            targeted_pass=True,
            preserve_pass=True,
            holdout_pass=True,
            control_spec_rate=0.8,
            variant_spec_rate=0.4,
        ),
        iteration=1,
        max_iterations=3,
    )
    assert revert.action == "revert"
    no_headroom = decide(
        ScoreCard(
            targeted_pass=True,
            preserve_pass=True,
            holdout_pass=True,
            control_spec_rate=1.0,
            variant_spec_rate=1.0,
        ),
        iteration=1,
        max_iterations=3,
    )
    assert no_headroom.action == "next_card"


def test_dry_run_loop_writes_plan(tmp_path):
    cfg = LoopConfig(shift_id="50737", dry_run=True)
    result = run_loop(cfg, root=tmp_path)
    assert result["decision"] is None
    plan = result["plan"]
    assert plan["status"] == "architecture"
    assert [s["session"] for s in plan["pipeline"]] == ["A", "B", "C", "D", "done"]
    out = Path(result["out_dir"])
    assert out.exists()
    manifest = (out / "manifest.json").read_text(encoding="utf-8")
    assert "no personas" in manifest
    assert "unverified_claim" in manifest


def test_live_loop_runs_the_miner_then_stops_at_session_b(tmp_path):
    cfg = LoopConfig(shift_id="50737", dry_run=False)
    with pytest.raises(SessionTodo) as ei:
        run_loop(cfg, root=tmp_path)
    assert ei.value.session == "B"
