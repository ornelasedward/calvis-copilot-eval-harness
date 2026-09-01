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
| `ag` | photo-gamer process probe (scripted guard) |
| `va`/`v2`/`v3`/`vb`/`vc` | analyze Variant A / A2 / A3 / B / C |

Plan from changed files (offline, no API):

```bash
.\cx go -n --files scheduled_check_in.md
.\cx go v3 -n
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

Deterministic scorers own **PASS/FAIL**. The advisor only narrates.

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
