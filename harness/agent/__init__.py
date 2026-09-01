"""Eval-loop agent: mine shift JSON → diagnose → patch → score → keep/revert.

See LOOP.md. Public surface is types, catalog, policy, and run_loop.
"""

from harness.agent.catalog import CLASS_CATALOG, SESSION_MAP
from harness.agent.orchestrator import loop_plan, run_loop
from harness.agent.policy import assess_lift, decide
from harness.agent.spec import spec_rate
from harness.agent.types import (
    Diagnosis,
    LoopConfig,
    LoopDecision,
    PatchPlan,
    ProblemCard,
    ProcessSpec,
    ScoreCard,
)

__all__ = [
    "CLASS_CATALOG",
    "SESSION_MAP",
    "Diagnosis",
    "LoopConfig",
    "LoopDecision",
    "PatchPlan",
    "ProblemCard",
    "ProcessSpec",
    "ScoreCard",
    "assess_lift",
    "decide",
    "loop_plan",
    "run_loop",
    "spec_rate",
]
