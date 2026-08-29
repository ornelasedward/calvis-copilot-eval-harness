"""Review A3 shift-ending and probe quietness candidates."""
from __future__ import annotations

import json
from pathlib import Path

from harness.store import ExperimentStore


def review_endings() -> None:
    store = ExperimentStore(Path("runs"))
    pairs = [
        ("ctrl_sol_55252_full", "vara3_sol_55252_full_r1"),
        ("ctrl_sol_55252_full_r2", "vara3_sol_55252_full_r2"),
        ("ctrl_sol_55252_full_r3", "vara3_sol_55252_full_r3"),
    ]
    print("=== SHIFT-ENDING REVIEW (t10-11) ===")
    for c, v in pairs:
        ct = {int(t["turn"]): t for t in store.load_turns(c, "55252")}
        vt = {int(t["turn"]): t for t in store.load_turns(v, "55252")}
        print(f"\n-- {c} vs {v} --")
        for tid in (10, 11):
            c1, v1 = ct[tid], vt[tid]
            c_esc = bool(c1.get("escalations")) or c1.get("decision") == "escalate"
            v_esc = bool(v1.get("escalations")) or v1.get("decision") == "escalate"
            miss = c_esc and not v_esc
            print(
                f" t{tid}: ctrl={c1.get('decision')} a3={v1.get('decision')} "
                f"missed_esc={miss} instr={v1.get('selected_instruction')}"
            )
            if c1.get("escalations"):
                e = c1["escalations"][0]
                print(f"   ctrl esc: {e.get('kind')} {(e.get('details') or '')[:140]}")
            if v1.get("messages"):
                m = v1["messages"][0]
                body = m.get("body") or m.get("message") or ""
                print(f"   a3 dm: {body[:140]}")
            if v1.get("notes"):
                print(f"   a3 note: {str(v1['notes'][0])[:140]}")
            # prior A3 escalations before ending
        prior_esc = sum(
            1
            for t in sorted(vt.values(), key=lambda x: int(x["turn"]))
            if int(t["turn"]) < 10
            and (t.get("escalations") or t.get("decision") == "escalate")
        )
        print(f"   a3 escalations before t10: {prior_esc}")


def summarize_probe(runs: list[tuple[str, str, str, int]]) -> None:
    store = ExperimentStore(Path("runs"))
    print("\n=== QUIETNESS PROBES ===")
    print(f"{'shift':6} {'turn':4} {'ctrl':14} {'a3':14} lift?")
    for ctrl_run, a3_run, sid, tid in runs:
        cp = Path("runs") / ctrl_run / "results" / f"{sid}.jsonl"
        vp = Path("runs") / a3_run / "results" / f"{sid}.jsonl"
        if not cp.exists() or not vp.exists():
            print(sid, tid, "MISSING")
            continue
        c = next(t for t in store.load_turns(ctrl_run, sid) if int(t["turn"]) == tid)
        v = next(t for t in store.load_turns(a3_run, sid) if int(t["turn"]) == tid)
        cdm = bool(c.get("messages"))
        vdm = bool(v.get("messages"))
        lift = cdm and not vdm and v.get("decision") in ("no_op", "note_only")
        print(
            f"{sid:6} {tid:<4} {c.get('decision'):14} {v.get('decision'):14} "
            f"{'YES' if lift else 'no'}"
        )


if __name__ == "__main__":
    review_endings()
