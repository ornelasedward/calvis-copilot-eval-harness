"""Run Variant A group tests: control vs A on labeled turn sets (Sol)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def run(variant: str, run_id: str, shift: str, turns: list[int]) -> None:
    cmd = [
        sys.executable,
        "cli.py",
        "run",
        "--variant",
        variant,
        "--mode",
        "turn",
        "--shifts",
        shift,
        "--turns",
        *[str(t) for t in turns],
        "--adapter",
        "openai",
        "--model",
        "gpt-5.6-sol",
        "--run-id",
        run_id,
    ]
    print(">>", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def main() -> None:
    for group, by_shift in GROUPS.items():
        for shift, turns in by_shift.items():
            run(
                "variants/baseline",
                f"ctrl_a_{group}_{shift}",
                shift,
                turns,
            )
            run(
                "variants/variant_a",
                f"vara_{group}_{shift}",
                shift,
                turns,
            )


if __name__ == "__main__":
    main()
