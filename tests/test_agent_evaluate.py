"""Session D: rate math + ScoreCard assembly. No API calls anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent.catalog import process_spec_for
from harness.agent.diagnose import diagnose_deterministic
from harness.agent.evaluate import (
    FIXTURE_CONTROL_RUN,
    FIXTURE_VARIANT_RUN,
    build_scorecard,
    clause_gates,
    evaluate_diagnosis,
    preserve_verdict,
    resolve_turns,
    run_targeted_scorer,
    select_turns,
)
from harness.agent.policy import decide
from harness.agent.spec import spec_rate
from harness.agent.types import Diagnosis, Evidence, PatchPlan, ProblemCard
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_eval"


def _card(problem_class="unverified_claim", severity="lift", shift="50737", turns=None):
    return ProblemCard(
        id=f"{shift}-{problem_class}",
        shift_id=shift,
        turns=turns or [6, 12, 18],
        problem_class=problem_class,
        severity=severity,
        evidence=Evidence(
            guard_text="all clear on the north lot",
            missing_tools=["get_guard_locations"],
            event_indexes=[167],
        ),
        policy_files=["instructions/guard_response.md"],
    )


def _patch(parent="variants/baseline"):
    return PatchPlan(
        variant_dir="variants/auto_20250101T000000",
        changed_file="instructions/guard_response.md",
        diff="--- a\n+++ b\n",
        parent_variant=parent,
    )


def _turn(n, trigger="guard_message", tools=(), msgs=("ok",), escs=()):
    return {
        "turn": n,
        "trigger": trigger,
        "decision": "send_message" if msgs else "hold",
        "messages": [{"body": m} for m in msgs],
        "tools_used": [{"tool": f"mcp__calvis__{t}"} for t in tools],
        "escalations": [{"kind": e} for e in escs],
    }


# --------------------------------------------------------------------------
# rate math
# --------------------------------------------------------------------------


def test_select_turns_filters_and_orders_frozen_turns():
    rows = [_turn(18), _turn(6), _turn(12), _turn(1, "session_start")]
    assert [t["turn"] for t in select_turns(rows, [6, 12, 18])] == [6, 12, 18]
    assert [t["turn"] for t in select_turns(rows, None)] == [1, 6, 12, 18]
    assert select_turns(rows, [99]) == []


def test_spec_rate_is_fraction_of_card_turns_passing_every_clause():
    spec = process_spec_for("unverified_claim")
    control = [
        _turn(6, tools=["get_job_details"]),
        _turn(12, tools=[]),
        _turn(18, tools=["get_guard_locations"]),
    ]
    variant = [_turn(n, tools=["get_guard_locations"]) for n in (6, 12, 18)]
    assert spec_rate(control, spec) == pytest.approx(1 / 3)
    assert spec_rate(variant, spec) == 1.0
    assert spec_rate([], spec) is None


def test_clause_gates_and_across_turns():
    spec = process_spec_for("unverified_claim")
    mixed = [
        _turn(6, tools=["get_guard_locations"]),
        _turn(12, tools=[], msgs=()),
    ]
    gates = clause_gates(mixed, spec)
    assert gates["must_call:get_guard_locations"] is False
    assert gates["min_dms:1"] is False
    good = [_turn(6, tools=["get_guard_locations"])]
    assert clause_gates(good, spec) == {
        "must_call:get_guard_locations": True,
        "min_dms:1": True,
    }
    assert clause_gates([], spec) == {}


def test_resolve_turns_prefers_card_then_recipe_then_whole_shift():
    d = diagnose_deterministic([_card()])
    assert resolve_turns(d, None, [3, 4]) == ([3, 4], "argument")
    assert resolve_turns(d, _card(turns=[6]), None) == ([6], "card")
    # b-claims pins 50737 to turns 6/12/18 in experiments/recipes.json.
    turns, source = resolve_turns(d, None, None)
    assert turns == [6, 12, 18]
    assert source == "recipe:b-claims"
    shift_only = diagnose_deterministic([_card("under_escalation", "safety", "55252", [5])])
    assert resolve_turns(shift_only, None, None)[1] in {"whole_shift", "recipe:a3-shift-55252"}


def test_preserve_verdict_flags_dropped_welcome_or_reply():
    control = [_turn(1, "session_start"), _turn(6), _turn(12)]
    same = [_turn(1, "session_start"), _turn(6), _turn(12)]
    assert preserve_verdict(control, same)["pass"] is True
    lost_welcome = [_turn(1, "session_start", msgs=()), _turn(6), _turn(12)]
    v = preserve_verdict(control, lost_welcome)
    assert v["pass"] is False and v["welcomes_preserved"] is False
    silent_reply = [_turn(1, "session_start"), _turn(6, msgs=()), _turn(12)]
    silent_reply[1]["decision"] = "hold"
    v2 = preserve_verdict(control, silent_reply)
    assert v2["pass"] is False and v2["replies_preserved"] is False
    # Nothing to preserve on a turn subset with no welcome and no guard reply.
    only_obligation = [_turn(5, "scheduled_check_in", msgs=())]
    v3 = preserve_verdict(only_obligation, only_obligation)
    assert v3["observed"] is False and v3["pass"] is True


def test_unregistered_probe_is_never_a_pass():
    store = ExperimentStore(FIXTURE)
    result = run_targeted_scorer(
        "probe_that_does_not_exist", store, FIXTURE_CONTROL_RUN, FIXTURE_VARIANT_RUN, [{"shift": "50737"}]
    )
    assert result["pass"] is None
    assert "not registered" in result["detail"]
    assert run_targeted_scorer(None, store, "a", "b", [])["pass"] is None


def test_process_spec_probes_are_registered_and_deterministic():
    store = ExperimentStore(FIXTURE)
    for probe in ("photo_inspect", "ping_budget"):
        result = run_targeted_scorer(
            probe, store, FIXTURE_CONTROL_RUN, FIXTURE_VARIANT_RUN, [{"shift": "50737"}]
        )
        assert result["pass"] in (True, False)
        assert result["turns_scored"] > 0
        assert "variant_spec_rate" in result


# --------------------------------------------------------------------------
# ScoreCard assembly
# --------------------------------------------------------------------------


def _score(control, variant, *, scorer_pass=True, holdout=None, diagnosis=None):
    return build_scorecard(
        diagnosis or diagnose_deterministic([_card()]),
        control_rows=control,
        variant_rows=variant,
        scorer_result={"scorer": "verify_b", "pass": scorer_pass},
        holdout=holdout,
        control_run_id="ctrl_x",
        variant_run_id="var_x",
    )


def test_scorecard_lift_comes_from_spec_rates_not_from_the_scorer():
    control = [_turn(6, tools=[]), _turn(12, tools=[])]
    variant = [_turn(6, tools=["get_guard_locations"]), _turn(12, tools=["get_guard_locations"])]
    sc = _score(control, variant, scorer_pass=False, holdout={"pass": True})
    assert sc.control_spec_rate == 0.0
    assert sc.variant_spec_rate == 1.0
    assert sc.targeted_pass is True  # rates own lift, not the scorer's boolean
    assert sc.metrics["lift"]["headroom"] is True
    assert sc.metrics["gates"]["targeted_scorer_pass"] is False
    assert sc.scorer == "verify_b"
    assert sc.control_run_id == "ctrl_x" and sc.variant_run_id == "var_x"
    assert decide(sc, iteration=1, max_iterations=3).action == "keep"


def test_scorecard_regression_and_no_headroom_paths():
    good = [_turn(6, tools=["get_guard_locations"])]
    bad = [_turn(6, tools=[])]
    regressed = _score(good, bad, holdout={"pass": True})
    assert regressed.targeted_pass is False
    assert decide(regressed, iteration=1, max_iterations=3).action == "revert"
    flat = _score(good, good, holdout={"pass": True})
    assert flat.control_spec_rate == 1.0 and flat.variant_spec_rate == 1.0
    assert flat.metrics["lift"]["headroom"] is False
    assert decide(flat, iteration=1, max_iterations=3).action == "next_card"


def test_scorecard_holdout_none_vs_false():
    control = [_turn(6, tools=[])]
    variant = [_turn(6, tools=["get_guard_locations"])]
    none_card = _score(control, variant, holdout=None)
    assert none_card.holdout_pass is None
    assert decide(none_card, iteration=1, max_iterations=3).action == "keep"
    failed = _score(control, variant, holdout={"recipe": "a3-shift-55252", "pass": False})
    assert failed.holdout_pass is False
    assert decide(failed, iteration=1, max_iterations=3).action == "revert"
    unknown = _score(control, variant, holdout={"pass": None, "detail": "scorer raised"})
    assert unknown.holdout_pass is None


def test_scorecard_falls_back_to_scorer_boolean_when_no_turns_scored():
    sc = _score([], [], scorer_pass=True, holdout={"pass": True})
    assert sc.control_spec_rate is None and sc.variant_spec_rate is None
    assert sc.metrics["lift"] is None
    assert sc.targeted_pass is True
    assert decide(sc, iteration=1, max_iterations=3).action == "keep"


def test_scorecard_records_out_of_bounds_claims_and_no_quality_score():
    sc = _score([_turn(6, tools=[])], [_turn(6, tools=["get_guard_locations"])])
    assert "later historical guard replies" in sc.metrics["not_outcomes"]
    assert sc.metrics["spec"]["score_scope"] == "copilot_process"
    blob = json.dumps(sc.to_dict()).lower()
    assert "quality" not in blob  # no invented holistic score


def test_holdout_run_supplies_preserve_evidence():
    """Turn-mode card turns hold no welcome; the holdout shift run does."""
    control = [_turn(6, tools=[])]
    variant = [_turn(6, tools=["get_guard_locations"])]
    sc = build_scorecard(
        diagnose_deterministic([_card()]),
        control_rows=control,
        variant_rows=variant,
        scorer_result={"scorer": "verify_b", "pass": True},
        holdout={"recipe": "a3-shift-55252", "pass": True},
        control_run_id="ctrl_x",
        variant_run_id="var_x",
        preserve_extra=(
            [_turn(1, "session_start"), _turn(9)],
            [_turn(1, "session_start", msgs=()), _turn(9)],
        ),
    )
    assert sc.metrics["preserve"]["control"]["welcome_turns"] == 1
    assert sc.preserve_pass is False
    assert decide(sc, iteration=1, max_iterations=3).action == "revert"


# --------------------------------------------------------------------------
# dry_run: stored fixtures, zero API calls
# --------------------------------------------------------------------------


def _boom(**kwargs):
    raise AssertionError("dry_run must not call the model")


def test_dry_run_scores_stored_fixture_runs_without_api():
    card = _card()
    d = diagnose_deterministic([card])
    sc = evaluate_diagnosis(d, _patch(), dry_run=True, card=card, run_jobs_fn=_boom)
    assert sc.metrics["dry_run"] is True
    assert sc.control_run_id == FIXTURE_CONTROL_RUN
    assert sc.variant_run_id == FIXTURE_VARIANT_RUN
    assert sc.control_spec_rate == pytest.approx(1 / 3)
    assert sc.variant_spec_rate == 1.0
    assert sc.preserve_pass is True
    assert sc.holdout_pass is True
    assert sc.metrics["holdout"]["recipe"] == "a3-shift-55252"
    assert sc.metrics["holdout"]["scorer"] == "escalation_focus"
    assert sc.metrics["targeted_scorer"]["scorer"] == "verify_b"
    assert sc.metrics["turns"] == [6, 12, 18]
    assert decide(sc, iteration=1, max_iterations=3).action == "keep"


def test_dry_run_on_the_safety_shift_has_no_holdout():
    card = _card("under_escalation", "safety", "55252", [5, 6, 7, 8, 9])
    d = diagnose_deterministic([card])
    assert d.holdout_recipe is None  # the card IS the holdout shift
    sc = evaluate_diagnosis(d, _patch(), dry_run=True, card=card, run_jobs_fn=_boom)
    assert sc.holdout_pass is None
    assert sc.control_spec_rate == pytest.approx(0.2)
    assert sc.variant_spec_rate == pytest.approx(0.4)
    assert sc.metrics["gates"]["control"]["require_escalation"] is False
    assert sc.metrics["gates"]["variant"]["require_escalation"] is False
    assert any("IS the holdout shift" in n for n in sc.metrics["notes"])
    assert decide(sc, iteration=1, max_iterations=3).action == "keep"


def test_dry_run_substitutes_a_fixture_shift_when_the_card_shift_is_absent():
    card = _card(shift="99999", turns=[6, 12, 18])
    d = diagnose_deterministic([card])
    sc = evaluate_diagnosis(d, _patch(), dry_run=True, card=card, run_jobs_fn=_boom)
    assert sc.metrics["shift_id"] in {"50737", "55252"}
    assert any("fixture has no run" in n for n in sc.metrics["notes"])


# --------------------------------------------------------------------------
# live wiring (fake run_jobs, still no API)
# --------------------------------------------------------------------------


def _fake_run_jobs_factory(root: Path, calls: list[dict]):
    def fake_run_jobs(*, variant, run_id, mode, jobs, adapter, model, repeat=1, **kw):
        calls.append(
            {
                "variant": variant,
                "run_id": run_id,
                "mode": mode,
                "jobs": jobs,
                "adapter": adapter,
                "model": model,
            }
        )
        arm = FIXTURE_VARIANT_RUN if "auto_" in variant else FIXTURE_CONTROL_RUN
        for job in jobs:
            shift = str(job["shift"])
            src = FIXTURE / arm / "results" / f"{shift}.jsonl"
            dest = root / "runs" / run_id / "results"
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{shift}.jsonl").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
        return run_id

    return fake_run_jobs


def test_live_path_runs_control_parent_then_variant_then_holdout(tmp_path, capsys):
    card = _card()
    d = diagnose_deterministic([card])
    calls: list[dict] = []
    sc = evaluate_diagnosis(
        d,
        _patch(),
        dry_run=False,
        card=card,
        root=tmp_path,
        run_jobs_fn=_fake_run_jobs_factory(tmp_path, calls),
        include_conduct_holdout=False,
    )
    # two targeted arms + two holdout arms, same model on both sides
    assert len(calls) == 4
    assert calls[0]["variant"] == "variants/baseline"  # patch.parent_variant
    assert calls[1]["variant"] == "variants/auto_20250101T000000"
    assert calls[0]["jobs"] == [{"shift": "50737", "turns": [6, 12, 18]}]
    assert calls[1]["jobs"] == calls[0]["jobs"]
    assert {c["model"] for c in calls} == {"gpt-5.6-sol"}
    assert calls[2]["variant"] == "variants/baseline"
    assert calls[3]["variant"] == "variants/auto_20250101T000000"
    assert calls[2]["jobs"][0]["shift"] == "55252"

    assert sc.control_spec_rate == pytest.approx(1 / 3)
    assert sc.variant_spec_rate == 1.0
    assert sc.holdout_pass is True
    assert sc.metrics["holdout"]["recipe"] == "a3-shift-55252"
    # welcome/reply evidence came from the holdout full-shift run
    assert sc.metrics["preserve"]["control"]["welcome_turns"] == 1
    assert sc.preserve_pass is True
    assert sc.metrics["control_variant"] == "variants/baseline"
    assert decide(sc, iteration=1, max_iterations=3).action == "keep"


def test_live_control_arm_is_the_patch_parent_not_the_historical_baseline(tmp_path):
    """Compounding: after a keep, the parent is the kept auto-variant."""
    card = _card()
    d = diagnose_deterministic([card])
    calls: list[dict] = []
    evaluate_diagnosis(
        d,
        PatchPlan(
            variant_dir="variants/auto_iter2",
            changed_file="instructions/guard_response.md",
            diff="",
            parent_variant="variants/auto_iter1",
        ),
        dry_run=False,
        card=card,
        root=tmp_path,
        run_jobs_fn=_fake_run_jobs_factory(tmp_path, calls),
        include_conduct_holdout=False,
    )
    assert calls[0]["variant"] == "variants/auto_iter1"
    assert calls[1]["variant"] == "variants/auto_iter2"
    assert all("baseline_import" not in c["mode"] for c in calls)


def test_live_shift_mode_card_skips_the_holdout_and_uses_shift_mode(tmp_path):
    card = _card("under_escalation", "safety", "55252", [5, 6, 7, 8, 9])
    d = diagnose_deterministic([card])
    calls: list[dict] = []
    sc = evaluate_diagnosis(
        d,
        _patch(),
        dry_run=False,
        card=card,
        root=tmp_path,
        run_jobs_fn=_fake_run_jobs_factory(tmp_path, calls),
        include_conduct_holdout=False,
    )
    assert len(calls) == 2  # no holdout run: this card IS the holdout shift
    assert {c["mode"] for c in calls} == {"shift"}
    assert sc.holdout_pass is None
    assert sc.metrics["targeted_scorer"]["scorer"] == "escalation_focus"
    assert sc.metrics["targeted_scorer"]["pass"] is True
