"""Conduct-guidelines measurement layer: detectors, the guardrail, the floor.

No API calls anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from harness.guidelines import (
    CROSS_CUTTING_ID,
    anchor_jobs,
    baseline_report,
    baseline_turn_rows,
    check_floor,
    detect_situations,
    format_baseline_table,
    load_anchor_shift,
    load_guidelines,
    scenario_by_id,
    score_conduct_floor,
)
from harness.loader import Shift, load_shift
from harness.recipes import SCORERS, load_recipes, resolve_analyze_target, resolve_recipe_name

ROOT = Path(__file__).resolve().parents[1]
G = load_guidelines()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _rows(shift_id: str):
    shift = load_anchor_shift(shift_id)
    return shift, baseline_turn_rows(shift)


def _at(shift_id: str, turn: int):
    shift, rows = _rows(shift_id)
    i = next(i for i, r in enumerate(rows) if int(r["turn"]) == turn)
    return shift, rows[i], rows[:i]


def _situations(shift_id: str, turn: int) -> list[str]:
    shift, row, prior = _at(shift_id, turn)
    return detect_situations(shift, row, prior, G)


def _turn(
    turn=1,
    trigger="guard_message",
    ts="2026-08-04T03:00:00+00:00",
    tools=(),
    messages=(),
    escalations=(),
):
    """A canned store-shaped turn row. `tools` items are name or (name, input)."""
    recs = []
    for t in tools:
        name, inp = (t, {}) if isinstance(t, str) else t
        recs.append({"tool": name, "input": inp})
    return {
        "turn": turn,
        "trigger": trigger,
        "ts": ts,
        "messages": [{"body": m} for m in messages],
        "tools_used": recs,
        "escalations": list(escalations),
    }


def _dm(body: str):
    return ("request_copilot_dm", {"body": body})


def _sc(sid: str) -> dict:
    return scenario_by_id(G, sid)


# --------------------------------------------------------------------------
# detectors on real shift turns
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shift_id,turn,scenario",
    [
        ("58349", 18, "S1"),   # photos + "unknown vehicles ... inside of the gate"
        ("56370", 11, "S2"),   # "leave my phone in my car" (t10 cannot see it yet)
        ("55252", 8, "S3"),    # silent guard, hourly window closing empty
        ("53658", 2, "S4"),    # 30s into the shift, nobody has checked in
        ("50340", 19, "S5"),   # "No Supervisor at the site everyone have closed"
        ("50737", 6, "S6"),    # "inside perimeter is clear ... everything is clear"
        ("46116", 96, "S7"),   # photo instead of text
        ("58349", 84, "S8"),   # "the three individuals ... are security"
        ("50737", 30, "S10"),  # third+ critical of the shift
    ],
)
def test_detector_fires_on_its_dataset_anchor(shift_id, turn, scenario):
    assert scenario in _situations(shift_id, turn)


def test_cross_cutting_applies_to_every_turn():
    assert CROSS_CUTTING_ID in _situations("50340", 19)
    assert CROSS_CUTTING_ID in _situations("55252", 3)


def test_s1_needs_no_recent_escalation():
    """S1 is the *first* alarm; once a critical is out, S10 owns the turn."""
    assert "S1" in _situations("58349", 57)
    assert "S1" not in _situations("58349", 71)
    assert "S10" in _situations("58349", 71)


def test_s10_does_not_fire_on_the_first_critical():
    assert "S10" not in _situations("58349", 18)


def test_negated_confront_is_not_a_confront():
    """'don't approach them' is the opposite of 'approach them'."""
    shift, row, prior = _at("58349", 18)
    res = check_floor(_sc("S1"), row, prior, shift=shift, guidelines=G)
    assert res["passed"], res["failed_checks"]


# --------------------------------------------------------------------------
# THE GUARDRAIL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shift_id,turn",
    [("58349", 18), ("56370", 11), ("55252", 8), ("53658", 2), ("50737", 30), ("46116", 96)],
)
def test_guardrail_nothing_after_the_turn_ts_is_read(shift_id, turn, tmp_path):
    """Truncating the shift right after the turn ts changes no detection and no
    floor result. Nothing later than the turn's timestamp is evidence."""
    shift, row, prior = _at(shift_id, turn)
    full_sits = detect_situations(shift, row, prior, G)
    full_floor = [
        check_floor(_sc(s), row, prior, shift=shift, guidelines=G) for s in full_sits
    ]

    raw = json.loads(shift.path.read_text(encoding="utf-8"))
    cut = row["ts"]
    raw["events"] = [e for e in raw["events"] if e["ts"] <= cut]
    raw["baseline"] = [e for e in raw["baseline"] if e["ts"] <= cut]
    p = tmp_path / f"{shift_id}.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    cut_shift = load_shift(p)

    cut_sits = detect_situations(cut_shift, row, prior, G)
    cut_floor = [
        check_floor(_sc(s), row, prior, shift=cut_shift, guidelines=G) for s in cut_sits
    ]
    assert cut_sits == full_sits
    assert cut_floor == full_floor


def test_guardrail_a_later_guard_message_cannot_change_detection(tmp_path):
    """56370 t10 must not see the 02:05 walk-off message: it arrives later."""
    shift, row, prior = _at("56370", 10)
    assert "S2" not in detect_situations(shift, row, prior, G)


# --------------------------------------------------------------------------
# floor checks on canned turn rows — one pass and one fail per check type
# --------------------------------------------------------------------------


def test_floor_always_tools_any():
    sc = _sc("S1")
    ok = _turn(tools=[("escalate_to_human", {}), _dm("Ops is on it. Keep your distance.")])
    bad = _turn(tools=[_dm("Keep an eye on them.")])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert not fail["passed"]
    assert fail["failed_checks"][0]["check"] == "always.tools_any"


def test_floor_never_dm_contains():
    sc = _sc("S1")
    ok = _turn(tools=[("escalate_to_human", {}), _dm("Stay put and don't approach them.")])
    bad = _turn(tools=[("escalate_to_human", {}), _dm("Go check it out and tell me who they are.")])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert not fail["passed"]
    assert "never.dm_contains" in fail["failed_checks"][0]["check"]
    assert fail["failed_checks"][0]["evidence"]


def test_floor_never_dm_contains_reuses_the_surveillance_lexicon():
    sc = _sc("S9")
    bad = _turn(tools=[_dm("This is your last warning about post abandonment.")])
    fail = check_floor(sc, bad, [], guidelines=G)
    assert not fail["passed"]


def test_floor_never_dms_over():
    sc = _sc("S8")
    ok = _turn(tools=[_dm("Got it, logged.")])
    bad = _turn(tools=[_dm("Got it, logged."), _dm("Also send me a photo when you can.")])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    assert not check_floor(sc, bad, [], guidelines=G)["passed"]


def test_floor_never_tools_any():
    sc = _sc("S8")
    ok = _turn(tools=[_dm("Copy, thanks.")])
    bad = _turn(tools=[("escalate_to_human", {}), _dm("Copy, thanks.")])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert fail["failed_checks"][0]["check"] == "never.tools_any"


def test_floor_never_tool_severity():
    sc = _sc("S6")
    base = [("get_guard_locations", {}), _dm("Thanks, all logged.")]
    ok = _turn(
        tools=base + [("escalate_to_human", {"severity": "normal"})],
        escalations=[{"kind": "human", "details": "", "input": {"severity": "normal"}}],
    )
    bad = _turn(
        tools=base + [("escalate_to_human", {"severity": "critical"})],
        escalations=[{"kind": "human", "details": "", "input": {"severity": "critical"}}],
    )
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert any("tool_severity" in f["check"] for f in fail["failed_checks"])


def test_floor_before_affirming_dm():
    sc = _sc("S6")
    ok = _turn(tools=[("get_guard_locations", {}), _dm("Got it, nice work.")])
    bad = _turn(tools=[_dm("Got it, nice work."), ("get_guard_locations", {})])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert fail["failed_checks"][0]["check"] == "always.before_affirming_dm"


def test_an_escalation_announcement_is_not_an_affirming_dm():
    sc = _sc("S6")
    row = _turn(tools=[_dm("Got it. I'm escalating this to ops."), ("get_guard_locations", {})])
    res = check_floor(sc, row, [], guidelines=G)
    assert not any(f["check"] == "always.before_affirming_dm" for f in res["failed_checks"])


def test_floor_before_referencing_photo():
    sc = _sc("S7")
    ok = _turn(tools=[("fetch_chat_image", {}), _dm("Good photo, that gate looks locked.")])
    bad = _turn(tools=[_dm("Good photo, that gate looks locked."), ("fetch_chat_image", {})])
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert fail["failed_checks"][0]["check"] == "always.before_referencing_photo"


def test_floor_third_ask_same_window():
    sc = _sc("S7")
    prior = [
        _turn(turn=1, ts="2026-08-04T03:05:00+00:00",
              tools=[("fetch_chat_image", {}), _dm("Can you send me a photo when you can?")]),
        _turn(turn=2, ts="2026-08-04T03:25:00+00:00",
              tools=[("fetch_chat_image", {}), _dm("Need a quick photo of the rear gate?")]),
    ]
    third = _turn(turn=3, ts="2026-08-04T03:45:00+00:00",
                  tools=[("fetch_chat_image", {}), _dm("Still need that photo, send when you can.")])
    second_only = _turn(turn=3, ts="2026-08-04T03:45:00+00:00",
                        tools=[("fetch_chat_image", {}), _dm("Still need that photo, send when you can.")])
    assert check_floor(sc, second_only, prior[:1], guidelines=G)["passed"]
    fail = check_floor(sc, third, prior, guidelines=G)
    assert any(f["check"] == "never.third_ask_same_window" for f in fail["failed_checks"])


def test_floor_new_ask_at_shift_end():
    sc = _sc("S4")
    ok = _turn(trigger="shift_ending", tools=[("add_copilot_note", {}), _dm("Closing this out with ops.")])
    bad = _turn(
        trigger="shift_ending",
        tools=[("add_copilot_note", {}), _dm("Before you go, send me a photo of the rear gate.")],
    )
    assert check_floor(sc, ok, [], guidelines=G)["passed"]
    fail = check_floor(sc, bad, [], guidelines=G)
    assert any(f["check"] == "never.new_ask_at_shift_end" for f in fail["failed_checks"])


def test_floor_always_when_only_fires_on_its_condition():
    """S3: escalate only once guard AND device have both been dark >= 2h."""
    sc = _sc("S3")
    # 53658 t5: guard quiet 1h but the device is still reporting (37 min dark).
    shift, row, prior = _at("53658", 5)
    quiet = dict(row, tools_used=[{"tool": "get_job_logs", "input": {}}], escalations=[], messages=[])
    assert check_floor(sc, quiet, prior, shift=shift, guidelines=G)["passed"]

    # 53658 t7: guard 180 min quiet AND device 157 min dark — a person is owed.
    shift7, row7, prior7 = _at("53658", 7)
    no_esc = dict(row7, tools_used=[{"tool": "get_job_logs", "input": {}}], escalations=[], messages=[])
    fail = check_floor(sc, no_esc, prior7, shift=shift7, guidelines=G)
    assert any(f["check"] == "always.tools_any" for f in fail["failed_checks"])

    escalated = dict(
        row7,
        tools_used=[{"tool": "escalate_to_human", "input": {}}],
        escalations=[{"kind": "human", "details": "welfare check", "input": {}}],
        messages=[],
    )
    assert check_floor(sc, escalated, prior7, shift=shift7, guidelines=G)["passed"]


def test_check_floor_not_applicable_is_a_pass():
    res = check_floor(_sc("S1"), _turn(), [], guidelines=G, applicable=False)
    assert res["applicable"] is False and res["passed"] is True


# --------------------------------------------------------------------------
# baseline "what production did" report
# --------------------------------------------------------------------------


def test_baseline_report_is_api_free_and_covers_the_anchor_shifts():
    rep = baseline_report()
    assert rep["api_calls"] == 0
    assert rep["per_scenario"]
    table = format_baseline_table(rep)
    assert "what production did" in table
    assert "baseline_ok mismatches" in table


def test_baseline_report_matches_most_declared_baseline_ok_flags():
    """Declared flags are the parent's read of production; a few disagree and
    are reported, never forced. Guard the ratio so a detector regression shows."""
    rep = baseline_report()
    declared = _declared_flags()
    mismatches = rep["baseline_ok_mismatches"]
    assert len(mismatches) < len(declared) / 2
    keys = {(m["shift"], m["turn"], m["scenario"]) for m in mismatches}
    # known, documented disagreements (see HARNESS.md "Conduct guidelines")
    assert ("56370", 10, "S2") in keys      # walk-off message arrives after the turn ts
    assert ("58349", 84, "S8") in keys      # production escalated on an all-clear


def _declared_flags():
    from harness.guidelines import anchor_flags

    return [a for a in anchor_flags(G) if a["baseline_ok"] is not None]


def test_baseline_report_flags_the_verdict_language_turn():
    """56370 t14 is the production prompt issuing an ultimatum."""
    rep = baseline_report([{"shift": "56370", "turns": [14]}])
    assert rep["failing_turns"]


# --------------------------------------------------------------------------
# scorer + recipe wiring
# --------------------------------------------------------------------------


class _Store:
    """Minimal ExperimentStore stand-in: canned rows per (run, shift)."""

    def __init__(self, data):
        self.data = data

    def load_turns(self, run_id, shift_id):
        return list(self.data.get((run_id, str(shift_id))) or [])


def test_score_conduct_floor_gates_on_the_variant_only():
    shift, rows = _rows("50737")
    turn6 = [r for r in rows if int(r["turn"]) == 6]
    clean = [
        dict(
            turn6[0],
            tools_used=[{"tool": "get_guard_locations", "input": {}},
                        {"tool": "request_copilot_dm", "input": {"body": "Got it, thanks."}}],
            messages=[{"body": "Got it, thanks."}],
            escalations=[],
        )
    ]
    recipe = {"jobs": [{"shift": "50737", "turns": [6]}]}
    store = _Store({("ctrl", "50737"): turn6, ("var", "50737"): clean})
    out = score_conduct_floor(store, "ctrl", "var", recipe)
    assert out["scorer"] == "conduct_floor"
    assert out["pass"] is True                       # variant holds the floor
    assert out["control"]["failing_turns"]            # control (production) does not
    assert out["control_is_evidence_not_gate"] is True

    flipped = _Store({("ctrl", "50737"): clean, ("var", "50737"): turn6})
    bad = score_conduct_floor(flipped, "ctrl", "var", recipe)
    assert bad["pass"] is False
    assert bad["variant"]["failing_turns"][0]["quote"]


def test_score_conduct_floor_emits_judgment_items_but_never_answers_them():
    shift, rows = _rows("50340")
    t19 = [r for r in rows if int(r["turn"]) == 19]
    store = _Store({("ctrl", "50340"): t19, ("var", "50340"): t19})
    out = score_conduct_floor(store, "ctrl", "var", {"jobs": [{"shift": "50340", "turns": [19]}]})
    items = out["judgment_items"]
    assert items and all(i["questions"] for i in items)
    assert out["judgment_is_advisory"] is True
    assert not any("verdict" in json.dumps(i) for i in items)


def test_conduct_floor_is_registered_in_scorers():
    assert "conduct_floor" in SCORERS


def test_recipe_and_alias_wiring():
    data = load_recipes()
    assert resolve_recipe_name("ru") == "rules-dataset"
    r = data["recipes"]["rules-dataset"]
    assert r["mode"] == "turn"
    assert r["control_variant"] == "variants/baseline"
    assert r["candidate_variant"] == "variants/variant_r"
    assert r["scorer"] == "conduct_floor"
    card = r["card"]
    assert card["risk_class"] == "conduct"
    assert card["cost"] == "multi-turn"
    assert card["est_usd"] == pytest.approx(2.5)
    assert set(card["covers"]) == {
        "conduct_floor", "emergency", "walkoff", "silent_guard",
        "verdict_language", "reescalation",
    }
    assert card["after"] == ["smoke-welcome"]
    assert set(card["required_when"]["files"]) == {
        "tools.md", "holding_the_post.md", "comms_policy.md",
        "obligations.md", "guard_response.md", "scheduled_check_in.md",
    }
    assert set(card["required_when"]["intents"]) == {
        "conduct", "aggress", "tone", "emergency", "escalation", "professional",
    }
    assert resolve_analyze_target("vr") == "variants/variant_r"
    assert (ROOT / "variants" / "variant_r").is_dir()


def test_recipe_turns_equal_the_guidelines_anchors():
    data = load_recipes()
    assert data["recipes"]["rules-dataset"]["jobs"] == anchor_jobs(G)


def test_dry_run_of_the_recipe_makes_no_api_calls():
    from harness.recipes import execute_recipe

    def _boom(**kwargs):
        raise AssertionError("cx t ru -n must not call the model")

    out = execute_recipe("ru", run_jobs_fn=_boom, dry_run=True)
    assert out["score"] is None and out["pass"] is None
    assert out["plan"]["dry_mode"] == "baseline_conduct_floor"
    assert out["baseline_report"]["api_calls"] == 0


# --------------------------------------------------------------------------
# judge hook (advisory, additive, never gates)
# --------------------------------------------------------------------------


def test_judge_asks_the_scenario_questions_on_top_of_the_six(tmp_path):
    from harness.judge import (
        MUST_NOT_HAPPEN_IDS,
        format_advisory_dashboard,
        judge_transcript,
        load_scenario_items,
        scenario_items_from_run,
    )

    items = [
        {
            "shift": "50340",
            "turn": 19,
            "scenario": "S5",
            "name": "asks_for_what_you_cannot_grant",
            "questions": ["Was ops actually looped in?", "Was there a clear next step?"],
        }
    ]
    (tmp_path / "recipe_score.json").write_text(
        json.dumps({"score": {"judgment_items": items}}), encoding="utf-8"
    )
    assert scenario_items_from_run(tmp_path) == items
    assert load_scenario_items(tmp_path / "recipe_score.json") == items

    seen: list[str] = []

    def fake_complete(system, user):
        seen.append(user)
        if "SCENARIO questions" in user:
            return json.dumps(
                {
                    "items": [
                        {"id": "S5_50340_t19_q1", "verdict": "yes", "quote": "looping them in"},
                        {"id": "S5_50340_t19_q2", "verdict": "no", "quote": "Hang tight"},
                    ]
                }
            )
        return json.dumps({"items": [{"id": "third_ping", "verdict": "no", "quote": "x"}]})

    out = judge_transcript(
        "COPILOT DM: I'm looping them in. Hang tight",
        complete_fn=fake_complete,
        model="judge-model",
        copilot_model="copilot-model",
        scenario_items=items,
    )
    assert len(out["items"]) == 6  # the standard checklist is intact
    assert out["scenario"]["scenario_items"] == 2
    assert out["scenario"]["advisory"] is True and out["scenario"]["does_not_gate"] is True
    assert [i["verdict"] for i in out["scenario"]["items"]] == ["yes", "no"]
    assert all(i["quote"] for i in out["scenario"]["items"])
    # advisory: scenario answers never become must_not_happen flags
    assert out["flagged"] is False
    assert not (set(MUST_NOT_HAPPEN_IDS) & {i["id"] for i in out["scenario"]["items"]})
    assert "scenario S5_50340_t19_q1" in format_advisory_dashboard(out)
    assert any("SCENARIO questions" in u for u in seen)


def test_judge_without_scenario_items_is_unchanged():
    from harness.judge import judge_transcript

    out = judge_transcript(
        "COPILOT DM: hi",
        complete_fn=lambda s, u: json.dumps({"items": []}),
        model="judge-model",
        copilot_model="copilot-model",
    )
    assert "scenario" not in out


# --------------------------------------------------------------------------
# loop holdout wiring
# --------------------------------------------------------------------------


def _card():
    from harness.agent.types import Evidence, ProblemCard

    return ProblemCard(
        id="50737-unverified_claim",
        shift_id="50737",
        turns=[6, 12, 18],
        problem_class="unverified_claim",
        severity="lift",
        evidence=Evidence(
            guard_text="all clear on the north lot",
            missing_tools=["get_guard_locations"],
            event_indexes=[167],
        ),
        policy_files=["instructions/guard_response.md"],
    )


def _diagnosis():
    from harness.agent.diagnose import diagnose_deterministic

    return diagnose_deterministic([_card()])


def _patch_plan():
    from harness.agent.types import PatchPlan

    return PatchPlan(
        variant_dir="variants/auto_x",
        changed_file="instructions/guard_response.md",
        diff="",
        parent_variant="variants/baseline",
    )


def test_a_conduct_floor_break_fails_the_holdout_and_reverts():
    from harness.agent.evaluate import CONDUCT_HOLDOUT_RECIPE, build_scorecard, combine_holdouts
    from harness.agent.policy import decide

    assert CONDUCT_HOLDOUT_RECIPE == "rules-dataset"
    a3 = {"recipe": "a3-shift-55252", "pass": True, "scorer": "escalation_focus"}
    fail = {
        "recipe": "rules-dataset",
        "pass": False,
        "scorer": "conduct_floor",
        "detail": "variant breaks the floor: 50737 t6 S6 never.dm_contains[verdict]",
    }
    assert combine_holdouts([a3, dict(fail, **{"pass": True})])["pass"] is True
    assert combine_holdouts([a3, fail])["pass"] is False
    # a holdout that could not run neither passes nor fails the keep
    assert combine_holdouts([a3, {"recipe": "rules-dataset", "pass": None}])["pass"] is True

    rows = [{"turn": 1, "trigger": "guard_message", "messages": [{"body": "ok"}], "tools_used": []}]
    broken = build_scorecard(
        _diagnosis(),
        control_rows=rows,
        variant_rows=rows,
        scorer_result={"scorer": "verify_b", "pass": True},
        holdout=a3,
        holdouts=[a3, fail],
        control_run_id="c",
        variant_run_id="v",
    )
    assert broken.holdout_pass is False
    assert broken.metrics["holdout"]["recipe"] == "a3-shift-55252"  # primary unchanged
    assert [h["recipe"] for h in broken.metrics["holdouts"]] == [
        "a3-shift-55252",
        "rules-dataset",
    ]
    assert decide(broken, iteration=1, max_iterations=3).action == "revert"


def test_conduct_holdout_runs_after_the_a3_holdout_on_the_live_path(tmp_path):
    """Faked run_jobs: both holdout recipes execute, and no model is called."""
    from harness.agent.evaluate import evaluate_diagnosis

    calls: list[dict] = []

    def fake_run_jobs(*, variant, run_id, mode, jobs, adapter, model, repeat=1, **kw):
        calls.append({"variant": variant, "run_id": run_id, "jobs": jobs})
        dest = tmp_path / "runs" / run_id / "results"
        dest.mkdir(parents=True, exist_ok=True)
        for job in jobs:
            (dest / f"{job['shift']}.jsonl").write_text("", encoding="utf-8")
        return run_id

    sc = evaluate_diagnosis(
        _diagnosis(),
        _patch_plan(),
        dry_run=False,
        turns=[6],
        root=tmp_path,
        run_jobs_fn=fake_run_jobs,
    )
    assert [h["recipe"] for h in sc.metrics["holdouts"]] == ["a3-shift-55252", "rules-dataset"]
    # the conduct holdout replayed the anchor shifts on both arms
    conduct_jobs = [c for c in calls if any(str(j["shift"]) == "58349" for j in c["jobs"])]
    assert len(conduct_jobs) == 2
    assert {c["variant"] for c in conduct_jobs} == {"variants/baseline", "variants/auto_x"}


def test_conduct_holdout_is_skippable_and_dry_run_stays_api_free():
    from harness.agent.evaluate import CONDUCT_HOLDOUT_RECIPE, evaluate_diagnosis

    def boom(**kwargs):
        raise AssertionError("dry_run must not call the model")

    card = _card()
    on = evaluate_diagnosis(
        _diagnosis(), _patch_plan(), dry_run=True, card=card, run_jobs_fn=boom
    )
    assert [h["recipe"] for h in on.metrics["holdouts"]] == [
        "a3-shift-55252",
        CONDUCT_HOLDOUT_RECIPE,
    ]
    # dry mode reports the conduct holdout as NOT RUN — never as a pass
    assert on.metrics["holdouts"][1]["pass"] is None
    assert on.holdout_pass is True  # the a3 holdout still decides
    assert any("not run in dry mode" in n for n in on.metrics["notes"])

    off = evaluate_diagnosis(
        _diagnosis(),
        _patch_plan(),
        dry_run=True,
        card=card,
        run_jobs_fn=boom,
        include_conduct_holdout=False,
    )
    assert [h["recipe"] for h in off.metrics["holdouts"]] == ["a3-shift-55252"]
