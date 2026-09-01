"""Session B: pick one mined card and write the intent triple.

Never sets pass/fail. skip_llm must work without an API key.
"""

from __future__ import annotations

from harness.agent.catalog import SEVERITY_RANK, catalog_entry, process_spec_for
from harness.agent.errors import SessionTodo
from harness.agent.types import Diagnosis, ProblemCard


def diagnose(
    cards: list[ProblemCard],
    *,
    skip_llm: bool = True,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
) -> Diagnosis:
    """Select one card. skip_llm uses severity rank + catalog only."""
    if skip_llm:
        return diagnose_deterministic(cards)
    raise SessionTodo(
        "B",
        "harness.agent.diagnose.diagnose",
        hint="LLM path: rank cards from JSON evidence; still must return a catalog scorer.",
    )


def diagnose_deterministic(cards: list[ProblemCard]) -> Diagnosis:
    """Highest severity, then first card. No API."""
    if not cards:
        raise ValueError("diagnose requires at least one ProblemCard")
    for card in cards:
        card.validate()
    chosen = sorted(
        cards,
        key=lambda c: (
            SEVERITY_RANK[c.severity],
            c.shift_id,
            c.turns[0] if c.turns else 0,
        ),
    )[0]
    meta = catalog_entry(chosen.problem_class)
    return Diagnosis(
        card_id=chosen.id,
        shift_id=chosen.shift_id,
        problem_class=chosen.problem_class,
        must_improve=meta["json_signal"],
        must_preserve=[
            "session_start welcome DM",
            "reply to every guard_message",
        ],
        must_not_happen=[
            "missed escalation vs same-model control on holdout",
            "soft DM in place of a required flag",
        ],
        target_file=meta["policy_files"][0],
        scorer=meta["scorer"],
        recipe=meta.get("recipe"),
        holdout_recipe=meta.get("holdout_recipe"),
        spec=process_spec_for(chosen.problem_class),
        rationale=(
            f"Deterministic pick: severity={chosen.severity} "
            f"class={chosen.problem_class} (Session B LLM can override pick, not gates)."
        ),
        mode=meta["mode"],
    )
