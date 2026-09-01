"""Session A: mine ProblemCards from shift JSON (events + baseline).

No LLM. No personas. Every card must cite evidence in the file.

The miner reads one `shifts/<id>.json` through `harness.loader.load_shift` and
slices the recorded baseline into turns with
`harness.adapters.replay.iter_baseline_turns`, so every `turns` value on a card
is a real turn number the rest of the harness can replay.

Hard rules honored here (LOOP.md):

* Rule 1 — every signal is read out of `events` / `baseline`. Nothing is
  synthesized, no guard personas, no scripted replies.
* Rule 2 — photos in this bundle are `"[photo]"` placeholders. We mine
  *inspect-or-not* (`photo_without_inspect`) and *ask-again-or-not*
  (`hammer_after_photo`). We never claim two photos showed the same place.
* Cards carry the catalog `ProcessSpec` verbatim (`process_spec_for`), so the
  evaluator scores copilot process on the frozen wake, never guard outcomes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from harness.adapters.replay import BaselineTurnActions, iter_baseline_turns
from harness.agent.catalog import CLASS_CATALOG, process_spec_for
from harness.agent.types import Evidence, ProblemCard, ProblemClass, Severity
from harness.lexicon import (
    OPS_TOOLS,
    asks_photo,
    is_ack,
    is_request,
    surveillance_hits,
    tool_short,
)
from harness.loader import Shift, load_shift, parse_ts

ROOT = Path(__file__).resolve().parents[2]
SHIFTS_DIR = ROOT / "shifts"

DM_TOOL = "request_copilot_dm"
LOCATION_TOOL = "get_guard_locations"
IMAGE_TOOL = "fetch_chat_image"
PHOTO_PLACEHOLDER = "[photo]"

# A turn is an "obligation window opener" when the harness woke the copilot for
# a scheduled duty rather than because the guard said something.
OBLIGATION_TRIGGERS = frozenset(
    {"session_start", "scheduled_check_in", "shift_ending", "obligation_due"}
)

# How long after an event we still consider a turn to be "the response".
RESPONSE_WINDOW = timedelta(minutes=20)
# Sustained-silence threshold for the coverage-risk (under_escalation) probe.
SILENCE_MINUTES = 45
# ping_budget: the catalog signal is >=3 DMs on one window; we additionally
# require >=2 of them to have gone out with no guard message in between, so the
# quoted evidence really is a repeat ping and not a normal back-and-forth.
PING_DM_MIN = 3
PING_UNANSWERED_MIN = 2
# Cap how many turns after a photo count as "the copilot's chance to inspect".
PHOTO_LOOKAHEAD_TURNS = 3

# --- lexicons -------------------------------------------------------------
# Guard text that asserts work was performed ("unverified claim" candidates).
WORK_CLAIM_RE = re.compile(
    r"\b("
    r"all[- ]?clear|clear|cleared|checked|check[- ]?in|checks?\b|"
    r"patrol(?:led|s)?|perimeter|walked|walk[- ]?around|swept|sweep|"
    r"covered|complete(?:d)?|finished|done|good to go|all good|"
    r"reported|report"
    r")\b",
    re.I,
)

# Copilot DM text showing the claim was probed rather than accepted at face
# value. Any hit means the turn is NOT a naive affirmation.
CHALLENGE_RE = re.compile(
    r"("
    r"\bgps\b|\bline up\b|\blines up\b|\bmatch(?:es|ing)?\b|"
    r"\bsign off\b|\bcan'?t log\b|\bescalat|\bdouble[- ]?check\b|"
    r"\bflagg?ed\b|\bshows? you\b|\bdata shows\b|\block it in\b|"
    r"\bwalk me through\b|\bverify\b|\bverified\b|\bdoesn'?t line\b|"
    r"\bstayed (?:in|at)\b|\bno movement\b"
    r")",
    re.I,
)

# Guard pushback about being monitored / messaged (deterministic, from events).
PUSHBACK_RE = re.compile(
    r"("
    r"leave me alone|stop (?:texting|messaging|asking)|get off my|"
    r"harass|annoying|tired of this|quit (?:texting|messaging)|"
    r"enough already|you guys just sit|behind desks|let me do my job|"
    r"back off|stop babysitting|i know what i'?m doing|i know my job"
    r")",
    re.I,
)


# ---------------------------------------------------------------------------
# Turn / event views
# ---------------------------------------------------------------------------


@dataclass
class TurnView:
    """One baseline turn with the JSON indexes needed for citations."""

    actions: BaselineTurnActions
    start_index: int
    call_indexes: list[int] = field(default_factory=list)

    @property
    def turn(self) -> int:
        return self.actions.turn

    @property
    def trigger(self) -> str:
        return self.actions.trigger

    @property
    def ts(self) -> datetime:
        return self.actions.ts

    @property
    def tools(self) -> list[str]:
        return [tool_short(c.get("tool", "")) for c in self.actions.tool_calls]

    @property
    def messages(self) -> list[str]:
        return list(self.actions.messages)

    @property
    def indexes(self) -> list[int]:
        return [self.start_index, *self.call_indexes]

    def called(self, tool: str) -> bool:
        return tool in self.tools

    def escalated(self) -> bool:
        return bool(OPS_TOOLS & set(self.tools))

    def first_dm_position(self) -> int | None:
        for i, name in enumerate(self.tools):
            if name == DM_TOOL:
                return i
        return None

    def called_before_first_dm(self, tool: str) -> bool:
        dm_at = self.first_dm_position()
        if dm_at is None:
            return tool in self.tools
        return tool in self.tools[:dm_at]

    def text(self) -> str:
        return "\n".join(self.messages)


@dataclass
class EventView:
    index: int
    ts: datetime
    type: str
    raw: dict

    @property
    def text(self) -> str:
        return (self.raw.get("text") or "").strip()

    @property
    def image(self) -> str | None:
        return self.raw.get("image")

    @property
    def is_guard_message(self) -> bool:
        return self.type == "guard_message"

    @property
    def is_photo(self) -> bool:
        return self.is_guard_message and bool(self.image)


def _turn_views(shift: Shift) -> list[TurnView]:
    """iter_baseline_turns + the baseline array indexes for each entry."""
    index_of = {id(entry): i for i, entry in enumerate(shift.baseline)}
    starts = {
        entry.get("turn"): i
        for i, entry in enumerate(shift.baseline)
        if entry.get("type") == "turn_start"
    }
    views: list[TurnView] = []
    for actions in iter_baseline_turns(shift):
        views.append(
            TurnView(
                actions=actions,
                start_index=starts.get(actions.turn, -1),
                call_indexes=[
                    index_of[id(c)] for c in actions.tool_calls if id(c) in index_of
                ],
            )
        )
    return views


def _event_views(shift: Shift) -> list[EventView]:
    """Raw `events` with their array index (the citation LOOP.md asks for)."""
    raw = json.loads(shift.path.read_text(encoding="utf-8"))
    out: list[EventView] = []
    for i, ev in enumerate(raw.get("events") or []):
        if not isinstance(ev, dict) or not ev.get("ts"):
            continue
        out.append(EventView(index=i, ts=parse_ts(ev["ts"]), type=ev.get("type", ""), raw=ev))
    return out


def _responding_turn(
    turns: list[TurnView], ts: datetime, *, window: timedelta = RESPONSE_WINDOW
) -> TurnView | None:
    """The first turn the copilot woke for at/after `ts` (within `window`)."""
    for view in turns:
        if view.ts < ts:
            continue
        if view.ts - ts > window:
            return None
        return view
    return None


def _turns_after(turns: list[TurnView], ts: datetime, limit: int) -> list[TurnView]:
    return [v for v in turns if v.ts >= ts][:limit]


def _quote(text: str, limit: int = 240) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# Sentence-ish: clause boundaries too, so "send your company the photos, text
# me the note" does not read as one photo request aimed at the copilot.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+|(?<=[.!?])(?=[A-Z])|[–—;,]")

# "send me another shot", "can you take a picture", "need a photo of the gate".
_PHOTO_REQUEST_RE = re.compile(
    r"\b(?:send|snap|grab|take|shoot|get|need)\b[^.!?]{0,40}?"
    r"\b(?:photos?|pics?|pictures?|shots?|images?)\b",
    re.I,
)
# The ask has to be pointed at the copilot ("send me one", "…?"), otherwise
# "send the pictures to your company" reads as a re-ask when it is not.
_TO_ME_RE = re.compile(r"\b(?:me|us|my end|over here|through)\b|\?", re.I)
_NEGATED_RE = re.compile(
    r"\b(?:don'?t|do not|no need|not|never|instead|rather than|stop)\b", re.I
)


def _asks_for_a_photo(body: str) -> bool:
    """True only when one sentence actually re-requests a photo *from the guard*.

    Whole-body lexicon matching flagged "Got it, I see the photo … just need you
    staying on the post" and "don't need more shots" as re-asks. Those are an
    acknowledgement and a stand-down; neither is a ping.
    """
    for sentence in _SENTENCE_SPLIT.split(body or ""):
        if not sentence or not asks_photo(sentence) or not is_request(sentence):
            continue
        if _NEGATED_RE.search(sentence):
            continue
        if _PHOTO_REQUEST_RE.search(sentence) and _TO_ME_RE.search(sentence):
            return True
    return False


# ---------------------------------------------------------------------------
# Card construction
# ---------------------------------------------------------------------------


def _make_card(
    shift_id: str,
    problem_class: ProblemClass,
    turns: list[int],
    evidence: Evidence,
) -> ProblemCard:
    entry = CLASS_CATALOG[problem_class]
    card = ProblemCard(
        id=f"{shift_id}-{problem_class}-t{'_'.join(str(t) for t in turns)}",
        shift_id=shift_id,
        turns=list(turns),
        problem_class=problem_class,
        severity=entry["severity"],  # type: ignore[arg-type]
        evidence=evidence,
        policy_files=list(entry["policy_files"]),
        spec=process_spec_for(problem_class),
        source="json",
    )
    card.validate()
    return card


# ---------------------------------------------------------------------------
# Probes — one per mined problem class
# ---------------------------------------------------------------------------


def _mine_unverified_claim(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """Guard asserts work done; the copilot affirms without a location check."""
    cards: list[ProblemCard] = []
    for ev in events:
        if not ev.is_guard_message or not ev.text:
            continue
        if not WORK_CLAIM_RE.search(ev.text):
            continue
        view = _responding_turn(turns, ev.ts)
        if view is None or not view.messages:
            continue
        body = view.text()
        if not is_ack(body):
            continue
        if CHALLENGE_RE.search(body) or view.escalated():
            continue
        if view.called_before_first_dm(LOCATION_TOOL):
            continue
        cards.append(
            _make_card(
                shift.id,
                "unverified_claim",
                [view.turn],
                Evidence(
                    guard_text=_quote(ev.text),
                    baseline_dms=[_quote(m) for m in view.messages],
                    baseline_tools=view.tools,
                    missing_tools=[LOCATION_TOOL],
                    event_indexes=[ev.index],
                    baseline_indexes=view.indexes,
                    notes=[
                        f"turn {view.turn} ({view.trigger}) affirmed the work claim "
                        f"with no {LOCATION_TOOL} call before the DM",
                    ],
                ),
            )
        )
    return cards


def _mine_photo_without_inspect(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """`[photo]` arrives; no fetch_chat_image on that turn or the next few.

    LOOP.md rule 2: the placeholder tells us a photo existed, nothing about what
    it showed. The only claim made here is inspect-or-not.
    """
    cards: list[ProblemCard] = []
    photos = [e for e in events if e.is_photo]
    for i, ev in enumerate(photos):
        window = _turns_after(turns, ev.ts, PHOTO_LOOKAHEAD_TURNS)
        if i + 1 < len(photos):
            window = [v for v in window if v.ts <= photos[i + 1].ts] or window[:1]
        if not window:
            continue
        if any(v.called(IMAGE_TOOL) for v in window):
            continue
        tools = sorted({t for v in window for t in v.tools})
        cards.append(
            _make_card(
                shift.id,
                "photo_without_inspect",
                [v.turn for v in window],
                Evidence(
                    guard_text=PHOTO_PLACEHOLDER,
                    baseline_dms=[_quote(m) for v in window for m in v.messages],
                    baseline_tools=tools,
                    missing_tools=[IMAGE_TOOL],
                    event_indexes=[ev.index],
                    baseline_indexes=[i for v in window for i in v.indexes],
                    notes=[
                        f"events[{ev.index}].image == '{PHOTO_PLACEHOLDER}'; turns "
                        f"{[v.turn for v in window]} never called {IMAGE_TOOL}",
                        "placeholder photo: inspect-or-not only, no visual claim",
                    ],
                ),
            )
        )
    return cards


def _mine_hammer_after_photo(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """A photo landed and the copilot still asked for a photo afterwards."""
    cards: list[ProblemCard] = []
    photos = [e for e in events if e.is_photo]
    for i, ev in enumerate(photos):
        next_photo = photos[i + 1].ts if i + 1 < len(photos) else None
        for view in turns:
            if view.ts < ev.ts:
                continue
            if next_photo is not None and view.ts > next_photo:
                break
            if view.trigger in OBLIGATION_TRIGGERS and view.ts - ev.ts > RESPONSE_WINDOW:
                break
            asks = [m for m in view.messages if _asks_for_a_photo(m)]
            if not asks:
                continue
            cards.append(
                _make_card(
                    shift.id,
                    "hammer_after_photo",
                    [view.turn],
                    Evidence(
                        guard_text=PHOTO_PLACEHOLDER,
                        baseline_dms=[_quote(m) for m in asks],
                        baseline_tools=view.tools,
                        missing_tools=[] if view.called(IMAGE_TOOL) else [IMAGE_TOOL],
                        event_indexes=[ev.index],
                        baseline_indexes=view.indexes,
                        notes=[
                            f"photo at events[{ev.index}] then turn {view.turn} asked "
                            "for a photo again",
                        ],
                    ),
                )
            )
            break
    return cards


def _obligation_windows(turns: list[TurnView]) -> list[list[TurnView]]:
    """Group turns into windows opened by an obligation trigger."""
    windows: list[list[TurnView]] = []
    for view in turns:
        if view.trigger in OBLIGATION_TRIGGERS or not windows:
            windows.append([view])
        else:
            windows[-1].append(view)
    return windows


def _mine_ping_budget(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """>=3 DMs on one obligation window, >=2 of them with no reply in between."""
    guard_ts = [e.ts for e in events if e.is_guard_message]
    cards: list[ProblemCard] = []
    for window in _obligation_windows(turns):
        dms: list[tuple[TurnView, str]] = [
            (v, m) for v in window for m in v.messages
        ]
        if len(dms) < PING_DM_MIN:
            continue
        unanswered: list[tuple[TurnView, str]] = []
        prev_ts: datetime | None = None
        for view, body in dms:
            if prev_ts is not None and not any(prev_ts <= g <= view.ts for g in guard_ts):
                unanswered.append((view, body))
            prev_ts = view.ts
        if len(unanswered) < PING_UNANSWERED_MIN:
            continue
        turn_ids = sorted({v.turn for v, _ in dms})
        cards.append(
            _make_card(
                shift.id,
                "ping_budget",
                turn_ids,
                Evidence(
                    baseline_dms=[_quote(m) for _, m in dms],
                    baseline_tools=sorted({t for v in window for t in v.tools}),
                    baseline_indexes=[i for v in window for i in v.indexes],
                    notes=[
                        f"{len(dms)} DMs on the window opened by turn "
                        f"{window[0].turn} ({window[0].trigger})",
                        f"{len(unanswered)} sent with no guard message in between: "
                        + "; ".join(_quote(m, 90) for _, m in unanswered),
                    ],
                ),
            )
        )
    return cards


def _mine_surveillance_voice(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """Baseline DM matches the threat / consequence / verdict lexicon."""
    cards: list[ProblemCard] = []
    for view in turns:
        matched: list[str] = []
        offending: list[str] = []
        for body in view.messages:
            hits = surveillance_hits(body)
            rows = [f"{bucket}:{h}" for bucket, hs in hits.items() for h in hs]
            if rows:
                matched.extend(rows)
                offending.append(body)
        if not matched:
            continue
        cards.append(
            _make_card(
                shift.id,
                "surveillance_voice",
                [view.turn],
                Evidence(
                    baseline_dms=[_quote(m) for m in offending],
                    baseline_tools=view.tools,
                    baseline_indexes=view.indexes,
                    notes=[f"no-surveillance lexicon hits: {', '.join(sorted(set(matched)))}"],
                ),
            )
        )
    return cards


def _mine_pushback_failure(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """Guard pushes back; the copilot doubles down (or goes silent)."""
    cards: list[ProblemCard] = []
    for ev in events:
        if not ev.is_guard_message or not ev.text:
            continue
        if not PUSHBACK_RE.search(ev.text):
            continue
        view = _responding_turn(turns, ev.ts)
        if view is None:
            continue
        body = view.text()
        if view.messages and not CHALLENGE_RE.search(body):
            continue
        if not view.messages and view.called("add_copilot_note"):
            # Deliberate quiet-and-log is a legitimate response; not a card.
            continue
        mode = "doubled down on the same challenge" if view.messages else "sent nothing back"
        cards.append(
            _make_card(
                shift.id,
                "pushback_failure",
                [view.turn],
                Evidence(
                    guard_text=_quote(ev.text),
                    baseline_dms=[_quote(m) for m in view.messages],
                    baseline_tools=view.tools,
                    missing_tools=[] if view.called("add_copilot_note") else ["add_copilot_note"],
                    event_indexes=[ev.index],
                    baseline_indexes=view.indexes,
                    notes=[f"guard pushback at events[{ev.index}]; turn {view.turn} {mode}"],
                ),
            )
        )
    return cards


def _mine_under_escalation(
    shift: Shift, turns: list[TurnView], events: list[EventView]
) -> list[ProblemCard]:
    """A whole obligation window passed with no guard contact and no escalation."""
    guard_events = [e for e in events if e.is_guard_message]
    obligations = [v for v in turns if v.trigger in OBLIGATION_TRIGGERS]
    cards: list[ProblemCard] = []
    for i, view in enumerate(obligations):
        if i == 0:
            continue
        prev = obligations[i - 1]
        contact = [e for e in guard_events if prev.ts <= e.ts <= view.ts]
        if contact:
            continue
        last = [e for e in guard_events if e.ts <= view.ts]
        # Silence is measured from the last guard word, but never from before
        # the shift itself started — a check-in at minute zero is not a miss.
        since = max(last[-1].ts, shift.start) if last else max(prev.ts, shift.start)
        silent_minutes = (view.ts - since).total_seconds() / 60.0
        if silent_minutes < SILENCE_MINUTES:
            continue
        if view.escalated():
            continue
        # A fresh escalation on the previous obligation turn still stands.
        window_turns = [v for v in turns if prev.ts <= v.ts <= view.ts]
        if any(v.escalated() for v in window_turns):
            continue
        cards.append(
            _make_card(
                shift.id,
                "under_escalation",
                [view.turn],
                Evidence(
                    guard_text=_quote(last[-1].text) if last and last[-1].text else None,
                    baseline_dms=[_quote(m) for m in view.messages],
                    baseline_tools=view.tools,
                    missing_tools=sorted(OPS_TOOLS),
                    event_indexes=[last[-1].index] if last else [],
                    baseline_indexes=view.indexes,
                    notes=[
                        f"no guard message between turn {prev.turn} "
                        f"({prev.ts.isoformat()}) and turn {view.turn} "
                        f"({view.ts.isoformat()}): {silent_minutes:.0f} min silent",
                        f"turn {view.turn} ({view.trigger}) called no "
                        "escalate_to_ops / escalate_to_human / flag_copilot_guard",
                    ],
                ),
            )
        )
    return cards


PROBES: dict[ProblemClass, Any] = {
    "unverified_claim": _mine_unverified_claim,
    "photo_without_inspect": _mine_photo_without_inspect,
    "hammer_after_photo": _mine_hammer_after_photo,
    "ping_budget": _mine_ping_budget,
    "surveillance_voice": _mine_surveillance_voice,
    "pushback_failure": _mine_pushback_failure,
    "under_escalation": _mine_under_escalation,
}

_SEVERITY_ORDER: dict[Severity, int] = {
    "safety": 0,
    "conduct": 1,
    "lift": 2,
    "compliance": 3,
}


def _merge_duplicates(cards: list[ProblemCard]) -> list[ProblemCard]:
    """One card per (class, turn window); extra hits fold into its evidence.

    Two `[photo]` events landing in the same un-inspected turn window are one
    problem, not two, and the card id must stay unique.
    """
    merged: dict[tuple[str, tuple[int, ...]], ProblemCard] = {}
    for card in cards:
        key = (card.problem_class, tuple(card.turns))
        first = merged.get(key)
        if first is None:
            merged[key] = card
            continue
        ev, extra = first.evidence, card.evidence
        for index in extra.event_indexes:
            if index not in ev.event_indexes:
                ev.event_indexes.append(index)
        for index in extra.baseline_indexes:
            if index not in ev.baseline_indexes:
                ev.baseline_indexes.append(index)
        for dm in extra.baseline_dms:
            if dm not in ev.baseline_dms:
                ev.baseline_dms.append(dm)
        for note in extra.notes:
            if note not in ev.notes:
                ev.notes.append(note)
    return list(merged.values())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def shift_path(shift_id: str, shifts_dir: str | Path | None = None) -> Path:
    """Resolve a shift id (or an explicit path) to shifts/<id>.json."""
    candidate = Path(str(shift_id))
    if candidate.suffix == ".json" and candidate.exists():
        return candidate
    directory = Path(shifts_dir) if shifts_dir else SHIFTS_DIR
    return directory / f"{shift_id}.json"


def mine_shift(
    shift_id: str,
    shifts_dir: str | Path | None = None,
    classes: Iterable[ProblemClass] | None = None,
) -> list[ProblemCard]:
    """Return validated cards for one shifts/<id>.json file.

    Uses harness.loader.load_shift and harness.adapters.replay.iter_baseline_turns
    so turn numbers match the rest of the harness. No LLM, no personas: every
    card cites `event_indexes` / `baseline_indexes` / turns / quoted text.
    """
    path = shift_path(shift_id, shifts_dir)
    if not path.exists():
        raise FileNotFoundError(f"no shift bundle at {path}")
    shift = load_shift(path)
    turns = _turn_views(shift)
    events = _event_views(shift)

    wanted = list(classes) if classes else list(PROBES)
    cards: list[ProblemCard] = []
    for problem_class in wanted:
        probe = PROBES[problem_class]
        cards.extend(probe(shift, turns, events))

    cards = _merge_duplicates(cards)
    cards.sort(key=lambda c: (_SEVERITY_ORDER[c.severity], c.turns[0], c.problem_class))
    return cards


def mine_all(shift_ids: list[str] | None = None) -> list[ProblemCard]:
    """Mine several shifts. Default: all files under shifts/."""
    if shift_ids is None:
        shift_ids = [p.stem for p in sorted(SHIFTS_DIR.glob("*.json"))]
    cards: list[ProblemCard] = []
    for sid in shift_ids:
        cards.extend(mine_shift(sid))
    return cards


def summarize(cards: Iterable[ProblemCard]) -> dict[str, int]:
    """Cards per problem class — handy for CLI output and tests."""
    counts: dict[str, int] = {}
    for card in cards:
        counts[card.problem_class] = counts.get(card.problem_class, 0) + 1
    return counts
