"""Replay engine — turn mode and shift mode.

Turn mode: threads baseline conversation history (fast inner loop).
Shift mode: threads the variant's own messages; guard replies stay frozen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .adapters.base import ModelAdapter, ModelMessage, ModelRequest, ToolDef, ToolResultMessage
from .adapters.replay import BaselineTurnActions, ReplayAdapter, iter_baseline_turns
from .loader import Shift
from .prompts import GuardRef, compile_system_prompt, compile_turn_message, render_copilot_context
from .schemas import (
    EscalationAction,
    MessageAction,
    TurnResult,
    Usage,
    normalize_decision,
)
from .thread import ThreadManager
from .tools_sim import ACTION_TOOLS, ToolSimulator, Workspace, _short


# Minimal tool defs so live adapters can call the simulator. Schemas are
# intentionally loose — exact validation is not the adapter's job.
def default_tool_defs() -> list[ToolDef]:
    names = [
        "Read", "Write", "Glob", "Grep", "ToolSearch",
        "mcp__calvis__get_guard_locations",
        "mcp__calvis__get_job_logs",
        "mcp__calvis__get_site_history",
        "mcp__calvis__get_copilot_message_history",
        "mcp__calvis__get_job_chat_messages",
        "mcp__calvis__get_copilot_context",
        "mcp__calvis__get_job_communications",
        "mcp__calvis__get_guard_status",
        "mcp__calvis__get_job_incidents",
        "mcp__calvis__get_entity_summary",
        "mcp__calvis__fetch_chat_image",
        "mcp__calvis__get_open_obligations",
        "mcp__calvis__request_copilot_dm",
        "mcp__calvis__add_copilot_note",
        "mcp__calvis__escalate_to_ops",
        "mcp__calvis__escalate_to_human",
        "mcp__calvis__flag_copilot_guard",
        "mcp__calvis__create_copilot_alert",
        "mcp__calvis__create_copilot_task",
        "mcp__calvis__create_feature_request",
        "mcp__calvis__save_chat_image",
    ]
    defs = []
    for n in names:
        defs.append(ToolDef(
            name=n,
            description=f"Calvis tool `{_short(n)}`",
            input_schema={"type": "object", "additionalProperties": True},
        ))
    return defs


def _guards_for(shift: Shift) -> list[GuardRef]:
    roster = shift.fixtures.guard_roster()
    if roster:
        return [GuardRef(name=g["name"], id=g["id"]) for g in roster]
    g = shift.context.get("guard") or {}
    return [GuardRef(name=g.get("name") or "Unknown", id="unknown")]


def simple_divergence(a: str | None, b: str | None) -> float:
    """Cheap token-Jaccard divergence in [0, 1]. 0 = identical, 1 = disjoint."""
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not ta and not tb:
        return 0.0
    return 1.0 - (len(ta & tb) / len(ta | tb))


@dataclass
class EngineConfig:
    variant_dir: Path
    mode: Literal["turn", "shift"] = "shift"
    max_tool_iters: int = 20
    divergence_threshold: float = 0.45
    allow_empty_obligations: bool = False
    model_params: dict | None = None


class ReplayEngine:
    def __init__(
        self,
        shift: Shift,
        adapter: ModelAdapter,
        config: EngineConfig,
        run_id: str = "adhoc",
    ):
        self.shift = shift
        self.adapter = adapter
        self.config = config
        self.run_id = run_id
        self.thread = ThreadManager(shift)
        self.workspace = Workspace()
        self.workspace.seed_from_fixtures(shift)
        self.system_prompt = compile_system_prompt(
            config.variant_dir,
            render_copilot_context(shift.context),
        )
        self.session_id = shift.fixtures.session_id() or "replay-session"
        self.guards = _guards_for(shift)
        self._reduced_confidence = False
        self._baseline_by_turn = {
            t.turn: t for t in iter_baseline_turns(shift)
        }

    def run_turn(self, turn: int, trigger: str, ts: datetime) -> TurnResult:
        # Independent turn-mode cases must not inherit divergence state.
        if self.config.mode == "turn":
            self._reduced_confidence = False

        first_turn = trigger == "session_start"
        from .prompts import instruction_file_for
        selected_instruction = instruction_file_for(trigger)
        turn_message = compile_turn_message(
            self.config.variant_dir,
            turn=turn,
            trigger=trigger,
            ts=ts,
            session_id=self.session_id,
            job_id=self.shift.id,
            guards=self.guards,
            shift_start=self.shift.start,
            shift_end=self.shift.end,
            tz_name=self.shift.timezone,
            first_turn=first_turn,
            site_account=self.shift.context["site"].get("account"),
            site_address=self.shift.context["site"].get("address"),
        )

        history = self.thread.as_model_messages(ts, mode=self.config.mode)
        messages = [
            ModelMessage(role=m["role"], content=m["content"]) for m in history
        ]
        messages.append(ModelMessage(role="user", content=turn_message))

        sim = ToolSimulator(
            shift=self.shift,
            as_of=ts,
            workspace=self.workspace,
            allow_empty_obligations=self.config.allow_empty_obligations,
        )

        usage = Usage()
        final_text: str | None = None
        raw_events: list[dict] = []

        for _ in range(self.config.max_tool_iters):
            req = ModelRequest(
                system=self.system_prompt,
                messages=messages,
                tools=default_tool_defs(),
                params=self.config.model_params or {},
            )
            resp = self.adapter.complete(req)
            usage.add(resp.usage)
            raw_events.append({
                "type": "model_response",
                "text": resp.text,
                "tool_calls": [
                    {"id": t.id, "name": t.name, "input": t.input}
                    for t in resp.tool_calls
                ],
                "stop_reason": resp.stop_reason,
            })

            if resp.tool_calls:
                # Assistant message with tool calls, then tool results.
                messages.append(ModelMessage(
                    role="assistant",
                    content=resp.text,
                    tool_calls=resp.tool_calls,
                ))
                for tc in resp.tool_calls:
                    result = sim.call(tc.name, tc.input)
                    content = result.output
                    if not isinstance(content, str):
                        import json
                        content = json.dumps(content, default=str)
                    messages.append(ModelMessage(
                        role="tool",
                        content=content,
                        tool_call_id=tc.id,
                        name=tc.name,
                    ))
                    raw_events.append({
                        "type": "tool_result",
                        "tool": tc.name,
                        "source": result.source,
                        "unavailable_reason": result.unavailable_reason,
                    })
                continue

            final_text = resp.text
            break

        # Collect actions from the simulator.
        messages_out: list[MessageAction] = []
        escalations: list[EscalationAction] = []
        notes: list[str] = []
        for cap in sim.actions:
            short = _short(cap.tool) if cap.tool.startswith("mcp__") else cap.kind
            if short == "request_copilot_dm" or cap.kind == "request_copilot_dm":
                inp = cap.input or {}
                body = (
                    inp.get("body")
                    or inp.get("message")
                    or inp.get("text")
                    or ""
                )
                messages_out.append(MessageAction(
                    body=body,
                    meta=inp.get("meta") or {},
                    recipient_guard_id=(
                        inp.get("recipient_guard_id")
                        or inp.get("guard_id")
                    ),
                ))
                if self.config.mode == "shift" and body:
                    self.thread.record_variant_message(ts, body)
            elif short == "add_copilot_note" or cap.kind == "add_copilot_note":
                notes.append(
                    (cap.input or {}).get("note")
                    or (cap.input or {}).get("content")
                    or (cap.input or {}).get("text")
                    or ""
                )
            elif short in ("escalate_to_ops", "escalate_to_human", "flag_copilot_guard"):
                kind = (
                    "ops" if "ops" in short
                    else "human" if "human" in short
                    else "flag"
                )
                escalations.append(EscalationAction(
                    kind=kind,
                    details=(cap.input or {}).get("details")
                    or (cap.input or {}).get("reason")
                    or "",
                    input=cap.input or {},
                ))

        # If the model produced final text but no DM tool call, treat text as a
        # soft message only when the replay adapter put DM bodies there.
        if not messages_out and final_text and isinstance(self.adapter, ReplayAdapter):
            for part in final_text.split("\n\n"):
                if part.strip():
                    messages_out.append(MessageAction(body=part.strip()))
                    if self.config.mode == "shift":
                        self.thread.record_variant_message(ts, part.strip())

        # Orphan assistant prose: model wrote a guard-facing message as plain
        # text but never called request_copilot_dm. Production only delivers via
        # the tool, so decision stays no_op — but we flag the intent so evals
        # don't confuse "silent" with "spoke without the tool."
        orphan_prose = None
        if not messages_out and final_text and final_text.strip():
            orphan_prose = final_text.strip()
            raw_events.append({
                "type": "orphan_assistant_prose",
                "text": orphan_prose,
                "note": "Assistant text present without request_copilot_dm; not delivered",
            })

        data_gaps = [
            f"{r.tool}: {r.unavailable_reason}"
            for r in sim.records if r.source == "unavailable"
        ]
        if orphan_prose:
            data_gaps.append("orphan_assistant_prose: text_without_dm_tool")

        baseline = self._baseline_by_turn.get(turn)
        baseline_text = "\n\n".join(baseline.messages) if baseline else None
        variant_text = "\n\n".join(m.body for m in messages_out) or None
        div = simple_divergence(baseline_text, variant_text) if baseline else None
        if div is not None and div >= self.config.divergence_threshold:
            self._reduced_confidence = True

        result = TurnResult(
            run_id=self.run_id,
            shift_id=self.shift.id,
            turn=turn,
            trigger=trigger,
            ts=ts.isoformat(),
            mode=self.config.mode,
            decision=normalize_decision(messages_out, escalations, notes),
            messages=messages_out,
            escalations=escalations,
            notes=notes,
            tools_used=sim.records,
            data_gaps=data_gaps,
            confidence="reduced" if self._reduced_confidence else "full",
            divergence_vs_baseline=div,
            usage=usage,
            reasoning_summary=final_text,
            selected_instruction=selected_instruction,
            raw_events=raw_events,
        )
        return result


def import_baseline_as_run(
    shift: Shift,
    variant_dir: Path,
    run_id: str,
) -> list[TurnResult]:
    """Import production baseline into the same TurnResult schema (run zero).

    Uses ReplayAdapter so the path shares code with live replay. Association of
    baseline tools to turns is window-based; callers/UI must label confidence.
    """
    results = []
    workspace = Workspace()
    workspace.seed_from_fixtures(shift)
    thread = ThreadManager(shift)

    for plan in iter_baseline_turns(shift):
        adapter = ReplayAdapter(plan)
        # Lightweight path: don't re-call the model loop; materialize directly
        # from the plan for fidelity and speed. Still use ToolSimulator for
        # action capture consistency when re-executing tool calls is desired.
        messages = [
            MessageAction(body=m) for m in plan.messages
        ]
        # Also pick up DMs only present as tool calls.
        if not messages:
            for c in plan.tool_calls:
                if _short(c.get("tool", "")) == "request_copilot_dm":
                    body = (c.get("input") or {}).get("body")
                    if body:
                        messages.append(MessageAction(
                            body=body,
                            meta=(c.get("input") or {}).get("meta") or {},
                            recipient_guard_id=(c.get("input") or {}).get("recipient_guard_id"),
                        ))

        escalations = []
        notes = []
        for c in plan.tool_calls:
            short = _short(c.get("tool", ""))
            inp = c.get("input") or {}
            if short == "add_copilot_note":
                notes.append(inp.get("note") or inp.get("content") or "")
            elif short == "escalate_to_ops":
                escalations.append(EscalationAction(kind="ops", details=inp.get("details") or "", input=inp))
            elif short == "escalate_to_human":
                escalations.append(EscalationAction(kind="human", details=inp.get("details") or "", input=inp))
            elif short == "flag_copilot_guard":
                escalations.append(EscalationAction(kind="flag", details=inp.get("reason") or "", input=inp))

        results.append(TurnResult(
            run_id=run_id,
            shift_id=shift.id,
            turn=plan.turn,
            trigger=plan.trigger,
            ts=plan.ts.isoformat(),
            mode="baseline_import",
            decision=normalize_decision(messages, escalations, notes),
            messages=messages,
            escalations=escalations,
            notes=notes,
            tools_used=[],
            data_gaps=[],
            confidence="full",
            divergence_vs_baseline=0.0,
            usage=Usage(),
            association_confidence="window",
        ))
        # Silence unused — kept for future shared-path expansion.
        _ = (adapter, workspace, thread, variant_dir)
    return results
