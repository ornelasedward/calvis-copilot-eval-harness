"""Tests for the Variant C voice-compliance scorer.

The scorer must own PASS/FAIL deterministically: count comms_policy voice
violations (em-dashes, sign-off filler, multi-DM turns), require the variant to
fully comply, preserve guard replies, and not drift escalations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.recipes import _score_voice, _voice_violations
from harness.schemas import EscalationAction, MessageAction, TurnResult, Usage
from harness.store import ExperimentStore, new_run_id
from harness.schemas import RunManifest


def _turn(shift_id, turn, trigger, bodies, escalations=0):
    return TurnResult(
        run_id="r",
        shift_id=shift_id,
        turn=turn,
        trigger=trigger,
        ts=datetime.now(timezone.utc).isoformat(),
        mode="turn",
        decision="send_message" if bodies else "no_op",
        messages=[MessageAction(body=b) for b in bodies],
        escalations=[
            EscalationAction(kind="flag", details="x") for _ in range(escalations)
        ],
        usage=Usage(),
    )


def test_voice_violations_counts_emdash_filler_and_multidm():
    turns = [
        _turn("s", 1, "session_start", ["Hey, welcome to the site\u2014glad you're here."]),
        _turn("s", 2, "guard_message", ["All clear. let me know if you need anything."]),
        _turn("s", 3, "guard_message", ["Copy that.", "Second buzz in same turn."]),
    ]
    v = _voice_violations([t.to_dict() for t in turns])
    assert v["emdash"] == 1
    assert v["filler"] == 1
    assert v["multi_dm_turns"] == 1
    assert v["total_violations"] == 3
    assert v["reply_rate"] == 1.0


def test_voice_violations_clean_run_has_zero():
    turns = [
        _turn("s", 1, "session_start", ["Hey, welcome. Walk the rear yard first."]),
        _turn("s", 2, "guard_message", ["All-clear, nice. Got it logged."]),
    ]
    v = _voice_violations([t.to_dict() for t in turns])
    assert v["total_violations"] == 0
    assert v["reply_rate"] == 1.0


def _seed_run(store: ExperimentStore, run_id, turns):
    manifest = RunManifest(
        run_id=run_id,
        variant_name="x",
        prompt_hash="h",
        model="m",
        model_params={},
        adapter="replay",
        data_version="d",
        code_version="c",
        mode="turn",
        shifts=["s"],
        repetitions=1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.create_run(manifest)
    for t in turns:
        store.append_turn(run_id, "s", t)
    store.seal_run(run_id, {})


def test_score_voice_passes_when_variant_complies(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    ctrl = "ctrl_" + new_run_id()
    var = "var_" + new_run_id()
    _seed_run(store, ctrl, [
        _turn("s", 1, "session_start", ["Welcome\u2014glad you're here. let me know if stuck."]),
        _turn("s", 2, "guard_message", ["All clear, copy."]),
    ])
    _seed_run(store, var, [
        _turn("s", 1, "session_start", ["Welcome, glad you're here. Shoot me an all-clear after your first loop."]),
        _turn("s", 2, "guard_message", ["All clear, copy."]),
    ])
    recipe = {"jobs": [{"shift": "s"}]}
    result = _score_voice(store, ctrl, var, recipe)
    assert result["pass"] is True
    assert result["variant"]["total_violations"] == 0
    assert result["control"]["total_violations"] == 2  # emdash + filler
    assert result["violation_lift"] == 2


def test_score_voice_passes_no_regression_when_both_clean(tmp_path: Path):
    # Sol-like case: strong same-model control already complies, so lift is 0
    # but the guardrail gate still passes (no regression, nothing dropped).
    store = ExperimentStore(tmp_path / "runs")
    ctrl = "ctrl_" + new_run_id()
    var = "var_" + new_run_id()
    clean = [
        _turn("s", 1, "session_start", ["Hey, welcome. Walk the rear yard first."]),
        _turn("s", 2, "guard_message", ["All-clear, nice. Got it logged."]),
    ]
    _seed_run(store, ctrl, clean)
    _seed_run(store, var, clean)
    recipe = {"jobs": [{"shift": "s"}]}
    result = _score_voice(store, ctrl, var, recipe)
    assert result["pass"] is True
    assert result["violation_lift"] == 0
    assert result["full_compliance"] is True


def test_score_voice_fails_when_variant_violates(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    ctrl = "ctrl_" + new_run_id()
    var = "var_" + new_run_id()
    _seed_run(store, ctrl, [_turn("s", 1, "session_start", ["Clean welcome."])])
    _seed_run(store, var, [_turn("s", 1, "session_start", ["Welcome\u2014still dashing."])])
    recipe = {"jobs": [{"shift": "s"}]}
    result = _score_voice(store, ctrl, var, recipe)
    assert result["pass"] is False


def test_score_voice_fails_on_dropped_reply(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    ctrl = "ctrl_" + new_run_id()
    var = "var_" + new_run_id()
    _seed_run(store, ctrl, [_turn("s", 2, "guard_message", ["Reply here."])])
    _seed_run(store, var, [_turn("s", 2, "guard_message", [])])  # no reply
    recipe = {"jobs": [{"shift": "s"}]}
    result = _score_voice(store, ctrl, var, recipe)
    assert result["pass"] is False
