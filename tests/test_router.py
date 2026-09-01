"""Deterministic `cx go` compiler."""

from __future__ import annotations

from pathlib import Path

from harness.router import (
    SMOKE,
    catalog_ids,
    compile_plan,
    plan_payload,
    router_defaults,
    run_go,
)

ROOT = Path(__file__).resolve().parents[1]


def test_router_model_differs_from_copilot_model():
    import json

    data = json.loads((ROOT / "experiments" / "recipes.json").read_text(encoding="utf-8"))
    copilot = (data.get("defaults") or {}).get("model")
    cfg = router_defaults(data)
    assert cfg["model"]
    assert cfg["model"] != copilot


def test_compile_plan_smoke_welcome_first_and_partitions_catalog():
    plan = compile_plan(
        control_dir=ROOT / "variants" / "baseline",
        variant_dir=ROOT / "variants" / "variant_b",
        budget=4,
    )
    ids = catalog_ids()
    payload = plan_payload(plan)
    assert payload["must_run"][0] == SMOKE
    assert payload["order"][0] == SMOKE
    assert "b-claims" in payload["must_run"]
    combined = payload["must_run"] + payload["should_run"] + payload["skip"]
    assert sorted(combined) == sorted(ids)
    assert len(combined) == len(set(combined))
    assert set(payload["order"]) <= set(payload["must_run"]) | set(payload["should_run"])
    assert payload["budget"] == 4


def test_compile_plan_file_match_a3():
    plan = compile_plan(
        control_dir=ROOT / "variants" / "baseline",
        variant_dir=ROOT / "variants" / "variant_a3",
        budget=4,
    )
    assert SMOKE in plan["must_run"]
    assert "a3-shift-55252" in plan["must_run"]
    assert "a3-quiet-probe" in plan["must_run"]
    assert "b-claims" in plan["skip"]


def test_compile_plan_intent_pulls_should_run_until_budget():
    plan = compile_plan(
        changed_files=["core/identity.md"],
        intent="also run claims verification",
        budget=2,
    )
    # identity.md is not a recipe trigger → only smoke-welcome is must_run
    assert plan["must_run"] == [SMOKE]
    assert plan["should_run"] == ["b-claims"]
    assert "b-claims" in plan["order"]
    assert plan["order"][0] == SMOKE


def test_compile_plan_budget_moves_overflow_should_run_to_skip():
    plan = compile_plan(
        changed_files=["core/identity.md"],
        intent="claims verification and voice compliance",
        budget=1,  # only room for must_run
    )
    assert plan["must_run"] == [SMOKE]
    assert plan["should_run"] == []
    assert "b-claims" in plan["skip"]
    assert "c-voice" in plan["skip"]
    assert plan["order"] == [SMOKE]


def test_run_go_without_rank_does_not_call_llm(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("ranker LLM must not run unless --rank")

    monkeypatch.setattr("harness.router._openai_rank", boom)
    monkeypatch.setattr("harness.router._anthropic_rank", boom)
    result = run_go(
        target="vb",
        dry_run=True,
        rank=False,
        print_fn=None,
    )
    assert result["rank"] is False
    assert result["ranked_plan"] is None
    assert "=== go plan (compiler) ===" in result["output"]
    assert "ranked plan" not in result["output"]
    assert "PASS" not in result["output"]
    assert "FAIL" not in result["output"]
    assert result["plan"]["order"][0] == SMOKE
