"""Summarize Variant A quietness vs action-preservation on labeled groups."""
from __future__ import annotations

import json
from pathlib import Path

from harness.store import ExperimentStore

GROUPS = {
    "safe_to_noop": {
        "55252": [3, 5],
        "50837": [8, 9],
        "50737": [26],
    },
    "discretionary": {
        "56370": [4, 6, 8],
        "55252": [2],
        "50737": [10],
    },
    "must_act": {
        "55252": [4, 6, 9],
        "53658": [2, 7],
    },
}


def stats(turns: list[dict]) -> dict:
    n = len(turns)
    noop = sum(1 for t in turns if t.get("decision") in ("noop", "no_op", "note_only"))
    dm = sum(1 for t in turns if t.get("messages"))
    # "acted" = DM or escalation (operational action)
    acted = sum(
        1
        for t in turns
        if t.get("messages") or t.get("escalations") or t.get("decision") == "escalate"
    )
    esc = sum(len(t.get("escalations") or []) for t in turns)
    return {
        "turns": n,
        "noop_rate": (noop / n) if n else None,
        "dm_rate": (dm / n) if n else None,
        "acted_rate": (acted / n) if n else None,
        "escalations": esc,
        "decisions": [t.get("decision") for t in turns],
        "turns_detail": [
            {
                "turn": t.get("turn"),
                "trigger": t.get("trigger"),
                "decision": t.get("decision"),
                "dms": len(t.get("messages") or []),
                "escalations": len(t.get("escalations") or []),
                "instr": t.get("selected_instruction"),
            }
            for t in turns
        ],
    }


def main() -> None:
    store = ExperimentStore(Path("runs"))
    out: dict = {}
    for group, by_shift in GROUPS.items():
        ctrl_all: list[dict] = []
        vara_all: list[dict] = []
        print(f"\n=== {group} ===")
        for shift, want in by_shift.items():
            c = [
                t
                for t in store.load_turns(f"ctrl_a_{group}_{shift}", shift)
                if int(t.get("turn", -1)) in want
            ]
            v = [
                t
                for t in store.load_turns(f"vara_{group}_{shift}", shift)
                if int(t.get("turn", -1)) in want
            ]
            cs, vs = stats(c), stats(v)
            print(f"  shift {shift} control={cs} variant={vs}")
            ctrl_all.extend(c)
            vara_all.extend(v)
        cg, vg = stats(ctrl_all), stats(vara_all)
        out[group] = {"control": cg, "variant_a": vg}
        print(f"  GROUP control noop={cg['noop_rate']} dm={cg['dm_rate']} esc={cg['escalations']}")
        print(f"  GROUP variant noop={vg['noop_rate']} dm={vg['dm_rate']} esc={vg['escalations']}")
    Path("runs/a_group_summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\nwrote runs/a_group_summary.json")


if __name__ == "__main__":
    main()
