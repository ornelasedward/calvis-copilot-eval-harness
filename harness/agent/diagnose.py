"""Session B: pick one mined card and write the intent triple.

Never sets pass/fail. skip_llm must work without an API key.

Two paths, one contract:

* ``skip_llm=True`` — severity rank + catalog only, no API call.
* ``skip_llm=False`` — the mined cards (with their JSON evidence) go to the
  model, which picks ONE card and writes ``must_improve`` / ``must_preserve`` /
  ``must_not_happen`` plus a rationale.

The model only ever gets to choose *among the mined cards* and to phrase the
intent. Everything that the rest of the loop depends on is re-derived in code
after the call: ``card_id`` must be one of the input cards, ``target_file`` must
be a single catalog ``policy_files`` entry under ``core/`` or ``instructions/``
for that card's class, and ``scorer`` / ``recipe`` / ``holdout_recipe`` /
``mode`` / ``spec`` are copied straight out of ``catalog.py`` — never from the
response. Anything else is rejected: one retry, then
:func:`diagnose_deterministic`, with the fallback reason recorded on
``Diagnosis.rationale``.

LOOP.md hard rule 3: the diagnostician never flips a gate. The response schema
has no pass/fail field and a payload that smuggles one in is rejected.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from harness.agent.catalog import (
    CLASS_CATALOG,
    SEVERITY_RANK,
    catalog_entry,
    process_spec_for,
)
from harness.agent.types import Diagnosis, ProblemCard

# (system, user) -> raw model text. Same shape as harness.judge.CompleteFn so a
# test can inject a canned response without touching an SDK.
CallLLM = Callable[[str, str], str]

MAX_ATTEMPTS = 2  # first call + exactly one retry, then deterministic fallback

# target_file must live in the prompt tree the patcher is allowed to edit.
ALLOWED_TARGET_PREFIXES = ("core/", "instructions/")

# Keys that would mean the diagnostician tried to own a gate (LOOP.md rule 3).
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "pass",
        "fail",
        "passed",
        "failed",
        "gate",
        "targeted_pass",
        "preserve_pass",
        "holdout_pass",
        "score",
        "verdict",
        "grade",
        "rating",
    }
)

MUST_PRESERVE_FLOOR = [
    "session_start welcome DM",
    "reply to every guard_message",
]
MUST_NOT_HAPPEN_FLOOR = [
    "missed escalation vs same-model control on holdout",
    "soft DM in place of a required flag",
]

DIAGNOSE_SYSTEM = """You are the Calvis eval-loop diagnostician for a security-guard copilot prompt harness.

You are given problem cards that were mined deterministically from one shift JSON
(events + baseline). Pick EXACTLY ONE card and state the intent for the patch.

Hard rules:
- You do NOT decide pass/fail and you do NOT touch gates. Deterministic scorers own
  PASS/FAIL. Never output a pass, fail, score, grade, or verdict field.
- card_id MUST be copied verbatim from the supplied cards. Never invent one.
- target_file MUST be exactly one path copied from that card's allowed_target_files.
  One file per iteration — never a list, never a second file, never a new path.
- scorer MUST be copied verbatim from that card's scorer field. Never invent one.
- must_improve is the one behavior the patch should move, phrased against the cited
  JSON evidence for the chosen card.
- must_preserve is what the patch must not cost us (welcome DM, replying to guards).
- must_not_happen is the regression that would make this patch not worth keeping.
- Photos in this bundle are "[photo]" placeholders. You may reason about inspect-or-not
  and ask-again-or-not. Never claim two photos showed the same place.
- Return JSON only, matching the schema. No markdown, no prose, no commentary.
"""

# Strict response schema. Sent as an OpenAI json_schema response format and as an
# Anthropic forced-tool input_schema, and re-checked in code either way.
DIAGNOSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "card_id",
        "target_file",
        "scorer",
        "must_improve",
        "must_preserve",
        "must_not_happen",
        "rationale",
    ],
    "properties": {
        "card_id": {
            "type": "string",
            "description": "verbatim id of the chosen card",
        },
        "target_file": {
            "type": "string",
            "description": "one path from that card's allowed_target_files",
        },
        "scorer": {
            "type": "string",
            "description": "verbatim scorer of the chosen card",
        },
        "must_improve": {"type": "string"},
        "must_preserve": {"type": "array", "items": {"type": "string"}},
        "must_not_happen": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
}


class DiagnosisRejected(ValueError):
    """The LLM payload did not survive post-validation."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diagnose(
    cards: list[ProblemCard],
    *,
    skip_llm: bool = True,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    call_llm: CallLLM | None = None,
) -> Diagnosis:
    """Select one card. skip_llm uses severity rank + catalog only.

    `call_llm(system, user) -> str` overrides the adapter transport; tests inject
    a canned response through it so no API key is ever needed.
    """
    if skip_llm:
        return diagnose_deterministic(cards)

    fallback = diagnose_deterministic(cards)  # also validates the cards
    fn = call_llm or (
        lambda system, user: _llm_complete(system, user, adapter=adapter, model=model)
    )

    system = DIAGNOSE_SYSTEM
    user = build_user_prompt(cards)
    reasons: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = fn(system, user)
            payload = parse_strict_json(raw)
            return diagnosis_from_payload(payload, cards)
        except Exception as exc:  # noqa: BLE001 — any failure falls back, never raises
            reason = f"{type(exc).__name__}: {exc}"
            reasons.append(f"attempt {attempt}: {reason}")
            if attempt >= MAX_ATTEMPTS:
                break
            user = _retry_prompt(cards, reason)

    fallback.rationale = (
        f"{fallback.rationale} [LLM fallback after {len(reasons)} attempt(s): "
        f"{'; '.join(reasons)}]"
    )
    return fallback


def diagnose_deterministic(cards: list[ProblemCard]) -> Diagnosis:
    """Highest severity, then first card. No API."""
    if not cards:
        raise ValueError("diagnose requires at least one ProblemCard")
    for card in cards:
        card.validate()
    chosen = sorted(
        cards,
        key=lambda c: (
            SEVERITY_RANK[c.severity],
            c.shift_id,
            c.turns[0] if c.turns else 0,
        ),
    )[0]
    meta = catalog_entry(chosen.problem_class)
    return Diagnosis(
        card_id=chosen.id,
        shift_id=chosen.shift_id,
        problem_class=chosen.problem_class,
        must_improve=meta["json_signal"],
        must_preserve=list(MUST_PRESERVE_FLOOR),
        must_not_happen=list(MUST_NOT_HAPPEN_FLOOR),
        target_file=meta["policy_files"][0],
        scorer=meta["scorer"],
        recipe=meta.get("recipe"),
        holdout_recipe=meta.get("holdout_recipe"),
        spec=process_spec_for(chosen.problem_class),
        rationale=(
            f"Deterministic pick: severity={chosen.severity} "
            f"class={chosen.problem_class} (Session B LLM can override pick, not gates)."
        ),
        mode=meta["mode"],
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def card_brief(card: ProblemCard) -> dict[str, Any]:
    """What the model sees: the card, its JSON evidence, and its fixed choices."""
    meta = CLASS_CATALOG[card.problem_class]
    return {
        "card_id": card.id,
        "shift_id": card.shift_id,
        "problem_class": card.problem_class,
        "severity": card.severity,
        "turns": list(card.turns),
        "json_signal": meta["json_signal"],
        "evidence": {
            "guard_text": card.evidence.guard_text,
            "baseline_dms": list(card.evidence.baseline_dms),
            "baseline_tools": list(card.evidence.baseline_tools),
            "missing_tools": list(card.evidence.missing_tools),
            "event_indexes": list(card.evidence.event_indexes),
            "baseline_indexes": list(card.evidence.baseline_indexes),
            "notes": list(card.evidence.notes),
        },
        # Fixed by the catalog. Echo them back verbatim or the pick is rejected.
        "allowed_target_files": list(meta["policy_files"]),
        "scorer": meta["scorer"],
        "mode": meta["mode"],
    }


def build_user_prompt(cards: list[ProblemCard]) -> str:
    briefs = [card_brief(c) for c in cards]
    return (
        "Pick exactly one card and write the intent triple.\n\n"
        "Response JSON schema (return an object matching this and nothing else):\n"
        f"{json.dumps(DIAGNOSIS_JSON_SCHEMA, indent=2)}\n\n"
        "CARDS_JSON:\n"
        f"{json.dumps(briefs, indent=2)[:24000]}\n"
    )


def _retry_prompt(cards: list[ProblemCard], reason: str) -> str:
    return (
        f"Your previous reply was rejected: {reason}\n"
        "Return JSON only, with card_id / target_file / scorer copied verbatim "
        "from one of the cards below.\n\n" + build_user_prompt(cards)
    )


# ---------------------------------------------------------------------------
# Parsing + post-validation
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```$")


def parse_strict_json(text: str) -> dict[str, Any]:
    """Whole reply must be one JSON object. Prose is rejected, not scavenged."""
    blob = (text or "").strip()
    if not blob:
        raise DiagnosisRejected("empty model reply")
    if blob.startswith("```"):
        blob = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", blob)).strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise DiagnosisRejected(f"reply is not JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise DiagnosisRejected(f"reply JSON must be an object, got {type(data).__name__}")
    return data


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DiagnosisRejected(f"{key} must be a non-empty string")
    return value.strip()


def _require_str_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise DiagnosisRejected(f"{key} must be a non-empty list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DiagnosisRejected(f"{key} entries must be non-empty strings")
        out.append(item.strip())
    return out


def diagnosis_from_payload(payload: dict[str, Any], cards: list[ProblemCard]) -> Diagnosis:
    """Post-validate the LLM pick and rebuild the Diagnosis from the catalog.

    The model owns the pick and the wording. It never owns card identity,
    target file, scorer, recipe, holdout, mode, or spec.
    """
    if not cards:
        raise ValueError("diagnose requires at least one ProblemCard")

    smuggled = sorted(FORBIDDEN_PAYLOAD_KEYS & {str(k).lower() for k in payload})
    if smuggled:
        raise DiagnosisRejected(
            f"diagnostician never sets pass/fail; forbidden keys: {smuggled}"
        )

    card_id = _require_text(payload, "card_id")
    by_id = {c.id: c for c in cards}
    card = by_id.get(card_id)
    if card is None:
        raise DiagnosisRejected(
            f"card_id {card_id!r} is not one of the mined cards {sorted(by_id)}"
        )

    meta = catalog_entry(card.problem_class)

    # problem_class is optional in the schema, but a mismatch means the model
    # was reasoning about a different card than the one it named.
    claimed_class = payload.get("problem_class")
    if claimed_class is not None and str(claimed_class) != card.problem_class:
        raise DiagnosisRejected(
            f"problem_class {claimed_class!r} does not match card {card_id!r} "
            f"({card.problem_class})"
        )

    target_file = _require_text(payload, "target_file")
    if not target_file.startswith(ALLOWED_TARGET_PREFIXES):
        raise DiagnosisRejected(
            f"target_file {target_file!r} must be under core/ or instructions/"
        )
    if target_file not in meta["policy_files"]:
        raise DiagnosisRejected(
            f"target_file {target_file!r} is not a policy file for "
            f"{card.problem_class}: {meta['policy_files']}"
        )

    scorer = _require_text(payload, "scorer")
    if scorer != meta["scorer"]:
        raise DiagnosisRejected(
            f"scorer {scorer!r} is not the catalog scorer for "
            f"{card.problem_class} ({meta['scorer']!r})"
        )

    must_improve = _require_text(payload, "must_improve")
    must_preserve = _require_str_list(payload, "must_preserve")
    must_not_happen = _require_str_list(payload, "must_not_happen")
    rationale = _require_text(payload, "rationale")

    # The floor is non-negotiable; the model may add to it, not drop it.
    for line in MUST_PRESERVE_FLOOR:
        if line not in must_preserve:
            must_preserve.append(line)
    for line in MUST_NOT_HAPPEN_FLOOR:
        if line not in must_not_happen:
            must_not_happen.append(line)

    return Diagnosis(
        card_id=card.id,
        shift_id=card.shift_id,
        problem_class=card.problem_class,
        must_improve=must_improve,
        must_preserve=must_preserve,
        must_not_happen=must_not_happen,
        target_file=target_file,
        scorer=meta["scorer"],
        recipe=meta.get("recipe"),
        holdout_recipe=meta.get("holdout_recipe"),
        spec=process_spec_for(card.problem_class),
        rationale=(
            f"LLM pick: {rationale} "
            f"(scorer/recipe/holdout/mode/spec from catalog.py; gates unchanged.)"
        ),
        mode=meta["mode"],
    )


# ---------------------------------------------------------------------------
# Transport — same direct-SDK style as harness/judge.py and harness/advisor.py
# ---------------------------------------------------------------------------


def _llm_complete(system: str, user: str, *, adapter: str, model: str) -> str:
    if adapter == "openai":
        return _openai_json(system, user, model=model)
    if adapter == "anthropic":
        return _anthropic_json(system, user, model=model)
    raise ValueError(f"unknown adapter: {adapter}")


def _openai_json(system: str, user: str, *, model: str) -> str:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if str(model).startswith("gpt-5"):
        kwargs["reasoning_effort"] = "none"
    else:
        kwargs["temperature"] = 0

    strict = {
        "type": "json_schema",
        "json_schema": {
            "name": "diagnosis",
            "strict": True,
            "schema": DIAGNOSIS_JSON_SCHEMA,
        },
    }
    try:
        resp = client.chat.completions.create(response_format=strict, **kwargs)
    except Exception:
        # Older SDK / model without json_schema support: plain JSON mode still
        # rejects prose, and the schema is repeated in the user prompt.
        resp = client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs
        )
    return (resp.choices[0].message.content or "").strip()


def _anthropic_json(system: str, user: str, *, model: str) -> str:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    # A forced tool call is Anthropic's structured-output mode: the reply is the
    # schema-shaped tool input, so prose cannot come back.
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[
            {
                "name": "emit_diagnosis",
                "description": "Return the chosen card and the intent triple.",
                "input_schema": DIAGNOSIS_JSON_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "emit_diagnosis"},
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return json.dumps(dict(block.input or {}))
    raise DiagnosisRejected("anthropic reply carried no emit_diagnosis tool call")
