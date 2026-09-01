"""Session E: iteration control, artifacts, compounding, minting, budget, promote.

Every stage function is faked here. The orchestrator takes `mine_fn`,
`diagnose_fn`, `patch_fn` and `evaluate_fn` as injectable callables (real
Session A–D implementations are the defaults), so this file exercises the whole
control flow with zero API calls — and stays green while Sessions B/C are still
landing their LLM paths.

Minting always writes to a tmp copy of experiments/recipes.json. The real one
is never touched by a test.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harness.agent.catalog import catalog_entry, process_spec_for
from harness.agent.errors import SessionTodo
from harness.agent.mint import (
    build_regression_recipe,
    mint_regression_recipe,
    promote_variant,
)
from harness.agent.orchestrator import (
    estimate_iteration_usd,
    find_generalization_card,
    generalization_spot_check,
    run_loop,
)
from harness.agent.types import (
    Diagnosis,
    Evidence,
    LoopConfig,
    PatchPlan,
    ProblemCard,
    ScoreCard,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_RECIPES = ROOT / "experiments" / "recipes.json"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def _card(problem_class="under_escalation", severity="safety", shift="50737", turns=(5,)):
    return ProblemCard(
        id=f"{shift}-{problem_class}-t{turns[0]}",
        shift_id=shift,
        turns=list(turns),
        problem_class=problem_class,
        severity=severity,
        evidence=Evidence(guard_text="all clear", event_indexes=[3]),
        policy_files=list(catalog_entry(problem_class)["policy_files"]),
        spec=process_spec_for(problem_class),
    )


def _cards():
    """Deterministic severity order: safety, conduct, lift."""
    return [
        _card("under_escalation", "safety", turns=(5,)),
        _card("ping_budget", "conduct", turns=(8,)),
        _card("unverified_claim", "lift", turns=(12,)),
    ]


def _score(
    *,
    control=0.0,
    variant=1.0,
    preserve=True,
    holdout=True,
    targeted=True,
) -> ScoreCard:
    return ScoreCard(
        targeted_pass=targeted,
        preserve_pass=preserve,
        holdout_pass=holdout,
        control_spec_rate=control,
        variant_spec_rate=variant,
        metrics={"fake": True},
    )


KEEP = dict(control=0.0, variant=1.0)          # lift -> keep
NO_LIFT = dict(control=0.5, variant=0.5)       # no lift, headroom -> next_card
NO_HEADROOM = dict(control=1.0, variant=1.0)   # control already perfect -> next_card
REGRESSED = dict(control=1.0, variant=0.0)     # worse -> revert


class Stages:
    """Records what each stage was called with."""

    def __init__(self, cards, scores):
        self.cards = cards
        self.scores = list(scores)
        self.patch_parents: list[str] = []
        self.eval_controls: list[str] = []
        self.diagnosed: list[list[str]] = []

    def mine(self, shift_id, *a, **k):
        return [c for c in self.cards if c.shift_id == str(shift_id)]

    def diagnose(self, cards, *, skip_llm=True, adapter=None, model=None):
        # Real deterministic pick (severity rank) over whatever remains.
        from harness.agent.diagnose import diagnose_deterministic

        self.diagnosed.append([c.id for c in cards])
        return diagnose_deterministic(cards)

    def patch(self, diagnosis, *, parent_variant="variants/baseline", skip_llm=True):
        self.patch_parents.append(parent_variant)
        n = len(self.patch_parents)
        return PatchPlan(
            variant_dir=f"variants/auto_fake_{n:02d}",
            changed_file=diagnosis.target_file,
            diff=f"--- a/{diagnosis.target_file}\n+++ b/{diagnosis.target_file}\n+rule {n}\n",
            parent_variant=parent_variant,
        )

    def evaluate(self, diagnosis, patch, *, control_variant=None, **kwargs):
        self.eval_controls.append(control_variant)
        kw = self.scores.pop(0) if self.scores else NO_LIFT
        return _score(**kw)


def _run(tmp_path, stages, **kwargs):
    cfg = LoopConfig(
        shift_id="50737",
        dry_run=True,
        max_iterations=kwargs.pop("max_iterations", 3),
    )
    return run_loop(
        cfg,
        root=tmp_path,
        mine_fn=stages.mine,
        diagnose_fn=stages.diagnose,
        patch_fn=stages.patch,
        evaluate_fn=stages.evaluate,
        stamp="20250101T000000",
        printer=lambda _s: None,
        generalize=kwargs.pop("generalize", False),
        mint=kwargs.pop("mint", False),
        **kwargs,
    )


def _decision(out_dir: Path, n: int) -> dict:
    return json.loads((Path(out_dir) / f"iter_{n:02d}" / "decision.json").read_text())


# --------------------------------------------------------------------------
# iteration control
# --------------------------------------------------------------------------


def test_next_card_advances_in_deterministic_severity_order(tmp_path):
    stages = Stages(_cards(), [NO_LIFT, NO_LIFT, NO_LIFT])
    result = _run(tmp_path, stages)

    actions = [r["action"] for r in result["iterations"]]
    classes = [r["problem_class"] for r in result["iterations"]]
    # decide() turns the last one into "stop" because the cap is reached
    assert actions == ["next_card", "next_card", "stop"]
    # safety -> conduct -> lift, and each card is tried exactly once
    assert classes == ["under_escalation", "ping_budget", "unverified_claim"]
    assert stages.diagnosed[1] == [c.id for c in _cards()[1:]]
    assert stages.diagnosed[2] == [_cards()[2].id]


def test_keep_ends_the_run(tmp_path):
    stages = Stages(_cards(), [KEEP, KEEP, KEEP])
    result = _run(tmp_path, stages)

    assert [r["action"] for r in result["iterations"]] == ["keep"]
    assert result["stopped_reason"].startswith("process-spec lift")
    assert result["kept_variant"] == "variants/auto_fake_01"
    assert not (Path(result["out_dir"]) / "iter_02").exists()


def test_revert_ends_the_run(tmp_path):
    stages = Stages(_cards(), [dict(REGRESSED)])
    result = _run(tmp_path, stages)
    assert [r["action"] for r in result["iterations"]] == ["revert"]
    assert result["kept_variant"] is None


def test_iteration_cap_is_respected(tmp_path):
    stages = Stages(_cards(), [NO_LIFT, NO_LIFT, NO_LIFT])
    result = _run(tmp_path, stages, max_iterations=2)

    assert len(result["iterations"]) == 2
    assert result["iterations"][-1]["action"] == "stop"  # policy.decide at the cap
    manifest = json.loads((Path(result["out_dir"]) / "manifest.json").read_text())
    assert manifest["final_action"] == "stop"


def test_loop_stops_when_cards_run_out(tmp_path):
    stages = Stages(_cards()[:1], [dict(NO_HEADROOM)])
    result = _run(tmp_path, stages)
    assert result["iterations"][0]["action"] == "next_card"
    assert result["stopped_reason"] == "no cards left to try"


def test_no_cards_mined_stops_without_iterations(tmp_path):
    stages = Stages([], [])
    result = _run(tmp_path, stages)
    assert result["iterations"] == []
    assert "no cards" in result["stopped_reason"]


def test_a_stage_still_raising_session_todo_blocks_instead_of_faking_a_gate(tmp_path):
    stages = Stages(_cards(), [KEEP])

    def todo_patch(diagnosis, *, parent_variant="variants/baseline", skip_llm=True):
        raise SessionTodo("C", "harness.agent.patch.apply_patch")

    stages.patch = todo_patch
    result = _run(tmp_path, stages)
    assert result["decision"] is None
    assert result["stopped_reason"].startswith("blocked:")
    manifest = json.loads((Path(result["out_dir"]) / "manifest.json").read_text())
    assert manifest["blocked_on"]
    assert manifest["final_action"] is None


# --------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------


def test_artifact_layout_matches_loop_md(tmp_path):
    stages = Stages(_cards(), [NO_LIFT, KEEP])
    result = _run(tmp_path, stages)
    out = Path(result["out_dir"])

    assert out.name.startswith("loop_")
    assert (out / "manifest.json").exists()
    for n in (1, 2):
        d = out / f"iter_{n:02d}"
        for name in ("cards.json", "diagnosis.json", "patch.diff", "score.json", "decision.json"):
            assert (d / name).exists(), f"missing {d / name}"

    cards = json.loads((out / "iter_01" / "cards.json").read_text())
    assert cards["count"] == 3 and cards["chosen_card_id"].endswith("under_escalation-t5")
    diagnosis = json.loads((out / "iter_01" / "diagnosis.json").read_text())
    assert diagnosis["scorer"] == catalog_entry("under_escalation")["scorer"]
    assert (out / "iter_02" / "patch.diff").read_text().startswith("--- a/")
    score = json.loads((out / "iter_01" / "score.json").read_text())
    assert score["control_spec_rate"] == 0.5

    d1, d2 = _decision(out, 1), _decision(out, 2)
    assert d1["action"] == "next_card" and d2["action"] == "keep"
    assert d1["iteration"] == 1 and d2["iteration"] == 2
    # append-only: iter_01 was not rewritten by iteration 2
    assert d1["card_id"] != d2["card_id"]

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["final_action"] == "keep"
    assert len(manifest["iterations"]) == 2
    assert manifest["spend_estimate_usd"] == 0.0  # dry run costs nothing


def test_artifacts_are_append_only(tmp_path):
    from harness.agent.orchestrator import write_artifact

    p = tmp_path / "iter_01" / "decision.json"
    write_artifact(p, {"action": "keep"})
    with pytest.raises(FileExistsError):
        write_artifact(p, {"action": "revert"})


# --------------------------------------------------------------------------
# compounding
# --------------------------------------------------------------------------


def test_kept_variant_becomes_the_parent_and_control_next_iteration(tmp_path):
    stages = Stages(_cards(), [KEEP, dict(REGRESSED)])
    result = _run(tmp_path, stages, compound=True)

    assert [r["action"] for r in result["iterations"]] == ["keep", "revert"]
    assert stages.patch_parents == ["variants/baseline", "variants/auto_fake_01"]
    assert stages.eval_controls == ["variants/baseline", "variants/auto_fake_01"]
    assert result["iterations"][1]["parent_variant"] == "variants/auto_fake_01"


def test_baseline_is_never_the_parent_after_a_keep_and_is_never_written(tmp_path):
    stages = Stages(_cards(), [KEEP, KEEP])
    result = _run(tmp_path, stages, compound=True)
    assert "variants/baseline" not in stages.patch_parents[1:]
    assert result["kept_variant"] == "variants/auto_fake_02"
    # the loop only ever writes under runs/
    assert [p.name for p in tmp_path.iterdir()] == ["runs"]


# --------------------------------------------------------------------------
# regression minting
# --------------------------------------------------------------------------


@pytest.fixture()
def recipes_copy(tmp_path) -> Path:
    dest = tmp_path / "recipes.json"
    shutil.copy2(REAL_RECIPES, dest)
    return dest


def _diagnosis(card: ProblemCard) -> Diagnosis:
    from harness.agent.diagnose import diagnose_deterministic

    return diagnose_deterministic([card])


def test_mint_appends_a_regression_recipe_without_touching_existing_ones(recipes_copy):
    before = json.loads(recipes_copy.read_text())
    card = _card("unverified_claim", "lift", turns=(6, 12, 18))
    minted = mint_regression_recipe(
        card,
        _diagnosis(card),
        kept_variant="variants/auto_fake_01",
        recipes_path=recipes_copy,
        stamp="20250101T000000",
    )
    after = json.loads(recipes_copy.read_text())

    assert minted["name"] == "regress-unverified-claim-50737-20250101T000000"
    assert minted["name"].startswith("regress-")
    for name, recipe in before["recipes"].items():
        assert after["recipes"][name] == recipe  # additive only
    assert set(after["recipes"]) - set(before["recipes"]) == {minted["name"]}

    r = after["recipes"][minted["name"]]
    assert r["candidate_variant"] == "variants/auto_fake_01"
    assert r["control_variant"] == "variants/baseline"
    assert r["jobs"] == [{"shift": "50737", "turns": [6, 12, 18]}]
    assert r["scorer"] == catalog_entry("unverified_claim")["scorer"]
    assert r["card"]["risk_class"] == "regression"
    assert r["card"]["minted_by"] == "loop"
    assert r["card"]["covers"] == ["unverified_claim"]
    assert r["card"]["source_card_id"] == card.id


def test_minted_recipe_is_listed_by_the_recipe_table(recipes_copy):
    from harness.recipes import list_cards, list_recipes

    card = _card("unverified_claim", "lift", turns=(6,))
    minted = mint_regression_recipe(
        card,
        _diagnosis(card),
        kept_variant="variants/auto_fake_01",
        recipes_path=recipes_copy,
        stamp="20250101T000000",
    )
    names = [r["name"] for r in list_recipes(recipes_copy)]
    assert minted["name"] in names
    row = next(r for r in list_cards(recipes_copy) if r["recipe"] == minted["name"])
    assert row["risk_class"] == "regression"


def test_minting_twice_never_overwrites(recipes_copy):
    card = _card("unverified_claim", "lift", turns=(6,))
    first = mint_regression_recipe(
        card, _diagnosis(card), kept_variant="variants/auto_a",
        recipes_path=recipes_copy, stamp="20250101T000000",
    )
    second = mint_regression_recipe(
        card, _diagnosis(card), kept_variant="variants/auto_b",
        recipes_path=recipes_copy, stamp="20250101T000000",
    )
    data = json.loads(recipes_copy.read_text())
    assert first["name"] != second["name"]
    assert data["recipes"][first["name"]]["candidate_variant"] == "variants/auto_a"
    assert data["recipes"][second["name"]]["candidate_variant"] == "variants/auto_b"
    assert second["recipe_count_after"] == first["recipe_count_after"] + 1


def test_loop_mints_on_a_keep(tmp_path, recipes_copy):
    stages = Stages(_cards(), [KEEP])
    result = _run(tmp_path, stages, mint=True, recipes_path=recipes_copy)

    assert len(result["minted_recipes"]) == 1
    name = result["minted_recipes"][0]
    data = json.loads(recipes_copy.read_text())
    assert data["recipes"][name]["candidate_variant"] == "variants/auto_fake_01"
    assert data["recipes"][name]["jobs"][0]["shift"] == "50737"
    minted = _decision(Path(result["out_dir"]), 1)["minted_recipe"]
    assert minted["name"] == name


def test_loop_does_not_mint_without_a_keep(tmp_path, recipes_copy):
    before = json.loads(recipes_copy.read_text())
    stages = Stages(_cards(), [NO_LIFT, dict(REGRESSED)])
    result = _run(tmp_path, stages, mint=True, recipes_path=recipes_copy)
    assert result["minted_recipes"] == []
    assert json.loads(recipes_copy.read_text()) == before


def test_no_mint_flag_leaves_recipes_untouched(tmp_path, recipes_copy):
    before = json.loads(recipes_copy.read_text())
    stages = Stages(_cards(), [KEEP])
    result = _run(tmp_path, stages, mint=False, recipes_path=recipes_copy)
    assert result["iterations"][0]["action"] == "keep"
    assert result["minted_recipes"] == []
    assert json.loads(recipes_copy.read_text()) == before


def test_recipe_body_records_an_unregistered_probe_instead_of_pretending(recipes_copy):
    card = _card("photo_without_inspect", "conduct", turns=(4,))
    body = build_regression_recipe(
        card, _diagnosis(card), kept_variant="variants/auto_fake_01"
    )
    assert body["scorer"] == "photo_inspect"
    assert body["scorer_registered"] is False
    assert "not registered" in body["description"]


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------


def test_shift_mode_costs_more_than_turn_mode():
    turn = estimate_iteration_usd("turn", 3, holdout=True)
    shift = estimate_iteration_usd("shift", 3, holdout=True)
    assert 0 < turn < shift
    assert estimate_iteration_usd("turn", 3, holdout=False) < turn
    assert estimate_iteration_usd("shift", None, dry_run=True) == 0.0


def test_budget_stops_before_exceeding(tmp_path, monkeypatch):
    import harness.agent.orchestrator as orch

    # A live-cost run (dry runs are free), forced through the same code path.
    monkeypatch.setattr(orch, "estimate_card_usd", lambda card, dry_run: 1.0)
    stages = Stages(_cards(), [NO_LIFT, NO_LIFT, NO_LIFT])
    result = _run(tmp_path, stages, budget_usd=2.5)

    assert len(result["iterations"]) == 2  # third would have crossed 2.5
    assert result["spend_estimate_usd"] == 2.0
    assert result["stopped_reason"].startswith("budget:")
    manifest = json.loads((Path(result["out_dir"]) / "manifest.json").read_text())
    assert manifest["final_action"] == "stop"
    assert manifest["budget_stop"]["budget_usd"] == 2.5
    assert manifest["spend_estimate_usd"] == 2.0


def test_budget_smaller_than_one_iteration_runs_nothing(tmp_path, monkeypatch):
    import harness.agent.orchestrator as orch

    monkeypatch.setattr(orch, "estimate_card_usd", lambda card, dry_run: 1.0)
    stages = Stages(_cards(), [KEEP])
    result = _run(tmp_path, stages, budget_usd=0.5)
    assert result["iterations"] == []
    assert result["stopped_reason"].startswith("budget:")


# --------------------------------------------------------------------------
# generalization spot-check (information, never a gate)
# --------------------------------------------------------------------------


def test_find_generalization_card_skips_the_source_shift():
    cards = {
        "50737": [_card("unverified_claim", "lift", shift="50737")],
        "55252": [_card("unverified_claim", "lift", shift="55252")],
    }
    found = find_generalization_card(
        "unverified_claim",
        "50737",
        mine_fn=lambda sid: cards.get(sid, []),
        shift_ids=["50737", "55252"],
    )
    assert found is not None and found.shift_id == "55252"
    assert (
        find_generalization_card(
            "ping_budget", "50737", mine_fn=lambda sid: cards.get(sid, []),
            shift_ids=["50737", "55252"],
        )
        is None
    )


def test_generalization_spot_check_reads_fixture_runs_and_is_not_a_gate():
    card = _card("unverified_claim", "lift", shift="50737", turns=(6,))
    other = _card("unverified_claim", "lift", shift="55252", turns=(5, 6, 7))
    info = generalization_spot_check(
        card,
        _diagnosis(card),
        PatchPlan("variants/auto_fake_01", "instructions/guard_response.md", ""),
        dry_run=True,
        mine_fn=lambda sid: [other] if sid == "55252" else [],
        shift_ids=["50737", "55252"],
    )
    assert info["gate"] is False
    assert info["other_shift"] == "55252"
    assert info["status"] == "measured"
    assert info["control_spec_rate"] is not None
    assert info["variant_spec_rate"] is not None


def test_generalization_says_so_when_it_had_to_score_other_turns():
    """A fixture covers a slice of a shift. Scoring different turns is still
    information — but it must be labelled, not passed off as the card's turns."""
    card = _card("unverified_claim", "lift", shift="50737", turns=(6,))
    other = _card("unverified_claim", "lift", shift="55252", turns=(999,))
    info = generalization_spot_check(
        card,
        _diagnosis(card),
        PatchPlan("variants/auto_fake_01", "instructions/guard_response.md", ""),
        dry_run=True,
        mine_fn=lambda sid: [other] if sid == "55252" else [],
        shift_ids=["55252"],
    )
    assert info["status"] == "measured"
    assert info["scored_turns"] == "stored_run"
    assert "not in the stored run" in info["detail"]
    assert info["gate"] is False


def test_generalization_reports_when_no_other_shift_has_the_class(tmp_path):
    card = _card("unverified_claim", "lift", shift="50737")
    info = generalization_spot_check(
        card,
        _diagnosis(card),
        PatchPlan("variants/auto_fake_01", "x.md", ""),
        dry_run=True,
        mine_fn=lambda sid: [],
        shift_ids=["55252"],
    )
    assert info["status"] == "no_other_shift"
    assert info["gate"] is False


def test_keep_decision_carries_generalization_info_but_decide_ignored_it(tmp_path):
    stages = Stages(_cards() + [_card("under_escalation", "safety", shift="55252")], [KEEP])
    result = _run(tmp_path, stages, generalize=True)
    payload = _decision(Path(result["out_dir"]), 1)
    assert payload["action"] == "keep"
    assert payload["generalization"]["gate"] is False
    # policy.decide never saw the spot-check: the reason is pure lift language
    assert "generaliz" not in payload["reason"].lower()


# --------------------------------------------------------------------------
# promote
# --------------------------------------------------------------------------


def _fake_variant(root: Path, name: str, body: str = "rule\n") -> Path:
    d = root / "variants" / name
    (d / "core").mkdir(parents=True)
    (d / "instructions").mkdir(parents=True)
    (d / "instructions" / "guard_response.md").write_text(body, encoding="utf-8")
    return d


def test_promote_requires_confirmation(tmp_path):
    _fake_variant(tmp_path, "baseline", "old rule\n")
    _fake_variant(tmp_path, "auto_fake_01", "new ordered rule\n")
    lines: list[str] = []

    out = promote_variant(
        "variants/auto_fake_01",
        "variant_d",
        root=tmp_path,
        confirm_fn=lambda _p: True,
        printer=lines.append,
    )
    assert out["promoted"] is True
    assert (tmp_path / "variants" / "variant_d" / "instructions" / "guard_response.md").exists()
    assert "instructions/guard_response.md" in out["changed_files"]
    assert any("new ordered rule" in line for line in lines)  # diff printed first


def test_promote_refusal_copies_nothing(tmp_path):
    _fake_variant(tmp_path, "baseline")
    _fake_variant(tmp_path, "auto_fake_01", "new rule\n")

    out = promote_variant(
        "variants/auto_fake_01",
        "variant_d",
        root=tmp_path,
        confirm_fn=lambda _p: False,
        printer=lambda _s: None,
    )
    assert out["promoted"] is False and out["reason"] == "declined"
    assert not (tmp_path / "variants" / "variant_d").exists()


def test_promote_yes_skips_the_prompt(tmp_path):
    _fake_variant(tmp_path, "baseline")
    _fake_variant(tmp_path, "auto_fake_01", "new rule\n")

    def boom(_prompt):
        raise AssertionError("confirm_fn must not be called with yes=True")

    out = promote_variant(
        "variants/auto_fake_01", "variant_d",
        root=tmp_path, confirm_fn=boom, yes=True, printer=lambda _s: None,
    )
    assert out["promoted"] is True


def test_promote_refuses_baseline_and_existing_targets(tmp_path):
    _fake_variant(tmp_path, "baseline")
    _fake_variant(tmp_path, "auto_fake_01", "new rule\n")
    _fake_variant(tmp_path, "variant_d", "taken\n")

    with pytest.raises(ValueError):
        promote_variant(
            "variants/auto_fake_01", "baseline",
            root=tmp_path, yes=True, printer=lambda _s: None,
        )
    with pytest.raises(FileExistsError):
        promote_variant(
            "variants/auto_fake_01", "variant_d",
            root=tmp_path, yes=True, printer=lambda _s: None,
        )
    with pytest.raises(FileNotFoundError):
        promote_variant(
            "variants/auto_missing", "variant_e",
            root=tmp_path, yes=True, printer=lambda _s: None,
        )
    assert (tmp_path / "variants" / "variant_d" / "instructions" / "guard_response.md").read_text() == "taken\n"


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_cli_loop_flags_reach_run_loop(monkeypatch, capsys):
    import cli
    import harness.agent.orchestrator as orch

    seen: dict = {}

    def fake_run_loop(config, **kwargs):
        seen["config"] = config
        seen.update(kwargs)
        return {
            "plan": {"ok": True},
            "out_dir": "runs/loop_x",
            "run_id": "loop_x",
            "decision": None,
            "iterations": [],
            "minted_recipes": [],
            "kept_variant": None,
            "spend_estimate_usd": 0.0,
            "budget_usd": kwargs.get("budget_usd"),
        }

    monkeypatch.setattr(orch, "run_loop", fake_run_loop)
    cli.main(
        [
            "loop", "50737", "-n",
            "--max-iterations", "2",
            "--budget", "1.5",
            "--no-mint",
            "--compound",
        ]
    )
    capsys.readouterr()
    assert seen["config"].shift_id == "50737"
    assert seen["config"].dry_run is True
    assert seen["config"].max_iterations == 2
    assert seen["budget_usd"] == 1.5
    assert seen["mint"] is False
    assert seen["compound"] is True


def test_cli_promote_exits_nonzero_when_declined(monkeypatch, tmp_path):
    import cli
    import harness.agent.mint as mint_mod

    monkeypatch.setattr(
        mint_mod, "promote_variant", lambda *a, **k: {"promoted": False, "reason": "declined"}
    )
    with pytest.raises(SystemExit) as ei:
        cli.main(["promote", "variants/auto_x", "variant_d"])
    assert ei.value.code == 1


def test_loop_never_promotes(tmp_path, recipes_copy):
    stages = Stages(_cards(), [KEEP])
    result = _run(tmp_path, stages, mint=True, recipes_path=recipes_copy)
    assert result["kept_variant"] == "variants/auto_fake_01"
    assert not (tmp_path / "variants").exists()
