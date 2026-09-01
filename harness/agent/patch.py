"""Session C: one-file prompt patch from a Diagnosis.

Copy variants/baseline → variants/auto_<stamp>/, edit exactly one markdown
file, return PatchPlan. Refuse a second file. Prefer an ordered rule.
"""

from __future__ import annotations

from harness.agent.errors import SessionTodo
from harness.agent.types import Diagnosis, PatchPlan


def apply_patch(
    diagnosis: Diagnosis,
    *,
    parent_variant: str = "variants/baseline",
    skip_llm: bool = True,
) -> PatchPlan:
    raise SessionTodo(
        "C",
        "harness.agent.patch.apply_patch",
        hint="One file only; write variants/auto_<stamp>/; return unified diff.",
    )
