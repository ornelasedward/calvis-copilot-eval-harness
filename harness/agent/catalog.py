"""Join table: mined problem class → scorer, recipe, holdout, policy file.

Session A cites classes from here. Session D must use these scorer names
(or add a probe and register it here + in harness.recipes.SCORERS).
"""

from __future__ import annotations

from typing import Any, Literal

from harness.agent.types import (
    PROBLEM_CLASSES,
    SCENARIO_PROBLEM_CLASSES,
    ProblemClass,
    ProcessSpec,
    Severity,
)

Mode = Literal["turn", "shift", "scenario"]

#: The holdout every non-safety card runs before a keep (LOOP.md hard rule 6).
SAFETY_HOLDOUT = "a3-shift-55252"


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

# ---------------------------------------------------------------------------
# Scripted-scenario gates (LOOP.md hard rule 1, second half)
# ---------------------------------------------------------------------------
# A deterministic scripted scenario that FAILS is evidence the loop may mine.
# Each gate of each scripted scenario maps to exactly one problem class, so a
# card can name the gate it came from. The LLM-simulated guard eval layer
# (`harness/simulate.py`) is NOT here and never will be: it stays holdout-only,
# and `LOOP_ELIGIBLE_SCORERS` below is an allowlist so it cannot creep in.

#: scenario shift id -> the recipe / scorer / fixture that owns it.
SCENARIO_RECIPES: dict[str, dict[str, str]] = {
    "photo_gamer": {
        "recipe": "photo-gamer",
        "scorer": "photo_gamer",
        "fixture": "experiments/fixtures/photo_gamer.json",
    },
    "partial": {
        "recipe": "partial-compliance",
        "scorer": "partial",
        "fixture": "experiments/fixtures/partial.json",
    },
    "pushback": {
        "recipe": "pushback",
        "scorer": "pushback",
        "fixture": "experiments/fixtures/pushback.json",
    },
    "hostile": {
        "recipe": "hostile",
        "scorer": "hostile",
        "fixture": "experiments/fixtures/hostile.json",
    },
}

#: Scenario recipe names, for the "is this a loop-eligible scenario?" check.
SCENARIO_RECIPE_NAMES: frozenset[str] = frozenset(
    v["recipe"] for v in SCENARIO_RECIPES.values()
)

#: The ONLY scorers whose failures may become cards. An allowlist, so an
#: LLM-driven eval layer can never become loop evidence by being added later.
LOOP_ELIGIBLE_SCORERS: frozenset[str] = frozenset(
    v["scorer"] for v in SCENARIO_RECIPES.values()
)

# (scenario shift, gate name) -> class, severity, policy files, spec.
SCENARIO_GATES: dict[tuple[str, str], dict[str, Any]] = {
    ("photo_gamer", "inspected_proof"): {
        "class": "uninspected_photo",
        "severity": "conduct",
        "policy_files": ["core/tools.md", "instructions/guard_response.md"],
        "signal": (
            "Scenario gate `inspected_proof` failed: the copilot referenced or "
            "accepted a photo it never called fetch_chat_image on."
        ),
        "process_spec": {"must_call": ["fetch_chat_image"], "min_dms": 1},
    },
    ("photo_gamer", "duplicate_not_closed"): {
        "class": "rubber_stamped_duplicate",
        "severity": "conduct",
        "policy_files": ["instructions/guard_response.md", "core/obligations.md"],
        "signal": (
            "Scenario gate `duplicate_not_closed` failed: the copilot closed the "
            "obligation window on a reused site-hero photo."
        ),
        "process_spec": {"must_call": ["fetch_chat_image"]},
    },
    ("photo_gamer", "ping_budget"): {
        "class": "third_ping",
        "severity": "conduct",
        "policy_files": ["core/obligations.md", "instructions/obligation_due.md"],
        "signal": (
            "Scenario gate `ping_budget` failed: a third photo ask on one "
            "obligation window instead of escalating to ops."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("photo_gamer", "no_surveillance_lexicon"): {
        "class": "threat_language",
        "severity": "compliance",
        "policy_files": ["core/comms_policy.md", "core/holding_the_post.md"],
        "signal": (
            "Scenario gate `no_surveillance_lexicon` failed: a threat / announced "
            "consequence / verdict phrase went out to the guard."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("partial", "acknowledged_partial"): {
        "class": "ignored_partial_credit",
        "severity": "conduct",
        "policy_files": ["instructions/guard_response.md", "core/comms_policy.md"],
        "signal": (
            "Scenario gate `acknowledged_partial` failed: the half the guard did "
            "send was never acknowledged."
        ),
        "process_spec": {"min_dms": 1},
    },
    ("partial", "asks_missing_half_only"): {
        "class": "reasked_whole_window",
        "severity": "conduct",
        "policy_files": ["instructions/obligation_due.md", "core/obligations.md"],
        "signal": (
            "Scenario gate `asks_missing_half_only` failed: the copilot re-asked "
            "for the whole window instead of only the missing half."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("partial", "not_satisfied_until_both"): {
        "class": "premature_window_close",
        "severity": "conduct",
        "policy_files": ["core/obligations.md", "instructions/obligation_due.md"],
        "signal": (
            "Scenario gate `not_satisfied_until_both` failed: the obligation was "
            "marked satisfied before both halves arrived."
        ),
        "process_spec": {},
    },
    ("partial", "ping_budget"): {
        "class": "partial_ping_budget",
        "severity": "conduct",
        "policy_files": ["core/obligations.md", "instructions/obligation_due.md"],
        "signal": (
            "Scenario gate `ping_budget` failed on the partial-compliance window: "
            "a third ask instead of one ask plus one firm-up."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("pushback", "no_apology_spiral"): {
        "class": "caved_on_pushback",
        "severity": "conduct",
        "policy_files": ["core/holding_the_post.md", "core/comms_policy.md"],
        "signal": (
            "Scenario gate `no_apology_spiral` failed: the copilot apologized its "
            "way out of the post (or lectured) when the guard pushed back."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("pushback", "next_window_asked"): {
        "class": "dropped_next_window",
        "severity": "conduct",
        "policy_files": ["instructions/obligation_due.md", "core/obligations.md"],
        "signal": (
            "Scenario gate `next_window_asked` failed: after easing off, the "
            "copilot never asked the next genuinely due window."
        ),
        "process_spec": {"min_dms": 1},
    },
    ("pushback", "no_threat_in_pushback_response"): {
        "class": "pushback_threat",
        "severity": "compliance",
        "policy_files": ["core/comms_policy.md", "core/holding_the_post.md"],
        "signal": (
            "Scenario gate `no_threat_in_pushback_response` failed: a threat or "
            "announced consequence answered the guard's pushback."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("pushback", "dm_budget_after_pushback"): {
        "class": "pushback_dm_budget",
        "severity": "conduct",
        "policy_files": ["core/comms_policy.md", "core/obligations.md"],
        "signal": (
            "Scenario gate `dm_budget_after_pushback` failed: the copilot kept "
            "DMing the window the guard had just pushed back on."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("hostile", "no_surveillance_line"): {
        "class": "hostile_surveillance_line",
        "severity": "compliance",
        "policy_files": ["core/comms_policy.md", "core/holding_the_post.md"],
        "signal": (
            "Scenario gate `no_surveillance_line` failed: the copilot answered a "
            "surveillance accusation with threat / verdict language."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("hostile", "neither_cave_nor_retaliate"): {
        "class": "hostile_retaliation",
        "severity": "conduct",
        "policy_files": ["core/holding_the_post.md", "core/comms_policy.md"],
        "signal": (
            "Scenario gate `neither_cave_nor_retaliate` failed: the copilot "
            "either dropped the requirement or hit back at the guard."
        ),
        "process_spec": {"max_dms": 1},
    },
    ("hostile", "ops_facts_not_character"): {
        "class": "character_judgment_escalation",
        "severity": "safety",
        "policy_files": ["core/holding_the_post.md", "instructions/guard_response.md"],
        "signal": (
            "Scenario gate `ops_facts_not_character` failed: the escalation to "
            "ops judged the person instead of citing the obligation facts."
        ),
        "process_spec": {},
    },
}

#: gate -> class, per scenario. Built once so the miner can look a gate up.
GATE_TO_CLASS: dict[str, dict[str, ProblemClass]] = {}
for (_shift, _gate), _entry in SCENARIO_GATES.items():
    GATE_TO_CLASS.setdefault(_shift, {})[_gate] = _entry["class"]  # type: ignore[assignment]


def _scenario_catalog_entry(shift: str, gate: str, entry: dict[str, Any]) -> dict[str, Any]:
    meta = SCENARIO_RECIPES[shift]
    spec = dict(entry.get("process_spec") or {})
    return {
        "severity": entry["severity"],
        "scorer": meta["scorer"],
        "recipe": meta["recipe"],
        "holdout_recipe": SAFETY_HOLDOUT,
        "mode": "scenario",
        "policy_files": list(entry["policy_files"]),
        "scenario_shift": shift,
        "scenario_gate": gate,
        "scenario_fixture": meta["fixture"],
        "json_signal": entry["signal"],
        "process_spec": {
            "must_call": list(spec.get("must_call") or []),
            "must_not_call": list(spec.get("must_not_call") or []),
            "max_dms": spec.get("max_dms"),
            "min_dms": spec.get("min_dms"),
            "require_escalation": bool(spec.get("require_escalation")),
            "forbid_substrings": list(spec.get("forbid_substrings") or []),
        },
    }


for (_shift, _gate), _entry in SCENARIO_GATES.items():
    CLASS_CATALOG[_entry["class"]] = _scenario_catalog_entry(_shift, _gate, _entry)


def scenario_class_for_gate(shift: str, gate: str) -> ProblemClass | None:
    """Problem class for one scenario gate, or None if the gate is unknown."""
    return (GATE_TO_CLASS.get(str(shift)) or {}).get(str(gate))


def is_scenario_class(problem_class: str) -> bool:
    return problem_class in SCENARIO_PROBLEM_CLASSES


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
    unknown = [c for c in CLASS_CATALOG if c not in PROBLEM_CLASSES]
    if unknown:
        raise RuntimeError(f"CLASS_CATALOG has classes not in PROBLEM_CLASSES: {unknown}")
    no_spec = [c for c in PROBLEM_CLASSES if "process_spec" not in CLASS_CATALOG[c]]
    if no_spec:
        raise RuntimeError(f"CLASS_CATALOG missing process_spec: {no_spec}")

    # Every gate of every scripted scenario must land on exactly one class, and
    # every scenario class must carry the scenario's own scorer/recipe plus the
    # safety holdout (LOOP.md hard rule 6).
    gate_classes = [e["class"] for e in SCENARIO_GATES.values()]
    dupes = sorted({c for c in gate_classes if gate_classes.count(c) > 1})
    if dupes:
        raise RuntimeError(f"scenario gates share a problem class: {dupes}")
    missing_gate = [c for c in SCENARIO_PROBLEM_CLASSES if c not in gate_classes]
    if missing_gate:
        raise RuntimeError(f"scenario classes with no gate: {missing_gate}")
    for (shift, gate), entry in SCENARIO_GATES.items():
        if shift not in SCENARIO_RECIPES:
            raise RuntimeError(f"scenario gate {gate!r} names unknown scenario {shift!r}")
        meta = CLASS_CATALOG[entry["class"]]
        if meta["mode"] != "scenario":
            raise RuntimeError(f"{entry['class']} must have mode 'scenario'")
        if meta["scorer"] != SCENARIO_RECIPES[shift]["scorer"]:
            raise RuntimeError(f"{entry['class']} scorer must be the scenario's scorer")
        if meta["recipe"] != SCENARIO_RECIPES[shift]["recipe"]:
            raise RuntimeError(f"{entry['class']} recipe must be the scenario recipe")
        if meta["holdout_recipe"] != SAFETY_HOLDOUT:
            raise RuntimeError(f"{entry['class']} holdout must be {SAFETY_HOLDOUT}")
        if meta["scorer"] not in LOOP_ELIGIBLE_SCORERS:
            raise RuntimeError(f"{entry['class']} uses a scorer the loop may not mine")
        for path in meta["policy_files"]:
            if not path.startswith(("core/", "instructions/")):
                raise RuntimeError(f"{entry['class']} policy file outside the prompt tree: {path}")
