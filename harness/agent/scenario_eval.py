"""Session D, scenario mode: evaluate a patch against a scripted scenario.

`harness/agent/evaluate.py` keeps one small hook into here so the two paths
stay separable. Everything specific to a *deterministic scripted scenario*
(photo-gamer, partial, pushback, hostile) lives in this module.

What a scenario evaluation is:

* **Targeted** — `execute_recipe(<scenario recipe>)` twice, at the recipe's own
  `repetitions` (pass^k), with `control_variant` = the prompt the patch came
  from and `candidate_variant` = the patched auto-variant. The patch counts as
  targeted-pass only when the candidate PASSES the scenario **and** the control
  either failed outright or failed more gates than the candidate. A scenario
  that was already green under the parent prompt is no lift to claim.
* **Holdout** — the catalog safety holdout (`a3-shift-55252`) *plus* the other
  three scripted scenarios as conduct holdouts. Any holdout that FAILS sets
  `holdout_pass=False`, which `policy.decide` turns into a revert.
* **Preserve** — welcome / guard-reply preservation is measured on the holdout
  shift, never on the scenario arms: the correct fix for a scenario often
  *replaces* a DM with an escalation, and that must not read as a lost reply.

`dry_run=True` stays API-free: scenario recipes run against
`harness.scenario.CannedAdapter`, and the historical holdout prints its plan
without calling a model.

The simulated guard (`sim-*`, scorer `simulation_conduct`) never reaches this
module — LOOP.md hard rule 1 keeps it holdout-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harness.agent.catalog import (
    SAFETY_HOLDOUT,
    SCENARIO_RECIPES,
    catalog_entry,
)
from harness.agent.evaluate import build_scorecard
from harness.agent.types import Diagnosis, PatchPlan, ProblemCard, ScoreCard
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[2]

#: Conduct holdouts: every scripted scenario except the one being targeted.
CONDUCT_HOLDOUT_RECIPES: tuple[str, ...] = tuple(
    sorted(v["recipe"] for v in SCENARIO_RECIPES.values())
)


# ---------------------------------------------------------------------------
# gate arithmetic
# ---------------------------------------------------------------------------


def _failed_gates(row: dict[str, Any]) -> list[str]:
    failed = list(row.get("failed_gates") or row.get("failed") or [])
    if failed:
        return failed
    return [name for name, ok in (row.get("gates") or {}).items() if not ok]


def gate_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    """Failed-gate tally over every repetition of one scenario arm."""
    score = dict((result or {}).get("score") or {})
    reps = list(score.get("per_repetition") or [])
    per_rep = [_failed_gates(r) for r in reps]
    names = sorted({g for row in per_rep for g in row})
    return {
        "pass": (result or {}).get("pass"),
        "repetitions": len(reps),
        "failed_gate_count": sum(len(row) for row in per_rep),
        "failed_gates": names,
        "failed_gates_per_repetition": per_rep,
        "run_id": ((result or {}).get("plan") or {}).get("variant_run_id"),
        "detail": score.get("detail"),
    }


def targeted_verdict(
    control: dict[str, Any], variant: dict[str, Any], recipe: str
) -> dict[str, Any]:
    """Candidate must pass, and control must have been worse. No lift, no keep."""
    c = gate_summary(control)
    v = gate_summary(variant)
    variant_pass = bool(v["pass"])
    control_pass = bool(c["pass"])
    strictly_better = v["failed_gate_count"] < c["failed_gate_count"]
    passed = variant_pass and ((not control_pass) or strictly_better)
    if not variant_pass:
        detail = (
            f"candidate still fails {recipe} "
            f"(gates {v['failed_gates'] or ['?']}, pass^{v['repetitions']})"
        )
    elif control_pass and not strictly_better:
        detail = (
            f"candidate passes {recipe}, but so did the control prompt "
            "— no lift to claim"
        )
    else:
        detail = (
            f"candidate passes {recipe} at pass^{v['repetitions']}; control failed "
            f"{c['failed_gate_count']} gate(s) {c['failed_gates']}"
        )
    return {
        "scorer": (control or variant or {}).get("plan", {}).get("scorer"),
        "recipe": recipe,
        "pass": passed,
        "variant_pass": variant_pass,
        "control_pass": control_pass,
        "strictly_better": strictly_better,
        "control": c,
        "variant": v,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# holdouts
# ---------------------------------------------------------------------------


def holdout_recipes_for(diagnosis: Diagnosis) -> list[str]:
    """Safety holdout first, then the other scripted scenarios."""
    target = diagnosis.recipe or catalog_entry(diagnosis.problem_class)["recipe"]
    names = [diagnosis.holdout_recipe or SAFETY_HOLDOUT]
    names += [r for r in CONDUCT_HOLDOUT_RECIPES if r != target]
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def combine_holdouts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One holdout verdict: any FAIL fails, all-unknown stays unknown."""
    verdicts = [r.get("pass") for r in rows]
    if any(v is False for v in verdicts):
        overall: bool | None = False
    elif any(v is True for v in verdicts):
        overall = True
    else:
        overall = None
    failed = [r["recipe"] for r in rows if r.get("pass") is False]
    return {
        "recipe": ", ".join(r["recipe"] for r in rows) or None,
        "pass": overall,
        "recipes": rows,
        "failed_recipes": failed,
        "detail": (
            f"holdout failed: {', '.join(failed)}"
            if failed
            else (
                "every holdout held (safety shift + other scripted scenarios)"
                if overall
                else "no holdout produced a verdict (dry run)"
            )
        ),
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def evaluate_scenario_diagnosis(
    diagnosis: Diagnosis,
    patch: PatchPlan,
    *,
    control_variant: str = "variants/baseline",
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    dry_run: bool = False,
    card: ProblemCard | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
    execute_recipe_fn: Callable[..., dict[str, Any]] | None = None,
    run_jobs_fn: Callable[..., str] | None = None,
) -> ScoreCard:
    """Control prompt vs patched variant on the scenario the card came from."""
    root = Path(root or ROOT)
    control_dir = patch.parent_variant or control_variant
    variant_dir = patch.variant_dir
    recipe = diagnosis.recipe or catalog_entry(diagnosis.problem_class)["recipe"]
    if not recipe:
        raise ValueError(f"no scenario recipe for class {diagnosis.problem_class}")

    if execute_recipe_fn is None:
        from harness.recipes import execute_recipe as execute_recipe_fn
    if run_jobs_fn is None and not dry_run:
        from cli import run_jobs as run_jobs_fn  # lazy: keeps tests API-free

    def _run(name: str, candidate: str) -> dict[str, Any]:
        return execute_recipe_fn(
            name,
            run_jobs_fn=run_jobs_fn,
            root=root,
            adapter=adapter,
            model=model,
            candidate_variant=candidate,
            control_variant=control_dir,
            dry_run=dry_run,
            recipes_path=recipes_path,
        )

    print(f"=== loop eval [scenario {recipe}]: control ({control_dir}) ===")
    control_result = _run(recipe, control_dir)
    print(f"=== loop eval [scenario {recipe}]: candidate ({variant_dir}) ===")
    variant_result = _run(recipe, variant_dir)

    scorer_result = targeted_verdict(control_result, variant_result, recipe)
    scorer_result["scorer"] = diagnosis.scorer

    holdout_rows: list[dict[str, Any]] = []
    preserve_rows: tuple[list[dict], list[dict]] | None = None
    for name in holdout_recipes_for(diagnosis):
        print(f"=== loop eval [holdout {name}]: candidate ({variant_dir}) ===")
        row_result = _run(name, variant_dir)
        plan = dict(row_result.get("plan") or {})
        row = {
            "recipe": plan.get("recipe", name),
            "pass": row_result.get("pass"),
            "scorer": plan.get("scorer"),
            "mode": plan.get("mode"),
            "variant_run_id": plan.get("variant_run_id"),
            "detail": (row_result.get("score") or {}).get("detail"),
        }
        holdout_rows.append(row)
        # must_preserve evidence comes from the historical holdout shift, not
        # from the scenario arms: a correct scenario fix may replace a DM with
        # an escalation, which is not a dropped reply.
        if preserve_rows is None and plan.get("mode") == "shift":
            store = ExperimentStore(root / "runs")
            c_rows: list[dict] = []
            v_rows: list[dict] = []
            for job in plan.get("jobs") or []:
                sid = str(job.get("shift"))
                c_rows += store.load_turns(plan.get("control_run_id") or "", sid)
                v_rows += store.load_turns(plan.get("variant_run_id") or "", sid)
            if c_rows or v_rows:
                preserve_rows = (c_rows, v_rows)

    holdout = combine_holdouts(holdout_rows)
    variant_plan = dict(variant_result.get("plan") or {})
    control_plan = dict(control_result.get("plan") or {})

    return build_scorecard(
        diagnosis,
        # Spec rates stay None in scenario mode: the scenario gates own the
        # verdict, so `policy.decide` reads `targeted_pass` and nothing else.
        control_rows=[],
        variant_rows=[],
        scorer_result=scorer_result,
        holdout=holdout,
        control_run_id=str(control_plan.get("variant_run_id") or ""),
        variant_run_id=str(variant_plan.get("variant_run_id") or ""),
        preserve_extra=preserve_rows,
        extra_metrics={
            "dry_run": bool(dry_run),
            "adapter": "canned" if dry_run else adapter,
            "model": "canned" if dry_run else model,
            "scenario_recipe": recipe,
            "scenario_shift": variant_plan.get("jobs", [{}])[0].get("shift")
            if variant_plan.get("jobs")
            else None,
            "fixture": (card.evidence.fixture if card else None),
            "failed_gate": (card.evidence.failed_gate if card else None),
            "repetitions": variant_plan.get("repetitions"),
            "control_variant": control_dir,
            "variant_dir": variant_dir,
            "holdout_recipes": [r["recipe"] for r in holdout_rows],
            "notes": [
                "scenario mode: gates own pass/fail; spec rates are not used",
                "must_preserve measured on the holdout shift, not the scenario arms",
            ],
        },
    )
