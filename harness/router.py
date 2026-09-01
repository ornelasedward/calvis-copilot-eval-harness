"""Deterministic eval-plan compiler.

Selects, orders, and (at execute time) stops recipes. Scorers own pass/fail.
No embeddings, no LLM, no network.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from harness.recipes import load_recipes, resolve_analyze_target

ROOT = Path(__file__).resolve().parents[1]
SMOKE_ID = "smoke-welcome"
STOP_RULES = [
    "smoke fail -> halt",
    "safety fail -> no lift recipes",
]
COST_RANK = {
    "turn-cheap": 0,
    "multi-turn": 1,
    "full-shift": 2,
}
SKIP_SAFETY_BANNER = (
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    "WARNING: --skip-safety-i-know is set.\n"
    "Safety recipes will NOT be required. Scorers will not gate them.\n"
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
)


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens. Underscores/hyphens split."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t}


def _basenames(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        norm = str(p).replace("\\", "/").strip().lower()
        if not norm:
            continue
        out.add(Path(norm).name)
        out.add(norm)
    return out


def files_match(required_files: list[str], changed_files: list[str]) -> bool:
    if not required_files or not changed_files:
        return False
    changed = _basenames(changed_files)
    for req in required_files:
        r = str(req).replace("\\", "/").strip().lower()
        if not r:
            continue
        base = Path(r).name
        if base in changed or r in changed:
            return True
        if any(c.endswith("/" + r) or c.endswith(r) or r in c for c in changed):
            return True
    return False


def _token_haystack(items: Iterable[str]) -> set[str]:
    hay: set[str] = set()
    for item in items:
        if not item:
            continue
        s = str(item).lower()
        hay.add(s)
        hay |= tokenize(s)
    return hay


def intent_overlap(needles: Iterable[str], haystack_items: Iterable[str]) -> bool:
    q = _token_haystack(needles)
    if not q:
        return False
    return bool(q & _token_haystack(haystack_items))


def _card(recipe: dict) -> dict:
    return dict(recipe.get("card") or {})


def coverage_tokens(card: dict) -> set[str]:
    items = list(card.get("intent") or []) + list(card.get("covers") or [])
    return _token_haystack(items)


def _required_when(card: dict) -> dict:
    rw = card.get("required_when") or {}
    return {
        "files": list(rw.get("files") or []),
        "intents": list(rw.get("intents") or []),
    }


def required_when_match(card: dict, changed_files: list[str], intent_text: str | None) -> bool:
    rw = _required_when(card)
    if files_match(rw["files"], changed_files):
        return True
    if intent_text and intent_overlap([intent_text], rw["intents"]):
        return True
    return False


def keyword_match(card: dict, intent_text: str | None) -> bool:
    """`--intent` matched by keyword against card intent and covers."""
    if not intent_text or not str(intent_text).strip():
        return False
    items = list(card.get("intent") or []) + list(card.get("covers") or [])
    return intent_overlap([intent_text], items)


def _cost_key(rid: str, card: dict) -> tuple:
    cost = card.get("cost") or "full-shift"
    est = float(card.get("est_usd") or 0)
    return (COST_RANK.get(str(cost), 99), est, rid)


def _sort_ids(ids: Iterable[str], cards: dict[str, dict]) -> list[str]:
    return sorted(ids, key=lambda rid: _cost_key(rid, cards.get(rid) or {}))


def order_plan(ids: list[str], cards: dict[str, dict]) -> list[str]:
    """smoke-welcome first, then cheap -> expensive, honoring `after` deps."""
    remaining = set(ids)
    ordered: list[str] = []
    if SMOKE_ID in remaining:
        ordered.append(SMOKE_ID)
        remaining.remove(SMOKE_ID)
    id_set = set(ids)
    while remaining:
        ready: list[str] = []
        for rid in remaining:
            after = list((cards.get(rid) or {}).get("after") or [])
            deps = [d for d in after if d in id_set]
            if all(d in ordered for d in deps):
                ready.append(rid)
        if not ready:
            ready = list(remaining)
        ready.sort(key=lambda r: _cost_key(r, cards.get(r) or {}))
        pick = ready[0]
        ordered.append(pick)
        remaining.remove(pick)
    return ordered


def greedy_set_cover(
    candidates: list[str],
    cards: dict[str, dict],
    uncovered: set[str],
) -> tuple[list[str], list[str]]:
    """Cheapest-first greedy cover. Returns (selected, redundant)."""
    selected: list[str] = []
    redundant: list[str] = []
    remaining = set(uncovered)
    for rid in _sort_ids(candidates, cards):
        extra = coverage_tokens(cards.get(rid) or {}) & remaining
        if extra:
            selected.append(rid)
            remaining -= extra
        else:
            redundant.append(rid)
    return selected, redundant


def discover_changed_files(
    variant_dir: Path | None = None,
    *,
    baseline_dir: Path | None = None,
    files_override: list[str] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Changed prompt files for the planner.

    Preference: explicit `--files` override, then `git diff --name-only`
    between the candidate variant and variants/baseline, then a content
    diff if git is unavailable. With no variant, uses the working-tree diff.
    """
    if files_override is not None:
        return [f for f in files_override if str(f).strip()]

    root = root or ROOT
    baseline_dir = baseline_dir or (root / "variants" / "baseline")

    if variant_dir is not None:
        git_files = _git_diff_name_only(baseline_dir, variant_dir, cwd=root)
        if git_files is not None:
            return git_files
        return _content_diff_files(baseline_dir, variant_dir)

    wt = _git_working_tree_names(cwd=root)
    if wt is not None:
        return wt
    return []


def _git_diff_name_only(a: Path, b: Path, *, cwd: Path) -> list[str] | None:
    attempts = [
        ["git", "diff", "--no-index", "--name-only", "--", str(a), str(b)],
        ["git", "diff", "--name-only", "--", str(a), str(b)],
    ]
    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        except OSError:
            return None
        # --no-index exits 1 when files differ, 0 when identical.
        if proc.returncode not in (0, 1):
            continue
        files = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return files
    return None


def _git_working_tree_names(*, cwd: Path) -> list[str] | None:
    files: list[str] = []
    seen: set[str] = set()
    for cmd in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
    ):
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), capture_output=True, text=True, check=False
            )
        except (FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        for ln in proc.stdout.splitlines():
            name = ln.strip()
            if name and name not in seen:
                seen.add(name)
                files.append(name)
    return files


def _content_diff_files(baseline_dir: Path, variant_dir: Path) -> list[str]:
    try:
        from harness.advisor import prompt_file_diff

        changed, _ = prompt_file_diff(Path(baseline_dir), Path(variant_dir))
        return list(changed)
    except Exception:
        changed: list[str] = []
        for sub in ("core", "instructions"):
            left, right = Path(baseline_dir) / sub, Path(variant_dir) / sub
            names = set()
            if left.exists():
                names |= {p.name for p in left.glob("*.md")}
            if right.exists():
                names |= {p.name for p in right.glob("*.md")}
            for name in sorted(names):
                lp, rp = left / name, right / name
                lt = lp.read_text(encoding="utf-8") if lp.exists() else ""
                rt = rp.read_text(encoding="utf-8") if rp.exists() else ""
                if lt != rt:
                    changed.append(f"{sub}/{name}")
        return changed


def resolve_variant_dir(spec: str | None, *, root: Path | None = None) -> Path | None:
    if not spec:
        return None
    root = root or ROOT
    path_spec = spec
    try:
        path_spec = resolve_analyze_target(spec)
    except KeyError:
        path_spec = spec
    candidates = [
        Path(path_spec),
        root / path_spec,
        root / "variants" / spec,
    ]
    for p in candidates:
        if (p / "core").exists():
            return p.resolve()
    raise SystemExit(f"unknown variant: {spec}")


def compile_plan(
    *,
    changed_files: list[str] | None = None,
    intent: str | None = None,
    budget: float | None = None,
    skip_safety_i_know: bool = False,
    recipes_data: dict | None = None,
    recipes_path: Path | None = None,
) -> dict[str, Any]:
    """Pure planner. Returns the plan dict; never calls APIs or scorers."""
    data = recipes_data if recipes_data is not None else load_recipes(recipes_path)
    recipes: dict[str, dict] = dict(data.get("recipes") or {})
    changed_files = list(changed_files or [])
    intent_text = intent.strip() if isinstance(intent, str) and intent.strip() else None

    cards: dict[str, dict] = {}
    for rid, rec in recipes.items():
        cards[rid] = _card(rec)

    warnings: list[str] = []
    skip: list[dict[str, str]] = []
    skip_ids: set[str] = set()

    def add_skip(rid: str, reason: str) -> None:
        if rid in skip_ids:
            return
        skip_ids.add(rid)
        skip.append({"id": rid, "reason": reason})

    if skip_safety_i_know:
        warnings.append(SKIP_SAFETY_BANNER)

    matching: list[str] = []
    safety_required: list[str] = []

    for rid, rec in recipes.items():
        card = cards.get(rid) or {}
        if not card:
            add_skip(rid, "missing card metadata")
            continue
        rw_hit = required_when_match(card, changed_files, intent_text)
        kw_hit = keyword_match(card, intent_text)
        is_match = rid == SMOKE_ID or rw_hit or kw_hit
        if not is_match:
            add_skip(
                rid,
                "required_when files/intents do not match changed files or intent",
            )
            continue
        matching.append(rid)
        if card.get("risk_class") == "safety" and rw_hit:
            safety_required.append(rid)

    must_run: list[str] = []
    if SMOKE_ID in recipes and SMOKE_ID not in skip_ids:
        must_run.append(SMOKE_ID)

    for rid in safety_required:
        if rid in must_run:
            continue
        if skip_safety_i_know:
            add_skip(rid, "skipped by --skip-safety-i-know")
            matching = [m for m in matching if m != rid]
            continue
        must_run.append(rid)

    must_set = set(must_run)

    universe: set[str] = set()
    for rid in matching:
        universe |= coverage_tokens(cards.get(rid) or {})
    if intent_text:
        universe |= tokenize(intent_text)

    covered: set[str] = set()
    for rid in must_run:
        covered |= coverage_tokens(cards.get(rid) or {})

    remaining_candidates = [rid for rid in matching if rid not in must_set]
    uncovered = universe - covered
    should_run, redundant = greedy_set_cover(remaining_candidates, cards, uncovered)
    for rid in redundant:
        add_skip(
            rid,
            "redundant coverage; already covered by must_run or cheaper recipes",
        )

    def est_sum(ids: Iterable[str]) -> float:
        total = 0.0
        for rid in ids:
            total += float((cards.get(rid) or {}).get("est_usd") or 0)
        return total

    must_cost = est_sum(must_run)
    if budget is not None:
        budget_f = float(budget)
        if must_cost - 1e-12 > budget_f:
            warnings.append(
                f"WARNING: must_run est ${must_cost:.2f} exceeds --budget ${budget_f:.2f}; "
                "keeping must_run intact"
            )
        # Trim should_run from the expensive end; never must_run.
        kept_sorted = _sort_ids(should_run, cards)
        while kept_sorted and must_cost + est_sum(kept_sorted) - 1e-12 > budget_f:
            drop = kept_sorted.pop()
            add_skip(
                drop,
                f"trimmed by --budget (cap={budget_f}, est={float((cards.get(drop) or {}).get('est_usd') or 0)})",
            )
        should_run = kept_sorted

    should_run = [rid for rid in should_run if rid not in skip_ids]
    must_run = [rid for rid in must_run if rid not in skip_ids]
    # Stable presentation: smoke first, then cheap -> expensive.
    must_run = order_plan(must_run, cards)
    should_run = _sort_ids(should_run, cards)
    order = order_plan(must_run + should_run, cards)

    # Any recipe not in must/should/skip (shouldn't happen) gets a skip reason.
    for rid in recipes:
        if rid not in must_run and rid not in should_run and rid not in skip_ids:
            add_skip(rid, "not selected")

    skip.sort(key=lambda row: row["id"])
    budget_usd = round(est_sum(order), 4)

    return {
        "must_run": must_run,
        "should_run": should_run,
        "skip": skip,
        "order": order,
        "budget_usd": budget_usd,
        "stop_rules": list(STOP_RULES),
        "changed_files": list(changed_files),
        "intent": intent_text,
        "warnings": warnings,
        "budget_cap": budget,
    }


def format_plan_table(plan: dict[str, Any]) -> str:
    files = plan.get("changed_files") or []
    files_s = ", ".join(str(f) for f in files) if files else "(none)"
    must = plan.get("must_run") or []
    should = plan.get("should_run") or []
    skip = plan.get("skip") or []
    must_s = ", ".join(must) if must else "(none)"
    should_s = ", ".join(should) if should else "(none)"
    lines = [
        "=== eval plan ===",
        f"Touched files : {files_s}",
        f"Must          : {must_s}",
        f"Should        : {should_s}",
    ]
    if not skip:
        lines.append("Skip          : (none)")
    else:
        first = skip[0]
        lines.append(f"Skip          : {first['id']} — {first['reason']}")
        for row in skip[1:]:
            lines.append(f"                {row['id']} — {row['reason']}")
    budget = float(plan.get("budget_usd") or 0)
    lines.append(f"Est budget    : ${budget:.2f}")
    order = plan.get("order") or []
    if order:
        lines.append(f"Order         : {' -> '.join(order)}")
    intent = plan.get("intent")
    if intent:
        lines.append(f"Intent        : {intent}")
    cap = plan.get("budget_cap")
    if cap is not None:
        lines.append(f"Budget cap    : ${float(cap):.2f}")
    return "\n".join(lines)


def save_plan(plan: dict[str, Any], runs_dir: Path | None = None) -> Path:
    runs_dir = Path(runs_dir) if runs_dir is not None else ROOT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = runs_dir / f"plan_{stamp}.json"
    # Keep a stable core shape; extras are fine for replay.
    payload = dict(plan)
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def execute_plan(
    plan: dict[str, Any],
    *,
    execute_fn: Callable[[str], dict[str, Any]],
    recipes_data: dict | None = None,
    recipes_path: Path | None = None,
) -> dict[str, Any]:
    """Run `order` via execute_fn. Stop rules only; scorers own verdicts."""
    data = recipes_data if recipes_data is not None else load_recipes(recipes_path)
    recipes: dict[str, dict] = dict(data.get("recipes") or {})

    def risk(rid: str) -> str:
        return str(((recipes.get(rid) or {}).get("card") or {}).get("risk_class") or "")

    results: list[dict[str, Any]] = []
    skipped_remaining: list[dict[str, str]] = []
    halted = False
    halt_reason: str | None = None
    safety_failed = False
    seen: set[str] = set()
    order = list(plan.get("order") or [])

    for rid in order:
        if safety_failed and risk(rid) == "lift":
            skipped_remaining.append(
                {"id": rid, "reason": "safety fail -> no lift recipes"}
            )
            seen.add(rid)
            print(f"skip {rid}: safety fail -> no lift recipes")
            continue
        print(f"=== go: execute {rid} ===")
        result = execute_fn(rid)
        passed = result.get("pass")
        results.append({"id": rid, "pass": passed, "result": result})
        seen.add(rid)
        if rid == SMOKE_ID and passed is False:
            halted = True
            halt_reason = "smoke fail -> halt"
            print("stop: smoke-welcome failed -> halt")
            for rest in order:
                if rest not in seen:
                    skipped_remaining.append({"id": rest, "reason": "smoke fail -> halt"})
                    seen.add(rest)
            break
        if risk(rid) == "safety" and passed is False:
            safety_failed = True
            print("stop_rule: safety fail -> no further lift recipes")

    return {
        "results": results,
        "halted": halted,
        "halt_reason": halt_reason,
        "skipped_remaining": skipped_remaining,
        "safety_failed": safety_failed,
    }


def confirm_execute(*, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "refusing to execute without --yes (stdin is not a TTY). "
            "Re-run with --yes or -n.",
            file=sys.stderr,
        )
        return False
    try:
        ans = input("Execute this plan? [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")
