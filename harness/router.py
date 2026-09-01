"""Deterministic eval compiler plus an optional LLM ranker.

The compiler (`cx go`) owns {must_run, should_run, skip, order, budget}.
`--rank` may propose a revised plan; the catalog and a deterministic validator
constrain it. The ranker never declares pass/fail — same rule as the advisor.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.advisor import prompt_file_diff
from harness.recipes import load_recipes, resolve_analyze_target, suggest_recipes_for_changed_files

ROOT = Path(__file__).resolve().parents[1]

SMOKE = "smoke-welcome"
PLAN_FIELDS = ("must_run", "should_run", "skip", "order", "budget")

# Strict JSON schema for the ranker. `reasons` records drop/pull explanations;
# it is never a verdict.
PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["must_run", "should_run", "skip", "order", "budget", "reasons"],
    "properties": {
        "must_run": {"type": "array", "items": {"type": "string"}},
        "should_run": {"type": "array", "items": {"type": "string"}},
        "skip": {"type": "array", "items": {"type": "string"}},
        "order": {"type": "array", "items": {"type": "string"}},
        "budget": {"type": "integer"},
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
- Do not drop must_run. Do not invent recipe ids. Do not exceed budget.
- smoke-welcome stays first in order whenever it is in the catalog.
- Keep must_run exactly as the compiler provided.
"""

# Deterministic intent keywords. The ranker may use the full --intent text;
# the compiler only does cheap token matching.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "smoke-welcome": ("welcome", "smoke"),
    "b-claims": ("claim", "verify", "verification", "patrol", "work-claim", "work claim"),
    "a3-shift-55252": ("escalat", "safety", "silent", "ladder", "missed"),
    "a3-quiet-probe": ("quiet", "no-op", "noop", "discretionary"),
    "c-voice": ("voice", "comms", "em-dash", "emdash", "filler"),
}

DIFF_EXCERPT_CHARS = 4000

CompleteFn = Callable[[str, str, str], str]
"""(system, user, model) -> raw model text"""


def catalog_ids(data: dict | None = None, *, path: Path | None = None) -> list[str]:
    data = data if data is not None else load_recipes(path)
    return list((data.get("recipes") or {}).keys())


def recipe_cards(data: dict | None = None, *, path: Path | None = None) -> list[dict[str, Any]]:
    data = data if data is not None else load_recipes(path)
    cards = []
    for name, recipe in (data.get("recipes") or {}).items():
        cards.append(
            {
                "id": name,
                "description": recipe.get("description", ""),
                "suggested_when": recipe.get("suggested_when") or [],
                "scorer": recipe.get("scorer"),
                "mode": recipe.get("mode"),
                "candidate_variant": recipe.get("candidate_variant"),
            }
        )
    return cards


def router_defaults(data: dict | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Ranker model/budget from recipes.json defaults (not the copilot model)."""
    data = data if data is not None else load_recipes(path)
    d = data.get("defaults") or {}
    nested = d.get("router") if isinstance(d.get("router"), dict) else {}
    model = nested.get("model") or d.get("router.model") or "gpt-4.1-mini"
    adapter = nested.get("adapter") or d.get("router.adapter") or d.get("adapter") or "openai"
    budget = nested.get("budget") or d.get("router.budget") or 4
    return {"model": str(model), "adapter": str(adapter), "budget": int(budget)}


def plan_payload(plan: dict) -> dict[str, Any]:
    """Exact plan schema (no extra keys)."""
    return {
        "must_run": list(plan.get("must_run") or []),
        "should_run": list(plan.get("should_run") or []),
        "skip": list(plan.get("skip") or []),
        "order": list(plan.get("order") or []),
        "budget": int(plan.get("budget") or 0),
    }


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _as_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x) for x in val]


def _aliases_for(data: dict) -> dict[str, list[str]]:
    aliases = data.get("aliases") or {}
    by_recipe: dict[str, list[str]] = {}
    for alias, full in aliases.items():
        by_recipe.setdefault(full, []).append(str(alias))
    return by_recipe


def _intent_hits(recipe_id: str, recipe: dict, intent: str, data: dict) -> bool:
    blob = (intent or "").strip().lower()
    if not blob:
        return False
    needles = [recipe_id.lower(), recipe_id.replace("-", " ").lower()]
    needles.extend(a.lower() for a in _aliases_for(data).get(recipe_id, []))
    needles.extend(str(t).lower() for t in (recipe.get("suggested_when") or []) if t != "any prompt change")
    needles.extend(_INTENT_KEYWORDS.get(recipe_id, ()))
    return any(n and n in blob for n in needles)


def _apply_budget(
    must_run: list[str],
    should_run: list[str],
    skip: list[str],
    budget: int,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Keep all must_run; fill remaining budget from should_run; overflow → skip."""
    must_run = _dedupe(must_run)
    should_run = _dedupe([x for x in should_run if x not in must_run])
    skip = _dedupe([x for x in skip if x not in must_run and x not in should_run])
    room = max(0, int(budget) - len(must_run))
    kept_should = should_run[:room]
    overflow = should_run[room:]
    if overflow:
        skip = _dedupe(skip + overflow)
    should_run = kept_should
    order = must_run + should_run
    return must_run, should_run, skip, order


def compile_plan(
    *,
    control_dir: Path | None = None,
    variant_dir: Path | None = None,
    intent: str = "",
    budget: int | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic compiler. Catalog constrains; files + intent classify."""
    root = root or ROOT
    data = load_recipes(recipes_path)
    recipes = data.get("recipes") or {}
    ids = list(recipes.keys())
    defaults = router_defaults(data)
    cap = int(budget) if budget is not None else int(defaults["budget"])
    cap = max(1, cap)

    if changed_files is None:
        if control_dir is None or variant_dir is None:
            changed_files = []
        else:
            changed_files, _ = prompt_file_diff(Path(control_dir), Path(variant_dir))

    matched = suggest_recipes_for_changed_files(list(changed_files), recipes_path)
    # smoke-welcome is always a must_run sanity card when present in the catalog.
    must_run: list[str] = []
    if SMOKE in ids:
        must_run.append(SMOKE)
    for rid in ids:
        if rid == SMOKE:
            continue
        if rid in matched:
            must_run.append(rid)

    should_run: list[str] = []
    for rid in ids:
        if rid in must_run:
            continue
        if _intent_hits(rid, recipes.get(rid) or {}, intent, data):
            should_run.append(rid)

    skip = [rid for rid in ids if rid not in must_run and rid not in should_run]
    must_run, should_run, skip, order = _apply_budget(must_run, should_run, skip, cap)

    # smoke-welcome stays first.
    if SMOKE in must_run and (not order or order[0] != SMOKE):
        order = [SMOKE] + [x for x in order if x != SMOKE]
        must_run = [SMOKE] + [x for x in must_run if x != SMOKE]

    return {
        "must_run": must_run,
        "should_run": should_run,
        "skip": skip,
        "order": order,
        "budget": cap,
        "changed_files": list(changed_files),
        "intent": intent or "",
        "source": "compiler",
    }


def diff_summary(
    control_dir: Path,
    variant_dir: Path,
    *,
    excerpt_chars: int = DIFF_EXCERPT_CHARS,
) -> dict[str, Any]:
    changed, diff = prompt_file_diff(Path(control_dir), Path(variant_dir))
    excerpt = diff if len(diff) <= excerpt_chars else diff[:excerpt_chars] + "\n… [truncated]"
    return {"filenames": changed, "unified_diff_excerpt": excerpt}


def validate_ranked_plan(
    ranked: dict,
    compiler: dict,
    *,
    catalog: list[str] | None = None,
    budget: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Deterministic post-validation. Agent proposes; catalog constrains.

    Corrections:
      (1) any dropped must_run is re-inserted
      (2) any recipe id not in the catalog is removed
      (3) budget cap re-applied
      (4) smoke-welcome stays first
    """
    catalog = list(catalog) if catalog is not None else catalog_ids()
    catalog_set = set(catalog)
    cap = int(budget) if budget is not None else int(compiler.get("budget") or 1)
    cap = max(1, cap)
    corrections: list[str] = []

    must_run = _as_str_list(ranked.get("must_run"))
    should_run = _as_str_list(ranked.get("should_run"))
    skip = _as_str_list(ranked.get("skip"))
    order = _as_str_list(ranked.get("order"))
    reasons = ranked.get("reasons") if isinstance(ranked.get("reasons"), list) else []

    def _strip_unknown(seq: list[str], field: str) -> list[str]:
        keep, dropped = [], []
        for x in seq:
            if x in catalog_set:
                keep.append(x)
            else:
                dropped.append(x)
        if dropped:
            corrections.append(
                f"removed unknown recipe id(s) from {field}: {dropped}"
            )
        return keep

    # (2) drop ids the catalog does not know.
    must_run = _strip_unknown(must_run, "must_run")
    should_run = _strip_unknown(should_run, "should_run")
    skip = _strip_unknown(skip, "skip")
    order = _strip_unknown(order, "order")

    compiler_must = [x for x in _as_str_list(compiler.get("must_run")) if x in catalog_set]
    if SMOKE in catalog_set and SMOKE not in compiler_must:
        compiler_must = [SMOKE] + compiler_must

    # Ranker may not promote extra ids into must_run.
    extra_must = [x for x in must_run if x not in compiler_must]
    if extra_must:
        corrections.append(f"moved unauthorized must_run into should_run: {extra_must}")
        for x in extra_must:
            if x not in should_run:
                should_run.append(x)

    # (1) re-insert any compiler must_run the ranker dropped.
    dropped_must = [rid for rid in compiler_must if rid not in must_run]
    for rid in dropped_must:
        corrections.append(f"re-inserted dropped must_run: {rid}")
    must_run = list(compiler_must)

    # Ranker may reorder / drop / pull should_run. must_run is not a should_run.
    should_run = _dedupe([x for x in should_run if x not in must_run and x in catalog_set])
    ranked_should_order = [x for x in order if x in should_run]
    rest_should = [x for x in should_run if x not in ranked_should_order]
    should_run = _dedupe(ranked_should_order + rest_should)

    skip = [x for x in catalog if x not in must_run and x not in should_run]

    # (4) log if the ranker did not keep smoke-welcome first, then force it.
    ranked_order_for_first = list(order) if order else list(must_run + should_run)
    if SMOKE in catalog_set and SMOKE in must_run:
        if not ranked_order_for_first or ranked_order_for_first[0] != SMOKE:
            corrections.append("moved smoke-welcome to first in order")

    # (3) budget cap: never trim must_run; overflow should_run → skip.
    before_should = list(should_run)
    must_run, should_run, skip, order = _apply_budget(must_run, should_run, skip, cap)
    trimmed = [x for x in before_should if x not in should_run]
    if trimmed:
        corrections.append(
            f"re-applied budget cap {cap}; trimmed from should_run/order: {trimmed}"
        )

    if SMOKE in catalog_set and SMOKE in must_run:
        order = [SMOKE] + [x for x in order if x != SMOKE]
        must_run = [SMOKE] + [x for x in must_run if x != SMOKE]

    plan = {
        "must_run": must_run,
        "should_run": should_run,
        "skip": skip,
        "order": order,
        "budget": cap,
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
    missing = [k for k in PLAN_FIELDS if k not in data]
    if missing:
        raise ValueError(f"missing plan keys: {missing}")
    return data


def _openai_rank(system: str, user: str, model: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "eval_plan",
                "strict": True,
                "schema": PLAN_JSON_SCHEMA,
            },
        },
    }
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


def _default_complete(adapter: str) -> CompleteFn:
    if adapter == "openai":
        return _openai_rank
    if adapter == "anthropic":
        return _anthropic_rank
    raise ValueError(f"unknown ranker adapter: {adapter}")


def _ranker_user_payload(
    *,
    cards: list[dict],
    diff: dict,
    intent: str,
    compiler: dict,
) -> str:
    body = {
        "recipe_cards": cards,
        "diff_summary": diff,
        "intent": intent or "",
        "compiler_plan": plan_payload(compiler),
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
    cards: list[dict] | None = None,
    diff: dict | None = None,
    intent: str = "",
    catalog: list[str] | None = None,
    model: str | None = None,
    adapter: str | None = None,
    recipes_path: Path | None = None,
    complete_fn: CompleteFn | None = None,
) -> RankResult:
    """Call the ranker, parse JSON (one retry), validate, or fall back silently."""
    data = load_recipes(recipes_path)
    defaults = router_defaults(data)
    model = model or defaults["model"]
    adapter = adapter or defaults["adapter"]
    catalog = list(catalog) if catalog is not None else catalog_ids(data)
    cards = cards if cards is not None else recipe_cards(data)
    diff = diff if diff is not None else {"filenames": compiler.get("changed_files") or [], "unified_diff_excerpt": ""}
    complete = complete_fn or _default_complete(adapter)
    user = _ranker_user_payload(
        cards=cards, diff=diff, intent=intent, compiler=compiler
    )

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
            raw = complete(RANKER_SYSTEM, prompt, model)
            parsed = parse_plan_json(raw)
            break
        except Exception as exc:  # noqa: BLE001 — any LLM/parse failure falls back
            last_err = f"{type(exc).__name__}: {exc}"
            parsed = None

    if parsed is None:
        fallback_plan = {
            **plan_payload(compiler),
            "reasons": [],
            "source": "ranker_fallback",
        }
        return RankResult(
            compiler_plan=plan_payload(compiler),
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
        budget=int(compiler.get("budget") or defaults["budget"]),
    )
    return RankResult(
        compiler_plan=plan_payload(compiler),
        ranked_plan=validated,
        corrections=corrections,
        fallback=False,
        fallback_reason=None,
        ranker_model=model,
    )


def plan_diff_text(compiler: dict, ranked: dict) -> str:
    """Human-readable diff of the two plans. Not a verdict."""
    left = plan_payload(compiler)
    right = plan_payload(ranked)
    lines = ["plan diff (compiler → ranked):"]
    changed = False
    for key in PLAN_FIELDS:
        a, b = left.get(key), right.get(key)
        if a == b:
            lines.append(f"  {key}: (unchanged) {a!r}")
        else:
            changed = True
            lines.append(f"  {key}: {a!r} → {b!r}")
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


def format_go_output(
    *,
    compiler: dict,
    ranked: RankResult | None = None,
    rank: bool = False,
) -> str:
    """Printed `cx go` output. Ranker text is never framed as pass/fail."""
    chunks: list[str] = []
    if rank and ranked is not None:
        chunks.append("=== compiler plan ===")
        chunks.append(json.dumps(plan_payload(compiler), indent=2))
        chunks.append("")
        chunks.append("=== ranked plan ===")
        chunks.append(json.dumps(plan_payload(ranked.ranked_plan), indent=2))
        chunks.append("")
        chunks.append("=== plan diff ===")
        chunks.append(plan_diff_text(compiler, ranked.ranked_plan))
        if ranked.fallback:
            chunks.append("")
            chunks.append(
                f"note: ranker fallback to compiler plan ({ranked.fallback_reason})"
            )
        if ranked.corrections:
            chunks.append("")
            chunks.append("ranker validator corrections:")
            for c in ranked.corrections:
                chunks.append(f"  - {c}")
        if ranked.ranker_model:
            chunks.append("")
            chunks.append(f"ranker model: {ranked.ranker_model} (not a verdict)")
    else:
        chunks.append("=== go plan (compiler) ===")
        chunks.append(json.dumps(plan_payload(compiler), indent=2))
        if compiler.get("changed_files") is not None:
            chunks.append("")
            chunks.append(f"changed files: {compiler.get('changed_files')}")
        if compiler.get("intent"):
            chunks.append(f"intent: {compiler.get('intent')}")
    return "\n".join(chunks) + "\n"


def resolve_go_dirs(
    target: str | None,
    *,
    control: str | None = None,
    variant: str | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
) -> tuple[Path, Path]:
    root = root or ROOT
    data = load_recipes(recipes_path)
    defaults = data.get("defaults") or {}
    control_path = Path(control or defaults.get("control_variant") or "variants/baseline")
    if not control_path.is_absolute():
        control_path = root / control_path

    variant_spec = variant or target
    if variant_spec:
        try:
            resolved = resolve_analyze_target(variant_spec, recipes_path)
        except KeyError:
            resolved = variant_spec
        variant_path = Path(resolved)
        if not variant_path.is_absolute():
            variant_path = root / variant_path
        return control_path, variant_path

    # No target: pick the analyze target with the most prompt-file diffs.
    best: Path | None = None
    best_n = -1
    for path_str in (data.get("analyze_targets") or {}).values():
        p = Path(path_str)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            continue
        changed, _ = prompt_file_diff(control_path, p)
        if len(changed) > best_n:
            best_n = len(changed)
            best = p
    if best is None:
        best = root / "variants" / "variant_a3"
    return control_path, best


def run_go(
    *,
    target: str | None = None,
    control: str | None = None,
    variant: str | None = None,
    intent: str = "",
    budget: int | None = None,
    rank: bool = False,
    dry_run: bool = True,
    root: Path | None = None,
    recipes_path: Path | None = None,
    complete_fn: CompleteFn | None = None,
    run_jobs_fn: Callable[..., str] | None = None,
    adapter: str | None = None,
    model: str | None = None,
    print_fn: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    """Compile (and optionally rank) a plan; execute order unless dry_run."""
    root = root or ROOT
    control_dir, variant_dir = resolve_go_dirs(
        target,
        control=control,
        variant=variant,
        root=root,
        recipes_path=recipes_path,
    )
    compiler = compile_plan(
        control_dir=control_dir,
        variant_dir=variant_dir,
        intent=intent,
        budget=budget,
        root=root,
        recipes_path=recipes_path,
    )
    ranked: RankResult | None = None
    if rank:
        diff = diff_summary(control_dir, variant_dir)
        ranked = rank_plan(
            compiler,
            cards=recipe_cards(path=recipes_path),
            diff=diff,
            intent=intent,
            catalog=catalog_ids(path=recipes_path),
            recipes_path=recipes_path,
            complete_fn=complete_fn,
        )
        compiler["changed_files"] = diff["filenames"]

    text = format_go_output(compiler=compiler, ranked=ranked, rank=rank)
    if print_fn:
        print_fn(text.rstrip("\n"))

    final = plan_payload(ranked.ranked_plan if (rank and ranked) else compiler)
    result: dict[str, Any] = {
        "compiler_plan": plan_payload(compiler),
        "ranked_plan": plan_payload(ranked.ranked_plan) if ranked else None,
        "corrections": list(ranked.corrections) if ranked else [],
        "fallback": bool(ranked.fallback) if ranked else False,
        "fallback_reason": ranked.fallback_reason if ranked else None,
        "plan": final,
        "control_dir": str(control_dir),
        "variant_dir": str(variant_dir),
        "dry_run": dry_run,
        "rank": rank,
        "output": text,
    }
    if dry_run:
        return result

    from harness.recipes import execute_recipe

    scores = []
    for name in final["order"]:
        scores.append(
            execute_recipe(
                name,
                run_jobs_fn=run_jobs_fn,  # type: ignore[arg-type]
                root=root,
                adapter=adapter,
                model=model,
                candidate_variant=str(variant_dir.relative_to(root))
                if variant_dir.is_relative_to(root)
                else str(variant_dir),
                recipes_path=recipes_path,
            )
        )
    result["scores"] = scores
    return result
