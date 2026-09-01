"""Session E: wire miner → diagnose → patch → evaluate → decide, over iterations.

One `run_loop` call is one run under `runs/loop_<stamp>/`:

    iter_01/cards.json      miner output + which card this iteration took
    iter_01/diagnosis.json  chosen card, intent triple, target file, scorer
    iter_01/patch.diff      unified diff of the ONE changed prompt file
    iter_01/score.json      ScoreCard (deterministic scorers only)
    iter_01/decision.json   decide() action + reason (+ mint / generalization info)
    manifest.json           run-level: shift, cap, model, budget, spend estimate

Append-only: an iteration directory is written once and never rewritten. Only
`manifest.json` at the run root is refreshed at the end with the final tally.

The four stage functions are injectable (`mine_fn`, `diagnose_fn`, `patch_fn`,
`evaluate_fn`) with the real Session A–D implementations as defaults, so tests
run the whole control flow with zero API calls and zero dependence on a stage
that is still being built.

What this module may NOT do (LOOP.md hard rule 3): touch a gate. `decide()` is
called verbatim from `harness.agent.policy`; the generalization spot-check is
information printed next to the decision, never an input to it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness.agent.catalog import CLASS_CATALOG, SESSION_MAP, catalog_entry
from harness.agent.errors import SessionTodo
from harness.agent.policy import decide
from harness.agent.spec import spec_rate
from harness.agent.types import (
    Diagnosis,
    LoopConfig,
    LoopDecision,
    PatchPlan,
    ProblemCard,
    ScoreCard,
)

ROOT = Path(__file__).resolve().parents[2]
SHIFTS_DIR = ROOT / "shifts"

# --- cost model ------------------------------------------------------------
# Rough per-iteration estimate, used only to stop *before* overspending. Two
# arms (same-model control + patched variant) are always run, plus the catalog
# holdout shift when the card is not itself the safety shift.
USD_PER_TURN = 0.02
TURNS_PER_SHIFT = 20  # a whole-shift arm replays roughly this many wakes
USD_PER_LLM_STAGE = 0.02  # diagnose + patch prompt calls
ARMS = 2

TERMINAL_ACTIONS = ("keep", "revert", "stop")


# ---------------------------------------------------------------------------
# plan (dry-run friendly, no stages required)
# ---------------------------------------------------------------------------


def loop_plan(
    config: LoopConfig,
    *,
    budget_usd: float | None = None,
    mint: bool = True,
    compound: bool = False,
) -> dict[str, Any]:
    """Architecture plan. Safe to call with no API keys and no Session A–D."""
    return {
        "status": "architecture",
        "shift_id": config.shift_id,
        "dry_run": config.dry_run,
        "max_iterations": config.max_iterations,
        "control_variant": config.control_variant,
        "holdout_shift": config.holdout_shift,
        "adapter": config.adapter,
        "model": config.model,
        "budget_usd": budget_usd,
        "mint_regressions": bool(mint),
        "compound_after_keep": bool(compound),
        "pipeline": [
            {"stage": "mine", "session": "A", "module": SESSION_MAP["A"]},
            {"stage": "diagnose", "session": "B", "module": SESSION_MAP["B"]},
            {"stage": "patch", "session": "C", "module": SESSION_MAP["C"]},
            {"stage": "evaluate", "session": "D", "module": SESSION_MAP["D"]},
            {"stage": "decide", "session": "done", "module": "harness.agent.policy.decide"},
        ],
        "problem_classes": {
            k: {
                "scorer": v["scorer"],
                "recipe": v["recipe"],
                "holdout_recipe": v["holdout_recipe"],
                "mode": v["mode"],
            }
            for k, v in CLASS_CATALOG.items()
        },
        "rules": [
            "cards from shift JSON only (no personas)",
            "scorers own pass/fail",
            "same-model original prompt is the control",
            "lift is variant_spec_rate > control_spec_rate on frozen turns",
            "do not score later historical guard replies as outcomes",
            "one prompt file per iteration",
            "holdout must not fail before keep",
            "generalization spot-check is information, never a gate",
            "keep mints a regression recipe; promote stays human-invoked",
        ],
        "see": "LOOP.md",
    }


def write_plan(plan: dict[str, Any], root: Path | None = None) -> Path:
    root = Path(root or ROOT)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = root / "runs" / f"loop_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def write_artifact(path: Path, payload: Any) -> Path:
    """Write one artifact. Refuses to rewrite an existing iteration file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"artifacts are append-only; {path} already exists")
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(_jsonable(payload), indent=2) + "\n", encoding="utf-8")
    return path


def _write_manifest(out_dir: Path, manifest: dict[str, Any]) -> Path:
    p = out_dir / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


def estimate_iteration_usd(
    mode: str,
    n_turns: int | None,
    *,
    holdout: bool = True,
    dry_run: bool = False,
    llm_stages: int = 2,
) -> float:
    """What one iteration is expected to cost, from the catalog mode.

    `turn` mode scores the card's frozen turns; `shift` mode replays the whole
    shift. Both arms are charged, plus the holdout shift when one applies.
    A dry run reads stored fixtures and costs nothing.
    """
    if dry_run:
        return 0.0
    target_turns = int(n_turns or TURNS_PER_SHIFT) if mode == "turn" else TURNS_PER_SHIFT
    holdout_turns = TURNS_PER_SHIFT if holdout else 0
    replay = ARMS * (target_turns + holdout_turns) * USD_PER_TURN
    return round(replay + llm_stages * USD_PER_LLM_STAGE, 4)


def estimate_card_usd(card: ProblemCard, *, dry_run: bool) -> float:
    meta = catalog_entry(card.problem_class)
    return estimate_iteration_usd(
        meta["mode"],
        len(card.turns),
        holdout=bool(meta.get("holdout_recipe")),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# generalization spot-check (information only)
# ---------------------------------------------------------------------------


def find_generalization_cards(
    problem_class: str,
    exclude_shift: str,
    *,
    mine_fn: Callable[[str], list[ProblemCard]],
    shifts_dir: Path | None = None,
    shift_ids: list[str] | None = None,
) -> list[ProblemCard]:
    """One card per OTHER shift that mines the same class (shift order)."""
    ids = shift_ids
    if ids is None:
        directory = Path(shifts_dir or SHIFTS_DIR)
        ids = [p.stem for p in sorted(directory.glob("*.json"))]
    found: list[ProblemCard] = []
    for sid in ids:
        if str(sid) == str(exclude_shift):
            continue
        try:
            cards = mine_fn(str(sid))
        except Exception:  # a shift we cannot mine is not a failure of the loop
            continue
        match = next((c for c in cards if c.problem_class == problem_class), None)
        if match is not None:
            found.append(match)
    return found


def find_generalization_card(
    problem_class: str,
    exclude_shift: str,
    *,
    mine_fn: Callable[[str], list[ProblemCard]],
    shifts_dir: Path | None = None,
    shift_ids: list[str] | None = None,
) -> ProblemCard | None:
    """First card of the same class mined from some OTHER shift, or None."""
    found = find_generalization_cards(
        problem_class,
        exclude_shift,
        mine_fn=mine_fn,
        shifts_dir=shifts_dir,
        shift_ids=shift_ids,
    )
    return found[0] if found else None


def _describe_other(
    info: dict[str, Any], other: ProblemCard, candidates: list[ProblemCard]
) -> None:
    info["other_shift"] = str(other.shift_id)
    info["other_card_id"] = other.id
    info["turns"] = [int(t) for t in other.turns]
    info["other_shift_candidates"] = [str(c.shift_id) for c in candidates]


def generalization_spot_check(
    card: ProblemCard,
    diagnosis: Diagnosis,
    patch: PatchPlan,
    *,
    dry_run: bool,
    mine_fn: Callable[[str], list[ProblemCard]],
    root: Path | None = None,
    shifts_dir: Path | None = None,
    shift_ids: list[str] | None = None,
    run_jobs_fn: Callable[..., str] | None = None,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    fixture_root: Path | None = None,
    budget_left: float | None = None,
) -> dict[str, Any]:
    """Does the kept edit help on a DIFFERENT shift with the same problem?

    Reported next to the decision as information. It is never a gate: LOOP.md
    gives PASS/FAIL to the deterministic scorers on the card's own turns, and a
    weak generalization number must not be able to veto a real lift (nor a good
    one to rescue a failed holdout).
    """
    from harness.agent.evaluate import (
        FIXTURE_CONTROL_RUN,
        FIXTURE_ROOT,
        FIXTURE_VARIANT_RUN,
        select_turns,
    )
    from harness.store import ExperimentStore

    info: dict[str, Any] = {
        "gate": False,
        "note": "information only; never a gate (LOOP.md hard rule 3)",
        "problem_class": card.problem_class,
        "source_shift": str(card.shift_id),
        "other_shift": None,
        "est_usd": 0.0,
    }

    candidates = find_generalization_cards(
        card.problem_class,
        str(card.shift_id),
        mine_fn=mine_fn,
        shifts_dir=shifts_dir,
        shift_ids=shift_ids,
    )
    if not candidates:
        info["status"] = "no_other_shift"
        info["detail"] = f"no other shift mines {card.problem_class}"
        return info

    control_id: str | None
    variant_id: str | None
    if dry_run:
        store = ExperimentStore(Path(fixture_root or FIXTURE_ROOT))
        control_id, variant_id = FIXTURE_CONTROL_RUN, FIXTURE_VARIANT_RUN
        info["source"] = "stored fixture runs (dry run, no API)"
        # Prefer a shift the fixture actually has a stored run for, so the
        # dry-run number is real rather than a shrug.
        other = next(
            (
                c
                for c in candidates
                if store.load_turns(control_id, str(c.shift_id))
                and store.load_turns(variant_id, str(c.shift_id))
            ),
            candidates[0],
        )
        _describe_other(info, other, candidates)
    else:
        other = candidates[0]
        _describe_other(info, other, candidates)
        if run_jobs_fn is None:
            try:
                from cli import run_jobs as run_jobs_fn  # lazy: keeps tests API-free
            except Exception:
                info["status"] = "skipped"
                info["detail"] = "no run_jobs available for a live spot-check"
                return info
        meta = catalog_entry(card.problem_class)
        est = estimate_iteration_usd(
            meta["mode"], len(other.turns), holdout=False, dry_run=False, llm_stages=0
        )
        info["est_usd"] = est
        if budget_left is not None and est > budget_left:
            info["status"] = "skipped"
            info["detail"] = (
                f"spot-check estimate ${est:.2f} exceeds remaining budget ${budget_left:.2f}"
            )
            return info
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        slug = f"{card.problem_class}_{other.shift_id}_{stamp}"
        control_id, variant_id = f"ctrl_gen_{slug}", f"var_gen_{slug}"
        jobs = [{"shift": str(other.shift_id), "turns": [int(t) for t in other.turns]}]
        run_jobs_fn(
            variant=patch.parent_variant,
            run_id=control_id,
            mode=meta["mode"],
            jobs=jobs,
            adapter=adapter,
            model=model,
        )
        run_jobs_fn(
            variant=patch.variant_dir,
            run_id=variant_id,
            mode=meta["mode"],
            jobs=jobs,
            adapter=adapter,
            model=model,
        )
        store = ExperimentStore(Path(root or ROOT) / "runs")
        info["source"] = f"live runs {control_id} / {variant_id}"

    want: list[int] | None = [int(t) for t in other.turns]
    all_control = store.load_turns(control_id, str(other.shift_id))
    all_variant = store.load_turns(variant_id, str(other.shift_id))
    control_rows = select_turns(all_control, want)
    variant_rows = select_turns(all_variant, want)
    if not control_rows and not variant_rows:
        if not all_control and not all_variant:
            info["status"] = "skipped"
            info["detail"] = f"no stored turns for shift {other.shift_id} in {control_id}"
            return info
        # The other shift's card turns are not in the stored run (a fixture
        # covers a slice of the shift). Score the stored turns instead and say
        # so — a spot-check on different turns is still information, as long as
        # it is not silently labelled as the card's turns.
        info["detail"] = (
            f"card turns {want} are not in the stored run; scored the "
            f"{len(all_control)} stored turns of shift {other.shift_id} instead"
        )
        info["scored_turns"] = "stored_run"
        control_rows, variant_rows, want = all_control, all_variant, None

    c_rate = spec_rate(control_rows, other.spec)
    v_rate = spec_rate(variant_rows, other.spec)
    info.update(
        {
            "status": "measured",
            "control_run_id": control_id,
            "variant_run_id": variant_id,
            "control_spec_rate": c_rate,
            "variant_spec_rate": v_rate,
            "delta": (
                round(v_rate - c_rate, 4)
                if c_rate is not None and v_rate is not None
                else None
            ),
            "control_turns_scored": len(control_rows),
            "variant_turns_scored": len(variant_rows),
        }
    )
    return info


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def _card_by_id(cards: list[ProblemCard], card_id: str) -> ProblemCard | None:
    return next((c for c in cards if c.id == card_id), None)


def _default_stages() -> dict[str, Callable[..., Any]]:
    from harness.agent.diagnose import diagnose as diagnose_fn
    from harness.agent.evaluate import evaluate_diagnosis as evaluate_fn
    from harness.agent.mine import mine_shift as mine_fn
    from harness.agent.patch import apply_patch as patch_fn

    return {
        "mine_fn": mine_fn,
        "diagnose_fn": diagnose_fn,
        "patch_fn": patch_fn,
        "evaluate_fn": evaluate_fn,
    }


def run_loop(
    config: LoopConfig,
    root: Path | None = None,
    *,
    mine_fn: Callable[..., list[ProblemCard]] | None = None,
    diagnose_fn: Callable[..., Diagnosis] | None = None,
    patch_fn: Callable[..., PatchPlan] | None = None,
    evaluate_fn: Callable[..., ScoreCard] | None = None,
    decide_fn: Callable[..., LoopDecision] = decide,
    mint_fn: Callable[..., dict[str, Any]] | None = None,
    budget_usd: float | None = None,
    mint: bool = True,
    compound: bool = False,
    generalize: bool = True,
    recipes_path: Path | None = None,
    shifts_dir: Path | None = None,
    fixture_root: Path | None = None,
    stamp: str | None = None,
    printer: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run up to `config.max_iterations` mine→patch→score→decide cycles.

    Actions come from `policy.decide` and are honored, not second-guessed:

    * `next_card` — drop the tried card, diagnose the next one in the miner's
      deterministic severity order, keep the same parent prompt.
    * `keep`      — the patch is kept: mint a regression recipe, spot-check
      generalization, and end the run (LOOP.md). With `compound=True` the kept
      auto-variant becomes the parent (and the evaluator's control) for the
      next iteration inside this same run.
    * `revert` / `stop` — end the run. Nothing is copied anywhere; the auto
      variant dir is left on disk as evidence.

    `variants/baseline` is never written to by any path here.
    """
    root = Path(root or ROOT)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dry = bool(config.dry_run)

    stages = {
        "mine_fn": mine_fn,
        "diagnose_fn": diagnose_fn,
        "patch_fn": patch_fn,
        "evaluate_fn": evaluate_fn,
    }
    if any(v is None for v in stages.values()):
        defaults = _default_stages()
        for key, value in stages.items():
            if value is None:
                stages[key] = defaults[key]
    mine_fn = stages["mine_fn"]
    diagnose_fn = stages["diagnose_fn"]
    patch_fn = stages["patch_fn"]
    evaluate_fn = stages["evaluate_fn"]
    if mint_fn is None:
        from harness.agent.mint import mint_regression_recipe as mint_fn

    plan = loop_plan(config, budget_usd=budget_usd, mint=mint, compound=compound)
    out_dir = root / "runs" / f"loop_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"loop_{stamp}"

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "plan": plan,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "budget_usd": budget_usd,
        "spend_estimate_usd": 0.0,
        "iterations": [],
        "minted_recipes": [],
        "final_action": None,
        "final_reason": None,
        "kept_variant": None,
        "parent_variant": config.control_variant,
        "blocked_on": None,
    }
    _write_manifest(out_dir, manifest)

    result: dict[str, Any] = {
        "plan": plan,
        "out_dir": str(out_dir),
        "run_id": run_id,
        "decision": None,
        "decisions": [],
        "iterations": [],
        "minted_recipes": [],
        "kept_variant": None,
        "spend_estimate_usd": 0.0,
        "budget_usd": budget_usd,
        "stopped_reason": None,
    }

    def finish(action: str | None, reason: str | None) -> dict[str, Any]:
        manifest["final_action"] = action
        manifest["final_reason"] = reason
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["spend_estimate_usd"] = round(result["spend_estimate_usd"], 4)
        manifest["iterations"] = result["iterations"]
        manifest["minted_recipes"] = result["minted_recipes"]
        manifest["kept_variant"] = result["kept_variant"]
        _write_manifest(out_dir, manifest)
        result["stopped_reason"] = reason
        return result

    try:
        cards = list(mine_fn(config.shift_id))
    except SessionTodo as todo:
        manifest["blocked_on"] = str(todo)
        manifest["blocked_session"] = getattr(todo, "session", None)
        result["blocked_session"] = getattr(todo, "session", None)
        printer(f"loop blocked: {todo}")
        return finish(None, f"blocked: {todo}")

    for card in cards:
        card.validate()
    if not cards:
        printer(f"no cards mined from shift {config.shift_id}")
        return finish("stop", "miner found no cards in this shift")

    remaining = list(cards)  # miner order == deterministic severity order
    parent_variant = config.control_variant
    iteration = 0

    while remaining and iteration < config.max_iterations:
        next_est = estimate_card_usd(remaining[0], dry_run=dry)
        if budget_usd is not None and result["spend_estimate_usd"] + next_est > budget_usd:
            reason = (
                f"budget: next iteration ~${next_est:.2f} would exceed the "
                f"${budget_usd:.2f} budget (spent ~${result['spend_estimate_usd']:.2f})."
            )
            printer(f"stop (budget): {reason}")
            manifest["budget_stop"] = {
                "estimate_usd": next_est,
                "spend_estimate_usd": round(result["spend_estimate_usd"], 4),
                "budget_usd": budget_usd,
            }
            return finish("stop", reason)

        iteration += 1
        iter_dir = out_dir / f"iter_{iteration:02d}"
        iter_dir.mkdir(parents=True, exist_ok=True)

        try:
            diagnosis = diagnose_fn(
                remaining,
                skip_llm=dry,
                adapter=config.adapter,
                model=config.model,
            )
            card = _card_by_id(cards, diagnosis.card_id) or remaining[0]
            write_artifact(
                iter_dir / "cards.json",
                {
                    "shift_id": config.shift_id,
                    "iteration": iteration,
                    "count": len(cards),
                    "chosen_card_id": diagnosis.card_id,
                    "considered_card_ids": [c.id for c in remaining],
                    "cards": [c.to_dict() for c in cards],
                },
            )
            write_artifact(iter_dir / "diagnosis.json", diagnosis.to_dict())

            patch = patch_fn(diagnosis, parent_variant=parent_variant, skip_llm=dry)
            write_artifact(iter_dir / "patch.diff", patch.diff or "")

            score = evaluate_fn(
                diagnosis,
                patch,
                control_variant=parent_variant,
                adapter=config.adapter,
                model=config.model,
                dry_run=dry,
                card=card,
            )
        except SessionTodo as todo:
            manifest["blocked_on"] = str(todo)
            manifest["blocked_session"] = getattr(todo, "session", None)
            result["blocked_session"] = getattr(todo, "session", None)
            printer(f"loop blocked at iteration {iteration}: {todo}")
            return finish(None, f"blocked: {todo}")

        # The prompt this iteration was measured against — recorded before a
        # keep moves the parent forward, so the artifact says what was compared.
        iteration_parent = patch.parent_variant or parent_variant
        result["spend_estimate_usd"] += next_est
        write_artifact(iter_dir / "score.json", score.to_dict())

        decision = decide_fn(
            score, iteration=iteration, max_iterations=config.max_iterations
        )
        payload: dict[str, Any] = dict(decision.to_dict())
        payload.update(
            {
                "iteration": iteration,
                "run_id": run_id,
                "shift_id": config.shift_id,
                "card_id": diagnosis.card_id,
                "problem_class": diagnosis.problem_class,
                "target_file": diagnosis.target_file,
                "parent_variant": iteration_parent,
                "variant_dir": patch.variant_dir,
                "budget_usd": budget_usd,
                "iteration_estimate_usd": next_est,
                "spend_estimate_usd": round(result["spend_estimate_usd"], 4),
            }
        )

        minted: dict[str, Any] | None = None
        generalization: dict[str, Any] | None = None

        if decision.action == "keep":
            if generalize:
                try:
                    generalization = generalization_spot_check(
                        card,
                        diagnosis,
                        patch,
                        dry_run=dry,
                        mine_fn=mine_fn,
                        root=root,
                        shifts_dir=shifts_dir,
                        adapter=config.adapter,
                        model=config.model,
                        fixture_root=fixture_root,
                        budget_left=(
                            None
                            if budget_usd is None
                            else budget_usd - result["spend_estimate_usd"]
                        ),
                    )
                    result["spend_estimate_usd"] += float(
                        generalization.get("est_usd") or 0.0
                    )
                except Exception as exc:  # info only: never blocks a keep
                    generalization = {
                        "gate": False,
                        "status": "error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
            if mint:
                try:
                    minted = mint_fn(
                        card,
                        diagnosis,
                        kept_variant=patch.variant_dir,
                        control_variant=iteration_parent,
                        recipes_path=recipes_path,
                        run_id=run_id,
                        stamp=f"{stamp}-i{iteration:02d}",
                    )
                    result["minted_recipes"].append(minted["name"])
                    from harness.agent.mint import format_mint_summary

                    printer(format_mint_summary(minted))
                except Exception as exc:
                    minted = {"error": f"{type(exc).__name__}: {exc}"}
                    printer(f"mint failed: {minted['error']}")
            else:
                printer("minting disabled (--no-mint); no regression recipe written")
            result["kept_variant"] = patch.variant_dir
            # Compounding: the kept variant is the parent (and the evaluator's
            # control) from here on. variants/baseline is untouched.
            parent_variant = patch.variant_dir
            manifest["parent_variant"] = parent_variant

        payload["generalization"] = generalization
        payload["minted_recipe"] = minted
        write_artifact(iter_dir / "decision.json", payload)

        result["decisions"].append(payload)
        result["decision"] = payload
        result["iterations"].append(
            {
                "iteration": iteration,
                "dir": str(iter_dir),
                "card_id": diagnosis.card_id,
                "problem_class": diagnosis.problem_class,
                "action": decision.action,
                "reason": decision.reason,
                "parent_variant": iteration_parent,
                "variant_dir": patch.variant_dir,
                "estimate_usd": next_est,
                "minted_recipe": (minted or {}).get("name"),
                "generalization": (generalization or {}).get("status"),
            }
        )
        printer(
            f"iter {iteration:02d} [{diagnosis.problem_class}] -> "
            f"{decision.action}: {decision.reason}"
        )

        if decision.action == "keep" and not compound:
            return finish("keep", decision.reason)
        if decision.action in ("revert", "stop"):
            return finish(decision.action, decision.reason)

        # keep+compound, or next_card: retire this card and go again.
        remaining = [c for c in remaining if c.id != diagnosis.card_id]

    if not remaining:
        reason = "no cards left to try"
    else:
        reason = f"iteration cap {config.max_iterations} reached"
    printer(f"stop: {reason}")
    return finish("stop", reason)


def format_run_summary(result: dict[str, Any]) -> str:
    """Human summary of a run. Reports the keep, then the generalization info."""
    lines = [f"loop run {result.get('run_id')} -> {result.get('out_dir')}"]
    for row in result.get("iterations") or []:
        lines.append(
            f"  iter {row['iteration']:02d}  {row['problem_class']:<22} "
            f"{row['action']:<10} {row['reason']}"
        )
    last = result.get("decision") or {}
    gen = last.get("generalization") or {}
    if gen.get("status") == "measured":
        lines.append(
            f"  generalization (info, not a gate): shift {gen.get('other_shift')} "
            f"spec_rate control={gen.get('control_spec_rate')} "
            f"variant={gen.get('variant_spec_rate')} delta={gen.get('delta')}"
        )
    elif gen:
        lines.append(
            f"  generalization (info, not a gate): {gen.get('status')} — {gen.get('detail')}"
        )
    if result.get("minted_recipes"):
        lines.append(f"  minted: {', '.join(result['minted_recipes'])}")
    if result.get("kept_variant"):
        lines.append(
            f"  kept variant: {result['kept_variant']} "
            "(promote it by hand with: py cli.py promote <auto> <name>)"
        )
    lines.append(
        f"  spend estimate: ${result.get('spend_estimate_usd', 0.0):.2f}"
        + (
            f" of ${result['budget_usd']:.2f} budget"
            if result.get("budget_usd") is not None
            else ""
        )
    )
    return "\n".join(lines)
