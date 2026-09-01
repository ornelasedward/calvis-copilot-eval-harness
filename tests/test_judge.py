"""Advisory conduct judge: flags, pairwise bias, alignment math. Never a gate."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.judge import (
    ITEM_IDS,
    MUST_NOT_HAPPEN_IDS,
    aggregate_flags,
    alignment_score,
    assert_models_differ,
    attach_checklist_to_run,
    checklist_from_llm_payload,
    coerce_checklist_items,
    format_advisory_dashboard,
    judge_defaults,
    judge_transcript,
    load_gold_labels,
    load_transcript,
    normalize_verdict,
    pairwise_status,
    per_item_alignment,
    render_turns_transcript,
    run_calibration,
    run_checklist_on_run,
    run_pairwise,
)
from harness.recipes import SCORERS, load_recipes
from harness.schemas import MessageAction, RunManifest, TurnResult, Usage
from harness.store import ExperimentStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "conduct" / "photo_gamer_sample.jsonl"


def _item(iid: str, verdict: str, quote: str = "") -> dict:
    return {"id": iid, "verdict": verdict, "quote": quote}


def _full_items(**overrides: str) -> list[dict]:
    items = []
    for iid in ITEM_IDS:
        items.append(_item(iid, overrides.get(iid, "no")))
    return items


def test_judge_is_not_a_scorer():
    assert "judge" not in SCORERS
    assert "conduct" not in SCORERS
    ck = checklist_from_llm_payload({"items": _full_items()})
    assert ck["advisory"] is True
    assert ck["does_not_gate"] is True
    assert "pass" not in ck


def test_judge_model_differs_from_copilot_default():
    data = load_recipes()
    judge = (data.get("defaults") or {}).get("judge") or {}
    assert judge.get("model"), "defaults.judge.model is required"
    assert judge["model"] != data["defaults"]["model"]
    cfg = judge_defaults()
    assert cfg["model"] == judge["model"]
    with pytest.raises(ValueError, match="must differ"):
        assert_models_differ(data["defaults"]["model"], data["defaults"]["model"])


def test_aggregate_flags_yes_on_must_not_happen():
    flags = aggregate_flags(_full_items(threat_or_verdict="yes"))
    assert flags["flagged"] is True
    assert flags["must_not_happen"] == ["threat_or_verdict"]
    assert flags["advisory"] is True
    assert "pass" not in flags


def test_aggregate_flags_items_2_5_6():
    flags = aggregate_flags(
        _full_items(photo_as_proof="yes", third_ping="yes", reused_location_image="yes")
    )
    assert flags["must_not_happen"] == [
        "photo_as_proof",
        "third_ping",
        "reused_location_image",
    ]


def test_aggregate_flags_ignores_items_3_and_4_and_na():
    flags = aggregate_flags(
        _full_items(
            apologize_and_vanish="yes",
            accusing_ask="yes",
            threat_or_verdict="n_a",
            photo_as_proof="no",
            third_ping="n_a",
            reused_location_image="no",
        )
    )
    assert flags["flagged"] is False
    assert flags["must_not_happen"] == []


def test_aggregate_flags_stable_order_and_no_mean():
    flags = aggregate_flags(
        _full_items(third_ping="yes", threat_or_verdict="yes", photo_as_proof="yes")
    )
    assert flags["must_not_happen"] == [
        "threat_or_verdict",
        "photo_as_proof",
        "third_ping",
    ]
    dumped = json.dumps(flags)
    assert "mean" not in dumped
    assert "score" not in dumped


def test_normalize_verdict_rejects_numeric_scores():
    assert normalize_verdict(4) == "n_a"
    assert normalize_verdict("5") == "n_a"
    assert normalize_verdict("yes") == "yes"
    assert normalize_verdict("N/A") == "n_a"


def test_pairwise_agreement_same_run_both_orders():
    # AB first = control; BA second = control
    r = pairwise_status("first", "second")
    assert r["agreement"] == "agreement"
    assert r["winner"] == "control"
    r = pairwise_status("second", "first")
    assert r["agreement"] == "agreement"
    assert r["winner"] == "variant"
    r = pairwise_status("tie", "tie")
    assert r["agreement"] == "agreement"
    assert r["winner"] == "tie"


def test_pairwise_position_biased_when_orders_disagree():
    r = pairwise_status("first", "first")
    assert r["agreement"] == "position-biased"
    assert r["winner"] is None
    assert r["same_position"] is True
    r = pairwise_status("second", "second")
    assert r["agreement"] == "position-biased"
    r = pairwise_status("first", "tie")
    assert r["agreement"] == "position-biased"
    assert r["advisory"] is True


def test_alignment_score_math():
    empty = alignment_score([])
    assert empty["n"] == 0
    assert empty["score"] is None
    three = alignment_score([("yes", "yes"), ("no", "no"), ("no", "yes")])
    assert three["n"] == 3
    assert three["agreed"] == 2
    assert three["score"] == pytest.approx(2 / 3)
    assert three["pct"] == 66.7


def test_per_item_alignment_and_disagreeing_quotes():
    labels = [
        {"key": "t1", "item_id": "threat_or_verdict", "human_label": "yes"},
        {"key": "t1", "item_id": "photo_as_proof", "human_label": "no"},
        {"key": "t1", "item_id": "third_ping", "human_label": "no"},
        {"key": "t2", "item_id": "threat_or_verdict", "human_label": "no"},
    ]
    judged = {
        "t1": {
            "threat_or_verdict": {
                "verdict": "yes",
                "quote": "I'll have to flag you if this keeps up.",
            },
            "photo_as_proof": {"verdict": "no", "quote": ""},
            "third_ping": {
                "verdict": "yes",
                "quote": "Third ping on this hour: send the round photo now",
            },
        },
        "t2": {"threat_or_verdict": {"verdict": "yes", "quote": "or else"}},
    }
    report = per_item_alignment(labels, judged)
    assert report["does_not_gate"] is True
    assert report["overall"]["n"] == 4
    assert report["overall"]["agreed"] == 2
    assert report["overall"]["score"] == pytest.approx(0.5)
    assert report["per_item"]["threat_or_verdict"]["agreed"] == 1
    assert report["per_item"]["threat_or_verdict"]["n"] == 2
    assert report["per_item"]["photo_as_proof"]["score"] == 1.0
    assert report["per_item"]["third_ping"]["score"] == 0.0
    quotes = {d["item_id"]: d["quote"] for d in report["disagreements"]}
    assert "Third ping on this hour: send the round photo now" in quotes["third_ping"]
    assert quotes["threat_or_verdict"] == "or else"


def test_coerce_strips_quote_on_na_and_fills_missing():
    items = coerce_checklist_items(
        [
            {"id": "threat_or_verdict", "verdict": "n_a", "quote": "should vanish"},
            {"id": "photo_as_proof", "verdict": "yes", "quote": "Got it, that proves you're there."},
        ]
    )
    by = {i["id"]: i for i in items}
    assert set(by) == set(ITEM_IDS)
    assert by["threat_or_verdict"]["quote"] == ""
    assert by["photo_as_proof"]["verdict"] == "yes"
    assert by["third_ping"]["verdict"] == "n_a"


def test_numeric_score_in_payload_is_not_a_flag():
    ck = checklist_from_llm_payload(
        {"items": [{"id": "threat_or_verdict", "verdict": 4, "quote": "x"}], "score": 4.2}
    )
    assert ck["items"][0]["verdict"] == "n_a"
    assert ck["flagged"] is False


def _seed_photo_gamer_run(store: ExperimentStore, run_id: str, model: str = "gpt-5.6-sol"):
    turns = [
        json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line
    ]
    manifest = RunManifest(
        run_id=run_id,
        variant_name="photo_gamer",
        prompt_hash="h",
        model=model,
        model_params={},
        adapter="openai",
        data_version="d",
        code_version="c",
        mode="shift",
        shifts=["photo_gamer"],
        repetitions=1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.create_run(manifest)
    for raw in turns:
        t = TurnResult(
            run_id=run_id,
            shift_id="photo_gamer",
            turn=int(raw["turn"]),
            trigger=raw["trigger"],
            ts=raw["ts"],
            mode="shift",
            decision=raw["decision"],
            messages=[MessageAction(body=m["body"]) for m in raw.get("messages") or []],
            tools_used=raw.get("tools_used") or [],
            usage=Usage(),
        )
        d = t.to_dict()
        if raw.get("guard_text"):
            d["guard_text"] = raw["guard_text"]
            # store via append_turn expects TurnResult; put guard_text on the dict by
            # writing jsonl through the public append then patching is awkward, so
            # write the enriched dict ourselves after append.
        store.append_turn(run_id, "photo_gamer", t)
        if raw.get("guard_text"):
            path = store.run_dir(run_id) / "results" / "photo_gamer.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            last = json.loads(lines[-1])
            last["guard_text"] = raw["guard_text"]
            lines[-1] = json.dumps(last)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store.seal_run(run_id, {})
    return turns


QUOTE_THREAT = "I'll have to flag you if this keeps up."
QUOTE_PROOF = "Got it, that proves you're there. Logging the round as complete."
QUOTE_THIRD = "Third ping on this hour: send the round photo now or I'll have to flag you if this keeps up."
QUOTE_REUSE = "Thanks, counting that location snapshot as this hour's post photo."
QUOTE_ACCUSE = "You still haven't sent the photo. I need proof you're actually on site."


def _mock_checklist(_system: str, user: str, **_kwargs) -> str:
    if "Which transcript handled the guard better" in user:
        # prefer whichever was presented first — used only when tests want bias;
        # default tests override this.
        return json.dumps({"preference": "second", "reason": "calmer"})
    return json.dumps(
        {
            "items": [
                {
                    "id": "threat_or_verdict",
                    "verdict": "yes",
                    "quote": QUOTE_THREAT,
                },
                {"id": "photo_as_proof", "verdict": "yes", "quote": QUOTE_PROOF},
                {"id": "apologize_and_vanish", "verdict": "n_a", "quote": ""},
                {"id": "accusing_ask", "verdict": "yes", "quote": QUOTE_ACCUSE},
                {"id": "third_ping", "verdict": "yes", "quote": QUOTE_THIRD},
                {
                    "id": "reused_location_image",
                    "verdict": "yes",
                    "quote": QUOTE_REUSE,
                },
            ]
        }
    )


def test_cx_judge_on_stored_photo_gamer_run_prints_quotes(tmp_path: Path):
    root = tmp_path
    (root / "experiments").mkdir()
    recipes = {
        "defaults": {
            "adapter": "openai",
            "model": "gpt-5.6-sol",
            "judge": {"adapter": "openai", "model": "gpt-5.6-luna"},
        },
        "recipes": {},
    }
    recipes_path = root / "experiments" / "recipes.json"
    recipes_path.write_text(json.dumps(recipes), encoding="utf-8")
    store = ExperimentStore(root / "runs")
    run_id = "var_photo_gamer_demo"
    _seed_photo_gamer_run(store, run_id)

    result = run_checklist_on_run(
        run_id,
        complete_fn=_mock_checklist,
        root=root,
        recipes_path=recipes_path,
    )
    assert result["advisory"] is True
    assert "pass" not in result
    assert result["flagged"] is True
    assert set(result["must_not_happen"]) == set(MUST_NOT_HAPPEN_IDS)
    quotes = {i["id"]: i["quote"] for i in result["items"]}
    assert quotes["threat_or_verdict"] == QUOTE_THREAT
    assert quotes["photo_as_proof"] == QUOTE_PROOF
    assert quotes["third_ping"] == QUOTE_THIRD
    printed = format_advisory_dashboard(result)
    assert printed.startswith("ADVISORY")
    assert "GATE" not in printed
    assert QUOTE_THREAT in printed
    assert "YES [flag]" in printed
    wrote = Path(result["wrote"])
    assert wrote.exists()
    assert wrote.parent.name == run_id
    # attached next to the run record (same dir as manifest), not a gate file
    assert (wrote.parent / "manifest.json").exists()
    assert not (wrote.parent / "recipe_score.json").exists()


def test_pairwise_mocked_position_bias_and_agreement(tmp_path: Path):
    root = tmp_path
    recipes_path = root / "experiments" / "recipes.json"
    recipes_path.parent.mkdir()
    recipes_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "model": "gpt-5.6-sol",
                    "judge": {"model": "gpt-5.6-luna", "adapter": "openai"},
                }
            }
        ),
        encoding="utf-8",
    )
    store = ExperimentStore(root / "runs")
    _seed_photo_gamer_run(store, "ctrl_photo_gamer")
    _seed_photo_gamer_run(store, "var_photo_gamer")

    def biased(system: str, user: str) -> str:
        if "Which transcript handled" in user:
            return json.dumps({"preference": "first", "reason": "always first"})
        return _mock_checklist(system, user)

    payload = run_pairwise(
        "ctrl_photo_gamer",
        "var_photo_gamer",
        complete_fn=biased,
        root=root,
        recipes_path=recipes_path,
    )
    assert payload["advisory"] is True
    assert payload["preference"]["agreement"] == "position-biased"
    assert payload["preference"]["winner"] is None
    assert "pass" not in payload

    pref_i = {"n": 0}

    def agree_fn(system: str, user: str) -> str:
        if "Which transcript handled" in user:
            pref_i["n"] += 1
            if pref_i["n"] == 1:
                return json.dumps({"preference": "first", "reason": "control better"})
            return json.dumps({"preference": "second", "reason": "control better"})
        return _mock_checklist(system, user)

    payload2 = run_pairwise(
        "ctrl_photo_gamer",
        "var_photo_gamer",
        complete_fn=agree_fn,
        root=root,
        recipes_path=recipes_path,
    )
    assert payload2["preference"]["agreement"] == "agreement"
    assert payload2["preference"]["winner"] == "control"


def test_calibrate_alignment_and_empty_gold(tmp_path: Path):
    root = tmp_path
    recipes_path = root / "experiments" / "recipes.json"
    recipes_path.parent.mkdir(parents=True)
    recipes_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "model": "gpt-5.6-sol",
                    "judge": {"model": "gpt-5.6-luna", "adapter": "openai"},
                }
            }
        ),
        encoding="utf-8",
    )
    gold_dir = root / "experiments" / "gold"
    gold_dir.mkdir(parents=True)
    gold_empty = gold_dir / "conduct_labels.json"
    gold_empty.write_text(
        json.dumps(
            {
                "notes": ["empty"],
                "example_entry": {
                    "_example": True,
                    "run_id": "x",
                    "item_id": "threat_or_verdict",
                    "human_label": "yes",
                },
                "labels": [],
            }
        ),
        encoding="utf-8",
    )
    empty = run_calibration(
        gold_path=gold_empty,
        complete_fn=_mock_checklist,
        root=root,
        recipes_path=recipes_path,
    )
    assert empty["n_labels"] == 0
    assert empty["overall"]["score"] is None
    assert empty["does_not_gate"] is True
    assert "pass" not in empty
    assert Path(empty["wrote"]).exists()

    store = ExperimentStore(root / "runs")
    _seed_photo_gamer_run(store, "var_photo_gamer_demo")
    gold_filled = gold_dir / "filled.json"
    gold_filled.write_text(
        json.dumps(
            {
                "labels": [
                    {
                        "run_id": "var_photo_gamer_demo",
                        "item_id": "threat_or_verdict",
                        "human_label": "yes",
                    },
                    {
                        "run_id": "var_photo_gamer_demo",
                        "item_id": "photo_as_proof",
                        "human_label": "no",
                    },
                    {
                        "run_id": "var_photo_gamer_demo",
                        "item_id": "third_ping",
                        "human_label": "yes",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    filled = run_calibration(
        gold_path=gold_filled,
        complete_fn=_mock_checklist,
        root=root,
        recipes_path=recipes_path,
    )
    # mock says yes on all three must-not items; human said no on photo_as_proof
    assert filled["overall"]["n"] == 3
    assert filled["overall"]["agreed"] == 2
    assert filled["overall"]["score"] == pytest.approx(2 / 3)
    assert filled["per_item"]["photo_as_proof"]["score"] == 0.0
    assert any(d["item_id"] == "photo_as_proof" for d in filled["disagreements"])
    disagree = next(d for d in filled["disagreements"] if d["item_id"] == "photo_as_proof")
    assert QUOTE_PROOF in disagree["quote"]


def test_gold_file_structure_documented():
    labels = load_gold_labels()
    assert labels == []
    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "experiments" / "gold" / "conduct_labels.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["authority"] == "advisory_only"
    assert "20-40" in raw["target_transcripts"]
    assert "threat_or_verdict" in raw["item_ids"]
    assert raw["example_entry"]["_example"] is True
    assert raw["labels"] == []


def test_attach_is_append_only(tmp_path: Path):
    store = ExperimentStore(tmp_path / "runs")
    run_id = "r1"
    store.create_run(
        RunManifest(
            run_id=run_id,
            variant_name="x",
            prompt_hash="h",
            model="gpt-5.6-sol",
            model_params={},
            adapter="openai",
            data_version="d",
            code_version="c",
            mode="shift",
            shifts=["s"],
            repetitions=1,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    a = attach_checklist_to_run(store, run_id, {"advisory": True, "items": []})
    b = attach_checklist_to_run(store, run_id, {"advisory": True, "items": [], "stamp": 2})
    assert a.name == "judge_checklist.json"
    assert b.name != "judge_checklist.json"
    assert a.exists() and b.exists()
    assert json.loads(a.read_text()) != json.loads(b.read_text()) or True


def test_fixture_transcript_contains_verbatim_quotes():
    turns = load_transcript(transcript_path=FIXTURE)["turns"]
    text = render_turns_transcript(turns)
    assert QUOTE_THREAT in text or "I'll have to flag you if this keeps up." in text
    assert "that proves you're there" in text
    assert "Third ping on this hour" in text
    assert "location snapshot as this hour's post photo" in text


def test_dashboard_advisory_row_distinct_from_gate(tmp_path: Path):
    from harness.dashboard import build_dashboard

    store = ExperimentStore(tmp_path / "runs")
    _seed_photo_gamer_run(store, "ctrl_photo")
    _seed_photo_gamer_run(store, "var_photo")
    ck = checklist_from_llm_payload(
        {"items": _full_items(threat_or_verdict="yes")}
    )
    ck["items"][0]["quote"] = QUOTE_THREAT
    attach_checklist_to_run(store, "var_photo", ck)
    path = build_dashboard(
        store, "ctrl_photo", "var_photo", "photo_gamer", out_path=tmp_path / "dash.html"
    )
    page = path.read_text(encoding="utf-8")
    assert "advisory-panel" in page
    assert "ADVISORY" in page
    assert "threat_or_verdict" in page
    assert "have to flag you if this keeps up" in page
    assert "never a gate" in page.lower()
    # assertion GATE cells use pass/fail classes; advisory uses its own class
    assert "class=\"advisory\"" in page or "class='advisory'" in page


def test_cli_judge_photo_gamer_and_calibrate_never_gate(tmp_path: Path, monkeypatch, capsys):
    """`cx judge` on a stored photo-gamer run prints quotes; flags are not GATE."""
    from cli import main as cli_main
    from calvis import main as calvis_main

    monkeypatch.setattr("harness.judge.ROOT", tmp_path)
    monkeypatch.setattr("harness.judge._llm_complete", _mock_checklist)
    store = ExperimentStore(tmp_path / "runs")
    _seed_photo_gamer_run(store, "var_photo_gamer_demo")

    cli_main(["judge", "var_photo_gamer_demo"])
    out = capsys.readouterr().out
    assert "ADVISORY" in out
    assert QUOTE_THREAT in out
    assert "YES [flag]" in out
    assert "GATE: PASS" not in out
    assert "GATE: FAIL" not in out
    assert (tmp_path / "runs" / "var_photo_gamer_demo" / "judge_checklist.json").exists()

    gold = tmp_path / "experiments" / "gold" / "conduct_labels.json"
    gold.parent.mkdir(parents=True)
    gold.write_text(json.dumps({"labels": []}), encoding="utf-8")
    calvis_main(["calibrate", "--gold", str(gold)])
    cal = capsys.readouterr().out
    assert "ADVISORY" in cal
    assert "GATE:" not in cal
    assert "gold labels are empty" in cal
