"""Join table: mined problem class → scorer, recipe, holdout, policy file.

Session A cites classes from here. Session D must use these scorer names
(or add a probe and register it here + in harness.recipes.SCORERS).
"""

from __future__ import annotations

from typing import Any, Literal

from harness.agent.types import PROBLEM_CLASSES, ProblemClass, ProcessSpec, Severity

Mode = Literal["turn", "shift"]


CLASS_CATALOG: dict[ProblemClass, dict[str, Any]] = {
    "unverified_claim": {
        "severity": "lift",
        "scorer": "verify_b",
        "recipe": "b-claims",
        "holdout_recipe": "a3-shift-55252",
        "mode": "turn",
        "policy_files": ["instructions/guard_response.md"],
        "json_signal": (
            "Guard work-done / patrol claim in events; baseline window "
            "affirms without get_guard_locations."
        ),
        "process_spec": {
            "must_call": ["get_guard_locations"],
            "must_not_call": [],
            "max_dms": None,
            "min_dms": 1,
            "require_escalation": False,
            "forbid_substrings": [],
        },
    },
    "photo_without_inspect": {
        "severity": "conduct",
        "scorer": "photo_inspect",  # Session D: add to harness.recipes.SCORERS
        "recipe": None,
        "holdout_recipe": "a3-shift-55252",
        "mode": "turn",
        "policy_files": ["core/tools.md", "instructions/guard_response.md"],
        "json_signal": (
            "events[].image == '[photo]' and baseline window has no "
            "fetch_chat_image."
        ),
        "process_spec": {
            "must_call": ["fetch_chat_image"],
            "must_not_call": [],
            "max_dms": None,
            "min_dms": 1,
            "require_escalation": False,
            "forbid_substrings": [],
        },
    },
    "hammer_after_photo": {
        "severity": "conduct",
        "scorer": "photo_inspect",
        "recipe": None,
        "holdout_recipe": "a3-shift-55252",
        "mode": "shift",
        "policy_files": ["core/obligations.md", "core/comms_policy.md"],
        "json_signal": (
            "Photo event, then a later request_copilot_dm still asking "
            "for a photo."
        ),
        "process_spec": {
            "must_call": ["fetch_chat_image"],
            "must_not_call": [],
            "max_dms": 1,
            "min_dms": None,
            "require_escalation": False,
            "forbid_substrings": [],
        },
    },
    "ping_budget": {
        "severity": "conduct",
        "scorer": "ping_budget",  # Session D
        "recipe": None,
        "holdout_recipe": "a3-shift-55252",
        "mode": "shift",
        "policy_files": ["core/obligations.md", "core/comms_policy.md"],
        "json_signal": (
            "Three or more request_copilot_dm on the same obligation window."
        ),
        "process_spec": {
            "must_call": [],
            "must_not_call": [],
            "max_dms": 1,
            "min_dms": None,
            "require_escalation": False,
            "forbid_substrings": [],
        },
    },
    "surveillance_voice": {
        "severity": "compliance",
        "scorer": "voice",
        "recipe": "c-voice",
        "holdout_recipe": "a3-shift-55252",
        "mode": "turn",
        "policy_files": ["core/comms_policy.md", "core/holding_the_post.md"],
        "json_signal": (
            "Baseline DM matches threat / verdict / announced-consequence lexicon."
        ),
        "process_spec": {
            "must_call": [],
            "must_not_call": [],
            "max_dms": 1,
            "min_dms": None,
            "require_escalation": False,
            "forbid_substrings": [
                "this is a warning",
                "you'll be marked abandoned",
                "further misses will be reported",
                "i've escalated this to ops for non-compliance",
                "you're being removed",
            ],
        },
    },
    "pushback_failure": {
        "severity": "conduct",
        "scorer": "pushback",  # Session D
        "recipe": None,
        "holdout_recipe": "a3-shift-55252",
        "mode": "turn",
        "policy_files": ["core/holding_the_post.md", "instructions/guard_response.md"],
        "json_signal": (
            "Guard pushback in events; copilot DM apologizes-and-vanishes "
            "or doubles down."
        ),
        "process_spec": {
            "must_call": ["add_copilot_note"],
            "must_not_call": [],
            "max_dms": 1,
            "min_dms": 1,
            "require_escalation": False,
            "forbid_substrings": [
                "this is a warning",
                "you'll be marked abandoned",
            ],
        },
    },
    "under_escalation": {
        "severity": "safety",
        "scorer": "escalation_focus",
        "recipe": "a3-shift-55252",
        "holdout_recipe": None,  # this class *is* the holdout shift
        "mode": "shift",
        "policy_files": ["instructions/scheduled_check_in.md", "core/holding_the_post.md"],
        "json_signal": (
            "Silent or coverage-risk window; baseline did not flag/escalate "
            "when policy requires it."
        ),
        "process_spec": {
            "must_call": [],
            "must_not_call": [],
            "max_dms": None,
            "min_dms": None,
            "require_escalation": True,
            "forbid_substrings": [],
        },
    },
}

# Severity order for dry-run card pick (Session B skip_llm).
SEVERITY_RANK: dict[Severity, int] = {
    "safety": 0,
    "conduct": 1,
    "lift": 2,
    "compliance": 3,
}

SESSION_MAP: dict[str, str] = {
    "A": "harness.agent.mine.mine_shift",
    "B": "harness.agent.diagnose.diagnose",
    "C": "harness.agent.patch.apply_patch",
    "D": "harness.agent.evaluate.evaluate_diagnosis",
    "E": "harness.agent.orchestrator.run_loop (live path)",
}


def catalog_entry(problem_class: ProblemClass) -> dict[str, Any]:
    return CLASS_CATALOG[problem_class]


def process_spec_for(problem_class: ProblemClass) -> ProcessSpec:
    raw = CLASS_CATALOG[problem_class].get("process_spec") or {}
    return ProcessSpec(
        must_call=list(raw.get("must_call") or []),
        must_not_call=list(raw.get("must_not_call") or []),
        max_dms=raw.get("max_dms"),
        min_dms=raw.get("min_dms"),
        require_escalation=bool(raw.get("require_escalation")),
        forbid_substrings=list(raw.get("forbid_substrings") or []),
    )


def assert_catalog_complete() -> None:
    missing = [c for c in PROBLEM_CLASSES if c not in CLASS_CATALOG]
    if missing:
        raise RuntimeError(f"CLASS_CATALOG missing classes: {missing}")
    no_spec = [c for c in PROBLEM_CLASSES if "process_spec" not in CLASS_CATALOG[c]]
    if no_spec:
        raise RuntimeError(f"CLASS_CATALOG missing process_spec: {no_spec}")
