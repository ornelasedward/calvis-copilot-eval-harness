"""Session B: the LLM diagnostician, driven by a faked model. No API calls.

Every test injects `call_llm` (or asserts it is never reached), so the suite
never needs an API key and never touches the network. The point of these tests
is the post-validator: the model owns the pick and the wording, the catalog owns
the scorer / recipe / holdout / mode / spec, and anything the model invents
falls back to the deterministic pick with the reason on the rationale.
"""

from __future__ import annotations

import json

import pytest

from harness.agent.catalog import CLASS_CATALOG, process_spec_for
from harness.agent.diagnose import (
    ALLOWED_TARGET_PREFIXES,
    DiagnosisRejected,
    build_user_prompt,
    diagnose,
    diagnose_deterministic,
    parse_strict_json,
)
from harness.agent.mine import mine_shift
from harness.agent.types import Diagnosis


# --- helpers ---------------------------------------------------------------


@pytest.fixture(scope="module")
def cards():
    """Session A is live: 10 real evidence-cited cards, no API call."""
    mined = mine_shift("50737")
    assert len(mined) >= 2
    return mined


def card_of(cards, problem_class):
    for c in cards:
        if c.problem_class == problem_class:
            return c
    raise AssertionError(f"no {problem_class} card mined")


def good_payload(card, **overrides):
    meta = CLASS_CATALOG[card.problem_class]
    payload = {
        "card_id": card.id,
        "target_file": meta["policy_files"][0],
        "scorer": meta["scorer"],
        "must_improve": "call get_guard_locations before affirming a work claim",
        "must_preserve": ["the ask stays curious, not accusing"],
        "must_not_happen": ["a second DM on the same window"],
        "rationale": "turn 4 affirmed the claim with no location check.",
    }
    payload.update(overrides)
    return payload


class FakeLLM:
    """Returns canned replies in order; records what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return reply if isinstance(reply, str) else json.dumps(reply)


def no_api(*args, **kwargs):
    raise AssertionError("the diagnostician must not call the model here")


# --- valid pick ------------------------------------------------------------


def test_valid_llm_pick_overrides_the_deterministic_card(cards):
    """The model may override the pick; the catalog still owns everything else."""
    card = card_of(cards, "unverified_claim")
    assert diagnose_deterministic(cards).problem_class == "under_escalation"

    llm = FakeLLM(good_payload(card))
    d = diagnose(cards, skip_llm=False, call_llm=llm)

    assert isinstance(d, Diagnosis)
    assert len(llm.calls) == 1
    assert d.card_id == card.id
    assert d.shift_id == card.shift_id
    assert d.problem_class == "unverified_claim"
    assert d.target_file == "instructions/guard_response.md"
    assert d.target_file.startswith(ALLOWED_TARGET_PREFIXES)
    # scorer / recipe / holdout / mode / spec are catalog values, not LLM values
    assert d.scorer == "verify_b"
    assert d.recipe == "b-claims"
    assert d.holdout_recipe == "a3-shift-55252"
    assert d.mode == "turn"
    assert d.spec == process_spec_for("unverified_claim")
    assert "get_guard_locations" in d.must_improve
    assert "LLM pick" in d.rationale
    assert "fallback" not in d.rationale


def test_llm_pick_keeps_the_preserve_floor(cards):
    card = card_of(cards, "unverified_claim")
    d = diagnose(cards, skip_llm=False, call_llm=FakeLLM(good_payload(card)))
    assert "session_start welcome DM" in d.must_preserve
    assert "reply to every guard_message" in d.must_preserve
    assert "soft DM in place of a required flag" in d.must_not_happen
    # the model's own lines survive too
    assert "the ask stays curious, not accusing" in d.must_preserve


def test_llm_may_pick_the_second_policy_file_of_its_class(cards):
    card = card_of(cards, "photo_without_inspect")
    payload = good_payload(card, target_file="instructions/guard_response.md")
    d = diagnose(cards, skip_llm=False, call_llm=FakeLLM(payload))
    assert d.target_file == "instructions/guard_response.md"
    assert d.scorer == "photo_inspect"
    assert d.mode == "turn"


def test_the_prompt_carries_the_json_evidence_and_the_allowed_choices(cards):
    prompt = build_user_prompt(cards)
    for card in cards:
        assert card.id in prompt
    assert "event_indexes" in prompt
    assert "allowed_target_files" in prompt
    assert "escalation_focus" in prompt  # catalog scorer, echoed back verbatim


# --- invalid output falls back --------------------------------------------


def _assert_fell_back(d, cards, *, contains):
    deterministic = diagnose_deterministic(cards)
    assert d.card_id == deterministic.card_id
    assert d.scorer == deterministic.scorer
    assert d.target_file == deterministic.target_file
    assert "LLM fallback" in d.rationale
    assert contains in d.rationale


def test_invented_card_id_falls_back(cards):
    llm = FakeLLM(good_payload(cards[0], card_id="50737-made-up-card"))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    assert len(llm.calls) == 2  # one retry before the fallback
    _assert_fell_back(d, cards, contains="not one of the mined cards")


def test_invented_scorer_falls_back(cards):
    card = card_of(cards, "unverified_claim")
    llm = FakeLLM(good_payload(card, scorer="vibes_v2"))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    _assert_fell_back(d, cards, contains="not the catalog scorer")


def test_target_file_outside_core_or_instructions_falls_back(cards):
    card = card_of(cards, "unverified_claim")
    llm = FakeLLM(good_payload(card, target_file="variants/baseline/README.md"))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    _assert_fell_back(d, cards, contains="must be under core/ or instructions/")


def test_target_file_not_a_policy_file_for_the_class_falls_back(cards):
    """core/tools.md is a real prompt file, but not for unverified_claim."""
    card = card_of(cards, "unverified_claim")
    llm = FakeLLM(good_payload(card, target_file="core/tools.md"))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    _assert_fell_back(d, cards, contains="is not a policy file")


def test_two_target_files_falls_back(cards):
    """One prompt file per iteration (LOOP.md rule 5)."""
    card = card_of(cards, "photo_without_inspect")
    llm = FakeLLM(
        good_payload(card, target_file="core/tools.md, instructions/guard_response.md")
    )
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    _assert_fell_back(d, cards, contains="is not a policy file")


def test_malformed_json_retries_once_then_falls_back(cards):
    llm = FakeLLM("Sure! Here is my pick: the unverified_claim card on turn 4.")
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    assert len(llm.calls) == 2
    assert "attempt 1:" in d.rationale and "attempt 2:" in d.rationale
    _assert_fell_back(d, cards, contains="not JSON")


def test_malformed_json_then_a_good_retry_is_accepted(cards):
    card = card_of(cards, "unverified_claim")
    llm = FakeLLM("not json at all", json.dumps(good_payload(card)))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    assert len(llm.calls) == 2
    assert d.card_id == card.id
    assert "LLM fallback" not in d.rationale
    # the retry prompt tells the model what was wrong
    assert "was rejected" in llm.calls[1][1]


def test_transport_failure_falls_back_without_raising(cards):
    def boom(system, user):
        raise RuntimeError("OPENAI_API_KEY not set")

    d = diagnose(cards, skip_llm=False, call_llm=boom)
    _assert_fell_back(d, cards, contains="OPENAI_API_KEY not set")


def test_missing_intent_triple_falls_back(cards):
    card = card_of(cards, "unverified_claim")
    payload = good_payload(card)
    payload.pop("must_not_happen")
    d = diagnose(cards, skip_llm=False, call_llm=FakeLLM(payload))
    _assert_fell_back(d, cards, contains="must_not_happen")


def test_a_payload_that_sets_pass_fail_falls_back(cards):
    """LOOP.md rule 3: the diagnostician never flips a gate."""
    card = card_of(cards, "unverified_claim")
    llm = FakeLLM(good_payload(card, targeted_pass=True))
    d = diagnose(cards, skip_llm=False, call_llm=llm)
    _assert_fell_back(d, cards, contains="never sets pass/fail")


def test_json_array_reply_falls_back(cards):
    d = diagnose(cards, skip_llm=False, call_llm=FakeLLM('[{"card_id": "x"}]'))
    _assert_fell_back(d, cards, contains="must be an object")


# --- parser ----------------------------------------------------------------


def test_parse_strict_json_accepts_a_fenced_object_and_rejects_prose():
    assert parse_strict_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_strict_json('  {"a": 1}  ') == {"a": 1}
    for bad in ("", "I picked the first card.", 'The answer is {"a": 1} — hope that helps'):
        with pytest.raises(DiagnosisRejected):
            parse_strict_json(bad)


# --- deterministic path is unchanged ---------------------------------------


def test_deterministic_path_is_unchanged_and_api_free(cards, monkeypatch):
    import harness.agent.diagnose as diag

    monkeypatch.setattr(diag, "_llm_complete", no_api)
    d = diagnose(cards, skip_llm=True)
    same = diagnose_deterministic(cards)
    assert d.to_dict() == same.to_dict()
    assert d.card_id == "50737-under_escalation-t35"
    assert d.problem_class == "under_escalation"
    assert d.scorer == "escalation_focus"
    assert d.recipe == "a3-shift-55252"
    assert d.holdout_recipe is None
    assert d.mode == "shift"
    assert d.target_file == "instructions/scheduled_check_in.md"
    assert d.spec.require_escalation is True
    assert "Deterministic pick" in d.rationale


def test_diagnosis_never_carries_a_gate(cards):
    card = card_of(cards, "unverified_claim")
    d = diagnose(cards, skip_llm=False, call_llm=FakeLLM(good_payload(card)))
    keys = set(d.to_dict())
    assert not keys & {"targeted_pass", "preserve_pass", "holdout_pass", "pass", "score"}


def test_empty_card_list_is_an_error(monkeypatch):
    import harness.agent.diagnose as diag

    monkeypatch.setattr(diag, "_llm_complete", no_api)
    with pytest.raises(ValueError, match="at least one ProblemCard"):
        diagnose([], skip_llm=True)
    with pytest.raises(ValueError, match="at least one ProblemCard"):
        diagnose([], skip_llm=False)
