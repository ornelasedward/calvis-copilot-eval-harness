"""Named eval recipes: one-command control vs variant runs + deterministic scorers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness.diagnose_escalation import compare_pair
from harness.store import ExperimentStore
from harness.verify_b import verification_stats

ROOT = Path(__file__).resolve().parents[1]
RECIPES_PATH = ROOT / "experiments" / "recipes.json"


def load_recipes(path: Path | None = None) -> dict:
    p = path or RECIPES_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_recipe_name(name: str, path: Path | None = None) -> str:
    """Resolve short alias (b, a3, smoke) to full recipe id."""
    data = load_recipes(path)
    aliases = data.get("aliases") or {}
    recipes = data.get("recipes") or {}
    if name in recipes:
        return name
    if name in aliases:
        return aliases[name]
    raise KeyError(
        f"unknown recipe/alias: {name}. "
        f"Aliases: {sorted(aliases)} Recipes: {sorted(recipes)}"
    )


def resolve_analyze_target(name: str, path: Path | None = None) -> str:
    """Map short variant key (a3, b) to variants/... path."""
    data = load_recipes(path)
    targets = data.get("analyze_targets") or {}
    if name in targets:
        return targets[name]
    # allow full path or variants/foo
    if name.startswith("variants/") or Path(name).exists():
        return name
    raise KeyError(
        f"unknown analyze target: {name}. Known: {sorted(targets)}"
    )


def list_recipes(path: Path | None = None) -> list[dict]:
    data = load_recipes(path)
    aliases = data.get("aliases") or {}
    alias_help = data.get("alias_help") or {}
    by_recipe: dict[str, list[str]] = {}
    for alias, full in aliases.items():
        by_recipe.setdefault(full, []).append(alias)
    rows = []
    for name, r in data.get("recipes", {}).items():
        alias_list = sorted(by_recipe.get(name, []))
        meaning = ", ".join(
            f"{a}={alias_help.get(a, a)}" for a in alias_list
        ) if alias_list else ""
        rows.append(
            {
                "name": name,
                "aliases": alias_list,
                "alias_meaning": meaning,
                "description": r.get("description", ""),
                "candidate": r.get("candidate_variant"),
                "mode": r.get("mode"),
                "scorer": r.get("scorer"),
                "suggested_when": r.get("suggested_when") or [],
            }
        )
    return rows


def list_analyze_targets(path: Path | None = None) -> list[dict]:
    data = load_recipes(path)
    targets = data.get("analyze_targets") or {}
    help_map = data.get("analyze_help") or {}
    return [
        {"code": code, "path": path_, "meaning": help_map.get(code, "")}
        for code, path_ in sorted(targets.items())
    ]


def suggest_recipes_for_changed_files(changed: list[str], path: Path | None = None) -> list[str]:
    data = load_recipes(path)
    names: list[str] = []
    basenames = {Path(c).name for c in changed}
    for name, r in data.get("recipes", {}).items():
        triggers = r.get("suggested_when") or []
        if "any prompt change" in triggers:
            names.append(name)
            continue
        for t in triggers:
            if t in basenames or any(t in c.replace("\\", "/") for c in changed):
                names.append(name)
                break
    seen: set[str] = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _score_welcome(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    shift = recipe["jobs"][0]["shift"]
    c = store.load_turns(control_id, shift)
    v = store.load_turns(variant_id, shift)
    c1 = next((t for t in c if int(t.get("turn", -1)) == 1), None)
    v1 = next((t for t in v if int(t.get("turn", -1)) == 1), None)

    def ok(t: dict | None) -> bool:
        return bool(t and t.get("messages") and t.get("decision") == "send_message")

    c_ok, v_ok = ok(c1), ok(v1)
    return {
        "scorer": "welcome",
        "shift": shift,
        "control_welcome": c_ok,
        "variant_welcome": v_ok,
        "pass": c_ok and v_ok,
        "detail": "Welcome DM required on turn 1 for both arms.",
    }


def _score_verify_b(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    shifts = sorted({j["shift"] for j in recipe["jobs"]})
    per_shift = {}
    for sid in shifts:
        cs = verification_stats(store.load_turns(control_id, sid))
        vs = verification_stats(store.load_turns(variant_id, sid))
        lift = None
        if cs["verification_rate"] is not None and vs["verification_rate"] is not None:
            lift = round(vs["verification_rate"] - cs["verification_rate"], 3)
        per_shift[sid] = {"control": cs, "variant": vs, "lift": lift}
    passed = True
    details = []
    for sid, row in per_shift.items():
        cs, vs = row["control"], row["variant"]
        if (vs.get("reply_rate") or 0) < 1.0:
            passed = False
            details.append(f"{sid}: reply_rate < 1")
        if (vs.get("escalations") or 0) > (cs.get("escalations") or 0):
            passed = False
            details.append(f"{sid}: escalations increased")
        if row["lift"] is not None and row["lift"] < 0:
            passed = False
            details.append(f"{sid}: verification rate dropped")
    return {
        "scorer": "verify_b",
        "per_shift": per_shift,
        "pass": passed,
        "detail": "; ".join(details) if details else "Verification held/improved; replies preserved.",
    }


def _score_escalation_focus(
    store: ExperimentStore, control_id: str, variant_id: str, recipe: dict
) -> dict:
    args = recipe.get("scorer_args") or {}
    focus = list(args.get("focus_turns") or [5, 6, 7, 8, 9])
    shift = recipe["jobs"][0]["shift"]
    rep = compare_pair(store, control_id, variant_id, shift, focus)
    return {
        "scorer": "escalation_focus",
        "shift": shift,
        "focus_turns": focus,
        "control_escalation_total": rep["control_escalation_total"],
        "variant_escalation_total": rep["variant_escalation_total"],
        "missed_escalations": rep["failed_safety_assertions_missed_escalation"],
        "pass": rep["safety_pass"],
        "detail": (
            "No missed high-consequence escalations on focus turns."
            if rep["safety_pass"]
            else f"Missed escalations on turns {rep['failed_safety_assertions_missed_escalation']}"
        ),
        "report": {
            "control_decisions": rep["control_decisions"],
            "variant_decisions": rep["variant_decisions"],
            "changed_turns": [
                {
                    "turn": f["turn"],
                    "control": f["control_decision"],
                    "variant": f["variant_decision"],
                    "missed": f["missed_escalation"],
                }
                for f in rep["changed_turns_5_to_9"]
            ],
        },
    }


def _score_quietness(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    shift = recipe["jobs"][0]["shift"]
    want = set(recipe["jobs"][0].get("turns") or [])
    c = [t for t in store.load_turns(control_id, shift) if not want or int(t.get("turn", -1)) in want]
    v = [t for t in store.load_turns(variant_id, shift) if not want or int(t.get("turn", -1)) in want]

    def dm_rate(turns: list[dict]) -> float | None:
        if not turns:
            return None
        return sum(1 for t in turns if t.get("messages")) / len(turns)

    cr, vr = dm_rate(c), dm_rate(v)
    no_regression = cr is not None and vr is not None and vr <= cr + 1e-9
    improved = cr is not None and vr is not None and vr < cr
    return {
        "scorer": "quietness",
        "shift": shift,
        "control_dm_rate": cr,
        "variant_dm_rate": vr,
        "no_regression": no_regression,
        "improved": improved,
        "pass": no_regression,
        "detail": (
            "Quietness improved over control."
            if improved
            else (
                "No quietness regression (not necessarily an improvement)."
                if no_regression
                else "Variant sent more DMs than control."
            )
        ),
        "decisions": {
            "control": [t.get("decision") for t in c],
            "variant": [t.get("decision") for t in v],
        },
    }


SCORERS: dict[str, Callable[..., dict]] = {
    "welcome": _score_welcome,
    "verify_b": _score_verify_b,
    "escalation_focus": _score_escalation_focus,
    "quietness": _score_quietness,
}


def execute_recipe(
    name: str,
    *,
    run_jobs_fn: Callable[..., str],
    root: Path | None = None,
    adapter: str | None = None,
    model: str | None = None,
    candidate_variant: str | None = None,
    control_variant: str | None = None,
    repeat: int = 1,
    dry_run: bool = False,
    recipes_path: Path | None = None,
) -> dict[str, Any]:
    """
    run_jobs_fn(variant=, run_id=, mode=, jobs=, adapter=, model=, repeat=) -> run_id
    Runs all jobs for one arm in a single sealed run.
    """
    root = root or ROOT
    data = load_recipes(recipes_path)
    defaults = data.get("defaults") or {}
    name = resolve_recipe_name(name, recipes_path)
    recipe = (data.get("recipes") or {}).get(name)
    if not recipe:
        raise KeyError(f"unknown recipe: {name}. Known: {list((data.get('recipes') or {}))}")

    adapter = adapter or defaults.get("adapter") or "openai"
    model = model or defaults.get("model") or "gpt-5.6-sol"
    control_variant = control_variant or recipe.get("control_variant") or defaults.get("control_variant")
    candidate_variant = candidate_variant or recipe.get("candidate_variant")
    mode = recipe.get("mode") or "turn"
    jobs = recipe.get("jobs") or []
    stamp = _stamp()
    control_id = f"ctrl_{name.replace('-', '_')}_{stamp}"
    variant_id = f"var_{name.replace('-', '_')}_{stamp}"

    plan = {
        "recipe": name,
        "description": recipe.get("description"),
        "adapter": adapter,
        "model": model,
        "mode": mode,
        "control_variant": control_variant,
        "candidate_variant": candidate_variant,
        "control_run_id": control_id,
        "variant_run_id": variant_id,
        "jobs": jobs,
        "scorer": recipe.get("scorer"),
        "dry_run": dry_run,
    }
    if dry_run:
        return {"plan": plan, "score": None, "pass": None}

    store = ExperimentStore(root / "runs")
    print(f"=== recipe {name}: control ({control_variant}) -> {control_id} ===")
    run_jobs_fn(
        variant=control_variant,
        run_id=control_id,
        mode=mode,
        jobs=jobs,
        adapter=adapter,
        model=model,
        repeat=repeat,
    )
    print(f"=== recipe {name}: candidate ({candidate_variant}) -> {variant_id} ===")
    run_jobs_fn(
        variant=candidate_variant,
        run_id=variant_id,
        mode=mode,
        jobs=jobs,
        adapter=adapter,
        model=model,
        repeat=repeat,
    )

    scorer_name = recipe.get("scorer") or "welcome"
    scorer = SCORERS.get(scorer_name)
    if not scorer:
        raise KeyError(f"unknown scorer: {scorer_name}")
    score = scorer(store, control_id, variant_id, recipe)
    result = {"plan": plan, "score": score, "pass": bool(score.get("pass"))}
    out_dir = root / "runs" / variant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recipe_score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\n=== score card ===")
    print(json.dumps(score, indent=2))
    print(f"GATE: {'PASS' if result['pass'] else 'FAIL'}")
    print(f"wrote {out_dir / 'recipe_score.json'}")
    return result
