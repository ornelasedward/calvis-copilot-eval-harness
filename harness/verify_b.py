"""Post-run checks for Variant B verification behavior."""

from __future__ import annotations

import json
from pathlib import Path


VERIFY_TOOLS = {
    "mcp__calvis__get_guard_locations",
    "get_guard_locations",
    "mcp__calvis__get_job_logs",
    "get_job_logs",
}


def verification_stats(turns: list[dict]) -> dict:
    guard = [t for t in turns if t.get("trigger") == "guard_message"]
    verified = 0
    for t in guard:
        tools = {
            (u.get("tool") or "").replace("mcp__calvis__", "")
            for u in (t.get("tools_used") or [])
        }
        if "get_guard_locations" in tools or "get_job_logs" in tools:
            verified += 1
    n = len(guard)
    return {
        "guard_message_turns": n,
        "verified_turns": verified,
        "verification_rate": (verified / n) if n else None,
        "reply_rate": (
            sum(1 for t in guard if t.get("messages")) / n if n else None
        ),
        "escalations": sum(len(t.get("escalations") or []) for t in turns),
    }


def main() -> None:
    import argparse
    from harness.store import ExperimentStore

    p = argparse.ArgumentParser()
    p.add_argument("--control", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--shift", required=True)
    args = p.parse_args()
    store = ExperimentStore(Path("runs"))
    c = store.load_turns(args.control, args.shift)
    v = store.load_turns(args.variant, args.shift)
    cs, vs = verification_stats(c), verification_stats(v)
    print("control", json.dumps(cs, indent=2))
    print("variant", json.dumps(vs, indent=2))
    if vs["verification_rate"] is not None and cs["verification_rate"] is not None:
        print(
            "verification lift",
            round(vs["verification_rate"] - cs["verification_rate"], 3),
        )


if __name__ == "__main__":
    main()
