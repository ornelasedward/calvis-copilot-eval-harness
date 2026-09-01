"""Process-spec checks on frozen copilot turns.

Improvement on a static shift JSON is: the variant satisfies more of this spec
than the same-model control, on the same card turns. Later guard events are
not a metric — the conversation did not actually happen under the new prompt.
"""

from __future__ import annotations

from typing import Any

from harness.agent.types import ProcessSpec


def _short(name: str) -> str:
    return (name or "").replace("mcp__calvis__", "")


def tools_on_turn(turn: dict[str, Any]) -> set[str]:
    return {_short(u.get("tool") or "") for u in (turn.get("tools_used") or [])}


def dm_bodies(turn: dict[str, Any]) -> list[str]:
    out = []
    for m in turn.get("messages") or []:
        if isinstance(m, str):
            out.append(m)
            continue
        out.append(m.get("body") or m.get("message") or m.get("text") or "")
    return out


def clause_results(turn: dict[str, Any], spec: ProcessSpec) -> dict[str, bool]:
    """One frozen turn vs the spec. Copilot process only."""
    if spec.score_scope != "copilot_process":
        raise ValueError("ProcessSpec.score_scope must be copilot_process")
    tools = tools_on_turn(turn)
    bodies = dm_bodies(turn)
    blob = " ".join(bodies).lower()
    n_dm = len(bodies)
    results: dict[str, bool] = {}
    for tool in spec.must_call:
        results[f"must_call:{_short(tool)}"] = _short(tool) in tools
    for tool in spec.must_not_call:
        results[f"must_not_call:{_short(tool)}"] = _short(tool) not in tools
    if spec.max_dms is not None:
        results[f"max_dms:{spec.max_dms}"] = n_dm <= spec.max_dms
    if spec.min_dms is not None:
        results[f"min_dms:{spec.min_dms}"] = n_dm >= spec.min_dms
    if spec.require_escalation:
        results["require_escalation"] = bool(turn.get("escalations"))
    for s in spec.forbid_substrings:
        results[f"forbid:{s[:40]}"] = s.lower() not in blob
    return results


def turn_passes(turn: dict[str, Any], spec: ProcessSpec) -> bool:
    rows = clause_results(turn, spec)
    return all(rows.values()) if rows else True


def spec_rate(turns: list[dict[str, Any]], spec: ProcessSpec) -> float | None:
    """Fraction of frozen turns that pass every clause. None if no turns."""
    if not turns:
        return None
    return sum(1 for t in turns if turn_passes(t, spec)) / len(turns)
