"""Prompt compiler: rebuilds the production prompt assembly from a variant dir.

A "variant" is any directory containing `core/` and `instructions/` mirroring
the bundle's `prompts/`. `ASSEMBLED_SYSTEM_PROMPT.md` and `turn_message/` are
treated strictly as generated validation fixtures; golden tests compare this
compiler's output against them.

Assembly rules recovered from the bundle (verified by golden tests):

* System prompt: the six `core/` files concatenated in a fixed order, joined
  by one blank line, with `{COPILOT_CONTEXT}` substituted at build time.
* Turn message: session preamble, first-turn job context, exactly one
  `instructions/` file embedded verbatim (trailing newline preserved, which
  yields the two blank lines seen after it in every render), optional
  per-turn sections, then the turn header with server-computed local times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CORE_ORDER = [
    "identity",
    "context",
    "holding_the_post",
    "obligations",
    "comms_policy",
    "tools",
]

# PROMPTS.md, "Which instruction file arrives is chosen by what woke the agent"
TRIGGER_TO_INSTRUCTION = {
    "session_start": "session_start.md",
    "guard_message": "guard_response.md",
    "operator_message": "operator_message.md",
    "approval_decision": "approval_decision.md",
    "scheduled_check_in": "scheduled_check_in.md",
    "obligation_due": "obligation_due.md",
    "job_event": "job_event.md",
    "guard_in_transit": "job_event.md",
    "guard_checked_in": "job_event.md",
}
DEFAULT_INSTRUCTION = "default.md"

CONTEXT_PLACEHOLDER = "{COPILOT_CONTEXT}"

CURRENT_TIME_LABEL = (
    "**Current time (authoritative — use this for any time-of-day reasoning, "
    "not the session-start time above):**"
)


@dataclass(frozen=True)
class GuardRef:
    name: str
    id: str | int


def instruction_file_for(trigger: str) -> str:
    return TRIGGER_TO_INSTRUCTION.get(trigger, DEFAULT_INSTRUCTION)


def compile_system_prompt(variant_dir: str | Path, copilot_context: str) -> str:
    """Concatenate core files and fill the context placeholder. Passing the
    literal placeholder as `copilot_context` reproduces the checked-in
    ASSEMBLED_SYSTEM_PROMPT.md (modulo its generated-file header comment)."""
    core = Path(variant_dir) / "core"
    parts = [
        (core / f"{name}.md").read_text(encoding="utf-8").strip("\n")
        for name in CORE_ORDER
    ]
    assembled = "\n\n".join(parts) + "\n"
    return assembled.replace(CONTEXT_PLACEHOLDER, copilot_context)


def render_copilot_context(shift_context: dict) -> str:
    """The shift briefing that fills {COPILOT_CONTEXT}.

    The bundle ships no rendered fixture for this section -- the README only
    says "everything under `shift` in a shift JSON is the same material" -- so
    this readable rendering is our own choice, documented here.
    """
    site = shift_context.get("site", {})
    guard = shift_context.get("guard", {})
    lines = [
        f"Shift {shift_context.get('id')}",
        f"Window: {shift_context.get('start')} → {shift_context.get('end')} "
        f"({shift_context.get('timezone')}, {shift_context.get('duration_hours')}h)",
        f"Scheduled wake interval: every {shift_context.get('wake_interval_minutes')} minutes",
        "",
        "Site",
        f"- Account: {site.get('account')}",
        f"- Address: {site.get('address')}",
        f"- Coordinates: {site.get('lat')}, {site.get('lng')}",
        f"- Geofence radius: {site.get('geofence_radius_m')} m",
    ]

    instructions = (shift_context.get("instructions") or {}).get("content")
    if instructions:
        lines += ["", "Job instructions:", "", instructions.strip("\n")]

    lines += [
        "",
        f"Guard: {guard.get('name')}",
        f"- Prior shifts for this account: {guard.get('prior_shifts_for_account')}",
        f"- Prior shifts total: {guard.get('prior_shifts_total')}",
    ]
    if guard.get("copilot_summary"):
        lines.append(f"- Copilot summary: {guard['copilot_summary']}")
    for note in guard.get("notes") or []:
        lines.append(f"- Note: {note}")

    if shift_context.get("account_summary"):
        lines += ["", f"Account summary: {shift_context['account_summary']}"]

    site_notes = shift_context.get("site_notes") or []
    if site_notes:
        lines += ["", "Site notes:"]
        for note in site_notes:
            written = note.get("written", "")
            content = note.get("content", "")
            lines.append(f"- [{written}] {content}")

    return "\n".join(lines)


def _session_preamble(session_id: str, job_id: str, guards: list[GuardRef]) -> str:
    guard_list = ", ".join(f"{g.name} (id `{g.id}`)" for g in guards)
    return (
        "## Session\n"
        f"- **Session ID:** `{session_id}`\n"
        f"- **Job ID:** `{job_id}` — pass as `job_id` to job-scoped data tools "
        "(get_guard_locations, get_job_logs, get_job_incidents, get_site_history)\n"
        f"- **Assigned guard(s):** {guard_list} — the only confirmed guards on this "
        "shift. Use these guard_id(s) for guard-scoped tools and DMs; treat anyone "
        "else (e.g. a stale ping from a guard since removed) as not on this shift.\n"
        "- Use this session_id for all copilot tool calls (create_copilot_task, "
        "request_copilot_dm, add_copilot_note, get_copilot_context)"
    )


def _job_context(job_id: str, account: str, address: str, guards: list[GuardRef]) -> str:
    names = ", ".join(g.name for g in guards)
    return (
        "## Job Context\n"
        f"- Job #{job_id}: {account}\n"
        f"- Location: {address}\n"
        f"- Guards: {names}\n"
        "- Full details in context/job.json"
    )


def _turn_header(
    turn: int,
    trigger: str,
    ts: datetime,
    shift_start: datetime,
    shift_end: datetime,
    tz_name: str,
) -> str:
    local = ts.astimezone(ZoneInfo(tz_name))
    current = f"{local.strftime('%A')} {local.strftime('%Y-%m-%dT%H:%M:%S')} {tz_name}"
    if ts < shift_start:
        label = "Minutes until shift start"
        minutes = round((shift_start - ts).total_seconds() / 60)
    else:
        label = "Time left on shift"
        minutes = round((shift_end - ts).total_seconds() / 60)
    return (
        f"## Turn {turn} (triggered by: {trigger})\n"
        f"{CURRENT_TIME_LABEL} {current}\n"
        f"**{label} (authoritative):** {minutes} min"
    )


def compile_turn_message(
    variant_dir: str | Path,
    *,
    turn: int,
    trigger: str,
    ts: datetime,
    session_id: str,
    job_id: str,
    guards: list[GuardRef],
    shift_start: datetime,
    shift_end: datetime,
    tz_name: str,
    first_turn: bool = False,
    site_account: str | None = None,
    site_address: str | None = None,
    operator_messages: list[str] | None = None,
    approval_decisions: list[tuple[str, str]] | None = None,
    gate_block: str | None = None,
) -> str:
    """Build the per-wake message. Section order follows PROMPTS.md; joining
    rules (verified against all ten renders): generated sections are separated
    by one blank line; the instruction file keeps its trailing newline, which
    produces the two blank lines that follow it."""
    instruction_path = Path(variant_dir) / "instructions" / instruction_file_for(trigger)
    instruction_raw = instruction_path.read_text(encoding="utf-8")

    parts: list[str] = [_session_preamble(session_id, job_id, guards)]

    if first_turn:
        parts.append(
            _job_context(job_id, site_account or "", site_address or "", guards)
        )

    # Embedded whole and unmodified; keep exactly one trailing newline so the
    # join yields the double blank line every render shows after it.
    parts.append(instruction_raw.rstrip("\n") + "\n")

    if operator_messages:
        body = "\n".join(f"{i}. {m}" for i, m in enumerate(operator_messages, 1))
        parts.append(f"## Operator Messages (new since last turn)\n{body}")

    if approval_decisions:
        body = "\n".join(f"- **{title}**: {decision}" for title, decision in approval_decisions)
        parts.append(f"## Approval Decisions (new since last turn)\n{body}")

    if gate_block:
        parts.append(gate_block.strip("\n"))

    parts.append(_turn_header(turn, trigger, ts, shift_start, shift_end, tz_name))

    return "\n\n".join(parts) + "\n"
