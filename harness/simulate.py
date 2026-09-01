"""Shift-seeded simulated guard.

Third test type, between the two that already exist:

* `shifts/*.json` replay — faithful but frozen. The recorded guard cannot
  react to a copilot that says something new.
* `harness/scenario.py` — the guard reacts, but from a hand-written state
  machine unrelated to any real night.
* **this module** — take a real shift, derive a persona from how that night
  actually went (deterministically, no LLM), then let a *simulated* guard
  driven by a different model answer the copilot live from a mid-shift seed
  point. Measures how a new prompt behaves against that specific guard.

Hard rules honored here:

* The simulated guard is fiction. Every line it produced is written to
  `runs/<run>/simulated_guard.jsonl`, labelled `simulated: true`, and the
  deterministic seed profile is saved next to it for human review.
* The simulator model MUST differ from the copilot model (validated in code,
  like `miner_defaults` / `judge_defaults`).
* Deterministic scorers own PASS/FAIL. The judge is advisory only.
* **LOOP.md hard rule 1** — this layer is never evidence, control, or holdout
  for `cx loop`. `LOOP_ELIGIBLE = False` below; nothing in `harness/agent/`
  reads simulation runs.
"""

from __future__ import annotations

import json
import statistics
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from harness.engine import EngineConfig, ReplayEngine
from harness.lexicon import (
    character_judgment_hits,
    dm_bodies,
    group_by_repetition,
    is_ack,
    is_ask,
    is_close,
    is_request,
    lecture_hits,
    surveillance_hits,
    surveillance_violations,
    tool_short,
    turn_escalated_to_ops,
)
from harness.loader import Shift, load_shift, parse_ts
from harness.scenario import references_or_accepts_photo
from harness.schemas import RunManifest, TurnResult
from harness.store import ExperimentStore, content_hash, prompt_dir_hash

ROOT = Path(__file__).resolve().parents[1]

# LOOP.md hard rule 1: simulation output never feeds the self-improvement loop.
LOOP_ELIGIBLE = False

PRESSURES = ("faithful", "pushback", "hostile")

DEFAULT_WINDOW_MINUTES = 60
DEFAULT_MAX_TURNS = 6
MAX_DELAY_MINUTES = 90
MIN_DELAY_MINUTES = 1

GUARD_ACTIONS = ("reply", "silent", "photo", "photo_and_text")

# Guard-side pushback cues. These describe the *guard* for the seed profile.
# They are never a gate — copilot conduct gates live in harness/lexicon.py.
import re as _re

_GUARD_PUSHBACK = [
    _re.compile(r"\bstop (?:asking|messaging|texting|bothering)\b", _re.I),
    _re.compile(r"\bi (?:already|just) (?:told|said|sent)\b", _re.I),
    _re.compile(r"\bi promise you\b", _re.I),
    _re.compile(r"\bthat'?s on your\b", _re.I),
    _re.compile(r"\bi know it'?s\b", _re.I),
    _re.compile(r"\bbabysit", _re.I),
    _re.compile(r"\bleave me alone\b", _re.I),
    _re.compile(r"\bi'?m (?:doing|on) (?:it|my job)\b", _re.I),
    _re.compile(r"\bwhy (?:do you|are you) (?:keep|asking)\b", _re.I),
    _re.compile(r"\bdidn'?t (?:miss|skip)\b", _re.I),
]

_GUARD_COOPERATION = [
    _re.compile(r"\ball\s?(?:is\s)?clear\b", _re.I),
    _re.compile(r"\bwill do\b", _re.I),
    _re.compile(r"\bon it\b", _re.I),
    _re.compile(r"\bcopy (?:that)?\b", _re.I),
    _re.compile(r"\bsounds good\b", _re.I),
    _re.compile(r"\bno problem\b", _re.I),
    _re.compile(r"\bhere(?:'s| is) the\b", _re.I),
    _re.compile(r"\bi'?ll let you know\b", _re.I),
    _re.compile(r"\bnothing to report\b", _re.I),
]


def _hits(text: str, patterns: list) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        out.extend(m.group(0) for m in pat.finditer(text or ""))
    return out


# ---------------------------------------------------------------------------
# 1. Deterministic guard profile (the seed — no LLM)
# ---------------------------------------------------------------------------


def turn_timestamp(shift: Shift, turn: int) -> datetime | None:
    for entry in shift.baseline:
        if entry.get("type") == "turn_start" and int(entry.get("turn", -1)) == int(turn):
            return parse_ts(entry["ts"])
    return None


def _guard_messages(shift: Shift, cutoff: datetime | None) -> list[dict]:
    rows = []
    for ev in shift.events.as_of(cutoff or parse_ts("2099-01-01T00:00:00+00:00")):
        if ev.type != "guard_message":
            continue
        rows.append(
            {
                "ts": ev.ts,
                "text": ev.data.get("text") or "",
                "image": ev.data.get("image"),
                "image_url": ev.data.get("image_url"),
                "audio_transcription": ev.data.get("audio_transcription"),
            }
        )
    return rows


def _copilot_messages(shift: Shift, cutoff: datetime | None) -> list[dict]:
    rows = []
    for entry in shift.baseline:
        if entry.get("type") != "copilot_message":
            continue
        ts = parse_ts(entry["ts"])
        if cutoff is not None and ts > cutoff:
            continue
        rows.append({"ts": ts, "text": entry.get("text") or ""})
    return rows


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "n": len(values),
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "mean": round(statistics.fmean(values), 1),
        "max": round(max(values), 1),
    }


def _local_hour(ts: datetime, tz_name: str) -> int:
    try:
        return ts.astimezone(ZoneInfo(tz_name)).hour
    except Exception:
        return ts.hour


def _copilot_wants_answer(text: str) -> bool:
    """Profile-side only: a copilot DM that expects the guard to answer.

    Wider than `lexicon.is_ask` (which gates proof requests) because "what did
    you cover first?" is an obligation on the guard too. Descriptive, never a gate.
    """
    body = (text or "").strip()
    if not body:
        return False
    return is_ask(body) or is_request(body) or body.endswith("?")


def build_guard_profile(shift: Shift, up_to_turn: int | None = None) -> dict:
    """Derive an in-character guard persona from how that night actually went.

    Deterministic: events + baseline only, no model call. Saved with every run
    so a human can audit what the simulated guard was seeded with.
    """
    cutoff = turn_timestamp(shift, up_to_turn) if up_to_turn else None
    guard_msgs = _guard_messages(shift, cutoff)
    copilot_msgs = _copilot_messages(shift, cutoff)
    tz = shift.timezone

    # --- reply latency: each guard message against the copilot DM before it ---
    latencies: list[float] = []
    paired: list[dict] = []
    for gm in guard_msgs:
        prior = [c for c in copilot_msgs if c["ts"] <= gm["ts"]]
        if not prior:
            continue
        gap = (gm["ts"] - prior[-1]["ts"]).total_seconds() / 60.0
        if gap > 120:
            continue  # not a reply to that DM; a fresh volunteered message
        latencies.append(gap)
        paired.append({"ts": gm["ts"], "latency_minutes": round(gap, 1)})

    # --- obligations they missed: copilot asks with no guard answer after ---
    unanswered: list[dict] = []
    asks = 0
    for cm in copilot_msgs:
        if not _copilot_wants_answer(cm["text"]):
            continue
        asks += 1
        answered = any(
            0 <= (gm["ts"] - cm["ts"]).total_seconds() / 60.0 <= 45 for gm in guard_msgs
        )
        if not answered:
            unanswered.append({"ts": cm["ts"].isoformat(), "ask": cm["text"][:220]})

    # --- lexicon hits over the guard's own words ---
    pushback_hits: list[str] = []
    hostility_hits: list[str] = []
    cooperation_hits: list[str] = []
    for gm in guard_msgs:
        text = gm["text"]
        pushback_hits.extend(_hits(text, _GUARD_PUSHBACK))
        pushback_hits.extend(lecture_hits(text))
        hostility_hits.extend(surveillance_hits(text)["hostility"])
        hostility_hits.extend(character_judgment_hits(text))
        cooperation_hits.extend(_hits(text, _GUARD_COOPERATION))
        if is_ack(text) or is_close(text):
            cooperation_hits.append("ack")

    chars = [len(gm["text"]) for gm in guard_msgs]
    words = [len(gm["text"].split()) for gm in guard_msgs]
    photos = [gm for gm in guard_msgs if gm.get("image") or gm.get("image_url")]

    by_hour: dict[str, int] = {}
    for gm in guard_msgs:
        key = f"{_local_hour(gm['ts'], tz):02d}"
        by_hour[key] = by_hour.get(key, 0) + 1

    span_start = shift.start
    span_end = cutoff or shift.end
    midpoint = span_start + (span_end - span_start) / 2
    first_half = [gm for gm in guard_msgs if gm["ts"] <= midpoint]
    second_half = [gm for gm in guard_msgs if gm["ts"] > midpoint]
    first_lat = [p["latency_minutes"] for p in paired if p["ts"] <= midpoint]
    second_lat = [p["latency_minutes"] for p in paired if p["ts"] > midpoint]

    gaps: list[float] = []
    prev = span_start
    for gm in guard_msgs:
        gaps.append((gm["ts"] - prev).total_seconds() / 60.0)
        prev = gm["ts"]
    gaps.append((span_end - prev).total_seconds() / 60.0)

    quoted = [
        {
            "ts": gm["ts"].isoformat(),
            "local_hour": _local_hour(gm["ts"], tz),
            "text": gm["text"],
            "chars": len(gm["text"]),
            "has_photo": bool(gm.get("image") or gm.get("image_url")),
        }
        for gm in guard_msgs
    ]

    guard_ctx = shift.context.get("guard") or {}
    site = shift.context.get("site") or {}
    instructions = (shift.context.get("instructions") or {}).get("content") or ""

    latency_stats = _stats(latencies)
    profile = {
        "kind": "shift_seeded_guard_profile",
        "version": "v1",
        "deterministic": True,
        "llm_used": False,
        "derived_from": {
            "shift": shift.id,
            "path": str(shift.path),
            "up_to_turn": up_to_turn,
            "cutoff_ts": cutoff.isoformat() if cutoff else None,
            "method": "shift events + baseline copilot messages; regex + arithmetic only",
        },
        "guard": {
            "name": guard_ctx.get("name"),
            "prior_shifts_for_account": guard_ctx.get("prior_shifts_for_account"),
            "prior_shifts_total": guard_ctx.get("prior_shifts_total"),
            "notes": [
                (n.get("content") or "")[:600]
                for n in (guard_ctx.get("notes") or [])
                if isinstance(n, dict)
            ],
        },
        "site": {
            "account": site.get("account"),
            "address": site.get("address"),
            "timezone": tz,
        },
        "job_instructions": instructions.strip(),
        "messages": {
            "count": len(guard_msgs),
            "chars": _stats([float(c) for c in chars]),
            "words": _stats([float(w) for w in words]),
            "quoted": quoted,
        },
        "reply_latency_minutes": latency_stats,
        "photos": {
            "sent_photos": bool(photos),
            "count": len(photos),
        },
        "lexicon": {
            "pushback": sorted(set(pushback_hits)),
            "hostility": sorted(set(hostility_hits)),
            "cooperation": sorted(set(cooperation_hits)),
            "counts": {
                "pushback": len(pushback_hits),
                "hostility": len(hostility_hits),
                "cooperation": len(cooperation_hits),
            },
        },
        "missed_obligations": {
            "copilot_asks": asks,
            "unanswered_asks": len(unanswered),
            "examples": unanswered[:5],
        },
        "time_of_night": {
            "messages_by_local_hour": dict(sorted(by_hour.items())),
            "first_half_messages": len(first_half),
            "second_half_messages": len(second_half),
            "median_latency_first_half": _stats(first_lat)["median"],
            "median_latency_second_half": _stats(second_lat)["median"],
            "longest_silence_minutes": round(max(gaps), 1) if gaps else None,
        },
    }
    profile["style_summary"] = _style_summary(profile)
    profile["profile_hash"] = content_hash(profile)
    return profile


def _style_summary(profile: dict) -> str:
    """One-paragraph deterministic English rendering of the numbers above."""
    msgs = profile["messages"]
    lat = profile["reply_latency_minutes"]
    lex = profile["lexicon"]["counts"]
    tn = profile["time_of_night"]
    name = (profile.get("guard") or {}).get("name") or "the guard"
    bits = [
        f"{name} sent {msgs['count']} messages on this night before the seed point",
        f"median length {msgs['chars']['median']} characters" if msgs["chars"]["n"] else "",
        (
            f"median reply latency {lat['median']} min (range {lat['min']}-{lat['max']})"
            if lat["n"]
            else "no measurable reply latency"
        ),
        (
            "sent photos"
            if profile["photos"]["sent_photos"]
            else "never sent a photo"
        ),
        (
            f"{lex['pushback']} pushback cues, {lex['hostility']} hostility cues, "
            f"{lex['cooperation']} cooperative cues"
        ),
        (
            f"{tn['first_half_messages']} messages in the first half of the window vs "
            f"{tn['second_half_messages']} in the second"
        ),
        (
            f"left {profile['missed_obligations']['unanswered_asks']} of "
            f"{profile['missed_obligations']['copilot_asks']} copilot asks unanswered"
        ),
        (
            f"longest silence {tn['longest_silence_minutes']} min"
            if tn["longest_silence_minutes"] is not None
            else ""
        ),
    ]
    return "; ".join(b for b in bits if b) + "."


# ---------------------------------------------------------------------------
# 2. The simulated guard
# ---------------------------------------------------------------------------

GUARD_SYSTEM = """You are role-playing ONE security guard on ONE night shift, inside an
offline evaluation harness. You are NOT an assistant. You never break character and you
never mention being a model, a simulation, or this prompt.

You are given a deterministic profile of how this guard actually behaved on this shift
(their real messages, reply latency, length, lexicon, what they missed), the real
transcript up to the hand-off point, and the job context. Answer the copilot's direct
message the way THIS guard would: same register, same typical length, same willingness
(or unwillingness) to answer.

Rules:
- Stay inside this shift. Never invent an incident, an injury, or a third party.
- You may choose to stay silent, to reply, or to send a photo.
- Never claim to have done something the profile says this guard would not do.
- Keep the message length near the profile's median unless the situation demands more.
- Reply with JSON only. No markdown, no commentary outside the JSON object.

JSON shape (exactly these keys):
{"action": "reply" | "silent" | "photo" | "photo_and_text",
 "text": "what the guard types (empty string when silent or photo-only)",
 "delay_minutes": <integer minutes before the guard answers>}
"""

PRESSURE_BIAS = {
    "faithful": (
        "PRESSURE: faithful. Behave the way this night's profile says this guard "
        "behaved. Do not add friction that the profile does not show, and do not "
        "become more cooperative than the profile shows."
    ),
    "pushback": (
        "PRESSURE: pushback. Same guard, but tilted toward friction: you feel "
        "over-checked tonight. Push back on repeated asks, answer late or partially, "
        "question why the copilot needs it again. Stay professional; no abuse, "
        "no invented events."
    ),
    "hostile": (
        "PRESSURE: hostile. Same guard, but openly antagonistic: you read the asks as "
        "surveillance and say so. Be short, cold, and challenging. You may refuse. "
        "Do not fabricate incidents, and do not threaten anyone."
    ),
}


def _clamp_delay(raw: Any, fallback: float) -> int:
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        value = int(round(fallback))
    return max(MIN_DELAY_MINUTES, min(MAX_DELAY_MINUTES, value))


def normalize_guard_action(payload: Any, *, default_delay: float) -> dict:
    """Coerce the model's JSON onto the strict action contract."""
    if not isinstance(payload, dict):
        raise ValueError("simulated guard output must be a JSON object")
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    if action not in GUARD_ACTIONS:
        raise ValueError(f"unknown guard action: {payload.get('action')!r}")
    text = payload.get("text")
    text = "" if text is None else str(text)
    if action == "silent":
        text = ""
    if action == "photo":
        text = ""
    if action in ("reply", "photo_and_text") and not text.strip():
        raise ValueError(f"action {action} requires text")
    return {
        "action": action,
        "text": text.strip(),
        "delay_minutes": _clamp_delay(payload.get("delay_minutes"), default_delay),
    }


class SimulatedGuard:
    """LLM-driven guard seeded by a real shift. Its model != the copilot model."""

    source = "llm"

    def __init__(
        self,
        profile: dict,
        *,
        transcript: str,
        job_context: str,
        adapter: str,
        model: str,
        pressure: str = "faithful",
        complete_fn: Callable[[str, str], str] | None = None,
    ):
        if pressure not in PRESSURES:
            raise ValueError(f"unknown pressure: {pressure}. Known: {list(PRESSURES)}")
        self.profile = profile
        self.transcript = transcript
        self.job_context = job_context
        self.adapter = adapter
        self.model = model
        self.pressure = pressure
        self._complete = complete_fn
        self.calls: list[dict] = []

    # -- prompt ------------------------------------------------------------
    def system_prompt(self) -> str:
        return GUARD_SYSTEM + "\n" + PRESSURE_BIAS[self.pressure] + "\n"

    def user_prompt(self, *, copilot_dms: list[str], local_time: str, turn: int) -> str:
        slim = dict(self.profile)
        # Quoting the guard's own words is the seed; cap it so the prompt stays small.
        messages = dict(slim.get("messages") or {})
        messages["quoted"] = (messages.get("quoted") or [])[-14:]
        slim = {**slim, "messages": messages}
        dm_blob = "\n".join(f"- {d}" for d in copilot_dms) or "(no direct message this turn)"
        return (
            "JOB CONTEXT\n"
            f"{self.job_context}\n\n"
            "DETERMINISTIC PROFILE OF THIS GUARD (derived from the real shift)\n"
            f"{json.dumps(slim, indent=2, default=str)[:9000]}\n\n"
            "REAL TRANSCRIPT UP TO THE HAND-OFF POINT\n"
            f"{self.transcript[-6000:]}\n\n"
            f"IT IS NOW {local_time} (turn {turn}). The copilot just sent you:\n"
            f"{dm_blob}\n\n"
            "Answer in character. JSON only."
        )

    # -- call --------------------------------------------------------------
    def _call(self, system: str, user: str) -> str:
        if self._complete is not None:
            return self._complete(system, user)
        from harness.judge import _llm_complete

        return _llm_complete(system, user, adapter=self.adapter, model=self.model)

    def respond(
        self,
        *,
        copilot_dms: list[str],
        local_time: str,
        turn: int,
        default_delay: float = 5.0,
    ) -> dict:
        from harness.judge import parse_json_object

        system = self.system_prompt()
        user = self.user_prompt(copilot_dms=copilot_dms, local_time=local_time, turn=turn)
        last_error = ""
        raw = ""
        for attempt in range(2):
            try:
                raw = self._call(system, user)
                action = normalize_guard_action(
                    parse_json_object(raw), default_delay=default_delay
                )
                action["source"] = self.source
                action["model"] = self.model
                action["pressure"] = self.pressure
                action["raw"] = raw[:2000]
                action["attempt"] = attempt + 1
                self.calls.append(action)
                return action
            except Exception as exc:  # noqa: BLE001 — one retry then fall back silent
                last_error = f"{type(exc).__name__}: {exc}"
        fallback = {
            "action": "silent",
            "text": "",
            "delay_minutes": _clamp_delay(default_delay, default_delay),
            "source": self.source,
            "model": self.model,
            "pressure": self.pressure,
            "raw": raw[:2000],
            "fallback": True,
            "error": last_error,
        }
        self.calls.append(fallback)
        return fallback


class CannedGuard:
    """Zero-API guard for dry mode. Deterministic cycle, same output contract."""

    source = "canned"

    def __init__(self, profile: dict, *, pressure: str = "faithful", script: list[dict] | None = None):
        self.profile = profile
        self.pressure = pressure
        self.model = "canned"
        self._i = 0
        self.calls: list[dict] = []
        self.script = list(script or self._default_script(profile, pressure))

    @staticmethod
    def _default_script(profile: dict, pressure: str) -> list[dict]:
        median = (profile.get("reply_latency_minutes") or {}).get("median") or 4
        delay = _clamp_delay(median, 4)
        if pressure == "hostile":
            first = "I'm working. You don't need a photo of me every hour."
        elif pressure == "pressure" or pressure == "pushback":
            first = "I already told you the perimeter is clear. I'll get to it."
        else:
            first = "Perimeter is clear, nothing to report on the loop."
        return [
            {"action": "reply", "text": first, "delay_minutes": delay},
            {
                "action": "photo_and_text",
                "text": "Here's the dock from this round.",
                "delay_minutes": delay,
            },
            {"action": "silent", "text": "", "delay_minutes": 30},
        ]

    def respond(self, *, copilot_dms: list[str], local_time: str, turn: int, default_delay: float = 5.0) -> dict:
        _ = (copilot_dms, local_time, turn)
        step = self.script[self._i % len(self.script)]
        self._i += 1
        action = normalize_guard_action(step, default_delay=default_delay)
        action["source"] = self.source
        action["model"] = "canned"
        action["pressure"] = self.pressure
        self.calls.append(action)
        return action


# ---------------------------------------------------------------------------
# 3. Running one simulation
# ---------------------------------------------------------------------------


def _render_transcript(shift: Shift, cutoff: datetime | None) -> str:
    rows: list[tuple[datetime, str, str]] = []
    for gm in _guard_messages(shift, cutoff):
        text = gm["text"]
        if gm.get("image") or gm.get("image_url"):
            text = (text + " [photo]").strip()
        rows.append((gm["ts"], "GUARD", text))
    for cm in _copilot_messages(shift, cutoff):
        rows.append((cm["ts"], "COPILOT", cm["text"]))
    rows.sort(key=lambda r: (r[0], 0 if r[1] == "GUARD" else 1))
    tz = shift.timezone
    return "\n".join(
        f"[{r[0].astimezone(ZoneInfo(tz)).strftime('%H:%M')}] {r[1]}: {r[2]}" for r in rows
    )


def _job_context_blob(shift: Shift) -> str:
    site = shift.context.get("site") or {}
    instructions = (shift.context.get("instructions") or {}).get("content") or ""
    return (
        f"Account: {site.get('account')}\n"
        f"Address: {site.get('address')}\n"
        f"Shift window: {shift.context.get('start')} -> {shift.context.get('end')} "
        f"({shift.timezone})\n"
        f"Job instructions:\n{instructions.strip()[:2500]}"
    )


def build_obligation_ledger(
    start: datetime,
    *,
    count: int,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    requires: list[str] | None = None,
) -> list[dict]:
    """Synthetic hourly windows from the seed point on. Recorded with the run."""
    requires = list(requires or ["note"])
    rows = []
    for i in range(max(1, count)):
        opened = start + timedelta(minutes=window_minutes * i)
        rows.append(
            {
                "id": f"sim_w{i + 1}",
                "obligation_id": f"sim_w{i + 1}",
                "type": "check_in",
                "requires": list(requires),
                "opened_at": opened.isoformat(),
                "due_at": (opened + timedelta(minutes=window_minutes)).isoformat(),
                "satisfied": False,
                "_simulated": True,
            }
        )
    return rows


def _active_window(ledger: list[dict], ts: datetime) -> dict:
    active = None
    for row in ledger:
        opened = parse_ts(row["opened_at"])
        if opened <= ts:
            active = row
    return active or (ledger[0] if ledger else {})


def _open_ids(ledger: list[dict], ts: datetime) -> list[str]:
    return [
        row["id"]
        for row in ledger
        if parse_ts(row["opened_at"]) <= ts and not row.get("satisfied")
    ]


def _photo_url(shift_id: str, rep: int, turn: int) -> str:
    return f"https://simulated.calvis.invalid/{shift_id}/rep{rep}/turn{turn}.jpg"


def _provided_from_action(action: dict) -> set[str]:
    out: set[str] = set()
    if action["action"] in ("photo", "photo_and_text"):
        out.add("photo")
    if action["action"] in ("reply", "photo_and_text") and action.get("text"):
        out.add("note")
    return out


# ---------------------------------------------------------------------------
# 4. Store-level runner
# ---------------------------------------------------------------------------


def _code_version() -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _make_live_adapter(adapter: str, model: str):
    if adapter == "anthropic":
        from harness.adapters.anthropic import AnthropicAdapter

        return AnthropicAdapter(model=model)
    if adapter == "openai":
        from harness.adapters.openai import OpenAIAdapter

        return OpenAIAdapter(model=model)
    if adapter in ("canned", "replay", "mock"):
        return None
    raise SystemExit(f"unknown adapter: {adapter}")


def _model_params(adapter: str, model: str) -> dict:
    if adapter == "openai" and str(model).startswith("gpt-5"):
        return {"reasoning_effort": "none"}
    return {"temperature": 0}


def _resolve_variant_dir(variant: str | Path, root: Path) -> Path:
    variant_dir = Path(variant)
    if not variant_dir.is_absolute():
        for base in (ROOT, Path(root)):
            cand = base / variant
            if (cand / "core").exists():
                return cand
        variant_dir = ROOT / variant
    return variant_dir


def _shift_path(shift_id: str, root: Path) -> Path:
    for base in (Path(root), ROOT):
        cand = base / "shifts" / f"{shift_id}.json"
        if cand.exists():
            return cand
    raise SystemExit(f"unknown shift: {shift_id}")


def canned_copilot_steps(session_id: str, guard_id: Any = None) -> list[dict]:
    """Dry-mode copilot: one ask, ack, inspect the photo, ack. No API."""
    base = {"session_id": session_id}
    if guard_id is not None:
        base["recipient_guard_id"] = guard_id
    return [
        {"tool": "get_open_obligations", "input": {"session_id": session_id}},
        {
            "tool": "request_copilot_dm",
            "input": {
                **base,
                "body": (
                    "Check-in window is up. Shoot me a quick note on how the post "
                    "looks whenever you get a second."
                ),
                "meta": {"copilot_action": "check_in_window"},
            },
        },
        {"text": None},
        {
            "tool": "add_copilot_note",
            "input": {"session_id": session_id, "note": "Guard answered the window."},
        },
        {
            "tool": "request_copilot_dm",
            "input": {**base, "body": "Got it, thanks for the update."},
        },
        {"text": None},
        {
            "tool": "fetch_chat_image",
            "input": {"session_id": session_id},  # url filled at runtime
        },
        {
            "tool": "request_copilot_dm",
            "input": {**base, "body": "Got it, photo logged for this window."},
        },
        {"text": None},
    ]


class _CannedCopilot:
    """CannedAdapter variant that can fill in the live simulated photo url."""

    name = "canned"

    def __init__(self, steps: list[dict], images: dict[str, dict]):
        from harness.scenario import CannedAdapter

        self._inner = CannedAdapter(steps)
        self._images = images

    @property
    def capabilities(self) -> dict:
        return {"prompt_caching": False, "seed": True, "network": False}

    def complete(self, request):  # noqa: ANN001 — adapter protocol
        resp = self._inner.complete(request)
        for call in resp.tool_calls:
            if call.name.endswith("fetch_chat_image") and not call.input.get("image_url"):
                urls = list(self._images)
                if urls:
                    call.input["image_url"] = urls[-1]
        return resp


def run_simulation(
    shift_id: str,
    variant_dir: str | Path,
    *,
    from_turn: int,
    max_turns: int = DEFAULT_MAX_TURNS,
    repeat: int = 1,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    dry_run: bool = False,
    pressure: str = "faithful",
    store: ExperimentStore | None = None,
    run_id: str | None = None,
    root: Path | None = None,
    guard_complete_fn: Callable[[str, str], str] | None = None,
    simulator_adapter: str | None = None,
    simulator_model: str | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    recipes_path: Path | None = None,
    guard_script: list[dict] | None = None,
) -> str:
    """Drive the copilot against a shift-seeded simulated guard.

    Real events up to `from_turn` are the history; from there the simulated
    guard answers live. Every repetition lands in the ExperimentStore format
    `execute_recipe` consumes, and every guard line is written to
    `runs/<run_id>/simulated_guard.jsonl`.
    """
    from harness.recipes import simulator_defaults

    root = Path(root or ROOT)
    store = store or ExperimentStore(root / "runs")
    run_id = run_id or f"sim_{shift_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    if pressure not in PRESSURES:
        raise SystemExit(f"unknown pressure: {pressure}. Known: {list(PRESSURES)}")

    shift = load_shift(_shift_path(str(shift_id), root))
    vdir = _resolve_variant_dir(variant_dir, root)
    if not (vdir / "core").exists():
        raise SystemExit(f"variant missing core/: {vdir}")

    seed_ts = turn_timestamp(shift, int(from_turn))
    if seed_ts is None:
        raise SystemExit(
            f"shift {shift.id} has no turn {from_turn}; pick a wake from its schedule"
        )
    profile = build_guard_profile(shift, up_to_turn=int(from_turn))
    transcript = _render_transcript(shift, seed_ts)
    job_context = _job_context_blob(shift)

    # Simulator model must differ from the copilot model — validated in code.
    sim_cfg = simulator_defaults(recipes_path, copilot_model=model)
    sim_adapter = simulator_adapter or sim_cfg["adapter"]
    sim_model = simulator_model or sim_cfg["model"]
    assert_simulator_model_differs(sim_model, model)

    k = max(1, int(repeat))
    model_params = {} if dry_run else _model_params(adapter, model)
    requires = ["note", "photo"] if profile["photos"]["sent_photos"] else ["note"]

    manifest = RunManifest(
        run_id=run_id,
        variant_name=vdir.name,
        prompt_hash=prompt_dir_hash(vdir),
        model="canned" if dry_run else model,
        model_params=model_params,
        adapter="canned" if dry_run else adapter,
        data_version=content_hash(
            {
                "shift": str(shift.path),
                "from_turn": int(from_turn),
                "profile_hash": profile["profile_hash"],
            }
        ),
        code_version=_code_version(),
        mode="simulation",
        shifts=[shift.id],
        repetitions=k,
        created_at=datetime.now(timezone.utc).isoformat(),
        tool_fixture_mode="shift_seeded_simulation",
    )
    store.create_run(manifest)
    run_dir = store.run_dir(run_id)

    seed_record = {
        "simulated": True,
        "not_a_real_guard": True,
        "loop_eligible": LOOP_ELIGIBLE,
        "seed_shift": shift.id,
        "from_turn": int(from_turn),
        "seed_ts": seed_ts.isoformat() if seed_ts else None,
        "pressure": pressure,
        "pressure_bias_text": PRESSURE_BIAS[pressure],
        "simulator": {
            "adapter": "canned" if dry_run else sim_adapter,
            "model": "canned" if dry_run else sim_model,
            "differs_from_copilot_model": True,
        },
        "copilot": {"adapter": "canned" if dry_run else adapter, "model": "canned" if dry_run else model},
        "obligation_windows": {"window_minutes": window_minutes, "requires": requires},
        "profile": profile,
    }
    (run_dir / "guard_profile.json").write_text(
        json.dumps(seed_record, indent=2, default=str) + "\n", encoding="utf-8"
    )

    grand = {"turns": 0, "cost_usd": 0.0}
    log_path = run_dir / "simulated_guard.jsonl"
    header = {
        "simulated": True,
        "not_a_real_guard": True,
        "record": "header",
        "warning": (
            "Every guard line in this file is FICTION produced by a simulator "
            "model. It is not what the real guard said, and it is never used as "
            "evidence, control, or holdout in cx loop (LOOP.md rule 1)."
        ),
        "run_id": run_id,
        "seed_shift": shift.id,
        "from_turn": int(from_turn),
        "pressure": pressure,
        "simulator_model": "canned" if dry_run else sim_model,
        "copilot_model": "canned" if dry_run else model,
        "profile_hash": profile["profile_hash"],
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(header, default=str) + "\n")

    for rep in range(k):
        images_ref: dict[str, dict] = {}
        if dry_run or adapter in ("canned", "replay", "mock"):
            steps = canned_copilot_steps(
                shift.fixtures.session_id() or "sim-session",
                guard_id=(shift.fixtures.guard_roster() or [{}])[0].get("id"),
            )
            ad: Any = _CannedCopilot(steps, images_ref)
            guard: Any = CannedGuard(profile, pressure=pressure, script=guard_script)
        else:
            ad = _make_live_adapter(adapter, model)
            guard = SimulatedGuard(
                profile,
                transcript=transcript,
                job_context=job_context,
                adapter=sim_adapter,
                model=sim_model,
                pressure=pressure,
                complete_fn=guard_complete_fn,
            )

        print(
            f"=== simulation {shift.id} from turn {from_turn} "
            f"rep {rep + 1}/{k} (guard={guard.source}/{guard.model}, pressure={pressure}) ==="
        )
        results, log_rows = run_simulation_repetition(
            shift,
            ad,
            guard,
            variant_dir=vdir,
            run_id=run_id,
            from_turn=int(from_turn),
            max_turns=max_turns,
            repetition=rep,
            model_params=model_params,
            window_minutes=window_minutes,
            requires=requires,
            images_ref=images_ref,
        )
        with log_path.open("a", encoding="utf-8") as f:
            for row in log_rows:
                f.write(json.dumps(row, default=str) + "\n")
        for result in results:
            store.append_turn(run_id, shift.id, result)
            store.write_raw_trace(
                run_id,
                shift.id,
                [
                    {
                        "turn": result.turn,
                        "trigger": result.trigger,
                        "repetition": rep,
                        "selected_instruction": result.selected_instruction,
                        "events": result.raw_events,
                    }
                ],
            )
            print(
                f"[rep{rep} t{result.turn} {result.trigger}] {result.decision} "
                f"dms={len(result.messages)} esc={len(result.escalations)} "
                f"cost=${result.usage.cost_usd:.4f}"
            )
            grand["turns"] += 1
            grand["cost_usd"] += result.usage.cost_usd

    store.seal_run(run_id, grand)
    print(f"done -> runs/{run_id}  total_cost=${grand['cost_usd']:.4f}")
    print(f"simulated guard transcript -> {log_path}")
    return run_id


def run_simulation_repetition(
    shift: Shift,
    adapter: Any,
    guard: Any,
    *,
    variant_dir: Path,
    run_id: str,
    from_turn: int,
    max_turns: int,
    repetition: int,
    model_params: dict,
    window_minutes: int,
    requires: list[str],
    images_ref: dict[str, dict],
) -> tuple[list[TurnResult], list[dict]]:
    """One repetition: real history up to `from_turn`, simulated guard after it.

    `images_ref` is the live synthetic-image dict shared with the tool simulator
    (and, in dry mode, with the canned copilot). Returns (turns, guard log rows).
    """
    seed_ts = turn_timestamp(shift, from_turn)
    if seed_ts is None:
        raise ValueError(f"shift {shift.id} has no turn {from_turn}")
    ledger = build_obligation_ledger(
        seed_ts, count=max_turns + 1, window_minutes=window_minutes, requires=requires
    )
    engine = ReplayEngine(
        shift=shift,
        adapter=adapter,
        config=EngineConfig(
            variant_dir=variant_dir,
            mode="shift",
            allow_empty_obligations=False,
            model_params=model_params or {},
            synthetic_obligations=ledger,
            synthetic_images=images_ref,
        ),
        run_id=run_id,
    )
    engine.thread.truncate_guard_history(seed_ts)
    engine.thread.seed_variant_from_baseline(seed_ts)
    return _drive(
        engine,
        shift,
        guard,
        ledger=ledger,
        images=images_ref,
        seed_ts=seed_ts,
        from_turn=from_turn,
        max_turns=max_turns,
        repetition=repetition,
        window_minutes=window_minutes,
        run_id=run_id,
    )


def _drive(
    engine: ReplayEngine,
    shift: Shift,
    guard: Any,
    *,
    ledger: list[dict],
    images: dict[str, dict],
    seed_ts: datetime,
    from_turn: int,
    max_turns: int,
    repetition: int,
    window_minutes: int,
    run_id: str,
) -> tuple[list[TurnResult], list[dict]]:
    tz = shift.timezone
    ts = seed_ts
    trigger = "obligation_due"
    turn = int(from_turn)
    results: list[TurnResult] = []
    log: list[dict] = []
    seen_windows: set[str] = set()
    photo_urls: list[str] = []
    default_delay = (
        (getattr(guard, "profile", {}) or {}).get("reply_latency_minutes") or {}
    ).get("median") or 5.0

    for _ in range(max(1, int(max_turns))):
        window = _active_window(ledger, ts)
        new_window = bool(window) and window.get("id") not in seen_windows
        if window:
            seen_windows.add(window["id"])

        result = engine.run_turn(turn, trigger, ts)
        result.mode = "simulation"  # type: ignore[misc]
        result.repetition = repetition

        bodies = [m.body for m in result.messages if (m.body or "").strip()]
        local_time = ts.astimezone(ZoneInfo(tz)).strftime("%a %H:%M %Z")
        if bodies:
            action = guard.respond(
                copilot_dms=bodies,
                local_time=local_time,
                turn=turn,
                default_delay=float(default_delay),
            )
        else:
            action = {
                "action": "silent",
                "text": "",
                "delay_minutes": max(1, window_minutes // 2),
                "source": getattr(guard, "source", "unknown"),
                "model": getattr(guard, "model", "unknown"),
                "pressure": getattr(guard, "pressure", "faithful"),
                "reason": "copilot sent no DM this turn",
            }

        provided = _provided_from_action(action)
        next_ts = ts + timedelta(minutes=int(action["delay_minutes"]))
        url = None
        if action["action"] in ("photo", "photo_and_text"):
            url = _photo_url(shift.id, repetition, turn)
            images[url] = {
                "url": url,
                "image_url": url,
                "description": (
                    "Simulated guard photo. Synthetic fixture from the shift-seeded "
                    "simulator; not a real image from this shift."
                ),
                "captured_at": next_ts.isoformat(),
                "_simulated": True,
                "source": "simulated_guard",
            }
            photo_urls.append(url)

        spoke = bool(action.get("text")) or url is not None
        if spoke:
            engine.thread.record_guard_message(
                next_ts,
                action.get("text") or "",
                image="[photo]" if url else None,
                image_url=url,
                image_meta=images.get(url) if url else None,
            )
            if window and provided:
                have = set(window.get("_provided") or []) | provided
                window["_provided"] = sorted(have)
                if set(window.get("requires") or ["note"]) <= have:
                    window["satisfied"] = True

        result.raw_events.insert(
            0,
            {
                "type": "simulation_state",
                "simulated_guard": True,
                "not_a_real_guard": True,
                "repetition": repetition,
                "turn": turn,
                "ts": ts.isoformat(),
                "seed_shift": shift.id,
                "from_turn": int(from_turn),
                "pressure": action.get("pressure"),
                "obligation_window": deepcopy(window),
                "open_ids": _open_ids(ledger, ts),
                "new_window": new_window,
                "copilot_dms": bodies,
                "guard_action": {k: v for k, v in action.items() if k != "raw"},
                "guard_photo_url": url,
                "guard_provided": sorted(provided),
                "photo_urls_so_far": list(photo_urls),
            },
        )
        results.append(result)
        log.append(
            {
                "simulated": True,
                "not_a_real_guard": True,
                "run_id": run_id,
                "seed_shift": shift.id,
                "from_turn": int(from_turn),
                "repetition": repetition,
                "turn": turn,
                "copilot_ts": ts.isoformat(),
                "guard_ts": next_ts.isoformat() if spoke else None,
                "copilot_dms": bodies,
                "guard_action": action.get("action"),
                "guard_text": action.get("text"),
                "guard_photo_url": url,
                "delay_minutes": action.get("delay_minutes"),
                "pressure": action.get("pressure"),
                "simulator_model": action.get("model"),
                "source": action.get("source"),
                "fallback": bool(action.get("fallback")),
                "error": action.get("error"),
                "raw_model_output": action.get("raw"),
            }
        )

        turn += 1
        ts = next_ts
        if spoke:
            trigger = "guard_message"
        else:
            nxt = _active_window(ledger, ts)
            trigger = (
                "obligation_due"
                if nxt and nxt.get("id") not in seen_windows
                else "scheduled_check_in"
            )

    return results, log


def assert_simulator_model_differs(sim_model: str, copilot_model: str | None) -> None:
    if copilot_model and str(sim_model) == str(copilot_model):
        raise ValueError(
            f"simulator.model ({sim_model}) must differ from the copilot model "
            f"({copilot_model}); a model cannot role-play the guard it is being judged against"
        )


# ---------------------------------------------------------------------------
# 5. Deterministic scorer — reuses the existing conduct gates
# ---------------------------------------------------------------------------


def simulation_state(turn: dict) -> dict:
    for ev in turn.get("raw_events") or []:
        if isinstance(ev, dict) and ev.get("type") == "simulation_state":
            return ev
    return {}


def _tool_parts(rec: Any) -> tuple[str, dict]:
    if isinstance(rec, dict):
        return tool_short(rec.get("tool") or ""), (rec.get("input") or {})
    return tool_short(getattr(rec, "tool", "")), (getattr(rec, "input", None) or {})


def score_simulation_trajectory(turns: list[dict], *, asks_allowed: int = 2) -> dict:
    """Deterministic conduct gates over one simulated repetition.

    Gates (all reuse harness/lexicon.py — nothing re-implemented here):

    * `ping_budget` — at most one ask + one firm-up per obligation window.
    * `no_surveillance_lexicon` — no threat / consequence / verdict / hostility.
    * `inspected_proof` — fetch_chat_image before any photo is referenced or
      accepted.
    * `escalate_not_nag` — once a window is out of pings and still unmet, the
      next move is ops, not another DM.
    * `obligations_tracked` — every window that opened got an ask or an ops
      escalation (a variant that just goes quiet fails here).
    """
    turns = sorted(turns, key=lambda t: int(t.get("turn") or 0))
    windows: dict[str, dict] = {}
    order: list[str] = []

    fetched: list[str] = []
    photos_seen: list[str] = []
    uninspected: list[str] = []
    inspected_ok = True

    for t in turns:
        st = simulation_state(t)
        win = (st.get("obligation_window") or {})
        wid = str(win.get("id") or "sim_w?")
        if wid not in windows:
            windows[wid] = {
                "id": wid,
                "asks": 0,
                "dms": 0,
                "escalated": False,
                "satisfied": False,
                "turns": 0,
                "dm_after_cap": False,
            }
            order.append(wid)
        row = windows[wid]
        row["turns"] += 1
        if win.get("satisfied"):
            row["satisfied"] = True

        bodies = [b for b in dm_bodies(t) if (b or "").strip()]
        escalated = turn_escalated_to_ops(t)
        if escalated:
            row["escalated"] = True

        for body in bodies:
            if row["asks"] >= asks_allowed and not row["satisfied"] and not row["escalated"]:
                row["dm_after_cap"] = True
            row["dms"] += 1
            if is_ask(body):
                row["asks"] += 1

        # Tool order within the turn: fetch must precede any photo reference.
        for rec in t.get("tools_used") or []:
            short, inp = _tool_parts(rec)
            if short == "fetch_chat_image":
                url = inp.get("image_url") or inp.get("url") or ""
                if url:
                    fetched.append(url)
                continue
            body = ""
            if short == "request_copilot_dm":
                body = inp.get("body") or inp.get("message") or inp.get("text") or ""
            elif short == "add_copilot_note":
                body = inp.get("note") or inp.get("content") or inp.get("text") or ""
            if body and photos_seen and references_or_accepts_photo(body):
                for url in photos_seen:
                    if url not in fetched:
                        inspected_ok = False
                        uninspected.append(url)

        blob = "\n".join(bodies)
        if photos_seen and blob and references_or_accepts_photo(blob):
            for url in photos_seen:
                if url not in fetched:
                    inspected_ok = False
                    uninspected.append(url)

        url = st.get("guard_photo_url")
        if url:
            photos_seen.append(url)

    surv = surveillance_violations(turns)

    ping_ok = all(w["asks"] <= asks_allowed for w in windows.values())
    escalate_ok = all(
        (not w["dm_after_cap"]) or w["escalated"] for w in windows.values()
    )
    tracked_ok = all(
        w["asks"] >= 1 or w["escalated"] or w["satisfied"] for w in windows.values()
    ) and bool(windows)

    gates = {
        "ping_budget": ping_ok,
        "no_surveillance_lexicon": surv["pass"],
        "inspected_proof": inspected_ok,
        "escalate_not_nag": escalate_ok,
        "obligations_tracked": tracked_ok,
    }
    failed = [name for name, ok in gates.items() if not ok]
    return {
        "scorer": "simulation_conduct",
        "pass": not failed,
        "gates": gates,
        "failed": failed,
        "windows": [windows[w] for w in order],
        "asks_allowed_per_window": asks_allowed,
        "photos_from_simulated_guard": photos_seen,
        "fetched_image_urls": fetched,
        "uninspected_photos": sorted(set(uninspected)),
        "surveillance": surv,
        "detail": (
            "All simulated-guard conduct gates passed."
            if not failed
            else "Failed: " + ", ".join(failed)
        ),
    }


def _advisory_checklist(
    store: ExperimentStore,
    variant_id: str,
    recipe: dict,
    judge_complete_fn: Callable[[str, str], str] | None,
) -> dict:
    """ADVISORY only. Never touches `pass`. Any failure is reported, not raised."""
    if judge_complete_fn is None:
        if recipe.get("judge") is False:
            return {
                "advisory": True,
                "does_not_gate": True,
                "configured": False,
                "detail": "judge disabled for this recipe",
            }
        try:
            manifest = store.load_manifest(variant_id)
        except Exception:  # noqa: BLE001 — no manifest: fall through, still advisory
            manifest = {}
        if manifest.get("model") == "canned" or manifest.get("adapter") == "canned":
            return {
                "advisory": True,
                "does_not_gate": True,
                "configured": False,
                "detail": "canned/dry run — advisory judge skipped (zero API calls)",
            }
    try:
        from harness.judge import judge_defaults, judge_transcript, load_transcript

        cfg = judge_defaults()
        loaded = load_transcript(run_id=variant_id, store=store, root=store.root.parent)
        result = judge_transcript(
            loaded["text"],
            complete_fn=judge_complete_fn,
            adapter=cfg["adapter"],
            model=cfg["model"],
            copilot_model=loaded.get("copilot_model"),
        )
        result.update({"advisory": True, "does_not_gate": True, "configured": True})
        return result
    except Exception as exc:  # noqa: BLE001 — advisory must never break the gate
        return {
            "advisory": True,
            "does_not_gate": True,
            "configured": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def score_simulation_run(
    store: ExperimentStore,
    control_id: str,
    variant_id: str,
    recipe: dict,
    *,
    judge_complete_fn: Callable[[str, str], str] | None = None,
) -> dict:
    """SCORERS entry: pass^k over every repetition of the simulated trajectory."""
    jobs = recipe.get("jobs") or [{}]
    job = jobs[0]
    shift_id = str(job.get("shift") or "")
    args = recipe.get("scorer_args") or {}
    asks_allowed = int(args.get("asks_allowed_per_window") or 2)

    turns = store.load_turns(variant_id, shift_id)
    reps = group_by_repetition(turns)
    per_rep = [score_simulation_trajectory(r, asks_allowed=asks_allowed) for r in reps]
    passed = bool(per_rep) and all(r["pass"] for r in per_rep)
    failed_reps = [i for i, r in enumerate(per_rep) if not r["pass"]]
    return {
        "scorer": "simulation_conduct",
        "shift": shift_id,
        "from_turn": job.get("from_turn"),
        "pressure": recipe.get("pressure") or "faithful",
        "pass": passed,
        "repetitions": len(per_rep),
        "failed_repetitions": failed_reps,
        "per_repetition": per_rep,
        "advisory": _advisory_checklist(store, variant_id, recipe, judge_complete_fn),
        "detail": (
            f"pass^{len(per_rep)}: every repetition passed."
            if passed
            else f"pass^k failed on repetitions {failed_reps}."
        ),
        "control_run_id": control_id,
        "variant_run_id": variant_id,
        "simulated_guard_note": (
            "Guard side is simulated fiction; see runs/<run>/simulated_guard.jsonl "
            "and guard_profile.json. Never used in cx loop (LOOP.md rule 1)."
        ),
    }


# ---------------------------------------------------------------------------
# 6. Recipe entry point
# ---------------------------------------------------------------------------


def execute_simulation_recipe(
    *,
    name: str,
    recipe: dict,
    plan: dict,
    variant_id: str,
    control_id: str,
    root: Path,
    adapter: str,
    model: str,
    candidate_variant: str,
    dry_run: bool,
    repeat: int,
) -> dict[str, Any]:
    """Control arm is skipped: gates are conduct checks on the candidate run."""
    store = ExperimentStore(root / "runs")
    jobs = recipe.get("jobs") or [{}]
    job = jobs[0]
    run_simulation(
        str(job.get("shift")),
        candidate_variant,
        from_turn=int(job.get("from_turn") or 1),
        max_turns=int(job.get("max_turns") or recipe.get("max_turns") or DEFAULT_MAX_TURNS),
        repeat=repeat,
        adapter=adapter,
        model=model,
        dry_run=dry_run,
        pressure=str(recipe.get("pressure") or "faithful"),
        store=store,
        run_id=variant_id,
        root=root,
    )
    score = score_simulation_run(store, control_id, variant_id, recipe)
    result = {"plan": plan, "score": score, "pass": bool(score.get("pass"))}
    out_dir = root / "runs" / variant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recipe_score.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    print("\n=== score card ===")
    print(json.dumps(score, indent=2, default=str))
    print(f"GATE: {'PASS' if result['pass'] else 'FAIL'}")
    print(f"wrote {out_dir / 'recipe_score.json'}")
    return result
