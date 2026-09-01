"""Session C: the patcher edits exactly one file, in code-enforced bounds.

No API calls anywhere: dry mode is canned, and the LLM path is exercised by
injecting a fake `complete_fn`. Every variant dir these tests build lands under
pytest's tmp_path, never under the repo's real `variants/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent.diagnose import diagnose_deterministic
from harness.agent.mine import mine_shift
from harness.agent.patch import (
    DRY_MARKER,
    MAX_REWRITE_RATIO,
    PatchRefused,
    apply_patch,
    canned_ordered_rule,
    changed_files,
    check_patch_size,
    parse_patch_response,
    patch_size_stats,
    validate_target_file,
)
from harness.agent.types import Diagnosis, Evidence, PatchPlan, ProblemCard
from harness.prompts import (
    CONTEXT_PLACEHOLDER,
    GuardRef,
    compile_system_prompt,
    compile_turn_message,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "variants" / "baseline"


@pytest.fixture(scope="module")
def diagnosis() -> Diagnosis:
    """A real Diagnosis: mined cards from shifts/50737.json, no API."""
    return diagnose_deterministic(mine_shift("50737"))


@pytest.fixture
def variants(tmp_path: Path) -> Path:
    """Every auto_* dir a test builds lives here, not in the repo."""
    out = tmp_path / "variants"
    out.mkdir()
    return out


def _card() -> ProblemCard:
    return ProblemCard(
        id="50737-unverified_claim",
        shift_id="50737",
        turns=[6],
        problem_class="unverified_claim",
        severity="lift",
        evidence=Evidence(
            guard_text="all clear on the north lot",
            missing_tools=["get_guard_locations"],
            event_indexes=[167],
        ),
        policy_files=["instructions/guard_response.md"],
    )


def _run(diagnosis: Diagnosis, variants: Path, **kwargs) -> PatchPlan:
    return apply_patch(
        diagnosis,
        parent_variant=str(BASELINE),
        variants_dir=variants,
        **kwargs,
    )


def _fake_llm(payload: dict, calls: list | None = None):
    def complete(system: str, user: str) -> str:
        if calls is not None:
            calls.append((system, user))
        return json.dumps(payload)

    return complete


# --------------------------------------------------------------------------
# dry mode
# --------------------------------------------------------------------------


def test_dry_mode_changes_exactly_one_file_and_returns_a_valid_diff(
    diagnosis, variants
):
    plan = _run(diagnosis, variants, skip_llm=True)

    variant_dir = Path(plan.variant_dir)
    assert variant_dir.parent == variants
    assert variant_dir.name.startswith("auto_")
    assert plan.changed_file == diagnosis.target_file
    assert plan.parent_variant == str(BASELINE)

    # exactly one file differs from the parent variant
    assert changed_files(BASELINE, variant_dir) == [diagnosis.target_file]

    # the diff is a real unified diff of that one file
    lines = plan.diff.splitlines()
    assert lines[0] == f"--- a/{diagnosis.target_file}"
    assert lines[1] == f"+++ b/{diagnosis.target_file}"
    assert any(line.startswith("@@") for line in lines)
    added = [line[1:] for line in lines if line.startswith("+") and line[1:2] != "+"]
    assert DRY_MARKER in added

    # the diff reconstructs the file that was actually written
    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    written = (variant_dir / diagnosis.target_file).read_text(encoding="utf-8")
    assert written != original
    # ordered rule, not a rewrite: every original line survives
    assert all(line in written for line in original.splitlines() if line.strip())
    assert not [line for line in lines if line.startswith("-") and line[1:2] != "-"]


def test_dry_mode_is_an_ordered_rule_not_a_stacked_addendum(diagnosis, variants):
    plan = _run(diagnosis, variants, skip_llm=True)
    text = (Path(plan.variant_dir) / diagnosis.target_file).read_text(encoding="utf-8")

    assert DRY_MARKER in text
    assert "Ordered rule" in text
    assert "1. **First:**" in text and "2. **Then:**" in text

    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    # inserted near the top (ahead of the body it orders), not appended at the end
    assert text.index(DRY_MARKER) < len(original) / 2
    assert not text.rstrip().endswith(DRY_MARKER)


def test_dry_mode_makes_no_llm_call(diagnosis, variants):
    def boom(system, user):
        raise AssertionError("skip_llm=True must not call the model")

    plan = _run(diagnosis, variants, skip_llm=True, complete_fn=boom)
    assert plan.diff


def test_patched_variant_still_assembles_with_the_prompt_loader(diagnosis, variants):
    plan = _run(diagnosis, variants, skip_llm=True)
    variant_dir = Path(plan.variant_dir)

    system = compile_system_prompt(variant_dir, CONTEXT_PLACEHOLDER)
    assert "## " in system

    from datetime import datetime, timedelta, timezone

    start = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    rendered = {
        trigger: compile_turn_message(
            variant_dir,
            turn=3,
            trigger=trigger,
            ts=start,
            session_id="s",
            job_id="7",
            guards=[GuardRef(name="Ana", id=1)],
            shift_start=start,
            shift_end=start + timedelta(hours=8),
            tz_name="UTC",
        )
        for trigger in ("session_start", "guard_message", "scheduled_check_in")
    }
    assert all(rendered.values())

    # the edit actually reaches the model somewhere in the assembly
    haystack = system + "\n".join(rendered.values())
    assert DRY_MARKER in haystack


def test_repo_variants_dir_is_untouched(diagnosis, variants):
    before = sorted(p.name for p in (ROOT / "variants").iterdir())
    _run(diagnosis, variants, skip_llm=True)
    after = sorted(p.name for p in (ROOT / "variants").iterdir())
    assert before == after  # real loop runs may leave auto_* here; tests must add none


# --------------------------------------------------------------------------
# LLM path: accepted edit
# --------------------------------------------------------------------------


def test_fake_llm_single_file_edit_is_applied(diagnosis, variants):
    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    new_text = original.replace(
        original.splitlines()[0],
        original.splitlines()[0] + "\n\nCheck the data before you affirm a claim.",
        1,
    )
    calls: list = []
    plan = _run(
        diagnosis,
        variants,
        skip_llm=False,
        card=_card(),
        complete_fn=_fake_llm(
            {
                "target_file": diagnosis.target_file,
                "new_content": new_text,
                "summary": "ordered verify-first rule",
            },
            calls,
        ),
    )

    assert changed_files(BASELINE, Path(plan.variant_dir)) == [diagnosis.target_file]
    assert "Check the data before you affirm a claim." in plan.diff
    # the model was handed the file, the diagnosis, and the card evidence
    (_system, user), = calls
    assert diagnosis.target_file in user
    assert "CURRENT_FILE_CONTENT" in user and original[:40] in user
    assert "must_improve" in user and "CARD_EVIDENCE_JSON" in user
    assert "50737-unverified_claim" in user


# --------------------------------------------------------------------------
# LLM path: refusals (enforced in code, not in the prompt)
# --------------------------------------------------------------------------


def _assert_no_variant_left(variants: Path) -> None:
    assert list(variants.iterdir()) == []


def test_second_file_edit_is_refused(diagnosis, variants):
    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    payload = {
        "edits": [
            {"path": diagnosis.target_file, "new_content": original + "\nOne rule.\n"},
            {"path": "core/comms_policy.md", "new_content": "# rewritten\n"},
        ]
    }
    with pytest.raises(PatchRefused, match="one file per iteration"):
        _run(
            diagnosis,
            variants,
            skip_llm=False,
            complete_fn=_fake_llm(payload),
        )
    _assert_no_variant_left(variants)


def test_edit_to_a_different_single_file_is_refused(diagnosis, variants):
    other = (
        "core/comms_policy.md"
        if diagnosis.target_file != "core/comms_policy.md"
        else "core/tools.md"
    )
    with pytest.raises(PatchRefused, match="diagnosis targets"):
        _run(
            diagnosis,
            variants,
            skip_llm=False,
            complete_fn=_fake_llm({"target_file": other, "new_content": "# hi\n"}),
        )
    _assert_no_variant_left(variants)


def test_path_outside_the_variant_dir_is_refused(diagnosis, variants):
    for escape in ("../../etc/passwd.md", "/etc/passwd.md", "core/../../x.md"):
        with pytest.raises(PatchRefused):
            _run(
                diagnosis,
                variants,
                skip_llm=False,
                complete_fn=_fake_llm(
                    {"target_file": escape, "new_content": "# nope\n"}
                ),
            )
    _assert_no_variant_left(variants)


def test_wholesale_rewrite_is_refused(diagnosis, variants):
    rewrite = "\n".join(f"# rewritten line {i}" for i in range(60)) + "\n"
    with pytest.raises(PatchRefused, match="wholesale rewrite"):
        _run(
            diagnosis,
            variants,
            skip_llm=False,
            complete_fn=_fake_llm(
                {"target_file": diagnosis.target_file, "new_content": rewrite}
            ),
        )
    _assert_no_variant_left(variants)


def test_sharp_shrink_is_refused(diagnosis, variants):
    """Under the rewrite cap (36% of lines dropped) but still a sharp shrink."""
    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    all_lines = original.splitlines()
    kept = "\n".join(all_lines[: int(len(all_lines) * 0.64)]) + "\n"
    with pytest.raises(PatchRefused, match="shrank"):
        _run(
            diagnosis,
            variants,
            skip_llm=False,
            complete_fn=_fake_llm(
                {"target_file": diagnosis.target_file, "new_content": kept}
            ),
        )
    _assert_no_variant_left(variants)


def test_no_op_edit_is_refused(diagnosis, variants):
    original = (BASELINE / diagnosis.target_file).read_text(encoding="utf-8")
    with pytest.raises(PatchRefused, match="changed nothing"):
        _run(
            diagnosis,
            variants,
            skip_llm=False,
            complete_fn=_fake_llm(
                {"target_file": diagnosis.target_file, "new_content": original}
            ),
        )
    _assert_no_variant_left(variants)


def test_unusable_json_gets_one_retry_then_refuses(diagnosis, variants):
    calls: list = []

    def complete(system, user):
        calls.append(user)
        return "sorry, here is some prose instead"

    with pytest.raises(PatchRefused, match="unusable JSON"):
        _run(diagnosis, variants, skip_llm=False, complete_fn=complete)
    assert len(calls) == 2
    _assert_no_variant_left(variants)


def test_file_the_loader_never_assembles_is_refused(diagnosis, tmp_path, variants):
    """A prompt file the compiler would never read is not a legal target."""
    import shutil
    from dataclasses import replace

    parent = tmp_path / "parent"
    shutil.copytree(BASELINE, parent)
    (parent / "core" / "extra.md").write_text("# stray\n" * 20, encoding="utf-8")
    stray = replace(diagnosis, target_file="core/extra.md")

    with pytest.raises(PatchRefused, match="assembled system prompt"):
        apply_patch(
            stray,
            parent_variant=str(parent),
            variants_dir=variants,
            skip_llm=True,
        )
    _assert_no_variant_left(variants)


def test_bad_parent_variant_is_refused(diagnosis, tmp_path, variants):
    empty = tmp_path / "not_a_variant"
    empty.mkdir()
    with pytest.raises(PatchRefused, match="missing core/"):
        apply_patch(
            diagnosis,
            parent_variant=str(empty),
            variants_dir=variants,
            skip_llm=True,
        )
    _assert_no_variant_left(variants)


# --------------------------------------------------------------------------
# unit-level enforcement helpers
# --------------------------------------------------------------------------


def test_validate_target_file_accepts_one_prompt_file_and_rejects_the_rest():
    assert validate_target_file("instructions/guard_response.md") == (
        "instructions/guard_response.md"
    )
    assert validate_target_file("core\\tools.md") == "core/tools.md"
    for bad in (
        "",
        "README.md",
        "core/nested/tools.md",
        "notes/tools.md",
        "core/tools.txt",
        "../core/tools.md",
        "C:/tmp/tools.md",
    ):
        with pytest.raises(PatchRefused):
            validate_target_file(bad)


def test_patch_size_stats_and_cap():
    original = "\n".join(f"line {i}" for i in range(20)) + "\n"
    inserted = original.replace("line 0", "line 0\nnew ordered rule", 1)
    stats = patch_size_stats(original, inserted)
    assert stats["removed"] == 0 and stats["added"] == 1
    assert check_patch_size(original, inserted, "core/tools.md")["rewrite_ratio"] == 0.0

    half_gone = "\n".join(f"changed {i}" for i in range(20)) + "\n"
    assert patch_size_stats(original, half_gone)["rewrite_ratio"] > MAX_REWRITE_RATIO
    with pytest.raises(PatchRefused, match="wholesale rewrite"):
        check_patch_size(original, half_gone, "core/tools.md")


def test_parse_patch_response_handles_fences_and_multi_file_shapes():
    fenced = (
        "```json\n"
        + json.dumps({"target_file": "core/tools.md", "new_content": "# ok\n"})
        + "\n```"
    )
    assert parse_patch_response(fenced, "core/tools.md") == "# ok\n"

    with pytest.raises(PatchRefused, match="one file per iteration"):
        parse_patch_response(
            json.dumps({"files": {"core/tools.md": "a\n", "core/identity.md": "b\n"}}),
            "core/tools.md",
        )
    with pytest.raises(PatchRefused, match="no content"):
        parse_patch_response(json.dumps({"target_file": "core/tools.md"}), "core/tools.md")


def test_canned_rule_is_inserted_after_the_first_heading():
    original = "## Title\n\nbody line one\nbody line two\n"
    diagnosis = diagnose_deterministic([_card()])
    patched = canned_ordered_rule(original, diagnosis)
    assert patched.startswith("## Title\n")
    assert patched.index(DRY_MARKER) < patched.index("body line one")
    assert "body line two" in patched
