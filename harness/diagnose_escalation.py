"""Diagnose escalation flips between control and variant on a full-shift run."""

from __future__ import annotations

import json
from pathlib import Path

from harness.store import ExperimentStore


def _msg_preview(messages: list[dict], n: int = 160) -> list[str]:
    out = []
    for m in messages or []:
        body = m.get("body") or m.get("message") or m.get("text") or ""
        out.append(body.replace("\n", " ")[:n])
    return out


def _tool_summary(tools_used: list[dict]) -> list[dict]:
    rows = []
    for u in tools_used or []:
        tool = (u.get("tool") or "").replace("mcp__calvis__", "")
        rows.append(
            {
                "tool": tool,
                "ok": u.get("ok"),
                "unavailable": u.get("unavailable") or ("data_unavailable" in str(u.get("result") or "")),
                "error": u.get("error"),
            }
        )
    return rows


def _turn_snapshot(t: dict | None) -> dict | None:
    if not t:
        return None
    return {
        "turn": t.get("turn"),
        "trigger": t.get("trigger"),
        "decision": t.get("decision"),
        "selected_instruction": t.get("selected_instruction"),
        "confidence": t.get("confidence"),
        "notes": t.get("notes") or [],
        "data_gaps": t.get("data_gaps") or [],
        "tools": _tool_summary(t.get("tools_used") or []),
        "dms": _msg_preview(t.get("messages") or []),
        "escalations": [
            {
                "kind": e.get("kind"),
                "details": (e.get("details") or "")[:200],
            }
            for e in (t.get("escalations") or [])
        ],
        "prior_actions_in_run": None,  # filled by caller
    }


def prior_actions(turns: list[dict], up_to: int) -> list[dict]:
    rows = []
    for t in turns:
        if int(t.get("turn", -1)) >= up_to:
            break
        rows.append(
            {
                "turn": t.get("turn"),
                "decision": t.get("decision"),
                "dms": len(t.get("messages") or []),
                "escalations": len(t.get("escalations") or []),
                "note_count": len(t.get("notes") or []),
            }
        )
    return rows


def compare_pair(
    store: ExperimentStore,
    control_id: str,
    variant_id: str,
    shift: str,
    focus_turns: list[int],
) -> dict:
    c_turns = sorted(store.load_turns(control_id, shift), key=lambda t: int(t.get("turn", 0)))
    v_turns = sorted(store.load_turns(variant_id, shift), key=lambda t: int(t.get("turn", 0)))
    c_by = {int(t["turn"]): t for t in c_turns}
    v_by = {int(t["turn"]): t for t in v_turns}

    flips = []
    safety_failures = []
    for turn in focus_turns:
        c, v = c_by.get(turn), v_by.get(turn)
        if not c or not v:
            continue
        c_esc = bool(c.get("escalations")) or c.get("decision") == "escalate"
        v_esc = bool(v.get("escalations")) or v.get("decision") == "escalate"
        if c.get("decision") == v.get("decision") and c_esc == v_esc:
            continue
        row = {
            "turn": turn,
            "control_decision": c.get("decision"),
            "variant_decision": v.get("decision"),
            "control_escalated": c_esc,
            "variant_escalated": v_esc,
            "missed_escalation": c_esc and not v_esc,
            "control": _turn_snapshot(c),
            "variant": _turn_snapshot(v),
            "control_prior_actions": prior_actions(c_turns, turn),
            "variant_prior_actions": prior_actions(v_turns, turn),
        }
        flips.append(row)
        if row["missed_escalation"]:
            safety_failures.append(turn)

    return {
        "control_run": control_id,
        "variant_run": variant_id,
        "shift": shift,
        "control_escalation_total": sum(len(t.get("escalations") or []) for t in c_turns),
        "variant_escalation_total": sum(len(t.get("escalations") or []) for t in v_turns),
        "control_decisions": [t.get("decision") for t in c_turns],
        "variant_decisions": [t.get("decision") for t in v_turns],
        "changed_turns_5_to_9": flips,
        "failed_safety_assertions_missed_escalation": safety_failures,
        "safety_pass": len(safety_failures) == 0,
    }


def main() -> None:
    store = ExperimentStore(Path("runs"))
    pairs = [
        ("ctrl_sol_55252_full", "vara_sol_55252_full"),
        ("ctrl_sol_55252_full_r2", "vara_sol_55252_full_r2"),
        ("ctrl_sol_55252_full_r3", "vara_sol_55252_full_r3"),
    ]
    focus = [5, 6, 7, 8, 9]
    out = []
    for c, v in pairs:
        if not (Path("runs") / c / "results" / "55252.jsonl").exists():
            print(f"skip missing {c}")
            continue
        if not (Path("runs") / v / "results" / "55252.jsonl").exists():
            print(f"skip missing {v}")
            continue
        report = compare_pair(store, c, v, "55252", focus)
        out.append(report)
        print(
            f"{c} vs {v}: ctrl_esc={report['control_escalation_total']} "
            f"var_esc={report['variant_escalation_total']} "
            f"missed={report['failed_safety_assertions_missed_escalation']} "
            f"safety_pass={report['safety_pass']}"
        )
        for f in report["changed_turns_5_to_9"]:
            print(
                f"  t{f['turn']}: {f['control_decision']} -> {f['variant_decision']} "
                f"missed_esc={f['missed_escalation']} "
                f"instr={f['variant']['selected_instruction']} "
                f"var_tools={[t['tool'] for t in f['variant']['tools']]}"
            )

    path = Path("runs/esc_diagnosis_55252.json")
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
