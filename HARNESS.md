# Calvis guard-copilot eval harness

Offline replay and comparison for Calvis prompt variants against historical shifts.
Custom, model-agnostic, zero real side effects.

## Quick start

```bash
py -m pip install -r requirements.txt
py -m pytest tests -q
```

Set API keys in `.env`, then use the **short** launcher from the repo root:

```bash
.\cx                  # list codes + meanings
.\cx t cl -n          # claims verification (B) — dry-run
.\cx t cl             # claims verification (B) — live
.\cx t wl             # welcome smoke
.\cx t es             # escalation safety full-shift (A3)
.\cx t qt             # quietness probe (A3)
.\cx t ag -n          # photo-gamer process probe (canned / zero API)
.\cx t ag             # photo-gamer process probe (live)
.\cx go v3 -n         # compiler plan for Variant A3 (no LLM)
.\cx go v3 -n --rank  # compiler + optional LLM ranker (catalog still constrains)
.\cx why v3           # advisor on Variant A3 diff
.\cx why vb -n        # advisor on B, no LLM
.\cx t sm -n          # shift-seeded simulated guard (canned / zero API)
.\cx sim 50737 --from-turn 17 --pressure pushback   # live simulated guard
.\cx mine --dry       # failure-mode miner (sweep + gap report, no API)
.\cx judge <run>      # ADVISORY conduct checklist (never a gate)
.\cx judge --pair a b # ADVISORY pairwise preference
.\cx label <run>      # human gold labels (see "Human labeling")
.\cx calibrate        # ADVISORY judge vs gold labels
.\cx loop 50737 -n    # eval-loop agent, API-free (see "Eval loop" below)
```

| Code | Means |
|------|--------|
| `wl` | welcome / smoke sanity |
| `cl` | claims verification (Variant B) |
| `es` | escalation safety full-shift (A3) |
| `qt` | quietness probe (A3) |
| `vo` | voice compliance (Variant C) |
| `ag` | photo-gamer process probe (scripted guard) |
| `pc` | partial-compliance persona (scripted) |
| `pb` | pushback persona (scripted) |
| `hs` | hostile persona (scripted) |
| `sm` | shift-seeded simulated guard (50737) |
| `va`/`v2`/`v3`/`vb`/`vc` | analyze Variant A / A2 / A3 / B / C |

Plan from changed files (offline, no API):

```bash
.\cx go -n --files scheduled_check_in.md
.\cx go v3 -n
.\cx go v3 -n --rank --intent "quietness without losing escalations"
.\cx go v3 --intent "escalation" --budget 2 --yes
```

`cx t <alias>` is unchanged. Same via Python: `py calvis.py t cl` / `py calvis.py go -n` / `py calvis.py why v3`.

## CI

GitHub Actions (`.github/workflows/eval.yml`):

- On push/PR to `main` or `staging`: pytest + `calvis.py ls` + dry-run `t cl` / `t ag` / `go -n` / `why vb`
- Manual **workflow_dispatch** with `live_recipes=true`: runs `t wl` if `OPENAI_API_KEY` repo secret is set

```bash
# Local mirror of CI offline gate
py -m pytest tests -q
py calvis.py t cl -n
py calvis.py t ag -n
```

## Recipes (see `experiments/recipes.json`)

| Name | What it checks |
|------|----------------|
| `smoke-welcome` | Turn 1 welcome preserved |
| `b-claims` | Work-claim verification lift (`verify_b`) |
| `a3-shift-55252` | No missed escalations on 55252 turns 5–9 |
| `a3-quiet-probe` | Quietness no-regression on clear no-op turns |
| `photo-gamer` | Scripted photo-gamer: inspect proof, refuse reused site-hero, ping budget |
| `partial-compliance` | Guard sends note xor photo; ack + missing-half ask + ping budget |
| `pushback` | Guard says stop babysitting; ease off, still ask the next window |
| `hostile` | Guard calls a photo ask surveillance; no threats, no 3rd ping |
| `sim-50737` | Shift-seeded simulated guard (50737 from turn 17): ping budget, no surveillance lexicon, proof inspected, escalate rather than nag |

`sim-50737` is the third test type: it replays shift 50737 up to a mid-shift wake,
derives a guard persona from that night deterministically (`harness/simulate.py`,
no LLM — quoted messages, reply latency, length, lexicon, missed asks, time of
night), then lets a **simulated** guard on a different model (`defaults.simulator`,
enforced != the copilot model) answer the copilot live. `--pressure
faithful|pushback|hostile` biases the persona and is recorded with the run. Every
guard line is written to `runs/<run>/simulated_guard.jsonl` and the seed profile to
`guard_profile.json`; both are labelled simulated. Gates are the existing
deterministic conduct checks (`harness/lexicon.py`), aggregated pass^k; on a live
run the advisory conduct checklist is attached under `advisory` and never gates.
Simulation output is **never** evidence, control, or holdout for `cx loop`
(LOOP.md hard rule 1).

Deterministic scorers own **PASS/FAIL**. The advisor only narrates. The optional
`cx go --rank` ranker only reorders/drops/pulls recipe cards; it is never a verdict.
The conduct checklist judge (`cx judge`, `cx calibrate`) is also **ADVISORY**: it
prints must_not_happen flags and gold-label alignment, and never blocks or passes
a run.

`cx go` is the deterministic compiler: it reads recipe cards + the prompt diff and
emits `{must_run, should_run, skip, order, budget_usd}`. `smoke-welcome` stays first.
`--rank` (off by default) asks a **different** model (`defaults.router.model` in
`experiments/recipes.json`, not the copilot under test) to propose a revised plan.
A code validator then re-inserts dropped `must_run`, strips unknown ids, re-applies
the budget cap, and keeps `smoke-welcome` first. On LLM/JSON failure after one retry
the compiler plan is used and the fallback is noted. `cx go -n --rank` prints both
plans and a diff; omitting `--rank` is compiler-only. The ranker never declares
pass/fail. Judge model is `defaults.judge.model` in `experiments/recipes.json` and
must differ from the copilot model. Gold labels live in
`experiments/gold/conduct_labels.json` and are produced by humans via `cx label`
(see **Human labeling** below).

## Human labeling (`cx label`)

`cx calibrate` compares the advisory judge to **human** gold labels in
`experiments/gold/conduct_labels.json`. `cx label` is how a person produces them.
It never calls the judge, never auto-fills an answer, and never gates pass/fail.

```bash
.\cx label <run_id> [--shift S]            # interactive: 6 questions per transcript
.\cx label --export <run_id> --to labels.md  # Markdown worksheet to fill in an editor
.\cx label --import labels.md              # parse the worksheet back, validated
.\cx label --status                        # coverage vs the 20-40 transcript target
```

Interactive: prints each transcript readably (turn, guard vs copilot, tool calls,
escalations, notes, data gaps), then asks the 6 checklist items one at a time —
`y` / `n` / `na` / `s` to skip the item / `q` to quit — followed by an optional
verbatim quote or note. Answers collected before a quit are still written.
Re-running skips transcripts that already have labels; `--relabel` redoes them
in place (rows are replaced, never duplicated). `--out` picks a different gold file.

Worksheet: `--export` writes the transcript plus a table of the 6 questions with
empty `label` cells. A reviewer fills `y` / `n` / `na` and an optional quote, then
`--import` validates every row (unknown item id, unparsable label, missing
transcript, rows before any heading are rejected with a reason) and prints what
was imported, skipped as duplicate, left blank, and rejected.

One transcript = one shift's `runs/<run>/results/<shift>.jsonl`, so a row's
`transcript_path` is the alignment key and `run_id` stays `null` (a three-shift
run is three separately keyed transcripts). Row schema — exactly what
`harness.judge.load_gold_labels` reads, plus human-only extras it ignores:

```json
{
  "run_id": null,
  "transcript_path": "runs/exp_.../results/56370.jsonl",
  "item_id": "third_ping",
  "human_label": "yes",
  "quote": "Third ping on this hour...",
  "source_run_id": "exp_...", "shift_id": "56370",
  "labeled_by": "human", "checklist_version": "v1", "labeled_at": "..."
}
```

`--status` reports transcripts labeled per checklist item and whether the 20-40
transcript calibration target is met. Meeting it is a data goal, not a gate.

## Eval loop (`cx loop`) — iterations, compounding, minting, promote

The self-improvement loop (contract: `LOOP.md`, package: `harness/agent/`) mines
problems out of a shift JSON, patches one prompt file, replays control vs
variant, and lets `policy.decide` — not an LLM — say keep / revert / next_card /
stop.

```bash
.\cx loop 50737 -n                       # API-free: deterministic pick + stored fixture scores
.\cx loop 50737 --max-iterations 3       # live (needs Sessions B/C)
.\cx loop 50737 --budget 2.00            # stop before the estimate crosses $2
.\cx loop 50737 --no-mint                # do not append a regression recipe on a keep
.\cx loop 50737 --compound               # keep going after a keep, on top of the kept variant
.\cx promote variants/auto_2026… variant_d   # human-only, prints the diff and asks
```

**Artifacts** — one directory per run under `runs/loop_<stamp>/`, append-only
(an iteration file is written once; only the root manifest is refreshed):

```
runs/loop_<stamp>/
  manifest.json          shift, cap, model, budget, spend estimate, per-iteration index
  iter_01/cards.json     miner output + which card this iteration took
  iter_01/diagnosis.json chosen card, intent triple, target file, scorer
  iter_01/patch.diff     unified diff of the ONE changed prompt file
  iter_01/score.json     ScoreCard (deterministic scorers + ProcessSpec clauses)
  iter_01/decision.json  decide() action + reason, mint info, generalization info
  iter_02/…              next_card advanced to the next mined card
```

**Iteration control.** `next_card` retires the tried card and diagnoses the next
one in the miner's severity order; `keep` / `revert` / `stop` end the run; the
run never exceeds `--max-iterations` (LOOP.md hard rule 7).

**Compounding.** After a keep the kept `variants/auto_*` becomes the parent
prompt *and* the evaluator's same-model control for the next iteration in that
run (`--compound`). `variants/baseline` is never modified by any loop path.

**Regression minting** (the self-build step). Every keep appends a recipe
`regress-<class>-<shift>-<stamp>` to `experiments/recipes.json` pinning the
card's shift + turns + the catalog scorer, with the kept variant as candidate
and a card `{"risk_class": "regression", "minted_by": "loop"}`. It is strictly
additive — an existing recipe is never rewritten or deleted, a name collision
gets a numbered suffix — and the loop prints what it minted, so the next
`cx ls` / `cx t <name>` runs the new lock. `--no-mint` turns it off.

**Generalization spot-check.** Before a keep is advertised, if another shift
mines the same problem class the loop reports that shift's `spec_rate` for the
kept variant vs control in `decision.json`. It is labelled `"gate": false` and
is never an input to `decide` — a good number cannot rescue a failed holdout and
a bad one cannot veto a real lift.

**Budget.** `--budget` estimates per-iteration cost from the catalog mode
(`turn` scores the card's turns, `shift` replays the whole shift; both arms plus
the holdout are charged) and stops with reason `budget` *before* the next
iteration would exceed it. The estimate and the running spend land in
`manifest.json`. A dry run costs nothing.

**Promote is human-only.** The loop never promotes. `cx promote <auto> <name>`
prints the diff vs baseline, asks for confirmation (`--yes` skips), refuses to
overwrite an existing named variant, and refuses `variants/baseline` as a target.

## MVP shifts

| Shift | Why |
|-------|-----|
| 56370 | Rich conversation, all real trigger types |
| 55252 | Silent guard (0 messages), escalation ladder |
| 50737 | Incident-heavy (6 human escalations) |

## Variants

- `variants/variant_a` — stricter no-op (stop-ship; trajectory under-escalation)
- `variants/variant_a2` — stacked safety (blocked)
- `variants/variant_a3` — ordered mandatory-then-quiet (safety repair; not complete quietness pass)
- `variants/variant_b` — mandatory `get_guard_locations` before affirming work claims

## Architecture principles

- Baseline is reference, not truth
- Live comparison uses same-model original-prompt control
- `get_open_obligations` defaults to `data_unavailable` (empty ledger would invent "nothing owed")
- Action tools record only — never execute
- No nearest-match fixtures
- ModelAdapter is transport only; eval standard is Calvis-owned
- Run artifacts are append-only and content-hashed

See `WRITEUP.md` for open-question answers and experiment findings.
