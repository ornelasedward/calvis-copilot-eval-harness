"""Mine existing A3 quietness evidence from prior runs."""
from __future__ import annotations

from pathlib import Path

from harness.store import ExperimentStore

KNOWN = [
    ("probe_ctrl_56370_t35", "probe_a3_56370_t35", "56370", 35),
    ("ctrl_a3_quiet_56370_t35_r1", "vara3_quiet_56370_t35_r1", "56370", 35),
    ("ctrl_a3_quiet_56370_t35_r2", "vara3_quiet_56370_t35_r2", "56370", 35),
    ("ctrl_a3_quiet_56370_t35_r3", "vara3_quiet_56370_t35_r3", "56370", 35),
    ("ctrl_a3_noop_50837", "vara3_noop_50837", "50837", 8),
    ("ctrl_a3_noop_50837", "vara3_noop_50837", "50837", 9),
    ("probe_ctrl_50340_t8", "probe_a3_50340_t8", "50340", 8),
    ("probe_ctrl_50340_t16", "probe_a3_50340_t16", "50340", 16),
    ("probe_ctrl_50833_t13", "probe_a3_50833_t13", "50833", 13),
    ("probe_ctrl_50833_t15", "probe_a3_50833_t15", "50833", 15),
    ("probe_ctrl_56212_t2", "probe_a3_56212_t2", "56212", 2),
    ("probe_ctrl_56212_t16", "probe_a3_56212_t16", "56212", 16),
    ("ctrl_a3_disc_56370", "vara3_disc_56370", "56370", 6),
    ("ctrl_a3_disc_56370", "vara3_disc_56370", "56370", 8),
    ("probe_ctrl_53658_t6", "probe_a3_53658_t6", "53658", 6),
    ("probe_ctrl_55252_t2", "probe_a3_55252_t2", "55252", 2),
    ("probe_ctrl_56370_t4", "probe_a3_56370_t4", "56370", 4),
    ("probe_ctrl_50737_t10", "probe_a3_50737_t10", "50737", 10),
    ("probe_ctrl_58349_t83", "probe_a3_58349_t83", "58349", 83),
]


def main() -> None:
    store = ExperimentStore(Path("runs"))
    lifts = []
    print(f"{'ctrl':28} {'a3':22} {'sid':6} t  {'ctrl_dec':12} {'a3_dec':12} lift")
    for cr, ar, sid, tid in KNOWN:
        if not (Path("runs") / cr / "results" / f"{sid}.jsonl").exists():
            continue
        if not (Path("runs") / ar / "results" / f"{sid}.jsonl").exists():
            continue
        c = next(
            (t for t in store.load_turns(cr, sid) if int(t.get("turn", -1)) == tid),
            None,
        )
        v = next(
            (t for t in store.load_turns(ar, sid) if int(t.get("turn", -1)) == tid),
            None,
        )
        if not c or not v:
            continue
        cdm = bool(c.get("messages"))
        vdm = bool(v.get("messages"))
        lift = cdm and not vdm and v.get("decision") in ("no_op", "note_only")
        if lift:
            lifts.append((sid, tid, cr, ar))
        print(
            f"{cr[:28]:28} {ar[:22]:22} {sid:6} {tid:<2} "
            f"{c.get('decision'):12} {v.get('decision'):12} "
            f"{'YES' if lift else 'no'}"
        )
    print(f"\nLift hits (single-run): {len(lifts)}")
    for row in lifts:
        print(" ", row)


if __name__ == "__main__":
    main()
