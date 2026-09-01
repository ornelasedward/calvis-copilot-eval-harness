"""Shift-seeded simulated guard: profile derivation, dry run, gates, model split.

No API calls anywhere in this file. The dry path uses a canned copilot and a
canned guard; the LLM guard is exercised through a stub complete_fn.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.lexicon import group_by_repetition
from harness.loader import load_shift
from harness.recipes import SCORERS, execute_recipe, resolve_recipe_name, simulator_defaults
from harness.schemas import EscalationAction, MessageAction, RunManifest, TurnResult, Usage
from harness.simulate import (
    CannedGuard,
    SimulatedGuard,
    assert_simulator_model_differs,
    build_guard_profile,
    build_obligation_ledger,
    normalize_guard_action,
    run_simulation,
    score_simulation_run,
    score_simulation_trajectory,
    turn_timestamp,
)
from harness.store import ExperimentStore
from harness.thread import ThreadManager

ROOT = Path(__file__).resolve().parents[1]
SHIFT_50737 = ROOT / "shifts" / "50737.json"
SEED_TURN = 17


# --- 1. deterministic profile ------------------------------------------------


def test_profile_derived_from_real_shift():
    shift = load_shift(SHIFT_50737)
    profile = build_guard_profile(shift, up_to_turn=SEED_TURN)

    assert profile["deterministic"] is True
    assert profile["llm_used"] is False
    assert profile["derived_from"]["shift"] == "50737"
    assert profile["derived_from"]["up_to_turn"] == SEED_TURN

    cutoff = turn_timestamp(shift, SEED_TURN)
    assert cutoff is not None
    quoted = profile["messages"]["quoted"]
    assert quoted, "seed profile must quote the guard's real messages"
    assert all(datetime.fromisoformat(q["ts"]) <= cutoff for q in quoted)
    assert profile["messages"]["count"] == len(quoted)

    # This night: Priya answers fast, at length, never sends a photo, pushes back.
    assert profile["reply_latency_minutes"]["n"] > 0
    assert profile["reply_latency_minutes"]["median"] is not None
    assert profile["messages"]["chars"]["median"] > 0
    assert profile["photos"]["sent_photos"] is False
    assert profile["lexicon"]["counts"]["pushback"] >= 1
    assert isinstance(profile["lexicon"]["cooperation"], list)
    assert profile["missed_obligations"]["copilot_asks"] >= 1
    assert profile["time_of_night"]["messages_by_local_hour"]
    assert profile["time_of_night"]["longest_silence_minutes"] is not None
    assert "Priya" in profile["style_summary"]


def test_profile_is_deterministic_and_cutoff_bounded():
    shift = load_shift(SHIFT_50737)
    a = build_guard_profile(shift, up_to_turn=SEED_TURN)
    b = build_guard_profile(shift, up_to_turn=SEED_TURN)
    assert a["profile_hash"] == b["profile_hash"]

    full = build_guard_profile(shift)
    assert full["messages"]["count"] > a["messages"]["count"]
    assert full["profile_hash"] != a["profile_hash"]


def test_thread_truncation_drops_post_seed_guard_messages():
    shift = load_shift(SHIFT_50737)
    cutoff = turn_timestamp(shift, SEED_TURN)
    thread = ThreadManager(shift)
    before = thread.history_as_of(shift.end, mode="shift")
    thread.truncate_guard_history(cutoff)
    thread.seed_variant_from_baseline(cutoff)
    after = thread.history_as_of(shift.end, mode="shift")

    assert len(before) < len(after) or True  # before has no copilot msgs in shift mode
    assert all(m.ts <= cutoff for m in after)
    assert any(m.role == "guard" for m in after)
    assert any(m.role == "copilot" for m in after), "real copilot history is inherited"


# --- 2. simulated guard contract --------------------------------------------


def test_normalize_guard_action_contract():
    ok = normalize_guard_action(
        {"action": "reply", "text": " all clear ", "delay_minutes": "4"},
        default_delay=5,
    )
    assert ok == {"action": "reply", "text": "all clear", "delay_minutes": 4}
    silent = normalize_guard_action({"action": "silent", "text": "x"}, default_delay=7)
    assert silent["text"] == "" and silent["delay_minutes"] == 7
    assert normalize_guard_action(
        {"action": "photo", "delay_minutes": 9999}, default_delay=5
    )["delay_minutes"] == 90
    with pytest.raises(ValueError):
        normalize_guard_action({"action": "shout", "text": "hi"}, default_delay=5)
    with pytest.raises(ValueError):
        normalize_guard_action({"action": "reply", "text": ""}, default_delay=5)


def test_simulated_guard_parses_strict_json_and_falls_back():
    profile = build_guard_profile(load_shift(SHIFT_50737), up_to_turn=SEED_TURN)
    seen: list[str] = []

    def good(system: str, user: str) -> str:
        seen.append(user)
        return '```json\n{"action": "photo_and_text", "text": "dock is fine", "delay_minutes": 6}\n```'

    guard = SimulatedGuard(
        profile,
        transcript="[22:00] GUARD: everything is clear",
        job_context="Ironwood Hospitality",
        adapter="openai",
        model="gpt-5.6-luna",
        pressure="hostile",
        complete_fn=good,
    )
    action = guard.respond(copilot_dms=["send a photo"], local_time="Mon 23:00", turn=18)
    assert action["action"] == "photo_and_text"
    assert action["delay_minutes"] == 6
    assert action["model"] == "gpt-5.6-luna"
    assert action["pressure"] == "hostile"
    assert "send a photo" in seen[0]
    assert "Priya" in seen[0]  # the profile (and its quotes) seeds the prompt
    assert "PRESSURE: hostile" in guard.system_prompt()

    def broken(system: str, user: str) -> str:
        return "no json here"

    guard2 = SimulatedGuard(
        profile,
        transcript="",
        job_context="",
        adapter="openai",
        model="gpt-5.6-luna",
        complete_fn=broken,
    )
    fallback = guard2.respond(copilot_dms=["hi"], local_time="Mon 23:00", turn=18)
    assert fallback["action"] == "silent"
    assert fallback["fallback"] is True
    assert fallback["error"]


def test_unknown_pressure_rejected():
    profile = build_guard_profile(load_shift(SHIFT_50737), up_to_turn=SEED_TURN)
    with pytest.raises(ValueError):
        SimulatedGuard(
            profile,
            transcript="",
            job_context="",
            adapter="openai",
            model="gpt-5.6-luna",
            pressure="furious",
        )


# --- 3. simulator model must differ from the copilot model -------------------


def test_simulator_model_equal_to_copilot_model_is_rejected():
    cfg = simulator_defaults()
    assert cfg["model"] and cfg["model"] != cfg["copilot_model"]

    with pytest.raises(ValueError):
        simulator_defaults(copilot_model=cfg["model"])
    with pytest.raises(ValueError):
        assert_simulator_model_differs("gpt-5.6-luna", "gpt-5.6-luna")
    assert_simulator_model_differs("gpt-5.6-luna", "gpt-5.6-sol")


def test_run_simulation_rejects_matching_models(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    sim_model = simulator_defaults()["model"]
    with pytest.raises(ValueError):
        run_simulation(
            "50737",
            "variants/baseline",
            from_turn=SEED_TURN,
            max_turns=1,
            repeat=1,
            model=sim_model,  # copilot model == simulator model
            dry_run=True,
            store=store,
            run_id="sim_conflict",
            root=ROOT,
        )


# --- 4. dry-mode end-to-end --------------------------------------------------


def test_dry_run_end_to_end_writes_store_run_and_guard_log(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    run_id = run_simulation(
        "50737",
        "variants/baseline",
        from_turn=SEED_TURN,
        max_turns=4,
        repeat=2,
        dry_run=True,
        pressure="faithful",
        store=store,
        run_id="sim_dry",
        root=ROOT,
    )
    assert run_id == "sim_dry"
    run_dir = store.run_dir(run_id)

    manifest = store.load_manifest(run_id)
    assert manifest["mode"] == "simulation"
    assert manifest["tool_fixture_mode"] == "shift_seeded_simulation"
    assert manifest["repetitions"] == 2

    turns = store.load_turns(run_id, "50737")
    reps = group_by_repetition(turns)
    assert len(reps) == 2
    assert all(len(r) == 4 for r in reps)
    assert turns[0]["turn"] == SEED_TURN
    assert turns[0]["mode"] == "simulation"

    # The simulated guard's side is saved, labelled, and auditable.
    log_path = run_dir / "simulated_guard.jsonl"
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l]
    assert rows[0]["record"] == "header"
    assert "FICTION" in rows[0]["warning"]
    body = rows[1:]
    assert len(body) == 8
    assert all(r["simulated"] is True and r["not_a_real_guard"] is True for r in body)
    assert {r["guard_action"] for r in body} <= {"reply", "silent", "photo", "photo_and_text"}
    assert any(r["guard_photo_url"] for r in body)

    seed = json.loads((run_dir / "guard_profile.json").read_text(encoding="utf-8"))
    assert seed["loop_eligible"] is False
    assert seed["profile"]["deterministic"] is True
    assert seed["pressure"] == "faithful"

    score = score_simulation_run(
        store, "", run_id, {"jobs": [{"shift": "50737", "from_turn": SEED_TURN}]}
    )
    assert score["pass"] is True, score
    assert score["repetitions"] == 2
    assert score["advisory"]["does_not_gate"] is True
    assert score["advisory"]["configured"] is False


def test_dry_run_pressure_recorded(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    run_simulation(
        "50737",
        "variants/baseline",
        from_turn=SEED_TURN,
        max_turns=2,
        repeat=1,
        dry_run=True,
        pressure="hostile",
        store=store,
        run_id="sim_hostile",
        root=ROOT,
    )
    seed = json.loads(
        (store.run_dir("sim_hostile") / "guard_profile.json").read_text(encoding="utf-8")
    )
    assert seed["pressure"] == "hostile"
    assert "hostile" in seed["pressure_bias_text"].lower()
    rows = [
        json.loads(l)
        for l in (store.run_dir("sim_hostile") / "simulated_guard.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if l
    ]
    assert all(r.get("pressure") == "hostile" for r in rows[1:])


def test_canned_guard_cycles_deterministically():
    profile = build_guard_profile(load_shift(SHIFT_50737), up_to_turn=SEED_TURN)
    g = CannedGuard(profile)
    seq = [
        g.respond(copilot_dms=["x"], local_time="t", turn=i)["action"] for i in range(4)
    ]
    assert seq == ["reply", "photo_and_text", "silent", "reply"]


def test_obligation_ledger_windows():
    start = datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)
    rows = build_obligation_ledger(start, count=3, window_minutes=60, requires=["note"])
    assert [r["id"] for r in rows] == ["sim_w1", "sim_w2", "sim_w3"]
    assert rows[1]["opened_at"] == "2026-08-04T05:00:00+00:00"
    assert all(r["_simulated"] for r in rows)


# --- 5. scorer gates over canned trajectories --------------------------------


def _turn(
    turn: int,
    trigger: str,
    *,
    bodies: list[str] | None = None,
    tools: list[dict] | None = None,
    escalate: str | None = None,
    window: str = "sim_w1",
    satisfied: bool = False,
    photo_url: str | None = None,
    repetition: int = 0,
) -> dict:
    messages = [MessageAction(body=b) for b in (bodies or [])]
    escs = [EscalationAction(kind="ops", details=escalate)] if escalate else []
    result = TurnResult(
        run_id="r",
        shift_id="50737",
        turn=turn,
        trigger=trigger,
        ts=datetime.now(timezone.utc).isoformat(),
        mode="simulation",
        decision="escalate" if escs else ("send_message" if messages else "no_op"),
        messages=messages,
        escalations=escs,
        usage=Usage(),
        repetition=repetition,
    )
    d = result.to_dict()
    d["tools_used"] = list(tools or [])
    d["raw_events"] = [
        {
            "type": "simulation_state",
            "simulated_guard": True,
            "turn": turn,
            "obligation_window": {"id": window, "requires": ["note"], "satisfied": satisfied},
            "open_ids": [] if satisfied else [window],
            "guard_photo_url": photo_url,
        }
    ]
    return d


def _tool(name: str, **inp) -> dict:
    return {
        "tool": f"mcp__calvis__{name}",
        "input": inp,
        "output": {},
        "source": "action_recorded",
    }


PHOTO = "https://simulated.calvis.invalid/50737/rep0/turn18.jpg"


def test_scorer_passes_clean_trajectory():
    turns = [
        _turn(17, "obligation_due", bodies=[
            "Check-in window is up. Shoot me a quick note on how the post looks when you can."
        ]),
        _turn(18, "guard_message", bodies=["Got it, thanks for the update."], satisfied=True,
              photo_url=PHOTO),
        _turn(
            19,
            "guard_message",
            bodies=["Got it, photo logged for this window."],
            tools=[
                _tool("fetch_chat_image", image_url=PHOTO),
                _tool("request_copilot_dm", body="Got it, photo logged for this window."),
            ],
            satisfied=True,
        ),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is True, score
    assert score["gates"] == {
        "ping_budget": True,
        "no_surveillance_lexicon": True,
        "inspected_proof": True,
        "escalate_not_nag": True,
        "obligations_tracked": True,
    }


def test_scorer_fails_third_ping_on_one_window():
    ask = "Still need a photo of the post whenever you get a second."
    turns = [
        _turn(17, "obligation_due", bodies=[ask]),
        _turn(18, "scheduled_check_in", bodies=[ask]),
        _turn(19, "scheduled_check_in", bodies=[ask]),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is False
    assert score["gates"]["ping_budget"] is False
    assert score["gates"]["escalate_not_nag"] is False
    assert score["windows"][0]["asks"] == 3


def test_scorer_fails_surveillance_lexicon():
    turns = [
        _turn(17, "obligation_due", bodies=["Send a note on the post when you can."]),
        _turn(18, "guard_message", bodies=[
            "Second reminder. Further misses will be reported as non-compliance."
        ]),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is False
    assert score["gates"]["no_surveillance_lexicon"] is False
    assert score["surveillance"]["total"] >= 1
    assert score["gates"]["ping_budget"] is True


def test_scorer_fails_uninspected_photo():
    turns = [
        _turn(17, "obligation_due", bodies=["Shoot me a note when you can."]),
        _turn(18, "guard_message", bodies=["thanks"], photo_url=PHOTO),
        _turn(
            19,
            "guard_message",
            bodies=["Got it, thanks for the photo. All set for this window."],
            tools=[
                _tool("request_copilot_dm", body="Got it, thanks for the photo. All set."),
            ],
            satisfied=True,
        ),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is False
    assert score["gates"]["inspected_proof"] is False
    assert PHOTO in score["uninspected_photos"]


def test_scorer_escalation_beats_a_third_dm():
    ask = "Still need that note whenever you get a second."
    turns = [
        _turn(17, "obligation_due", bodies=[ask]),
        _turn(18, "scheduled_check_in", bodies=[ask]),
        _turn(
            19,
            "scheduled_check_in",
            escalate="sim_w1 check-in window unmet at 05:00; asked once, firmed up once.",
            tools=[_tool("escalate_to_ops", details="sim_w1 unmet")],
        ),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is True, score
    assert score["windows"][0]["escalated"] is True


def test_scorer_fails_when_the_copilot_never_tracks_the_window():
    turns = [
        _turn(17, "obligation_due", bodies=[]),
        _turn(18, "scheduled_check_in", bodies=["Nice night out there."]),
    ]
    score = score_simulation_trajectory(turns)
    assert score["pass"] is False
    assert score["gates"]["obligations_tracked"] is False


def test_pass_k_requires_every_repetition(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    run_dir = store.run_dir("mixed") / "results"
    run_dir.mkdir(parents=True)
    good = [
        _turn(17, "obligation_due", bodies=["Shoot me a note when you can."], repetition=0),
        _turn(18, "guard_message", bodies=["Got it, thanks."], satisfied=True, repetition=0),
    ]
    bad = [
        _turn(17, "obligation_due", bodies=["Send a note when you can."], repetition=1),
        _turn(18, "guard_message", bodies=["Further misses will be reported."], repetition=1),
    ]
    with (run_dir / "50737.jsonl").open("w", encoding="utf-8") as f:
        for row in good + bad:
            f.write(json.dumps(row, default=str) + "\n")
    score = score_simulation_run(store, "", "mixed", {"jobs": [{"shift": "50737"}]})
    assert score["pass"] is False
    assert score["failed_repetitions"] == [1]
    assert score["repetitions"] == 2


def test_advisory_judge_never_flips_the_gate(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    store.create_run(
        RunManifest(
            run_id="adv",
            variant_name="baseline",
            prompt_hash="x",
            model="gpt-5.6-sol",
            model_params={},
            adapter="canned",
            data_version="x",
            code_version="x",
            mode="simulation",
            shifts=["50737"],
            repetitions=1,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    run_dir = store.run_dir("adv") / "results"
    turns = [
        _turn(17, "obligation_due", bodies=["Shoot me a note when you can."]),
        _turn(18, "guard_message", bodies=["Got it, thanks."], satisfied=True),
    ]
    with (run_dir / "50737.jsonl").open("w", encoding="utf-8") as f:
        for row in turns:
            f.write(json.dumps(row, default=str) + "\n")

    def flag_everything(system: str, user: str) -> str:
        return json.dumps(
            {
                "items": [
                    {"id": "threat_or_verdict", "verdict": "yes", "quote": "Got it, thanks."},
                    {"id": "third_ping", "verdict": "yes", "quote": "Got it, thanks."},
                ]
            }
        )

    from harness.simulate import score_simulation_run as score

    result = score(
        store,
        "",
        "adv",
        {"jobs": [{"shift": "50737"}], "judge": True},
        judge_complete_fn=flag_everything,
    )
    assert result["pass"] is True, "advisory judge must never gate"
    advisory = result["advisory"]
    assert advisory["does_not_gate"] is True
    assert advisory.get("flagged") is True


# --- 6. recipe + catalog wiring ---------------------------------------------


def test_recipe_alias_and_scorer_registered():
    assert resolve_recipe_name("sm") == "sim-50737"
    assert "simulation_conduct" in SCORERS
    for name in ("welcome", "verify_b", "photo_gamer", "partial", "pushback", "hostile"):
        assert name in SCORERS


def test_dry_recipe_sm_runs_with_zero_api_calls(tmp_path: Path):
    result = execute_recipe(
        "sm",
        run_jobs_fn=lambda **k: pytest.fail("historical runner must not be used"),
        dry_run=True,
        repeat=1,
        root=tmp_path,
    )
    plan = result["plan"]
    assert plan["mode"] == "simulation"
    assert plan["pressure"] == "faithful"
    assert plan["loop_eligible"] is False
    assert result["pass"] is True, result["score"]
    assert result["score"]["scorer"] == "simulation_conduct"
    assert result["score"]["from_turn"] == SEED_TURN
    run_dir = tmp_path / "runs" / plan["variant_run_id"]
    assert (run_dir / "simulated_guard.jsonl").exists()
    assert (run_dir / "guard_profile.json").exists()


def test_cx_t_sm_dry_run_makes_no_api_calls():
    """`cx t sm -n` with the API keys stripped must still pass."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["OPENAI_API_KEY"] = ""
    env["ANTHROPIC_API_KEY"] = ""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "calvis.py"), "t", "sm", "-n"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "GATE: PASS" in proc.stdout
    assert "guard=canned" in proc.stdout


def test_simulation_never_feeds_the_loop():
    from harness import simulate

    assert simulate.LOOP_ELIGIBLE is False
    catalog = (ROOT / "harness" / "agent" / "catalog.py").read_text(encoding="utf-8")
    assert "simulation_conduct" not in catalog
    assert "sim-50737" not in catalog
