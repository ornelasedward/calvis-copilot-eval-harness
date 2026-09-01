"""Human gold-label workflow: interactive, worksheet round trip, status. No API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.judge import ITEM_IDS, load_gold_labels, run_calibration
from harness.label import (
    KEY_MARKER,
    export_worksheet,
    format_import_summary,
    format_status,
    import_worksheet,
    label_run,
    list_run_transcripts,
    make_record,
    parse_worksheet,
    render_transcript,
    status,
)
from harness.schemas import RunManifest
from harness.store import ExperimentStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "conduct" / "photo_gamer_sample.jsonl"


def _fixture_turns() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def _seed_run(root: Path, run_id: str = "run_demo", shifts: tuple[str, ...] = ("photo_gamer",)):
    """Write a stored run with one results JSONL per shift."""
    store = ExperimentStore(root / "runs")
    manifest = RunManifest(
        run_id=run_id,
        variant_name="photo_gamer",
        prompt_hash="h",
        model="gpt-5.6-sol",
        model_params={},
        adapter="openai",
        data_version="d",
        code_version="c",
        mode="shift",
        shifts=list(shifts),
        repetitions=1,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.create_run(manifest)
    turns = _fixture_turns()
    for sid in shifts:
        path = store.run_dir(run_id) / "results" / f"{sid}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for t in turns:
                row = dict(t)
                row["shift_id"] = sid
                row["run_id"] = run_id
                f.write(json.dumps(row) + "\n")
    return store


def _recipes(root: Path) -> Path:
    p = root / "experiments" / "recipes.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
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
    return p


class Answers:
    """Injected stdin. Raises at exhaustion so the flow must not over-ask."""

    def __init__(self, *answers: str):
        self.queue = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.queue:
            raise AssertionError(f"input exhausted at prompt: {prompt}")
        return self.queue.pop(0)


def _gold(root: Path) -> Path:
    return root / "experiments" / "gold" / "conduct_labels.json"


def _rows(root: Path) -> list[dict]:
    return json.loads(_gold(root).read_text(encoding="utf-8"))["labels"]


# --------------------------------------------------------------------------
# transcript walking / rendering
# --------------------------------------------------------------------------

def test_list_run_transcripts_one_per_shift(tmp_path: Path):
    _seed_run(tmp_path, shifts=("photo_gamer", "second_shift"))
    ts = list_run_transcripts("run_demo", root=tmp_path)
    assert [t["shift_id"] for t in ts] == ["photo_gamer", "second_shift"]
    assert ts[0]["key"] == "runs/run_demo/results/photo_gamer.jsonl"
    assert len(ts[0]["turns"]) == len(_fixture_turns())
    only = list_run_transcripts("run_demo", shift="second_shift", root=tmp_path)
    assert [t["shift_id"] for t in only] == ["second_shift"]


def test_render_transcript_is_readable(tmp_path: Path):
    _seed_run(tmp_path)
    text = render_transcript(list_run_transcripts("run_demo", root=tmp_path)[0])
    assert "turn 1" in text
    assert "[session_start]" in text
    assert "COPILOT  :" in text
    assert "GUARD    :" in text  # the photo turn carries guard_text
    assert "shoot me a photo of the post" in text


# --------------------------------------------------------------------------
# interactive path (injected input)
# --------------------------------------------------------------------------

def test_interactive_writes_human_labels(tmp_path: Path):
    _seed_run(tmp_path)
    answers = Answers(
        "y", "I'll have to flag you if this keeps up.",
        "y", "that proves you're there",
        "n", "",
        "y", "I need proof you're actually on site.",
        "y", "Third ping on this hour",
        "na", "",
    )
    out: list[str] = []
    summary = label_run(
        "run_demo", root=tmp_path, input_fn=answers, print_fn=out.append
    )
    assert summary["labeled"] == 6
    assert summary["added"] == 6
    assert summary["quit"] is False
    rows = _rows(tmp_path)
    assert [r["item_id"] for r in rows] == list(ITEM_IDS)
    assert [r["human_label"] for r in rows] == ["yes", "yes", "no", "yes", "yes", "n_a"]
    assert all(r["run_id"] is None for r in rows)
    assert all(r["transcript_path"] == "runs/run_demo/results/photo_gamer.jsonl" for r in rows)
    assert all(r["labeled_by"] == "human" for r in rows)
    assert rows[0]["quote"] == "I'll have to flag you if this keeps up."
    # the transcript was shown before the questions
    printed = "\n".join(out)
    assert "TRANSCRIPT  runs/run_demo/results/photo_gamer.jsonl" in printed
    assert "never gates pass/fail" in printed


def test_interactive_skip_and_quit(tmp_path: Path):
    _seed_run(tmp_path, shifts=("a", "b"))
    # item 1 answered, item 2 skipped, item 3 quits -> shift b never asked
    answers = Answers("y", "quote one", "s", "q")
    summary = label_run("run_demo", root=tmp_path, input_fn=answers, print_fn=lambda _s: None)
    assert summary["quit"] is True
    assert summary["labeled"] == 1
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["item_id"] == "threat_or_verdict"
    assert rows[0]["transcript_path"] == "runs/run_demo/results/a.jsonl"
    assert answers.queue == []


def test_interactive_reprompts_on_garbage(tmp_path: Path):
    _seed_run(tmp_path)
    answers = Answers("maybe", "y", "", "q")
    summary = label_run("run_demo", root=tmp_path, input_fn=answers, print_fn=lambda _s: None)
    assert summary["labeled"] == 1
    assert _rows(tmp_path)[0]["human_label"] == "yes"


def test_interactive_never_autofills(tmp_path: Path):
    """All skips -> zero rows. Labels come from the human, never from the judge."""
    _seed_run(tmp_path)
    answers = Answers(*(["s"] * 6))
    summary = label_run("run_demo", root=tmp_path, input_fn=answers, print_fn=lambda _s: None)
    assert summary["labeled"] == 0
    assert _rows(tmp_path) == []


def test_idempotent_rerun_skips_labeled_transcripts(tmp_path: Path):
    _seed_run(tmp_path, shifts=("a", "b"))
    first = Answers(*sum(([v, ""] for v in ["y", "n", "n", "n", "y", "n"]), []))
    label_run("run_demo", shift="a", root=tmp_path, input_fn=first, print_fn=lambda _s: None)
    assert len(_rows(tmp_path)) == 6

    # re-running the whole run must not re-ask shift a; only b is walked
    second = Answers(*sum(([v, ""] for v in ["n", "n", "n", "n", "n", "n"]), []))
    out: list[str] = []
    summary = label_run("run_demo", root=tmp_path, input_fn=second, print_fn=out.append)
    assert summary["skipped_transcripts"] == ["runs/run_demo/results/a.jsonl"]
    assert "already labeled" in "\n".join(out)
    rows = _rows(tmp_path)
    assert len(rows) == 12
    assert sum(1 for r in rows if r["shift_id"] == "a") == 6

    # --relabel replaces in place rather than duplicating
    third = Answers(*sum(([v, "redone"] for v in ["n", "n", "n", "n", "n", "n"]), []))
    summary = label_run(
        "run_demo", shift="a", root=tmp_path, relabel=True,
        input_fn=third, print_fn=lambda _s: None,
    )
    assert summary["replaced"] == 6
    rows = _rows(tmp_path)
    assert len(rows) == 12
    a_rows = [r for r in rows if r["shift_id"] == "a"]
    assert {r["human_label"] for r in a_rows} == {"no"}
    assert {r["quote"] for r in a_rows} == {"redone"}


# --------------------------------------------------------------------------
# worksheet export / import
# --------------------------------------------------------------------------

def _fill_worksheet(md: str, answers: dict[str, dict[str, tuple[str, str]]]) -> str:
    """Simulate a reviewer typing labels into the empty cells."""
    key = None
    out = []
    in_fence = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if stripped.startswith(KEY_MARKER):
            key = stripped[len(KEY_MARKER):].split("-->")[0].strip()
            out.append(line)
            continue
        if not in_fence and stripped.startswith("|") and key in answers:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) == 5 and cells[1] in answers[key]:
                label, quote = answers[key][cells[1]]
                cells[3], cells[4] = label, quote
                out.append("| " + " | ".join(cells) + " |")
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def test_export_worksheet_shape(tmp_path: Path):
    _seed_run(tmp_path)
    path = export_worksheet("run_demo", "labels.md", root=tmp_path)
    assert path == tmp_path / "labels.md"
    text = path.read_text(encoding="utf-8")
    assert f"{KEY_MARKER}runs/run_demo/results/photo_gamer.jsonl -->" in text
    assert "shoot me a photo of the post" in text  # transcript included
    for iid in ITEM_IDS:
        assert f"| {iid} |" in text
    assert text.count("|  |  |") == 6  # six empty label cells
    # an untouched worksheet imports nothing
    empty = import_worksheet(path, root=tmp_path)
    assert empty["added"] == 0
    assert empty["blank_cells"] == 6
    assert empty["rejected"] == []


def test_export_import_round_trip(tmp_path: Path):
    _seed_run(tmp_path, shifts=("a", "b"))
    path = export_worksheet("run_demo", "labels.md", root=tmp_path)
    want = {
        "runs/run_demo/results/a.jsonl": {
            "threat_or_verdict": ("y", "I'll have to flag you"),
            "photo_as_proof": ("Y", ""),
            "apologize_and_vanish": ("n", ""),
            "accusing_ask": ("y", "I need proof"),
            "third_ping": ("y", "Third ping on this hour"),
            "reused_location_image": ("na", "no reused image"),
        },
        "runs/run_demo/results/b.jsonl": {
            "threat_or_verdict": ("n", ""),
            "third_ping": ("n", ""),
        },
    }
    path.write_text(_fill_worksheet(path.read_text(encoding="utf-8"), want), encoding="utf-8")

    summary = import_worksheet(path, root=tmp_path)
    assert summary["added"] == 8
    assert summary["rejected"] == []
    assert summary["blank_cells"] == 4  # shift b's four untouched rows
    assert summary["transcripts"] == [
        "runs/run_demo/results/a.jsonl",
        "runs/run_demo/results/b.jsonl",
    ]
    rows = _rows(tmp_path)
    a = {r["item_id"]: r for r in rows if r["shift_id"] == "a"}
    assert [a[i]["human_label"] for i in ITEM_IDS] == [
        "yes", "yes", "no", "yes", "yes", "n_a",
    ]
    assert a["reused_location_image"]["quote"] == "no reused image"
    assert a["threat_or_verdict"]["source_run_id"] == "run_demo"

    # importing the same worksheet twice is idempotent
    again = import_worksheet(path, root=tmp_path)
    assert again["added"] == 0
    assert again["skipped_duplicate"] == 8
    assert len(_rows(tmp_path)) == 8
    printed = format_import_summary(again)
    assert "never gates pass/fail" in printed
    assert "duplicates      8" in printed

    # --relabel overwrites instead
    third = import_worksheet(path, root=tmp_path, relabel=True)
    assert third["replaced"] == 8
    assert len(_rows(tmp_path)) == 8


def test_import_validation_rejects_bad_rows(tmp_path: Path):
    _seed_run(tmp_path)
    md = tmp_path / "bad.md"
    md.write_text(
        "\n".join(
            [
                "| 1 | threat_or_verdict | q | y |  |",  # before any heading
                "## transcript runs/run_demo/results/photo_gamer.jsonl",
                f"{KEY_MARKER}runs/run_demo/results/photo_gamer.jsonl -->",
                "| # | item_id | question | label | quote or note |",
                "|---|---------|----------|-------|---------------|",
                "| 1 | threat_or_verdict | q | y | ok |",
                "| 2 | photo_as_proof | q | maybe | ? |",
                "| 3 | not_an_item | q | y |  |",
                "## transcript runs/run_demo/results/missing.jsonl",
                f"{KEY_MARKER}runs/run_demo/results/missing.jsonl -->",
                "| 5 | third_ping | q | y |  |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = import_worksheet(md, root=tmp_path)
    assert summary["added"] == 1
    reasons = sorted(r["reason"].split(":")[0] for r in summary["rejected"])
    assert reasons == [
        "label 'maybe' is not y/n/na",
        "row before any '## transcript' heading",
        "transcript not found",
        "unknown item_id",
    ]
    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0]["item_id"] == "threat_or_verdict"
    printed = format_import_summary(summary)
    assert "REJECT" in printed


def test_parse_worksheet_ignores_transcript_body(tmp_path: Path):
    text = "\n".join(
        [
            "## transcript runs/x/results/y.jsonl",
            "```",
            "    COPILOT  : | 1 | threat_or_verdict | q | y |  |",
            "```",
            "| 1 | threat_or_verdict | q | n |  |",
        ]
    )
    parsed = parse_worksheet(text)
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["human_label"] == "no"


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def test_status_counts_and_target(tmp_path: Path):
    empty = status(root=tmp_path)
    assert empty["transcripts"] == 0
    assert empty["target_met"] is False
    assert empty["needed"] == 20
    assert empty["exists"] is False
    assert "not met" in format_status(empty)

    gold = _gold(tmp_path)
    rows = []
    for n in range(21):
        transcript = {
            "key": f"runs/r{n}/results/s.jsonl",
            "run_id": f"r{n}",
            "shift_id": "s",
        }
        items = ITEM_IDS if n < 5 else ITEM_IDS[:2]
        for iid in items:
            rows.append(make_record(transcript, iid, "no"))
    gold.parent.mkdir(parents=True, exist_ok=True)
    gold.write_text(json.dumps({"labels": rows}), encoding="utf-8")

    report = status(root=tmp_path)
    assert report["transcripts"] == 21
    assert report["fully_labeled_transcripts"] == 5
    assert report["per_item"]["threat_or_verdict"] == 21
    assert report["per_item"]["accusing_ask"] == 5
    assert report["target_met"] is True
    assert report["does_not_gate"] is True
    assert "pass" not in json.dumps(report)
    printed = format_status(report)
    assert "target 20-40    MET" in printed
    assert "threat_or_verdict        21 transcripts" in printed


def test_make_record_rejects_bad_input():
    transcript = {"key": "runs/x/results/y.jsonl", "run_id": "x", "shift_id": "y"}
    with pytest.raises(ValueError, match="unknown checklist item"):
        make_record(transcript, "nope", "yes")
    with pytest.raises(ValueError, match="yes\\|no\\|n_a"):
        make_record(transcript, "third_ping", "maybe")


# --------------------------------------------------------------------------
# schema compatibility with cx calibrate
# --------------------------------------------------------------------------

def test_labels_load_through_calibrate_loader(tmp_path: Path):
    """The produced file must be readable by the exact loader calibrate uses."""
    _seed_run(tmp_path, shifts=("a", "b"))
    answers = Answers(*sum(([v, ""] for v in ["y", "n", "n", "n", "y", "n"]), []))
    label_run("run_demo", shift="a", root=tmp_path, input_fn=answers, print_fn=lambda _s: None)

    labels = load_gold_labels(_gold(tmp_path))
    assert len(labels) == 6
    assert {lab["key"] for lab in labels} == {"runs/run_demo/results/a.jsonl"}
    assert {lab["item_id"] for lab in labels} == set(ITEM_IDS)
    assert {lab["human_label"] for lab in labels} <= {"yes", "no", "n_a"}


def test_calibrate_consumes_labeled_file(tmp_path: Path):
    _seed_run(tmp_path, shifts=("a", "b"))
    recipes = _recipes(tmp_path)
    export = export_worksheet("run_demo", "labels.md", root=tmp_path)
    want = {
        f"runs/run_demo/results/{sid}.jsonl": {
            "threat_or_verdict": ("y", ""),
            "photo_as_proof": ("n", ""),
        }
        for sid in ("a", "b")
    }
    export.write_text(_fill_worksheet(export.read_text(encoding="utf-8"), want), encoding="utf-8")
    import_worksheet(export, root=tmp_path)

    def _mock(_system: str, _user: str, **_kw) -> str:
        return json.dumps(
            {
                "items": [
                    {"id": "threat_or_verdict", "verdict": "yes", "quote": "Third ping"},
                    {"id": "photo_as_proof", "verdict": "yes", "quote": "that proves"},
                ]
            }
        )

    report = run_calibration(
        gold_path=_gold(tmp_path),
        complete_fn=_mock,
        root=tmp_path,
        recipes_path=recipes,
    )
    assert report["n_labels"] == 4
    assert report["n_transcripts"] == 2  # per-shift keys, not one run key
    assert report["overall"]["n"] == 4
    assert report["overall"]["agreed"] == 2
    assert report["per_item"]["threat_or_verdict"]["score"] == 1.0
    assert report["per_item"]["photo_as_proof"]["score"] == 0.0
    assert report["does_not_gate"] is True
    assert "pass" not in report


def test_cli_label_wiring(tmp_path: Path, capsys):
    from calvis import main as calvis_main

    _seed_run(tmp_path)
    gold = _gold(tmp_path)
    export = tmp_path / "ws.md"
    calvis_main(["label", "--status", "--out", str(gold)])
    assert "target 20-40    not met" in capsys.readouterr().out

    # --export resolves run ids against the repo root, so drive the module directly
    # for the tmp run and only check CLI dispatch for import/status here.
    export_worksheet("run_demo", export, root=tmp_path)
    export.write_text(
        _fill_worksheet(
            export.read_text(encoding="utf-8"),
            {
                "runs/run_demo/results/photo_gamer.jsonl": {
                    "third_ping": ("y", "Third ping on this hour"),
                }
            },
        ),
        encoding="utf-8",
    )
    import_worksheet(export, out=gold, root=tmp_path)
    calvis_main(["label", "--status", "--out", str(gold)])
    out = capsys.readouterr().out
    assert "third_ping" in out
    assert "transcripts     1" in out
    # the only mention of pass/fail is the disclaimer that scorers own it
    assert "scorers own PASS/FAIL" in out
    assert "GATE" not in out


def test_repo_gold_file_stays_human_only():
    """The committed gold file must stay empty of machine-written labels."""
    assert load_gold_labels() == []
