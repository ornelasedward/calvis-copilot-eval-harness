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
.\cx go v3 -n         # compiler plan for Variant A3 (no LLM)
.\cx go v3 -n --rank  # compiler + optional LLM ranker (catalog still constrains)
.\cx why v3           # advisor on Variant A3 diff
.\cx why vb -n        # advisor on B, no LLM
```

| Code | Means |
|------|--------|
| `wl` | welcome / smoke sanity |
| `cl` | claims verification (Variant B) |
| `es` | escalation safety full-shift (A3) |
| `qt` | quietness probe (A3) |
| `vo` | voice compliance (Variant C) |
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

- On push/PR to `main` or `staging`: pytest + `calvis.py ls` + dry-run `t cl` / `why vb`
- Manual **workflow_dispatch** with `live_recipes=true`: runs `t wl` if `OPENAI_API_KEY` repo secret is set

```bash
# Local mirror of CI offline gate
py -m pytest tests -q
py calvis.py t cl -n
```

## Recipes (see `experiments/recipes.json`)

| Name | What it checks |
|------|----------------|
| `smoke-welcome` | Turn 1 welcome preserved |
| `b-claims` | Work-claim verification lift (`verify_b`) |
| `a3-shift-55252` | No missed escalations on 55252 turns 5–9 |
| `a3-quiet-probe` | Quietness no-regression on clear no-op turns |

Deterministic scorers own **PASS/FAIL**. The advisor only narrates. The optional
`cx go --rank` ranker only reorders/drops/pulls recipe cards; it is never a verdict.

`cx go` is the deterministic compiler: it reads recipe cards + the prompt diff and
emits `{must_run, should_run, skip, order, budget_usd}`. `smoke-welcome` stays first.
`--rank` (off by default) asks a **different** model (`defaults.router.model` in
`experiments/recipes.json`, not the copilot under test) to propose a revised plan.
A code validator then re-inserts dropped `must_run`, strips unknown ids, re-applies
the budget cap, and keeps `smoke-welcome` first. On LLM/JSON failure after one retry
the compiler plan is used and the fallback is noted. `cx go -n --rank` prints both
plans and a diff; omitting `--rank` is compiler-only. The ranker never declares
pass/fail.

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
