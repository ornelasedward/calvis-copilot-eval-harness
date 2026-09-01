"""Session A: mine ProblemCards from shift JSON (events + baseline).

No LLM. No personas. Every card must cite evidence in the file.
"""

from __future__ import annotations

from harness.agent.errors import SessionTodo
from harness.agent.types import ProblemCard


def mine_shift(shift_id: str) -> list[ProblemCard]:
    """Return validated cards for one shifts/<id>.json file.

    Use harness.loader.load_shift and harness.adapters.replay.iter_baseline_turns
    so turn numbers match the rest of the harness.
    """
    raise SessionTodo(
        "A",
        "harness.agent.mine.mine_shift",
        hint="Walk events + baseline windows; emit unverified_claim on 50737 first.",
    )


def mine_all(shift_ids: list[str] | None = None) -> list[ProblemCard]:
    """Mine several shifts. Default: all files under shifts/."""
    raise SessionTodo("A", "harness.agent.mine.mine_all")
