"""Provider-neutral schemas for traces, turns, and experiment provenance.

Baseline is reference, not truth. Association of baseline actions to turns is
presentation-only and carries association_confidence; metrics and assertions
must not consume `inferred` associations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


Decision = Literal["send_message", "no_op", "escalate", "note_only"]
Confidence = Literal["full", "reduced"]
AssociationConfidence = Literal["exact", "window", "inferred"]
ToolSource = Literal[
    "reconstructed",
    "fixture",
    "unavailable",
    "workspace",
    "action_recorded",
    "synthetic_scenario",
]
SchemaSource = Literal["recorded", "prompt_text", "approximated", "inferred"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.cost_usd += other.cost_usd
        self.latency_ms += other.latency_ms


@dataclass
class ToolUseRecord:
    tool: str
    input: dict
    output: Any
    source: ToolSource
    schema_source: SchemaSource = "recorded"
    unavailable_reason: str | None = None


@dataclass
class MessageAction:
    body: str
    meta: dict = field(default_factory=dict)
    recipient_guard_id: int | str | None = None


@dataclass
class EscalationAction:
    kind: Literal["ops", "human", "flag"]
    details: str
    input: dict = field(default_factory=dict)


@dataclass
class TurnResult:
    run_id: str
    shift_id: str
    turn: int
    trigger: str
    ts: str
    mode: Literal["turn", "shift", "baseline_import", "scenario"]
    decision: Decision
    messages: list[MessageAction] = field(default_factory=list)
    escalations: list[EscalationAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tools_used: list[ToolUseRecord] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    confidence: Confidence = "full"
    divergence_vs_baseline: float | None = None
    usage: Usage = field(default_factory=Usage)
    reasoning_summary: str | None = None
    selected_instruction: str | None = None
    repetition: int = 0
    association_confidence: AssociationConfidence | None = None
    raw_trace_ref: str | None = None
    raw_events: list[dict] = field(default_factory=list)
    repetition: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunManifest:
    run_id: str
    variant_name: str
    prompt_hash: str
    model: str
    model_params: dict
    adapter: str
    data_version: str
    code_version: str
    mode: str
    shifts: list[str]
    repetitions: int
    created_at: str
    anonymization_note: str = (
        "Replay uses inconsistent anonymized layers from the bundle: "
        "job.json geography differs from shift.site+pings; render IDs "
        "differ from fixture IDs. Served as recorded fixtures / shift.site "
        "respectively; no claim about production geography."
    )
    tool_fixture_mode: str = "exact_or_unavailable"
    totals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_decision(
    messages: list[MessageAction],
    escalations: list[EscalationAction],
    notes: list[str],
) -> Decision:
    if escalations:
        return "escalate"
    if messages:
        return "send_message"
    if notes:
        return "note_only"
    return "no_op"


def iso(ts: datetime) -> str:
    return ts.isoformat()
