"""Failure-mode miner: discover gaps in the recipe-card catalog.

`cx mine` walks shifts/ and stored run transcripts, extracts deterministic
signals, optionally clusters flagged threads with an LLM, then matches named
modes against card `covers` / `intent`. Uncovered modes become draft proposals
under experiments/proposals/. The catalog itself is never written.

Hard rules:
- NEVER writes experiments/recipes.json or experiments/fixtures/
- Never runs recipes, never scores, never declares pass/fail
- Stage 1 (`--dry`) is useful standalone: sweep + gap report, zero API calls
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from harness.loader import Shift, load_all_shifts, parse_ts
from harness.recipes import list_cards, load_recipes, miner_defaults
from harness.store import ExperimentStore
from harness.thread import ThreadManager

ROOT = Path(__file__).resolve().parents[1]
RECIPES_PATH = ROOT / "experiments" / "recipes.json"
PROPOSALS_DIR = ROOT / "experiments" / "proposals"
FIXTURES_DIR = ROOT / "experiments" / "fixtures"
FORBIDDEN_WRITE = (RECIPES_PATH,)

# ---------------------------------------------------------------------------
# Lexicons (Stage 1 is deterministic; keep these boring and reviewable)
# ---------------------------------------------------------------------------

PUSHBACK_RE = re.compile(
    r"\b("
    r"leave me alone|stop (?:texting|messaging|asking)|get off my|"
    r"i don't care|i do not care|whatever|harass|annoying|"
    r"tired of this|quit (?:texting|messaging)|enough already|"
    r"you guys just sit|behind desks"
    r")\b",
    re.I,
)
HOSTILE_RE = re.compile(
    r"\b(shut up|stupid|idiot|fuck|hate this|piss off)\b",
    re.I,
)
APOLOGY_RE = re.compile(
    r"\b(i'm sorry|i am sorry|sorry about that|apologize|my bad|didn't mean to)\b",
    re.I,
)
THREAT_RE = re.compile(
    r"\b("
    r"last warning|final warning|write[- ]ups?|you(?:'re| are) fired|"
    r"terminat(?:e|ion)|non[- ]compliant|i(?:'ll| will) report you|"
    r"you(?:'re| are) in (?:trouble|violation)|this is your final"
    r")\b",
    re.I,
)
ASK_RE = re.compile(
    r"(\?|"
    r"\b(?:can you|could you|need you to|please (?:send|confirm|check|get|reply)|"
    r"are you |did you |let me know|when you can|still on|"
    r"what's the (?:status|update)|holler|shout if)"
    r")",
    re.I,
)
TOKEN_RE = re.compile(r"[a-z0-9']{3,}")
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "you", "your", "any", "all", "out",
    "when", "what", "how", "why", "who", "can", "will", "just", "got", "see",
}

FETCH_TOOLS = {"mcp__calvis__fetch_chat_image", "fetch_chat_image"}
DM_TOOLS = {"mcp__calvis__request_copilot_dm", "request_copilot_dm"}
ESC_TOOLS = {
    "mcp__calvis__escalate_to_ops",
    "mcp__calvis__escalate_to_human",
    "mcp__calvis__flag_copilot_guard",
    "escalate_to_ops",
    "escalate_to_human",
    "flag_copilot_guard",
}
OBLIGATION_TRIGGERS = {
    "scheduled_check_in",
    "obligation_due",
    "session_start",
    "shift_ending",
}

DM_WINDOW_NAG_THRESHOLD = 3
REPEATED_ASK_JACCARD = 0.30
DUPLICATE_URL_MIN = 2
PHOTO_STREAK_MIN = 2
SILENCE_MINUTES = 45
LADDER_UNANSWERED_DMS = 3
MATCH_JACCARD = 0.22
DEFAULT_LIMIT = 30

CLUSTER_SYSTEM = """You cluster real security-guard copilot threads into named failure modes.

Return STRICT JSON only, no markdown, matching:
{"failure_modes":[{"name":"short lowercase name","description":"one line","quotes":["quote1","quote2"],"thread_ids":["..."]}]}

Rules:
- Name the behavior (e.g. "silent guard", "duplicate photo", "nagging"), not the shift id.
- 1-2 example quotes per mode, copied from the excerpts (do not invent).
- One-line description of what goes wrong.
- A thread may belong to more than one mode.
- Do not declare pass/fail. Do not propose prompt text.
"""


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class ThreadEvent:
    ts: str
    role: str  # guard | copilot | tool
    text: str = ""
    image: str | None = None
    tool: str | None = None
    trigger: str | None = None
    turn: int | None = None


@dataclass
class ThreadView:
    thread_id: str
    source: str  # shift | run
    shift_id: str
    run_id: str | None = None
    events: list[ThreadEvent] = field(default_factory=list)
    shift_end: str | None = None

    def copilot_dms(self) -> list[ThreadEvent]:
        return [e for e in self.events if e.role == "copilot" and (e.text or "").strip()]

    def guard_msgs(self) -> list[ThreadEvent]:
        return [e for e in self.events if e.role == "guard"]

    def tools(self) -> list[ThreadEvent]:
        return [e for e in self.events if e.role == "tool"]


@dataclass
class SignalHit:
    name: str
    count: int
    flagged: bool
    evidence: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _short_tool(name: str) -> str:
    return (name or "").replace("mcp__calvis__", "")


def tokenize(text: str) -> set[str]:
    return {
        t for t in TOKEN_RE.findall((text or "").lower())
        if t not in STOPWORDS and len(t) > 2
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "unnamed"


def _clip(text: str, n: int = 180) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _minutes_between(a: str, b: str) -> float | None:
    try:
        return (parse_ts(b) - parse_ts(a)).total_seconds() / 60.0
    except Exception:
        return None


def _assert_allowed_write(path: Path) -> None:
    resolved = path.resolve()
    for forbidden in FORBIDDEN_WRITE:
        if resolved == forbidden.resolve():
            raise RuntimeError(f"miner must not write {forbidden}")
    try:
        resolved.relative_to(FIXTURES_DIR.resolve())
        raise RuntimeError("miner must not write experiments/fixtures/")
    except ValueError:
        pass
    try:
        resolved.relative_to(RECIPES_PATH.parent.resolve())
    except ValueError:
        return
    # experiments/ is OK only for proposals/
    try:
        resolved.relative_to(PROPOSALS_DIR.resolve())
    except ValueError:
        if resolved.parent.resolve() == RECIPES_PATH.parent.resolve():
            raise RuntimeError(f"miner must not write {resolved} under experiments/")


# ---------------------------------------------------------------------------
# Stage 1 extractors
# ---------------------------------------------------------------------------

def signal_dm_per_obligation_window(thread: ThreadView) -> SignalHit:
    """Count copilot DMs inside scheduled/obligation wake windows.

    Chatty guard_message turns are excluded so a real conversation is not
    scored as nagging. Flag when a scheduled window itself stacks DMs.
    """
    windows: list[dict] = []
    current: dict | None = None
    for e in thread.events:
        if e.role == "copilot" and e.trigger in OBLIGATION_TRIGGERS:
            if current and current.get("closed"):
                current = None
            if current is None or e.trigger in ("scheduled_check_in", "obligation_due", "shift_ending"):
                if current:
                    windows.append(current)
                current = {
                    "trigger": e.trigger,
                    "turn": e.turn,
                    "dms": 0,
                    "asks": 0,
                    "closed": False,
                }
            current["dms"] += 1
            if ASK_RE.search(e.text or ""):
                current["asks"] += 1
        elif e.role == "copilot" and current is not None and e.trigger not in OBLIGATION_TRIGGERS:
            # a later non-obligation DM closes the window (conversation took over)
            current["closed"] = True
        elif e.role == "guard" and current is not None and (e.text or "").strip():
            current["closed"] = True
    if current:
        windows.append(current)

    hot = [w for w in windows if w["dms"] >= DM_WINDOW_NAG_THRESHOLD or w["asks"] >= 2]
    excerpts = [
        f"t{w.get('turn')} {w.get('trigger')} dms={w['dms']} asks={w['asks']}"
        for w in hot[:5]
    ]
    return SignalHit(
        name="dm_per_obligation_window",
        count=len(hot),
        flagged=bool(hot),
        evidence=excerpts,
        excerpts=excerpts,
    )


def signal_repeated_asks(thread: ThreadView) -> SignalHit:
    dms = thread.copilot_dms()
    pairs: list[str] = []
    excerpts: list[str] = []
    for a, b in zip(dms, dms[1:]):
        if not (ASK_RE.search(a.text) or ASK_RE.search(b.text)):
            continue
        score = jaccard(tokenize(a.text), tokenize(b.text))
        if score >= REPEATED_ASK_JACCARD:
            pairs.append(f"j={score:.2f}")
            excerpts.append(_clip(a.text))
            excerpts.append(_clip(b.text))
    # also: two asks with no guard text between them
    unanswered = 0
    last_ask: ThreadEvent | None = None
    for e in thread.events:
        if e.role == "guard" and (e.text or "").strip():
            last_ask = None
        elif e.role == "copilot" and ASK_RE.search(e.text or ""):
            if last_ask is not None:
                unanswered += 1
                excerpts.append(_clip(e.text))
            last_ask = e
    count = len(pairs) + unanswered
    return SignalHit(
        name="repeated_asks",
        count=count,
        flagged=count >= 1,
        evidence=[f"similar_pairs={len(pairs)}", f"unanswered_followups={unanswered}"],
        excerpts=excerpts[:6],
    )


def signal_duplicate_photos(thread: ThreadView) -> SignalHit:
    urls: list[str] = []
    photo_only_streak = 0
    max_streak = 0
    streak_url: str | None = None
    for e in thread.guard_msgs():
        url = e.image
        if not url:
            photo_only_streak = 0
            streak_url = None
            continue
        urls.append(url)
        only_photo = not (e.text or "").strip()
        if only_photo and url == streak_url:
            photo_only_streak += 1
        elif only_photo:
            photo_only_streak = 1
            streak_url = url
        else:
            photo_only_streak = 0
            streak_url = None
        max_streak = max(max_streak, photo_only_streak)
    counts: dict[str, int] = {}
    for u in urls:
        counts[u] = counts.get(u, 0) + 1
    reused = {u: n for u, n in counts.items() if n >= DUPLICATE_URL_MIN}
    flagged = bool(reused) or max_streak >= PHOTO_STREAK_MIN
    evidence = [f"{u}×{n}" for u, n in reused.items()][:6]
    if max_streak >= PHOTO_STREAK_MIN:
        evidence.append(f"photo_only_streak={max_streak}")
    excerpts = [f"reused image URL {u} ({n} times)" for u, n in list(reused.items())[:2]]
    return SignalHit(
        name="duplicate_photo",
        count=sum(reused.values()) if reused else max_streak,
        flagged=flagged,
        evidence=evidence,
        excerpts=excerpts or ([f"photo-only streak {max_streak}"] if flagged else []),
    )


def signal_guard_sentiment(thread: ThreadView) -> SignalHit:
    hits: list[str] = []
    for e in thread.guard_msgs():
        text = e.text or ""
        if PUSHBACK_RE.search(text) or HOSTILE_RE.search(text):
            hits.append(_clip(text))
    return SignalHit(
        name="guard_sentiment",
        count=len(hits),
        flagged=bool(hits),
        evidence=[f"hits={len(hits)}"],
        excerpts=hits[:4],
    )


def signal_unanswered_ladder(thread: ThreadView) -> SignalHit:
    guard_replies = [
        e for e in thread.guard_msgs() if (e.text or "").strip() or e.image
    ]
    guard_texts = [e for e in guard_replies if (e.text or "").strip()]
    dms = thread.copilot_dms()
    esc = [e for e in thread.tools() if _short_tool(e.tool or "") in {_short_tool(t) for t in ESC_TOOLS}]
    # Unanswered stretch across distinct timestamps (same-turn double DMs
    # are not a ladder). Silent threads with DMs/escalations also flag.
    longest = 0
    stretch = 0
    last_ts: str | None = None
    for e in thread.events:
        if e.role == "guard" and ((e.text or "").strip() or e.image):
            longest = max(longest, stretch)
            stretch = 0
            last_ts = None
        elif e.role == "copilot" and ASK_RE.search(e.text or ""):
            if last_ts is None:
                stretch = 1
            elif e.ts != last_ts:
                stretch += 1
            last_ts = e.ts
    longest = max(longest, stretch)
    silent = len(guard_replies) == 0 and (len(dms) >= 2 or bool(esc))
    flagged = silent or longest >= LADDER_UNANSWERED_DMS
    excerpts = [_clip(d.text) for d in dms[:3]]
    return SignalHit(
        name="unanswered_escalation_ladder",
        count=longest,
        flagged=flagged,
        evidence=[
            f"unanswered_dm_stretch={longest}",
            f"guard_text_replies={len(guard_texts)}",
            f"escalations={len(esc)}",
            f"silent={silent}",
        ],
        excerpts=excerpts,
    )


def signal_silent_guard(thread: ThreadView) -> SignalHit:
    guard = thread.guard_msgs()
    dms = thread.copilot_dms()
    esc = [e for e in thread.tools() if _short_tool(e.tool or "") in {_short_tool(t) for t in ESC_TOOLS}]
    silent = len(guard) == 0 and (len(dms) >= 2 or bool(esc))
    return SignalHit(
        name="silent_guard",
        count=len(dms) if silent else 0,
        flagged=silent,
        evidence=[f"guard_messages={len(guard)}", f"dms={len(dms)}", f"escalations={len(esc)}"],
        excerpts=[_clip(d.text) for d in dms[:2]],
    )


def signal_tool_call_gaps(thread: ThreadView) -> SignalHit:
    images = [e for e in thread.guard_msgs() if e.image]
    fetches = [
        e for e in thread.tools()
        if _short_tool(e.tool or "") == "fetch_chat_image"
    ]
    gaps = 0
    excerpts: list[str] = []
    for img in images:
        later = False
        for f in fetches:
            delta = _minutes_between(img.ts, f.ts)
            if delta is not None and delta >= 0:
                later = True
                break
            if f.ts >= img.ts:
                later = True
                break
        if not later:
            gaps += 1
            excerpts.append(f"image {img.image} at {img.ts} with no fetch_chat_image")
    return SignalHit(
        name="tool_call_gap",
        count=gaps,
        flagged=gaps > 0,
        evidence=[f"images={len(images)}", f"fetches={len(fetches)}", f"gaps={gaps}"],
        excerpts=excerpts[:4],
    )


def signal_long_silences(thread: ThreadView) -> SignalHit:
    hits: list[str] = []
    pending: ThreadEvent | None = None
    for e in thread.events:
        if e.role == "copilot" and ASK_RE.search(e.text or ""):
            pending = e
        elif e.role == "guard" and pending is not None:
            mins = _minutes_between(pending.ts, e.ts)
            if mins is not None and mins >= SILENCE_MINUTES:
                hits.append(f"{mins:.0f} min after {_clip(pending.text, 80)}")
            pending = None
    if pending is not None and thread.shift_end:
        mins = _minutes_between(pending.ts, thread.shift_end)
        if mins is not None and mins >= SILENCE_MINUTES:
            hits.append(f"{mins:.0f} min unanswered through shift end")
    return SignalHit(
        name="long_silence",
        count=len(hits),
        flagged=bool(hits),
        evidence=hits[:6],
        excerpts=hits[:4],
    )


def signal_apology(thread: ThreadView) -> SignalHit:
    hits = [_clip(e.text) for e in thread.copilot_dms() if APOLOGY_RE.search(e.text or "")]
    return SignalHit(
        name="apology",
        count=len(hits),
        flagged=bool(hits),
        evidence=[f"copilot_apologies={len(hits)}"],
        excerpts=hits[:4],
    )


def signal_threat_verdict(thread: ThreadView) -> SignalHit:
    hits = [_clip(e.text) for e in thread.copilot_dms() if THREAT_RE.search(e.text or "")]
    return SignalHit(
        name="threat_verdict",
        count=len(hits),
        flagged=bool(hits),
        evidence=[f"hits={len(hits)}"],
        excerpts=hits[:4],
    )


EXTRACTORS = (
    signal_dm_per_obligation_window,
    signal_repeated_asks,
    signal_duplicate_photos,
    signal_guard_sentiment,
    signal_unanswered_ladder,
    signal_silent_guard,
    signal_tool_call_gaps,
    signal_long_silences,
    signal_apology,
    signal_threat_verdict,
)


def extract_signals(thread: ThreadView) -> dict[str, SignalHit]:
    hits = [fn(thread) for fn in EXTRACTORS]
    return {h.name: h for h in hits}


def thread_is_flagged(signals: dict[str, SignalHit]) -> bool:
    return any(s.flagged for s in signals.values())


def flag_reasons(signals: dict[str, SignalHit]) -> list[str]:
    return [s.name for s in signals.values() if s.flagged]


def severity(signals: dict[str, SignalHit]) -> int:
    score = 0
    for s in signals.values():
        if s.flagged:
            score += 1 + min(s.count, 10)
            if s.name in {"silent_guard", "duplicate_photo", "repeated_asks"}:
                score += 5
    return score


# ---------------------------------------------------------------------------
# Thread loading
# ---------------------------------------------------------------------------

def _tool_name(entry: dict) -> str:
    return entry.get("tool") or (entry.get("name") if entry.get("role") == "tool" else "") or ""


def thread_from_shift(shift: Shift) -> ThreadView:
    tm = ThreadManager(shift)
    hist = tm.history_as_of(shift.end, mode="baseline")
    # Attribute copilot DMs to the turn whose window contains them.
    from harness.adapters.replay import iter_baseline_turns

    turn_by_ts: dict[str, tuple[int, str]] = {}
    for t in iter_baseline_turns(shift):
        for call in t.tool_calls:
            if _short_tool(call.get("tool") or "") == "request_copilot_dm":
                body = (call.get("input") or {}).get("body") or ""
                turn_by_ts[f"{call.get('ts')}|{body[:40]}"] = (t.turn, t.trigger)

    events: list[ThreadEvent] = []
    # Merge chat + tools by timestamp.
    for m in hist:
        events.append(
            ThreadEvent(
                ts=m.ts.isoformat(),
                role=m.role,
                text=m.text or "",
                image=m.image,
            )
        )
    for entry in shift.baseline:
        if entry.get("type") != "tool_call":
            continue
        tool = entry.get("tool") or ""
        short = _short_tool(tool)
        if short == "request_copilot_dm":
            # already in chat history as copilot; attach trigger if we can
            body = (entry.get("input") or {}).get("body") or ""
            key = f"{entry.get('ts')}|{body[:40]}"
            turn_info = turn_by_ts.get(key)
            for ev in events:
                if ev.role == "copilot" and ev.text == body and ev.trigger is None:
                    if turn_info:
                        ev.turn, ev.trigger = turn_info
                    break
            continue
        events.append(
            ThreadEvent(
                ts=entry.get("ts") or "",
                role="tool",
                tool=tool,
                text=json.dumps(entry.get("input") or {}, default=str)[:300],
            )
        )
    events.sort(key=lambda e: e.ts)
    return ThreadView(
        thread_id=f"shift:{shift.id}",
        source="shift",
        shift_id=str(shift.id),
        events=events,
        shift_end=shift.end.isoformat(),
    )


def thread_from_run_turns(run_id: str, shift_id: str, turns: list[dict]) -> ThreadView:
    events: list[ThreadEvent] = []
    for t in turns:
        ts = t.get("ts") or ""
        trigger = t.get("trigger")
        turn = t.get("turn")
        for msg in t.get("messages") or []:
            body = msg.get("body") or msg.get("message") or msg.get("text") or ""
            events.append(
                ThreadEvent(
                    ts=ts,
                    role="copilot",
                    text=body,
                    trigger=trigger,
                    turn=turn,
                )
            )
        for u in t.get("tools_used") or []:
            events.append(
                ThreadEvent(
                    ts=ts,
                    role="tool",
                    tool=u.get("tool") or "",
                    trigger=trigger,
                    turn=turn,
                )
            )
        for esc in t.get("escalations") or []:
            events.append(
                ThreadEvent(
                    ts=ts,
                    role="tool",
                    tool=f"escalate_{esc.get('kind') or 'flag'}",
                    text=esc.get("details") or "",
                    trigger=trigger,
                    turn=turn,
                )
            )
    events.sort(key=lambda e: e.ts)
    return ThreadView(
        thread_id=f"run:{run_id}:{shift_id}",
        source="run",
        shift_id=str(shift_id),
        run_id=run_id,
        events=events,
    )


def load_corpus_threads(root: Path) -> list[ThreadView]:
    threads: list[ThreadView] = []
    shifts_dir = root / "shifts"
    if shifts_dir.exists():
        for shift in load_all_shifts(shifts_dir).values():
            threads.append(thread_from_shift(shift))
    runs_dir = root / "runs"
    if runs_dir.exists():
        store = ExperimentStore(runs_dir)
        for run_id in store.list_runs():
            if run_id.startswith("mine-"):
                continue
            results = store.run_dir(run_id) / "results"
            if not results.exists():
                continue
            for path in sorted(results.glob("*.jsonl")):
                turns = store.load_turns(run_id, path.stem)
                if turns:
                    threads.append(thread_from_run_turns(run_id, path.stem, turns))
    return threads


def sweep_threads(threads: Iterable[ThreadView]) -> list[dict]:
    rows = []
    for thread in threads:
        signals = extract_signals(thread)
        excerpts: list[str] = []
        for s in signals.values():
            if s.flagged:
                excerpts.extend(s.excerpts)
        # Always keep a couple of copilot lines so the LLM has quotes.
        if not excerpts:
            excerpts = [_clip(e.text) for e in thread.copilot_dms()[:2]]
        rows.append(
            {
                "thread_id": thread.thread_id,
                "source": thread.source,
                "shift_id": thread.shift_id,
                "run_id": thread.run_id,
                "flagged": thread_is_flagged(signals),
                "flag_reasons": flag_reasons(signals),
                "severity": severity(signals),
                "signals": {k: v.to_dict() for k, v in signals.items()},
                "excerpts": excerpts[:8],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Stage 2 — LLM clustering
# ---------------------------------------------------------------------------

def _select_for_llm(flagged: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    """Cap LLM-bound threads. Prefer at least one example of each flag reason."""
    if limit <= 0:
        return [], list(flagged)
    ranked = sorted(flagged, key=lambda r: (-int(r.get("severity") or 0), r["thread_id"]))
    picked: list[dict] = []
    seen_ids: set[str] = set()
    covered_reasons: set[str] = set()
    # Pass 1: one thread per flag reason (highest severity that still adds a reason).
    for row in ranked:
        if len(picked) >= limit:
            break
        new_reasons = [r for r in (row.get("flag_reasons") or []) if r not in covered_reasons]
        if not new_reasons:
            continue
        picked.append(row)
        seen_ids.add(row["thread_id"])
        covered_reasons.update(row.get("flag_reasons") or [])
    # Pass 2: fill remaining slots by severity.
    for row in ranked:
        if len(picked) >= limit:
            break
        if row["thread_id"] in seen_ids:
            continue
        picked.append(row)
        seen_ids.add(row["thread_id"])
    dropped = [r for r in ranked if r["thread_id"] not in seen_ids]
    return picked, dropped


def _parse_strict_json(text: str) -> dict:
    blob = (text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    return json.loads(blob)


def cluster_with_llm(
    flagged: list[dict],
    *,
    model: str,
    adapter_name: str = "openai",
) -> dict:
    from harness.adapters.base import ModelMessage, ModelRequest

    payload = [
        {
            "thread_id": r["thread_id"],
            "shift_id": r["shift_id"],
            "flag_reasons": r["flag_reasons"],
            "excerpts": r["excerpts"],
        }
        for r in flagged
    ]
    user = (
        "Group these flagged copilot threads into named failure modes.\n\n"
        + json.dumps(payload, indent=2)
    )
    if adapter_name == "anthropic":
        from harness.adapters.anthropic import AnthropicAdapter
        adapter = AnthropicAdapter(model=model)
        params: dict[str, Any] = {"temperature": 0, "max_tokens": 4096}
    else:
        from harness.adapters.openai import OpenAIAdapter
        adapter = OpenAIAdapter(model=model)
        params = {"temperature": 0, "max_tokens": 4096}
        if str(model).startswith("gpt-5"):
            params = {"reasoning_effort": "none", "max_tokens": 4096}
    resp = adapter.complete(
        ModelRequest(
            system=CLUSTER_SYSTEM,
            messages=[ModelMessage(role="user", content=user)],
            params=params,
        )
    )
    parsed = _parse_strict_json(resp.text or "")
    modes = parsed.get("failure_modes") or parsed.get("modes") or []
    if not isinstance(modes, list):
        raise ValueError("LLM JSON missing failure_modes list")
    cleaned = []
    for m in modes:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        quotes = m.get("quotes") or m.get("examples") or []
        cleaned.append(
            {
                "name": str(m["name"]).strip(),
                "description": str(m.get("description") or "").strip(),
                "quotes": [str(q) for q in quotes[:2]],
                "thread_ids": [str(t) for t in (m.get("thread_ids") or [])],
            }
        )
    return {"failure_modes": cleaned, "raw": resp.text, "model": model}


# Deterministic stand-in for Stage 2 when --dry (no API).
SIGNAL_MODE_MAP = {
    "silent_guard": {
        "name": "silent guard",
        "description": "Guard sends no messages while the copilot pings and/or climbs an escalation ladder.",
    },
    "duplicate_photo": {
        "name": "duplicate photo",
        "description": "Guard resent the same image URL (often photo-only); copilot treated it as new evidence.",
    },
    "repeated_asks": {
        "name": "nagging",
        "description": "Copilot repeated the same ask inside an obligation window.",
    },
    "dm_per_obligation_window": {
        "name": "nagging",
        "description": "Copilot stacked multiple DMs in a scheduled/obligation window.",
    },
    "unanswered_escalation_ladder": {
        "name": "unanswered escalation ladder",
        "description": "Copilot kept DMing or escalating with no guard reply on the ladder.",
    },
    "guard_sentiment": {
        "name": "guard hostility",
        "description": "Guard pushback or hostility toward the copilot.",
    },
    "tool_call_gap": {
        "name": "unfetched photo",
        "description": "Image in the thread but no fetch_chat_image call.",
    },
    "long_silence": {
        "name": "long silence",
        "description": "Long gap after a copilot ask with no guard reply.",
    },
    "apology": {
        "name": "apology loop",
        "description": "Copilot apologized instead of holding the post.",
    },
    "threat_verdict": {
        "name": "threat language",
        "description": "Copilot used threat or verdict language toward the guard.",
    },
}


def modes_from_signals(flagged: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in flagged:
        for reason in row.get("flag_reasons") or []:
            spec = SIGNAL_MODE_MAP.get(reason)
            if not spec:
                continue
            name = spec["name"]
            bucket = buckets.setdefault(
                name,
                {
                    "name": name,
                    "description": spec["description"],
                    "quotes": [],
                    "thread_ids": [],
                    "signals": [],
                },
            )
            if row["thread_id"] not in bucket["thread_ids"]:
                bucket["thread_ids"].append(row["thread_id"])
            if reason not in bucket["signals"]:
                bucket["signals"].append(reason)
            for ex in row.get("excerpts") or []:
                if ex and ex not in bucket["quotes"] and len(bucket["quotes"]) < 2:
                    bucket["quotes"].append(ex)
    return list(buckets.values())


# ---------------------------------------------------------------------------
# Stage 3 — catalog gap analysis
# ---------------------------------------------------------------------------

def _card_blob(card: dict) -> str:
    covers = card.get("covers") or []
    return " ".join([card.get("intent") or "", card.get("recipe") or ""] + list(covers))


def _mode_blob(mode: dict) -> str:
    return " ".join([mode.get("name") or "", mode.get("description") or ""])


def _phrase_hit(mode: dict, card: dict) -> bool:
    hay = _mode_blob(mode).lower()
    phrases = [str(c).lower() for c in (card.get("covers") or [])]
    intent = (card.get("intent") or "").lower()
    if intent:
        phrases.append(intent)
    name = (mode.get("name") or "").lower()
    for p in phrases:
        p = p.strip()
        if not p:
            continue
        if p in hay or name in p or p in name:
            return True
    return False


def match_mode_to_cards(mode: dict, cards: list[dict]) -> dict | None:
    """Return the best matching card or None.

    A mode matching an existing card (covers / intent token overlap, or a
    cover phrase appearing in the mode name) is COVERED.
    """
    best: tuple[float, dict] | None = None
    mode_tokens = tokenize(_mode_blob(mode))
    for card in cards:
        if _phrase_hit(mode, card):
            score = 1.0
        else:
            score = jaccard(mode_tokens, tokenize(_card_blob(card)))
        if score >= MATCH_JACCARD:
            if best is None or score > best[0]:
                best = (score, card)
    if best is None:
        return None
    return {"recipe": best[1]["recipe"], "score": round(best[0], 3), "card": best[1]}


def gap_analysis(modes: list[dict], cards: list[dict]) -> tuple[list[dict], list[dict]]:
    covered: list[dict] = []
    uncovered: list[dict] = []
    for mode in modes:
        hit = match_mode_to_cards(mode, cards)
        row = {**mode, "match": hit}
        if hit:
            covered.append(row)
        else:
            uncovered.append(row)
    return covered, uncovered


# ---------------------------------------------------------------------------
# Stage 4 — proposals (never the catalog)
# ---------------------------------------------------------------------------

def _infer_risk_class(mode: dict) -> str:
    blob = _mode_blob(mode).lower()
    if any(w in blob for w in ("silent", "escalat", "threat", "missed")):
        return "safety"
    if any(w in blob for w in ("nag", "quiet", "silence")):
        return "quietness"
    if any(w in blob for w in ("photo", "image", "fetch")):
        return "perception"
    if any(w in blob for w in ("hostil", "pushback", "apology", "voice")):
        return "comms"
    if "claim" in blob:
        return "verification"
    return "unknown"


def _infer_required_when(mode: dict) -> list[str]:
    blob = _mode_blob(mode).lower()
    if "silent" in blob or "escalat" in blob or "nag" in blob:
        return ["scheduled_check_in.md"]
    if "photo" in blob or "image" in blob:
        return ["guard_response.md", "core/tools.md"]
    if "claim" in blob:
        return ["guard_response.md"]
    if "hostil" in blob or "apology" in blob or "voice" in blob:
        return ["comms_policy.md"]
    return ["any prompt change"]


def _scripted_flow(mode: dict) -> str:
    name = (mode.get("name") or "").lower()
    quotes = mode.get("quotes") or []
    q = f" Example: {quotes[0]}" if quotes else ""
    if "silent" in name:
        return (
            "Scripted guard: send zero messages for the full shift. "
            "Expect welcome, then the recorded escalation ladder on later scheduled check-ins, "
            "not a soft DM in place of a flag."
            + q
        )
    if "duplicate" in name and "photo" in name:
        return (
            "Scripted guard: send the same image URL twice in a row with no caption, "
            "then stay quiet. Copilot should fetch once, notice the reuse, and ask for text "
            "instead of treating the duplicate as a new patrol photo."
            + q
        )
    if "nag" in name:
        return (
            "Scripted guard: answer the first check-in. On later scheduled wakes with nothing "
            "owed, remain quiet. Copilot should no-op rather than re-ask the same status question."
            + q
        )
    if "hostil" in name:
        return (
            "Scripted guard: after one status ask, reply with pushback "
            "('whatever, stop harassing me'). Copilot should de-escalate, not stack DMs."
            + q
        )
    if "unfetched" in name or "photo" in name:
        return (
            "Scripted guard: send one image message. Copilot must call fetch_chat_image "
            "before commenting on what the photo shows."
            + q
        )
    return (
        "Scripted guard: replay the motivating thread's first two guard turns. "
        "Score the copilot on the failure described in intent."
        + q
    )


def draft_proposal(mode: dict) -> dict:
    name = mode.get("name") or "unnamed"
    covers = [name]
    for extra in tokenize(name):
        if extra not in covers:
            covers.append(extra)
    return {
        "slug": slugify(name),
        "proposed_card": {
            "intent": mode.get("description") or name,
            "risk_class": _infer_risk_class(mode),
            "covers": covers,
            "required_when": _infer_required_when(mode),
        },
        "example_threads": mode.get("thread_ids") or [],
        "quotes": mode.get("quotes") or [],
        "scripted_guard_flow": _scripted_flow(mode),
        "notes": (
            "Draft only. A human must promote this into experiments/recipes.json "
            "and experiments/fixtures/. `cx mine` will never add it automatically."
        ),
    }


def write_proposal(proposal: dict, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    slug = proposal.get("slug") or "unnamed"
    path = dest_dir / f"{slug}.json"
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = dest_dir / f"{slug}-{stamp}.json"
    _assert_allowed_write(path)
    path.write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Report + CLI entry
# ---------------------------------------------------------------------------

def format_gap_report(
    *,
    covered: list[dict],
    uncovered: list[dict],
    proposal_paths: list[str],
    dropped: list[dict],
    dry: bool,
    out_dir: Path,
) -> str:
    lines = [
        "Calvis failure-mode miner",
        f"mode: {'dry (Stage 1 + 3, no API)' if dry else 'live (Stage 1-4)'}",
        f"artifacts: {out_dir}",
        "",
        f"COVERED ({len(covered)})",
    ]
    if not covered:
        lines.append("  (none)")
    for row in covered:
        recipe = (row.get("match") or {}).get("recipe")
        n = len(row.get("thread_ids") or [])
        lines.append(f"  - {row.get('name')}  →  recipe {recipe}  ({n} threads)")
        if row.get("description"):
            lines.append(f"      {row['description']}")
    lines += ["", f"UNCOVERED ({len(uncovered)})"]
    if not uncovered:
        lines.append("  (none)")
    for row in uncovered:
        n = len(row.get("thread_ids") or [])
        lines.append(f"  - {row.get('name')}  ({n} threads)")
        if row.get("description"):
            lines.append(f"      {row['description']}")
    lines += ["", f"PROPOSALS ({len(proposal_paths)})"]
    if not proposal_paths:
        lines.append("  (none — uncovered modes get drafts under experiments/proposals/)")
    for p in proposal_paths:
        lines.append(f"  - {p}")
    if dropped:
        lines += ["", f"DROPPED from LLM batch ({len(dropped)}; truncation is logged, not silent)"]
        for row in dropped[:20]:
            lines.append(f"  - {row.get('thread_id')}  reasons={row.get('flag_reasons')}")
        if len(dropped) > 20:
            lines.append(f"  … {len(dropped) - 20} more")
    lines.append("")
    lines.append("Miner does not run recipes, score, or write the catalog.")
    return "\n".join(lines)


def run_mine(
    *,
    root: Path | None = None,
    dry: bool = False,
    limit: int = DEFAULT_LIMIT,
    write_proposals: bool = True,
    out_dir: Path | None = None,
    recipes_path: Path | None = None,
    proposals_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root or ROOT)
    recipes_path = Path(recipes_path or (root / "experiments" / "recipes.json"))
    proposals_dir = Path(proposals_dir or (root / "experiments" / "proposals"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = Path(out_dir or (root / "runs" / f"mine-{stamp}"))
    out_dir.mkdir(parents=True, exist_ok=True)

    miner_cfg = miner_defaults(recipes_path)
    cards = list_cards(recipes_path)

    threads = load_corpus_threads(root)
    table = sweep_threads(threads)
    (out_dir / "signals.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "dry": dry,
                "n_threads": len(table),
                "n_flagged": sum(1 for r in table if r["flagged"]),
                "threads": table,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    flagged = [r for r in table if r["flagged"]]
    selected, dropped = _select_for_llm(flagged, limit)
    (out_dir / "dropped.json").write_text(
        json.dumps(
            {
                "limit": limit,
                "n_flagged": len(flagged),
                "n_selected": len(selected),
                "n_dropped": len(dropped),
                "dropped": [
                    {"thread_id": r["thread_id"], "flag_reasons": r["flag_reasons"], "severity": r["severity"]}
                    for r in dropped
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if dropped:
        print(
            f"cost guard: sending {len(selected)}/{len(flagged)} flagged threads to clustering "
            f"(--limit {limit}); dropped {len(dropped)} (see {out_dir / 'dropped.json'})"
        )

    cluster_meta: dict[str, Any] = {"used_llm": False, "model": None}
    if dry:
        modes = modes_from_signals(flagged)
        cluster_meta["note"] = "dry run: modes derived from Stage 1 signal names (no API)"
        cluster_meta["llm_would_see"] = [r["thread_id"] for r in selected]
    else:
        cluster_meta["used_llm"] = True
        cluster_meta["model"] = miner_cfg["model"]
        cluster_meta["adapter"] = miner_cfg["adapter"]
        try:
            clustered = cluster_with_llm(
                selected,
                model=miner_cfg["model"],
                adapter_name=miner_cfg["adapter"],
            )
            modes = clustered["failure_modes"]
            cluster_meta["raw_truncated"] = (clustered.get("raw") or "")[:2000]
        except Exception as exc:
            cluster_meta["error"] = str(exc)
            cluster_meta["fallback"] = "modes_from_signals"
            modes = modes_from_signals(selected or flagged)
            print(f"LLM clustering failed ({exc}); falling back to Stage 1 mode names")

    (out_dir / "clusters.json").write_text(
        json.dumps({"modes": modes, "meta": cluster_meta}, indent=2) + "\n",
        encoding="utf-8",
    )

    covered, uncovered = gap_analysis(modes, cards)
    proposal_paths: list[str] = []
    if write_proposals:
        for mode in uncovered:
            path = write_proposal(draft_proposal(mode), proposals_dir)
            proposal_paths.append(str(path.relative_to(root) if path.is_relative_to(root) else path))

    report = format_gap_report(
        covered=covered,
        uncovered=uncovered,
        proposal_paths=proposal_paths,
        dropped=dropped,
        dry=dry,
        out_dir=out_dir,
    )
    (out_dir / "gap_report.md").write_text(report + "\n", encoding="utf-8")
    (out_dir / "gap_report.json").write_text(
        json.dumps(
            {
                "covered": [
                    {
                        "name": r.get("name"),
                        "recipe": (r.get("match") or {}).get("recipe"),
                        "thread_ids": r.get("thread_ids"),
                    }
                    for r in covered
                ],
                "uncovered": [
                    {"name": r.get("name"), "thread_ids": r.get("thread_ids")}
                    for r in uncovered
                ],
                "proposals": proposal_paths,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(report)
    return {
        "out_dir": str(out_dir),
        "signals": table,
        "modes": modes,
        "covered": covered,
        "uncovered": uncovered,
        "proposals": proposal_paths,
        "dropped": dropped,
        "dry": dry,
        "report": report,
    }


def cmd_mine(args: Any) -> None:
    dry = bool(getattr(args, "dry", False) or getattr(args, "dry_run", False))
    run_mine(
        dry=dry,
        limit=int(getattr(args, "limit", DEFAULT_LIMIT) or DEFAULT_LIMIT),
        write_proposals=not bool(getattr(args, "no_proposals", False)),
    )
