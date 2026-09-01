"""Human gold-label workflow for the ADVISORY conduct checklist.

`cx label` is how a person produces `experiments/gold/conduct_labels.json`,
the file `cx calibrate` reads. Three paths, all human-driven:

  cx label <run_id>                       walk transcripts, answer 6 questions
  cx label --export <run_id> --to f.md    Markdown worksheet to fill offline
  cx label --import f.md                  parse the worksheet back, validated
  cx label --status                       coverage vs the 20-40 transcript target

Hard rules:
- Labels are HUMAN data. Nothing here calls the judge or auto-fills a verdict.
- Nothing here gates pass/fail. Deterministic scorers own GATE; calibration is
  advisory even after this file is full.
- Appends only. Re-running skips transcripts already labeled unless relabel=True.

Schema written (exactly what `harness.judge.load_gold_labels` reads):
    {"run_id": null,
     "transcript_path": "runs/<run>/results/<shift>.jsonl",
     "item_id": "<checklist item id>",
     "human_label": "yes" | "no" | "n_a",
     ...human-only extras the loader ignores (quote, shift_id, labeled_at)}

One transcript == one shift's results JSONL, so `transcript_path` (not run_id)
is the alignment key — a run with three shifts is three separately keyed
transcripts. run_id stays null for that reason.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from harness.judge import CHECKLIST_ITEMS_V1, ITEM_BY_ID, ITEM_IDS
from harness.store import ExperimentStore

ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = ROOT / "experiments" / "gold" / "conduct_labels.json"

TARGET_MIN = 20
TARGET_MAX = 40
CHECKLIST_VERSION = "v1"

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]

# Interactive answer vocabulary -> stored human_label.
ANSWERS = {
    "y": "yes",
    "yes": "yes",
    "n": "no",
    "no": "no",
    "a": "n_a",
    "na": "n_a",
    "n_a": "n_a",
    "n/a": "n_a",
}
SKIP_WORDS = frozenset({"s", "skip", ""})
QUIT_WORDS = frozenset({"q", "quit", "exit"})
# Worksheet cell vocabulary. Blank means "not reviewed yet", never a label.
CELL_ANSWERS = dict(ANSWERS)


# --------------------------------------------------------------------------
# gold file I/O
# --------------------------------------------------------------------------

def new_gold_doc() -> dict[str, Any]:
    """Fresh scaffold, same shape as experiments/gold/conduct_labels.json."""
    return {
        "version": 1,
        "authority": "advisory_only",
        "pass_fail_authority": "deterministic_scorers_only",
        "target_transcripts": f"{TARGET_MIN}-{TARGET_MAX}",
        "notes": [
            "Human gold labels for the advisory conduct checklist. Humans fill this file.",
            "Written by `cx label` (interactive) or `cx label --import` (worksheet).",
            "Each row is one checklist item on one transcript.",
            "Alignment is NEVER a gate. Deterministic scorers own PASS/FAIL.",
            "human_label values: yes | no | n_a",
        ],
        "item_ids": list(ITEM_IDS),
        "human_label_values": ["yes", "no", "n_a"],
        "labels": [],
    }


def load_gold_doc(path: Path) -> dict[str, Any]:
    """Load the gold file preserving its documentation keys."""
    if not path.exists():
        return new_gold_doc()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {**new_gold_doc(), "labels": list(data)}
    if not isinstance(data, dict):
        raise ValueError(f"gold file must be an object or list: {path}")
    data.setdefault("labels", [])
    if not isinstance(data["labels"], list):
        raise ValueError(f"gold file labels must be a list: {path}")
    return data


def write_gold_doc(path: Path, doc: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _is_label_row(row: Any) -> bool:
    return isinstance(row, dict) and bool(row.get("item_id")) and not row.get("_example")


def existing_index(doc: dict[str, Any]) -> dict[str, set[str]]:
    """{transcript key: {item_id, ...}} already present in the gold file."""
    index: dict[str, set[str]] = {}
    for row in doc.get("labels") or []:
        if not _is_label_row(row):
            continue
        key = str(row.get("run_id") or row.get("transcript_path") or "")
        if not key:
            continue
        index.setdefault(key, set()).add(str(row["item_id"]))
    return index


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------

def _rel_key(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def list_run_transcripts(
    run_id: str,
    *,
    shift: str | None = None,
    root: Path | None = None,
    store: ExperimentStore | None = None,
) -> list[dict[str, Any]]:
    """One entry per shift results file in the run. Order follows the manifest."""
    root = root or ROOT
    store = store or ExperimentStore(root / "runs")
    run_dir = store.run_dir(run_id)
    if not run_dir.exists():
        raise FileNotFoundError(f"no such run: {run_dir}")
    results = run_dir / "results"

    order: list[str] = []
    try:
        manifest = store.load_manifest(run_id)
        order = [str(s) for s in (manifest.get("shifts") or [])]
    except FileNotFoundError:
        manifest = {}
    on_disk = [p.stem for p in sorted(results.glob("*.jsonl"))] if results.exists() else []
    shifts = [s for s in order if s in on_disk] + [s for s in on_disk if s not in order]
    if shift:
        shifts = [s for s in shifts if s == str(shift)]

    out: list[dict[str, Any]] = []
    for sid in shifts:
        path = results / f"{sid}.jsonl"
        turns = store.load_turns(run_id, sid)
        out.append(
            {
                "run_id": run_id,
                "shift_id": sid,
                "path": path,
                "key": _rel_key(path, root),
                "turns": turns,
            }
        )
    return out


def render_transcript(transcript: dict[str, Any]) -> str:
    """Readable dump for a human reviewer: turns, guard vs copilot, tools, escalations."""
    turns = transcript.get("turns") or []
    bar = "=" * 78
    head = [
        bar,
        f"TRANSCRIPT  {transcript.get('key')}",
        f"run={transcript.get('run_id')}  shift={transcript.get('shift_id')}  "
        f"turns={len(turns)}",
        bar,
    ]
    if not turns:
        return "\n".join(head + ["(empty transcript)"])
    body: list[str] = []
    for t in turns:
        body.append("")
        body.append(
            f"turn {t.get('turn')}  [{t.get('trigger')}]  "
            f"decision={t.get('decision')}  confidence={t.get('confidence')}"
        )
        guard = (
            t.get("guard_text")
            or t.get("incoming")
            or t.get("guard_message")
            or (t.get("meta") or {}).get("guard_text")
        )
        if guard:
            body.append(f"    GUARD    : {guard}")
        for m in t.get("messages") or []:
            text = m if isinstance(m, str) else (
                m.get("body") or m.get("message") or m.get("text") or ""
            )
            body.append(f"    COPILOT  : {text}")
        for tool in t.get("tools_used") or []:
            name = tool if isinstance(tool, str) else (tool.get("tool") or tool.get("name") or "")
            body.append(f"    TOOL     : {name}")
        for esc in t.get("escalations") or []:
            if isinstance(esc, dict):
                body.append(
                    f"    ESCALATE : {esc.get('kind')} {esc.get('details') or ''}".rstrip()
                )
            else:
                body.append(f"    ESCALATE : {esc}")
        for note in t.get("notes") or []:
            body.append(f"    NOTE     : {note}")
        for gap in t.get("data_gaps") or []:
            body.append(f"    DATA GAP : {gap}")
    return "\n".join(head + body)


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

def make_record(
    transcript: dict[str, Any],
    item_id: str,
    human_label: str,
    *,
    quote: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    """One gold row. run_id stays null so transcript_path is the alignment key."""
    if item_id not in ITEM_BY_ID:
        raise ValueError(f"unknown checklist item: {item_id}")
    if human_label not in ("yes", "no", "n_a"):
        raise ValueError(f"human_label must be yes|no|n_a, got {human_label!r}")
    return {
        "run_id": None,
        "transcript_path": transcript["key"],
        "item_id": item_id,
        "human_label": human_label,
        "quote": quote or "",
        "source_run_id": transcript.get("run_id"),
        "shift_id": transcript.get("shift_id"),
        "labeled_by": "human",
        "checklist_version": CHECKLIST_VERSION,
        "labeled_at": now or datetime.now(timezone.utc).isoformat(),
    }


def append_records(
    path: Path,
    records: Iterable[dict[str, Any]],
    *,
    relabel: bool = False,
) -> dict[str, Any]:
    """Append rows to the gold file. Duplicates are skipped unless relabel."""
    doc = load_gold_doc(path)
    labels: list[dict[str, Any]] = list(doc.get("labels") or [])
    seen = {
        (str(r.get("run_id") or r.get("transcript_path") or ""), str(r.get("item_id"))): i
        for i, r in enumerate(labels)
        if _is_label_row(r)
    }
    added, replaced, skipped = 0, 0, 0
    for rec in records:
        ident = (str(rec.get("run_id") or rec.get("transcript_path") or ""), str(rec["item_id"]))
        if ident in seen:
            if not relabel:
                skipped += 1
                continue
            labels[seen[ident]] = rec
            replaced += 1
            continue
        seen[ident] = len(labels)
        labels.append(rec)
        added += 1
    doc["labels"] = labels
    write_gold_doc(path, doc)
    return {"added": added, "replaced": replaced, "skipped_duplicate": skipped, "path": str(path)}


# --------------------------------------------------------------------------
# interactive
# --------------------------------------------------------------------------

def _prompt_line() -> str:
    return "    answer [y=yes  n=no  na=not applicable  s=skip  q=quit] > "


def label_run(
    run_id: str,
    *,
    shift: str | None = None,
    out: Path | None = None,
    relabel: bool = False,
    input_fn: InputFn | None = None,
    print_fn: PrintFn | None = None,
    root: Path | None = None,
    store: ExperimentStore | None = None,
) -> dict[str, Any]:
    """Walk transcripts and ask the 6 checklist questions. Human answers only.

    input_fn is injected so the flow is testable without stdin.
    """
    root = root or ROOT
    out = Path(out) if out else (root / "experiments" / "gold" / "conduct_labels.json")
    say = print_fn or print
    ask = input_fn or input

    transcripts = list_run_transcripts(run_id, shift=shift, root=root, store=store)
    if not transcripts:
        say(f"no transcripts found in run {run_id}")
        return {
            "run_id": run_id,
            "transcripts": 0,
            "labeled": 0,
            "skipped_transcripts": [],
            "quit": False,
            "added": 0,
        }

    doc = load_gold_doc(out)
    already = existing_index(doc)
    records: list[dict[str, Any]] = []
    skipped_transcripts: list[str] = []
    quit_early = False

    say("HUMAN LABELING  advisory conduct checklist (never gates pass/fail)")
    say(f"run={run_id}  transcripts={len(transcripts)}  gold={out}")

    for transcript in transcripts:
        if quit_early:
            break
        key = transcript["key"]
        if already.get(key) and not relabel:
            say(f"\nskip (already labeled): {key}  -- use --relabel to redo")
            skipped_transcripts.append(key)
            continue
        say("")
        say(render_transcript(transcript))
        say("")
        for pos, spec in enumerate(CHECKLIST_ITEMS_V1, start=1):
            flag = "  [must_not_happen]" if spec["must_not_happen"] else ""
            say(f"  Q{pos}/{len(CHECKLIST_ITEMS_V1)}  {spec['id']}{flag}")
            say(f"    {spec['text']}")
            try:
                raw = (ask(_prompt_line()) or "").strip().lower()
            except (EOFError, StopIteration):
                quit_early = True
                break
            if raw in QUIT_WORDS:
                quit_early = True
                break
            if raw in SKIP_WORDS:
                say("    skipped")
                continue
            label = ANSWERS.get(raw)
            while label is None:
                say(f"    '{raw}' is not y / n / na / s / q")
                try:
                    raw = (ask(_prompt_line()) or "").strip().lower()
                except (EOFError, StopIteration):
                    quit_early = True
                    break
                if raw in QUIT_WORDS:
                    quit_early = True
                    break
                if raw in SKIP_WORDS:
                    break
                label = ANSWERS.get(raw)
            if quit_early:
                break
            if label is None:
                say("    skipped")
                continue
            try:
                quote = (ask("    quote or note (optional, Enter to skip) > ") or "").strip()
            except (EOFError, StopIteration):
                quote = ""
                quit_early = True
            records.append(make_record(transcript, spec["id"], label, quote=quote))
            if quit_early:
                break

    summary = append_records(out, records, relabel=relabel)
    summary.update(
        {
            "run_id": run_id,
            "transcripts": len(transcripts),
            "labeled": len(records),
            "skipped_transcripts": skipped_transcripts,
            "quit": quit_early,
        }
    )
    say("")
    say(
        f"wrote {out}  added={summary['added']}  replaced={summary['replaced']}  "
        f"duplicate_skipped={summary['skipped_duplicate']}"
        + ("  (quit early)" if quit_early else "")
    )
    return summary


# --------------------------------------------------------------------------
# worksheet export / import
# --------------------------------------------------------------------------

KEY_MARKER = "<!-- transcript_key: "


def _escape_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def export_worksheet(
    run_id: str,
    to: str | Path,
    *,
    shift: str | None = None,
    root: Path | None = None,
    store: ExperimentStore | None = None,
) -> Path:
    """Write a Markdown worksheet a reviewer fills in a text editor."""
    root = root or ROOT
    transcripts = list_run_transcripts(run_id, shift=shift, root=root, store=store)
    to_path = Path(to)
    if not to_path.is_absolute():
        to_path = root / to_path

    lines = [
        f"# Conduct label worksheet - run {run_id}",
        "",
        "Human labels for the ADVISORY conduct checklist. Advisory only: these",
        "labels never gate pass/fail, they only calibrate the judge.",
        "",
        "Fill the `label` cell with `y` (yes), `n` (no), or `na` (not applicable).",
        "Leave it blank to leave that item unlabeled. Add a verbatim quote or a",
        "short note in the last cell. Do not edit the `transcript_key` comments.",
        "",
        "Then: `cx label --import <this file>`",
        "",
    ]
    for transcript in transcripts:
        lines.append(f"## transcript {transcript['key']}")
        lines.append("")
        lines.append(f"{KEY_MARKER}{transcript['key']} -->")
        lines.append("")
        lines.append("```")
        lines.append(render_transcript(transcript))
        lines.append("```")
        lines.append("")
        lines.append("| # | item_id | question | label | quote or note |")
        lines.append("|---|---------|----------|-------|---------------|")
        for spec in CHECKLIST_ITEMS_V1:
            flag = " (must_not_happen)" if spec["must_not_happen"] else ""
            lines.append(
                f"| {spec['number']} | {spec['id']} | "
                f"{_escape_cell(spec['text'])}{flag} |  |  |"
            )
        lines.append("")
    to_path.parent.mkdir(parents=True, exist_ok=True)
    to_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return to_path


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.replace("\\|", "|").strip() for c in body.split("|")]


def parse_worksheet(text: str) -> dict[str, Any]:
    """Parse a filled worksheet into candidate rows plus rejections."""
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    blank = 0
    key: str | None = None
    keys: list[str] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            # transcript body, not worksheet input
            continue
        if stripped.startswith(KEY_MARKER):
            key = stripped[len(KEY_MARKER):].split("-->")[0].strip()
            if key and key not in keys:
                keys.append(key)
            continue
        if stripped.startswith("## transcript "):
            candidate = stripped[len("## transcript "):].strip()
            if candidate:
                key = candidate
                if key not in keys:
                    keys.append(key)
            continue
        if not stripped.startswith("|"):
            continue
        cells = _split_row(stripped)
        if len(cells) < 4:
            continue
        item_id = cells[1]
        if item_id in ("item_id", "") or set(item_id) <= set("-: "):
            continue
        label_cell = cells[3]
        quote = cells[4] if len(cells) > 4 else ""
        if item_id not in ITEM_BY_ID:
            rejected.append(
                {"line": lineno, "item_id": item_id, "reason": "unknown item_id", "key": key}
            )
            continue
        if key is None:
            rejected.append(
                {
                    "line": lineno,
                    "item_id": item_id,
                    "reason": "row before any '## transcript' heading",
                    "key": None,
                }
            )
            continue
        if not label_cell:
            blank += 1
            continue
        label = CELL_ANSWERS.get(label_cell.lower())
        if label is None:
            rejected.append(
                {
                    "line": lineno,
                    "item_id": item_id,
                    "reason": f"label {label_cell!r} is not y/n/na",
                    "key": key,
                }
            )
            continue
        rows.append({"key": key, "item_id": item_id, "human_label": label, "quote": quote})
    return {"rows": rows, "rejected": rejected, "blank": blank, "keys": keys}


def import_worksheet(
    md_path: str | Path,
    *,
    out: Path | None = None,
    root: Path | None = None,
    relabel: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Parse a filled worksheet into the gold file, validating every row."""
    root = root or ROOT
    out = Path(out) if out else (root / "experiments" / "gold" / "conduct_labels.json")
    src = Path(md_path)
    if not src.is_absolute():
        src = root / src
    parsed = parse_worksheet(src.read_text(encoding="utf-8"))
    rejected = list(parsed["rejected"])
    records: list[dict[str, Any]] = []
    for row in parsed["rows"]:
        key = row["key"]
        path = Path(key)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            rejected.append(
                {
                    "line": None,
                    "item_id": row["item_id"],
                    "reason": f"transcript not found: {key}",
                    "key": key,
                }
            )
            continue
        transcript = {
            "key": key,
            "run_id": _run_id_from_key(key),
            "shift_id": path.stem,
        }
        records.append(
            make_record(transcript, row["item_id"], row["human_label"], quote=row["quote"], now=now)
        )
    summary = append_records(out, records, relabel=relabel)
    summary.update(
        {
            "source": str(src),
            "parsed_rows": len(parsed["rows"]),
            "blank_cells": parsed["blank"],
            "rejected": rejected,
            "transcripts": sorted({r["transcript_path"] for r in records}),
        }
    )
    return summary


def _run_id_from_key(key: str) -> str | None:
    parts = Path(key).as_posix().split("/")
    if len(parts) >= 3 and parts[-2] == "results":
        return parts[-3]
    return None


def format_import_summary(summary: dict[str, Any]) -> str:
    lines = [
        "HUMAN LABELS  import (advisory only - never gates pass/fail)",
        f"  source          {summary.get('source')}",
        f"  gold            {summary.get('path')}",
        f"  rows parsed     {summary.get('parsed_rows')}",
        f"  imported        {summary.get('added')}",
        f"  replaced        {summary.get('replaced')}",
        f"  duplicates      {summary.get('skipped_duplicate')} (already labeled; --relabel to overwrite)",
        f"  blank cells     {summary.get('blank_cells')} (left unlabeled)",
    ]
    rejected = summary.get("rejected") or []
    if rejected:
        lines.append(f"  rejected        {len(rejected)}")
        for r in rejected:
            where = f"line {r['line']}" if r.get("line") else (r.get("key") or "?")
            lines.append(f"    REJECT  {where}  {r.get('item_id')}  {r.get('reason')}")
    else:
        lines.append("  rejected        0")
    for key in summary.get("transcripts") or []:
        lines.append(f"  transcript      {key}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def status(
    *,
    out: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Coverage of the gold file: transcripts per item, and the 20-40 target."""
    root = root or ROOT
    out = Path(out) if out else (root / "experiments" / "gold" / "conduct_labels.json")
    doc = load_gold_doc(out)
    per_item: dict[str, int] = {iid: 0 for iid in ITEM_IDS}
    unknown_items: dict[str, int] = {}
    keys: set[str] = set()
    complete: set[str] = set()
    by_key: dict[str, set[str]] = {}
    for row in doc.get("labels") or []:
        if not _is_label_row(row):
            continue
        key = str(row.get("run_id") or row.get("transcript_path") or "")
        if not key:
            continue
        iid = str(row["item_id"])
        keys.add(key)
        by_key.setdefault(key, set()).add(iid)
        if iid in per_item:
            per_item[iid] += 1
        else:
            unknown_items[iid] = unknown_items.get(iid, 0) + 1
    for key, items in by_key.items():
        if set(ITEM_IDS) <= items:
            complete.add(key)
    n = len(keys)
    return {
        "advisory": True,
        "does_not_gate": True,
        "gold_path": str(out),
        "exists": out.exists(),
        "transcripts": n,
        "fully_labeled_transcripts": len(complete),
        "labels": sum(per_item.values()) + sum(unknown_items.values()),
        "per_item": per_item,
        "unknown_items": unknown_items,
        "target_min": TARGET_MIN,
        "target_max": TARGET_MAX,
        "target_met": n >= TARGET_MIN,
        "over_target": n > TARGET_MAX,
        "needed": max(0, TARGET_MIN - n),
    }


def format_status(report: dict[str, Any]) -> str:
    lines = [
        "HUMAN LABELS  gold coverage (advisory only - never gates pass/fail)",
        f"  gold file       {report.get('gold_path')}"
        + ("" if report.get("exists") else "  (not created yet)"),
        f"  transcripts     {report.get('transcripts')} "
        f"({report.get('fully_labeled_transcripts')} with all 6 items)",
        f"  labels          {report.get('labels')}",
    ]
    for iid in ITEM_IDS:
        lines.append(f"  {iid:<24} {(report.get('per_item') or {}).get(iid, 0)} transcripts")
    for iid, count in (report.get("unknown_items") or {}).items():
        lines.append(f"  {iid:<24} {count}  (NOT a checklist v1 item)")
    target = f"{report.get('target_min')}-{report.get('target_max')}"
    if report.get("target_met"):
        extra = "  (above the suggested max)" if report.get("over_target") else ""
        lines.append(f"  target {target}    MET{extra}")
    else:
        lines.append(
            f"  target {target}    not met - {report.get('needed')} more transcripts needed"
        )
    lines.append("  (calibration alignment is advisory; scorers own PASS/FAIL)")
    return "\n".join(lines)
