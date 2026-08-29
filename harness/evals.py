"""Custom evaluation engine — Calvis-owned, provider-agnostic.

Primary: behavioral deltas + declared intent assertions.
Secondary: optional LLM judge on diverged turns.
Descriptive only: trajectory labels vs baseline (never scores).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Assertion:
    id: str
    description: str
    trigger: str | None = None
    shifts: list[str] | None = None
    metric: str = ""
    expect: dict = field(default_factory=dict)


@dataclass
class AssertionResult:
    id: str
    description: str
    passed: bool | None  # None = N/A
    detail: dict
    status: str = "fail"  # pass | fail | na


def summarize_turns(turns: list[dict]) -> dict:
    """Deterministic behavioral counters from TurnResult dicts."""
    n = len(turns)
    decisions = Counter(t.get("decision") for t in turns)
    triggers = Counter(t.get("trigger") for t in turns)
    esc = Counter()
    for t in turns:
        for e in t.get("escalations") or []:
            esc[e.get("kind")] += 1

    scheduled = [t for t in turns if t.get("trigger") == "scheduled_check_in"]
    scheduled_noop = sum(1 for t in scheduled if t.get("decision") == "no_op")
    guard_turns = [t for t in turns if t.get("trigger") == "guard_message"]
    guard_replied = sum(
        1 for t in guard_turns
        if t.get("decision") == "send_message" or (t.get("messages") or [])
    )

    dm_count = sum(len(t.get("messages") or []) for t in turns)
    note_count = sum(len(t.get("notes") or []) for t in turns)
    data_gaps = sum(len(t.get("data_gaps") or []) for t in turns)
    reduced = sum(1 for t in turns if t.get("confidence") == "reduced")

    cost = sum((t.get("usage") or {}).get("cost_usd", 0) or 0 for t in turns)
    tokens_in = sum((t.get("usage") or {}).get("input_tokens", 0) or 0 for t in turns)
    tokens_out = sum((t.get("usage") or {}).get("output_tokens", 0) or 0 for t in turns)

    return {
        "turns": n,
        "decisions": dict(decisions),
        "triggers": dict(triggers),
        "dms": dm_count,
        "notes": note_count,
        "escalations": dict(esc),
        "escalation_total": sum(esc.values()),
        "scheduled_check_ins": len(scheduled),
        "scheduled_noop_rate": (scheduled_noop / len(scheduled)) if scheduled else None,
        "guard_message_turns": len(guard_turns),
        "guard_reply_rate": (guard_replied / len(guard_turns)) if guard_turns else None,
        "data_gaps": data_gaps,
        "reduced_confidence_turns": reduced,
        "cost_usd": cost,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
    }


def evaluate_assertion(
    assertion: Assertion,
    variant_turns: list[dict],
    baseline_turns: list[dict] | None = None,
) -> AssertionResult:
    v = _scoped(variant_turns, assertion)
    b = _scoped(baseline_turns or [], assertion)
    vs = summarize_turns(v)
    bs = summarize_turns(b) if b else {}

    metric = assertion.metric
    expect = assertion.expect
    actual = vs.get(metric)
    baseline_val = bs.get(metric)

    detail: dict[str, Any] = {
        "metric": metric,
        "actual": actual,
        "baseline": baseline_val,
        "expect": expect,
        "n_variant": len(v),
        "n_baseline": len(b),
    }

    needs_baseline = any(
        k in expect
        for k in ("gt_baseline", "lt_baseline", "gte_baseline", "lte_baseline")
    )
    if len(v) == 0:
        detail["reason"] = "no variant turns in assertion scope"
        return AssertionResult(assertion.id, assertion.description, None, detail, "na")
    if needs_baseline and (len(b) == 0 or baseline_val is None):
        detail["reason"] = "no baseline value in assertion scope"
        return AssertionResult(assertion.id, assertion.description, None, detail, "na")
    if actual is None and "eq" not in expect:
        detail["reason"] = "metric undefined for variant scope"
        return AssertionResult(assertion.id, assertion.description, None, detail, "na")

    passed = False
    if "eq" in expect:
        passed = actual == expect["eq"]
    elif "gte" in expect:
        passed = actual is not None and actual >= expect["gte"]
    elif "lte" in expect:
        passed = actual is not None and actual <= expect["lte"]
    elif "gt_baseline" in expect and expect["gt_baseline"]:
        passed = actual is not None and baseline_val is not None and actual > baseline_val
    elif "lt_baseline" in expect and expect["lt_baseline"]:
        passed = actual is not None and baseline_val is not None and actual < baseline_val
    elif "lte_baseline" in expect and expect["lte_baseline"]:
        passed = actual is not None and baseline_val is not None and actual <= baseline_val
    elif "gte_baseline" in expect and expect["gte_baseline"]:
        passed = actual is not None and baseline_val is not None and actual >= baseline_val
    else:
        detail["error"] = "unknown expect keys"
        passed = False

    return AssertionResult(
        id=assertion.id,
        description=assertion.description,
        passed=passed,
        detail=detail,
        status="pass" if passed else "fail",
    )


def _scoped(turns: list[dict], assertion: Assertion) -> list[dict]:
    out = turns
    if assertion.trigger:
        out = [t for t in out if t.get("trigger") == assertion.trigger]
    if assertion.shifts:
        allowed = set(str(s) for s in assertion.shifts)
        out = [t for t in out if str(t.get("shift_id")) in allowed]
    return out


def changed_decisions(
    baseline: list[dict], variant: list[dict]
) -> list[dict]:
    """Operational + cosmetic decision diffs keyed by turn number."""
    bmap = {t["turn"]: t for t in baseline}
    changes = []
    for t in variant:
        b = bmap.get(t["turn"])
        if not b:
            continue
        if b.get("decision") != t.get("decision"):
            tier = "operational" if _operational_change(b, t) else "cosmetic"
            changes.append({
                "turn": t["turn"],
                "trigger": t.get("trigger"),
                "baseline_decision": b.get("decision"),
                "variant_decision": t.get("decision"),
                "tier": tier,
                "confidence": t.get("confidence"),
                "divergence": t.get("divergence_vs_baseline"),
            })
    return changes


def _operational_change(b: dict, v: dict) -> bool:
    if bool(b.get("escalations")) != bool(v.get("escalations")):
        return True
    if b.get("trigger") == "guard_message":
        b_msg = bool(b.get("messages"))
        v_msg = bool(v.get("messages"))
        if b_msg != v_msg:
            return True
    if b.get("decision") != v.get("decision") and (
        "escalate" in (b.get("decision"), v.get("decision"))
        or b.get("trigger") == "scheduled_check_in"
    ):
        return True
    return False


def trajectory_label(baseline_tools: list[str], variant_tools: list[str]) -> str:
    """Descriptive only — never used as a quality score."""
    b, v = list(baseline_tools), list(variant_tools)
    if b == v:
        return "strict"
    if sorted(b) == sorted(v):
        return "unordered"
    bs, vs = set(b), set(v)
    if vs <= bs:
        return "subset"
    if bs <= vs:
        return "superset"
    return "divergent"
