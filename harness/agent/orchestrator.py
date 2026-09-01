"""Session E: wire miner → diagnose → patch → evaluate → decide.

Dry-run is implemented now (no API, no stubs that would fail the CLI).
Live run waits on Sessions A–D.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.agent.catalog import CLASS_CATALOG, SESSION_MAP
from harness.agent.policy import decide
from harness.agent.types import LoopConfig, LoopDecision

ROOT = Path(__file__).resolve().parents[2]


def loop_plan(config: LoopConfig) -> dict[str, Any]:
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
        ],
        "see": "LOOP.md",
    }


def write_plan(plan: dict[str, Any], root: Path | None = None) -> Path:
    root = root or ROOT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = root / "runs" / f"loop_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )
    return out


def run_loop(config: LoopConfig, root: Path | None = None) -> dict[str, Any]:
    plan = loop_plan(config)
    out_dir = write_plan(plan, root)
    result: dict[str, Any] = {"plan": plan, "out_dir": str(out_dir), "decision": None}

    if config.dry_run:
        return result

    from harness.agent.diagnose import diagnose
    from harness.agent.evaluate import evaluate_diagnosis
    from harness.agent.mine import mine_shift
    from harness.agent.patch import apply_patch

    cards = mine_shift(config.shift_id)
    for card in cards:
        card.validate()
    diagnosis = diagnose(
        cards,
        skip_llm=False,
        adapter=config.adapter,
        model=config.model,
    )
    patch = apply_patch(diagnosis, parent_variant=config.control_variant, skip_llm=False)
    score = evaluate_diagnosis(
        diagnosis,
        patch,
        control_variant=config.control_variant,
        adapter=config.adapter,
        model=config.model,
        dry_run=False,
    )
    decision: LoopDecision = decide(score, iteration=1, max_iterations=config.max_iterations)
    result["decision"] = decision.to_dict()
    (Path(out_dir) / "decision.json").write_text(
        json.dumps(result["decision"], indent=2) + "\n", encoding="utf-8"
    )
    return result
