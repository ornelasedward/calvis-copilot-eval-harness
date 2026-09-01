"""Contracts for the eval-loop agent. JSON-mined problems only."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ProblemClass = Literal[
    # mined from a shift JSON (source="json")
    "unverified_claim",
    "photo_without_inspect",
    "hammer_after_photo",
    "ping_budget",
    "surveillance_voice",
    "pushback_failure",
    "under_escalation",
    # mined from a FAILED deterministic scripted scenario (source="scenario").
    # One class per scenario gate, so the card names the gate that failed.
    "uninspected_photo",
    "rubber_stamped_duplicate",
    "third_ping",
    "threat_language",
    "ignored_partial_credit",
    "reasked_whole_window",
    "premature_window_close",
    "partial_ping_budget",
    "caved_on_pushback",
    "dropped_next_window",
    "pushback_threat",
    "pushback_dm_budget",
    "hostile_surveillance_line",
    "hostile_retaliation",
    "character_judgment_escalation",
]

Severity = Literal["safety", "conduct", "lift", "compliance"]
DecisionAction = Literal["keep", "revert", "next_card", "stop"]
#: Where a card came from. LOOP.md hard rule 1: shift JSON or a failed scripted
#: scenario. Never an LLM-simulated guard (sim-*), never a persona.
CardSource = Literal["json", "scenario"]
CARD_SOURCES: tuple[CardSource, ...] = ("json", "scenario")


JSON_PROBLEM_CLASSES: tuple[ProblemClass, ...] = (
    "unverified_claim",
    "photo_without_inspect",
    "hammer_after_photo",
    "ping_budget",
    "surveillance_voice",
    "pushback_failure",
    "under_escalation",
)

SCENARIO_PROBLEM_CLASSES: tuple[ProblemClass, ...] = (
    "uninspected_photo",
    "rubber_stamped_duplicate",
    "third_ping",
    "threat_language",
    "ignored_partial_credit",
    "reasked_whole_window",
    "premature_window_close",
    "partial_ping_budget",
    "caved_on_pushback",
    "dropped_next_window",
    "pushback_threat",
    "pushback_dm_budget",
    "hostile_surveillance_line",
    "hostile_retaliation",
    "character_judgment_escalation",
)

PROBLEM_CLASSES: tuple[ProblemClass, ...] = (
    *JSON_PROBLEM_CLASSES,
    *SCENARIO_PROBLEM_CLASSES,
)


@dataclass
class ProcessSpec:
    """Hypothetical better behavior, stated as checks on a frozen copilot turn.

    The dataset does not change when the prompt changes. We do not score what
    the guard would have done next. We score whether *this wake*, given the
    same as-of evidence, the copilot took the policy-correct process.

    `not_outcomes` is documentation of claims that are out of bounds.
    """

    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    max_dms: int | None = None
    min_dms: int | None = None
    require_escalation: bool = False
    forbid_substrings: list[str] = field(default_factory=list)
    score_scope: Literal["copilot_process"] = "copilot_process"
    not_outcomes: list[str] = field(default_factory=lambda: [
        "later historical guard replies",
        "whether the guard would send a real photo",
        "real-world coverage or theft outcomes",
    ])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    """Pointers into the shift JSON, or into a failed scenario run.

    Empty evidence invalidates the card. For `source="scenario"` cards the
    citation is the recorded run: which gate failed, in which fixture, in which
    run directory, plus the failing turn's DMs and tools.
    """

    guard_text: str | None = None
    baseline_dms: list[str] = field(default_factory=list)
    baseline_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    event_indexes: list[int] = field(default_factory=list)
    baseline_indexes: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # scenario-only pointers (None / empty for JSON-mined cards)
    scenario_run_id: str | None = None
    fixture: str | None = None
    failed_gate: str | None = None

    def has_citation(self) -> bool:
        return bool(
            self.event_indexes
            or self.baseline_indexes
            or self.guard_text
            or self.baseline_dms
            or self.baseline_tools
            or self.missing_tools
            or self.failed_gate
            or self.scenario_run_id
        )


@dataclass
class ProblemCard:
    id: str
    shift_id: str
    turns: list[int]
    problem_class: ProblemClass
    severity: Severity
    evidence: Evidence
    policy_files: list[str]
    spec: ProcessSpec = field(default_factory=ProcessSpec)
    source: CardSource = "json"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if self.source not in CARD_SOURCES:
            raise ValueError(
                "ProblemCard.source must be 'json' (shift bundle) or 'scenario' "
                "(failed scripted scenario) — no personas, no simulated guards"
            )
        if not self.turns:
            raise ValueError("ProblemCard.turns must cite at least one turn")
        if not self.evidence.has_citation():
            raise ValueError("ProblemCard.evidence must cite the shift JSON")


@dataclass
class Diagnosis:
    """What to improve. Never includes pass/fail."""

    card_id: str
    shift_id: str
    problem_class: ProblemClass
    must_improve: str
    must_preserve: list[str]
    must_not_happen: list[str]
    target_file: str
    scorer: str
    recipe: str | None
    holdout_recipe: str | None
    rationale: str
    spec: ProcessSpec = field(default_factory=ProcessSpec)
    mode: Literal["turn", "shift", "scenario"] = "turn"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PatchPlan:
    variant_dir: str
    changed_file: str
    diff: str
    parent_variant: str = "variants/baseline"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreCard:
    """Booleans from deterministic scorers only.

    Lift is variant_spec_rate > control_spec_rate on the card's frozen turns.
    Isolation pass (variant looks good alone) is not enough — that was Variant C's
    0-lift case and Variant A's isolated-turn illusion.
    """

    targeted_pass: bool
    preserve_pass: bool
    holdout_pass: bool | None
    control_spec_rate: float | None = None
    variant_spec_rate: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    control_run_id: str | None = None
    variant_run_id: str | None = None
    scorer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoopDecision:
    action: DecisionAction
    reason: str
    score: ScoreCard | None = None
    iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass
class LoopConfig:
    shift_id: str
    max_iterations: int = 3
    control_variant: str = "variants/baseline"
    holdout_shift: str = "55252"
    adapter: str = "openai"
    model: str = "gpt-5.6-sol"
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
