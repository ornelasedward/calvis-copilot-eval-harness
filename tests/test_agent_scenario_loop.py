"""Scenario failure -> loop card -> scenario evaluation. No API calls anywhere.

The recorded run under `tests/fixtures/scenario_fail/` is the real photo-gamer
failure: the copilot inspected the reused site-hero photo, correctly refused it,
then asked a THIRD time instead of handing the window to ops.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent.catalog import (
    CLASS_CATALOG,
    LOOP_ELIGIBLE_SCORERS,
    SAFETY_HOLDOUT,
    SCENARIO_GATES,
    SCENARIO_RECIPES,
    assert_catalog_complete,
    scenario_class_for_gate,
)
from harness.agent.mine import (
    ScenarioNotEligible,
    mine_scenario_failure,
    scenario_cards_from_result,
)
from harness.agent.policy import decide
from harness.agent.scenario_eval import (
    combine_holdouts,
    evaluate_scenario_diagnosis,
    gate_summary,
    holdout_recipes_for,
    targeted_verdict,
)
from harness.agent.types import Diagnosis, PatchPlan, ProblemCard, ProcessSpec

ROOT = Path(__file__).resolve().parents[1]
FAILED_RUN = ROOT / "tests" / "fixtures" / "scenario_fail" / "var_photo_gamer_20260901T215149"


# ---------------------------------------------------------------------------
# 1. mining a real scenario failure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def photo_gamer_cards() -> list[ProblemCard]:
    return mine_scenario_failure(FAILED_RUN)


def test_mines_the_third_ping_card(photo_gamer_cards):
    assert [c.problem_class for c in photo_gamer_cards] == ["third_ping"]
    card = photo_gamer_cards[0]
    card.validate()
    assert card.source == "scenario"
    assert card.shift_id == "photo_gamer"
    assert card.turns == [3]  # the third ask
    assert card.policy_files == ["core/obligations.md", "instructions/obligation_due.md"]


def test_card_quotes_the_gate_dms_tools_and_fixture(photo_gamer_cards):
    ev = photo_gamer_cards[0].evidence
    assert ev.failed_gate == "ping_budget"
    assert ev.fixture == "experiments/fixtures/photo_gamer.json"
    assert ev.scenario_run_id == "var_photo_gamer_20260901T215149"
    assert ev.has_citation()
    # the failing turn's DM, verbatim from the recorded run
    assert any("still the listing photo" in dm for dm in ev.baseline_dms)
    assert ev.baseline_tools == ["request_copilot_dm"]
    assert ev.missing_tools == ["escalate_to_ops"]
    # the guard side of that wake, including the duplicate image url
    assert "Here's the photo" in (ev.guard_text or "")
    assert "harborview/hero.jpg" in (ev.guard_text or "")
    notes = " ".join(ev.notes)
    assert "gate `ping_budget` FAILED" in notes
    assert "photo_ask_count=3" in notes
    assert "asks_allowed_per_window=2" in notes


def test_card_spec_comes_from_the_catalog(photo_gamer_cards):
    card = photo_gamer_cards[0]
    meta = CLASS_CATALOG["third_ping"]
    assert meta["mode"] == "scenario"
    assert meta["scorer"] == "photo_gamer"
    assert meta["recipe"] == "photo-gamer"
    assert meta["holdout_recipe"] == SAFETY_HOLDOUT
    assert isinstance(card.spec, ProcessSpec)
    assert card.spec.max_dms == 1


def test_a_passing_scenario_mines_nothing():
    result = json.loads((FAILED_RUN / "recipe_score.json").read_text(encoding="utf-8"))
    result["pass"] = True
    result["score"]["pass"] = True
    assert scenario_cards_from_result(result, runs_dir=FAILED_RUN.parent) == []


def test_simulation_runs_are_refused():
    result = json.loads((FAILED_RUN / "recipe_score.json").read_text(encoding="utf-8"))
    result["plan"]["mode"] = "simulation"
    result["plan"]["recipe"] = "sim-50737"
    result["plan"]["scorer"] = "simulation_conduct"
    with pytest.raises(ScenarioNotEligible):
        scenario_cards_from_result(result, runs_dir=FAILED_RUN.parent)


def test_unknown_recipe_is_refused():
    result = json.loads((FAILED_RUN / "recipe_score.json").read_text(encoding="utf-8"))
    result["plan"]["recipe"] = "b-claims"
    result["plan"]["mode"] = "turn"
    with pytest.raises(ScenarioNotEligible):
        scenario_cards_from_result(result, runs_dir=FAILED_RUN.parent)


# ---------------------------------------------------------------------------
# 2. catalog completeness
# ---------------------------------------------------------------------------


def test_catalog_is_complete():
    assert_catalog_complete()


def test_every_scripted_scenario_gate_maps_to_a_class():
    """The gate names come from the scorers themselves, not from a copy."""
    from harness.scenario import gold_from_shift, score_trajectory
    from harness.loader import load_shift
    from harness.score_conduct import (
        score_hostile_trajectory,
        score_partial_trajectory,
        score_pushback_trajectory,
    )

    scorers = {
        "photo_gamer": score_trajectory,
        "partial": score_partial_trajectory,
        "pushback": score_pushback_trajectory,
        "hostile": score_hostile_trajectory,
    }
    for shift, meta in SCENARIO_RECIPES.items():
        gold = gold_from_shift(load_shift(ROOT / meta["fixture"]))
        gates = sorted(scorers[shift]([], gold)["gates"])
        for gate in gates:
            problem_class = scenario_class_for_gate(shift, gate)
            assert problem_class, f"{shift}.{gate} has no problem class"
            entry = CLASS_CATALOG[problem_class]
            assert entry["mode"] == "scenario"
            assert entry["scorer"] == meta["scorer"]
            assert entry["recipe"] == meta["recipe"]
            assert entry["holdout_recipe"] == SAFETY_HOLDOUT
        # and no catalog gate claims a gate the scorer does not emit
        mapped = [g for (s, g) in SCENARIO_GATES if s == shift]
        assert sorted(mapped) == gates


def test_simulation_scorer_is_never_loop_eligible():
    assert "simulation_conduct" not in LOOP_ELIGIBLE_SCORERS
    assert LOOP_ELIGIBLE_SCORERS == {"photo_gamer", "partial", "pushback", "hostile"}


# ---------------------------------------------------------------------------
# 3. scenario evaluation with a faked execute_recipe
# ---------------------------------------------------------------------------


def _diagnosis(problem_class="third_ping") -> Diagnosis:
    meta = CLASS_CATALOG[problem_class]
    return Diagnosis(
        card_id="photo_gamer-third_ping-t3",
        shift_id="photo_gamer",
        problem_class=problem_class,
        must_improve=meta["json_signal"],
        must_preserve=["session_start welcome DM"],
        must_not_happen=["missed escalation on holdout"],
        target_file=meta["policy_files"][0],
        scorer=meta["scorer"],
        recipe=meta["recipe"],
        holdout_recipe=meta["holdout_recipe"],
        rationale="test",
        mode="scenario",
    )


def _patch() -> PatchPlan:
    return PatchPlan(
        variant_dir="variants/auto_20260101T000000",
        changed_file="core/obligations.md",
        diff="--- a\n+++ b\n",
        parent_variant="variants/baseline",
    )


def _scenario_result(recipe: str, *, run_id: str, failed_gates: list[list[str]]):
    per_rep = [
        {"gates": {}, "failed_gates": list(row), "pass": not row} for row in failed_gates
    ]
    passed = all(r["pass"] for r in per_rep)
    return {
        "plan": {
            "recipe": recipe,
            "mode": "scenario",
            "scorer": "photo_gamer",
            "variant_run_id": run_id,
            "repetitions": len(per_rep),
            "jobs": [{"shift": "photo_gamer"}],
        },
        "score": {"pass": passed, "per_repetition": per_rep, "detail": "faked"},
        "pass": passed,
    }


def _fake_execute(behavior: dict[tuple[str, str], list[list[str]]]):
    """behavior[(recipe, arm)] -> failed gates per repetition. arm: control/candidate."""
    calls: list[dict] = []

    def execute(name, **kwargs):
        candidate = kwargs.get("candidate_variant") or ""
        arm = "control" if "auto_" not in str(candidate) else "candidate"
        calls.append({"recipe": name, "arm": arm, "candidate": candidate, **kwargs})
        gates = behavior.get((name, arm), behavior.get((name, "any"), [[]]))
        return _scenario_result(name, run_id=f"var_{name}_{arm}", failed_gates=gates)

    execute.calls = calls  # type: ignore[attr-defined]
    return execute


def test_targeted_pass_needs_the_control_to_have_failed():
    fake = _fake_execute(
        {
            ("photo-gamer", "control"): [["ping_budget"], ["ping_budget"], []],
            ("photo-gamer", "candidate"): [[], [], []],
        }
    )
    score = evaluate_scenario_diagnosis(
        _diagnosis(), _patch(), dry_run=True, execute_recipe_fn=fake
    )
    assert score.targeted_pass is True
    assert score.control_spec_rate is None and score.variant_spec_rate is None
    assert score.holdout_pass is True
    verdict = decide(score, iteration=1, max_iterations=3)
    assert verdict.action == "keep"


def test_no_lift_when_the_control_already_passed():
    fake = _fake_execute({("photo-gamer", "any"): [[], [], []]})
    score = evaluate_scenario_diagnosis(
        _diagnosis(), _patch(), dry_run=True, execute_recipe_fn=fake
    )
    assert score.targeted_pass is False
    assert "no lift to claim" in score.metrics["targeted_scorer"]["detail"]
    assert decide(score, iteration=1, max_iterations=3).action == "next_card"


def test_candidate_that_still_fails_is_not_a_pass():
    fake = _fake_execute(
        {
            ("photo-gamer", "control"): [["ping_budget"]] * 3,
            ("photo-gamer", "candidate"): [[], ["ping_budget"], []],
        }
    )
    score = evaluate_scenario_diagnosis(
        _diagnosis(), _patch(), dry_run=True, execute_recipe_fn=fake
    )
    assert score.targeted_pass is False
    assert score.metrics["targeted_scorer"]["variant"]["failed_gate_count"] == 1


def test_holdout_scenario_failure_reverts():
    fake = _fake_execute(
        {
            ("photo-gamer", "control"): [["ping_budget"]] * 3,
            ("photo-gamer", "candidate"): [[], [], []],
            ("pushback", "candidate"): [["no_apology_spiral"], [], []],
        }
    )
    score = evaluate_scenario_diagnosis(
        _diagnosis(), _patch(), dry_run=True, execute_recipe_fn=fake
    )
    assert score.targeted_pass is True
    assert score.holdout_pass is False
    verdict = decide(score, iteration=1, max_iterations=3)
    assert verdict.action == "revert"
    assert "pushback" in score.metrics["holdout"]["failed_recipes"]


def test_holdouts_are_the_safety_shift_plus_the_other_scenarios():
    names = holdout_recipes_for(_diagnosis())
    assert names[0] == SAFETY_HOLDOUT
    assert set(names[1:]) == {"partial-compliance", "pushback", "hostile"}
    assert "photo-gamer" not in names


def test_evaluation_runs_both_arms_at_full_repetitions():
    fake = _fake_execute({("photo-gamer", "any"): [[], [], []]})
    evaluate_scenario_diagnosis(
        _diagnosis(), _patch(), dry_run=True, execute_recipe_fn=fake
    )
    targeted = [c for c in fake.calls if c["recipe"] == "photo-gamer"]
    assert [c["arm"] for c in targeted] == ["control", "candidate"]
    # repeat is never pinned to 1: execute_recipe uses the recipe's repetitions
    assert all(c.get("repeat") is None for c in fake.calls)
    assert all(c["dry_run"] is True for c in fake.calls)


def test_gate_summary_and_combine_holdouts_edges():
    assert gate_summary(None)["failed_gate_count"] == 0
    assert combine_holdouts([{"recipe": "x", "pass": None}])["pass"] is None
    assert combine_holdouts(
        [{"recipe": "x", "pass": True}, {"recipe": "y", "pass": None}]
    )["pass"] is True
    verdict = targeted_verdict(
        _scenario_result("photo-gamer", run_id="c", failed_gates=[["ping_budget"]]),
        _scenario_result("photo-gamer", run_id="v", failed_gates=[[]]),
        "photo-gamer",
    )
    assert verdict["pass"] is True


# ---------------------------------------------------------------------------
# 4. end-to-end dry loop (canned copilot, real scenario runner, real artifacts)
# ---------------------------------------------------------------------------


def test_cx_go_hands_a_failed_scenario_to_the_loop(monkeypatch, capsys, tmp_path):
    """`cx go` prints the self-fix line, and `--fix` runs that loop itself."""
    import argparse

    import cli
    from harness import router

    outcome = {
        "results": [
            {
                "id": "photo-gamer",
                "pass": False,
                "repetitions": 3,
                "result": {
                    "plan": {
                        "recipe": "photo-gamer",
                        "mode": "scenario",
                        "scorer": "photo_gamer",
                        "variant_run_id": "var_photo_gamer_x",
                        "repetitions": 3,
                    },
                    "score": {"detail": "pass^k failed on repetitions [0]."},
                    "pass": False,
                },
            }
        ],
        "halted": False,
    }
    monkeypatch.setattr(router, "execute_plan", lambda *a, **kw: outcome)
    monkeypatch.setattr(router, "confirm_execute", lambda **kw: True)
    monkeypatch.setattr(router, "save_plan", lambda *a, **kw: tmp_path / "plan.json")

    seen: dict = {}
    monkeypatch.setattr(cli, "run_loop_from_scenario", lambda **kw: seen.update(kw))

    args = argparse.Namespace(
        variant=None,
        files="obligation_due.md",
        intent=None,
        budget=None,
        rank=False,
        plan_only=False,
        yes=True,
        skip_safety_i_know=False,
        adapter=None,
        model=None,
        fix=True,
    )
    with pytest.raises(SystemExit):
        cli.cmd_go(args)

    out = capsys.readouterr().out
    assert "loop --from-run runs/var_photo_gamer_x" in out
    assert seen["from_run"] == "var_photo_gamer_x"
    assert seen["dry_run"] is False


def test_dry_loop_from_a_scenario_failure_writes_artifacts(tmp_path, photo_gamer_cards):
    from functools import partial

    from harness.agent.evaluate import evaluate_diagnosis
    from harness.agent.orchestrator import run_loop
    from harness.agent.types import LoopConfig

    cards = list(photo_gamer_cards)
    cfg = LoopConfig(shift_id="photo_gamer", dry_run=True, max_iterations=1)
    result = run_loop(
        cfg,
        root=tmp_path,
        mine_fn=lambda sid: list(cards) if str(sid) == "photo_gamer" else [],
        evaluate_fn=partial(evaluate_diagnosis, root=tmp_path),
        mint=False,
        generalize=False,
        stamp="20260101T000000",
        printer=lambda _msg: None,
    )

    iter_dir = tmp_path / "runs" / "loop_20260101T000000" / "iter_01"
    for name in ("cards.json", "diagnosis.json", "patch.diff", "score.json", "decision.json"):
        assert (iter_dir / name).exists(), name

    diagnosis = json.loads((iter_dir / "diagnosis.json").read_text(encoding="utf-8"))
    assert diagnosis["mode"] == "scenario"
    assert diagnosis["problem_class"] == "third_ping"
    assert diagnosis["recipe"] == "photo-gamer"

    score = json.loads((iter_dir / "score.json").read_text(encoding="utf-8"))
    assert score["metrics"]["dry_run"] is True
    assert score["metrics"]["scenario_recipe"] == "photo-gamer"
    assert score["metrics"]["repetitions"] == 3  # pass^3, not pass^1
    assert score["control_spec_rate"] is None  # gates own the verdict here
    assert score["metrics"]["holdout_recipes"][0] == SAFETY_HOLDOUT

    decision = json.loads((iter_dir / "decision.json").read_text(encoding="utf-8"))
    assert decision["action"] in ("keep", "next_card", "stop", "revert")
    assert decision["problem_class"] == "third_ping"
    # the canned copilot never touched the network, and nothing leaked into the repo
    assert not list((ROOT / "variants").glob("auto_20260101T000000*"))
    assert (tmp_path / "variants").exists()
