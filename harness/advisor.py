"""Agentic eval advisor — narrative only; deterministic scorers own pass/fail."""

from __future__ import annotations

import difflib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.diagnose_escalation import compare_pair
from harness.evals import changed_decisions, summarize_turns
from harness.recipes import suggest_recipes_for_changed_files
from harness.store import ExperimentStore
from harness.verify_b import verification_stats

ROOT = Path(__file__).resolve().parents[1]

ADVISOR_SYSTEM = """You are the Calvis eval advisor for a security-guard copilot prompt harness.

Hard rules:
- Pass/fail is ALREADY decided by deterministic scorers in the facts JSON. Never invent metrics.
- Never overturn or contradict a scorer's pass/fail boolean.
- Explain what the prompt diff strengthens or weakens relative to Calvis core policies.
- Recommend which named recipes to run next (from suggested_recipes).
- If a gate failed, explain using the provided facts only.
- Recommend a concrete prompt refinement when useful (prefer ordered rules over stacked conflicting instructions).
- Be concise and operational. This is for a security company developer loop.
"""


def prompt_file_diff(a_dir: Path, b_dir: Path) -> tuple[list[str], str]:
    """Return (changed relative paths, unified diff text) for core/ and instructions/."""
    changed: list[str] = []
    chunks: list[str] = []
    for sub in ("core", "instructions"):
        left = a_dir / sub
        right = b_dir / sub
        names = sorted({p.name for p in left.glob("*.md")} | {p.name for p in right.glob("*.md")})
        for name in names:
            lp, rp = left / name, right / name
            lt = lp.read_text(encoding="utf-8") if lp.exists() else ""
            rt = rp.read_text(encoding="utf-8") if rp.exists() else ""
            if lt == rt:
                continue
            rel = f"{sub}/{name}"
            changed.append(rel)
            diff = difflib.unified_diff(
                lt.splitlines(),
                rt.splitlines(),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            chunks.append("\n".join(diff))
    return changed, "\n\n".join(chunks) if chunks else "(no differences in core/ or instructions/)"


def assemble_facts(
    *,
    control_dir: Path | None = None,
    variant_dir: Path | None = None,
    control_run: str | None = None,
    variant_run: str | None = None,
    shift: str | None = None,
    focus_turns: list[int] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    root = root or ROOT
    facts: dict[str, Any] = {
        "assembled_at": datetime.now(timezone.utc).isoformat(),
        "pass_fail_authority": "deterministic_scorers_only",
    }
    if control_dir and variant_dir:
        changed, diff = prompt_file_diff(control_dir, variant_dir)
        facts["prompt_diff"] = {
            "control": str(control_dir),
            "variant": str(variant_dir),
            "changed_files": changed,
            "unified_diff": diff[:12000],
        }
        facts["suggested_recipes"] = suggest_recipes_for_changed_files(changed)
    if control_run and variant_run and shift:
        store = ExperimentStore(root / "runs")
        c = store.load_turns(control_run, shift)
        v = store.load_turns(variant_run, shift)
        facts["runs"] = {
            "control": control_run,
            "variant": variant_run,
            "shift": shift,
            "control_summary": summarize_turns(c) if c else None,
            "variant_summary": summarize_turns(v) if v else None,
            "changed_decisions": changed_decisions(c, v) if c and v else [],
        }
        focus = focus_turns or [5, 6, 7, 8, 9]
        if c and v:
            esc = compare_pair(store, control_run, variant_run, shift, focus)
            facts["escalation_gate"] = {
                "focus_turns": focus,
                "pass": esc["safety_pass"],
                "missed_escalations": esc["failed_safety_assertions_missed_escalation"],
                "control_escalation_total": esc["control_escalation_total"],
                "variant_escalation_total": esc["variant_escalation_total"],
            }
            # welcome
            c1 = next((t for t in c if int(t.get("turn", -1)) == 1), None)
            v1 = next((t for t in v if int(t.get("turn", -1)) == 1), None)
            facts["welcome_gate"] = {
                "pass": bool(
                    c1
                    and v1
                    and c1.get("messages")
                    and v1.get("messages")
                    and c1.get("decision") == "send_message"
                    and v1.get("decision") == "send_message"
                ),
            }
            # verify_b style if guard messages present
            if any(t.get("trigger") == "guard_message" for t in c + v):
                cs, vs = verification_stats(c), verification_stats(v)
                facts["verify_b_gate"] = {
                    "control": cs,
                    "variant": vs,
                    "pass": (vs.get("reply_rate") or 0) >= 1.0
                    and (vs.get("escalations") or 0) <= (cs.get("escalations") or 0)
                    and (
                        vs.get("verification_rate") is None
                        or cs.get("verification_rate") is None
                        or vs["verification_rate"] >= cs["verification_rate"]
                    ),
                }
    return facts


def _llm_advise(facts: dict, adapter_name: str, model: str) -> str:
    user = (
        "Assemble an advisor report with these sections:\n"
        "1. Prompt change summary (strengthen/weaken/clarify)\n"
        "2. Recommended recipes to run\n"
        "3. Gate results explanation (use facts only; do not change pass/fail)\n"
        "4. Recommended next refinement (if any)\n\n"
        f"FACTS_JSON:\n{json.dumps(facts, indent=2)[:20000]}"
    )
    if adapter_name == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": ADVISOR_SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        if str(model).startswith("gpt-5"):
            kwargs["reasoning_effort"] = "none"
        else:
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    if adapter_name == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=ADVISOR_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "\n".join(parts).strip()
    raise ValueError(f"unknown adapter: {adapter_name}")


def run_advisor(
    *,
    control_variant: str | Path | None = None,
    variant: str | Path | None = None,
    control_run: str | None = None,
    variant_run: str | None = None,
    shift: str | None = None,
    focus_turns: list[int] | None = None,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    out_dir: Path | None = None,
    root: Path | None = None,
    skip_llm: bool = False,
) -> dict[str, Any]:
    root = root or ROOT

    def _resolve(p: str | Path | None) -> Path | None:
        if p is None:
            return None
        path = Path(p)
        if not path.is_absolute():
            path = root / path
        return path

    cdir, vdir = _resolve(control_variant), _resolve(variant)
    facts = assemble_facts(
        control_dir=cdir,
        variant_dir=vdir,
        control_run=control_run,
        variant_run=variant_run,
        shift=shift,
        focus_turns=focus_turns,
        root=root,
    )

    if skip_llm:
        narrative = (
            "## Advisor (deterministic stub; LLM skipped)\n\n"
            f"Changed files: {(facts.get('prompt_diff') or {}).get('changed_files')}\n\n"
            f"Suggested recipes: {facts.get('suggested_recipes')}\n\n"
            "Run `py cli.py test <recipe>` then re-analyze with --control-run/--variant-run.\n"
        )
    else:
        narrative = _llm_advise(facts, adapter, model)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = out_dir or (root / "runs" / f"advisor_{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "facts": facts,
        "narrative": narrative,
        "model": model if not skip_llm else None,
        "adapter": adapter if not skip_llm else None,
    }
    (out / "advisor_facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
    (out / "advisor_report.md").write_text(narrative + "\n", encoding="utf-8")
    (out / "advisor_bundle.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {"out_dir": str(out), **report}
