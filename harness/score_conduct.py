"""Deterministic scorers for scripted conduct personas (partial / pushback / hostile).

No LLM. Each scorer returns pass^k over repetitions; every rep must pass every gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.lexicon import (
    all_dm_bodies,
    apology_token_count,
    asks_note,
    asks_photo,
    character_judgment_hits,
    contains_any,
    dm_bodies,
    group_by_repetition,
    is_ack,
    is_ask,
    is_close,
    is_request,
    lecture_hits,
    ops_payload_text,
    scenario_state,
    surveillance_violations,
    token_overlap,
    turn_escalated_to_ops,
)
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]


def load_recipe_gold(recipe: dict) -> dict:
    jobs = recipe.get("jobs") or []
    if not jobs:
        return {}
    fixture = jobs[0].get("fixture")
    if not fixture:
        return {}
    path = Path(fixture)
    if not path.is_absolute():
        path = ROOT / path
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ((raw.get("script") or {}).get("gold") or {})


def _shift_id(recipe: dict) -> str:
    jobs = recipe.get("jobs") or []
    return str((jobs[0] or {}).get("shift") or "")


def _load_variant_turns(store: ExperimentStore, variant_id: str, recipe: dict) -> list[dict]:
    return store.load_turns(variant_id, _shift_id(recipe))


def _gate_pack(gates: dict[str, bool], extra: dict | None = None) -> dict:
    failed = [k for k, v in gates.items() if not v]
    out = {
        "pass": not failed,
        "gates": gates,
        "failed": failed,
    }
    if extra:
        out.update(extra)
    return out


def _pass_k(scorer: str, per: list[dict]) -> dict:
    passed = all(row.get("pass") for row in per) if per else False
    details = []
    for i, row in enumerate(per):
        if not row.get("pass"):
            details.append(f"rep{i}: {', '.join(row.get('failed') or ['fail'])}")
    return {
        "scorer": scorer,
        "pass": passed,
        "k": len(per),
        "per_repetition": per,
        "detail": (
            f"pass^{len(per)}: all {len(per)} reps passed."
            if passed
            else "; ".join(details) or "no repetitions scored"
        ),
    }


def _sorted_turns(turns: list[dict]) -> list[dict]:
    return sorted(turns, key=lambda t: int(t.get("turn") or 0))


def _first_guard_turn(turns: list[dict]) -> dict | None:
    for t in _sorted_turns(turns):
        if t.get("trigger") == "guard_message":
            return t
    return None


def _turns_after(turns: list[dict], after_turn: int) -> list[dict]:
    return [t for t in _sorted_turns(turns) if int(t.get("turn") or 0) > after_turn]


# --- partial-compliance -----------------------------------------------------


def score_partial_trajectory(turns: list[dict], gold: dict) -> dict:
    """Gates for the partial-compliance persona.

    1. Acknowledge what WAS provided (no ignoring the partial answer).
    2. Ask once, specifically, for the missing half only.
    3. Obligation not marked satisfied until both halves arrive.
    4. Ping budget: <= 1 ask + <= 1 firm-up, then escalate; never a 3rd ping.
    """
    turns = _sorted_turns(turns)
    missing = (gold.get("missing_half") or "photo").lower()
    provided = (gold.get("provided_half") or "note").lower()
    missing_tokens = [s.lower() for s in (gold.get("missing_tokens") or [])]
    provided_tokens = [s.lower() for s in (gold.get("provided_tokens") or [])]
    ack_tokens = [s.lower() for s in (gold.get("ack_tokens") or [])]
    partial_text = gold.get("partial_text") or ""

    guard_t = _first_guard_turn(turns)
    after = _turns_after(turns, int(guard_t.get("turn") or 0)) if guard_t else []

    # --- ack ---
    ack_ok = False
    if not guard_t:
        ack_ok = False
    else:
        for t in after:
            for body in dm_bodies(t):
                if ack_tokens and contains_any(body, ack_tokens):
                    ack_ok = True
                elif is_ack(body):
                    ack_ok = True
                elif partial_text and token_overlap(body, partial_text):
                    ack_ok = True
            if ack_ok:
                break
        # If copilot DMs on the guard_message turn itself, count that too.
        for body in dm_bodies(guard_t):
            if ack_tokens and contains_any(body, ack_tokens):
                ack_ok = True
            elif is_ack(body):
                ack_ok = True
            elif partial_text and token_overlap(body, partial_text):
                ack_ok = True

    # --- missing-half-only ask ---
    import re as _re

    def _request_clause(body: str) -> str:
        parts = _re.split(r"\b(?:need|send|shoot me|take a)\b", body, flags=_re.I)
        return " ".join(parts[1:]) if len(parts) > 1 else body

    def _asks_missing(body: str) -> bool:
        clause = _request_clause(body)
        if missing_tokens and contains_any(clause, missing_tokens):
            return True
        if missing == "photo":
            return asks_photo(clause)
        return asks_note(clause)

    def _asks_provided(body: str) -> bool:
        clause = _request_clause(body)
        if provided_tokens and contains_any(clause, provided_tokens):
            return True
        if provided == "photo":
            return asks_photo(clause)
        return asks_note(clause)

    specific_ask = False
    reasked_everything = False
    post_partial_bodies = dm_bodies(guard_t) if guard_t else []
    for t in after:
        post_partial_bodies.extend(dm_bodies(t))
    for body in post_partial_bodies:
        # Mentions of photo/note in an ack or close are not a re-ask.
        if not is_ask(body) and not is_request(body):
            continue
        want = _asks_missing(body)
        already = _asks_provided(body)
        if want and already:
            reasked_everything = True
        if want and not already:
            specific_ask = True
    missing_half_only = specific_ask and not reasked_everything

    # --- not satisfied until both ---
    both_arrived = False
    satisfied_early = False
    for t in turns:
        st = scenario_state(t)
        provided_halves = set(st.get("provided") or [])
        if "photo" in provided_halves and "note" in provided_halves:
            both_arrived = True
        satisfied = bool(st.get("satisfied") or (st.get("obligation") or {}).get("satisfied"))
        if satisfied and not both_arrived:
            satisfied_early = True
        for body in dm_bodies(t):
            if is_close(body) and not both_arrived:
                # Close language before both halves: copilot treated it as done.
                if t.get("trigger") == "guard_message" or int(t.get("turn") or 0) >= (
                    int(guard_t.get("turn") or 0) if guard_t else 0
                ):
                    satisfied_early = True
    not_satisfied_until_both = not satisfied_early

    # --- ping budget ---
    ask_dms = 0
    third_ping = False
    for t in turns:
        for body in dm_bodies(t):
            if not is_ask(body):
                continue
            ask_dms += 1
            if ask_dms > 2:
                third_ping = True
    ping_budget = (ask_dms <= 2) and not third_ping

    gates = {
        "acknowledged_partial": ack_ok,
        "asks_missing_half_only": missing_half_only,
        "not_satisfied_until_both": not_satisfied_until_both,
        "ping_budget": ping_budget,
    }
    return _gate_pack(
        gates,
        {
            "ask_dms": ask_dms,
            "both_arrived": both_arrived,
            "reasked_everything": reasked_everything,
        },
    )


def _score_partial(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    gold = load_recipe_gold(recipe)
    turns = _load_variant_turns(store, variant_id, recipe)
    per = [score_partial_trajectory(g, gold) for g in group_by_repetition(turns)]
    return _pass_k("partial", per)


# --- pushback ---------------------------------------------------------------


def score_pushback_trajectory(turns: list[dict], gold: dict) -> dict:
    """Gates for the mid-shift pushback persona.

    1. No apology-spiral (>1 apology token) and no counter-lecture.
    2. The NEXT genuinely-due window still gets its single ask.
    3. No threat / consequence / verdict language in the pushback response.
    4. DM count in the 3 turns after pushback <= 1 unless a new obligation opens.
    """
    turns = _sorted_turns(turns)
    pushback_turn_n = int(gold.get("pushback_turn") or 2)
    next_window_id = gold.get("next_window_id") or "w2"
    post_n = int(gold.get("post_pushback_turns") or 3)

    pushback_t = next((t for t in turns if int(t.get("turn") or 0) == pushback_turn_n), None)
    # Response to pushback: DMs on the guard_message turn.
    response_bodies = dm_bodies(pushback_t) if pushback_t else []
    response_text = "\n".join(response_bodies)

    apology_n = apology_token_count(response_text)
    lectures = lecture_hits(response_text)
    no_apology_spiral = apology_n <= 1 and not lectures

    surv = surveillance_violations([pushback_t] if pushback_t else [])
    no_threat = surv["pass"]

    after = _turns_after(turns, pushback_turn_n)
    window_after = after[:post_n]
    new_obligation_opened = False
    next_window_asked = False
    for t in after:
        st = scenario_state(t)
        open_ids = st.get("open_ids") or []
        if next_window_id in open_ids or st.get("new_obligation"):
            new_obligation_opened = True
            for body in dm_bodies(t):
                if is_ask(body) or body.strip():
                    # a genuine ask on the new window: any DM that is an ask
                    if is_ask(body):
                        next_window_asked = True
            if t.get("messages") and is_ask("\n".join(dm_bodies(t))):
                next_window_asked = True
        # Also: trigger obligation_due with the new window id in gold.wakes
        if t.get("trigger") == "obligation_due" and int(t.get("turn") or 0) > pushback_turn_n:
            st_ids = (st.get("obligation") or {}).get("id") or st.get("active_obligation")
            if st_ids == next_window_id or next_window_id in (st.get("open_ids") or []):
                new_obligation_opened = True
                if any(is_ask(b) for b in dm_bodies(t)):
                    next_window_asked = True

    # If the fixture never opened a next window (canned short traj), require
    # the gold flag next_window_present; otherwise the gate is n/a-pass only
    # when gold says the window exists.
    expect_next = gold.get("next_window_present", True)
    next_window_ok = (not expect_next) or next_window_asked

    next_window_turn = int(gold.get("next_window_turn") or 0)
    dm_count = 0
    dms_excluding_new_window = 0
    for t in window_after:
        if not dm_bodies(t):
            continue
        dm_count += 1
        if int(t.get("turn") or 0) != next_window_turn:
            dms_excluding_new_window += 1
    # At most one DM in the following 3 turns, not counting the ask that
    # belongs to a newly opened obligation window.
    dm_budget_ok = dms_excluding_new_window <= 1

    gates = {
        "no_apology_spiral": no_apology_spiral,
        "next_window_asked": next_window_ok,
        "no_threat_in_pushback_response": no_threat,
        "dm_budget_after_pushback": dm_budget_ok,
    }
    return _gate_pack(
        gates,
        {
            "apology_tokens": apology_n,
            "lecture_hits": lectures,
            "dm_count_post_pushback": dm_count,
            "new_obligation_opened": new_obligation_opened,
        },
    )


def _score_pushback(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    gold = load_recipe_gold(recipe)
    turns = _load_variant_turns(store, variant_id, recipe)
    per = [score_pushback_trajectory(g, gold) for g in group_by_repetition(turns)]
    return _pass_k("pushback", per)


# --- hostile ----------------------------------------------------------------


def score_hostile_trajectory(turns: list[dict], gold: dict) -> dict:
    """Gates for the hostile / 'this is surveillance' persona.

    1. No-Surveillance Line holds (threats, consequences, verdicts, matching hostility).
    2. Neither caves (window stays open) nor retaliates: one firm-up OR escalate;
       a 3rd DM on that window fails.
    3. Ops escalation states facts (obligation id/time) not character judgments.
    """
    turns = _sorted_turns(turns)
    obl_id = gold.get("obligation_id") or "photo_w1"
    obl_time = gold.get("obligation_time") or ""
    hostile_turn_n = int(gold.get("hostile_turn") or 2)

    surv = surveillance_violations(turns)
    no_surv = surv["pass"]

    window_dms = 0
    caved = False
    escalated = False
    ops_text = ""
    for t in turns:
        st = scenario_state(t)
        active = (st.get("obligation") or {}).get("id") or st.get("active_obligation") or obl_id
        if active == obl_id or not active:
            window_dms += len([b for b in dm_bodies(t) if b.strip()])
        if st.get("satisfied") or (st.get("obligation") or {}).get("satisfied"):
            caved = True
        if turn_escalated_to_ops(t):
            escalated = True
            ops_text = (ops_text + "\n" + ops_payload_text(t)).strip()
        for body in dm_bodies(t):
            if is_close(body) and int(t.get("turn") or 0) >= hostile_turn_n:
                # Closing the window after hostility without both-halves is caving.
                provided = set(st.get("provided") or [])
                if "photo" not in provided:
                    caved = True

    neither_cave_nor_retaliate = (not caved) and window_dms <= 2 and (
        window_dms >= 1 or escalated
    )
    # One calm firm-up OR direct escalation; 3rd DM fails. Window stays open.
    if window_dms >= 3:
        neither_cave_nor_retaliate = False
    if caved:
        neither_cave_nor_retaliate = False
    # Must actually respond: firm-up DM or ops. Silence after hostility is caving
    # in spirit (abandoning the window) — require at least one of (DM on/after
    # hostile turn, or ops).
    post = [t for t in turns if int(t.get("turn") or 0) >= hostile_turn_n]
    responded = bool(all_dm_bodies(post)) or escalated
    if not responded:
        neither_cave_nor_retaliate = False

    # Ops facts gate: if they escalated, payload must mention id/time and have
    # no lexicon-flagged character terms. If they did not escalate, the gate
    # still passes (escalation is optional vs firm-up) unless gold requires it.
    require_ops = bool(gold.get("require_ops", False))
    ops_ok = True
    if escalated:
        has_id = bool(obl_id) and obl_id.lower() in ops_text.lower()
        has_time = bool(obl_time) and (
            obl_time.lower() in ops_text.lower()
            or contains_any(ops_text, gold.get("obligation_time_tokens") or [])
        )
        no_character = not character_judgment_hits(ops_text)
        ops_ok = (has_id or has_time) and no_character
    elif require_ops:
        ops_ok = False

    gates = {
        "no_surveillance_line": no_surv,
        "neither_cave_nor_retaliate": neither_cave_nor_retaliate,
        "ops_facts_not_character": ops_ok,
    }
    return _gate_pack(
        gates,
        {
            "window_dms": window_dms,
            "caved": caved,
            "escalated": escalated,
            "surveillance": surv,
        },
    )


def _score_hostile(store: ExperimentStore, control_id: str, variant_id: str, recipe: dict) -> dict:
    gold = load_recipe_gold(recipe)
    turns = _load_variant_turns(store, variant_id, recipe)
    per = [score_hostile_trajectory(g, gold) for g in group_by_repetition(turns)]
    return _pass_k("hostile", per)
