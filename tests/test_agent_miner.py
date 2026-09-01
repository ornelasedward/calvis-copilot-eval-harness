"""Session A locks: the miner reads shift JSON and cites it. No API calls.

Every assertion here is offline: `mine_shift` only touches shifts/*.json via
harness.loader, and the synthetic bundles are written to tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.adapters.replay import iter_baseline_turns
from harness.agent.catalog import CLASS_CATALOG, process_spec_for
from harness.agent.mine import (
    PHOTO_PLACEHOLDER,
    _asks_for_a_photo,
    mine_all,
    mine_shift,
    shift_path,
    summarize,
)
from harness.agent.types import PROBLEM_CLASSES
from harness.loader import load_shift

ROOT = Path(__file__).resolve().parents[1]
SHIFTS = ROOT / "shifts"


def _turn_numbers(shift_id: str) -> set[int]:
    return {t.turn for t in iter_baseline_turns(load_shift(SHIFTS / f"{shift_id}.json"))}


# ---------------------------------------------------------------------------
# The two shifts named in LOOP.md's "Session done-when"
# ---------------------------------------------------------------------------


def test_50737_yields_unverified_claim_cards_on_real_turns():
    cards = mine_shift("50737")
    claims = [c for c in cards if c.problem_class == "unverified_claim"]
    assert claims, "50737 must mine at least one unverified_claim"

    real_turns = _turn_numbers("50737")
    for card in claims:
        card.validate()
        assert card.shift_id == "50737"
        assert set(card.turns) <= real_turns
        assert card.spec == process_spec_for("unverified_claim")
        assert card.severity == CLASS_CATALOG["unverified_claim"]["severity"]
        assert card.policy_files == CLASS_CATALOG["unverified_claim"]["policy_files"]
        # Evidence points back into the file: guard quote + the missing check.
        assert card.evidence.event_indexes
        assert card.evidence.guard_text
        assert card.evidence.baseline_dms
        assert card.evidence.missing_tools == ["get_guard_locations"]
        assert "get_guard_locations" not in card.evidence.baseline_tools[
            : card.evidence.baseline_tools.index("request_copilot_dm") + 1
        ]


def test_50737_first_claim_is_the_perimeter_all_clear_at_turn_4():
    cards = mine_shift("50737", classes=["unverified_claim"])
    turn4 = [c for c in cards if c.turns == [4]]
    assert turn4, [c.turns for c in cards]
    card = turn4[0]
    assert "perimeter" in (card.evidence.guard_text or "").lower()
    assert any("Got it" in dm for dm in card.evidence.baseline_dms)


def test_55252_yields_under_escalation():
    cards = mine_shift("55252")
    esc = [c for c in cards if c.problem_class == "under_escalation"]
    assert esc, "55252 (silent guard) must mine under_escalation"
    real_turns = _turn_numbers("55252")
    for card in esc:
        card.validate()
        assert set(card.turns) <= real_turns
        assert card.severity == "safety"
        assert card.spec == process_spec_for("under_escalation")
        assert card.spec.require_escalation is True
        assert "escalate_to_ops" in card.evidence.missing_tools
        assert any("silent" in n for n in card.evidence.notes)


def test_55252_does_not_flag_the_turn_that_escalated():
    cards = mine_shift("55252", classes=["under_escalation"])
    flagged = {t for c in cards for t in c.turns}
    # Turns 4/6/9 called escalate_to_ops / escalate_to_human / flag_copilot_guard.
    assert flagged.isdisjoint({4, 6, 9})


def test_55252_does_not_flag_the_first_check_in_of_the_shift():
    """Silence is measured from shift start, so minute-zero is not a miss."""
    cards = mine_shift("55252", classes=["under_escalation"])
    assert 2 not in {t for c in cards for t in c.turns}


# ---------------------------------------------------------------------------
# Corpus-wide invariants
# ---------------------------------------------------------------------------


def test_every_mined_card_validates_and_matches_the_catalog():
    cards = mine_all()
    assert cards
    for card in cards:
        card.validate()  # raises on empty evidence / non-json source
        assert card.source == "json"
        assert card.problem_class in PROBLEM_CLASSES
        assert card.spec == process_spec_for(card.problem_class)
        assert card.severity == CLASS_CATALOG[card.problem_class]["severity"]
        assert card.policy_files == CLASS_CATALOG[card.problem_class]["policy_files"]
        assert card.turns
        assert card.evidence.has_citation()


def test_card_ids_are_unique_per_shift():
    ids = [c.id for c in mine_all()]
    assert len(ids) == len(set(ids))


def test_photo_cards_make_no_visual_claim():
    """LOOP.md hard rule 2: placeholders support inspect-or-not only."""
    cards = mine_all()
    photo_cards = [
        c
        for c in cards
        if c.problem_class in ("photo_without_inspect", "hammer_after_photo")
    ]
    for card in photo_cards:
        assert card.evidence.guard_text == PHOTO_PLACEHOLDER
    blob = json.dumps([c.to_dict() for c in cards]).lower()
    for forbidden in ("duplicate", "same location", "site_hero", "reused photo"):
        assert forbidden not in blob


def test_mine_all_accepts_an_explicit_shift_list():
    cards = mine_all(["55252"])
    assert cards and {c.shift_id for c in cards} == {"55252"}
    assert summarize(cards) == {"under_escalation": len(cards)}


def test_missing_shift_raises():
    with pytest.raises(FileNotFoundError):
        mine_shift("does-not-exist")


def test_shift_path_accepts_an_explicit_json_path():
    assert shift_path(str(SHIFTS / "55252.json")).name == "55252.json"


# ---------------------------------------------------------------------------
# Probe-level tests on synthetic bundles (scaffolding, not mined cards)
# ---------------------------------------------------------------------------


def _bundle(tmp_path: Path, shift_id: str, events: list[dict], baseline: list[dict]) -> Path:
    payload = {
        "shift": {
            "id": shift_id,
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-01T08:00:00+00:00",
            "timezone": "UTC",
        },
        "events": events,
        "baseline": baseline,
    }
    path = tmp_path / f"{shift_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _guard(ts: str, text: str | None = None, image: str | None = None) -> dict:
    return {"ts": ts, "type": "guard_message", "text": text, "image": image}


def _turn(ts: str, turn: int, trigger: str) -> dict:
    return {"ts": ts, "type": "turn_start", "turn": turn, "trigger": trigger}


def _call(ts: str, tool: str, body: str | None = None) -> dict:
    entry: dict = {"ts": ts, "type": "tool_call", "tool": f"mcp__calvis__{tool}", "input": {}}
    if body is not None:
        entry["input"] = {"body": body}
    return entry


def test_unverified_claim_not_flagged_when_location_checked_first(tmp_path):
    events = [_guard("2026-01-01T01:00:00+00:00", "Perimeter is all clear, trucks done")]
    baseline = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call("2026-01-01T01:00:12+00:00", "get_guard_locations"),
        _call("2026-01-01T01:00:14+00:00", "request_copilot_dm", "Got it, thanks."),
    ]
    path = _bundle(tmp_path, "9001", events, baseline)
    assert mine_shift(str(path), classes=["unverified_claim"]) == []

    # Same turn, location check moved after the DM -> the claim was affirmed blind.
    baseline_blind = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call("2026-01-01T01:00:12+00:00", "request_copilot_dm", "Got it, thanks."),
        _call("2026-01-01T01:00:14+00:00", "get_guard_locations"),
    ]
    path2 = _bundle(tmp_path, "9002", events, baseline_blind)
    cards = mine_shift(str(path2), classes=["unverified_claim"])
    assert [c.turns for c in cards] == [[1]]
    assert cards[0].evidence.event_indexes == [0]
    assert cards[0].evidence.baseline_indexes[0] == 0  # the turn_start entry


def test_photo_without_inspect_flags_only_the_uninspected_photo(tmp_path):
    events = [
        _guard("2026-01-01T01:00:00+00:00", image=PHOTO_PLACEHOLDER),
        _guard("2026-01-01T02:00:00+00:00", image=PHOTO_PLACEHOLDER),
    ]
    baseline = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call("2026-01-01T01:00:12+00:00", "fetch_chat_image"),
        _turn("2026-01-01T02:00:10+00:00", 2, "guard_message"),
        _call("2026-01-01T02:00:12+00:00", "request_copilot_dm", "Nice, logged."),
    ]
    path = _bundle(tmp_path, "9003", events, baseline)
    cards = mine_shift(str(path), classes=["photo_without_inspect"])
    assert len(cards) == 1
    card = cards[0]
    assert card.turns == [2]
    assert card.evidence.event_indexes == [1]
    assert card.evidence.missing_tools == ["fetch_chat_image"]
    assert card.evidence.guard_text == PHOTO_PLACEHOLDER


def test_hammer_after_photo_flags_a_repeat_photo_ask(tmp_path):
    events = [_guard("2026-01-01T01:00:00+00:00", image=PHOTO_PLACEHOLDER)]
    baseline = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call("2026-01-01T01:00:12+00:00", "request_copilot_dm", "Thanks."),
        _turn("2026-01-01T01:05:00+00:00", 2, "guard_message"),
        _call(
            "2026-01-01T01:05:02+00:00",
            "request_copilot_dm",
            "Can you send me another photo of the gate?",
        ),
    ]
    path = _bundle(tmp_path, "9004", events, baseline)
    cards = mine_shift(str(path), classes=["hammer_after_photo"])
    assert [c.turns for c in cards] == [[2]]
    assert cards[0].evidence.baseline_dms == ["Can you send me another photo of the gate?"]


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Can you send me a photo of the front gate?", True),
        ("Send me another shot when you can.", True),
        ("Got it, I see the photo. Just need you staying on the post, yeah?", False),
        ("Good coverage on the photos, don't need more shots, just a heads up.", False),
        ("Send the pictures to your company like they ask.", False),
    ],
)
def test_photo_re_ask_detector(body, expected):
    assert _asks_for_a_photo(body) is expected


def test_ping_budget_needs_unanswered_repeats_not_a_conversation(tmp_path):
    # Three DMs, each answered by the guard: a back-and-forth, not a hammering.
    events = [
        _guard("2026-01-01T01:00:30+00:00", "on it"),
        _guard("2026-01-01T01:01:30+00:00", "still walking"),
    ]
    baseline = [
        _turn("2026-01-01T01:00:00+00:00", 1, "scheduled_check_in"),
        _call("2026-01-01T01:00:02+00:00", "request_copilot_dm", "Status?"),
        _turn("2026-01-01T01:01:00+00:00", 2, "guard_message"),
        _call("2026-01-01T01:01:02+00:00", "request_copilot_dm", "Thanks, keep going."),
        _turn("2026-01-01T01:02:00+00:00", 3, "guard_message"),
        _call("2026-01-01T01:02:02+00:00", "request_copilot_dm", "Copy that."),
    ]
    path = _bundle(tmp_path, "9005", events, baseline)
    assert mine_shift(str(path), classes=["ping_budget"]) == []

    # Same three DMs with no guard reply in between: two unanswered repeats.
    path2 = _bundle(tmp_path, "9006", [], baseline)
    cards = mine_shift(str(path2), classes=["ping_budget"])
    assert len(cards) == 1
    assert cards[0].turns == [1, 2, 3]
    assert len(cards[0].evidence.baseline_dms) == 3
    assert any("2 sent with no guard message" in n for n in cards[0].evidence.notes)


def test_surveillance_voice_quotes_the_lexicon_hit(tmp_path):
    baseline = [
        _turn("2026-01-01T01:00:00+00:00", 1, "scheduled_check_in"),
        _call(
            "2026-01-01T01:00:02+00:00",
            "request_copilot_dm",
            "I've escalated this to ops for non-compliance.",
        ),
    ]
    path = _bundle(tmp_path, "9007", [], baseline)
    cards = mine_shift(str(path), classes=["surveillance_voice"])
    assert len(cards) == 1
    assert cards[0].severity == "compliance"
    assert cards[0].evidence.baseline_dms == [
        "I've escalated this to ops for non-compliance."
    ]
    assert any("lexicon hits" in n for n in cards[0].evidence.notes)


def test_pushback_failure_spares_the_copilot_that_eased_off(tmp_path):
    events = [_guard("2026-01-01T01:00:00+00:00", "stop babysitting me, I know my job")]
    eased = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call("2026-01-01T01:00:12+00:00", "request_copilot_dm", "Hear you. I'll be around."),
    ]
    path = _bundle(tmp_path, "9008", events, eased)
    assert mine_shift(str(path), classes=["pushback_failure"]) == []

    doubled = [
        _turn("2026-01-01T01:00:10+00:00", 1, "guard_message"),
        _call(
            "2026-01-01T01:00:12+00:00",
            "request_copilot_dm",
            "GPS shows you stayed at the entry zone. That doesn't line up.",
        ),
    ]
    path2 = _bundle(tmp_path, "9009", events, doubled)
    cards = mine_shift(str(path2), classes=["pushback_failure"])
    assert [c.turns for c in cards] == [[1]]
    assert cards[0].evidence.guard_text == "stop babysitting me, I know my job"


def test_mining_a_bundle_with_no_baseline_turns_is_empty(tmp_path):
    path = _bundle(tmp_path, "9010", [_guard("2026-01-01T01:00:00+00:00", "all clear")], [])
    assert mine_shift(str(path)) == []
