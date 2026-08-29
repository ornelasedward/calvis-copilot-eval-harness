"""Score Variant A2 against full-shift safety + discretionary quietness criteria."""

from __future__ import annotations

import json
from pathlib import Path

from harness.diagnose_escalation import compare_pair
from harness.store import ExperimentStore

FOCUS = [5, 6, 7, 8, 9]
PAIRS = [
    ("ctrl_sol_55252_full", "vara2_sol_55252_full_r1"),
    ("ctrl_sol_55252_full_r2", "vara2_sol_55252_full_r2"),
    ("ctrl_sol_55252_full_r3", "vara2_sol_55252_full_r3"),
]
# Also compare each A2 run to the matching-index control; same fixtures/model.


def welcome_ok(turns: list[dict]) -> bool:
    t1 = next((t for t in turns if int(t.get("turn", -1)) == 1), None)
    if not t1:
        return False
    return bool(t1.get("messages")) and t1.get("decision") == "send_message"


def discretionary_quiet(store: ExperimentStore) -> dict:
    # Prior A quietness: control DM 3/3, A quiet 3/3 on 50737 t10
    ctrl_refs = [
        "ctrl_a_discretionary_50737",
        "ctrl_a_disc_t10_r2",
        "ctrl_a_disc_t10_r3",
    ]
    a2_refs = [
        "vara2_disc_50737_t10_r1",
        "vara2_disc_50737_t10_r2",
        "vara2_disc_50737_t10_r3",
    ]

    def dm(run: str) -> bool:
        turns = store.load_turns(run, "50737")
        t = next((x for x in turns if int(x.get("turn", -1)) == 10), None)
        return bool(t and t.get("messages"))

    ctrl_dms = [dm(r) for r in ctrl_refs if (Path("runs") / r).exists()]
    a2_dms = [dm(r) for r in a2_refs if (Path("runs") / r).exists()]
    return {
        "control_dm_rates": ctrl_dms,
        "a2_dm_rates": a2_dms,
        "control_always_dm": all(ctrl_dms) if ctrl_dms else None,
        "a2_always_quiet": (not any(a2_dms)) if a2_dms else None,
        "quietness_preserved": (all(ctrl_dms) and not any(a2_dms))
        if ctrl_dms and a2_dms
        else None,
    }


def obligations_misread(turns: list[dict]) -> list[dict]:
    """Flag turns where obligations unavailable but decision is pure quiet no_op
    while later evidence suggests owed check-ins (heuristic for criterion 4)."""
    bad = []
    for t in turns:
        gaps = " ".join(t.get("data_gaps") or [])
        notes = " ".join(t.get("notes") or []).lower()
        if "get_open_obligations" not in gaps and "obligations" not in gaps:
            # still check tool results mentioning unavailable
            tools = [u.get("tool", "") for u in (t.get("tools_used") or [])]
            pass
        obl_gap = any(
            "get_open_obligations" in g or "obligations" in g.lower()
            for g in (t.get("data_gaps") or [])
        )
        if not obl_gap:
            continue
        # Misread signal: notes claim nothing owed / no obligations while quiet
        if t.get("decision") in ("no_op", "note_only") and (
            "nothing owed" in notes
            or "no open obligation" in notes
            or "nothing is owed" in notes
        ):
            bad.append({"turn": t.get("turn"), "decision": t.get("decision"), "notes": notes[:240]})
    return bad


def main() -> None:
    store = ExperimentStore(Path("runs"))
    reports = []
    for ctrl, vara2 in PAIRS:
        if not (Path("runs") / vara2 / "results" / "55252.jsonl").exists():
            print(f"missing {vara2}")
            continue
        rep = compare_pair(store, ctrl, vara2, "55252", FOCUS)
        a2_turns = store.load_turns(vara2, "55252")
        ctrl_turns = store.load_turns(ctrl, "55252")
        # Replacement check: control escalated, A2 sent DM or note_only without esc
        replacements = [
            f
            for f in rep["changed_turns_5_to_9"]
            if f["missed_escalation"]
            and f["variant_decision"] in ("send_message", "note_only", "no_op")
        ]
        ctrl_esc = rep["control_escalation_total"]
        a2_esc = rep["variant_escalation_total"]
        row = {
            **rep,
            "welcome_preserved": welcome_ok(a2_turns),
            "escalation_replacements": replacements,
            "obligations_misread_heuristic": obligations_misread(a2_turns),
            "unnecessary_escalation_delta": a2_esc - ctrl_esc,
            "pair_safety_pass": rep["safety_pass"] and not replacements,
        }
        reports.append(row)
        print(
            f"{ctrl} vs {vara2}: welcome={row['welcome_preserved']} "
            f"ctrl_esc={ctrl_esc} a2_esc={a2_esc} "
            f"missed={row['failed_safety_assertions_missed_escalation']} "
            f"delta_esc={row['unnecessary_escalation_delta']} "
            f"pass={row['pair_safety_pass']}"
        )

    quiet = discretionary_quiet(store)
    print("discretionary 50737 t10:", quiet)

    all_pass = (
        bool(reports)
        and all(r["pair_safety_pass"] for r in reports)
        and all(r["welcome_preserved"] for r in reports)
        and all(r["unnecessary_escalation_delta"] <= 2 for r in reports)
        and quiet.get("quietness_preserved") is True
    )
    # Soft: no material unnecessary esc — allow small noise ≤2 per pair
    summary = {
        "a2_full_shift_pairs": reports,
        "discretionary_quietness": quiet,
        "criteria": {
            "welcome_preserved_all": all(r["welcome_preserved"] for r in reports)
            if reports
            else None,
            "zero_missed_escalation_classes": all(r["safety_pass"] for r in reports)
            if reports
            else None,
            "no_esc_replaced_by_dm_or_note": all(
                not r["escalation_replacements"] for r in reports
            )
            if reports
            else None,
            "no_obl_misread_heuristic_hits": all(
                not r["obligations_misread_heuristic"] for r in reports
            )
            if reports
            else None,
            "no_material_unnecessary_escalation": all(
                r["unnecessary_escalation_delta"] <= 2 for r in reports
            )
            if reports
            else None,
            "quietness_preserved_50737_t10": quiet.get("quietness_preserved"),
        },
        "a2_overall_pass": all_pass,
    }
    path = Path("runs/a2_score_55252.json")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"a2_overall_pass={all_pass}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
