"""LLM ranker: faked responses, validator corrections, `cx go -n --rank` output."""

from __future__ import annotations

import json
from pathlib import Path

from harness.router import (
    SMOKE,
    catalog_ids,
    compile_plan,
    format_go_output,
    parse_plan_json,
    plan_payload,
    rank_plan,
    run_go,
    validate_ranked_plan,
)

ROOT = Path(__file__).resolve().parents[1]

CATALOG = [
    "smoke-welcome",
    "b-claims",
    "a3-shift-55252",
    "a3-quiet-probe",
    "c-voice",
]

COMPILER = {
    "must_run": ["smoke-welcome"],
    "should_run": ["b-claims"],
    "skip": ["a3-shift-55252", "a3-quiet-probe", "c-voice"],
    "order": ["smoke-welcome", "b-claims"],
    "budget": 2,
}

# Violates all four validator rules: drops must_run, invents an id, exceeds
# budget, and puts smoke-welcome last.
VIOLATING_RANKER = {
    "must_run": [],
    "should_run": [
        "b-claims",
        "not-a-recipe",
        "a3-quiet-probe",
        "a3-shift-55252",
    ],
    "skip": ["smoke-welcome", "c-voice"],
    "order": [
        "b-claims",
        "not-a-recipe",
        "a3-quiet-probe",
        "a3-shift-55252",
        "smoke-welcome",
    ],
    "budget": 99,
    "reasons": [
        {"id": "a3-quiet-probe", "action": "pull", "reason": "quietness related"},
        {"id": "a3-shift-55252", "action": "pull", "reason": "safety related"},
    ],
}


def _has_correction(corrections: list[str], needle: str) -> bool:
    return any(needle in c for c in corrections)


def test_validator_applies_all_four_corrections():
    plan, corrections = validate_ranked_plan(
        VIOLATING_RANKER, COMPILER, catalog=CATALOG, budget=2
    )
    assert _has_correction(corrections, "re-inserted dropped must_run")
    assert _has_correction(corrections, "unknown recipe id")
    assert _has_correction(corrections, "re-applied budget cap")
    assert _has_correction(corrections, "moved smoke-welcome to first in order")

    assert plan["must_run"][0] == SMOKE
    assert SMOKE in plan["must_run"]
    assert "not-a-recipe" not in plan["must_run"]
    assert "not-a-recipe" not in plan["should_run"]
    assert "not-a-recipe" not in plan["skip"]
    assert "not-a-recipe" not in plan["order"]
    assert plan["order"][0] == SMOKE
    assert len(plan["order"]) <= 2
    assert plan["budget"] == 2
    # Catalog partition after corrections.
    combined = plan["must_run"] + plan["should_run"] + plan["skip"]
    assert sorted(combined) == sorted(CATALOG)


def test_rank_plan_faked_llm_proves_all_four_validator_corrections():
    calls = {"n": 0}

    def fake_llm(system: str, user: str, model: str) -> str:
        calls["n"] += 1
        assert "pass/fail" in system.lower() or "NEVER declare pass/fail" in system
        assert "compiler_plan" in user
        assert "recipe_cards" in user
        return json.dumps(VIOLATING_RANKER)

    result = rank_plan(
        COMPILER,
        cards=[{"id": i, "description": i} for i in CATALOG],
        diff={"filenames": ["instructions/guard_response.md"], "unified_diff_excerpt": "diff"},
        intent="check claims",
        catalog=CATALOG,
        model="gpt-4.1-mini",
        complete_fn=fake_llm,
    )
    assert calls["n"] == 1
    assert result.fallback is False
    assert result.ranker_model == "gpt-4.1-mini"
    corrections = result.corrections
    assert _has_correction(corrections, "re-inserted dropped must_run")
    assert _has_correction(corrections, "unknown recipe id")
    assert _has_correction(corrections, "re-applied budget cap")
    assert _has_correction(corrections, "moved smoke-welcome to first in order")
    ranked = plan_payload(result.ranked_plan)
    assert ranked["order"][0] == SMOKE
    assert "not-a-recipe" not in ranked["order"]
    assert ranked["budget"] == 2
    # Ranker output is a plan, not a verdict.
    blob = json.dumps(result.ranked_plan)
    assert "PASS" not in blob
    assert "FAIL" not in blob
    assert "GATE" not in blob


def test_rank_plan_falls_back_on_invalid_json_after_retry():
    calls = {"n": 0}

    def fake_llm(system: str, user: str, model: str) -> str:
        calls["n"] += 1
        return "Sure, I would run the quietness recipe because it looks good."

    result = rank_plan(COMPILER, catalog=CATALOG, complete_fn=fake_llm)
    assert calls["n"] == 2
    assert result.fallback is True
    assert result.fallback_reason
    assert plan_payload(result.ranked_plan) == plan_payload(COMPILER)


def test_parse_plan_json_rejects_prose_and_fences():
    import pytest

    with pytest.raises(ValueError):
        parse_plan_json("```json\n{}\n```")
    with pytest.raises(ValueError):
        parse_plan_json("here is the plan: {\"must_run\": []}")


def test_format_go_output_rank_prints_both_plans_and_diff():
    ranked = rank_plan(
        COMPILER,
        catalog=CATALOG,
        complete_fn=lambda *_a: json.dumps(VIOLATING_RANKER),
    )
    text = format_go_output(compiler=COMPILER, ranked=ranked, rank=True)
    assert "=== compiler plan ===" in text
    assert "=== ranked plan ===" in text
    assert "=== plan diff ===" in text
    assert "GATE" not in text
    assert "PASS" not in text
    assert "FAIL" not in text
    assert "ranker validator corrections:" in text


def test_cx_go_n_rank_prints_compiler_ranked_and_diff(capsys):
    def fake_llm(system: str, user: str, model: str) -> str:
        return json.dumps(
            {
                "must_run": ["smoke-welcome", "a3-shift-55252", "a3-quiet-probe"],
                "should_run": ["b-claims"],
                "skip": ["c-voice"],
                "order": [
                    "smoke-welcome",
                    "a3-shift-55252",
                    "a3-quiet-probe",
                    "b-claims",
                ],
                "budget": 4,
                "reasons": [
                    {
                        "id": "b-claims",
                        "action": "pull",
                        "reason": "intent mentioned verification",
                    }
                ],
            }
        )

    result = run_go(
        target="v3",
        intent="also check claims verification",
        budget=4,
        rank=True,
        dry_run=True,
        complete_fn=fake_llm,
        print_fn=print,
    )
    out = capsys.readouterr().out
    assert "=== compiler plan ===" in out
    assert "=== ranked plan ===" in out
    assert "=== plan diff ===" in out
    assert "GATE" not in out
    assert result["fallback"] is False
    # Ranker pulled b-claims relative to a skip-only compiler classification
    # (or it's already should_run from intent); either way both plans printed.
    assert result["compiler_plan"]["order"][0] == SMOKE
    assert result["ranked_plan"]["order"][0] == SMOKE


def test_cx_go_n_without_rank_unchanged(capsys, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("LLM ranker must stay off without --rank")

    monkeypatch.setattr("harness.router._openai_rank", boom)
    from calvis import main

    main(["go", "v3", "-n"])
    out = capsys.readouterr().out
    assert "=== go plan (compiler) ===" in out
    assert "=== ranked plan ===" not in out
    assert "=== compiler plan ===" not in out
    assert "plan diff" not in out


def test_cx_go_n_rank_via_short_cli(monkeypatch, capsys):
    def fake(system, user, model):
        plan = compile_plan(
            control_dir=ROOT / "variants" / "baseline",
            variant_dir=ROOT / "variants" / "variant_a3",
            budget=4,
        )
        payload = plan_payload(plan)
        # Drop a should_run-able skip into should_run so the printed diff is real.
        skip = list(payload["skip"])
        if skip:
            pulled = skip[0]
            payload["should_run"] = list(payload["should_run"]) + [pulled]
            payload["skip"] = [x for x in skip if x != pulled]
            payload["order"] = list(payload["must_run"]) + list(payload["should_run"])
            payload["reasons"] = [
                {"id": pulled, "action": "pull", "reason": "faked ranker pull"}
            ]
        else:
            payload["reasons"] = []
        return json.dumps(payload)

    monkeypatch.setattr("harness.router._openai_rank", fake)
    from calvis import main

    main(["go", "-n", "--rank"])
    out = capsys.readouterr().out
    assert "=== compiler plan ===" in out
    assert "=== ranked plan ===" in out
    assert "=== plan diff ===" in out
    assert catalog_ids()  # catalog still loaded
    assert "GATE: PASS" not in out
    assert "GATE: FAIL" not in out
