"""Session E: the self-build step — mint regression recipes, promote variants.

Two human-facing halves of "the loop builds its own eval suite":

* `mint_regression_recipe` — on every keep, append a recipe to
  `experiments/recipes.json` that pins the card's shift + turns + the catalog
  scorer with the kept auto-variant as candidate. **Additive only**: an
  existing recipe is never rewritten or deleted; a name collision gets a
  numbered suffix instead of clobbering.
* `promote_variant` — a human copies a kept `variants/auto_*` dir to a named
  variant. The loop never calls this: it prints the diff vs baseline and asks
  for confirmation first. `variants/baseline` can never be the target.

Neither function scores anything. Gates stay in `policy.decide` and the
deterministic scorers.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness.agent.catalog import catalog_entry
from harness.agent.types import Diagnosis, ProblemCard

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECIPES_PATH = ROOT / "experiments" / "recipes.json"
BASELINE_VARIANT = "variants/baseline"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# ---------------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------------


def regression_recipe_name(problem_class: str, shift_id: str, stamp: str) -> str:
    return f"regress-{problem_class.replace('_', '-')}-{shift_id}-{stamp}"


def _unique_name(name: str, taken: set[str]) -> str:
    """Never overwrite: fall back to name-2, name-3, … on a collision."""
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def build_regression_recipe(
    card: ProblemCard,
    diagnosis: Diagnosis,
    *,
    kept_variant: str,
    control_variant: str = BASELINE_VARIANT,
    run_id: str | None = None,
) -> dict[str, Any]:
    """The recipe body. Pure function so tests can assert on it directly."""
    meta = catalog_entry(card.problem_class)
    scorer = diagnosis.scorer or meta["scorer"]
    from harness.recipes import SCORERS  # local: keeps import cost off the CLI path

    registered = scorer in SCORERS
    target = (diagnosis.target_file or "").replace("\\", "/")
    note = (
        ""
        if registered
        else f" NOTE: scorer {scorer!r} is not registered in harness.recipes.SCORERS yet."
    )
    return {
        "description": (
            f"Regression lock minted by the loop: {card.problem_class} on shift "
            f"{card.shift_id} turns {list(card.turns)}; keeps the fix in "
            f"{kept_variant} from regressing." + note
        ),
        "control_variant": control_variant,
        "candidate_variant": kept_variant,
        "mode": diagnosis.mode or meta["mode"],
        "jobs": [{"shift": str(card.shift_id), "turns": [int(t) for t in card.turns]}],
        "scorer": scorer,
        "scorer_registered": registered,
        "suggested_when": [Path(target).name] if target else [],
        "card": {
            "intent": [card.problem_class],
            "risk_class": "regression",
            "minted_by": "loop",
            "minted_at": datetime.now(timezone.utc).isoformat(),
            "source_card_id": card.id,
            "source_run": run_id,
            "target_file": diagnosis.target_file,
            "cost": "full-shift" if (diagnosis.mode or meta["mode"]) == "shift" else "turn-cheap",
            "est_usd": 1.0 if (diagnosis.mode or meta["mode"]) == "shift" else 0.1 * max(len(card.turns), 1),
            "covers": [card.problem_class],
            "required_when": {
                "files": [Path(target).name] if target else [],
                "intents": [card.problem_class],
            },
            "after": [],
            "on_fail": [],
        },
        "spec": card.spec.to_dict(),
    }


def mint_regression_recipe(
    card: ProblemCard,
    diagnosis: Diagnosis,
    *,
    kept_variant: str,
    control_variant: str = BASELINE_VARIANT,
    recipes_path: Path | None = None,
    run_id: str | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    """Append one regression recipe to recipes.json. Additive only.

    Returns `{"name", "recipe", "path", "recipe_count_before", "recipe_count_after"}`.
    """
    path = Path(recipes_path or DEFAULT_RECIPES_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    recipes = data.setdefault("recipes", {})
    before = dict(recipes)

    stamp = stamp or _stamp()
    name = _unique_name(
        regression_recipe_name(card.problem_class, str(card.shift_id), stamp),
        set(recipes),
    )
    recipe = build_regression_recipe(
        card,
        diagnosis,
        kept_variant=kept_variant,
        control_variant=control_variant,
        run_id=run_id,
    )
    recipes[name] = recipe

    # Paranoia, not politeness: a mint that dropped or edited an existing
    # recipe would silently shrink the suite it is supposed to grow.
    for key, value in before.items():
        if recipes.get(key) != value:
            raise RuntimeError(f"minting would have modified existing recipe {key!r}")

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "name": name,
        "recipe": recipe,
        "path": str(path),
        "recipe_count_before": len(before),
        "recipe_count_after": len(recipes),
    }


def format_mint_summary(minted: dict[str, Any]) -> str:
    r = minted["recipe"]
    job = (r.get("jobs") or [{}])[0]
    lines = [
        f"minted regression recipe: {minted['name']}",
        f"  candidate : {r.get('candidate_variant')}",
        f"  control   : {r.get('control_variant')}",
        f"  shift     : {job.get('shift')} turns {job.get('turns')}",
        f"  scorer    : {r.get('scorer')}"
        + ("" if r.get("scorer_registered") else "  (probe not registered yet)"),
        f"  recipes   : {minted['recipe_count_before']} -> {minted['recipe_count_after']}"
        f" in {minted['path']}",
        "  run it with:  py cli.py test " + minted["name"],
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# promote (human-invoked)
# ---------------------------------------------------------------------------


def _resolve_variant(name: str, root: Path) -> Path:
    raw = str(name).replace("\\", "/").rstrip("/")
    p = Path(raw)
    if p.is_absolute():
        return p
    if not raw.startswith("variants/"):
        raw = f"variants/{raw}"
    return root / raw


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def promote_variant(
    auto_variant: str,
    named_variant: str,
    *,
    root: Path | None = None,
    baseline: str = BASELINE_VARIANT,
    confirm_fn: Callable[[str], bool] | None = None,
    yes: bool = False,
    printer: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Copy a kept auto-variant to a named variant dir, after a human says yes.

    The diff vs baseline is printed first. `confirm_fn` is injectable so tests
    never touch stdin. Refusing copies nothing.
    """
    from harness.advisor import prompt_file_diff

    root = Path(root or ROOT)
    src = _resolve_variant(auto_variant, root)
    dest = _resolve_variant(named_variant, root)
    base = _resolve_variant(baseline, root)

    if not src.is_dir():
        raise FileNotFoundError(f"no variant dir at {src}")
    if dest.resolve() == base.resolve():
        raise ValueError("refusing to promote over variants/baseline")
    if dest.exists():
        raise FileExistsError(
            f"{_rel(dest, root)} already exists; promote to a new name "
            "(promote never overwrites a named variant)"
        )

    changed: list[str] = []
    diff = ""
    if base.is_dir():
        changed, diff = prompt_file_diff(base, src)

    printer(f"promote {_rel(src, root)} -> {_rel(dest, root)}")
    printer(f"changed vs {_rel(base, root)}: {', '.join(changed) if changed else '(none)'}")
    printer(diff or "(no textual diff)")

    if yes:
        approved = True
    else:
        ask = confirm_fn or _stdin_confirm
        approved = bool(ask(f"copy {_rel(src, root)} to {_rel(dest, root)}? [y/N] "))

    if not approved:
        printer("declined; nothing copied.")
        return {
            "promoted": False,
            "reason": "declined",
            "source": _rel(src, root),
            "dest": _rel(dest, root),
            "changed_files": changed,
            "diff": diff,
        }

    shutil.copytree(src, dest)
    printer(f"promoted -> {_rel(dest, root)}")
    return {
        "promoted": True,
        "reason": "confirmed" if not yes else "confirmed (--yes)",
        "source": _rel(src, root),
        "dest": _rel(dest, root),
        "changed_files": changed,
        "diff": diff,
    }


def _stdin_confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")
