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
    from harness.adapters.anthropic import AnthropicAdapter
    from harness.adapters.openai import OpenAIAdapter

    variant_dir = Path(args.variant)
    if not variant_dir.is_absolute():
        variant_dir = ROOT / variant_dir
    if not (variant_dir / "core").exists():
        sys.exit(f"variant missing core/: {variant_dir}")

    if args.adapter == "anthropic":
        adapter: object = AnthropicAdapter(model=args.model)
    elif args.adapter == "openai":
        adapter = OpenAIAdapter(model=args.model)
    else:
        sys.exit(f"unknown adapter: {args.adapter}")

    model_params: dict = {"temperature": 0}
    if args.adapter == "openai" and str(args.model).startswith("gpt-5"):
        # Chat Completions + tools requires reasoning_effort=none on gpt-5.6-sol.
        model_params = {"reasoning_effort": "none"}

    store = ExperimentStore(ROOT / "runs")
    run_id = args.run_id or new_run_id()
    manifest = RunManifest(
        run_id=run_id,
        variant_name=variant_dir.name,
        prompt_hash=prompt_dir_hash(variant_dir),
        model=args.model,
        model_params=model_params,
        adapter=args.adapter,
        data_version=_data_version(),
        code_version=_code_version(),
        mode=args.mode,
        shifts=args.shifts,
        repetitions=args.repeat,
        created_at=datetime.now(timezone.utc).isoformat(),
        tool_fixture_mode="exact_or_unavailable",
    )
    store.create_run(manifest)

    grand = {"turns": 0, "cost_usd": 0.0}
    for sid in args.shifts:
        shift = load_shift(ROOT / "shifts" / f"{sid}.json")
        engine = ReplayEngine(
            shift=shift,
            adapter=adapter,  # type: ignore[arg-type]
            config=EngineConfig(
                variant_dir=variant_dir,
                mode=args.mode,
                allow_empty_obligations=args.allow_empty_obligations,
                model_params=model_params,
            ),
            run_id=run_id,
        )
        schedule = build_schedule(shift)
        if args.max_turns:
            schedule = schedule[: args.max_turns]
        if args.turns:
            want = set(args.turns)
            schedule = [w for w in schedule if w.turn in want]

        for wake in schedule:
            if wake.skipped:
                continue
            result = engine.run_turn(wake.turn, wake.trigger, wake.ts)
            store.append_turn(run_id, sid, result)
            store.write_raw_trace(run_id, sid, [{
                "turn": wake.turn,
                "trigger": wake.trigger,
                "selected_instruction": result.selected_instruction,
                "events": result.raw_events,
            }])
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

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
