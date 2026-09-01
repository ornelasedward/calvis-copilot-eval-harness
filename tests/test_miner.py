"""Stage 1 signal extractors and Stage 3 catalog matching for `cx mine`."""

from __future__ import annotations

import json
from pathlib import Path

from harness.miner import (
    ThreadEvent,
    ThreadView,
    draft_proposal,
    extract_signals,
    gap_analysis,
    match_mode_to_cards,
    modes_from_signals,
    run_mine,
    signal_duplicate_photos,
    signal_guard_sentiment,
    signal_repeated_asks,
    signal_silent_guard,
    signal_tool_call_gaps,
    signal_unanswered_ladder,
    sweep_threads,
    write_proposal,
)
from harness.recipes import list_cards, miner_defaults

BUNDLE = Path(__file__).resolve().parents[1]


def _ev(ts, role, text="", image=None, tool=None, trigger=None, turn=None) -> ThreadEvent:
    return ThreadEvent(
        ts=ts,
        role=role,
        text=text,
        image=image,
        tool=tool,
        trigger=trigger,
        turn=turn,
    )


def test_silent_guard_extractor_on_canned_thread():
    thread = ThreadView(
        thread_id="t-silent",
        source="shift",
        shift_id="55252",
        events=[
            _ev("2026-08-20T13:00:00+00:00", "copilot", "Hey, you're on today.", trigger="session_start", turn=1),
            _ev("2026-08-20T15:00:00+00:00", "copilot", "Need you to check in.", trigger="scheduled_check_in", turn=2),
            _ev("2026-08-20T17:00:00+00:00", "tool", tool="mcp__calvis__escalate_to_ops"),
            _ev("2026-08-20T17:00:01+00:00", "copilot", "Hector, check in now.", trigger="scheduled_check_in", turn=4),
        ],
        shift_end="2026-08-20T23:00:00+00:00",
    )
    hit = signal_silent_guard(thread)
    assert hit.flagged
    assert hit.count >= 2
    ladder = signal_unanswered_ladder(thread)
    assert ladder.flagged


def test_duplicate_photo_extractor_on_canned_thread():
    url = "https://cdn.example/photo/abc.jpg"
    thread = ThreadView(
        thread_id="t-dup",
        source="shift",
        shift_id="46116",
        events=[
            _ev("2026-07-01T04:00:00+00:00", "guard", "", image=url),
            _ev("2026-07-01T04:01:00+00:00", "copilot", "Got the photo."),
            _ev("2026-07-01T04:02:00+00:00", "guard", "", image=url),
            _ev("2026-07-01T04:03:00+00:00", "guard", "", image=url),
        ],
    )
    hit = signal_duplicate_photos(thread)
    assert hit.flagged
    assert any("abc.jpg" in e or "streak" in e for e in hit.evidence + hit.excerpts)


def test_repeated_asks_extractor_on_canned_thread():
    thread = ThreadView(
        thread_id="t-nag",
        source="shift",
        shift_id="56212",
        events=[
            _ev("2026-07-29T16:00:00+00:00", "copilot", "All-clear logged, thanks. Next patrol at 9:15, let me know if anything changes."),
            _ev("2026-07-29T17:00:00+00:00", "copilot", "All-clear logged, thanks. Next patrol at 10:14, let me know if anything changes."),
            _ev("2026-07-29T18:00:00+00:00", "copilot", "All-clear logged, thanks. Next patrol at 11:25, let me know if anything changes."),
        ],
    )
    hit = signal_repeated_asks(thread)
    assert hit.flagged
    assert hit.count >= 1


def test_tool_call_gap_when_image_never_fetched():
    thread = ThreadView(
        thread_id="t-gap",
        source="run",
        shift_id="x",
        events=[
            _ev("2026-07-01T04:00:00+00:00", "guard", "see this", image="https://cdn.example/p.jpg"),
            _ev("2026-07-01T04:01:00+00:00", "copilot", "Looks clear from here."),
        ],
    )
    hit = signal_tool_call_gaps(thread)
    assert hit.flagged
    assert hit.count == 1


def test_tool_call_gap_clears_when_fetched():
    thread = ThreadView(
        thread_id="t-fetch",
        source="shift",
        shift_id="x",
        events=[
            _ev("2026-07-01T04:00:00+00:00", "guard", "", image="[photo]"),
            _ev("2026-07-01T04:00:05+00:00", "tool", tool="mcp__calvis__fetch_chat_image"),
            _ev("2026-07-01T04:00:10+00:00", "copilot", "Got the photo."),
        ],
    )
    hit = signal_tool_call_gaps(thread)
    assert not hit.flagged


def test_guard_sentiment_pushback_lexicon():
    thread = ThreadView(
        thread_id="t-host",
        source="shift",
        shift_id="50737",
        events=[
            _ev(
                "2026-08-04T03:00:00+00:00",
                "guard",
                "Whatever I'm tired of this every night. you guys just sit behind desks and harass me.",
            )
        ],
    )
    hit = signal_guard_sentiment(thread)
    assert hit.flagged
    assert hit.count >= 1


def test_threat_verdict_ignores_when_youre_done():
    from harness.miner import signal_threat_verdict

    benign = ThreadView(
        thread_id="t-done",
        source="shift",
        shift_id="x",
        events=[
            _ev("t1", "copilot", "Head back when you're done."),
            _ev("t2", "copilot", "Need you to clock out when you're done. Holler back."),
        ],
    )
    assert not signal_threat_verdict(benign).flagged

    real = ThreadView(
        thread_id="t-warn",
        source="shift",
        shift_id="x",
        events=[_ev("t1", "copilot", "This is your final warning. I'll report you.")],
    )
    assert signal_threat_verdict(real).flagged


def test_unanswered_ladder_resets_on_photo_reply():
    thread = ThreadView(
        thread_id="t-photo-reply",
        source="shift",
        shift_id="x",
        events=[
            _ev("t1", "copilot", "Status?"),
            _ev("t2", "guard", "", image="[photo]"),
            _ev("t3", "copilot", "Got it. Status again?"),
        ],
    )
    assert not signal_unanswered_ladder(thread).flagged


def test_extract_signals_returns_all_named_keys():
    thread = ThreadView(thread_id="empty", source="shift", shift_id="0", events=[])
    keys = set(extract_signals(thread))
    assert "silent_guard" in keys
    assert "duplicate_photo" in keys
    assert "repeated_asks" in keys
    assert "tool_call_gap" in keys


def test_stage3_matching_existing_card_is_covered():
    cards = [
        {
            "recipe": "a3-shift-55252",
            "intent": "silent-guard escalation ladder is followed without missed flags",
            "covers": ["silent guard", "escalation ladder", "unanswered check-in"],
            "risk_class": "safety",
            "required_when": ["scheduled_check_in.md"],
        }
    ]
    mode = {
        "name": "silent guard",
        "description": "Guard never replies while the copilot escalates.",
        "thread_ids": ["shift:55252"],
        "quotes": ["Need you to check in."],
    }
    hit = match_mode_to_cards(mode, cards)
    assert hit is not None
    assert hit["recipe"] == "a3-shift-55252"
    covered, uncovered = gap_analysis([mode], cards)
    assert covered and covered[0]["name"] == "silent guard"
    assert uncovered == []


def test_stage3_uncovered_mode_does_not_match():
    cards = [
        {
            "recipe": "smoke-welcome",
            "intent": "session-start welcome DM is sent",
            "covers": ["welcome", "session start"],
            "risk_class": "smoke",
            "required_when": [],
        }
    ]
    mode = {
        "name": "duplicate photo",
        "description": "Guard resent the same image URL.",
        "thread_ids": ["shift:46116"],
        "quotes": [],
    }
    covered, uncovered = gap_analysis([mode], cards)
    assert uncovered and uncovered[0]["name"] == "duplicate photo"
    assert covered == []


def test_catalog_cards_include_covers():
    cards = {c["recipe"]: c for c in list_cards()}
    covers_esc = " ".join(cards["a3-shift-55252"]["covers"]).replace("_", " ")
    assert "silent guard" in covers_esc or "escalation ladder" in covers_esc
    assert "nagging" in cards["a3-quiet-probe"]["covers"]
    cfg = miner_defaults()
    assert cfg["model"] != cfg["copilot_model"]


def test_miner_never_writes_recipes_or_fixtures(tmp_path):
    recipes = BUNDLE / "experiments" / "recipes.json"
    before = recipes.read_bytes()
    fixtures = BUNDLE / "experiments" / "fixtures"
    fixture_before = (
        {p.name: p.read_bytes() for p in fixtures.glob("*")} if fixtures.exists() else {}
    )
    out = tmp_path / "mine-out"
    props = tmp_path / "proposals"
    result = run_mine(
        root=BUNDLE,
        dry=True,
        limit=2,
        write_proposals=True,
        out_dir=out,
        recipes_path=recipes,
        proposals_dir=props,
    )
    assert recipes.read_bytes() == before
    if fixtures.exists():
        assert {p.name: p.read_bytes() for p in fixtures.glob("*")} == fixture_before
    assert (out / "signals.json").exists()
    names = {m["name"].lower() for m in result["modes"]}
    assert any("silent" in n and "guard" in n for n in names)
    assert any("duplicate" in n and "photo" in n for n in names)
    assert any("nag" in n for n in names)
    # Dry gap report uses every flagged thread; --limit only truncates the LLM batch.
    assert result["dropped"], "limit=2 should drop some flagged threads from the LLM batch"
    covered_names = {c["name"].lower() for c in result["covered"]}
    assert any("silent" in n for n in covered_names)
    # uncovered drafts land only under the proposals dir we passed
    for p in props.glob("*.json"):
        assert p.parent == props
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert "proposed_card" in payload
        assert "scripted_guard_flow" in payload


def test_limit_logs_dropped_threads(tmp_path):
    flagged = []
    for i in range(5):
        flagged.append(
            {
                "thread_id": f"shift:{i}",
                "source": "shift",
                "shift_id": str(i),
                "run_id": None,
                "flagged": True,
                "flag_reasons": ["silent_guard"],
                "severity": i,
                "signals": {},
                "excerpts": ["x"],
            }
        )
    from harness.miner import _select_for_llm

    selected, dropped = _select_for_llm(flagged, 2)
    assert len(selected) == 2
    assert len(dropped) == 3
    # Diversity pass keeps a low-severity unique-reason thread if needed; here
    # every row shares silent_guard so fill is severity order (4, then 3).
    assert {s["thread_id"] for s in selected} == {"shift:4", "shift:3"}
    assert {d["thread_id"] for d in dropped} == {"shift:0", "shift:1", "shift:2"}


def test_limit_keeps_one_thread_per_flag_reason():
    from harness.miner import _select_for_llm

    rows = [
        {"thread_id": "a", "flag_reasons": ["duplicate_photo"], "severity": 100},
        {"thread_id": "b", "flag_reasons": ["duplicate_photo"], "severity": 90},
        {"thread_id": "c", "flag_reasons": ["silent_guard"], "severity": 1},
    ]
    selected, dropped = _select_for_llm(rows, 2)
    ids = {s["thread_id"] for s in selected}
    assert ids == {"a", "c"}
    assert {d["thread_id"] for d in dropped} == {"b"}


def test_write_proposal_refuses_catalog_paths(tmp_path):
    mode = {
        "name": "duplicate photo",
        "description": "Guard resent the same image URL.",
        "thread_ids": ["shift:46116"],
        "quotes": ["see this"],
    }
    path = write_proposal(draft_proposal(mode), tmp_path)
    assert path.parent == tmp_path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["proposed_card"]["covers"]
    assert "promote" in data["notes"].lower()


def test_modes_from_signals_maps_known_names():
    rows = sweep_threads(
        [
            ThreadView(
                thread_id="shift:55252",
                source="shift",
                shift_id="55252",
                events=[
                    _ev("t1", "copilot", "Hey.", trigger="session_start", turn=1),
                    _ev("t2", "copilot", "Check in?", trigger="scheduled_check_in", turn=2),
                ],
            )
        ]
    )
    modes = modes_from_signals([r for r in rows if r["flagged"]])
    assert any(m["name"] == "silent guard" for m in modes)
