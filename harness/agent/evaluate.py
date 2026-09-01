"""Session D: run same-model control vs patched variant; scorers own the booleans.

Reuse cli.run_jobs + harness.recipes. Historical baseline is not the control arm.

The two arms are the *same model* on the *same frozen turns*:

    control  = patch.parent_variant (the prompt the patch was derived from)
    variant  = patch.variant_dir    (variants/auto_<stamp>/)

Improvement is `spec_rate(variant_turns) > spec_rate(control_turns)` on the
card's turns (`harness.agent.spec`), handed to `policy.assess_lift`. The shift
JSON is static: later historical guard messages are evidence, never outcomes,
so nothing here reads guard text after a DM. No holistic quality score is
computed — every boolean on the ScoreCard comes from a deterministic scorer or
from ProcessSpec clause checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harness.agent.catalog import catalog_entry
from harness.agent.policy import assess_lift
from harness.agent.spec import clause_results, spec_rate
from harness.agent.types import Diagnosis, PatchPlan, ProblemCard, ProcessSpec, ScoreCard
from harness.recipes import SCORERS, execute_recipe, load_recipes, resolve_recipe_name
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[2]

# Stored runs used by dry_run=True. Two arms, two shifts (targeted + holdout),
# in ExperimentStore layout so the real scorers run over them with no API calls.
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "agent_eval"
FIXTURE_CONTROL_RUN = "ctrl_loop_dry"
FIXTURE_VARIANT_RUN = "var_loop_dry"


# --------------------------------------------------------------------------
# turn selection
# --------------------------------------------------------------------------


def resolve_turns(
    diagnosis: Diagnosis,
    card: ProblemCard | None = None,
    turns: list[int] | None = None,
    recipes_path: Path | None = None,
) -> tuple[list[int] | None, str]:
    """Which frozen turns to score. Returns (turns, source).

    `Diagnosis` does not carry the card's turns, so the caller may pass the
    ProblemCard (or an explicit turn list). Falling back, we use the catalog
    recipe's job turns for this shift; failing that, the whole shift.
    """
    if turns:
        return [int(t) for t in turns], "argument"
    if card is not None and card.turns:
        return [int(t) for t in card.turns], "card"
    if diagnosis.recipe:
        try:
            data = load_recipes(recipes_path)
            name = resolve_recipe_name(diagnosis.recipe, recipes_path)
            for job in (data.get("recipes") or {}).get(name, {}).get("jobs") or []:
                if str(job.get("shift")) == str(diagnosis.shift_id) and job.get("turns"):
                    return [int(t) for t in job["turns"]], f"recipe:{name}"
        except (KeyError, OSError, ValueError):
            pass
    return None, "whole_shift"


def select_turns(rows: list[dict[str, Any]], want: list[int] | None) -> list[dict[str, Any]]:
    """Frozen turns from a stored run, ordered, optionally filtered."""
    rows = sorted(rows, key=lambda t: int(t.get("turn", 0)))
    if not want:
        return rows
    keep = {int(t) for t in want}
    return [t for t in rows if int(t.get("turn", -1)) in keep]


# --------------------------------------------------------------------------
# deterministic gate helpers
# --------------------------------------------------------------------------


def clause_gates(rows: list[dict[str, Any]], spec: ProcessSpec) -> dict[str, bool]:
    """Per-clause booleans, ANDed across the scored turns. No LLM, no judgement."""
    gates: dict[str, bool] = {}
    for turn in rows:
        for clause, ok in clause_results(turn, spec).items():
            gates[clause] = gates.get(clause, True) and bool(ok)
    return gates


def preserve_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Welcome + guard-reply counts on one arm (must_preserve evidence)."""
    welcome_turns = [t for t in rows if t.get("trigger") == "session_start"]
    guard_turns = [t for t in rows if t.get("trigger") == "guard_message"]
    welcomes = sum(1 for t in welcome_turns if t.get("messages"))
    replies = sum(
        1
        for t in guard_turns
        if t.get("messages") or t.get("decision") == "send_message"
    )
    return {
        "welcome_turns": len(welcome_turns),
        "welcomes_sent": welcomes,
        "guard_message_turns": len(guard_turns),
        "guard_replies": replies,
        "reply_rate": (replies / len(guard_turns)) if guard_turns else None,
    }


def preserve_verdict(
    control_rows: list[dict[str, Any]], variant_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """must_preserve: the variant never drops a welcome or a guard reply.

    Measured against the same-model control on the same turns — not against the
    historical baseline. With no welcome and no guard_message turn in scope
    there is nothing to preserve, so the gate is vacuously true and says so.
    """
    c = preserve_stats(control_rows)
    v = preserve_stats(variant_rows)
    observed = bool(c["welcome_turns"] or c["guard_message_turns"])
    welcomes_ok = v["welcomes_sent"] >= c["welcomes_sent"]
    c_rate = c["reply_rate"]
    v_rate = v["reply_rate"]
    replies_ok = c_rate is None or (v_rate is not None and v_rate >= c_rate - 1e-9)
    return {
        "control": c,
        "variant": v,
        "observed": observed,
        "welcomes_preserved": welcomes_ok,
        "replies_preserved": replies_ok,
        "pass": bool(welcomes_ok and replies_ok),
    }


def run_targeted_scorer(
    scorer_name: str | None,
    store: ExperimentStore,
    control_id: str,
    variant_id: str,
    jobs: list[dict[str, Any]],
    scorer_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the catalog scorer over the two stored runs.

    Scorers own PASS/FAIL. A class whose probe is not registered yet (the
    catalog names some Session-D probes that do not exist in SCORERS) reports
    `pass: None` — it never silently becomes a pass, and never gets replaced by
    an invented score.
    """
    if not scorer_name:
        return {"scorer": None, "pass": None, "detail": "no scorer on the diagnosis"}
    scorer = SCORERS.get(scorer_name)
    if scorer is None:
        return {
            "scorer": scorer_name,
            "pass": None,
            "detail": f"scorer {scorer_name!r} is not registered in harness.recipes.SCORERS",
        }
    recipe = {"jobs": jobs, "scorer": scorer_name, "scorer_args": scorer_args or {}}
    try:
        result = scorer(store, control_id, variant_id, recipe)
    except Exception as exc:  # a probe that cannot read these runs is not a pass
        return {
            "scorer": scorer_name,
            "pass": None,
            "detail": f"scorer raised {type(exc).__name__}: {exc}",
        }
    result = dict(result)
    result["pass"] = bool(result.get("pass")) if result.get("pass") is not None else None
    return result


# --------------------------------------------------------------------------
# score card assembly
# --------------------------------------------------------------------------


def build_scorecard(
    diagnosis: Diagnosis,
    *,
    control_rows: list[dict[str, Any]],
    variant_rows: list[dict[str, Any]],
    scorer_result: dict[str, Any],
    holdout: dict[str, Any] | None,
    control_run_id: str,
    variant_run_id: str,
    preserve_extra: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    extra_metrics: dict[str, Any] | None = None,
) -> ScoreCard:
    """Assemble the ScoreCard. Every boolean traces to a deterministic check."""
    spec = diagnosis.spec
    control_rate = spec_rate(control_rows, spec)
    variant_rate = spec_rate(variant_rows, spec)

    lift: dict[str, Any] | None = None
    if control_rate is not None and variant_rate is not None:
        lift = dict(assess_lift(control_rate, variant_rate))
        targeted_pass = bool(lift["targeted_pass"])
    else:
        targeted_pass = bool(scorer_result.get("pass"))

    c_preserve = list(control_rows)
    v_preserve = list(variant_rows)
    if preserve_extra:
        c_preserve = c_preserve + list(preserve_extra[0])
        v_preserve = v_preserve + list(preserve_extra[1])
    preserve = preserve_verdict(c_preserve, v_preserve)

    holdout_pass: bool | None = None
    if holdout is not None and holdout.get("pass") is not None:
        holdout_pass = bool(holdout["pass"])

    gates = {
        "control": clause_gates(control_rows, spec),
        "variant": clause_gates(variant_rows, spec),
        "targeted_scorer_pass": scorer_result.get("pass"),
        "welcomes_preserved": preserve["welcomes_preserved"],
        "replies_preserved": preserve["replies_preserved"],
        "holdout_pass": holdout_pass,
    }

    metrics: dict[str, Any] = {
        "shift_id": diagnosis.shift_id,
        "problem_class": diagnosis.problem_class,
        "mode": diagnosis.mode,
        "control_turns_scored": len(control_rows),
        "variant_turns_scored": len(variant_rows),
        "lift": lift,
        "gates": gates,
        "preserve": preserve,
        "targeted_scorer": scorer_result,
        "holdout": holdout,
        "spec": spec.to_dict(),
        "not_outcomes": list(spec.not_outcomes),
    }
    metrics.update(extra_metrics or {})

    return ScoreCard(
        targeted_pass=targeted_pass,
        preserve_pass=bool(preserve["pass"]),
        holdout_pass=holdout_pass,
        control_spec_rate=control_rate,
        variant_spec_rate=variant_rate,
        metrics=metrics,
        control_run_id=control_run_id,
        variant_run_id=variant_run_id,
        scorer=diagnosis.scorer,
    )


# --------------------------------------------------------------------------
# dry run: stored fixtures, zero API calls
# --------------------------------------------------------------------------


def _fixture_shift(store: ExperimentStore, run_id: str, shift_id: str) -> str | None:
    if store.load_turns(run_id, shift_id):
        return shift_id
    return None


def evaluate_from_fixture(
    diagnosis: Diagnosis,
    patch: PatchPlan,
    *,
    card: ProblemCard | None = None,
    turns: list[int] | None = None,
    fixture_root: Path | None = None,
    control_run_id: str = FIXTURE_CONTROL_RUN,
    variant_run_id: str = FIXTURE_VARIANT_RUN,
    recipes_path: Path | None = None,
) -> ScoreCard:
    """dry_run path: score stored runs. No adapter, no model, no network."""
    root = Path(fixture_root or FIXTURE_ROOT)
    store = ExperimentStore(root)
    notes: list[str] = ["dry_run: stored fixture runs, no API calls"]

    shift = _fixture_shift(store, control_run_id, str(diagnosis.shift_id))
    if shift is None:
        shift = next(
            (
                s
                for s in sorted(
                    p.stem
                    for p in (root / control_run_id / "results").glob("*.jsonl")
                )
                if store.load_turns(variant_run_id, s)
            ),
            str(diagnosis.shift_id),
        )
        notes.append(
            f"fixture has no run for shift {diagnosis.shift_id}; scored fixture shift {shift}"
        )

    want, turn_source = resolve_turns(diagnosis, card, turns, recipes_path)
    control_rows = select_turns(store.load_turns(control_run_id, shift), want)
    variant_rows = select_turns(store.load_turns(variant_run_id, shift), want)

    jobs = [{"shift": shift, "turns": want}]
    meta = catalog_entry(diagnosis.problem_class)
    scorer_result = run_targeted_scorer(
        diagnosis.scorer,
        store,
        control_run_id,
        variant_run_id,
        jobs,
        meta.get("scorer_args"),
    )

    holdout: dict[str, Any] | None = None
    preserve_extra = None
    if diagnosis.holdout_recipe:
        holdout, preserve_extra = _fixture_holdout(
            diagnosis.holdout_recipe,
            store,
            control_run_id,
            variant_run_id,
            recipes_path,
        )
    else:
        notes.append("holdout_pass is None: this card IS the holdout shift")

    return build_scorecard(
        diagnosis,
        control_rows=control_rows,
        variant_rows=variant_rows,
        scorer_result=scorer_result,
        holdout=holdout,
        control_run_id=control_run_id,
        variant_run_id=variant_run_id,
        preserve_extra=preserve_extra,
        extra_metrics={
            "dry_run": True,
            "shift_id": shift,
            "turns": want,
            "turns_source": turn_source,
            "control_variant": patch.parent_variant,
            "variant_dir": patch.variant_dir,
            "notes": notes,
        },
    )


def _fixture_holdout(
    holdout_recipe: str,
    store: ExperimentStore,
    control_run_id: str,
    variant_run_id: str,
    recipes_path: Path | None,
) -> tuple[dict[str, Any] | None, tuple[list[dict], list[dict]] | None]:
    """Score the holdout recipe over the stored fixture runs (no execution)."""
    try:
        data = load_recipes(recipes_path)
        name = resolve_recipe_name(holdout_recipe, recipes_path)
        recipe = (data.get("recipes") or {})[name]
    except (KeyError, OSError, ValueError) as exc:
        return (
            {
                "recipe": holdout_recipe,
                "pass": None,
                "detail": f"holdout recipe unavailable: {exc}",
            },
            None,
        )
    jobs = recipe.get("jobs") or []
    shifts = [str(j.get("shift")) for j in jobs]
    if not all(store.load_turns(control_run_id, s) for s in shifts):
        return (
            {
                "recipe": name,
                "pass": None,
                "detail": "fixture has no stored holdout run",
            },
            None,
        )
    result = run_targeted_scorer(
        recipe.get("scorer"),
        store,
        control_run_id,
        variant_run_id,
        jobs,
        recipe.get("scorer_args"),
    )
    result["recipe"] = name
    result["control_run_id"] = control_run_id
    result["variant_run_id"] = variant_run_id
    c_rows: list[dict] = []
    v_rows: list[dict] = []
    for s in shifts:
        c_rows += store.load_turns(control_run_id, s)
        v_rows += store.load_turns(variant_run_id, s)
    return result, (c_rows, v_rows)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def evaluate_diagnosis(
    diagnosis: Diagnosis,
    patch: PatchPlan,
    *,
    control_variant: str = "variants/baseline",
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    dry_run: bool = False,
    card: ProblemCard | None = None,
    turns: list[int] | None = None,
    run_jobs_fn: Callable[..., str] | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
    fixture_root: Path | None = None,
) -> ScoreCard:
    """Control (parent prompt) vs patched variant, same model, same frozen turns.

    Returns a ScoreCard whose booleans come from deterministic scorers and
    ProcessSpec clause checks only. `dry_run=True` scores stored fixture runs
    and makes zero API calls.
    """
    if (diagnosis.mode or "") == "scenario":
        # Scripted-scenario cards are evaluated by replaying the scenario, not
        # by scoring frozen historical turns. See harness/agent/scenario_eval.py.
        from harness.agent.scenario_eval import evaluate_scenario_diagnosis

        return evaluate_scenario_diagnosis(
            diagnosis,
            patch,
            control_variant=control_variant,
            adapter=adapter,
            model=model,
            dry_run=dry_run,
            card=card,
            root=root,
            recipes_path=recipes_path,
            run_jobs_fn=run_jobs_fn,
        )

    if dry_run:
        return evaluate_from_fixture(
            diagnosis,
            patch,
            card=card,
            turns=turns,
            fixture_root=fixture_root,
            recipes_path=recipes_path,
        )

    root = Path(root or ROOT)
    # Hard rule 4: the live control is the same model on the prompt the patch
    # was derived from. Historical production output is never an arm.
    control_dir = patch.parent_variant or control_variant
    variant_dir = patch.variant_dir
    if run_jobs_fn is None:
        from cli import run_jobs as run_jobs_fn  # lazy: keeps tests API-free

    want, turn_source = resolve_turns(diagnosis, card, turns, recipes_path)
    mode = diagnosis.mode or catalog_entry(diagnosis.problem_class)["mode"]
    shift = str(diagnosis.shift_id)
    jobs = [{"shift": shift, "turns": want}]

    from harness.recipes import _stamp  # same run-id convention as recipes

    stamp = _stamp()
    slug = f"{diagnosis.problem_class}_{shift}_{stamp}"
    control_run_id = f"ctrl_loop_{slug}"
    variant_run_id = f"var_loop_{slug}"

    print(f"=== loop eval {diagnosis.problem_class}: control ({control_dir}) -> {control_run_id} ===")
    run_jobs_fn(
        variant=control_dir,
        run_id=control_run_id,
        mode=mode,
        jobs=jobs,
        adapter=adapter,
        model=model,
    )
    print(f"=== loop eval {diagnosis.problem_class}: variant ({variant_dir}) -> {variant_run_id} ===")
    run_jobs_fn(
        variant=variant_dir,
        run_id=variant_run_id,
        mode=mode,
        jobs=jobs,
        adapter=adapter,
        model=model,
    )

    store = ExperimentStore(root / "runs")
    control_rows = select_turns(store.load_turns(control_run_id, shift), want)
    variant_rows = select_turns(store.load_turns(variant_run_id, shift), want)

    meta = catalog_entry(diagnosis.problem_class)
    scorer_result = run_targeted_scorer(
        diagnosis.scorer,
        store,
        control_run_id,
        variant_run_id,
        jobs,
        meta.get("scorer_args"),
    )

    holdout: dict[str, Any] | None = None
    preserve_extra = None
    notes: list[str] = []
    if diagnosis.holdout_recipe:
        # LOOP hard rule 6: the holdout runs even when the card is unrelated.
        holdout_result = execute_recipe(
            diagnosis.holdout_recipe,
            run_jobs_fn=run_jobs_fn,
            root=root,
            adapter=adapter,
            model=model,
            candidate_variant=variant_dir,
            control_variant=control_dir,
            recipes_path=recipes_path,
        )
        plan = holdout_result.get("plan") or {}
        holdout = {
            "recipe": plan.get("recipe", diagnosis.holdout_recipe),
            "pass": holdout_result.get("pass"),
            "scorer": plan.get("scorer"),
            "control_run_id": plan.get("control_run_id"),
            "variant_run_id": plan.get("variant_run_id"),
            "detail": (holdout_result.get("score") or {}).get("detail"),
        }
        # The holdout is a full shift: reuse it as must_preserve evidence
        # (welcome + guard replies) instead of paying for a third run.
        h_shifts = [str(j.get("shift")) for j in (plan.get("jobs") or [])]
        c_rows: list[dict] = []
        v_rows: list[dict] = []
        for s in h_shifts:
            c_rows += store.load_turns(plan.get("control_run_id") or "", s)
            v_rows += store.load_turns(plan.get("variant_run_id") or "", s)
        if c_rows or v_rows:
            preserve_extra = (c_rows, v_rows)
    else:
        notes.append("holdout_pass is None: this card IS the holdout shift")

    return build_scorecard(
        diagnosis,
        control_rows=control_rows,
        variant_rows=variant_rows,
        scorer_result=scorer_result,
        holdout=holdout,
        control_run_id=control_run_id,
        variant_run_id=variant_run_id,
        preserve_extra=preserve_extra,
        extra_metrics={
            "dry_run": False,
            "adapter": adapter,
            "model": model,
            "turns": want,
            "turns_source": turn_source,
            "control_variant": control_dir,
            "control_variant_arg": control_variant,
            "variant_dir": variant_dir,
            "notes": notes,
        },
    )
