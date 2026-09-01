# Eval-loop agent — architecture

Self-fulfilling prompt loop: read a shift JSON, mine real problems from
`events` + `baseline`, patch one prompt file, replay, keep only if scorers
say the targeted class improved **and** holdout safety held.

This file is the contract for delegated build sessions. Implement against
`harness/agent/`. Do not invent new roles or pass/fail owners.

## Hard rules

1. **Cards come from recorded evidence, never from fiction.** Two sources and
   no others:
   * a shift JSON (`events` + `baseline`), or
   * a **FAILED deterministic scripted scenario** — photo-gamer, partial,
     pushback, hostile — whose guard side is a fixture state machine, so the
     failing turn is a real copilot decision against a frozen script.

   Never the LLM-simulated guard (`sim-*`, scorer `simulation_conduct`): that
   stays a holdout-only eval layer. Never invent guard personas, scripted
   replies, or synthetic images of your own.
2. **Photos in this bundle are `[photo]` placeholders.** You can mine
   inspect-or-not and ask-again-or-not. You cannot claim two photos were
   visually the same location shot.
3. **Deterministic scorers own PASS/FAIL.** The diagnostician and patcher
   never flip a gate. Same rule as `harness/advisor.py`.
4. **Control is same-model original prompt**, not historical production.
   Historical baseline is evidence for mining, not the live control arm.
5. **One prompt file per iteration.** Ordered rule, not a stacked addendum
   (that was Variant A2).
6. **Holdout is not the optimization target.** If the card is not an
   escalation/coverage miss, still run `a3-shift-55252` (or the catalog
   holdout) before keep.
7. **Cap iterations** (`max_iterations`, default 3). No unbounded self-play.
8. **Conduct floor (GUIDELINES.md) is a holdout; a floor break is a revert.**
   `rules-dataset` runs alongside `a3-shift-55252` before every keep.

## What improvement means (static JSON)

The shift file does not change when the prompt changes. Historical guard
messages after a new DM are **not** a response to that DM. Do not treat them
as outcomes.

**Hypothetical better** is a `ProcessSpec` on the frozen wake: given the same
as-of evidence, what process should the copilot have run? That spec lives on
the card (from `catalog.process_spec_for`). It is checkable without imagining
a new conversation.

| May claim | Must not claim |
|-----------|----------------|
| Variant called `get_guard_locations` / `fetch_chat_image` more often than same-model control on these turns | The guard would have sent a real patrol photo |
| Variant stopped a third ping / threat lexicon on these turns | The next historical guard reply "proves" the new DM worked |
| Variant flagged where control soft-DMed (holdout / under_escalation) | Theft was prevented or coverage was saved in the world |

**Concrete keep rule** (already in `policy.decide` when rates are set):

1. **Lift** — `variant_spec_rate > control_spec_rate` on the card's turns
   (same model, original prompt vs patch). Isolation pass is not lift
   (Variant C: both already 1.0).
2. **Did not break** — `preserve_pass` (welcome + guard replies) and
   `holdout_pass is not False` (55252 mid-shift, or catalog holdout).
3. **Did not get worse** — `variant_spec_rate < control_spec_rate` is revert,
   not "try another card."
4. **No headroom** — control already 1.0 → `next_card`, do not advertise a win.

Score **copilot process** only (`ProcessSpec.score_scope`). In shift mode,
keep the variant's earlier DMs in thread context if you need path dependence,
but still do not score later `events` guard text as success.

Session D must call `harness.agent.spec.spec_rate` on control vs variant
turns, then `assess_lift`, then fill `ScoreCard` rates. Do not invent a
holistic quality score.

## Pipeline

```
shift JSON
    → Miner           code, no LLM     → list[ProblemCard]
    → Diagnostician   LLM              → Diagnosis (picks one card, intent triple)
    → Patcher         LLM              → one-file variant under variants/auto_*
    → Evaluator       existing harness → ScoreCard (scorers)
    → decide()        code, no LLM     → keep | revert | next_card | stop
```

`decide()` is already implemented in `harness/agent/policy.py`. Do not
reimplement it in the patcher.

## Package

| Module | Owns | Session |
|--------|------|---------|
| `harness/agent/types.py` | Dataclasses | done (architecture) |
| `harness/agent/catalog.py` | class → scorer / recipe / holdout | done (architecture) |
| `harness/agent/policy.py` | keep / revert / stop | done (architecture) |
| `harness/agent/errors.py` | `SessionTodo` | done |
| `harness/agent/mine.py` | JSON → cards | **A** |
| `harness/agent/diagnose.py` | cards → one Diagnosis | **B** |
| `harness/agent/patch.py` | Diagnosis → one-file variant | **C** |
| `harness/agent/evaluate.py` | Diagnosis + variant → ScoreCard | **D** |
| `harness/agent/orchestrator.py` | wire + artifacts | **E** (thin; extend) |
| `cx loop <shift> [-n]` | CLI | **E** |

Dry-run (`cx loop 50737 -n`) works now. It prints the plan and session map
without calling unimplemented stages.

## Problem classes (mined, not invented)

| Class | JSON signal (miner must cite evidence) | Scorer / recipe |
|-------|----------------------------------------|-----------------|
| `unverified_claim` | Guard work-done text; baseline window lacks `get_guard_locations` before affirming DM | `verify_b` / `b-claims` |
| `photo_without_inspect` | `events[].image == "[photo]"`; baseline window has no `fetch_chat_image` | new code probe (Session A+D) |
| `hammer_after_photo` | Photo event, then another DM still asking for a photo | ping / inspect probe |
| `ping_budget` | ≥3 `request_copilot_dm` on the same obligation window | new code probe |
| `surveillance_voice` | Baseline DM matches no-surveillance / threat lexicon | extend `voice` or new probe |
| `pushback_failure` | Guard pushback text; copilot doubles down or vanishes | checklist later; code probe first |
| `under_escalation` | Silent / coverage window; no flag/escalate in baseline when policy requires it | `escalation_focus` / `a3-shift-55252` |

Every JSON-mined `ProblemCard` sets `source="json"` and fills `evidence` with
pointers into the file (`event_index`, `baseline_index`, turn numbers,
quoted text). A card without evidence is invalid.

## Scenario failures (`source="scenario"`)

A scripted scenario that FAILS is a card. `harness/agent/mine.py` reads the
sealed `recipe_score.json` plus the recorded turns and emits one card per
failed gate, with `evidence.failed_gate`, `evidence.fixture`,
`evidence.scenario_run_id`, and the failing turn's DMs and tools quoted. Each
gate maps to one class in `catalog.py` (`SCENARIO_GATES`), e.g.

| Scenario | Gate | Class | Policy files |
|----------|------|-------|--------------|
| photo-gamer | `ping_budget` | `third_ping` | `core/obligations.md`, `instructions/obligation_due.md` |
| photo-gamer | `inspected_proof` | `uninspected_photo` | `core/tools.md`, `instructions/guard_response.md` |
| pushback | `no_apology_spiral` | `caved_on_pushback` | `core/holding_the_post.md`, `core/comms_policy.md` |
| hostile | `neither_cave_nor_retaliate` | `hostile_retaliation` | `core/holding_the_post.md`, `core/comms_policy.md` |

Scenario cards are evaluated by `harness/agent/scenario_eval.py`, not by
scoring frozen historical turns:

* **targeted** — `execute_recipe(<scenario recipe>)` at the recipe's own
  `repetitions` (pass^k), control arm = parent prompt, candidate arm = the
  patched auto-variant. Pass requires the candidate to pass **and** the control
  to have failed (or failed more gates). A scenario that was already green is
  no lift.
* **holdout** — `a3-shift-55252` **plus** the other three scripted scenarios as
  conduct holdouts. Any holdout failure is `holdout_pass=False`, i.e. revert.
* **preserve** — measured on the holdout shift, not the scenario arms: the
  right fix often replaces a DM with an escalation, which is not a lost reply.

### Running it

```
cx loop --from-run runs/var_photo_gamer_20260901T215149   # self-fix a recorded failure
cx loop --from-scenario ag                                # run photo-gamer, self-fix if it fails
cx go --fix                                               # cx go, then self-fix the first failed scenario
```

`-n` stays API-free end to end: the dry patch is the canned ordered rule and
the scenario arms run against `harness.scenario.CannedAdapter`. `cx go` prints
the exact `cx loop --from-run <run_id>` line after any scenario failure, and
says "simulation-only evidence" instead for a `sim-*` failure. The loop still
never calls `promote`.

## Artifacts

Each live loop writes `runs/loop_<stamp>/`:

```
manifest.json      shift, iteration cap, model
cards.json         miner output
diagnosis.json     chosen card + must_improve / preserve / must_not
patch.diff         unified diff of the one file
score.json         ScoreCard (scorer booleans)
decision.json      keep | revert | next_card | stop + reason
```

Append-only. Do not rewrite a previous iteration in place; use
`iter_01/`, `iter_02/`, …

## Session done-when

**A Miner** — `mine_shift("50737")` returns ≥1 `unverified_claim` card with
turns that exist in the shift and `spec=process_spec_for(class)`;
`mine_shift("55252")` can return `under_escalation`; unit tests, no API.

**B Diagnostician** — given cards, returns one `Diagnosis` whose `card_id`
exists, `target_file` is a single path under `core/` or `instructions/`,
`scorer` is in `catalog.py`, and `spec` is copied from the catalog. Dry-run
(`skip_llm`) picks the highest severity card without an API call. Never sets
pass/fail.

**C Patcher** — copies `variants/baseline` to `variants/auto_<stamp>/`,
edits exactly one markdown file, returns `PatchPlan`. Refuses a second
file. Prefer an ordered rule over stacking.

**D Evaluator** — runs same-model control vs the auto-variant on the card’s
turns (or shift mode if catalog says so), plus holdout from catalog.
Computes `spec_rate` on those turns, `assess_lift`, fills `ScoreCard` rates.
Reuse `harness.recipes` / `cli.run_jobs`. Do not compare against historical
baseline as the live control. Do not use later guard events as outcomes.

**E Orchestrator** — `cx loop <shift>` runs A→B→C→D→`decide()`, writes
artifacts, stops on keep / revert-after-cap. `-n` stays API-free.

## Out of scope until the loop runs

- Guard simulators / forked replies — `harness/simulate.py` (recipe `sim-50737`,
  `cx sim`) exists as a separate eval layer, but rule 1 still holds: its runs are
  never a card, control arm, or holdout here. `harness/agent/catalog.py` must not
  reference `simulation_conduct`; `LOOP_ELIGIBLE_SCORERS` there is an allowlist
  of the four scripted-scenario scorers, so an LLM-driven layer cannot creep in.
- Visual duplicate-photo fixtures
- LLM-as-judge owning a gate
- Composite 1–5 “quality” scores
- Letting the patcher iterate after a holdout fail in the same iteration
  (revert first, then `next_card` / narrower file)
