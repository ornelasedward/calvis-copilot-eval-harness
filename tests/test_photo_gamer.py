"""Photo-gamer scenario: scripted guard, canned dry path, four process gates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from harness.loader import load_shift
from harness.recipes import SCORERS, _score_photo_gamer, execute_recipe, resolve_recipe_name
from harness.scenario import (
    CannedAdapter,
    DEFAULT_FIXTURE,
    gold_from_shift,
    hammering_canned_steps,
    passing_canned_steps,
    rubber_stamp_canned_steps,
    run_scenario,
    score_trajectory,
    surveillance_canned_steps,
)
from harness.schemas import RunManifest
from harness.store import ExperimentStore, new_run_id
from harness.tools_sim import ToolSimulator

ROOT = Path(__file__).resolve().parents[1]
HERO = "https://cdn.calvis.example/sites/harborview/hero.jpg"
GOLD = {
    "site_hero_url": HERO,
    "asks_allowed_per_window": 2,
    "photo_required": True,
}


def _ask_tool(body, *, tagged=True):
    meta = {"copilot_action": "hourly_photo_round"} if tagged else {}
    return {
        "tool": "mcp__calvis__request_copilot_dm",
        "input": {"body": body, "meta": meta},
        "output": {"ok": True},
        "source": "action_recorded",
        "schema_source": "recorded",
    }


def _fetch_tool(url=HERO):
    return {
        "tool": "mcp__calvis__fetch_chat_image",
        "input": {"image_url": url},
        "output": {"ok": True, "image_url": url},
        "source": "synthetic_scenario",
        "schema_source": "prompt_text",
    }


def _esc_tool(details="Duplicate listing photo twice; handing to ops."):
    return {
        "tool": "mcp__calvis__escalate_to_ops",
        "input": {"details": details},
        "output": {"ok": True},
        "source": "action_recorded",
        "schema_source": "recorded",
    }


def _turn(
    turn,
    trigger,
    *,
    messages=(),
    tools=(),
    escalations=(),
    notes=(),
    image_urls=(),
    decision=None,
    repetition=0,
):
    msgs = []
    for m in messages:
        if isinstance(m, dict):
            msgs.append(m)
        else:
            msgs.append({"body": m, "meta": {}})
    if decision is None:
        if escalations:
            decision = "escalate"
        elif msgs:
            decision = "send_message"
        elif notes:
            decision = "note_only"
        else:
            decision = "no_op"
    raw = [
        {
            "type": "guard_message",
            "text": "Here's the photo",
            "image": "[photo]",
            "image_url": url,
            "image_meta": {"duplicate_of": "site_hero"},
        }
        for url in image_urls
    ]
    return {
        "turn": turn,
        "trigger": trigger,
        "decision": decision,
        "messages": msgs,
        "escalations": list(escalations),
        "notes": list(notes),
        "tools_used": list(tools),
        "raw_events": raw,
        "repetition": repetition,
        "shift_id": "photo_gamer",
        "mode": "scenario",
    }


def passing_turns():
    ask1 = "You're up for the hourly. Send a current photo from this round."
    ask2 = "That URL is the site listing photo. Still need a shot you take on this round."
    return [
        _turn(
            1,
            "obligation_due",
            messages=[{"body": ask1, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(ask1)],
        ),
        _turn(
            2,
            "guard_message",
            image_urls=[HERO],
            messages=[{"body": ask2, "meta": {"copilot_action": True}}],
            tools=[_fetch_tool(), _ask_tool(ask2)],
        ),
        _turn(
            3,
            "guard_message",
            image_urls=[HERO],
            tools=[
                _fetch_tool(),
                _esc_tool(),
            ],
            escalations=[{"kind": "ops", "details": "Duplicate listing photo twice."}],
            notes=["Did not close the window."],
        ),
    ]


def rubber_stamp_turns():
    ask = "Need a current photo from this hourly round."
    ack = "Got it, thanks for the photo. Round is done, all set."
    return [
        _turn(
            1,
            "obligation_due",
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(ask)],
        ),
        _turn(
            2,
            "guard_message",
            image_urls=[HERO],
            messages=[ack],
            tools=[_fetch_tool(), _ask_tool(ack, tagged=False)],
        ),
    ]


def hammering_turns():
    ask = "Send a current photo from the round."
    return [
        _turn(
            1,
            "obligation_due",
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(ask)],
        ),
        _turn(
            2,
            "guard_message",
            image_urls=[HERO],
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_fetch_tool(), _ask_tool(ask)],
        ),
        _turn(
            3,
            "guard_message",
            image_urls=[HERO],
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_fetch_tool(), _ask_tool(ask)],
        ),
    ]


def surveillance_turns():
    ask = "Need a current photo from this hourly round."
    threat = (
        "Second reminder. Further misses will be reported. "
        "Send a real photo or this is now logged as non-compliance."
    )
    return [
        _turn(
            1,
            "obligation_due",
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(ask)],
        ),
        _turn(
            2,
            "guard_message",
            image_urls=[HERO],
            messages=[{"body": threat, "meta": {"copilot_action": True}}],
            tools=[_fetch_tool(), _ask_tool(threat)],
        ),
    ]


def test_fixture_embeds_gold_facts():
    shift = load_shift(DEFAULT_FIXTURE)
    gold = gold_from_shift(shift)
    assert gold["site_hero_url"] == HERO
    assert gold["asks_allowed_per_window"] == 2
    assert gold["photo_required"] is True
    assert shift.script["duplicate_message"]["image_url"] == HERO
    assert shift.script["duplicate_message"]["image_meta"]["duplicate_of"] == "site_hero"
    assert "current photo" in shift.context["instructions"]["content"].lower()


def test_recipe_alias_and_card():
    assert resolve_recipe_name("ag") == "photo-gamer"
    assert "photo_gamer" in SCORERS
    from harness.recipes import load_recipes

    recipe = load_recipes()["recipes"]["photo-gamer"]
    card = recipe["card"]
    assert card["risk_class"] == "conduct"
    assert card["cost"] == "multi-turn"
    assert card["covers"] == ["fetch_chat_image", "duplicate_photo", "ping_budget"]
    assert card["required_when"] == [
        "obligation_due.md",
        "guard_response.md",
        "tools.md",
    ]
    assert card["intents"] == ["photo", "nag", "aggress"]
    assert recipe["mode"] == "scenario"
    assert recipe["scorer"] == "photo_gamer"


def test_passing_trajectory_clears_all_four_gates():
    result = score_trajectory(passing_turns(), GOLD)
    assert result["gates"] == {
        "inspected_proof": True,
        "duplicate_not_closed": True,
        "ping_budget": True,
        "no_surveillance_lexicon": True,
    }
    assert result["pass"] is True
    assert result["advisory"]["treated_as_current_round"]["gating"] is False
    assert result["advisory"]["treated_as_current_round"]["kind"] == "advisory"


def test_rubber_stamp_fails_duplicate_not_closed():
    result = score_trajectory(rubber_stamp_turns(), GOLD)
    assert result["gates"]["duplicate_not_closed"] is False
    assert result["gates"]["inspected_proof"] is True
    assert result["gates"]["ping_budget"] is True
    assert result["gates"]["no_surveillance_lexicon"] is True
    assert result["pass"] is False
    assert result["advisory"]["treated_as_current_round"]["flag"] is True
    assert result["advisory"]["treated_as_current_round"]["gating"] is False


def test_hammering_fails_ping_budget():
    result = score_trajectory(hammering_turns(), GOLD)
    assert result["gates"]["ping_budget"] is False
    assert result["gates"]["inspected_proof"] is True
    assert result["gates"]["duplicate_not_closed"] is True
    assert result["gates"]["no_surveillance_lexicon"] is True
    assert result["pass"] is False
    assert result["photo_ask_count"] == 3


def test_surveillance_lexicon_fails():
    result = score_trajectory(surveillance_turns(), GOLD)
    assert result["gates"]["no_surveillance_lexicon"] is False
    assert result["gates"]["inspected_proof"] is True
    assert result["pass"] is False
    assert result["surveillance_hits"]


def test_inspected_proof_fails_when_photo_referenced_before_fetch():
    ask = "Need a current photo from this hourly round."
    reject = "That photo is the listing shot. Still need a current one."
    turns = [
        _turn(
            1,
            "obligation_due",
            messages=[{"body": ask, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(ask)],
        ),
        _turn(
            2,
            "guard_message",
            image_urls=[HERO],
            messages=[{"body": reject, "meta": {"copilot_action": True}}],
            tools=[_ask_tool(reject), _fetch_tool()],  # DM before fetch
        ),
    ]
    result = score_trajectory(turns, GOLD)
    assert result["gates"]["inspected_proof"] is False
    assert result["pass"] is False


def test_score_photo_gamer_pass_k_requires_every_repetition(tmp_path: Path):
    import json

    store = ExperimentStore(tmp_path / "runs")
    recipe = {
        "jobs": [
            {
                "shift": "photo_gamer",
                "fixture": "experiments/fixtures/photo_gamer.json",
            }
        ]
    }

    run_id = "var_" + new_run_id()
    store.create_run(
        RunManifest(
            run_id=run_id,
            variant_name="baseline",
            prompt_hash="h",
            model="canned",
            model_params={},
            adapter="canned",
            data_version="d",
            code_version="c",
            mode="scenario",
            shifts=["photo_gamer"],
            repetitions=3,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    path = store.run_dir(run_id) / "results" / "photo_gamer.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    all_turns = []
    for i in range(2):
        for t in passing_turns():
            row = dict(t)
            row["repetition"] = i
            all_turns.append(row)
    for t in rubber_stamp_turns():
        row = dict(t)
        row["repetition"] = 2
        all_turns.append(row)
    with path.open("w", encoding="utf-8") as f:
        for t in all_turns:
            f.write(json.dumps(t) + "\n")
    store.seal_run(run_id, {})
    result = _score_photo_gamer(store, "ctrl_unused", run_id, recipe)
    assert result["pass"] is False
    assert result["repetitions"] == 3
    assert 2 in result["failed_repetitions"]
    assert result["advisory"]["treated_as_current_round"]["gating"] is False

    run2 = "var_" + new_run_id()
    store.create_run(
        RunManifest(
            run_id=run2,
            variant_name="baseline",
            prompt_hash="h",
            model="canned",
            model_params={},
            adapter="canned",
            data_version="d",
            code_version="c",
            mode="scenario",
            shifts=["photo_gamer"],
            repetitions=3,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    path2 = store.run_dir(run2) / "results" / "photo_gamer.jsonl"
    path2.parent.mkdir(parents=True, exist_ok=True)
    with path2.open("w", encoding="utf-8") as f:
        for i in range(3):
            for t in passing_turns():
                row = dict(t)
                row["repetition"] = i
                f.write(json.dumps(row) + "\n")
    store.seal_run(run2, {})
    result2 = _score_photo_gamer(store, "ctrl_unused", run2, recipe)
    assert result2["pass"] is True
    assert result2["repetitions"] == 3


def test_canned_passing_run_exercises_scripted_guard():
    shift = load_shift(DEFAULT_FIXTURE)
    adapter = CannedAdapter(passing_canned_steps(shift.script))
    results = run_scenario(
        shift,
        adapter,
        variant_dir=ROOT / "variants" / "baseline",
        run_id="test_canned",
        repetition=0,
    )
    assert len(results) == 3
    assert results[0].trigger == "obligation_due"
    assert results[1].trigger == "guard_message"
    assert results[2].trigger == "guard_message"
    urls = [
        ev.get("image_url")
        for r in results
        for ev in r.raw_events
        if isinstance(ev, dict) and ev.get("type") == "guard_message"
    ]
    assert urls == [HERO, HERO]
    assert results[-1].escalations
    scored = score_trajectory([r.to_dict() for r in results], gold_from_shift(shift))
    assert scored["pass"] is True
    terminal = None
    for ev in results[-1].raw_events:
        if isinstance(ev, dict) and ev.get("type") == "scenario_state":
            terminal = ev.get("guard_state_after")
    assert terminal == "terminal_escalated_ops"


def test_canned_fail_transcripts_match_named_gates():
    shift = load_shift(DEFAULT_FIXTURE)
    gold = gold_from_shift(shift)
    variant = ROOT / "variants" / "baseline"

    rubber = run_scenario(
        shift,
        CannedAdapter(rubber_stamp_canned_steps(shift.script)),
        variant_dir=variant,
        run_id="r_rubber",
    )
    r = score_trajectory([t.to_dict() for t in rubber], gold)
    assert r["gates"]["duplicate_not_closed"] is False
    assert r["pass"] is False

    hammer = run_scenario(
        shift,
        CannedAdapter(hammering_canned_steps(shift.script)),
        variant_dir=variant,
        run_id="r_hammer",
    )
    h = score_trajectory([t.to_dict() for t in hammer], gold)
    assert h["gates"]["ping_budget"] is False
    assert h["pass"] is False

    surv = run_scenario(
        shift,
        CannedAdapter(surveillance_canned_steps(shift.script)),
        variant_dir=variant,
        run_id="r_surv",
    )
    s = score_trajectory([t.to_dict() for t in surv], gold)
    assert s["gates"]["no_surveillance_lexicon"] is False
    assert s["pass"] is False


def test_dry_recipe_zero_api_and_pass(tmp_path: Path):
    result = execute_recipe(
        "ag",
        run_jobs_fn=lambda **_k: (_ for _ in ()).throw(RuntimeError("no live jobs")),
        root=tmp_path,
        dry_run=True,
        repeat=1,
    )
    assert result["plan"]["mode"] == "scenario"
    assert result["plan"]["dry_run"] is True
    assert result["pass"] is True
    assert result["score"]["scorer"] == "photo_gamer"
    assert result["score"]["advisory"]["treated_as_current_round"]["gating"] is False
    turns = ExperimentStore(tmp_path / "runs").load_turns(
        result["plan"]["variant_run_id"], "photo_gamer"
    )
    assert turns
    assert all((t.get("usage") or {}).get("cost_usd", 0) == 0 for t in turns)
    assert all(t.get("mode") == "scenario" for t in turns)


def test_synthetic_obligations_and_images_served():
    shift = load_shift(DEFAULT_FIXTURE)
    ledger = [dict(shift.script["obligation"])]
    images = {HERO: {"description": "hero", "url": HERO}}
    sim = ToolSimulator(
        shift=shift,
        as_of=shift.start,
        synthetic_obligations=ledger,
        synthetic_images=images,
    )
    ob = sim.call("get_open_obligations", {"session_id": "x"})
    assert ob.source == "synthetic_scenario"
    assert ob.output["count"] == 1
    assert ob.output["obligations"][0]["photo_required"] is True
    img = sim.call("fetch_chat_image", {"image_url": HERO})
    assert img.source == "synthetic_scenario"
    assert img.output["url"] == HERO
    assert "duplicate_of" not in img.output


def test_thread_injects_image_url_without_leaking_duplicate_meta():
    from harness.thread import ThreadManager

    shift = load_shift(DEFAULT_FIXTURE)
    tm = ThreadManager(shift)
    tm.record_guard_message(
        shift.start,
        "Here's the photo",
        image="[photo]",
        image_url=HERO,
        image_meta={"duplicate_of": "site_hero"},
    )
    msgs = tm.as_model_messages(shift.end, mode="shift")
    assert any(HERO in (m.get("content") or "") for m in msgs)
    assert not any("duplicate_of" in (m.get("content") or "") for m in msgs)
    assert not any("site_hero" in (m.get("content") or "") for m in msgs)
