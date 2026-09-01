"""Session D: run same-model control vs patched variant; scorers own the booleans.

Reuse cli.run_jobs + harness.recipes. Historical baseline is not the control arm.
"""

from __future__ import annotations

from harness.agent.errors import SessionTodo
from harness.agent.types import Diagnosis, PatchPlan, ScoreCard


def evaluate_diagnosis(
    diagnosis: Diagnosis,
    patch: PatchPlan,
    *,
    control_variant: str = "variants/baseline",
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    dry_run: bool = False,
) -> ScoreCard:
    raise SessionTodo(
        "D",
        "harness.agent.evaluate.evaluate_diagnosis",
        hint="Targeted recipe/scorer from diagnosis; holdout from catalog; fill ScoreCard.",
    )
