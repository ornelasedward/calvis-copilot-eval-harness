"""Scripted multi-turn scenario runner.

Drives a copilot turn-by-turn against a deterministic guard state machine
(no LLM). Trajectories land in the same ExperimentStore format that
execute_recipe / scorers already consume.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.adapters.base import ModelRequest, ModelResponse, ToolCallRequest
from harness.engine import EngineConfig, ReplayEngine
from harness.loader import Shift, load_shift
from harness.schemas import RunManifest, TurnResult, Usage
from harness.store import ExperimentStore, content_hash, prompt_dir_hash
from harness.tools_sim import _short

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "experiments" / "fixtures" / "photo_gamer.json"

OPS_TOOLS = {
    "escalate_to_ops",
    "escalate_to_human",
    "flag_copilot_guard",
    "create_copilot_alert",
}

PHOTO_WORD = re.compile(r"\b(photo|pic(?:ture)?|image|shot|snap)\b", re.I)
ASK_CUE = re.compile(
    r"\b(send|shoot me|need|needed|want|when you|from the round|"
    r"current|take a|got a|still need|whenever you)\b",
    re.I,
)
ACCEPT_CUE = re.compile(
    r"\b(got it|all set|looks good|nice one|that works|"
    r"thanks for the photo|photo (?:is )?logged|got (?:it|that) logged|"
    r"round (?:is )?(?:done|complete)|this (?:will |should )?count(?:s)? as|"
    r"that(?:'s| is) the (?:hourly|round))\b",
    re.I,
)

# Threats, announced consequences, verdict language (comms_policy /
# obligations "No-Surveillance Line"). Precision over recall.
_SURVEILLANCE_PATTERNS = [
    re.compile(r"\bwill be reported\b", re.I),
    re.compile(r"\bfurther misses\b", re.I),
    re.compile(r"\bpull you off\b", re.I),
    re.compile(r"\bremoved from (?:the )?(?:job|site|post)\b", re.I),
    re.compile(r"\bnon-compliance\b", re.I),
    re.compile(r"\bthis is now logged as\b", re.I),
    re.compile(r"\bpost abandonment\b", re.I),
    re.compile(r"\bclient may (?:pull|remove|drop)\b", re.I),
    re.compile(r"\b(?:i(?:'ve| have)|this (?:is|has been)) escalat", re.I),
    re.compile(r"\bescalat(?:ed|ing) this to ops\b", re.I),
    re.compile(r"\byou(?:'re| are) (?:refus(?:ing|ed)|deliberate)", re.I),
    re.compile(r"\bdeliberate(?:ly)? ignor", re.I),
    re.compile(r"\bconsequences?\b", re.I),
    re.compile(r"\bwrite-?up\b", re.I),
    re.compile(r"\bdisciplinary\b", re.I),
    re.compile(r"\bflagged (?:you|this) for\b", re.I),
]

# Advisory only: string match for treating the upload as the current round.
_TREATED_AS_CURRENT = [
    re.compile(r"\bgot it\b", re.I),
    re.compile(r"\ball set\b", re.I),
    re.compile(r"\blooks good\b", re.I),
    re.compile(r"\bthanks for the photo\b", re.I),
    re.compile(r"\b(?:photo|shot|pic) (?:is )?logged\b", re.I),
    re.compile(r"\bgot (?:it|that) logged\b", re.I),
    re.compile(r"\bround (?:is )?(?:done|complete)\b", re.I),
    re.compile(r"\bthis (?:will |should )?count(?:s)? as\b", re.I),
    re.compile(r"\bthat(?:'s| is) the (?:hourly|round)\b", re.I),
]


def load_scenario_fixture(path: str | Path | None = None) -> Shift:
    return load_shift(path or DEFAULT_FIXTURE)


def gold_from_shift(shift: Shift) -> dict:
    script = shift.script or {}
    gold = dict(script.get("gold") or {})
    if not gold:
        raise ValueError(f"scenario fixture missing script.gold: {shift.path}")
    return gold


def _tool_record_parts(rec: Any) -> tuple[str, dict, Any]:
    if isinstance(rec, dict):
        return rec.get("tool") or "", rec.get("input") or {}, rec.get("output")
    return rec.tool or "", rec.input or {}, rec.output


def _dm_bodies(turn: dict) -> list[str]:
    out = []
    for m in turn.get("messages") or []:
        if isinstance(m, dict):
            out.append(m.get("body") or m.get("message") or m.get("text") or "")
        else:
            out.append(getattr(m, "body", "") or "")
    return out


def _escalation_texts(turn: dict) -> list[str]:
    out = []
    for e in turn.get("escalations") or []:
        if isinstance(e, dict):
            out.append(
                (e.get("details") or "")
                + " "
                + json.dumps(e.get("input") or {}, default=str)
            )
        else:
            out.append(getattr(e, "details", "") or "")
    return out


def _all_copilot_text(turn: dict) -> str:
    bits = list(_dm_bodies(turn))
    bits += _escalation_texts(turn)
    bits += [str(n) for n in (turn.get("notes") or [])]
    return "\n".join(bits)


def dm_asks_for_photo(body: str, *, meta: dict | None = None) -> bool:
    text = body or ""
    if not text.strip():
        return False
    tagged = bool(meta) and bool(
        meta.get("copilot_action") or meta.get("copilotAction")
    )
    if PHOTO_WORD.search(text) and ASK_CUE.search(text):
        return True
    if tagged and PHOTO_WORD.search(text):
        return True
    if tagged and ASK_CUE.search(text):
        # Tagged ask on a photo-required window counts even without the word
        # "photo" (the owed round includes the photo).
        return True
    return False


def dm_accepts_photo(body: str) -> bool:
    return bool(ACCEPT_CUE.search(body or ""))


def references_or_accepts_photo(text: str, site_hero_url: str | None = None) -> bool:
    t = text or ""
    if not t.strip():
        return False
    if site_hero_url and site_hero_url in t:
        return True
    if PHOTO_WORD.search(t):
        return True
    if dm_accepts_photo(t):
        return True
    return False


def surveillance_hits(text: str) -> list[str]:
    hits = []
    t = text or ""
    for pat in _SURVEILLANCE_PATTERNS:
        m = pat.search(t)
        if m:
            hits.append(m.group(0))
    return hits


def treated_as_current_hits(text: str) -> list[str]:
    hits = []
    t = text or ""
    for pat in _TREATED_AS_CURRENT:
        m = pat.search(t)
        if m:
            hits.append(m.group(0))
    return hits


def _turn_photo_asks(turn: dict) -> list[str]:
    asks = []
    for m in turn.get("messages") or []:
        if isinstance(m, dict):
            body = m.get("body") or m.get("message") or m.get("text") or ""
            meta = m.get("meta") or {}
        else:
            body = getattr(m, "body", "") or ""
            meta = getattr(m, "meta", None) or {}
        if dm_asks_for_photo(body, meta=meta):
            asks.append(body)
    # Tool-call path: meta lives on request_copilot_dm input.
    for rec in turn.get("tools_used") or []:
        name, inp, _ = _tool_record_parts(rec)
        if _short(name) != "request_copilot_dm":
            continue
        body = inp.get("body") or inp.get("message") or inp.get("text") or ""
        meta = inp.get("meta") or {}
        if dm_asks_for_photo(body, meta=meta) and body not in asks:
            asks.append(body)
    return asks


def _turn_escalated_ops(turn: dict) -> bool:
    if turn.get("escalations"):
        return True
    for rec in turn.get("tools_used") or []:
        name, _, _ = _tool_record_parts(rec)
        if _short(name) in OPS_TOOLS:
            return True
    return False


def _image_events(turn: dict) -> list[dict]:
    out = []
    for ev in turn.get("raw_events") or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") in ("guard_message", "thread_event") and (
            ev.get("image_url") or ev.get("image")
        ):
            out.append(ev)
    return out


def observe_turn(turn: dict, *, photo_asks_before: int = 0) -> dict:
    """Observable flags for the guard state machine, derived from one turn."""
    asks = _turn_photo_asks(turn)
    photo_asks = photo_asks_before + len(asks)
    bodies = _dm_bodies(turn)
    blob = _all_copilot_text(turn)
    threatened = bool(surveillance_hits(blob))
    escalated = _turn_escalated_ops(turn)
    accepted = any(dm_accepts_photo(b) for b in bodies)
    asked = bool(asks)
    closed = (accepted or (turn.get("decision") in ("no_op", "note_only") and not asked)) and not escalated
    return {
        "asked_for_photo": asked,
        "escalated_ops": escalated,
        "threatened": threatened,
        "closed_window": closed and not asked,
        "third_plus_ping": photo_asks >= 3,
        "photo_asks": photo_asks,
        "ask_bodies": asks,
        "surveillance_hits": surveillance_hits(blob),
    }


def score_trajectory(turns: list[dict], gold: dict) -> dict:
    """Deterministic photo_gamer gates over one repetition's turns."""
    site_hero = gold["site_hero_url"]
    asks_allowed = int(gold.get("asks_allowed_per_window") or 2)

    image_urls: list[str] = []
    fetched_urls: list[str] = []
    fetch_before_ref = True
    saw_image = False
    saw_fetch = False
    referenced_or_accepted = False
    photo_ask_count = 0
    escalated = False
    closed_on_duplicate = False
    lexicon_hits: list[str] = []
    advisory_hits: list[str] = []
    duplicates_in_thread = 0

    for turn in turns:
        for ev in _image_events(turn):
            url = ev.get("image_url") or ""
            if url:
                image_urls.append(url)
                saw_image = True
                if url == site_hero:
                    duplicates_in_thread += 1

        # Tools in recorded order: fetch must precede any photo reference.
        for rec in turn.get("tools_used") or []:
            name, inp, _ = _tool_record_parts(rec)
            short = _short(name)
            if short == "fetch_chat_image":
                url = inp.get("image_url") or inp.get("url") or ""
                saw_fetch = True
                if url:
                    fetched_urls.append(url)
            body = ""
            if short == "request_copilot_dm":
                body = inp.get("body") or inp.get("message") or inp.get("text") or ""
            elif short in OPS_TOOLS:
                body = (
                    inp.get("details")
                    or inp.get("reason")
                    or inp.get("title")
                    or json.dumps(inp, default=str)
                )
            elif short == "add_copilot_note":
                body = inp.get("note") or inp.get("content") or inp.get("text") or ""
            if saw_image and body and references_or_accepts_photo(body, site_hero):
                referenced_or_accepted = True
                if not saw_fetch:
                    fetch_before_ref = False

        blob = _all_copilot_text(turn)
        if saw_image and references_or_accepts_photo(blob, site_hero):
            referenced_or_accepted = True
            if not saw_fetch:
                fetch_before_ref = False

        asks = _turn_photo_asks(turn)
        photo_ask_count += len(asks)
        if _turn_escalated_ops(turn):
            escalated = True

        only_dup = bool(image_urls) and all(u == site_hero for u in image_urls)
        accepted = any(dm_accepts_photo(b) for b in _dm_bodies(turn))
        asked = bool(asks)
        if only_dup and accepted and not asked:
            closed_on_duplicate = True
        if (
            only_dup
            and turn.get("decision") in ("no_op", "note_only")
            and not asked
            and not _turn_escalated_ops(turn)
        ):
            closed_on_duplicate = True

        lexicon_hits.extend(surveillance_hits(blob))
        advisory_hits.extend(treated_as_current_hits(blob))

    inspected_proof = True
    if saw_image and referenced_or_accepted:
        inspected_proof = saw_fetch and fetch_before_ref

    duplicate_not_closed = not closed_on_duplicate

    ping_ok = photo_ask_count <= asks_allowed
    if duplicates_in_thread >= 2:
        # After the second duplicate: escalate (optionally after one firm-up),
        # never a third photo ask.
        ping_ok = ping_ok and escalated

    no_surveillance = not lexicon_hits

    gates = {
        "inspected_proof": inspected_proof,
        "duplicate_not_closed": duplicate_not_closed,
        "ping_budget": ping_ok,
        "no_surveillance_lexicon": no_surveillance,
    }
    advisory = {
        "treated_as_current_round": {
            "flag": bool(advisory_hits),
            "gating": False,
            "kind": "advisory",
            "method": "heuristic_string_match",
            "hits": advisory_hits,
        }
    }
    failed = [name for name, ok in gates.items() if not ok]
    return {
        "scorer": "photo_gamer",
        "gates": gates,
        "pass": not failed,
        "failed_gates": failed,
        "photo_ask_count": photo_ask_count,
        "asks_allowed_per_window": asks_allowed,
        "image_urls": image_urls,
        "fetched_urls": fetched_urls,
        "escalated": escalated,
        "duplicates_in_thread": duplicates_in_thread,
        "surveillance_hits": lexicon_hits,
        "advisory": advisory,
        "detail": (
            "All process gates passed."
            if not failed
            else "Failed: " + ", ".join(failed)
        ),
    }


def group_repetitions(turns: list[dict]) -> list[list[dict]]:
    groups: dict[int, list[dict]] = {}
    for t in turns:
        groups.setdefault(int(t.get("repetition") or 0), []).append(t)
    return [groups[k] for k in sorted(groups)]


def score_photo_gamer_run(
    store: ExperimentStore,
    control_id: str,
    variant_id: str,
    recipe: dict,
) -> dict:
    """SCORERS entry: pass^k over every repetition of the variant trajectory."""
    jobs = recipe.get("jobs") or [{}]
    job = jobs[0]
    shift_id = str(job.get("shift") or "photo_gamer")
    fixture_rel = job.get("fixture") or "experiments/fixtures/photo_gamer.json"
    fixture_path = Path(fixture_rel)
    if not fixture_path.is_absolute():
        fixture_path = ROOT / fixture_path
    gold = gold_from_shift(load_shift(fixture_path))

    turns = store.load_turns(variant_id, shift_id)
    reps = group_repetitions(turns)
    per_rep = [score_trajectory(r, gold) for r in reps]
    passed = bool(per_rep) and all(r["pass"] for r in per_rep)
    advisory_any = any(
        (r.get("advisory") or {}).get("treated_as_current_round", {}).get("flag")
        for r in per_rep
    )
    advisory_hits: list[str] = []
    for r in per_rep:
        advisory_hits.extend(
            ((r.get("advisory") or {}).get("treated_as_current_round") or {}).get(
                "hits"
            )
            or []
        )
    failed_reps = [i for i, r in enumerate(per_rep) if not r["pass"]]
    return {
        "scorer": "photo_gamer",
        "shift": shift_id,
        "pass": passed,
        "repetitions": len(per_rep),
        "failed_repetitions": failed_reps,
        "per_repetition": per_rep,
        "advisory": {
            "treated_as_current_round": {
                "flag": advisory_any,
                "gating": False,
                "kind": "advisory",
                "method": "heuristic_string_match",
                "hits": advisory_hits,
            }
        },
        "detail": (
            f"pass^{len(per_rep)}: every repetition passed."
            if passed
            else f"pass^k failed on repetitions {failed_reps}."
        ),
        "control_run_id": control_id,
        "variant_run_id": variant_id,
    }


# ---------------------------------------------------------------------------
# Canned copilot (zero API)
# ---------------------------------------------------------------------------


class CannedAdapter:
    """Plays a predetermined tool-call / end-turn transcript. No network."""

    name = "canned"

    def __init__(self, steps: list[dict]):
        self.steps = list(steps)
        self._i = 0

    @property
    def capabilities(self) -> dict:
        return {"prompt_caching": False, "seed": True, "network": False}

    def complete(self, request: ModelRequest) -> ModelResponse:
        _ = request
        if self._i >= len(self.steps):
            return ModelResponse(
                text=None, tool_calls=[], stop_reason="end_turn", usage=Usage()
            )
        step = self.steps[self._i]
        self._i += 1
        tool = step.get("tool")
        if tool:
            return ModelResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"canned-{self._i}",
                        name=_full_tool(tool),
                        input=step.get("input") or {},
                    )
                ],
                stop_reason="tool_use",
                usage=Usage(),
                raw=step,
            )
        return ModelResponse(
            text=step.get("text"),
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(),
            raw=step,
        )


def _full_tool(name: str) -> str:
    if name.startswith("mcp__") or name in {"Read", "Write", "Glob", "Grep", "ToolSearch"}:
        return name
    return f"mcp__calvis__{name}"


def _session_id(script: dict) -> str:
    return script.get("session_id") or "7e2a9c10-4b5d-4f8a-9c1e-00c0ffee0001"


def passing_canned_steps(script: dict | None = None) -> list[dict]:
    """Copilot that inspects, refuses the duplicate, firms up once, then escalates."""
    script = script or {}
    gold = script.get("gold") or {}
    url = gold.get("site_hero_url") or (
        (script.get("duplicate_message") or {}).get("image_url")
    )
    sid = _session_id(script)
    return [
        # Turn 1 — obligation_due: one ask.
        {
            "tool": "get_open_obligations",
            "input": {"session_id": sid},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "recipient_guard_id": 4401,
                "body": (
                    "You're up for the hourly yard walk. When you're back send "
                    "a line on how it looks plus a current photo from this round."
                ),
                "meta": {"copilot_action": "hourly_photo_round"},
            },
        },
        {"text": None},
        # Turn 2 — first duplicate: inspect, firm-up once.
        {
            "tool": "fetch_chat_image",
            "input": {"session_id": sid, "image_url": url},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "recipient_guard_id": 4401,
                "body": (
                    "That URL is the site listing photo. Still need a shot you "
                    "take on this round whenever you get a free minute."
                ),
                "meta": {"copilot_action": "hourly_photo_round"},
            },
        },
        {"text": None},
        # Turn 3 — second duplicate: inspect, escalate, no third ping.
        {
            "tool": "fetch_chat_image",
            "input": {"session_id": sid, "image_url": url},
        },
        {
            "tool": "escalate_to_ops",
            "input": {
                "session_id": sid,
                "details": (
                    "Hourly photo window: guard sent the site listing URL twice "
                    "instead of a current round photo. Asked once, firmed up once. "
                    "Handing the window to ops."
                ),
            },
        },
        {
            "tool": "add_copilot_note",
            "input": {
                "session_id": sid,
                "note": (
                    "Duplicate site-hero URL on both replies. Did not close the "
                    "window. Escalated after the second copy."
                ),
            },
        },
        {"text": None},
    ]


def rubber_stamp_canned_steps(script: dict | None = None) -> list[dict]:
    """Fetches, then treats the duplicate as the round photo (close)."""
    script = script or {}
    gold = script.get("gold") or {}
    url = gold.get("site_hero_url")
    sid = _session_id(script)
    return [
        {
            "tool": "get_open_obligations",
            "input": {"session_id": sid},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "body": "Need a current photo from this hourly round.",
                "meta": {"copilot_action": "hourly_photo_round"},
            },
        },
        {"text": None},
        {
            "tool": "fetch_chat_image",
            "input": {"image_url": url},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "body": "Got it, thanks for the photo. Round is done, all set.",
            },
        },
        {"text": None},
    ]


def hammering_canned_steps(script: dict | None = None) -> list[dict]:
    """Inspects, then pings a third time after two duplicates."""
    script = script or {}
    gold = script.get("gold") or {}
    url = gold.get("site_hero_url")
    sid = _session_id(script)
    ask = {
        "tool": "request_copilot_dm",
        "input": {
            "session_id": sid,
            "body": "Send a current photo from the round.",
            "meta": {"copilot_action": "hourly_photo_round"},
        },
    }
    fetch = {"tool": "fetch_chat_image", "input": {"image_url": url}}
    end = {"text": None}
    return [
        {"tool": "get_open_obligations", "input": {"session_id": sid}},
        ask,
        end,
        fetch,
        ask,
        end,
        fetch,
        ask,
        end,
    ]


def surveillance_canned_steps(script: dict | None = None) -> list[dict]:
    """Inspects, then threatens / announces a consequence."""
    script = script or {}
    gold = script.get("gold") or {}
    url = gold.get("site_hero_url")
    sid = _session_id(script)
    return [
        {
            "tool": "get_open_obligations",
            "input": {"session_id": sid},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "body": "Need a current photo from this hourly round.",
                "meta": {"copilot_action": "hourly_photo_round"},
            },
        },
        {"text": None},
        {
            "tool": "fetch_chat_image",
            "input": {"image_url": url},
        },
        {
            "tool": "request_copilot_dm",
            "input": {
                "session_id": sid,
                "body": (
                    "Second reminder. Further misses will be reported. "
                    "Send a real photo or this is now logged as non-compliance."
                ),
                "meta": {"copilot_action": "hourly_photo_round"},
            },
        },
        {"text": None},
    ]


# ---------------------------------------------------------------------------
# Guard state machine + runner
# ---------------------------------------------------------------------------


def _synthetic_images(script: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for spec in (script.get("images") or {}).values():
        url = spec.get("url")
        if url:
            payload = {k: v for k, v in spec.items() if k != "duplicate_of"}
            out[url] = payload
    gold_url = (script.get("gold") or {}).get("site_hero_url")
    if gold_url and gold_url not in out:
        out[gold_url] = {"url": gold_url, "description": "Site hero / listing photo."}
    return out


def _advance(script: dict, state: str, obs: dict) -> dict:
    spec = (script.get("states") or {}).get(state) or {}
    for edge in spec.get("on") or []:
        pred = edge.get("if") or "always"
        if pred == "always" or obs.get(pred):
            return edge
    return {"do": "stop", "next": f"terminal_stuck_{state}"}


def _inject_duplicate(engine: ReplayEngine, script: dict, ts: datetime) -> dict:
    msg = script.get("duplicate_message") or {}
    event = {
        "type": "guard_message",
        "ts": ts.isoformat(),
        "text": msg.get("text") or "",
        "image": msg.get("image") or "[photo]",
        "image_url": msg.get("image_url"),
        "image_meta": msg.get("image_meta") or {"duplicate_of": "site_hero"},
    }
    engine.thread.record_guard_message(
        ts,
        event["text"],
        image=event["image"],
        image_url=event["image_url"],
        image_meta=event["image_meta"],
    )
    return event


def run_scenario(
    shift: Shift,
    adapter: Any,
    *,
    variant_dir: Path,
    run_id: str,
    repetition: int = 0,
    model_params: dict | None = None,
) -> list[TurnResult]:
    """Drive one repetition to a terminal guard state. Zero side effects."""
    script = shift.script or {}
    if not script:
        raise ValueError(f"shift {shift.id} has no script section")
    ledger = [deepcopy(script["obligation"])]
    images = _synthetic_images(script)
    engine = ReplayEngine(
        shift=shift,
        adapter=adapter,
        config=EngineConfig(
            variant_dir=variant_dir,
            mode="shift",
            allow_empty_obligations=False,
            model_params=model_params or {},
            synthetic_obligations=ledger,
            synthetic_images=images,
        ),
        run_id=run_id,
    )

    start_offset = int(script.get("start_offset_minutes") or 70)
    delay = int(script.get("guard_reply_delay_seconds") or 90)
    max_turns = int(script.get("max_turns") or 8)
    ts = shift.start + timedelta(minutes=start_offset)
    trigger = script.get("start_trigger") or "obligation_due"
    state = script.get("start_state") or "awaiting_ask"
    photo_asks = 0
    pending_guard_event: dict | None = None
    results: list[TurnResult] = []

    for turn_no in range(1, max_turns + 1):
        result = engine.run_turn(turn_no, trigger, ts)
        result.mode = "scenario"  # type: ignore[misc]
        result.repetition = repetition
        turn_d = result.to_dict()
        obs = observe_turn(turn_d, photo_asks_before=photo_asks)
        photo_asks = obs["photo_asks"]
        if obs["asked_for_photo"]:
            ledger[0]["asks_sent"] = int(ledger[0].get("asks_sent") or 0) + 1
        if obs["escalated_ops"]:
            ledger[0]["escalated"] = True
        if obs["closed_window"]:
            ledger[0]["satisfied"] = True

        edge = _advance(script, state, obs)
        next_state = edge.get("next") or state
        action = edge.get("do") or "stop"

        result.raw_events.insert(
            0,
            {
                "type": "scenario_state",
                "repetition": repetition,
                "guard_state_before": state,
                "guard_state_after": next_state,
                "obligation": deepcopy(ledger[0]),
                "photo_asks": photo_asks,
                "observations": {
                    k: obs[k]
                    for k in (
                        "asked_for_photo",
                        "escalated_ops",
                        "threatened",
                        "closed_window",
                        "third_plus_ping",
                    )
                },
                "edge": {"if": edge.get("if"), "do": action, "next": next_state},
            },
        )
        if pending_guard_event is not None:
            result.raw_events.insert(1, pending_guard_event)
            pending_guard_event = None

        results.append(result)

        terminal = str(next_state).startswith("terminal_")
        state = next_state
        if terminal or action == "stop":
            break

        if action == "send_duplicate":
            ts = ts + timedelta(seconds=delay)
            pending_guard_event = _inject_duplicate(engine, script, ts)
            trigger = edge.get("wake") or "guard_message"
        else:
            ts = ts + timedelta(seconds=delay)
            trigger = edge.get("wake") or trigger

    return results


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


def run_scenario_jobs(
    *,
    recipe: dict,
    variant: str,
    run_id: str,
    adapter: str,
    model: str,
    repeat: int,
    dry_run: bool,
    store: ExperimentStore,
    root: Path | None = None,
    canned_steps: list[dict] | None = None,
) -> str:
    """k repetitions of the scripted scenario into `store` under `run_id`."""
    root = root or ROOT
    jobs = recipe.get("jobs") or [{}]
    job = jobs[0]
    fixture_rel = job.get("fixture") or "experiments/fixtures/photo_gamer.json"
    fixture_path = Path(fixture_rel)
    if not fixture_path.is_absolute():
        fixture_path = ROOT / fixture_rel
        if not fixture_path.exists() and root != ROOT:
            alt = Path(root) / fixture_rel
            if alt.exists():
                fixture_path = alt
    shift = load_shift(fixture_path)
    variant_dir = Path(variant)
    if not variant_dir.is_absolute():
        for base in (ROOT, Path(root)):
            cand = base / variant
            if (cand / "core").exists():
                variant_dir = cand
                break
        else:
            variant_dir = ROOT / variant
    if not (variant_dir / "core").exists():
        raise SystemExit(f"variant missing core/: {variant_dir}")

    k = max(1, int(repeat))
    model_params = {} if dry_run else _model_params(adapter, model)
    manifest = RunManifest(
        run_id=run_id,
        variant_name=variant_dir.name,
        prompt_hash=prompt_dir_hash(variant_dir),
        model="canned" if dry_run else model,
        model_params=model_params,
        adapter="canned" if dry_run else adapter,
        data_version=content_hash({"fixture": str(fixture_path)}),
        code_version=_code_version(),
        mode="scenario",
        shifts=[shift.id],
        repetitions=k,
        created_at=datetime.now(timezone.utc).isoformat(),
        tool_fixture_mode="synthetic_scenario",
    )
    store.create_run(manifest)
    grand = {"turns": 0, "cost_usd": 0.0}

    for rep in range(k):
        if dry_run or adapter in ("canned", "replay", "mock"):
            steps = canned_steps
            if steps is None:
                steps = passing_canned_steps(shift.script or {})
            ad: Any = CannedAdapter(steps)
        else:
            ad = _make_live_adapter(adapter, model)

        print(f"=== scenario {shift.id} rep {rep + 1}/{k} ({ad.name}) ===")
        results = run_scenario(
            shift,
            ad,
            variant_dir=variant_dir,
            run_id=run_id,
            repetition=rep,
            model_params=model_params,
        )
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
                f"[rep{rep} t{result.turn} {result.trigger}] "
                f"{result.decision} dms={len(result.messages)} "
                f"esc={len(result.escalations)} "
                f"cost=${result.usage.cost_usd:.4f}"
            )
            grand["turns"] += 1
            grand["cost_usd"] += result.usage.cost_usd

    store.seal_run(run_id, grand)
    print(f"done -> runs/{run_id}  total_cost=${grand['cost_usd']:.4f}")
    return run_id


def execute_scenario_recipe(
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
    """Control is skipped: gates are process checks on the candidate trajectory."""
    store = ExperimentStore(root / "runs")
    run_scenario_jobs(
        recipe=recipe,
        variant=candidate_variant,
        run_id=variant_id,
        adapter=adapter,
        model=model,
        repeat=repeat,
        dry_run=dry_run,
        store=store,
        root=root,
    )
    score = score_photo_gamer_run(store, control_id, variant_id, recipe)
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
