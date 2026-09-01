"""Deterministic eval-plan compiler plus an optional LLM ranker.

The compiler (`cx go`) owns {must_run, should_run, skip, order, budget_usd}.
`--rank` may propose a revised plan; the catalog and a deterministic validator
constrain it. The ranker never declares pass/fail — same rule as the advisor.
Scorers own pass/fail. No embeddings in the compiler path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
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


def _unquote_git_path(line: str) -> str:
    # git C-quotes paths containing backslashes or non-ASCII ("a\\b.md").
    name = line.strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return name


def _git_diff_name_only(a: Path, b: Path, *, cwd: Path) -> list[str] | None:
    attempts = [
        ["git", "diff", "--no-index", "--name-only", "--", a.as_posix(), b.as_posix()],
        ["git", "diff", "--name-only", "--", a.as_posix(), b.as_posix()],
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
        files = [_unquote_git_path(ln) for ln in proc.stdout.splitlines() if ln.strip()]
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
            name = _unquote_git_path(ln)
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


# --- optional LLM ranker ----------------------------------------------------
# The compiler still owns the plan shape. --rank may propose; the catalog and
# a deterministic validator constrain. The ranker never declares pass/fail.

CompleteFn = Callable[[str, str, str], str]

PLAN_CORE_FIELDS = ("must_run", "should_run", "skip", "order", "budget_usd")

PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["must_run", "should_run", "skip", "order", "budget_usd", "reasons"],
    "properties": {
        "must_run": {"type": "array", "items": {"type": "string"}},
        "should_run": {"type": "array", "items": {"type": "string"}},
        "skip": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "order": {"type": "array", "items": {"type": "string"}},
        "budget_usd": {"type": "number"},
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "action", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["drop", "pull", "reorder"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

RANKER_SYSTEM = """You are the Calvis eval plan ranker for a security-guard copilot harness.

Hard rules:
- You NEVER declare pass/fail. You never produce a verdict. Deterministic scorers own gates.
- Return JSON only matching the supplied schema. No prose, no markdown.
- You may only: reorder should_run; drop should_run items (action=drop + reason);
  pull a skipped catalog recipe into should_run (action=pull + reason).
- Do not drop must_run. Do not invent recipe ids. Do not exceed the budget cap.
- smoke-welcome stays first in order whenever it is in the catalog.
- Keep must_run exactly as the compiler provided.
- skip is an array of {id, reason} objects, not bare strings.
"""


def router_defaults(data: dict | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Ranker model from recipes.json defaults (not the copilot model)."""
    data = data if data is not None else load_recipes(path)
    d = data.get("defaults") or {}
    nested = d.get("router") if isinstance(d.get("router"), dict) else {}
    model = nested.get("model") or d.get("router.model") or "gpt-4.1-mini"
    adapter = nested.get("adapter") or d.get("router.adapter") or d.get("adapter") or "openai"
    return {"model": str(model), "adapter": str(adapter)}


def recipe_cards_for_ranker(data: dict | None = None, *, path: Path | None = None) -> list[dict[str, Any]]:
    data = data if data is not None else load_recipes(path)
    cards = []
    for name, recipe in (data.get("recipes") or {}).items():
        card = dict(recipe.get("card") or {})
        cards.append(
            {
                "id": name,
                "description": recipe.get("description", ""),
                "scorer": recipe.get("scorer"),
                "suggested_when": recipe.get("suggested_when") or [],
                "card": card,
            }
        )
    return cards


def _as_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if not isinstance(x, dict)]


def _normalize_skip(val: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(val, list):
        return rows
    for item in val:
        if isinstance(item, dict) and item.get("id"):
            rows.append({"id": str(item["id"]), "reason": str(item.get("reason") or "")})
        elif isinstance(item, str) and item:
            rows.append({"id": item, "reason": ""})
    return rows


def _skip_ids(skip: Any) -> list[str]:
    return [row["id"] for row in _normalize_skip(skip)]


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _est_sum(ids: Iterable[str], cards: dict[str, dict]) -> float:
    total = 0.0
    for rid in ids:
        total += float((cards.get(rid) or {}).get("est_usd") or 0)
    return total


def plan_core(plan: dict) -> dict[str, Any]:
    """Public plan fields the ranker must match."""
    return {
        "must_run": list(plan.get("must_run") or []),
        "should_run": list(plan.get("should_run") or []),
        "skip": _normalize_skip(plan.get("skip")),
        "order": list(plan.get("order") or []),
        "budget_usd": float(plan.get("budget_usd") or 0),
    }


def diff_summary_for_ranker(
    changed_files: list[str],
    *,
    variant_dir: Path | None = None,
    baseline_dir: Path | None = None,
    excerpt_chars: int = 4000,
) -> dict[str, Any]:
    excerpt = ""
    if variant_dir is not None:
        try:
            from harness.advisor import prompt_file_diff

            base = baseline_dir or (ROOT / "variants" / "baseline")
            files, diff = prompt_file_diff(Path(base), Path(variant_dir))
            changed_files = list(files) if files else list(changed_files)
            excerpt = diff if len(diff) <= excerpt_chars else diff[:excerpt_chars] + "\n… [truncated]"
        except Exception:
            excerpt = ""
    return {"filenames": list(changed_files), "unified_diff_excerpt": excerpt}


def validate_ranked_plan(
    ranked: dict,
    compiler: dict,
    *,
    catalog: list[str],
    cards: dict[str, dict],
    budget_cap: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic post-validation. Agent proposes; catalog constrains.

    Corrections:
      (1) any dropped must_run is re-inserted
      (2) any recipe id not in the catalog is removed
      (3) budget cap re-applied
      (4) smoke-welcome stays first
    """
    catalog = list(catalog)
    catalog_set = set(catalog)
    cap = budget_cap if budget_cap is not None else compiler.get("budget_cap")
    corrections: list[str] = []

    must_run = _as_str_list(ranked.get("must_run"))
    should_run = _as_str_list(ranked.get("should_run"))
    # allow skip ids mixed into should_run if the model used bare strings
    if isinstance(ranked.get("should_run"), list):
        should_run = []
        for x in ranked.get("should_run") or []:
            if isinstance(x, dict) and x.get("id"):
                should_run.append(str(x["id"]))
            elif not isinstance(x, dict):
                should_run.append(str(x))
    skip_rows = _normalize_skip(ranked.get("skip"))
    order = _as_str_list(ranked.get("order"))
    if not order:
        # order may have been sent with dicts; ignore
        order = [str(x) for x in (ranked.get("order") or []) if isinstance(x, str)]
    reasons = ranked.get("reasons") if isinstance(ranked.get("reasons"), list) else []

    def _strip_unknown(seq: list[str], field: str) -> list[str]:
        keep, dropped = [], []
        for x in seq:
            if x in catalog_set:
                keep.append(x)
            else:
                dropped.append(x)
        if dropped:
            corrections.append(f"removed unknown recipe id(s) from {field}: {dropped}")
        return keep

    # (2) unknown ids
    must_run = _strip_unknown(must_run, "must_run")
    should_run = _strip_unknown(should_run, "should_run")
    skip_unknown = [r for r in skip_rows if r["id"] not in catalog_set]
    if skip_unknown:
        corrections.append(
            f"removed unknown recipe id(s) from skip: {[r['id'] for r in skip_unknown]}"
        )
        skip_rows = [r for r in skip_rows if r["id"] in catalog_set]
    order = _strip_unknown(order, "order")

    compiler_must = [x for x in _as_str_list(compiler.get("must_run")) if x in catalog_set]
    if SMOKE_ID in catalog_set and SMOKE_ID not in compiler_must:
        compiler_must = [SMOKE_ID] + compiler_must

    extra_must = [x for x in must_run if x not in compiler_must]
    if extra_must:
        corrections.append(f"moved unauthorized must_run into should_run: {extra_must}")
        for x in extra_must:
            if x not in should_run:
                should_run.append(x)

    # (1) re-insert dropped must_run
    dropped_must = [rid for rid in compiler_must if rid not in must_run]
    for rid in dropped_must:
        corrections.append(f"re-inserted dropped must_run: {rid}")
    must_run = list(compiler_must)

    should_run = _dedupe([x for x in should_run if x not in must_run and x in catalog_set])
    ranked_should_order = [x for x in order if x in should_run]
    rest_should = [x for x in should_run if x not in ranked_should_order]
    should_run = _dedupe(ranked_should_order + rest_should)

    ranked_order_for_first = list(order) if order else list(must_run + should_run)
    # (4) log if the ranker did not keep smoke-welcome first
    if SMOKE_ID in catalog_set and SMOKE_ID in must_run:
        if not ranked_order_for_first or ranked_order_for_first[0] != SMOKE_ID:
            corrections.append("moved smoke-welcome to first in order")

    # (3) budget cap: never trim must_run; overflow should_run → skip
    skip_reasons = {r["id"]: r["reason"] for r in skip_rows}
    before_should = list(should_run)
    if cap is not None:
        budget_f = float(cap)
        must_cost = _est_sum(must_run, cards)
        kept = list(should_run)
        while kept and must_cost + _est_sum(kept, cards) - 1e-12 > budget_f:
            drop = _sort_ids(kept, cards)[-1]
            kept.remove(drop)
            skip_reasons[drop] = (
                f"trimmed by budget cap (cap={budget_f}, "
                f"est={float((cards.get(drop) or {}).get('est_usd') or 0)})"
            )
        trimmed = [x for x in before_should if x not in kept]
        if trimmed:
            corrections.append(
                f"re-applied budget cap {cap}; trimmed from should_run/order: {trimmed}"
            )
        should_run = kept

    skip_ids = [rid for rid in catalog if rid not in must_run and rid not in should_run]
    skip_rows = [
        {"id": rid, "reason": skip_reasons.get(rid) or "not selected"}
        for rid in skip_ids
    ]

    order = order_plan(must_run + should_run, cards)
    if SMOKE_ID in catalog_set and SMOKE_ID in must_run:
        order = [SMOKE_ID] + [x for x in order if x != SMOKE_ID]
        must_run = [SMOKE_ID] + [x for x in must_run if x != SMOKE_ID]

    plan = {
        "must_run": must_run,
        "should_run": should_run,
        "skip": skip_rows,
        "order": order,
        "budget_usd": round(_est_sum(order, cards), 4),
        "budget_cap": cap,
        "stop_rules": list(compiler.get("stop_rules") or STOP_RULES),
        "changed_files": list(compiler.get("changed_files") or []),
        "intent": compiler.get("intent"),
        "warnings": list(compiler.get("warnings") or []),
        "reasons": reasons,
        "source": "ranker",
    }
    return plan, corrections


def parse_plan_json(text: str) -> dict:
    """Reject prose. The whole payload must be a JSON object with the plan keys."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty ranker output")
    if raw.startswith("```") or not raw.startswith("{") or not raw.endswith("}"):
        raise ValueError("ranker output is not raw JSON")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("plan must be a JSON object")
    missing = [k for k in ("must_run", "should_run", "skip", "order") if k not in data]
    if missing:
        raise ValueError(f"missing plan keys: {missing}")
    return data


def _openai_rank(system: str, user: str, model: str, *, strict: bool = True) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if strict:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "eval_plan",
                "strict": True,
                "schema": PLAN_JSON_SCHEMA,
            },
        }
    else:
        kwargs["response_format"] = {"type": "json_object"}
    if str(model).startswith("gpt-5"):
        kwargs["reasoning_effort"] = "none"
    else:
        kwargs["temperature"] = 0
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _anthropic_rank(system: str, user: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _ranker_user_payload(
    *,
    cards: list[dict],
    diff: dict,
    intent: str | None,
    compiler: dict,
) -> str:
    body = {
        "recipe_cards": cards,
        "diff_summary": diff,
        "intent": intent or "",
        "compiler_plan": plan_core(compiler),
        "budget_cap": compiler.get("budget_cap"),
        "allowed_ops": [
            "reorder should_run",
            "drop should_run items with a reason",
            "pull a skipped card into should_run with a reason",
        ],
        "schema": PLAN_JSON_SCHEMA,
    }
    return (
        "Revise the compiler plan if warranted. JSON only.\n"
        + json.dumps(body, indent=2)[:24000]
    )


@dataclass
class RankResult:
    compiler_plan: dict
    ranked_plan: dict
    corrections: list[str] = field(default_factory=list)
    fallback: bool = False
    fallback_reason: str | None = None
    ranker_model: str | None = None


def rank_plan(
    compiler: dict,
    *,
    recipes_data: dict | None = None,
    recipes_path: Path | None = None,
    changed_files: list[str] | None = None,
    intent: str | None = None,
    variant_dir: Path | None = None,
    model: str | None = None,
    adapter: str | None = None,
    complete_fn: CompleteFn | None = None,
) -> RankResult:
    """Call the ranker, parse JSON (one retry), validate, or fall back silently."""
    data = recipes_data if recipes_data is not None else load_recipes(recipes_path)
    defaults = router_defaults(data)
    model = model or defaults["model"]
    adapter = adapter or defaults["adapter"]
    recipes = dict(data.get("recipes") or {})
    catalog = list(recipes.keys())
    cards = {rid: dict(rec.get("card") or {}) for rid, rec in recipes.items()}
    diff = diff_summary_for_ranker(
        list(changed_files if changed_files is not None else compiler.get("changed_files") or []),
        variant_dir=variant_dir,
    )
    user = _ranker_user_payload(
        cards=recipe_cards_for_ranker(data),
        diff=diff,
        intent=intent if intent is not None else compiler.get("intent"),
        compiler=compiler,
    )

    def _call(prompt: str, attempt: int) -> str:
        if complete_fn is not None:
            return complete_fn(RANKER_SYSTEM, prompt, model)
        if adapter == "anthropic":
            return _anthropic_rank(RANKER_SYSTEM, prompt, model)
        return _openai_rank(RANKER_SYSTEM, prompt, model, strict=(attempt == 0))

    last_err: str | None = None
    parsed: dict | None = None
    for attempt in range(2):
        prompt = user
        if attempt == 1:
            prompt = (
                "Your previous output was not valid JSON matching the plan schema. "
                "Return only a JSON object. No prose.\n\n" + user
            )
        try:
            raw = _call(prompt, attempt)
            parsed = parse_plan_json(raw)
            break
        except Exception as exc:  # noqa: BLE001 — any LLM/parse failure falls back
            last_err = f"{type(exc).__name__}: {exc}"
            parsed = None

    if parsed is None:
        fallback_plan = dict(compiler)
        fallback_plan["source"] = "ranker_fallback"
        fallback_plan["reasons"] = []
        return RankResult(
            compiler_plan=compiler,
            ranked_plan=fallback_plan,
            corrections=[],
            fallback=True,
            fallback_reason=last_err or "unknown ranker error",
            ranker_model=model,
        )

    validated, corrections = validate_ranked_plan(
        parsed,
        compiler,
        catalog=catalog,
        cards=cards,
        budget_cap=compiler.get("budget_cap"),
    )
    return RankResult(
        compiler_plan=compiler,
        ranked_plan=validated,
        corrections=corrections,
        fallback=False,
        fallback_reason=None,
        ranker_model=model,
    )


def plan_diff_text(compiler: dict, ranked: dict) -> str:
    """Human-readable diff of the two plans. Not a verdict."""
    left, right = plan_core(compiler), plan_core(ranked)
    lines = ["plan diff (compiler → ranked):"]
    changed = False
    for key in ("must_run", "should_run", "order", "budget_usd"):
        a, b = left.get(key), right.get(key)
        if a == b:
            lines.append(f"  {key}: (unchanged) {a!r}")
        else:
            changed = True
            lines.append(f"  {key}: {a!r} → {b!r}")
    a_skip, b_skip = _skip_ids(left.get("skip")), _skip_ids(right.get("skip"))
    if a_skip == b_skip:
        lines.append(f"  skip: (unchanged) {a_skip!r}")
    else:
        changed = True
        lines.append(f"  skip: {a_skip!r} → {b_skip!r}")
    reasons = ranked.get("reasons") or []
    if reasons:
        lines.append("  ranker reasons:")
        for r in reasons:
            if isinstance(r, dict):
                lines.append(
                    f"    - {r.get('action', '?')} {r.get('id', '?')}: {r.get('reason', '')}"
                )
            else:
                lines.append(f"    - {r}")
    if not changed:
        lines.append("  (no field changes)")
    return "\n".join(lines)


def format_ranked_go_output(
    compiler: dict,
    ranked: RankResult,
) -> str:
    chunks = [
        "=== compiler plan ===",
        format_plan_table(compiler),
        "",
        "=== ranked plan ===",
        format_plan_table(ranked.ranked_plan),
        "",
        "=== plan diff ===",
        plan_diff_text(compiler, ranked.ranked_plan),
    ]
    if ranked.fallback:
        chunks.append("")
        chunks.append(f"note: ranker fallback to compiler plan ({ranked.fallback_reason})")
    if ranked.corrections:
        chunks.append("")
        chunks.append("ranker validator corrections:")
        for c in ranked.corrections:
            chunks.append(f"  - {c}")
    if ranked.ranker_model:
        chunks.append("")
        chunks.append(f"ranker model: {ranked.ranker_model} (not a verdict)")
    return "\n".join(chunks) + "\n"

