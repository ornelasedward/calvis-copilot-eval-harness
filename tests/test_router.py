"""Compiler tests: membership, ordering, budget, stop rules. No API calls."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.router import (
    SKIP_SAFETY_BANNER,
    compile_plan,
    discover_changed_files,
    execute_plan,
    format_plan_table,
    save_plan,
)

ROOT = Path(__file__).resolve().parents[1]


def _card(**kwargs) -> dict:
    base = {
        "intent": [],
        "risk_class": "lift",
        "cost": "turn-cheap",
        "est_usd": 1.0,
        "covers": [],
        "required_when": {"files": [], "intents": []},
        "after": ["smoke-welcome"],
        "on_fail": [],
    }
    base.update(kwargs)
    if "required_when" in kwargs:
        rw = dict(base["required_when"])
        rw.setdefault("files", [])
        rw.setdefault("intents", [])
        base["required_when"] = rw
    return base


def fake_book() -> dict:
    def rec(card):
        return {"description": "x", "scorer": "welcome", "card": card}

    return {
        "recipes": {
            "smoke-welcome": rec(
                _card(
                    intent=["smoke", "welcome"],
                    risk_class="smoke",
                    cost="turn-cheap",
                    est_usd=0.05,
                    covers=["welcome"],
                    required_when={"files": ["session_start.md"], "intents": ["smoke"]},
                    after=[],
                )
            ),
            "safe-foo": rec(
                _card(
                    intent=["escalation", "safety"],
                    risk_class="safety",
                    cost="full-shift",
                    est_usd=5.0,
                    covers=["escalation_ladder"],
                    required_when={
                        "files": ["foo.md"],
                        "intents": ["escalation", "safety"],
                    },
                )
            ),
            "lift-foo": rec(
                _card(
                    intent=["quietness"],
                    risk_class="lift",
                    cost="turn-cheap",
                    est_usd=1.0,
                    covers=["dm_rate"],
                    required_when={"files": ["foo.md"], "intents": ["quietness"]},
                    on_fail=["safe-foo"],
                )
            ),
            "comp-bar": rec(
                _card(
                    intent=["voice"],
                    risk_class="compliance",
                    cost="multi-turn",
                    est_usd=2.0,
                    covers=["voice_rules"],
                    required_when={"files": ["bar.md"], "intents": ["voice"]},
                )
            ),
            "lift-baz": rec(
                _card(
                    intent=["claims"],
                    risk_class="lift",
                    cost="multi-turn",
                    est_usd=3.0,
                    covers=["claims_verification"],
                    required_when={"files": ["baz.md"], "intents": ["claims"]},
                )
            ),
            "lift-foo-dupe": rec(
                _card(
                    intent=["quietness"],
                    risk_class="lift",
                    cost="multi-turn",
                    est_usd=4.0,
                    covers=["dm_rate"],
                    required_when={"files": ["foo.md"], "intents": ["quietness"]},
                )
            ),
        }
    }


def _skip_map(plan: dict) -> dict[str, str]:
    return {row["id"]: row["reason"] for row in plan["skip"]}


def test_foo_md_safety_must_lift_should_voice_skipped():
    plan = compile_plan(
        changed_files=["instructions/foo.md"],
        recipes_data=fake_book(),
    )
    assert plan["must_run"][0] == "smoke-welcome"
    assert "safe-foo" in plan["must_run"]
    assert "lift-foo" in plan["should_run"]
    skip = _skip_map(plan)
    assert "comp-bar" in skip
    assert "do not match" in skip["comp-bar"]
    assert "lift-baz" in skip
    # cheaper lift covering dm_rate wins; expensive dupe is redundant
    assert "lift-foo-dupe" in skip
    assert "redundant" in skip["lift-foo-dupe"]
    assert plan["order"][0] == "smoke-welcome"
    # cheap lift before expensive safety
    assert plan["order"].index("lift-foo") < plan["order"].index("safe-foo")
    assert set(plan["stop_rules"]) == {
        "smoke fail -> halt",
        "safety fail -> no lift recipes",
    }


def test_intent_escalation_forces_safety_must_run():
    plan = compile_plan(intent="escalation", recipes_data=fake_book())
    assert "smoke-welcome" in plan["must_run"]
    assert "safe-foo" in plan["must_run"]
    assert "lift-foo" not in plan["must_run"]
    assert "lift-foo" not in plan["should_run"]
    assert _skip_map(plan)["comp-bar"]


def test_intent_voice_selects_compliance_should_run():
    plan = compile_plan(intent="voice rules", recipes_data=fake_book())
    assert plan["must_run"] == ["smoke-welcome"]
    assert "comp-bar" in plan["should_run"]
    assert "safe-foo" in _skip_map(plan)


def test_intent_covers_token_overlap_quietness():
    plan = compile_plan(intent="dm_rate", recipes_data=fake_book())
    assert "lift-foo" in plan["should_run"]
    assert "safe-foo" not in plan["must_run"]


def test_budget_trims_should_never_must():
    plan = compile_plan(
        changed_files=["foo.md"],
        budget=5.5,
        recipes_data=fake_book(),
    )
    # must = 0.05 + 5.0 = 5.05; lift-foo 1.0 would exceed 5.5
    assert "safe-foo" in plan["must_run"]
    assert "smoke-welcome" in plan["must_run"]
    assert "lift-foo" not in plan["should_run"]
    assert "trimmed by --budget" in _skip_map(plan)["lift-foo"]


def test_budget_below_must_warns_and_keeps_must():
    plan = compile_plan(
        changed_files=["foo.md"],
        budget=1.0,
        recipes_data=fake_book(),
    )
    assert "safe-foo" in plan["must_run"]
    assert "smoke-welcome" in plan["must_run"]
    assert "lift-foo" not in plan["should_run"]
    assert any("must_run" in w and "exceeds" in w for w in plan["warnings"])
    assert plan["budget_usd"] == 5.05


def test_skip_safety_flag_drops_safety_must_run():
    plan = compile_plan(
        changed_files=["foo.md"],
        skip_safety_i_know=True,
        recipes_data=fake_book(),
    )
    assert plan["must_run"] == ["smoke-welcome"]
    assert "safe-foo" not in plan["must_run"]
    assert "safe-foo" not in plan["should_run"]
    assert "skip-safety-i-know" in _skip_map(plan)["safe-foo"]
    assert any(SKIP_SAFETY_BANNER in w for w in plan["warnings"])
    assert "lift-foo" in plan["should_run"]


def test_no_files_no_intent_only_smoke_must():
    plan = compile_plan(changed_files=[], recipes_data=fake_book())
    assert plan["must_run"] == ["smoke-welcome"]
    assert plan["should_run"] == []
    skip = _skip_map(plan)
    for rid in ("safe-foo", "lift-foo", "comp-bar", "lift-baz"):
        assert rid in skip


def test_order_cheap_before_expensive():
    plan = compile_plan(
        changed_files=["foo.md", "bar.md"],
        recipes_data=fake_book(),
    )
    order = plan["order"]
    assert order[0] == "smoke-welcome"
    # turn-cheap lift-foo, then multi-turn comp-bar, then full-shift safe-foo
    assert order.index("lift-foo") < order.index("comp-bar")
    assert order.index("comp-bar") < order.index("safe-foo")


def test_format_table_lists_skip_reasons():
    plan = compile_plan(
        changed_files=["foo.md"],
        recipes_data=fake_book(),
    )
    table = format_plan_table(plan)
    assert "Touched files" in table
    assert "Must" in table
    assert "Should" in table
    assert "Skip" in table
    assert "Est budget" in table
    assert "comp-bar" in table
    assert "safe-foo" in table


def test_save_plan_writes_timestamped_json(tmp_path: Path):
    plan = compile_plan(changed_files=["foo.md"], recipes_data=fake_book())
    path = save_plan(plan, tmp_path)
    assert path.name.startswith("plan_")
    assert path.suffix == ".json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["must_run"] == plan["must_run"]
    assert loaded["order"] == plan["order"]


def test_execute_halts_on_smoke_fail():
    book = fake_book()
    plan = {
        "order": ["smoke-welcome", "lift-foo", "safe-foo"],
        "must_run": ["smoke-welcome"],
        "should_run": ["lift-foo", "safe-foo"],
    }
    calls: list[str] = []

    def fake_exec(name: str) -> dict:
        calls.append(name)
        return {"pass": False} if name == "smoke-welcome" else {"pass": True}

    out = execute_plan(plan, execute_fn=fake_exec, recipes_data=book)
    assert calls == ["smoke-welcome"]
    assert out["halted"] is True
    assert out["halt_reason"] == "smoke fail -> halt"
    skipped = {s["id"] for s in out["skipped_remaining"]}
    assert skipped == {"lift-foo", "safe-foo"}


def test_execute_skips_remaining_lift_after_safety_fail_keeps_compliance():
    book = fake_book()
    plan = {
        "order": ["smoke-welcome", "safe-foo", "comp-bar", "lift-baz"],
        "must_run": ["smoke-welcome", "safe-foo"],
        "should_run": ["comp-bar", "lift-baz"],
    }
    calls: list[str] = []

    def fake_exec(name: str) -> dict:
        calls.append(name)
        return {"pass": False} if name == "safe-foo" else {"pass": True}

    out = execute_plan(plan, execute_fn=fake_exec, recipes_data=book)
    assert calls == ["smoke-welcome", "safe-foo", "comp-bar"]
    assert out["safety_failed"] is True
    assert out["halted"] is False
    skipped = {s["id"]: s["reason"] for s in out["skipped_remaining"]}
    assert "lift-baz" in skipped
    assert "no lift" in skipped["lift-baz"]


def test_real_recipes_scheduled_check_in_selects_es_and_qt_skips_vo():
    plan = compile_plan(changed_files=["scheduled_check_in.md"])
    assert plan["order"][0] == "smoke-welcome"
    assert "smoke-welcome" in plan["must_run"]
    assert "a3-shift-55252" in plan["must_run"]
    assert "a3-quiet-probe" in plan["should_run"]
    skip = _skip_map(plan)
    assert "c-voice" in skip
    assert skip["c-voice"]
    assert "b-claims" in skip
    # cheap quietness before expensive full-shift safety
    assert plan["order"].index("a3-quiet-probe") < plan["order"].index("a3-shift-55252")
    table = format_plan_table(plan)
    assert "a3-shift-55252" in table
    assert "a3-quiet-probe" in table
    assert "c-voice" in table


def test_real_recipes_every_card_complete():
    data = json.loads((ROOT / "experiments" / "recipes.json").read_text(encoding="utf-8"))
    required = {
        "intent",
        "risk_class",
        "cost",
        "est_usd",
        "covers",
        "required_when",
        "after",
        "on_fail",
    }
    risk = {"safety", "lift", "conduct", "compliance", "smoke"}
    cost = {"turn-cheap", "multi-turn", "full-shift"}
    for name, rec in data["recipes"].items():
        assert "suggested_when" in rec, name
        card = rec["card"]
        assert required <= set(card), name
        assert card["risk_class"] in risk, name
        assert card["cost"] in cost, name
        assert isinstance(card["est_usd"], (int, float)), name
        assert isinstance(card["intent"], list), name
        assert isinstance(card["covers"], list), name
        assert "files" in card["required_when"] and "intents" in card["required_when"]
        if name != "smoke-welcome":
            assert "smoke-welcome" in card["after"]


def test_discover_variant_a3_includes_scheduled_check_in():
    files = discover_changed_files(ROOT / "variants" / "variant_a3", root=ROOT)
    names = {Path(f).name for f in files}
    assert "scheduled_check_in.md" in names


def test_discover_files_override():
    files = discover_changed_files(
        ROOT / "variants" / "variant_c",
        files_override=["scheduled_check_in.md"],
        root=ROOT,
    )
    assert files == ["scheduled_check_in.md"]


def test_discover_falls_back_to_content_diff(tmp_path: Path, monkeypatch):
    base = tmp_path / "baseline"
    var = tmp_path / "variant"
    (base / "core").mkdir(parents=True)
    (base / "instructions").mkdir()
    (var / "core").mkdir(parents=True)
    (var / "instructions").mkdir()
    (base / "instructions" / "scheduled_check_in.md").write_text("old", encoding="utf-8")
    (var / "instructions" / "scheduled_check_in.md").write_text("new", encoding="utf-8")
    (base / "core" / "identity.md").write_text("same", encoding="utf-8")
    (var / "core" / "identity.md").write_text("same", encoding="utf-8")

    def boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr("harness.router.subprocess.run", boom)
    files = discover_changed_files(var, baseline_dir=base, root=tmp_path)
    assert any(Path(f).name == "scheduled_check_in.md" for f in files)


def test_cx_go_n_scheduled_check_in_offline():
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "calvis.py"),
            "go",
            "-n",
            "--files",
            "scheduled_check_in.md",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = proc.stdout
    assert "Touched files" in out
    assert "Must" in out
    assert "a3-shift-55252" in out
    assert "a3-quiet-probe" in out
    assert "c-voice" in out
    assert "Skip" in out
    assert "Est budget" in out
    assert "wrote" in out
    plans = list((ROOT / "runs").glob("plan_*.json"))
    assert plans, "plan should be saved under runs/"


def test_cx_t_alias_untouched_dry_run():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "calvis.py"), "t", "cl", "-n"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "b-claims" in proc.stdout
    assert "verify_b" in proc.stdout


# ---------------------------------------------------------------------------
# pass^k: `cx go` must gate a recipe as hard as `cx t` does
# ---------------------------------------------------------------------------


def test_execute_plan_honors_each_recipe_repetitions():
    from harness.recipes import load_recipes

    book = load_recipes()
    plan = {"order": ["smoke-welcome", "photo-gamer", "a3-shift-55252", "sim-50737"]}
    seen: dict[str, int | None] = {}

    def fake_exec(name: str, repeat: int | None = None) -> dict:
        seen[name] = repeat
        return {"pass": True}

    out = execute_plan(plan, execute_fn=fake_exec, recipes_data=book)
    # scenario / simulation recipes declare repetitions; turn+shift recipes do not
    assert seen["photo-gamer"] == 3
    assert seen["sim-50737"] == 3
    assert seen["smoke-welcome"] == 1
    assert seen["a3-shift-55252"] == 1
    assert [r["repetitions"] for r in out["results"]] == [1, 3, 1, 3]


def test_execute_plan_still_accepts_a_one_arg_executor():
    book = fake_book()
    calls: list[str] = []

    def fake_exec(name: str) -> dict:
        calls.append(name)
        return {"pass": True}

    execute_plan(
        {"order": ["smoke-welcome", "lift-foo"]}, execute_fn=fake_exec, recipes_data=book
    )
    assert calls == ["smoke-welcome", "lift-foo"]


def test_cmd_go_does_not_pin_repetitions_to_one():
    """cx go used to run scenarios at pass^1 while cx t ran pass^3."""
    source = (ROOT / "cli.py").read_text(encoding="utf-8")
    body = source.split("def cmd_go(")[1].split("\ndef ")[0]
    assert "repeat=1" not in body
    assert "repeat=repeat" in body


# ---------------------------------------------------------------------------
# self-fix hand-off
# ---------------------------------------------------------------------------


def _failed_outcome(recipe: str, mode: str, scorer: str, run_id: str) -> dict:
    return {
        "results": [
            {
                "id": recipe,
                "pass": False,
                "result": {
                    "plan": {
                        "recipe": recipe,
                        "mode": mode,
                        "scorer": scorer,
                        "variant_run_id": run_id,
                        "repetitions": 3,
                    },
                    "score": {"detail": "pass^k failed on repetitions [0]."},
                    "pass": False,
                },
            }
        ]
    }


def test_failed_scenario_is_handed_to_the_loop():
    from harness.router import failed_scenario_runs, format_self_fix_hint

    rows = failed_scenario_runs(
        _failed_outcome("photo-gamer", "scenario", "photo_gamer", "var_photo_gamer_x")
    )
    assert len(rows) == 1 and rows[0]["loop_eligible"] is True
    hint = format_self_fix_hint(rows)
    assert "loop --from-run runs/var_photo_gamer_x" in hint


def test_failed_simulation_is_not_handed_to_the_loop():
    from harness.router import failed_scenario_runs, format_self_fix_hint

    rows = failed_scenario_runs(
        _failed_outcome("sim-50737", "simulation", "simulation_conduct", "var_sim_x")
    )
    assert len(rows) == 1 and rows[0]["loop_eligible"] is False
    hint = format_self_fix_hint(rows)
    assert "simulation-only evidence" in hint
    assert "--from-run" not in hint


def test_passing_plan_has_no_self_fix_hint():
    from harness.router import failed_scenario_runs, format_self_fix_hint

    outcome = _failed_outcome("photo-gamer", "scenario", "photo_gamer", "var_x")
    outcome["results"][0]["pass"] = True
    assert failed_scenario_runs(outcome) == []
    assert format_self_fix_hint([]) == ""
