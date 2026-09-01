"""Session C: one-file prompt patch from a Diagnosis.

Copy `parent_variant` → `variants/auto_<stamp>/`, edit exactly one markdown
file, return a `PatchPlan` carrying the unified diff.

Everything that could go wrong is refused **in code**, never merely asked for
in the prompt (LOOP.md hard rule 3 — the LLM proposes, the harness constrains):

* `validate_target_file` — the edit target must be a single relative path under
  `core/` or `instructions/`; absolute paths, `..`, and non-markdown are refused.
* `parse_patch_response` — an LLM reply carrying a second file (or a different
  file than the diagnosis named) is refused without retry.
* `resolve_inside` — the written path must resolve inside the new variant dir.
* `check_patch_size` — a wholesale rewrite (too much of the parent deleted, the
  file shrinking sharply, or an unbounded bolt-on) is refused. LOOP.md hard
  rule 5: one file, an *ordered* rule, not a stacked addendum (Variant A2).
* `changed_files` — after applying, exactly one file may differ from the parent.
* `assert_variant_assembles` / `assert_file_is_assembled` — the harness prompt
  loader must still build the system prompt and every turn message from the new
  variant, and the edited file must actually land in that assembly.

Any failure deletes the half-built variant dir and raises `PatchRefused`.

`skip_llm=True` is a dry mode: it applies a canned, clearly marked ordered-rule
edit so the loop can be exercised with zero API calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher, unified_diff
from pathlib import Path
from typing import Any, Callable

from harness.agent.types import Diagnosis, PatchPlan, ProblemCard
from harness.judge import parse_json_object
from harness.prompts import (
    CONTEXT_PLACEHOLDER,
    CORE_ORDER,
    DEFAULT_INSTRUCTION,
    TRIGGER_TO_INSTRUCTION,
    GuardRef,
    compile_system_prompt,
    compile_turn_message,
)

ROOT = Path(__file__).resolve().parents[2]
VARIANTS_DIR = ROOT / "variants"

#: Fraction of the parent file's lines the patch may delete or replace. Above
#: this the edit is a rewrite of someone else's prompt, not a targeted rule.
MAX_REWRITE_RATIO = 0.40
#: The patched file may not shrink below this fraction of the parent's lines.
MIN_KEEP_RATIO = 0.70
#: Growth cap. Tripped only together with ABS_GROWTH_ALLOWANCE, so a very short
#: file can still take a short ordered-rule block.
MAX_GROWTH_RATIO = 2.5
ABS_GROWTH_ALLOWANCE = 40

ALLOWED_DIRS = ("core", "instructions")

DRY_MARKER = "<!-- auto-patch: dry mode (skip_llm), canned ordered rule -->"

CompleteFn = Callable[[str, str], str]


class PatchRefused(RuntimeError):
    """The proposed edit broke a patcher rule. The variant dir is removed."""


# --------------------------------------------------------------------------
# path + size enforcement
# --------------------------------------------------------------------------


def validate_target_file(target_file: str) -> str:
    """Normalize the diagnosis target to `core/x.md` or `instructions/x.md`."""
    raw = (target_file or "").strip().replace("\\", "/")
    if not raw:
        raise PatchRefused("diagnosis.target_file is empty")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or Path(raw).is_absolute():
        raise PatchRefused(f"target_file must be variant-relative, got {target_file!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PatchRefused(f"target_file must not escape the variant dir: {target_file!r}")
    if len(parts) != 2 or parts[0] not in ALLOWED_DIRS:
        raise PatchRefused(
            f"target_file must be one file under core/ or instructions/, got {target_file!r}"
        )
    if not parts[1].endswith(".md"):
        raise PatchRefused(f"target_file must be a markdown file, got {target_file!r}")
    return "/".join(parts)


def resolve_inside(variant_dir: Path, rel_path: str) -> Path:
    """Resolve `rel_path` under `variant_dir`, refusing anything that escapes."""
    base = variant_dir.resolve()
    candidate = (base / rel_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:  # symlink or traversal
        raise PatchRefused(
            f"refusing to write outside the variant dir: {candidate}"
        ) from exc
    return candidate


def _tree_files(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def changed_files(parent_dir: Path, variant_dir: Path) -> list[str]:
    """Every relative path that differs between the two variant trees."""
    left, right = _tree_files(parent_dir), _tree_files(variant_dir)
    names = sorted(set(left) | set(right))
    return [n for n in names if left.get(n) != right.get(n)]


def patch_size_stats(original: str, new: str) -> dict[str, Any]:
    """Line churn between two file bodies (no verdict, just the numbers)."""
    old_lines = original.splitlines()
    new_lines = new.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    removed = len(old_lines) - matched
    added = len(new_lines) - matched
    return {
        "old_lines": len(old_lines),
        "new_lines": len(new_lines),
        "removed": removed,
        "added": added,
        "rewrite_ratio": removed / len(old_lines) if old_lines else 1.0,
        "keep_ratio": (len(new_lines) / len(old_lines)) if old_lines else 1.0,
    }


def check_patch_size(original: str, new: str, rel_path: str) -> dict[str, Any]:
    """Refuse wholesale rewrites, sharp shrinks, and unbounded bolt-ons."""
    if not new.strip():
        raise PatchRefused(f"patch emptied {rel_path}")
    if new == original:
        raise PatchRefused(f"patch changed nothing in {rel_path}")
    stats = patch_size_stats(original, new)
    if stats["rewrite_ratio"] > MAX_REWRITE_RATIO:
        raise PatchRefused(
            f"wholesale rewrite refused: {stats['removed']}/{stats['old_lines']} "
            f"parent lines deleted or replaced in {rel_path} "
            f"({stats['rewrite_ratio']:.0%} > {MAX_REWRITE_RATIO:.0%})"
        )
    if stats["keep_ratio"] < MIN_KEEP_RATIO:
        raise PatchRefused(
            f"patch shrank {rel_path} from {stats['old_lines']} to "
            f"{stats['new_lines']} lines (< {MIN_KEEP_RATIO:.0%} of the parent)"
        )
    grew_by = stats["new_lines"] - stats["old_lines"]
    if (
        stats["keep_ratio"] > MAX_GROWTH_RATIO
        and grew_by > ABS_GROWTH_ALLOWANCE
    ):
        raise PatchRefused(
            f"patch bloated {rel_path} by {grew_by} lines "
            f"({stats['keep_ratio']:.1f}x the parent); prefer an ordered rule"
        )
    return stats


# --------------------------------------------------------------------------
# loader validation: the variant must still assemble
# --------------------------------------------------------------------------


def assert_variant_assembles(variant_dir: Path) -> None:
    """The harness prompt loader must build every prompt from this variant."""
    missing = [
        f"core/{name}.md"
        for name in CORE_ORDER
        if not (variant_dir / "core" / f"{name}.md").exists()
    ]
    missing += [
        f"instructions/{name}"
        for name in sorted(set(TRIGGER_TO_INSTRUCTION.values()) | {DEFAULT_INSTRUCTION})
        if not (variant_dir / "instructions" / name).exists()
    ]
    if missing:
        raise PatchRefused(f"variant no longer assembles, missing: {missing}")

    try:
        system = compile_system_prompt(variant_dir, CONTEXT_PLACEHOLDER)
    except Exception as exc:  # noqa: BLE001 - surfaced as a refusal
        raise PatchRefused(f"system prompt failed to compile: {exc}") from exc
    if not system.strip():
        raise PatchRefused("system prompt compiled empty")

    start = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    for trigger in sorted(set(TRIGGER_TO_INSTRUCTION) | {"__unmapped__"}):
        try:
            message = compile_turn_message(
                variant_dir,
                turn=1,
                trigger=trigger,
                ts=start,
                session_id="assemble-check",
                job_id="0",
                guards=[GuardRef(name="Check", id=0)],
                shift_start=start,
                shift_end=start + timedelta(hours=8),
                tz_name="UTC",
                first_turn=True,
                site_account="acct",
                site_address="addr",
            )
        except Exception as exc:  # noqa: BLE001
            raise PatchRefused(
                f"turn message for trigger {trigger!r} failed to compile: {exc}"
            ) from exc
        if not message.strip():
            raise PatchRefused(f"turn message for trigger {trigger!r} compiled empty")


def assert_file_is_assembled(variant_dir: Path, rel_path: str, new_text: str) -> None:
    """The edited file must actually reach the model, verbatim."""
    sub, name = rel_path.split("/", 1)
    body = new_text.strip()
    if sub == "core":
        if Path(name).stem not in CORE_ORDER:
            raise PatchRefused(
                f"{rel_path} is not part of the assembled system prompt "
                f"(core order: {CORE_ORDER})"
            )
        assembled = compile_system_prompt(variant_dir, CONTEXT_PLACEHOLDER)
    else:
        reachable = set(TRIGGER_TO_INSTRUCTION.values()) | {DEFAULT_INSTRUCTION}
        if name not in reachable:
            raise PatchRefused(
                f"{rel_path} is not reachable from any trigger: {sorted(reachable)}"
            )
        trigger = next(
            (t for t, f in TRIGGER_TO_INSTRUCTION.items() if f == name), "__unmapped__"
        )
        start = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        assembled = compile_turn_message(
            variant_dir,
            turn=1,
            trigger=trigger,
            ts=start,
            session_id="assemble-check",
            job_id="0",
            guards=[GuardRef(name="Check", id=0)],
            shift_start=start,
            shift_end=start + timedelta(hours=8),
            tz_name="UTC",
        )
    if body not in assembled:
        raise PatchRefused(
            f"{rel_path} did not survive assembly verbatim (loader would drop the edit)"
        )


# --------------------------------------------------------------------------
# dry mode: canned ordered-rule edit
# --------------------------------------------------------------------------


def _one_line(text: str, limit: int = 200) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def canned_ordered_rule(original: str, diagnosis: Diagnosis) -> str:
    """A short, clearly-marked ordered rule inserted ahead of the file body.

    Ordered, not stacked: it states what runs *first* and hands control back to
    the file as written (LOOP.md hard rule 5). Dry mode only — no model call.
    """
    must_call = list(getattr(diagnosis.spec, "must_call", []) or [])
    clauses = []
    if must_call:
        clauses.append(
            f"Call {', '.join(f'`{t}`' for t in must_call)} before you affirm anything."
        )
    if getattr(diagnosis.spec, "require_escalation", False):
        clauses.append("Climb the ladder when policy requires it, never another soft DM.")
    call_clause = (" " + " ".join(clauses)) if clauses else ""
    never = _one_line(
        (diagnosis.must_not_happen or ["skip a required escalation"])[0], 120
    )
    preserve = "; ".join(_one_line(p, 80) for p in (diagnosis.must_preserve or [])) or (
        "everything this file already requires"
    )
    block = [
        DRY_MARKER,
        f"## Ordered rule — {diagnosis.problem_class} (auto-patch dry mode)",
        "",
        "Run these in order. Step 2 is the rest of this file, unchanged; nothing here",
        "stacks on top of it or overrides it.",
        "",
        f"1. **First:** close the gap this patch targets — "
        f"{_one_line(diagnosis.must_improve)}{call_clause}",
        "2. **Then:** follow the rest of this file exactly as written below.",
        f"3. **Never:** {never}.",
        "",
        f"Still required, unchanged: {preserve}.",
        "",
    ]
    body = "\n".join(block)

    lines = original.splitlines(keepends=True)
    insert_at = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            insert_at = index + 1
            while insert_at < len(lines) and not lines[insert_at].strip():
                insert_at += 1
            break
    head = "".join(lines[:insert_at])
    tail = "".join(lines[insert_at:])
    if not head:
        return f"{body}\n{tail}"
    if not head.endswith("\n"):
        head += "\n"
    gap = "" if head.endswith("\n\n") else "\n"
    return f"{head}{gap}{body}\n{tail}"


# --------------------------------------------------------------------------
# LLM path
# --------------------------------------------------------------------------


PATCHER_SYSTEM = """You are the Calvis prompt patcher for a security-guard copilot ("Sol").

You rewrite EXACTLY ONE markdown prompt file so the copilot runs the right process.

Hard rules:
- Edit only the target file you are given. Never touch, mention, or return a second file.
- Prefer an ORDERED RULE (state what runs first, then hand back to the existing text)
  over stacking a new addendum on the end. Stacked, conflicting addenda are what sank
  Variant A2.
- Keep the file's voice, structure, headings, and examples. Make the smallest edit that
  serves must_improve; do not rewrite the file wholesale, and do not delete guidance you
  are not fixing.
- Everything in must_preserve must still be required by the file afterwards.
- Nothing in must_not_happen may become possible or encouraged.
- You never declare pass/fail and never mention scorers, gates, or evals in the prompt text.

Return ONE JSON object and nothing else:
{"target_file": "<the given path>", "new_content": "<full new file content>", "summary": "<one line>"}
"""


def build_patch_user_prompt(
    diagnosis: Diagnosis,
    target_file: str,
    original: str,
    *,
    card: ProblemCard | None = None,
) -> str:
    intent = {
        "problem_class": diagnosis.problem_class,
        "shift_id": diagnosis.shift_id,
        "must_improve": diagnosis.must_improve,
        "must_preserve": diagnosis.must_preserve,
        "must_not_happen": diagnosis.must_not_happen,
        "process_spec": diagnosis.spec.to_dict(),
        "rationale": diagnosis.rationale,
    }
    evidence: dict[str, Any] = {}
    if card is not None:
        evidence = {
            "card_id": card.id,
            "turns": card.turns,
            "severity": card.severity,
            "evidence": card.evidence.__dict__,
        }
    return (
        f"TARGET_FILE (edit this file and no other): {target_file}\n\n"
        f"DIAGNOSIS_JSON:\n{json.dumps(intent, indent=2)}\n\n"
        f"CARD_EVIDENCE_JSON (mined from the shift JSON; quotes are real):\n"
        f"{json.dumps(evidence, indent=2)[:6000]}\n\n"
        f"CURRENT_FILE_CONTENT:\n{original}\n\n"
        "Return the full new content of that one file as JSON."
    )


def parse_patch_response(raw: str, target_file: str) -> str:
    """Extract the single-file edit. A second file is refused, not merged."""
    payload = parse_json_object(raw)

    edits: list[dict[str, Any]]
    if isinstance(payload.get("edits"), list):
        edits = [e for e in payload["edits"] if isinstance(e, dict)]
    elif isinstance(payload.get("files"), list):
        edits = [e for e in payload["files"] if isinstance(e, dict)]
    elif isinstance(payload.get("files"), dict):
        edits = [
            {"path": k, "new_content": v} for k, v in payload["files"].items()
        ]
    else:
        edits = [
            {
                "path": payload.get("target_file")
                or payload.get("path")
                or payload.get("file")
                or target_file,
                "new_content": payload.get("new_content")
                or payload.get("content")
                or payload.get("body"),
            }
        ]

    if len(edits) != 1:
        paths = [str(e.get("path") or e.get("target_file") or "?") for e in edits]
        raise PatchRefused(
            f"one file per iteration (LOOP.md rule 5); model returned {len(edits)} "
            f"file edits: {paths}"
        )

    edit = edits[0]
    path = str(edit.get("path") or edit.get("target_file") or target_file)
    if validate_target_file(path) != target_file:
        raise PatchRefused(
            f"model edited {path!r}, but the diagnosis targets {target_file!r}"
        )
    content = edit.get("new_content") or edit.get("content") or edit.get("body")
    if not isinstance(content, str) or not content.strip():
        raise PatchRefused(f"model returned no content for {target_file}")
    return content if content.endswith("\n") else content + "\n"


def _llm_complete(system: str, user: str, *, adapter: str, model: str) -> str:
    if adapter == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if str(model).startswith("gpt-5"):
            kwargs["reasoning_effort"] = "none"
        else:
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    if adapter == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ]
        return "\n".join(parts).strip()
    raise ValueError(f"unknown adapter: {adapter}")


def _propose_content(
    diagnosis: Diagnosis,
    target_file: str,
    original: str,
    *,
    card: ProblemCard | None,
    complete_fn: CompleteFn,
) -> str:
    """One retry on unparseable JSON. Policy refusals are never retried."""
    user = build_patch_user_prompt(diagnosis, target_file, original, card=card)
    last_error: Exception | None = None
    for attempt in range(2):
        prompt = user if attempt == 0 else user + "\n\nReturn ONLY the JSON object."
        raw = complete_fn(PATCHER_SYSTEM, prompt)
        try:
            return parse_patch_response(raw, target_file)
        except PatchRefused:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            last_error = exc
    raise PatchRefused(f"model returned unusable JSON after one retry: {last_error}")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _new_variant_dir(variants_dir: Path, stamp: str) -> Path:
    dest = variants_dir / f"auto_{stamp}"
    suffix = 2
    while dest.exists():
        dest = variants_dir / f"auto_{stamp}_{suffix}"
        suffix += 1
    return dest


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def apply_patch(
    diagnosis: Diagnosis,
    *,
    parent_variant: str = "variants/baseline",
    skip_llm: bool = True,
    adapter: str = "openai",
    model: str = "gpt-5.6-sol",
    complete_fn: CompleteFn | None = None,
    card: ProblemCard | None = None,
    root: Path | None = None,
    variants_dir: Path | None = None,
    stamp: str | None = None,
) -> PatchPlan:
    """Copy the parent variant, edit exactly one file, return a `PatchPlan`.

    `skip_llm=True` applies a canned ordered-rule edit (no API call). Otherwise
    the model is asked for the full new content of `diagnosis.target_file`; the
    reply is validated in code and the variant dir is deleted on any refusal.
    """
    root = Path(root or ROOT)
    variants_dir = Path(variants_dir or (root / "variants"))

    parent_dir = Path(parent_variant)
    if not parent_dir.is_absolute():
        parent_dir = root / parent_dir
    if not (parent_dir / "core").is_dir() or not (parent_dir / "instructions").is_dir():
        raise PatchRefused(f"parent variant missing core/ or instructions/: {parent_dir}")

    target_file = validate_target_file(diagnosis.target_file)
    source_file = parent_dir / target_file
    if not source_file.is_file():
        raise PatchRefused(f"target_file not in parent variant: {source_file}")
    original = source_file.read_text(encoding="utf-8")

    variants_dir.mkdir(parents=True, exist_ok=True)
    dest = _new_variant_dir(variants_dir, stamp or _stamp())
    shutil.copytree(parent_dir, dest)

    try:
        if skip_llm:
            new_text = canned_ordered_rule(original, diagnosis)
        else:
            fn = complete_fn or (
                lambda system, user: _llm_complete(
                    system, user, adapter=adapter, model=model
                )
            )
            new_text = _propose_content(
                diagnosis, target_file, original, card=card, complete_fn=fn
            )

        check_patch_size(original, new_text, target_file)

        write_path = resolve_inside(dest, target_file)
        write_path.write_text(new_text, encoding="utf-8", newline="\n")

        touched = changed_files(parent_dir, dest)
        if touched != [target_file]:
            raise PatchRefused(
                f"one file per iteration (LOOP.md rule 5); variant differs from "
                f"parent in {touched or 'no files'}"
            )

        assert_variant_assembles(dest)
        assert_file_is_assembled(dest, target_file, new_text)

        diff = "\n".join(
            unified_diff(
                original.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{target_file}",
                tofile=f"b/{target_file}",
                lineterm="",
            )
        )
        if not diff.strip():
            raise PatchRefused(f"empty diff for {target_file}")
    except PatchRefused:
        # Refused: leave nothing half-built for the evaluator to pick up.
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - one clear error type out of here
        shutil.rmtree(dest, ignore_errors=True)
        raise PatchRefused(
            f"patch of {target_file} failed ({type(exc).__name__}): {exc}"
        ) from exc

    return PatchPlan(
        variant_dir=_display_path(dest, root),
        changed_file=target_file,
        diff=diff + "\n",
        parent_variant=parent_variant,
    )
