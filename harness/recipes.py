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


import re as _re

# Voice-policy violations the original prompt fails to enforce. Each is a rule
# stated verbatim in core/comms_policy.md, so counting them is a faithful
# compliance measure, not an invented metric.
_EMDASH_CHARS = ("\u2014", "\u2013")  # em-dash, en-dash
_FILLER_PATTERNS = [
    _re.compile(r"\blet me know\b", _re.I),
    _re.compile(r"\bfeel free\b", _re.I),
    _re.compile(r"\bhope (?:this|that) helps\b", _re.I),
]


def _voice_violations(turns: list[dict]) -> dict:
    """Count comms_policy voice violations across a run's delivered DMs."""
    emdash = 0
    filler = 0
    multi_dm_turns = 0
    dm_turns = 0
    guard_turns = 0
    guard_replied = 0
    welcome_turns = 0
    welcomed = 0
    for t in turns:
        msgs = t.get("messages") or []
        if t.get("trigger") == "guard_message":
            guard_turns += 1
            if msgs or t.get("decision") == "send_message":
                guard_replied += 1
        if t.get("trigger") == "session_start":
            welcome_turns += 1
            if msgs:
                welcomed += 1
        if msgs:
            dm_turns += 1
            if len(msgs) > 1:
                multi_dm_turns += 1
        for m in msgs:
            body = m.get("body") or m.get("message") or m.get("text") or ""
            emdash += sum(body.count(c) for c in _EMDASH_CHARS)
            for pat in _FILLER_PATTERNS:
                filler += len(pat.findall(body))
    total = emdash + filler + multi_dm_turns
    return {
        "emdash": emdash,
        "filler": filler,
        "multi_dm_turns": multi_dm_turns,
        "total_violations": total,
        "dm_turns": dm_turns,
        "guard_message_turns": guard_turns,
        "reply_rate": (guard_replied / guard_turns) if guard_turns else None,
        "welcome_turns": welcome_turns,
        "welcomes_sent": welcomed,
        "escalations": sum(len(t.get("escalations") or []) for t in turns),
    }


def _score_voice(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    """Guardrail (no-regression) gate for a comms-voice edit.

    Modern models already largely comply with the voice policy, so this is not
    a behavioral-lift gate like verify_b. It confirms the edit (a) preserves
    every welcome and guard reply, (b) never emits MORE voice violations than
    the same-model control, and (c) does not perturb escalation behavior. Full
    compliance (variant == 0 violations) and any lift over control are reported
    as extra credit, not required for the gate.
    """
    shifts = sorted({j["shift"] for j in recipe["jobs"]})
    c_turns: list[dict] = []
    v_turns: list[dict] = []
    for sid in shifts:
        c_turns += store.load_turns(control_id, sid)
        v_turns += store.load_turns(variant_id, sid)
    cs = _voice_violations(c_turns)
    vs = _voice_violations(v_turns)
    lift = cs["total_violations"] - vs["total_violations"]

    replies_preserved = (vs["reply_rate"] or 0) >= (cs["reply_rate"] or 0) - 1e-9
    welcomes_preserved = vs["welcomes_sent"] >= cs["welcomes_sent"]
    no_regression = vs["total_violations"] <= cs["total_violations"]
    no_esc_drift = vs["escalations"] <= cs["escalations"]
    passed = replies_preserved and welcomes_preserved and no_regression and no_esc_drift
    full_compliance = vs["total_violations"] == 0

    details = []
    if not replies_preserved:
        details.append(f"reply rate dropped {cs['reply_rate']}->{vs['reply_rate']}")
    if not welcomes_preserved:
        details.append(f"welcomes dropped {cs['welcomes_sent']}->{vs['welcomes_sent']}")
    if not no_regression:
        details.append(
            f"variant added violations {cs['total_violations']}->{vs['total_violations']} "
            f"(emdash={vs['emdash']}, filler={vs['filler']}, multi_dm={vs['multi_dm_turns']})"
        )
    if not no_esc_drift:
        details.append(f"escalations rose {cs['escalations']}->{vs['escalations']}")
    return {
        "scorer": "voice",
        "shifts": shifts,
        "control": cs,
        "variant": vs,
        "violation_lift": lift,
        "full_compliance": full_compliance,
        "pass": passed,
        "detail": (
            f"No regression: variant {vs['total_violations']} violations vs control "
            f"{cs['total_violations']}; welcomes+replies preserved; no escalation drift"
            + (f"; full compliance (0 violations)." if full_compliance else ".")
            if passed
            else "; ".join(details)
        ),
    }


def _score_photo_gamer(
    store: ExperimentStore, control_id: str, variant_id: str, recipe: dict
) -> dict:
    """Process gates on a scripted photo-gamer trajectory. Aggregated pass^k."""
    from harness.scenario import score_photo_gamer_run

    return score_photo_gamer_run(store, control_id, variant_id, recipe)


SCORERS: dict[str, Callable[..., dict]] = {
    "welcome": _score_welcome,
    "verify_b": _score_verify_b,
    "escalation_focus": _score_escalation_focus,
    "quietness": _score_quietness,
    "voice": _score_voice,
    "photo_gamer": _score_photo_gamer,
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
    repeat: int | None = None,
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
    if repeat is None:
        repeat = int(recipe.get("repetitions") or 1)
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
        "repetitions": repeat,
        "dry_run": dry_run,
    }
    # Historical recipes: --dry prints the plan and stops. Scenario recipes
    # still run, against a canned copilot, so the scripted guard is exercised
    # with zero API calls.
    if dry_run and mode != "scenario":
        return {"plan": plan, "score": None, "pass": None}

    if mode == "scenario":
        from harness.scenario import execute_scenario_recipe

        return execute_scenario_recipe(
            name=name,
            recipe=recipe,
            plan=plan,
            variant_id=variant_id,
            control_id=control_id,
            root=root,
            adapter=adapter,
            model=model,
            candidate_variant=candidate_variant,
            dry_run=dry_run,
            repeat=repeat,
        )

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
