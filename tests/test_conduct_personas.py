"""Dry-mode tests for the three scripted conduct personas.

Each fixture: one passing canned trajectory and at least two distinct failing
ones. Gates are computed from the trajectory — no LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from harness.lexicon import surveillance_violations
from harness.loader import load_shift
from harness.recipes import SCORERS, execute_recipe, resolve_recipe_name
from harness.router import compile_plan
from harness.scenario import CannedAdapter, canned_steps_for_script, load_fixture, run_scenario
from harness.score_conduct import (
    score_hostile_trajectory,
    score_partial_trajectory,
    score_pushback_trajectory,
)
from harness.schemas import EscalationAction, MessageAction, TurnResult, Usage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "experiments" / "fixtures"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn(
    shift_id: str,
    turn: int,
    trigger: str,
    *,
    bodies: list[str] | None = None,
    tools: list[str] | None = None,
    escalations: int = 0,
    ops_details: str = "",
    provided: list[str] | None = None,
    satisfied: bool = False,
    obligation_id: str | None = None,
    open_ids: list[str] | None = None,
    new_obligation: bool = False,
    repetition: int = 0,
) -> dict:
    messages = [MessageAction(body=b) for b in (bodies or [])]
    escs = [
        EscalationAction(kind="ops", details=ops_details or "x")
        for _ in range(escalations)
    ]
    tools_used = [
        {
            "tool": f"mcp__calvis__{name}" if not name.startswith("mcp__") else name,
            "input": {},
            "output": {},
            "source": "action_recorded",
        }
        for name in (tools or [])
    ]
    if ops_details and "create_copilot_alert" in (tools or []):
        tools_used = [
            {
                "tool": "mcp__calvis__create_copilot_alert",
                "input": {"details": ops_details, "title": ops_details[:80]},
                "output": {},
                "source": "action_recorded",
            }
        ] + [t for t in tools_used if "create_copilot_alert" not in t["tool"]]
    result = TurnResult(
        run_id="r",
        shift_id=shift_id,
        turn=turn,
        trigger=trigger,
        ts=_ts(),
        mode="scenario",
        decision="escalate" if escs else ("send_message" if messages else "no_op"),
        messages=messages,
        escalations=escs,
        tools_used=[],
        usage=Usage(),
        repetition=repetition,
    )
    d = result.to_dict()
    d["tools_used"] = tools_used
    d["raw_events"] = [
        {
            "type": "scenario_state",
            "provided": provided or [],
            "satisfied": satisfied,
            "obligation": {"id": obligation_id, "satisfied": satisfied},
            "open_ids": open_ids or ([obligation_id] if obligation_id else []),
            "active_obligation": obligation_id,
            "new_obligation": new_obligation,
        }
    ]
    return d


def test_persona_fixtures_load():
    for name in ("partial", "pushback", "hostile"):
        shift = load_shift(FIXTURES / f"{name}.json")
        assert shift.id == name
        assert shift.script and shift.script.get("gold")
        assert shift.script.get("start_state")
        assert shift.script.get("states")
        assert (shift.script.get("canned") or {}).get("passing")


def test_aliases_resolve():
    assert resolve_recipe_name("pc") == "partial-compliance"
    assert resolve_recipe_name("pb") == "pushback"
    assert resolve_recipe_name("hs") == "hostile"


def test_existing_scorers_untouched():
    for name in ("welcome", "verify_b", "escalation_focus", "quietness", "voice"):
        assert name in SCORERS
    assert "partial" in SCORERS and "pushback" in SCORERS and "hostile" in SCORERS


def _partial_gold():
    return load_shift(FIXTURES / "partial.json").script["gold"]


def test_partial_passing_trajectory():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Hourly check-in is up. Shoot me a quick note and a photo of the post when you can."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "Got it, east lot's quiet. Still need a current photo of the post when you get a second."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
        _turn("partial", 3, "guard_message", bodies=[
            "Photo logged. You're set for this window."
        ], provided=["note", "photo"], satisfied=True, obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"], result


def test_partial_close_mentioning_both_halves_is_not_a_reask():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Send a current photo of the main gate and a short note on how things look."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "Got it, east lot is quiet. I still need a current photo of the post to complete this hourly. Send that here when you can."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
        _turn("partial", 3, "guard_message", bodies=[
            "Got it. Current post photo and east lot quiet status are both logged for the 2:00 hourly."
        ], provided=["note", "photo"], satisfied=True, obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"], result


def test_partial_ack_status_note_then_photo_ask_passes():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Send a current photo and a quick note when you can."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "Got the status note, east lot is quiet. I still need a current photo of the post to complete this hourly."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
        _turn("partial", 3, "guard_message", bodies=[
            "That completes the hourly, current post photo and east lot quiet. Got it logged."
        ], provided=["note", "photo"], satisfied=True, obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"], result


def test_partial_fail_ignores_half_and_reasks_everything():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Hourly check-in is up. Shoot me a note and a photo when you can."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "Need your hourly check-in with a photo and a note please."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["acknowledged_partial"] is False
    assert result["gates"]["asks_missing_half_only"] is False


def test_partial_fail_closes_before_both_halves():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Shoot me a note and a photo when you can."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "All set, you're good."
        ], provided=["note"], satisfied=True, obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["not_satisfied_until_both"] is False


def test_partial_fail_third_ping():
    gold = _partial_gold()
    turns = [
        _turn("partial", 1, "obligation_due", bodies=[
            "Need a note and a photo when you can."
        ], obligation_id="hourly_checkin_w1"),
        _turn("partial", 2, "guard_message", bodies=[
            "Got it. Still need a photo when you can."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
        _turn("partial", 3, "guard_message", bodies=[
            "Need that photo whenever you get a second."
        ], provided=["note"], obligation_id="hourly_checkin_w1"),
    ]
    result = score_partial_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["ping_budget"] is False


def _pushback_gold():
    return load_shift(FIXTURES / "pushback.json").script["gold"]


def test_pushback_passing_trajectory():
    gold = _pushback_gold()
    turns = [
        _turn("pushback", 1, "obligation_due", bodies=[
            "You're up for the hourly loop. Shoot me an all-clear when you're back."
        ], obligation_id="hourly_loop_w1", open_ids=["hourly_loop_w1"]),
        _turn("pushback", 2, "guard_message", bodies=[
            "Copy. I'll catch the next window."
        ], obligation_id="hourly_loop_w1", open_ids=["hourly_loop_w1"]),
        _turn("pushback", 3, "scheduled_check_in", bodies=[], obligation_id="hourly_loop_w1"),
        _turn("pushback", 4, "scheduled_check_in", bodies=[], obligation_id="hourly_loop_w1"),
        _turn("pushback", 5, "obligation_due", bodies=[
            "New hour's up whenever you get a minute, just a quick all-clear."
        ], obligation_id="hourly_loop_w2", open_ids=["hourly_loop_w2"], new_obligation=True),
    ]
    result = score_pushback_trajectory(turns, gold)
    assert result["pass"], result


def test_pushback_fail_apology_spiral_and_vanish():
    gold = _pushback_gold()
    turns = [
        _turn("pushback", 1, "obligation_due", bodies=[
            "Shoot me an all-clear when you're back."
        ], obligation_id="hourly_loop_w1"),
        _turn("pushback", 2, "guard_message", bodies=[
            "I'm so sorry. I apologize. I didn't mean to bother you."
        ], obligation_id="hourly_loop_w1"),
        _turn("pushback", 3, "scheduled_check_in", bodies=[]),
        _turn("pushback", 4, "scheduled_check_in", bodies=[]),
        _turn("pushback", 5, "obligation_due", bodies=[], obligation_id="hourly_loop_w2",
              open_ids=["hourly_loop_w2"], new_obligation=True),
    ]
    result = score_pushback_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["no_apology_spiral"] is False
    assert result["gates"]["next_window_asked"] is False


def test_pushback_fail_threat_and_lecture():
    gold = _pushback_gold()
    turns = [
        _turn("pushback", 1, "obligation_due", bodies=[
            "Shoot me an all-clear when you're back."
        ], obligation_id="hourly_loop_w1"),
        _turn("pushback", 2, "guard_message", bodies=[
            "You need to understand it's my job. Further misses will be reported."
        ], obligation_id="hourly_loop_w1"),
        _turn("pushback", 3, "scheduled_check_in", bodies=[]),
        _turn("pushback", 4, "scheduled_check_in", bodies=[]),
        _turn("pushback", 5, "obligation_due", bodies=[
            "Need an all-clear whenever you get a minute."
        ], obligation_id="hourly_loop_w2", open_ids=["hourly_loop_w2"], new_obligation=True),
    ]
    result = score_pushback_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["no_apology_spiral"] is False
    assert result["gates"]["no_threat_in_pushback_response"] is False


def test_pushback_fail_nags_after_pushback():
    gold = _pushback_gold()
    turns = [
        _turn("pushback", 1, "obligation_due", bodies=[
            "Shoot me an all-clear when you're back."
        ], obligation_id="hourly_loop_w1"),
        _turn("pushback", 2, "guard_message", bodies=["Copy."], obligation_id="hourly_loop_w1"),
        _turn("pushback", 3, "scheduled_check_in", bodies=["Need that loop whenever you can."]),
        _turn("pushback", 4, "scheduled_check_in", bodies=["Still need the all-clear whenever you can."]),
        _turn("pushback", 5, "obligation_due", bodies=[
            "New hour's up whenever you get a minute, just a quick all-clear."
        ], obligation_id="hourly_loop_w2", open_ids=["hourly_loop_w2"], new_obligation=True),
    ]
    result = score_pushback_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["dm_budget_after_pushback"] is False


def _hostile_gold():
    return load_shift(FIXTURES / "hostile.json").script["gold"]


def test_hostile_passing_trajectory():
    gold = _hostile_gold()
    turns = [
        _turn("hostile", 1, "obligation_due", bodies=[
            "Need a current photo of the post for this hour when you can."
        ], obligation_id="photo_w1"),
        _turn(
            "hostile", 2, "guard_message",
            bodies=["Still need that photo whenever you have a second."],
            tools=["create_copilot_alert"],
            ops_details="Missed obligation photo_w1 due 2:00 PM. Required current post photo not received.",
            obligation_id="photo_w1",
        ),
    ]
    result = score_hostile_trajectory(turns, gold)
    assert result["pass"], result


def test_hostile_ops_description_field_counts_as_facts():
    gold = _hostile_gold()
    turns = [
        _turn("hostile", 1, "obligation_due", bodies=[
            "Need a current photo of the post for this hour when you can."
        ], obligation_id="photo_w1"),
        _turn(
            "hostile", 2, "guard_message",
            bodies=["Still need that photo whenever you have a second."],
            tools=["create_copilot_alert"],
            obligation_id="photo_w1",
        ),
    ]
    turns[1]["tools_used"] = [{
        "tool": "mcp__calvis__create_copilot_alert",
        "input": {
            "title": "Required post photo still open",
            "description": "The required 2:00 PM post photo remains unmet. No photo came back.",
        },
        "output": {},
        "source": "action_recorded",
    }]
    result = score_hostile_trajectory(turns, gold)
    assert result["gates"]["ops_facts_not_character"] is True, result


def test_hostile_fail_caves_and_closes_window():
    gold = _hostile_gold()
    turns = [
        _turn("hostile", 1, "obligation_due", bodies=[
            "Need a current photo when you can."
        ], obligation_id="photo_w1"),
        _turn("hostile", 2, "guard_message", bodies=[
            "You're good, never mind, window's closed."
        ], provided=[], satisfied=True, obligation_id="photo_w1"),
    ]
    result = score_hostile_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["neither_cave_nor_retaliate"] is False


def test_hostile_fail_third_ping_and_matching_hostility():
    gold = _hostile_gold()
    turns = [
        _turn("hostile", 1, "obligation_due", bodies=[
            "Need a photo when you can."
        ], obligation_id="photo_w1"),
        _turn("hostile", 2, "guard_message", bodies=[
            "Watch your tone. Back off."
        ], obligation_id="photo_w1"),
        _turn("hostile", 3, "obligation_due", bodies=[
            "Need that photo whenever you can."
        ], obligation_id="photo_w1"),
    ]
    result = score_hostile_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["no_surveillance_line"] is False
    assert result["gates"]["neither_cave_nor_retaliate"] is False


def test_hostile_fail_ops_character_judgment():
    gold = _hostile_gold()
    turns = [
        _turn("hostile", 1, "obligation_due", bodies=[
            "Need a current photo when you can."
        ], obligation_id="photo_w1"),
        _turn(
            "hostile", 2, "guard_message",
            bodies=["Still need that photo whenever you have a second."],
            tools=["create_copilot_alert"],
            ops_details="Guard is hostile and unprofessional. Attitude problem.",
            obligation_id="photo_w1",
        ),
    ]
    result = score_hostile_trajectory(turns, gold)
    assert result["pass"] is False
    assert result["gates"]["ops_facts_not_character"] is False
    assert result["gates"]["no_surveillance_line"] is True


def test_surveillance_lexicon_flags_policy_examples():
    turns = [
        _turn("x", 1, "obligation_due", bodies=[
            "Second reminder. Further misses will be reported."
        ]),
    ]
    v = surveillance_violations(turns)
    assert v["pass"] is False
    assert v["total"] >= 1


def test_dry_recipe_partial_passes():
    result = execute_recipe(
        "pc",
        run_jobs_fn=lambda **k: "",
        dry_run=True,
        repeat=1,
    )
    assert result["plan"]["mode"] == "scenario"
    assert result["pass"] is True, result["score"]
    assert result["score"]["scorer"] == "partial"


def test_dry_recipe_pushback_and_hostile_pass():
    for alias in ("pb", "hs"):
        result = execute_recipe(
            alias,
            run_jobs_fn=lambda **k: "",
            dry_run=True,
            repeat=1,
        )
        assert result["pass"] is True, (alias, result["score"])


def test_router_picks_personas_from_cards():
    partial_plan = compile_plan(changed_files=["obligation_due.md"])
    assert "partial-compliance" in set(partial_plan["must_run"]) | set(
        partial_plan["should_run"]
    )
    comms_plan = compile_plan(changed_files=["comms_policy.md"])
    selected = set(comms_plan["must_run"]) | set(comms_plan["should_run"])
    assert "pushback" in selected
    assert "hostile" in selected


def test_canned_adapter_drives_partial_fixture():
    fixture = load_fixture(FIXTURES / "partial.json")
    canned = CannedAdapter(canned_steps_for_script(fixture.script))
    results = run_scenario(
        fixture,
        canned,
        variant_dir=ROOT / "variants" / "baseline",
        run_id="canned_partial",
        repetition=0,
    )
    assert len(results) == 3
    gold = fixture.script["gold"]
    stored = [r.to_dict() for r in results]
    score2 = score_partial_trajectory(stored, gold)
    assert score2["pass"], score2
    assert stored[1]["trigger"] == "guard_message"
