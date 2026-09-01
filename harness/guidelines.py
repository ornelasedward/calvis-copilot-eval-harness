"""Conduct-guidelines measurement layer (GUIDELINES.md + experiments/guidelines.json).

THE GUARDRAIL (non-negotiable)
------------------------------
Every check in this module reads exactly two things:

1. the copilot's own turn record (`turn`, `trigger`, `ts`, `messages`,
   `tools_used`, `escalations`), and
2. shift data with `event.ts <= turn ts`.

Nothing the guard did *after* the turn timestamp is ever read — not a guard
message, not telemetry, not a job_log. `tests/test_guidelines.py` proves it by
truncating each shift immediately after a turn's ts and asserting that neither
situation detection nor any floor result changes.

We never measure the guard. Situations are computed from the shift; the floor is
checked on the copilot's turn.

Layering (GUIDELINES.md):
  * floor      — deterministic, gates PASS/FAIL (this module)
  * judgment   — the scenario questions, emitted as `judgment_items` for the
                 advisory judge. This module never answers them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from harness import lexicon as lex
from harness.loader import Shift, load_shift, parse_ts
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]
GUIDELINES_PATH = ROOT / "experiments" / "guidelines.json"
SHIFTS_DIR = ROOT / "shifts"

# How far back a "wake window" reaches when the previous turn is only seconds
# old. Rapid guard exchanges split one situation across several turns; a turn
# fired 20s after the previous one would otherwise see almost nothing. Never
# reaches forward past the turn ts.
WAKE_WINDOW_MINUTES = 10

# Words allowed between the words of a lexicon phrase ("perimeter is clear"
# still matches the phrase "perimeter clear").
_PHRASE_GAP = 2

_ESCALATION_TOOLS = frozenset(
    {"escalate_to_human", "escalate_to_ops", "flag_copilot_guard", "create_copilot_alert"}
)
_CHECKIN_JOB_LOGS = ("checked in",)
_DEVICE_DARK_JOB_LOGS = ("app terminated", "offline")
_DEVICE_ALIVE_EVENTS = {"telemetry", "location"}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_guidelines(path: str | Path | None = None) -> dict:
    """Parse experiments/guidelines.json (lexicon, scenarios, cross-cutting)."""
    p = Path(path) if path else GUIDELINES_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def scenario_by_id(guidelines: dict, sid: str) -> dict | None:
    for sc in guidelines.get("scenarios") or []:
        if sc.get("id") == sid:
            return sc
    if sid == CROSS_CUTTING_ID:
        return cross_cutting_scenario(guidelines)
    return None


CROSS_CUTTING_ID = "XC"


def cross_cutting_scenario(guidelines: dict) -> dict:
    """The always-on floor (No-Surveillance Line + one ask per window)."""
    return {
        "id": CROSS_CUTTING_ID,
        "name": "cross_cutting",
        "situation": {},  # applies to every copilot turn
        "floor": dict(guidelines.get("cross_cutting") or {}),
        "judgment": [],
        "anchors": [],
    }


def anchor_jobs(guidelines: dict | None = None) -> list[dict]:
    """Recipe jobs: the union of anchor turns per shift, shift order stable."""
    g = guidelines or load_guidelines()
    per_shift: dict[str, set[int]] = {}
    order: list[str] = []
    for sc in g.get("scenarios") or []:
        for a in sc.get("anchors") or []:
            sid = str(a["shift"])
            if sid not in per_shift:
                per_shift[sid] = set()
                order.append(sid)
            per_shift[sid].add(int(a["turn"]))
    return [{"shift": sid, "turns": sorted(per_shift[sid])} for sid in sorted(order)]


def anchor_flags(guidelines: dict | None = None) -> list[dict]:
    """Every anchor with its declared baseline_ok (None when unset)."""
    g = guidelines or load_guidelines()
    rows = []
    for sc in g.get("scenarios") or []:
        for a in sc.get("anchors") or []:
            rows.append(
                {
                    "scenario": sc["id"],
                    "shift": str(a["shift"]),
                    "turn": int(a["turn"]),
                    "baseline_ok": a.get("baseline_ok"),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# lexicon matching
# ---------------------------------------------------------------------------


def _phrase_pattern(phrase: str) -> re.Pattern:
    words = [w for w in re.split(r"[^a-z0-9']+", phrase.lower()) if w]
    if not words:
        return re.compile(r"(?!x)x")
    if len(words) == 1:
        # prefix match on a single token ("injur" -> injured/injury), anchored
        # at a word boundary so "authorized" never fires inside "unauthorized".
        return re.compile(r"\b" + re.escape(words[0]), re.I)
    gap = r"(?:\W+\w+){0,%d}\W+" % _PHRASE_GAP
    body = gap.join(re.escape(w) for w in words)
    return re.compile(r"\b" + body, re.I)


_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _pattern(phrase: str) -> re.Pattern:
    pat = _PATTERN_CACHE.get(phrase)
    if pat is None:
        pat = _phrase_pattern(phrase)
        _PATTERN_CACHE[phrase] = pat
    return pat


# "don't approach them" is the opposite of "approach them". A hit whose lead-in
# carries a negation cue is not an instruction to do the thing.
_NEGATION = re.compile(
    r"(?:\b(?:do ?n'?t|do not|never|no need to|avoid|not)\b|\bstay (?:clear|back|away|visible)\b"
    r"|\bkeep (?:your )?distance\b|\bdo NOT\b)[^.!?]{0,40}$",
    re.I,
)


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[:start]))


def lexicon_hits(text: str, terms: Iterable[str]) -> list[str]:
    """Verbatim matches of `terms` in `text`.

    A term matches when its words appear in order, separated by at most two
    other words (so "perimeter clear" matches "perimeter is clear"). Matching is
    word-boundary anchored, which keeps "authorized" out of "unauthorized", and
    a hit negated in the same clause ("don't approach them") does not count.
    Returns the matched source text, for quoting as evidence.
    """
    out: list[str] = []
    if not text:
        return out
    for term in terms or []:
        for m in _pattern(term).finditer(text):
            if _negated(text, m.start()):
                continue
            out.append(m.group(0))
            break
    return out


def lexicon_terms(guidelines: dict, name: str) -> list[str]:
    return list((guidelines.get("lexicon") or {}).get(name) or [])


# ---------------------------------------------------------------------------
# turn rows
# ---------------------------------------------------------------------------


def row_ts(row: dict) -> datetime:
    ts = row.get("ts")
    if isinstance(ts, datetime):
        return ts
    return parse_ts(str(ts))


def row_tools(row: dict) -> list[str]:
    return lex.tools_used_short(row)


def row_dms(row: dict) -> list[str]:
    """Delivered DM bodies: `messages`, plus any request_copilot_dm tool input.

    Runs record both; a turn row that only carries the tool call still counts.
    """
    out: list[str] = []
    seen: set[str] = set()
    for body in lex.dm_bodies(row):
        if body and body not in seen:
            seen.add(body)
            out.append(body)
    for rec in row.get("tools_used") or []:
        if not isinstance(rec, dict):
            continue
        if lex.tool_short(rec.get("tool") or "") != "request_copilot_dm":
            continue
        body = str((rec.get("input") or {}).get("body") or "")
        if body and body not in seen:
            seen.add(body)
            out.append(body)
    return out


def _tool_inputs(row: dict, tool: str) -> list[dict]:
    out = []
    for rec in row.get("tools_used") or []:
        if isinstance(rec, dict) and lex.tool_short(rec.get("tool") or "") == tool:
            inp = rec.get("input")
            out.append(inp if isinstance(inp, dict) else {})
    for esc in row.get("escalations") or []:
        if isinstance(esc, dict) and (esc.get("input") or {}).get("_tool") == tool:
            out.append(esc.get("input") or {})
    return out


def tool_severities(row: dict, tool: str) -> list[str]:
    """Severity strings recorded for `tool` on this turn (escalations first)."""
    out: list[str] = []
    for esc in row.get("escalations") or []:
        if not isinstance(esc, dict):
            continue
        inp = esc.get("input") or {}
        if isinstance(inp, dict) and inp.get("severity"):
            out.append(str(inp["severity"]).lower())
    for inp in _tool_inputs(row, tool):
        if inp.get("severity"):
            out.append(str(inp["severity"]).lower())
    return out


def baseline_turn_rows(shift: Shift) -> list[dict]:
    """Production turns as store-shaped rows, so the same code scores both."""
    from harness.adapters.replay import iter_baseline_turns

    rows: list[dict] = []
    for t in iter_baseline_turns(shift):
        tools = []
        escalations = []
        for call in t.tool_calls:
            short = lex.tool_short(call.get("tool") or "")
            inp = call.get("input") or {}
            tools.append({"tool": short, "input": inp})
            if short in _ESCALATION_TOOLS:
                escalations.append(
                    {
                        "kind": "human" if short == "escalate_to_human" else "ops",
                        "details": str(inp.get("details") or inp.get("reason") or ""),
                        "input": dict(inp, _tool=short),
                    }
                )
        rows.append(
            {
                "shift_id": shift.id,
                "turn": t.turn,
                "trigger": t.trigger,
                "ts": t.ts.isoformat(),
                "messages": [{"body": m} for m in t.messages],
                "tools_used": tools,
                "escalations": escalations,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# situation detectors
# ---------------------------------------------------------------------------


def _prior_ts(prior_rows: list[dict] | None, ts: datetime, shift: Shift) -> datetime:
    """Timestamp of the copilot's previous turn (shift start when there is none)."""
    prev = shift.start
    for r in prior_rows or []:
        rts = row_ts(r)
        if rts < ts and rts > prev:
            prev = rts
    return prev


def wake_guard_messages(
    shift: Shift, ts: datetime, prev_ts: datetime, *, window_minutes: int = WAKE_WINDOW_MINUTES
) -> list[Any]:
    """Guard messages this turn woke on: (window_start, ts].

    window_start = min(previous turn ts, ts - window_minutes) — never later than
    the previous turn, never reaching past `ts`.
    """
    start = min(prev_ts, ts - timedelta(minutes=window_minutes))
    return list(shift.events.between(start, ts, types={"guard_message"}))


def _msg_text(ev: Any) -> str:
    return str(ev.data.get("text") or "")


def wake_text(events: Iterable[Any]) -> str:
    return "\n".join(_msg_text(e) for e in events)


def guard_silent_minutes(shift: Shift, ts: datetime) -> float:
    """Minutes since the last guard_message at/before ts (since shift start if none)."""
    msgs = shift.events.as_of(ts, types={"guard_message"})
    last = msgs[-1].ts if msgs else shift.start
    if last > ts:
        last = shift.start
    return max(0.0, (ts - last).total_seconds() / 60.0)


def device_dark_minutes(shift: Shift, ts: datetime) -> float:
    """Minutes since the device last showed life at/before ts.

    Life = the latest telemetry or location event. An "app terminated"/offline
    job_log recorded *after* the last such event restarts the dark clock at the
    job_log. Nothing after `ts` is read.
    """
    alive = shift.events.as_of(ts, types=_DEVICE_ALIVE_EVENTS)
    last = alive[-1].ts if alive else shift.start
    for e in shift.events.as_of(ts, types={"job_log"}):
        cat = str(e.data.get("category") or "").lower()
        if any(k in cat for k in _DEVICE_DARK_JOB_LOGS) and e.ts >= last:
            last = e.ts
    return max(0.0, (ts - last).total_seconds() / 60.0)


def no_checkin(shift: Shift, ts: datetime, prior_rows: list[dict] | None = None) -> bool:
    """True when nobody has shown: no `checked in` job_log and no check-in wake
    at/before ts."""
    for e in shift.events.as_of(ts, types={"job_log"}):
        cat = str(e.data.get("category") or "").lower()
        if any(k in cat for k in _CHECKIN_JOB_LOGS):
            return False
    for r in prior_rows or []:
        if row_ts(r) <= ts and r.get("trigger") in ("guard_checked_in", "guard_in_transit"):
            return False
    return True


def minutes_since_shift_start(shift: Shift, ts: datetime) -> float:
    return (ts - shift.start).total_seconds() / 60.0


def guard_silent_since_start(shift: Shift, ts: datetime) -> bool:
    """No guard message between the scheduled start and ts."""
    return not [e for e in shift.events.as_of(ts, types={"guard_message"}) if e.ts >= shift.start]


def image_in_wake(events: Iterable[Any]) -> bool:
    for e in events:
        if e.data.get("image") or e.data.get("image_url") or e.data.get("imageUrl"):
            return True
    return False


def _is_ask_dm(body: str) -> bool:
    """An ask = a request for proof/status (harness.lexicon.is_ask)."""
    return lex.is_ask(body or "")


def _row_asks(row: dict) -> list[str]:
    out = []
    for body in row_dms(row):
        if _is_ask_dm(body):
            out.append(body)
    meta = row.get("meta") or {}
    action = str(meta.get("copilot_action") or "")
    if "ask" in action.lower() and not out:
        out.extend(row_dms(row))
    return out


def open_ask(shift: Shift, ts: datetime, prior_rows: list[dict] | None = None) -> bool:
    """A prior ask is still unanswered as of ts.

    The copilot asked for something on an earlier turn (an ask-like DM, or a DM
    tagged `meta.copilot_action` containing "ask") and no guard message has
    arrived since that turn and at/before ts.
    """
    last_ask: datetime | None = None
    for r in prior_rows or []:
        rts = row_ts(r)
        if rts > ts:
            continue
        if _row_asks(r):
            if last_ask is None or rts > last_ask:
                last_ask = rts
    if last_ask is None:
        return False
    replies = [e for e in shift.events.as_of(ts, types={"guard_message"}) if e.ts > last_ask]
    return not replies


def tool_calls_before(
    tool: str, ts: datetime, prior_rows: list[dict] | None, *, minutes: float | None = None
) -> list[dict]:
    """Prior turns (ts <= turn ts) that called `tool`, optionally within a window."""
    out = []
    for r in prior_rows or []:
        rts = row_ts(r)
        if rts >= ts:
            continue
        if minutes is not None and rts < ts - timedelta(minutes=minutes):
            continue
        if tool in row_tools(r):
            out.append(r)
    return out


def _match_situation(spec: dict, ctx: dict) -> bool:
    """Evaluate one `situation` spec against the pre-computed context."""
    if not spec:
        return True
    if "either" in spec:
        return any(_match_situation(sub, ctx) for sub in spec["either"])
    g = ctx["guidelines"]
    for key, value in spec.items():
        if key == "guard_text_any":
            names = [value] if isinstance(value, str) else list(value)
            if not any(lexicon_hits(ctx["wake_text"], lexicon_terms(g, n)) for n in names):
                return False
        elif key == "guard_text_none":
            names = [value] if isinstance(value, str) else list(value)
            if any(lexicon_hits(ctx["wake_text"], lexicon_terms(g, n)) for n in names):
                return False
        elif key == "trigger_any":
            if ctx["trigger"] not in set(value):
                return False
        elif key == "guard_silent_minutes":
            if ctx["guard_silent_minutes"] < float(value):
                return False
        elif key == "device_dark_minutes":
            if ctx["device_dark_minutes"] < float(value):
                return False
        elif key == "no_checkin":
            if bool(value) != ctx["no_checkin"]:
                return False
        elif key == "guard_silent_since_start":
            if bool(value) != ctx["guard_silent_since_start"]:
                return False
        elif key == "minutes_since_shift_start_min":
            # Start-of-shift window: the turn is inside the first N minutes of
            # the scheduled shift. (`_min` reads as "minutes", not "minimum" —
            # the scenario is "at the start, nobody has shown".)
            if not (0 <= ctx["minutes_since_shift_start"] <= float(value)):
                return False
        elif key == "open_ask":
            if bool(value) != ctx["open_ask"]:
                return False
        elif key == "image_in_wake":
            if bool(value) != ctx["image_in_wake"]:
                return False
        elif key == "recent_tool":
            if not _recent_tool(value, ctx):
                return False
        elif key == "no_recent_tool":
            if _recent_tool({**value, "min_count": 1}, ctx, windowed=True):
                return False
        else:  # unknown key: fail closed rather than silently pass
            return False
    return True


def _recent_tool(spec: dict, ctx: dict, *, windowed: bool = False) -> bool:
    """`recent_tool` / `no_recent_tool`.

    `no_recent_tool` (windowed=True) is a strict time window: no call to `tool`
    in the `minutes` before the turn.

    `recent_tool` counts *every* prior call to `tool` since shift start and
    fires at `min_count`. The `minutes` field is kept as reported evidence, not
    as the gate: the dataset's "escalate once, then update" anchors (50737 t30
    and t32) sit hours after the previous critical and are still the same
    situation, so a strict 60-minute window would miss them.
    """
    tool = str(spec.get("tool") or "")
    minutes = spec.get("minutes")
    min_count = int(spec.get("min_count") or 1)
    if windowed:
        hits = tool_calls_before(tool, ctx["ts"], ctx["prior_rows"], minutes=minutes)
    else:
        hits = tool_calls_before(tool, ctx["ts"], ctx["prior_rows"])
    return len(hits) >= min_count


def situation_context(
    shift: Shift, turn_row: dict, prior_rows: list[dict] | None = None, guidelines: dict | None = None
) -> dict:
    """Every situation signal for one turn, computed as-of the turn ts."""
    g = guidelines or load_guidelines()
    ts = row_ts(turn_row)
    prev = _prior_ts(prior_rows, ts, shift)
    wake = wake_guard_messages(shift, ts, prev)
    return {
        "guidelines": g,
        "ts": ts,
        "prev_turn_ts": prev,
        "prior_rows": list(prior_rows or []),
        "trigger": turn_row.get("trigger"),
        "wake_events": wake,
        "wake_text": wake_text(wake),
        "guard_silent_minutes": guard_silent_minutes(shift, ts),
        "device_dark_minutes": device_dark_minutes(shift, ts),
        "no_checkin": no_checkin(shift, ts, prior_rows),
        "minutes_since_shift_start": minutes_since_shift_start(shift, ts),
        "guard_silent_since_start": guard_silent_since_start(shift, ts),
        "open_ask": open_ask(shift, ts, prior_rows),
        "image_in_wake": image_in_wake(wake),
    }


def detect_situations(
    shift: Shift,
    turn_row: dict,
    prior_rows: list[dict] | None = None,
    guidelines: dict | None = None,
    *,
    include_cross_cutting: bool = True,
) -> list[str]:
    """Scenario ids that apply to this turn, from the shift as-of the turn ts.

    Detector definitions (all bounded by the turn ts):

    * **wake window** — guard messages in `(min(previous turn ts, ts - 10 min), ts]`.
      The 10-minute floor keeps a rapid exchange (a turn fired 20s after the
      last one) from seeing an empty window. It never reaches past `ts`.
    * **guard_text_any / guard_text_none** — a `lexicon` list matched against the
      wake-window guard text, words in order with up to two words between them
      and word-boundary anchored ("perimeter clear" hits "perimeter is clear";
      "authorized" does not hit "unauthorized").
    * **guard_silent_minutes** — minutes since the last guard message at/before
      `ts`; the shift start when the guard has never written.
    * **device_dark_minutes** — minutes since the last telemetry/location event
      at/before `ts`, restarted by any later "app terminated"/offline job_log.
    * **no_checkin** — no `checked in` job_log and no `guard_checked_in` /
      `guard_in_transit` wake at/before `ts`.
    * **minutes_since_shift_start_min** — the turn is inside the first N minutes
      after the scheduled start (the start-of-shift coverage window).
    * **guard_silent_since_start** — no guard message between the scheduled
      start and `ts`.
    * **open_ask** — a prior turn sent an ask-like DM (`harness.lexicon.is_ask`,
      or `meta.copilot_action` containing "ask") and no guard message has
      arrived since, up to `ts`.
    * **image_in_wake** — a wake-window guard message carried an image.
    * **recent_tool {tool, minutes, min_count}** — the run's earlier turns called
      `tool` at least `min_count` times since shift start (see `_recent_tool`
      for why `minutes` is evidence rather than the gate).
    * **no_recent_tool {tool, minutes}** — no earlier turn called `tool` within
      `minutes` before `ts`.
    * **either [a, b]** — any sub-spec matches.
    * **trigger_any** — the turn's own trigger.

    `prior_rows` are the same run's earlier turns (control rows for the control
    arm, variant rows for the variant arm) — never the other arm's.
    """
    g = guidelines or load_guidelines()
    ctx = situation_context(shift, turn_row, prior_rows, g)
    out = []
    for sc in g.get("scenarios") or []:
        if _match_situation(sc.get("situation") or {}, ctx):
            out.append(sc["id"])
    if include_cross_cutting:
        out.append(CROSS_CUTTING_ID)
    return out


# ---------------------------------------------------------------------------
# the floor
# ---------------------------------------------------------------------------


def _first_index(row: dict, predicate) -> int | None:
    for i, rec in enumerate(row.get("tools_used") or []):
        if not isinstance(rec, dict):
            continue
        if predicate(lex.tool_short(rec.get("tool") or ""), rec.get("input") or {}):
            return i
    return None


_ESCALATION_ANNOUNCEMENT = re.compile(r"\b(escalat\w*|looping ops|ops (?:is|are) on it)\b", re.I)


def _affirming_dm_index(row: dict) -> tuple[int | None, str]:
    """Index (within tools_used) and body of the first *affirming* DM.

    Affirming = acknowledging or closing the claim ("got it", "nice work",
    "you're all set"). A DM that announces an escalation is not affirming — it
    is the opposite — so it does not require a prior location read.
    """
    for i, rec in enumerate(row.get("tools_used") or []):
        if not isinstance(rec, dict):
            continue
        if lex.tool_short(rec.get("tool") or "") != "request_copilot_dm":
            continue
        body = str((rec.get("input") or {}).get("body") or "")
        if _ESCALATION_ANNOUNCEMENT.search(body):
            continue
        if lex.is_ack(body) or lex.is_close(body):
            return i, body
    return None, ""


_PHOTO_REF = re.compile(r"\b(photo|photos|pic|pics|picture|pictures|image|images|shot|shots)\b", re.I)


def _photo_dm_index(row: dict) -> tuple[int | None, str]:
    for i, rec in enumerate(row.get("tools_used") or []):
        if not isinstance(rec, dict):
            continue
        if lex.tool_short(rec.get("tool") or "") != "request_copilot_dm":
            continue
        body = str((rec.get("input") or {}).get("body") or "")
        if _PHOTO_REF.search(body):
            return i, body
    return None, ""


def _obligation_window_key(ts: datetime) -> tuple:
    """One obligation window = one clock hour (site check-ins are hourly)."""
    return (ts.date(), ts.hour)


def asks_in_window(turn_row: dict, prior_rows: list[dict] | None) -> list[str]:
    """Ask-like DMs sent in this turn's obligation window, this turn included."""
    ts = row_ts(turn_row)
    key = _obligation_window_key(ts)
    out: list[str] = []
    for r in prior_rows or []:
        rts = row_ts(r)
        if rts >= ts or _obligation_window_key(rts) != key:
            continue
        out.extend(_row_asks(r))
    out.extend(_row_asks(turn_row))
    return out


def _shift_ending(turn_row: dict, shift: Shift | None) -> bool:
    if turn_row.get("trigger") == "shift_ending":
        return True
    if shift is None:
        return False
    return row_ts(turn_row) >= shift.end - timedelta(minutes=15)


def check_floor(
    scenario: dict,
    turn_row: dict,
    prior_rows: list[dict] | None = None,
    *,
    shift: Shift | None = None,
    guidelines: dict | None = None,
    applicable: bool = True,
) -> dict:
    """Evaluate one scenario's floor on the copilot's turn.

    Returns `{scenario, applicable, passed, failed_checks: [{check, evidence}], turn}`.
    Only the turn record (and, for `always_when`, the shift as-of the turn ts) is
    read. `failed_checks[].evidence` is a verbatim quote from the turn.
    """
    g = guidelines or load_guidelines()
    floor = scenario.get("floor") or {}
    sid = scenario.get("id")
    failed: list[dict] = []
    if not applicable:
        return {
            "scenario": sid,
            "applicable": False,
            "passed": True,
            "failed_checks": [],
            "turn": turn_row.get("turn"),
        }

    tools = row_tools(turn_row)
    dms = row_dms(turn_row)

    always = floor.get("always") or {}
    _check_always(always, turn_row, tools, failed)

    always_when = floor.get("always_when") or {}
    if always_when:
        cond = always_when.get("cond") or {}
        fires = False
        if shift is not None:
            ctx = situation_context(shift, turn_row, prior_rows, g)
            fires = _match_situation(cond, ctx)
        if fires:
            _check_always(
                {k: v for k, v in always_when.items() if k != "cond"}, turn_row, tools, failed
            )

    never = floor.get("never") or {}
    for key, value in never.items():
        if key == "tools_any":
            hit = [t for t in value if t in tools]
            if hit:
                failed.append({"check": "never.tools_any", "evidence": ", ".join(hit)})
        elif key == "dm_contains":
            names = [value] if isinstance(value, str) else list(value)
            for body in dms:
                quotes: list[str] = []
                for n in names:
                    quotes += lexicon_hits(body, lexicon_terms(g, n))
                    if n == "verdict":
                        hits = lex.surveillance_hits(body)
                        quotes += hits["verdicts"] + hits["threats"] + hits["consequences"]
                if quotes:
                    failed.append(
                        {"check": f"never.dm_contains[{'/'.join(names)}]", "evidence": quotes[0]}
                    )
                    break
        elif key == "dms_over":
            if len(dms) > int(value):
                failed.append(
                    {
                        "check": "never.dms_over",
                        "evidence": f"{len(dms)} DMs: " + (dms[int(value)][:160] if dms else ""),
                    }
                )
        elif key == "third_ask_same_window" and value:
            asks = asks_in_window(turn_row, prior_rows)
            if len(asks) >= 3:
                failed.append({"check": "never.third_ask_same_window", "evidence": asks[-1][:200]})
        elif key == "new_ask_at_shift_end" and value:
            if _shift_ending(turn_row, shift):
                asks = _row_asks(turn_row)
                if asks:
                    failed.append(
                        {"check": "never.new_ask_at_shift_end", "evidence": asks[0][:200]}
                    )
        elif key == "tool_severity":
            for tool, bad in (value or {}).items():
                sev = tool_severities(turn_row, tool)
                hit = [s for s in sev if s in {str(b).lower() for b in bad}]
                if tool in tools and hit:
                    failed.append(
                        {"check": f"never.tool_severity[{tool}]", "evidence": f"severity={hit[0]}"}
                    )

    return {
        "scenario": sid,
        "applicable": True,
        "passed": not failed,
        "failed_checks": failed,
        "turn": turn_row.get("turn"),
    }


def _check_always(always: dict, turn_row: dict, tools: list[str], failed: list[dict]) -> None:
    required = list(always.get("tools_any") or [])
    if required and not any(t in tools for t in required):
        failed.append(
            {
                "check": "always.tools_any",
                "evidence": f"required one of {required}; turn called {tools or '[]'}",
            }
        )
        return
    if always.get("before_affirming_dm"):
        dm_i, body = _affirming_dm_index(turn_row)
        if dm_i is not None:
            tool_i = _first_index(turn_row, lambda name, _inp: name in required)
            if tool_i is None or tool_i > dm_i:
                failed.append({"check": "always.before_affirming_dm", "evidence": body[:200]})
    if always.get("before_referencing_photo"):
        dm_i, body = _photo_dm_index(turn_row)
        if dm_i is not None:
            tool_i = _first_index(turn_row, lambda name, _inp: name in required)
            if tool_i is None or tool_i > dm_i:
                failed.append({"check": "always.before_referencing_photo", "evidence": body[:200]})


# ---------------------------------------------------------------------------
# scoring one arm / the scorer
# ---------------------------------------------------------------------------


def load_anchor_shift(shift_id: str, shifts_dir: Path | None = None) -> Shift:
    return load_shift((shifts_dir or SHIFTS_DIR) / f"{shift_id}.json")


def evaluate_rows(
    shift: Shift,
    rows: list[dict],
    scored_turns: set[int] | None,
    guidelines: dict,
) -> dict:
    """Detect + floor-check one arm's rows for one shift.

    `rows` is the whole run for the shift (earlier turns are the `prior_rows`
    context); only turns in `scored_turns` are reported.
    """
    rows = sorted(rows, key=lambda r: (row_ts(r), int(r.get("turn", 0))))
    results: list[dict] = []
    for i, row in enumerate(rows):
        tn = int(row.get("turn", -1))
        if scored_turns is not None and tn not in scored_turns:
            continue
        prior = rows[:i]
        sids = detect_situations(shift, row, prior, guidelines)
        for sid in sids:
            sc = scenario_by_id(guidelines, sid)
            if not sc:
                continue
            res = check_floor(sc, row, prior, shift=shift, guidelines=guidelines)
            res["shift"] = shift.id
            res["name"] = sc.get("name")
            res["judgment"] = list(sc.get("judgment") or [])
            results.append(res)
    return {"shift": shift.id, "results": results}


def _summarize(arm: list[dict]) -> dict:
    per_scenario: dict[str, dict] = {}
    failing: list[dict] = []
    for r in arm:
        row = per_scenario.setdefault(r["scenario"], {"applicable": 0, "passed": 0, "failed": 0})
        row["applicable"] += 1
        if r["passed"]:
            row["passed"] += 1
        else:
            row["failed"] += 1
            for fc in r["failed_checks"]:
                failing.append(
                    {
                        "shift": r.get("shift"),
                        "turn": r.get("turn"),
                        "scenario": r["scenario"],
                        "check": fc["check"],
                        "quote": fc["evidence"],
                    }
                )
    return {"per_scenario": per_scenario, "failing_turns": failing}


def score_conduct_floor(
    store: ExperimentStore,
    control_id: str,
    variant_id: str,
    recipe: dict,
    *,
    guidelines_path: Path | None = None,
    shifts_dir: Path | None = None,
) -> dict:
    """GATE: the VARIANT breaks no floor check on any applicable turn.

    Control results ride along as evidence — they never gate (GUIDELINES.md:
    "Baseline is what production did there — evidence of what happens today,
    not the control arm"). `judgment_items` carries each applicable turn's
    scenario questions for `cx judge`; this scorer never answers them.
    """
    g = load_guidelines(guidelines_path)
    control_all: list[dict] = []
    variant_all: list[dict] = []
    judgment_items: list[dict] = []

    for job in recipe.get("jobs") or []:
        sid = str(job["shift"])
        want = {int(t) for t in (job.get("turns") or [])} or None
        shift = load_anchor_shift(sid, shifts_dir)
        c = evaluate_rows(shift, store.load_turns(control_id, sid), want, g)
        v = evaluate_rows(shift, store.load_turns(variant_id, sid), want, g)
        control_all += c["results"]
        variant_all += v["results"]
        for r in v["results"]:
            if r["judgment"]:
                judgment_items.append(
                    {
                        "shift": sid,
                        "turn": r["turn"],
                        "scenario": r["scenario"],
                        "name": r["name"],
                        "questions": r["judgment"],
                    }
                )

    c_sum = _summarize(control_all)
    v_sum = _summarize(variant_all)
    passed = bool(variant_all) and not v_sum["failing_turns"]
    return {
        "scorer": "conduct_floor",
        "guidelines_version": g.get("version"),
        "guardrail": g.get("guardrail"),
        "turns_scored": len({(r.get("shift"), r.get("turn")) for r in variant_all}),
        "variant": v_sum,
        "control": c_sum,
        "control_is_evidence_not_gate": True,
        "judgment_items": judgment_items,
        "judgment_is_advisory": True,
        "pass": passed,
        "detail": (
            "variant holds the conduct floor on every applicable turn"
            if passed
            else (
                "no applicable turn scored"
                if not variant_all
                else "variant breaks the floor: "
                + "; ".join(
                    f"{f['shift']} t{f['turn']} {f['scenario']} {f['check']}"
                    for f in v_sum["failing_turns"][:6]
                )
            )
        ),
    }


# ---------------------------------------------------------------------------
# "what production did" — API-free baseline report (cx t ru -n)
# ---------------------------------------------------------------------------


def baseline_report(
    jobs: list[dict] | None = None,
    *,
    guidelines_path: Path | None = None,
    shifts_dir: Path | None = None,
) -> dict:
    """Run the detectors + floor over the BASELINE turns of the anchor shifts.

    Zero API calls: this is what production actually did on the anchor turns.
    Compared against the `baseline_ok` flags declared in guidelines.json;
    mismatches are reported, never forced.
    """
    g = load_guidelines(guidelines_path)
    jobs = jobs or anchor_jobs(g)
    results: list[dict] = []
    for job in jobs:
        sid = str(job["shift"])
        want = {int(t) for t in (job.get("turns") or [])} or None
        shift = load_anchor_shift(sid, shifts_dir)
        rows = baseline_turn_rows(shift)
        results += evaluate_rows(shift, rows, want, g)["results"]

    by_turn: dict[tuple, list[dict]] = {}
    for r in results:
        by_turn.setdefault((r["shift"], r["turn"]), []).append(r)

    mismatches = []
    for a in anchor_flags(g):
        if a["baseline_ok"] is None:
            continue
        rows = [r for r in by_turn.get((a["shift"], a["turn"]), []) if r["scenario"] == a["scenario"]]
        if not rows:
            mismatches.append(
                {**a, "observed": None, "why": "scenario did not fire on this turn"}
            )
            continue
        observed = all(r["passed"] for r in rows)
        if observed != bool(a["baseline_ok"]):
            mismatches.append(
                {
                    **a,
                    "observed": observed,
                    "why": "; ".join(
                        f"{fc['check']}: {fc['evidence'][:80]}"
                        for r in rows
                        for fc in r["failed_checks"]
                    )
                    or "floor held",
                }
            )

    summary = _summarize(results)
    return {
        "report": "baseline_conduct_floor",
        "api_calls": 0,
        "jobs": jobs,
        "per_scenario": summary["per_scenario"],
        "failing_turns": summary["failing_turns"],
        "baseline_ok_mismatches": mismatches,
        "results": results,
    }


def format_baseline_table(report: dict) -> str:
    """Compact table: scenario, turns applicable, baseline floor pass/fail."""
    g = load_guidelines()
    names = {sc["id"]: sc["name"] for sc in g.get("scenarios") or []}
    names[CROSS_CUTTING_ID] = "cross_cutting"
    lines = [
        "what production did (BASELINE) on the anchor turns — 0 API calls",
        f"{'scenario':<10} {'name':<30} {'applicable':>10} {'pass':>6} {'fail':>6}",
    ]
    per = report.get("per_scenario") or {}
    for sid in sorted(per, key=lambda s: (s == CROSS_CUTTING_ID, s)):
        row = per[sid]
        lines.append(
            f"{sid:<10} {names.get(sid, ''):<30} {row['applicable']:>10} "
            f"{row['passed']:>6} {row['failed']:>6}"
        )
    if not per:
        lines.append("(no scenario fired on the anchor turns)")
    miss = report.get("baseline_ok_mismatches") or []
    lines.append(f"baseline_ok mismatches: {len(miss)}")
    for m in miss:
        lines.append(
            f"  {m['shift']} t{m['turn']:<4} {m['scenario']:<4} "
            f"declared={m['baseline_ok']} observed={m['observed']}  {m['why'][:90]}"
        )
    return "\n".join(lines)
