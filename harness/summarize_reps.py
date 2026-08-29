"""Summarize verification behavior across B stability reps."""
from __future__ import annotations

import json
from pathlib import Path

from harness.store import ExperimentStore
from harness.verify_b import verification_stats

VERIFY = {"get_guard_locations", "get_job_logs"}

RUNS = [
    ("ctrl_b_56370_claims", "56370", 10),
    ("ctrl_b_56370_t10_r2", "56370", 10),
    ("ctrl_b_56370_t10_r3", "56370", 10),
    ("varb_56370_claims", "56370", 10),
    ("varb_56370_t10_r2", "56370", 10),
    ("varb_56370_t10_r3", "56370", 10),
    ("ctrl_b_50737_claims", "50737", 6),
    ("ctrl_b_50737_t6_r2", "50737", 6),
    ("ctrl_b_50737_t6_r3", "50737", 6),
    ("varb_50737_claims", "50737", 6),
    ("varb_50737_t6_r2", "50737", 6),
    ("varb_50737_t6_r3", "50737", 6),
]


def main() -> None:
    store = ExperimentStore(Path("runs"))
    for run, sid, tid in RUNS:
        turns = store.load_turns(run, sid)
        t = next((x for x in turns if int(x.get("turn", -1)) == tid), None)
        if not t:
            print(f"{run}: MISSING turn {tid}")
            continue
        tools = {
            (u.get("tool") or "").replace("mcp__calvis__", "")
            for u in (t.get("tools_used") or [])
        }
        verify = bool(tools & VERIFY)
        msgs = t.get("messages") or []
        preview = ""
        if msgs:
            body = msgs[0].get("body") or msgs[0].get("message") or msgs[0].get("text") or ""
            preview = body.replace("\n", " ")[:120]
        print(
            f"{run}: decision={t.get('decision')} verify={verify} "
            f"tools={sorted(tools)} escalations={len(t.get('escalations') or [])} "
            f"msg={preview!r}"
        )

    print("\n--- aggregate by arm ---")
    for label, ids in [
        ("ctrl 56370 t10", ["ctrl_b_56370_claims", "ctrl_b_56370_t10_r2", "ctrl_b_56370_t10_r3"]),
        ("varb 56370 t10", ["varb_56370_claims", "varb_56370_t10_r2", "varb_56370_t10_r3"]),
        ("ctrl 50737 t6", ["ctrl_b_50737_claims", "ctrl_b_50737_t6_r2", "ctrl_b_50737_t6_r3"]),
        ("varb 50737 t6", ["varb_50737_claims", "varb_50737_t6_r2", "varb_50737_t6_r3"]),
    ]:
        rates = []
        for run in ids:
            sid = "56370" if "56370" in run else "50737"
            tid = 10 if sid == "56370" else 6
            turns = [x for x in store.load_turns(run, sid) if int(x.get("turn", -1)) == tid]
            s = verification_stats(turns)
            rates.append(s["verification_rate"])
        print(f"{label}: verification rates={rates} mean={sum(r or 0 for r in rates)/len(rates):.2f}")


if __name__ == "__main__":
    main()
