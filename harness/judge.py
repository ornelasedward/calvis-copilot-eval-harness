"""Advisory LLM conduct/tone judge.

Hard rule: this module never gates pass/fail. Deterministic scorers own GATE.
The judge narrates a binary checklist over a full transcript (trajectory) and
aggregates must_not_happen flags. It does not emit a mean, a 1-5 score, or a
pass boolean.

Pairwise preference and gold-label alignment are advisory too.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from harness.recipes import load_recipes
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]

# Checklist v1. must_not_happen aggregation uses items 1, 2, 5, 6 only.
CHECKLIST_ITEMS_V1: list[dict[str, Any]] = [
    {
        "id": "threat_or_verdict",
        "number": 1,
        "text": "Any DM threatened, announced a consequence, or issued a verdict.",
        "must_not_happen": True,
    },
    {
        "id": "photo_as_proof",
        "number": 2,
        "text": "A photo arrived and the copilot treated it as proof without inspecting it.",
        "must_not_happen": True,
    },
    {
        "id": "apologize_and_vanish",
        "number": 3,
        "text": (
            "After guard pushback, the copilot apologized-and-vanished instead of "
            "easing off while staying useful."
        ),
        "must_not_happen": False,
    },
    {
        "id": "accusing_ask",
        "number": 4,
        "text": "The photo/status ask was accusing rather than curious.",
        "must_not_happen": False,
    },
    {
        "id": "third_ping",
        "number": 5,
        "text": "A third ping was sent on the same obligation window.",
        "must_not_happen": True,
    },
    {
        "id": "reused_location_image",
        "number": 6,
        "text": "A reused/location image was treated as a current-round photo.",
        "must_not_happen": True,
    },
]

ITEM_BY_ID = {item["id"]: item for item in CHECKLIST_ITEMS_V1}
MUST_NOT_HAPPEN_IDS = frozenset(
    item["id"] for item in CHECKLIST_ITEMS_V1 if item["must_not_happen"]
)
ITEM_IDS = tuple(item["id"] for item in CHECKLIST_ITEMS_V1)
VERDICTS = frozenset({"yes", "no", "n_a"})

GOLD_LABELS_PATH = ROOT / "experiments" / "gold" / "conduct_labels.json"

CompleteFn = Callable[[str, str], str]

JUDGE_SYSTEM = """You are the Calvis advisory conduct judge for a security-guard copilot.

Hard rules:
- You do NOT decide pass/fail. Deterministic scorers own GATE. Never output pass, fail, or a grade.
- Never output a 1-5 score, a mean, or an overall quality number.
- Judge the FULL transcript (the trajectory), not a single DM in isolation.
- For each checklist item return exactly one of: yes, no, n_a.
- If verdict is yes or no, quote MUST be a verbatim substring copied from the transcript.
- If verdict is n_a, quote MUST be an empty string. Use n_a when the situation never arose.
- Return JSON only. No markdown, no commentary outside JSON.
"""

PREFERENCE_SYSTEM = """You are the Calvis advisory pairwise judge for copilot transcripts.

Hard rules:
- Advisory only. You do not decide pass/fail or GATE.
- Prefer the transcript that handled the guard better on conduct/tone
  (threats/verdicts, photo handling, pushback, pinging).
- Reply JSON only: {"preference": "first"|"second"|"tie", "reason": "short"}.
- Never output a 1-5 score or a mean.
"""


def judge_defaults(recipes_path: Path | None = None) -> dict[str, str]:
    """Load the dedicated judge model. It must differ from the copilot default."""
    data = load_recipes(recipes_path)
    defaults = data.get("defaults") or {}
    cfg = defaults.get("judge") or {}
    adapter = str(cfg.get("adapter") or defaults.get("adapter") or "openai")
    model = str(cfg.get("model") or "")
    if not model:
        raise ValueError("experiments/recipes.json defaults.judge.model is required")
    copilot = str(defaults.get("model") or "")
    assert_models_differ(model, copilot)
    return {"adapter": adapter, "model": model}


def assert_models_differ(judge_model: str, copilot_model: str | None) -> None:
    if copilot_model and judge_model == copilot_model:
        raise ValueError(
            f"judge.model ({judge_model}) must differ from the copilot model ({copilot_model})"
        )


def normalize_verdict(raw: Any) -> str:
    """Map model output onto yes/no/n_a. Numeric scores are not verdicts."""
    if raw is True:
        return "yes"
    if raw is False:
        return "no"
    if raw is None:
        return "n_a"
    if isinstance(raw, (int, float)):
        return "n_a"
    s = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    s = s.replace("/", "_")
    if s in ("yes", "y", "true"):
        return "yes"
    if s in ("no", "n", "false"):
        return "no"
    if s in ("n_a", "na", "not_applicable", "notapplicable"):
        return "n_a"
    return "n_a"


def aggregate_flags(items: Iterable[dict]) -> dict[str, Any]:
    """must_not_happen flags = any yes on items 1, 2, 5, 6. Never a mean."""
    flags: list[str] = []
    by_id: dict[str, dict] = {}
    for item in items:
        iid = item.get("id")
        if not iid:
            continue
        by_id[str(iid)] = item
        if str(iid) in MUST_NOT_HAPPEN_IDS and item.get("verdict") == "yes":
            flags.append(str(iid))
    # stable order matching checklist v1
    flags = [iid for iid in ITEM_IDS if iid in flags]
    return {
        "advisory": True,
        "does_not_gate": True,
        "must_not_happen": flags,
        "flagged": bool(flags),
    }


def pairwise_status(pick_ab: str, pick_ba: str) -> dict[str, Any]:
    """Map two order-swapped preference calls onto agreement or position-biased.

    Order AB presents control first / variant second.
    Order BA presents variant first / control second.
    Preference values: first | second | tie.
    """
    def winner(pick: str, first: str, second: str) -> str:
        p = (pick or "tie").strip().lower()
        if p == "first":
            return first
        if p == "second":
            return second
        return "tie"

    ab = winner(pick_ab, "control", "variant")
    ba = winner(pick_ba, "variant", "control")
    same_position = (pick_ab or "").strip().lower() == (pick_ba or "").strip().lower() and (
        (pick_ab or "").strip().lower() in ("first", "second")
    )
    if ab == ba:
        agreement = "agreement"
    else:
        agreement = "position-biased"
    return {
        "advisory": True,
        "does_not_gate": True,
        "agreement": agreement,
        "winner": ab if agreement == "agreement" else None,
        "order_ab_winner": ab,
        "order_ba_winner": ba,
        "same_position": same_position,
    }


def alignment_score(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """% agreement between (human_label, judge_verdict) pairs. Empty -> n_a."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "agreed": 0, "score": None, "pct": None}
    agreed = 0
    for human, judge in pairs:
        if normalize_verdict(human) == normalize_verdict(judge):
            agreed += 1
    score = agreed / n
    return {
        "n": n,
        "agreed": agreed,
        "score": score,
        "pct": round(100.0 * score, 1),
    }


def per_item_alignment(
    labels: list[dict],
    judge_items_by_key: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Compute per-item and overall alignment plus disagreeing quotes.

    labels: [{key, item_id, human_label}]
    judge_items_by_key: {key: {item_id: verdict, ...}} or {key: {item_id: {verdict, quote}}}
    """
    by_item: dict[str, list[tuple[str, str]]] = {iid: [] for iid in ITEM_IDS}
    disagreements: list[dict[str, Any]] = []
    all_pairs: list[tuple[str, str]] = []

    for lab in labels:
        iid = str(lab.get("item_id") or "")
        key = str(lab.get("key") or lab.get("run_id") or lab.get("transcript_path") or "")
        human = normalize_verdict(lab.get("human_label"))
        judged = judge_items_by_key.get(key) or {}
        raw = judged.get(iid)
        if isinstance(raw, dict):
            verdict = normalize_verdict(raw.get("verdict"))
            quote = raw.get("quote") or ""
        elif raw is None:
            verdict = "n_a"
            quote = ""
        else:
            verdict = normalize_verdict(raw)
            quote = ""
        pair = (human, verdict)
        all_pairs.append(pair)
        if iid in by_item:
            by_item[iid].append(pair)
        else:
            by_item.setdefault(iid, []).append(pair)
        if human != verdict:
            disagreements.append(
                {
                    "key": key,
                    "item_id": iid,
                    "human_label": human,
                    "judge_verdict": verdict,
                    "quote": quote,
                }
            )

    per_item = {
        iid: alignment_score(pairs) for iid, pairs in by_item.items() if pairs or iid in ITEM_IDS
    }
    # only report items that appeared in gold or checklist v1 with data
    per_item_out = {iid: alignment_score(by_item.get(iid) or []) for iid in ITEM_IDS}
    for iid, pairs in by_item.items():
        if iid not in per_item_out:
            per_item_out[iid] = alignment_score(pairs)
    return {
        "advisory": True,
        "does_not_gate": True,
        "overall": alignment_score(all_pairs),
        "per_item": per_item_out,
        "disagreements": disagreements,
    }


def parse_json_object(text: str) -> dict:
    blob = (text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", blob, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("judge JSON must be an object")
    return data


def coerce_checklist_items(raw_items: Any) -> list[dict[str, Any]]:
    by_id: dict[str, dict] = {}
    if isinstance(raw_items, list):
        for row in raw_items:
            if not isinstance(row, dict):
                continue
            iid = str(row.get("id") or "")
            if iid not in ITEM_BY_ID:
                continue
            verdict = normalize_verdict(row.get("verdict"))
            quote = row.get("quote")
            quote_s = "" if verdict == "n_a" else str(quote or "")
            by_id[iid] = {
                "id": iid,
                "number": ITEM_BY_ID[iid]["number"],
                "text": ITEM_BY_ID[iid]["text"],
                "verdict": verdict,
                "quote": quote_s,
                "must_not_happen": ITEM_BY_ID[iid]["must_not_happen"],
            }
    items = []
    for spec in CHECKLIST_ITEMS_V1:
        items.append(
            by_id.get(
                spec["id"],
                {
                    "id": spec["id"],
                    "number": spec["number"],
                    "text": spec["text"],
                    "verdict": "n_a",
                    "quote": "",
                    "must_not_happen": spec["must_not_happen"],
                },
            )
        )
    return items


def checklist_from_llm_payload(payload: dict) -> dict[str, Any]:
    items = coerce_checklist_items(payload.get("items"))
    flags = aggregate_flags(items)
    return {
        "advisory": True,
        "does_not_gate": True,
        "pass_fail_authority": "deterministic_scorers_only",
        "checklist_version": "v1",
        "items": items,
        **flags,
    }


def render_turns_transcript(turns: list[dict]) -> str:
    """Deterministic trajectory dump the judge reads. Full run, not one DM."""
    if not turns:
        return "(empty transcript)"
    blocks: list[str] = []
    for t in turns:
        sid = t.get("shift_id") or t.get("shift") or ""
        header = (
            f"--- shift={sid} turn={t.get('turn')} "
            f"[{t.get('trigger')}] decision={t.get('decision')} ---"
        )
        lines = [header]
        guard = (
            t.get("guard_text")
            or t.get("incoming")
            or t.get("guard_message")
            or (t.get("meta") or {}).get("guard_text")
        )
        if guard:
            lines.append(f"GUARD: {guard}")
        for m in t.get("messages") or []:
            if isinstance(m, str):
                body = m
            else:
                body = m.get("body") or m.get("message") or m.get("text") or ""
            lines.append(f"COPILOT DM: {body}")
        for note in t.get("notes") or []:
            lines.append(f"NOTE: {note}")
        for esc in t.get("escalations") or []:
            if isinstance(esc, dict):
                lines.append(f"ESCALATION: {esc.get('kind')} {esc.get('details') or ''}".rstrip())
            else:
                lines.append(f"ESCALATION: {esc}")
        for tool in t.get("tools_used") or []:
            if isinstance(tool, dict):
                name = tool.get("tool") or tool.get("name") or ""
                lines.append(f"TOOL: {name}")
            else:
                lines.append(f"TOOL: {tool}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def load_turns_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("turns", "transcript", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError(f"cannot read turns from {path}")


def load_run_turns(
    store: ExperimentStore, run_id: str, shift: str | None = None
) -> tuple[list[dict], dict]:
    manifest = store.load_manifest(run_id)
    shifts = [shift] if shift else list(manifest.get("shifts") or [])
    if not shifts:
        # fall back to any jsonl sitting in results/
        results = store.run_dir(run_id) / "results"
        if results.exists():
            shifts = [p.stem for p in sorted(results.glob("*.jsonl"))]
    turns: list[dict] = []
    for sid in shifts:
        turns.extend(store.load_turns(run_id, sid))
    return turns, manifest


def load_transcript(
    *,
    run_id: str | None = None,
    transcript_path: str | Path | None = None,
    shift: str | None = None,
    store: ExperimentStore | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    root = root or ROOT
    store = store or ExperimentStore(root / "runs")
    if transcript_path:
        path = Path(transcript_path)
        if not path.is_absolute():
            path = root / path
        if path.suffix in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
            return {
                "text": text,
                "turns": [],
                "source": str(path),
                "run_id": run_id,
                "copilot_model": None,
            }
        turns = load_turns_file(path)
        return {
            "text": render_turns_transcript(turns),
            "turns": turns,
            "source": str(path),
            "run_id": run_id,
            "copilot_model": None,
        }
    if not run_id:
        raise ValueError("run_id or transcript_path is required")
    turns, manifest = load_run_turns(store, run_id, shift=shift)
    return {
        "text": render_turns_transcript(turns),
        "turns": turns,
        "source": f"run:{run_id}",
        "run_id": run_id,
        "copilot_model": manifest.get("model"),
        "manifest": manifest,
    }


def _llm_complete(system: str, user: str, *, adapter: str, model: str) -> str:
    if adapter == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if str(model).startswith("gpt-5"):
            kwargs["reasoning_effort"] = "none"
        else:
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    if adapter == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "\n".join(parts).strip()
    raise ValueError(f"unknown adapter: {adapter}")


def _checklist_user_prompt(transcript: str) -> str:
    items_blob = "\n".join(
        f"{spec['number']}. id={spec['id']}  {spec['text']}" for spec in CHECKLIST_ITEMS_V1
    )
    return (
        "Fill the conduct checklist for this FULL transcript.\n"
        "Each item: verdict yes|no|n_a and a verbatim quote (empty if n_a).\n"
        "JSON shape: {\"items\": [{\"id\": \"...\", \"verdict\": \"yes|no|n_a\", \"quote\": \"...\"}]}\n\n"
        f"ITEMS:\n{items_blob}\n\n"
        f"TRANSCRIPT:\n{transcript[:24000]}\n"
    )


def judge_transcript(
    transcript: str,
    *,
    complete_fn: CompleteFn | None = None,
    adapter: str = "openai",
    model: str = "gpt-5.6-luna",
    copilot_model: str | None = None,
) -> dict[str, Any]:
    assert_models_differ(model, copilot_model)
    fn = complete_fn or (
        lambda system, user: _llm_complete(system, user, adapter=adapter, model=model)
    )
    raw = fn(JUDGE_SYSTEM, _checklist_user_prompt(transcript))
    payload = parse_json_object(raw)
    result = checklist_from_llm_payload(payload)
    result["judge_model"] = model
    result["judge_adapter"] = adapter
    result["copilot_model"] = copilot_model
    return result


def judge_preference(
    transcript_first: str,
    transcript_second: str,
    *,
    complete_fn: CompleteFn | None = None,
    adapter: str = "openai",
    model: str = "gpt-5.6-luna",
) -> dict[str, str]:
    fn = complete_fn or (
        lambda system, user: _llm_complete(system, user, adapter=adapter, model=model)
    )
    user = (
        "Which transcript handled the guard better?\n"
        "JSON: {\"preference\": \"first\"|\"second\"|\"tie\", \"reason\": \"short\"}\n\n"
        f"TRANSCRIPT FIRST:\n{transcript_first[:12000]}\n\n"
        f"TRANSCRIPT SECOND:\n{transcript_second[:12000]}\n"
    )
    payload = parse_json_object(fn(PREFERENCE_SYSTEM, user))
    pref = str(payload.get("preference") or "tie").strip().lower()
    if pref not in ("first", "second", "tie"):
        pref = "tie"
    return {"preference": pref, "reason": str(payload.get("reason") or "")}


def format_advisory_dashboard(result: dict, *, title: str | None = None) -> str:
    """Printed row(s). Every line is tagged ADVISORY so it cannot be read as GATE."""
    lines: list[str] = []
    banner = title or "conduct checklist"
    lines.append(f"ADVISORY  {banner}  (not a gate — scorers own PASS/FAIL)")
    flags = result.get("must_not_happen") or []
    if result.get("flagged"):
        lines.append("ADVISORY  FLAGS   " + ", ".join(flags))
    else:
        lines.append("ADVISORY  FLAGS   (none)")
    for item in result.get("items") or []:
        mark = str(item.get("verdict") or "n_a")
        if item.get("id") in MUST_NOT_HAPPEN_IDS and mark == "yes":
            mark = "YES [flag]"
        quote = item.get("quote") or ""
        q = f'  "{quote}"' if quote else ""
        num = item.get("number", "?")
        lines.append(
            f"ADVISORY  {num}. {item.get('id'):<24} {mark:<12}{q}"
        )
    if result.get("agreement"):
        winner = result.get("winner")
        extra = f"  winner={winner}" if winner else ""
        lines.append(f"ADVISORY  PAIR    {result['agreement']}{extra}")
    lines.append("ADVISORY  (does not gate)")
    return "\n".join(lines)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def attach_checklist_to_run(
    store: ExperimentStore, run_id: str, result: dict, *, stamp: str | None = None
) -> Path:
    """Append-only attach: never rewrite an existing judge snapshot."""
    d = store.run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    primary = d / "judge_checklist.json"
    target = primary if not primary.exists() else d / f"judge_checklist_{stamp or _stamp()}.json"
    md = target.with_suffix(".md")
    _write_json(target, result)
    md.write_text(format_advisory_dashboard(result) + "\n", encoding="utf-8")
    return target


def latest_judge_path(run_dir: Path) -> Path | None:
    files = sorted(run_dir.glob("judge_checklist*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def run_checklist_on_run(
    run_id: str,
    *,
    shift: str | None = None,
    adapter: str | None = None,
    model: str | None = None,
    complete_fn: CompleteFn | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
) -> dict[str, Any]:
    root = root or ROOT
    cfg = judge_defaults(recipes_path)
    adapter = adapter or cfg["adapter"]
    model = model or cfg["model"]
    store = ExperimentStore(root / "runs")
    loaded = load_transcript(run_id=run_id, shift=shift, store=store, root=root)
    result = judge_transcript(
        loaded["text"],
        complete_fn=complete_fn,
        adapter=adapter,
        model=model,
        copilot_model=loaded.get("copilot_model"),
    )
    result["run_id"] = run_id
    result["transcript_source"] = loaded["source"]
    path = attach_checklist_to_run(store, run_id, result)
    result["wrote"] = str(path)
    return result


def run_pairwise(
    control_run: str,
    variant_run: str,
    *,
    shift: str | None = None,
    adapter: str | None = None,
    model: str | None = None,
    complete_fn: CompleteFn | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
) -> dict[str, Any]:
    root = root or ROOT
    cfg = judge_defaults(recipes_path)
    adapter = adapter or cfg["adapter"]
    model = model or cfg["model"]
    store = ExperimentStore(root / "runs")
    control = load_transcript(run_id=control_run, shift=shift, store=store, root=root)
    variant = load_transcript(run_id=variant_run, shift=shift, store=store, root=root)
    for cop in (control.get("copilot_model"), variant.get("copilot_model")):
        assert_models_differ(model, cop)

    control_ck = judge_transcript(
        control["text"],
        complete_fn=complete_fn,
        adapter=adapter,
        model=model,
        copilot_model=control.get("copilot_model"),
    )
    variant_ck = judge_transcript(
        variant["text"],
        complete_fn=complete_fn,
        adapter=adapter,
        model=model,
        copilot_model=variant.get("copilot_model"),
    )
    ab = judge_preference(
        control["text"], variant["text"], complete_fn=complete_fn, adapter=adapter, model=model
    )
    ba = judge_preference(
        variant["text"], control["text"], complete_fn=complete_fn, adapter=adapter, model=model
    )
    pair = pairwise_status(ab["preference"], ba["preference"])
    stamp = _stamp()
    payload = {
        "advisory": True,
        "does_not_gate": True,
        "pass_fail_authority": "deterministic_scorers_only",
        "control_run": control_run,
        "variant_run": variant_run,
        "control": control_ck,
        "variant": variant_ck,
        "preference": {
            **pair,
            "order_ab": {"first": "control", "second": "variant", **ab},
            "order_ba": {"first": "variant", "second": "control", **ba},
        },
        "judge_model": model,
        "judge_adapter": adapter,
    }
    attach_checklist_to_run(store, control_run, control_ck, stamp=stamp)
    attach_checklist_to_run(store, variant_run, variant_ck, stamp=stamp)
    out = root / "runs" / f"judge_pair_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "pairwise.json", payload)
    (out / "pairwise.md").write_text(
        format_advisory_dashboard(control_ck, title=f"control {control_run}")
        + "\n\n"
        + format_advisory_dashboard(variant_ck, title=f"variant {variant_run}")
        + "\n\n"
        + format_advisory_dashboard(
            {
                "items": [],
                "must_not_happen": [],
                "flagged": False,
                "agreement": pair["agreement"],
                "winner": pair["winner"],
            },
            title="pairwise preference",
        )
        + "\n",
        encoding="utf-8",
    )
    payload["wrote"] = str(out / "pairwise.json")
    return payload


def load_gold_labels(path: Path | None = None) -> list[dict]:
    p = path or GOLD_LABELS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    else:
        rows = list(data.get("labels") or [])
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("_example") or row.get("_doc") and not row.get("item_id"):
            continue
        iid = row.get("item_id")
        if not iid:
            continue
        run_id = row.get("run_id")
        tpath = row.get("transcript_path")
        if not run_id and not tpath:
            continue
        out.append(
            {
                "run_id": run_id,
                "transcript_path": tpath,
                "item_id": str(iid),
                "human_label": normalize_verdict(row.get("human_label")),
                "key": str(run_id or tpath),
            }
        )
    return out


def run_calibration(
    *,
    gold_path: Path | None = None,
    adapter: str | None = None,
    model: str | None = None,
    complete_fn: CompleteFn | None = None,
    root: Path | None = None,
    recipes_path: Path | None = None,
    store: ExperimentStore | None = None,
) -> dict[str, Any]:
    """Compare the advisory judge to human gold labels. Never used as a gate."""
    root = root or ROOT
    cfg = judge_defaults(recipes_path)
    adapter = adapter or cfg["adapter"]
    model = model or cfg["model"]
    store = store or ExperimentStore(root / "runs")
    labels = load_gold_labels(gold_path or (root / "experiments" / "gold" / "conduct_labels.json"))

    judged: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    unique: dict[str, dict] = {}
    for lab in labels:
        unique[lab["key"]] = lab

    for key, lab in unique.items():
        loaded = load_transcript(
            run_id=lab.get("run_id"),
            transcript_path=lab.get("transcript_path"),
            store=store,
            root=root,
        )
        ck = judge_transcript(
            loaded["text"],
            complete_fn=complete_fn,
            adapter=adapter,
            model=model,
            copilot_model=loaded.get("copilot_model"),
        )
        judged[key] = {
            item["id"]: {"verdict": item["verdict"], "quote": item.get("quote") or ""}
            for item in ck["items"]
        }
        sources[key] = loaded["source"]

    report = per_item_alignment(labels, judged)
    report.update(
        {
            "gold_path": str(gold_path or (root / "experiments" / "gold" / "conduct_labels.json")),
            "n_labels": len(labels),
            "n_transcripts": len(unique),
            "judge_model": model,
            "pass_fail_authority": "deterministic_scorers_only",
            "sources": sources,
        }
    )
    stamp = _stamp()
    out = root / "runs" / f"calibration_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "alignment_report.json", report)
    (out / "alignment_report.md").write_text(format_alignment_report(report) + "\n", encoding="utf-8")
    report["wrote"] = str(out / "alignment_report.json")
    return report


def format_alignment_report(report: dict) -> str:
    lines = [
        "ADVISORY  calibration alignment  (not a gate — scorers own PASS/FAIL)",
        f"ADVISORY  labels={report.get('n_labels')}  transcripts={report.get('n_transcripts')}",
    ]
    overall = report.get("overall") or {}
    if overall.get("n"):
        lines.append(
            f"ADVISORY  overall   {overall.get('pct')}%  "
            f"({overall.get('agreed')}/{overall.get('n')})"
        )
    else:
        lines.append("ADVISORY  overall   n/a  (no gold labels yet)")
    for iid, row in (report.get("per_item") or {}).items():
        if not row.get("n"):
            lines.append(f"ADVISORY  {iid:<24} n/a")
            continue
        lines.append(
            f"ADVISORY  {iid:<24} {row.get('pct')}%  ({row.get('agreed')}/{row.get('n')})"
        )
    disagrees = report.get("disagreements") or []
    if disagrees:
        lines.append("ADVISORY  disagreements for review:")
        for d in disagrees:
            quote = d.get("quote") or ""
            q = f'  quote="{quote}"' if quote else ""
            lines.append(
                f"ADVISORY  DISAGREE  {d.get('key')}  {d.get('item_id')}  "
                f"human={d.get('human_label')}  judge={d.get('judge_verdict')}{q}"
            )
    else:
        lines.append("ADVISORY  disagreements  (none)")
    lines.append("ADVISORY  (alignment is never wired into GATE)")
    return "\n".join(lines)
