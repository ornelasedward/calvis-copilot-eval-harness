"""Review A3-only escalations and mine discretionary DM candidates."""
from __future__ import annotations

import json
from pathlib import Path

from harness.store import ExperimentStore


def preview_msgs(messages: list[dict], n: int = 140) -> list[str]:
    out = []
    for m in messages or []:
        body = m.get("body") or m.get("message") or m.get("text") or ""
        out.append(body.replace("\n", " ")[:n])
    return out


def main() -> None:
    store = ExperimentStore(Path("runs"))
    pairs = [
        ("ctrl_sol_55252_full", "vara3_sol_55252_full_r1"),
        ("ctrl_sol_55252_full_r2", "vara3_sol_55252_full_r2"),
        ("ctrl_sol_55252_full_r3", "vara3_sol_55252_full_r3"),
    ]
    reviews = []
    for ctrl, a3 in pairs:
        c = {int(t["turn"]): t for t in store.load_turns(ctrl, "55252")}
        v = {int(t["turn"]): t for t in store.load_turns(a3, "55252")}
        print(f"==== {a3} ====")
        for turn in range(1, 12):
            ct, vt = c[turn], v[turn]
            c_esc = bool(ct.get("escalations")) or ct.get("decision") == "escalate"
            v_esc = bool(vt.get("escalations")) or vt.get("decision") == "escalate"
            if v_esc and not c_esc:
                row = {
                    "pair": a3,
                    "turn": turn,
                    "ctrl_decision": ct.get("decision"),
                    "a3_decision": vt.get("decision"),
                    "a3_notes": (vt.get("notes") or [None])[0],
                    "a3_esc": [
                        {"kind": e.get("kind"), "details": (e.get("details") or "")[:280]}
                        for e in (vt.get("escalations") or [])
                    ],
                    "a3_dms": preview_msgs(vt.get("messages") or []),
                    "ctrl_notes": (ct.get("notes") or [None])[0],
                    "ctrl_dms": preview_msgs(ct.get("messages") or []),
                    "a3_gaps": vt.get("data_gaps") or [],
                }
                reviews.append(row)
                print(f"A3-ONLY ESC t{turn}: ctrl={ct.get('decision')} a3={vt.get('decision')}")
                print(f"  a3 notes: {(row['a3_notes'] or '')[:260]}")
                print(f"  a3 esc: {row['a3_esc']}")
                print(f"  ctrl dms: {row['ctrl_dms']}")
                print(f"  a3 dms: {row['a3_dms']}")
            if c_esc and not v_esc:
                print(f"MISSED ESC t{turn}: ctrl={ct.get('decision')} a3={vt.get('decision')}")

    Path("runs/a3_escalation_review.json").write_text(
        json.dumps(reviews, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {len(reviews)} A3-only escalations to runs/a3_escalation_review.json")

    # Mine labels: discretionary where hist sent DM
    labels = json.loads(Path("experiments/scheduled_turn_labels.json").read_text(encoding="utf-8"))
    disc = [
        x
        for x in labels
        if x.get("label") == "discretionary_dm_candidate"
        and x.get("hist_decision") == "send_message"
        and (x.get("esc") or 0) == 0
    ]
    print("\nHistorical discretionary DM candidates (no hist esc):")
    for x in disc[:25]:
        print(f"  {x['shift']} t{x['turn']} dms={x.get('dms')}")


if __name__ == "__main__":
    main()
