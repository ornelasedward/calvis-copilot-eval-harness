"""CLI for the Calvis replay harness."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from harness.adapters.replay import iter_baseline_turns
from harness.engine import EngineConfig, ReplayEngine, import_baseline_as_run
from harness.evals import (
    Assertion,
    changed_decisions,
    evaluate_assertion,
    summarize_turns,
)
from harness.loader import load_shift
from harness.scheduler import build_schedule
from harness.store import ExperimentStore, new_run_id, prompt_dir_hash
from harness.schemas import RunManifest

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DEFAULT_SHIFTS = ["56370", "55252", "50737"]


def _code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _data_version() -> str:
    import hashlib
    h = hashlib.sha256()
    for p in sorted((ROOT / "shifts").glob("*.json")):
        h.update(p.name.encode())
        h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:12]


def _make_adapter(adapter: str, model: str):
    from harness.adapters.anthropic import AnthropicAdapter
    from harness.adapters.openai import OpenAIAdapter

    if adapter == "anthropic":
        return AnthropicAdapter(model=model)
    if adapter == "openai":
        return OpenAIAdapter(model=model)
    raise SystemExit(f"unknown adapter: {adapter}")


def _model_params(adapter: str, model: str) -> dict:
    if adapter == "openai" and str(model).startswith("gpt-5"):
        return {"reasoning_effort": "none"}
    return {"temperature": 0}


def run_jobs(
    *,
    variant: str,
    run_id: str,
    mode: str,
    jobs: list[dict],
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    repeat: int = 1,
    allow_empty_obligations: bool = False,
) -> str:
    """In-process multi-shift run used by recipes. One create + one seal."""
    variant_dir = Path(variant)
    if not variant_dir.is_absolute():
        variant_dir = ROOT / variant_dir
    if not (variant_dir / "core").exists():
        raise SystemExit(f"variant missing core/: {variant_dir}")

    model_params = _model_params(adapter, model)
    store = ExperimentStore(ROOT / "runs")
    shifts = [str(j["shift"]) for j in jobs]
    manifest = RunManifest(
        run_id=run_id,
        variant_name=variant_dir.name,
        prompt_hash=prompt_dir_hash(variant_dir),
        model=model,
        model_params=model_params,
        adapter=adapter,
        data_version=_data_version(),
        code_version=_code_version(),
        mode=mode,
        shifts=shifts,
        repetitions=repeat,
        created_at=datetime.now(timezone.utc).isoformat(),
        tool_fixture_mode="exact_or_unavailable",
    )
    store.create_run(manifest)
    ad = _make_adapter(adapter, model)
    grand = {"turns": 0, "cost_usd": 0.0}

    for job in jobs:
        sid = str(job["shift"])
        turns = job.get("turns")
        shift = load_shift(ROOT / "shifts" / f"{sid}.json")
        engine = ReplayEngine(
            shift=shift,
            adapter=ad,  # type: ignore[arg-type]
            config=EngineConfig(
                variant_dir=variant_dir,
                mode=mode,  # type: ignore[arg-type]
                allow_empty_obligations=allow_empty_obligations,
                model_params=model_params,
            ),
            run_id=run_id,
        )
        schedule = build_schedule(shift)
        if turns:
            want = set(int(t) for t in turns)
            schedule = [w for w in schedule if w.turn in want]
        for wake in schedule:
            if wake.skipped:
                continue
            result = engine.run_turn(wake.turn, wake.trigger, wake.ts)
            store.append_turn(run_id, sid, result)
            store.write_raw_trace(
                run_id,
                sid,
                [
                    {
                        "turn": wake.turn,
                        "trigger": wake.trigger,
                        "selected_instruction": result.selected_instruction,
                        "events": result.raw_events,
                    }
                ],
            )
            print(
                f"[{sid} t{wake.turn} {wake.trigger}] "
                f"{result.decision} dms={len(result.messages)} "
                f"gaps={len(result.data_gaps)} conf={result.confidence} "
                f"instr={result.selected_instruction} "
                f"cost=${result.usage.cost_usd:.4f}"
            )
            grand["turns"] += 1
            grand["cost_usd"] += result.usage.cost_usd

    store.seal_run(run_id, grand)
    print(f"done -> runs/{run_id}  total_cost=${grand['cost_usd']:.4f}")
    return run_id


def cmd_import_baseline(args: argparse.Namespace) -> None:
    store = ExperimentStore(ROOT / "runs")
    run_id = args.run_id or f"baseline_{new_run_id()}"
    variant_dir = ROOT / "prompts"
    manifest = RunManifest(
        run_id=run_id,
        variant_name="baseline",
        prompt_hash=prompt_dir_hash(variant_dir),
        model="production-baseline",
        model_params={},
        adapter="replay",
        data_version=_data_version(),
        code_version=_code_version(),
        mode="baseline_import",
        shifts=args.shifts,
        repetitions=1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.create_run(manifest)
    totals = {"turns": 0, "dms": 0, "escalations": 0}
    for sid in args.shifts:
        shift = load_shift(ROOT / "shifts" / f"{sid}.json")
        turns = import_baseline_as_run(shift, variant_dir, run_id)
        for t in turns:
            store.append_turn(run_id, sid, t)
        summary = summarize_turns([t.to_dict() for t in turns])
        print(f"shift {sid}: {summary}")
        totals["turns"] += summary["turns"]
        totals["dms"] += summary["dms"]
        totals["escalations"] += summary["escalation_total"]
    store.seal_run(run_id, totals)
    print(f"imported baseline -> runs/{run_id}")


def cmd_run(args: argparse.Namespace) -> None:
    jobs = [{"shift": sid, "turns": args.turns} for sid in args.shifts]
    if args.max_turns and not args.turns:
        # max_turns applies per shift when turns not specified
        for job in jobs:
            job["max_turns"] = args.max_turns
    run_id = args.run_id or new_run_id()
    # honor max_turns via schedule slice inside a thin wrapper
    if args.max_turns and not args.turns:
        variant_dir = Path(args.variant)
        if not variant_dir.is_absolute():
            variant_dir = ROOT / variant_dir
        limited_jobs = []
        for sid in args.shifts:
            shift = load_shift(ROOT / "shifts" / f"{sid}.json")
            schedule = build_schedule(shift)[: args.max_turns]
            limited_jobs.append(
                {"shift": sid, "turns": [w.turn for w in schedule if not w.skipped]}
            )
        jobs = limited_jobs
    run_jobs(
        variant=args.variant,
        run_id=run_id,
        mode=args.mode,
        jobs=jobs,
        adapter=args.adapter,
        model=args.model,
        repeat=args.repeat,
        allow_empty_obligations=args.allow_empty_obligations,
    )


def cmd_compare(args: argparse.Namespace) -> None:
    store = ExperimentStore(ROOT / "runs")
    baseline = store.load_turns(args.baseline, args.shift)
    variant = store.load_turns(args.variant, args.shift)
    if not baseline or not variant:
        sys.exit("missing turns — import baseline and run a variant first")

    bs = summarize_turns(baseline)
    vs = summarize_turns(variant)
    changes = changed_decisions(baseline, variant)
    operational = [c for c in changes if c["tier"] == "operational"]

    print("=== baseline ===")
    print(json.dumps(bs, indent=2))
    print("=== variant ===")
    print(json.dumps(vs, indent=2))
    print(f"=== changed decisions: {len(changes)} ({len(operational)} operational) ===")
    for c in changes[:30]:
        print(
            f"  turn {c['turn']} [{c['trigger']}] "
            f"{c['baseline_decision']} -> {c['variant_decision']} "
            f"({c['tier']}, conf={c['confidence']})"
        )

    if args.assertions:
        specs = json.loads(Path(args.assertions).read_text(encoding="utf-8"))
        for raw in specs:
            a = Assertion(**raw)
            r = evaluate_assertion(a, variant, baseline)
            mark = r.status.upper()
            print(f"[{mark}] {r.id}: {r.description}")
            print(f"       {r.detail}")


def cmd_init_variants(_: argparse.Namespace) -> None:
    """Copy bundle prompts into variants/baseline and stub A/B dirs."""
    src = ROOT / "prompts"
    variants = ROOT / "variants"
    for name in ("baseline", "variant_a", "variant_b"):
        dest = variants / name
        if dest.exists():
            print(f"skip existing {dest}")
            continue
        (dest / "core").mkdir(parents=True)
        (dest / "instructions").mkdir(parents=True)
        for p in (src / "core").glob("*.md"):
            shutil.copy2(p, dest / "core" / p.name)
        for p in (src / "instructions").glob("*.md"):
            shutil.copy2(p, dest / "instructions" / p.name)
        print(f"created {dest}")
    print("Edit variants/variant_a and variants/variant_b, then: py cli.py run --variant variants/variant_a")


def cmd_recipes(_: argparse.Namespace) -> None:
    from harness.recipes import list_analyze_targets, list_recipes

    rows = list_recipes()
    print("TEST RECIPES  (cx t <code>)")
    print(f"  {'CODE':4}  {'MEANING':36}  RECIPE")
    print("  " + "-" * 72)
    for r in rows:
        code = (r.get("aliases") or ["-"])[0]
        meaning = ""
        if r.get("alias_meaning"):
            # "wl=welcome..." -> take after first =
            parts = r["alias_meaning"].split("=", 1)
            meaning = parts[1] if len(parts) > 1 else r["alias_meaning"]
        print(f"  {code:4}  {meaning:36}  {r['name']}")

    print("\nANALYZE TARGETS  (cx why <code>)")
    print(f"  {'CODE':4}  {'MEANING':36}  PATH")
    print("  " + "-" * 72)
    for t in list_analyze_targets():
        print(f"  {t['code']:4}  {t['meaning']:36}  {t['path']}")

    print("\nExamples:  .\\cx t cl -n    .\\cx go -n --files scheduled_check_in.md    .\\cx t es")


def cmd_test(args: argparse.Namespace) -> None:
    from harness.recipes import execute_recipe

    result = execute_recipe(
        args.recipe,
        run_jobs_fn=run_jobs,
        adapter=args.adapter,
        model=args.model,
        candidate_variant=args.variant,
        control_variant=args.control,
        repeat=args.repeat,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        # Historical recipes: plan only. Scenario recipes already printed a GATE.
        if result.get("score") is None:
            print(json.dumps(result["plan"], indent=2))
            return
        if result.get("pass") is False:
            sys.exit(1)
        return
    if result.get("pass") is False:
        sys.exit(1)


def cmd_go(args: argparse.Namespace) -> None:
    """Plan (and optionally execute) recipes from changed files / intent."""
    from harness.recipes import execute_recipe
    from harness.router import (
        compile_plan,
        confirm_execute,
        discover_changed_files,
        execute_plan,
        format_plan_table,
        resolve_variant_dir,
        save_plan,
    )

    files_override = None
    if args.files is not None:
        files_override = [p.strip() for p in str(args.files).split(",") if p.strip()]

    variant_dir = None
    variant_path = None
    if args.variant:
        variant_dir = resolve_variant_dir(args.variant)
        if variant_dir is not None:
            try:
                variant_path = variant_dir.relative_to(ROOT).as_posix()
            except ValueError:
                variant_path = str(variant_dir)

    changed = discover_changed_files(
        variant_dir,
        files_override=files_override,
        root=ROOT,
    )
    plan = compile_plan(
        changed_files=changed,
        intent=args.intent,
        budget=args.budget,
        skip_safety_i_know=bool(args.skip_safety_i_know),
    )

    if getattr(args, "rank", False):
        from harness.router import format_ranked_go_output, rank_plan

        ranked = rank_plan(
            plan,
            changed_files=changed,
            intent=args.intent,
            variant_dir=variant_dir,
        )
        print(format_ranked_go_output(plan, ranked), flush=True)
        plan = ranked.ranked_plan
        plan_path = save_plan(plan, ROOT / "runs")
        print(f"wrote {plan_path}")
    else:
        print(format_plan_table(plan), flush=True)
        plan_path = save_plan(plan, ROOT / "runs")
        print(f"wrote {plan_path}")
    for warning in plan.get("warnings") or []:
        print(warning, file=sys.stderr, flush=True)

    if args.plan_only:
        return
    if not confirm_execute(yes=bool(args.yes)):
        print("aborted")
        sys.exit(1)

    def _exec(name: str) -> dict:
        return execute_recipe(
            name,
            run_jobs_fn=run_jobs,
            adapter=args.adapter,
            model=args.model,
            candidate_variant=variant_path,
            repeat=1,
            dry_run=False,
        )

    outcome = execute_plan(plan, execute_fn=_exec)
    failed = any(r.get("pass") is False for r in outcome.get("results") or [])
    if outcome.get("halted") or failed:
        sys.exit(1)


def cmd_loop(args: argparse.Namespace) -> None:
    """Eval-loop agent. Dry-run prints architecture plan (LOOP.md). Live needs Sessions A–D."""
    import json

    from harness.agent.orchestrator import run_loop
    from harness.agent.types import LoopConfig

    cfg = LoopConfig(
        shift_id=str(args.shift),
        dry_run=bool(args.dry_run),
        adapter=getattr(args, "adapter", None) or "openai",
        model=getattr(args, "model", None) or "gpt-5.6-sol",
    )
    result = run_loop(cfg)
    print(json.dumps(result["plan"], indent=2))
    print(f"\nwrote {result['out_dir']}/manifest.json")
    if result.get("decision"):
        print("\n=== decision ===")
        print(json.dumps(result["decision"], indent=2))


def cmd_sim(args: argparse.Namespace) -> None:
    """Shift-seeded simulated guard (see harness/simulate.py).

    Eval-only: the guard side is fiction and never feeds `cx loop` (LOOP.md rule 1).
    """
    from harness.simulate import run_simulation, score_simulation_run
    from harness.store import ExperimentStore

    store = ExperimentStore(ROOT / "runs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"sim_{args.shift}_{stamp}"
    run_simulation(
        str(args.shift),
        args.variant or "variants/baseline",
        from_turn=int(args.from_turn),
        max_turns=int(args.max_turns),
        repeat=int(args.repeat),
        adapter=args.adapter or "openai",
        model=args.model or "gpt-5.6-sol",
        dry_run=bool(args.dry_run),
        pressure=args.pressure,
        store=store,
        run_id=run_id,
        root=ROOT,
    )
    recipe = {
        "jobs": [{"shift": str(args.shift), "from_turn": int(args.from_turn)}],
        "pressure": args.pressure,
    }
    score = score_simulation_run(store, "", run_id, recipe)
    print("\n=== score card ===")
    print(json.dumps(score, indent=2, default=str))
    print(f"GATE: {'PASS' if score.get('pass') else 'FAIL'}")
    if not score.get("pass"):
        sys.exit(1)


def cmd_analyze(args: argparse.Namespace) -> None:
    from harness.advisor import run_advisor

    control_variant = args.control_variant
    variant = args.variant
    if args.diff:
        if len(args.diff) != 2:
            sys.exit("--diff needs exactly two paths: baseline candidate")
        control_variant, variant = args.diff
    out = run_advisor(
        control_variant=control_variant,
        variant=variant,
        control_run=args.control_run,
        variant_run=args.variant_run,
        shift=args.shift,
        focus_turns=args.focus_turns,
        adapter=args.adapter,
        model=args.model,
        skip_llm=args.no_llm,
    )
    print(out["narrative"])
    print(f"\nwrote {out['out_dir']}/advisor_report.md")


def _cmd_mine(args: argparse.Namespace) -> None:
    from harness.miner import cmd_mine

    cmd_mine(args)


def cmd_judge(args: argparse.Namespace) -> None:
    """Advisory only — flags never become GATE pass/fail."""
    from harness.judge import format_advisory_dashboard, run_checklist_on_run, run_pairwise

    if args.pair:
        out = run_pairwise(
            args.pair[0],
            args.pair[1],
            shift=args.shift,
            adapter=args.adapter,
            model=args.model,
        )
        print(format_advisory_dashboard(out["control"], title=f"control {args.pair[0]}"))
        print()
        print(format_advisory_dashboard(out["variant"], title=f"variant {args.pair[1]}"))
        print()
        print(
            format_advisory_dashboard(
                {
                    "items": [],
                    "must_not_happen": [],
                    "flagged": False,
                    "agreement": out["preference"]["agreement"],
                    "winner": out["preference"]["winner"],
                },
                title="pairwise preference",
            )
        )
        print(f"wrote {out['wrote']}")
        return
    if not args.run_id:
        sys.exit("usage: cx judge <run_id>  |  cx judge --pair <control_run> <variant_run>")
    out = run_checklist_on_run(
        args.run_id,
        shift=args.shift,
        adapter=args.adapter,
        model=args.model,
    )
    print(format_advisory_dashboard(out))
    print(f"wrote {out['wrote']}")


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Print alignment vs gold labels. Low alignment must not fail the process."""
    from harness.judge import format_alignment_report, run_calibration

    gold = Path(args.gold) if args.gold else None
    out = run_calibration(gold_path=gold, adapter=args.adapter, model=args.model)
    print(format_alignment_report(out))
    print(f"wrote {out['wrote']}")
    if not out.get("n_labels"):
        print(
            "ADVISORY  gold labels are empty — humans fill "
            "experiments/gold/conduct_labels.json (target 20-40 transcripts)"
        )


def cmd_label(args: argparse.Namespace) -> None:
    """Human gold labels for the advisory checklist. Never auto-filled, never a gate."""
    from harness.label import (
        export_worksheet,
        format_import_summary,
        format_status,
        import_worksheet,
        label_run,
        status,
    )

    out = Path(args.out) if args.out else None
    if args.status:
        print(format_status(status(out=out)))
        return
    if args.import_path:
        summary = import_worksheet(args.import_path, out=out, relabel=args.relabel)
        print(format_import_summary(summary))
        return
    if args.export:
        to = args.to or f"labels_{args.export}.md"
        path = export_worksheet(args.export, to, shift=args.shift)
        print(f"wrote worksheet {path}")
        print("Fill the label cells (y/n/na), then: cx label --import <file>")
        return
    if not args.run_id:
        sys.exit(
            "usage: cx label <run_id> [--shift S] [--relabel]\n"
            "       cx label --export <run_id> --to labels.md\n"
            "       cx label --import labels.md\n"
            "       cx label --status"
        )
    label_run(args.run_id, shift=args.shift, out=out, relabel=args.relabel)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="calvis-eval")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("import-baseline", help="Import production baseline as run zero")
    b.add_argument("--shifts", nargs="+", default=DEFAULT_SHIFTS)
    b.add_argument("--run-id", default=None)
    b.set_defaults(func=cmd_import_baseline)

    r = sub.add_parser("run", help="Run a prompt variant against shifts")
    r.add_argument("--variant", required=True)
    r.add_argument("--shifts", nargs="+", default=DEFAULT_SHIFTS)
    r.add_argument("--mode", choices=["turn", "shift"], default="shift")
    r.add_argument("--adapter", choices=["anthropic", "openai"], default="anthropic")
    r.add_argument("--model", default="claude-opus-4-6")
    r.add_argument("--reasoning-effort", default="low",
                   choices=["none", "low", "medium", "high", "xhigh", "max"])
    r.add_argument("--max-turns", type=int, default=None)
    r.add_argument("--turns", type=int, nargs="*", default=None)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--run-id", default=None)
    r.add_argument("--allow-empty-obligations", action="store_true")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="Compare a variant run to baseline")
    c.add_argument("--baseline", required=True)
    c.add_argument("--variant", required=True)
    c.add_argument("--shift", required=True)
    c.add_argument("--assertions", default=None)
    c.set_defaults(func=cmd_compare)

    i = sub.add_parser("init-variants", help="Seed variants/ from prompts/")
    i.set_defaults(func=cmd_init_variants)

    lr = sub.add_parser("recipes", help="List short named eval recipes")
    lr.set_defaults(func=cmd_recipes)

    t = sub.add_parser("test", help="Run a named recipe (control + candidate + score)")
    t.add_argument("recipe", help="Recipe name (see: py cli.py recipes)")
    t.add_argument("--adapter", choices=["anthropic", "openai"], default=None)
    t.add_argument("--model", default=None)
    t.add_argument("--variant", default=None, help="Override candidate variant path")
    t.add_argument("--control", default=None, help="Override control variant path")
    t.add_argument("--repeat", type=int, default=None, help="Override recipe repetitions")
    t.add_argument("--dry-run", action="store_true", help="Print plan only; no API calls (scenario recipes still run canned)")
    t.set_defaults(func=cmd_test)

    g = sub.add_parser(
        "go",
        help="Plan and run evals from changed files / intent (compiler owns select/order/stop)",
    )
    g.add_argument(
        "variant",
        nargs="?",
        default=None,
        help="Candidate variant dir, variants/<name>, or analyze code (v3/vb/vc)",
    )
    g.add_argument(
        "-n",
        "--plan-only",
        action="store_true",
        help="Print coverage table, save plan, exit (no API calls)",
    )
    g.add_argument(
        "--files",
        default=None,
        help="Comma-separated changed-file override (e.g. scheduled_check_in.md)",
    )
    g.add_argument("--intent", default=None, help="Free-text intent; keyword match only")
    g.add_argument("--budget", type=float, default=None, help="USD cap; trims should_run only")
    g.add_argument(
        "--rank",
        action="store_true",
        help="Optional LLM ranking after the compiler (catalog still constrains; never a verdict)",
    )
    g.add_argument("--yes", "-y", action="store_true", help="Skip confirm and execute the plan")
    g.add_argument(
        "--skip-safety-i-know",
        action="store_true",
        help="Allow skipping safety must_run items (prints a loud warning)",
    )
    g.add_argument("--adapter", choices=["anthropic", "openai"], default=None)
    g.add_argument("--model", default=None)
    g.set_defaults(func=cmd_go)

    sm = sub.add_parser(
        "sim",
        help="Shift-seeded simulated guard: replay a shift to a wake, then let a simulated guard reply live",
    )
    sm.add_argument("shift", help="Shift id (e.g. 50737)")
    sm.add_argument("--from-turn", type=int, default=17, help="Mid-shift hand-off wake")
    sm.add_argument("--max-turns", type=int, default=4, help="Simulated turns after the seed")
    sm.add_argument("--repeat", type=int, default=1)
    sm.add_argument("--variant", default=None, help="Candidate variant dir (default variants/baseline)")
    sm.add_argument(
        "--pressure",
        choices=["faithful", "pushback", "hostile"],
        default="faithful",
        help="Bias layered on the deterministic profile (recorded with the run)",
    )
    sm.add_argument("--adapter", choices=["anthropic", "openai"], default=None)
    sm.add_argument("--model", default=None, help="Copilot model (simulator model must differ)")
    sm.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Canned copilot + canned guard; zero API calls",
    )
    sm.set_defaults(func=cmd_sim)

    a = sub.add_parser(
        "analyze",
        help="Agentic advisor: explain prompt diff / gates (scorers own pass/fail)",
    )
    a.add_argument("--diff", nargs=2, metavar=("BASELINE", "CANDIDATE"), default=None)
    a.add_argument("--control-variant", default=None)
    a.add_argument("--variant", default=None)
    a.add_argument("--control-run", default=None)
    a.add_argument("--variant-run", default=None)
    a.add_argument("--shift", default=None)
    a.add_argument("--focus-turns", type=int, nargs="*", default=None)
    a.add_argument("--adapter", choices=["anthropic", "openai"], default="openai")
    a.add_argument("--model", default="gpt-5.6-sol")
    a.add_argument("--no-llm", action="store_true", help="Facts + stub narrative only")
    a.set_defaults(func=cmd_analyze)

    m = sub.add_parser(
        "mine",
        help="Mine shifts/runs for failure modes with no recipe card (never writes the catalog)",
    )
    m.add_argument(
        "--dry",
        "--dry-run",
        "-n",
        dest="dry",
        action="store_true",
        help="Stage 1 sweep + gap report only; zero API calls",
    )
    m.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max flagged threads sent to the LLM (default 30); extras are logged",
    )
    m.add_argument(
        "--no-proposals",
        action="store_true",
        help="Skip writing experiments/proposals/*.json",
    )
    m.set_defaults(func=_cmd_mine)

    j = sub.add_parser(
        "judge",
        help="ADVISORY conduct checklist over a full transcript (never gates pass/fail)",
    )
    j.add_argument("run_id", nargs="?", default=None, help="Stored run to judge")
    j.add_argument(
        "--pair",
        nargs=2,
        metavar=("CONTROL_RUN", "VARIANT_RUN"),
        default=None,
        help="Pairwise advisory preference (order-swapped; reports position-bias)",
    )
    j.add_argument("--shift", default=None)
    j.add_argument("--adapter", choices=["anthropic", "openai"], default=None)
    j.add_argument("--model", default=None, help="Override judge.model (must differ from copilot)")
    j.set_defaults(func=cmd_judge)

    cal = sub.add_parser(
        "calibrate",
        help="ADVISORY: align conduct judge with gold labels (never a gate)",
    )
    cal.add_argument(
        "--gold",
        default=None,
        help="Path to experiments/gold/conduct_labels.json",
    )
    cal.add_argument("--adapter", choices=["anthropic", "openai"], default=None)
    cal.add_argument("--model", default=None)
    cal.set_defaults(func=cmd_calibrate)

    lab = sub.add_parser(
        "label",
        help="Human gold labels for the ADVISORY conduct checklist (never a gate)",
    )
    lab.add_argument("run_id", nargs="?", default=None, help="Stored run to label")
    lab.add_argument("--shift", default=None, help="Only this shift's transcript")
    lab.add_argument("--out", default=None, help="Gold file (default experiments/gold/conduct_labels.json)")
    lab.add_argument("--relabel", action="store_true", help="Redo transcripts already labeled")
    lab.add_argument("--export", metavar="RUN_ID", default=None, help="Write a Markdown worksheet")
    lab.add_argument("--to", default=None, help="Worksheet path for --export")
    lab.add_argument("--import", dest="import_path", default=None, help="Parse a filled worksheet")
    lab.add_argument("--status", action="store_true", help="Gold coverage vs the 20-40 target")
    lab.set_defaults(func=cmd_label)

    lp = sub.add_parser(
        "loop",
        help="Eval-loop agent: mine shift JSON → diagnose → patch → score (see LOOP.md)",
    )
    lp.add_argument("shift", help="Shift id (e.g. 50737). Dataset is shifts/<id>.json")
    lp.add_argument("--adapter", choices=["anthropic", "openai"], default="openai")
    lp.add_argument("--model", default="gpt-5.6-sol")
    lp.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print architecture plan only; no API and no Session A–D",
    )
    lp.set_defaults(func=cmd_loop)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
