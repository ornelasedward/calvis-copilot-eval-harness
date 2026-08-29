# Calvis guard-copilot eval harness

Offline replay and comparison for Calvis prompt variants against historical shifts.
Custom, model-agnostic, zero real side effects.

## Quick start

```bash
py -m pip install -r requirements.txt
py -m pytest tests -q
py cli.py init-variants
py cli.py import-baseline --shifts 56370 55252 50737
```

Set `ANTHROPIC_API_KEY` (and/or `OPENAI_API_KEY`), then:

```bash
# Fast inner loop: one turn against baseline-threaded history
py cli.py run --variant variants/variant_a --mode turn --shifts 56370 --turns 29 --adapter anthropic

# Full shift replay (frozen guard messages)
py cli.py run --variant variants/variant_a --mode shift --shifts 56370 55252 --adapter anthropic

# Compare + assertions
py cli.py compare --baseline <baseline_run_id> --variant <variant_run_id> --shift 56370 --assertions experiments/assertions.json

# Dashboard
py -m harness.dashboard --baseline <baseline_run_id> --variant <variant_run_id> --shift 56370
```

## MVP shifts

| Shift | Why |
|-------|-----|
| 56370 | Rich conversation, all real trigger types |
| 55252 | Silent guard (0 messages), escalation ladder |
| 50737 | Incident-heavy (6 human escalations) |

## Variants

- `variants/variant_a` — stricter no-op discipline on scheduled wakes
- `variants/variant_b` — mandatory `get_guard_locations` before affirming work claims

## Architecture principles

- Baseline is reference, not truth
- `get_open_obligations` defaults to `data_unavailable` (empty ledger would invent "nothing owed")
- Action tools record only — never execute
- No nearest-match fixtures
- ModelAdapter is transport only; eval standard is Calvis-owned
- Run artifacts are append-only and content-hashed

See the take-home writeup for open-question answers and audit findings.
