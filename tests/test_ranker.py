"""LLM ranker: faked responses, validator corrections, `cx go -n --rank` output."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.router import (
    SMOKE_ID,
    compile_plan,
    format_ranked_go_output,
    parse_plan_json,
    plan_core,
    rank_plan,
    router_defaults,
    validate_ranked_plan,
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
    return {"description": "x", "scorer": "welcome", "card": base}


def fake_book() -> dict:
    return {
        "defaults": {
            "model": "gpt-5.6-sol",
            "router": {"model": "gpt-4.1-mini"},
        },
        "recipes": {
            "smoke-welcome": _card(
                intent=["smoke", "welcome"],
                risk_class="smoke",
                cost="turn-cheap",
                est_usd=0.05,
                covers=["welcome"],
                required_when={"files": ["session_start.md"], "intents": ["smoke"]},
                after=[],
            ),
            "safe-foo": _card(
                intent=["escalation", "safety"],
                risk_class="safety",
                cost="full-shift",
                est_usd=5.0,
                covers=["escalation_ladder"],
                required_when={"files": ["foo.md"], "intents": ["escalation"]},
            ),
            "lift-foo": _card(
                intent=["quietness"],
                risk_class="lift",
                cost="turn-cheap",
                est_usd=1.0,
                covers=["dm_rate"],
                required_when={"files": ["foo.md"], "intents": ["quietness"]},
            ),
            "lift-baz": _card(
                intent=["claims"],
                risk_class="lift",
                cost="multi-turn",
                est_usd=3.0,
                covers=["claims_verification"],
                required_when={"files": ["baz.md"], "intents": ["claims"]},
            ),
            "comp-bar": _card(
                intent=["voice"],
                risk_class="compliance",
                cost="multi-turn",
                est_usd=2.0,
                covers=["voice_rules"],
                required_when={"files": ["bar.md"], "intents": ["voice"]},
            ),
        },
    }


def _cards(book: dict) -> dict:
    return {rid: dict(rec.get("card") or {}) for rid, rec in book["recipes"].items()}


def _has(corrections: list[str], needle: str) -> bool:
    return any(needle in c for c in corrections)


# Compiler: smoke must, lift-foo should, budget 2.0 USD.
# Violating ranker: drops must_run, invents an id, pulls extra should_run
# past the budget, and puts smoke-welcome last.
def _compiler():
    return compile_plan(
        changed_files=["foo.md"],
        budget=2.0,
        recipes_data=fake_book(),
    )


VIOLATING = {
    "must_run": [],
    "should_run": ["lift-foo", "not-a-recipe", "lift-baz", "comp-bar"],
    "skip": [
        {"id": "smoke-welcome", "reason": "dropped"},
        {"id": "safe-foo", "reason": "x"},
    ],
    "order": ["lift-foo", "not-a-recipe", "lift-baz", "comp-bar", "smoke-welcome"],
    "budget_usd": 99,
    "reasons": [
        {"id": "lift-baz", "action": "pull", "reason": "claims"},
        {"id": "comp-bar", "action": "pull", "reason": "voice"},
    ],
}


def test_router_model_differs_from_copilot_model():
    data = json.loads((ROOT / "experiments" / "recipes.json").read_text(encoding="utf-8"))
    copilot = (data.get("defaults") or {}).get("model")
    cfg = router_defaults(data)
    assert cfg["model"]
    assert cfg["model"] != copilot


def test_validator_applies_all_four_corrections():
    compiler = _compiler()
    book = fake_book()
    plan, corrections = validate_ranked_plan(
        VIOLATING,
        compiler,
        catalog=list(book["recipes"]),
        cards=_cards(book),
        budget_cap=compiler.get("budget_cap"),
    )
    assert _has(corrections, "re-inserted dropped must_run")
    assert _has(corrections, "unknown recipe id")
    assert _has(corrections, "re-applied budget cap")
    assert _has(corrections, "moved smoke-welcome to first in order")

    assert plan["must_run"][0] == SMOKE_ID
    assert SMOKE_ID in plan["must_run"]
    assert "not-a-recipe" not in plan["must_run"]
    assert "not-a-recipe" not in plan["should_run"]
    assert "not-a-recipe" not in {s["id"] for s in plan["skip"]}
    assert "not-a-recipe" not in plan["order"]
    assert plan["order"][0] == SMOKE_ID
    # budget 2.0: smoke 0.05 + lift-foo 1.0 fits; lift-baz 3.0 and comp-bar 2.0 trim
    assert "lift-baz" not in plan["should_run"]
    assert "comp-bar" not in plan["should_run"]
    catalog = set(book["recipes"])
    combined = set(plan["must_run"]) | set(plan["should_run"]) | {s["id"] for s in plan["skip"]}
    assert combined == catalog


def test_rank_plan_faked_llm_proves_all_four_validator_corrections():
    calls = {"n": 0}

    def fake_llm(system: str, user: str, model: str) -> str:
        calls["n"] += 1
        assert "never declare pass/fail" in system.lower()
        assert "compiler_plan" in user
        assert "recipe_cards" in user
        return json.dumps(VIOLATING)

    result = rank_plan(
        _compiler(),
        recipes_data=fake_book(),
        intent="quietness",
        complete_fn=fake_llm,
    )
    assert calls["n"] == 1
    assert result.fallback is False
    assert result.ranker_model == "gpt-4.1-mini"
    assert _has(result.corrections, "re-inserted dropped must_run")
    assert _has(result.corrections, "unknown recipe id")
    assert _has(result.corrections, "re-applied budget cap")
    assert _has(result.corrections, "moved smoke-welcome to first in order")
    ranked = plan_core(result.ranked_plan)
    assert ranked["order"][0] == SMOKE_ID
    assert "not-a-recipe" not in ranked["order"]
    blob = json.dumps(result.ranked_plan)
    assert "PASS" not in blob
    assert "FAIL" not in blob
    assert "GATE" not in blob


def test_rank_plan_falls_back_on_invalid_json_after_retry():
    calls = {"n": 0}

    def fake_llm(system: str, user: str, model: str) -> str:
        calls["n"] += 1
        return "Sure, I would run the quietness recipe because it looks good."

    compiler = _compiler()
    result = rank_plan(compiler, recipes_data=fake_book(), complete_fn=fake_llm)
    assert calls["n"] == 2
    assert result.fallback is True
    assert result.fallback_reason
    assert result.ranked_plan["must_run"] == compiler["must_run"]
    assert result.ranked_plan["order"] == compiler["order"]


def test_parse_plan_json_rejects_prose_and_fences():
    import pytest

    with pytest.raises(ValueError):
        parse_plan_json("```json\n{}\n```")
    with pytest.raises(ValueError):
        parse_plan_json("here is the plan: {\"must_run\": []}")


def test_format_go_output_rank_prints_both_plans_and_diff():
    ranked = rank_plan(
        _compiler(),
        recipes_data=fake_book(),
        complete_fn=lambda *_a: json.dumps(VIOLATING),
    )
    text = format_ranked_go_output(_compiler(), ranked)
    assert "=== compiler plan ===" in text
    assert "=== ranked plan ===" in text
    assert "=== plan diff ===" in text
    assert "GATE" not in text
    assert "PASS" not in text
    assert "FAIL" not in text
    assert "ranker validator corrections:" in text


def test_cx_go_n_without_rank_unchanged():
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
    assert "=== eval plan ===" in out
    assert "=== ranked plan ===" not in out
    assert "=== compiler plan ===" not in out
    assert "plan diff" not in out
    assert "Touched files" in out


def test_cx_go_n_rank_prints_compiler_ranked_and_diff(monkeypatch, capsys):
    compiler = compile_plan(changed_files=["scheduled_check_in.md"])

    def fake(system, user, model, *args, **kwargs):
        payload = plan_core(compiler)
        skip_ids = [s["id"] for s in payload["skip"]]
        pulled = "b-claims" if "b-claims" in skip_ids else (skip_ids[0] if skip_ids else None)
        if pulled:
            payload["should_run"] = list(payload["should_run"]) + [pulled]
            payload["skip"] = [s for s in payload["skip"] if s["id"] != pulled]
            payload["order"] = list(payload["must_run"]) + list(payload["should_run"])
            payload["reasons"] = [
                {"id": pulled, "action": "pull", "reason": "faked ranker pull"}
            ]
        else:
            payload["reasons"] = []
        payload["budget_usd"] = payload.get("budget_usd") or 0
        return json.dumps(payload)

    monkeypatch.setattr("harness.router._openai_rank", fake)
    from calvis import main

    main(["go", "-n", "--rank", "--files", "scheduled_check_in.md"])
    out = capsys.readouterr().out
    assert "=== compiler plan ===" in out
    assert "=== ranked plan ===" in out
    assert "=== plan diff ===" in out
    assert "GATE: PASS" not in out
    assert "GATE: FAIL" not in out
    assert "ranker model:" in out
    assert "not a verdict" in out
