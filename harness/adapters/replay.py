"""ReplayAdapter — zero-spend dry run that replays baseline actions.

Used to:
1. Validate the full pipeline without API calls
2. Import the production baseline as run zero in the same trace schema
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from ..loader import Shift, parse_ts
from ..schemas import Usage
from .base import ModelRequest, ModelResponse, ToolCallRequest


@dataclass
class BaselineTurnActions:
    turn: int
    trigger: str
    ts: datetime
    tool_calls: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _short(tool: str) -> str:
    return tool.replace("mcp__calvis__", "")


def iter_baseline_turns(shift: Shift) -> list[BaselineTurnActions]:
    """Slice baseline into per-turn action groups using strict array windows.

    Window for turn i = entries after turn_start[i] and before turn_start[i+1].
    Entries before the first turn_start (session bootstrap) attach to turn 1.

    The ~5s pre-turn_start bleed seen in the audit is intentionally NOT folded
    into these windows. That bleed is a presentation concern and must carry
    association_confidence=`inferred` if shown in a UI — it must not affect
    counts, assertions, or message attribution.

    Messages are taken from request_copilot_dm tool calls in the window, not
    from copilot_message mirrors (mirrors are timestamped earlier due to
    streaming and would double-count across turn boundaries).
    """
    starts = [
        (i, e) for i, e in enumerate(shift.baseline) if e.get("type") == "turn_start"
    ]
    if not starts:
        return []

    turns: list[BaselineTurnActions] = []
    first_idx, _first_entry = starts[0]
    bootstrap = list(shift.baseline[:first_idx])

    for s_i, (idx, entry) in enumerate(starts):
        next_idx = starts[s_i + 1][0] if s_i + 1 < len(starts) else len(shift.baseline)
        window = list(shift.baseline[idx + 1 : next_idx])
        if s_i == 0:
            window = bootstrap + window

        calls = [e for e in window if e.get("type") == "tool_call"]
        msgs = [
            (c.get("input") or {}).get("body")
            for c in calls
            if _short(c.get("tool", "")) == "request_copilot_dm"
            and (c.get("input") or {}).get("body")
        ]

        turns.append(
            BaselineTurnActions(
                turn=entry["turn"],
                trigger=entry["trigger"],
                ts=parse_ts(entry["ts"]),
                tool_calls=calls,
                messages=msgs,
            )
        )
    return turns


class ReplayAdapter:
    """Replays a single baseline turn's tool-call sequence as model output."""

    name = "replay"

    def __init__(self, plan: BaselineTurnActions):
        self.plan = plan
        self._call_i = 0
        self._done = False

    @property
    def capabilities(self) -> dict:
        return {"prompt_caching": False, "seed": True, "network": False}

    def complete(self, request: ModelRequest) -> ModelResponse:
        while self._call_i < len(self.plan.tool_calls):
            raw = self.plan.tool_calls[self._call_i]
            self._call_i += 1
            return ModelResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"replay-{self.plan.turn}-{self._call_i}",
                        name=raw["tool"],
                        input=raw.get("input") or {},
                    )
                ],
                stop_reason="tool_use",
                usage=Usage(),
                raw=raw,
            )

        text = "\n\n".join(self.plan.messages) if self.plan.messages else None
        self._done = True
        return ModelResponse(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(),
            raw={"messages": self.plan.messages},
        )
