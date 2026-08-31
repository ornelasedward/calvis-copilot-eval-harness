# Calvis take-home writeup

## What I built

A small offline eval harness that lets you change a Guard Copilot prompt, replay it against historical shifts, and see how the behavior changed. Nothing is shipped to production and no real actions are taken.

You point it at a variant folder, pick turns or a full shift, run the same model on the original prompt and the edit, then compare decisions (DM / note / escalate / quiet), tools used, and cost. Recipes and a short CLI (`cx` / `calvis.py`) wrap the common checks. Unit tests and a GitHub Action cover the offline path.

[GitHub repository](https://github.com/ornelasedward/calvis-copilot-eval-harness)  
See `HARNESS.md` for the full setup.

---

## How to run (short)

```bash
py -m pip install -r requirements.txt
py -m pytest tests -q
# put OPENAI_API_KEY / ANTHROPIC_API_KEY in .env

.\cx                  # list recipe codes
.\cx t cl -n          # Variant B claims check (dry-run)
.\cx t cl             # live Sol run
.\cx t es             # A3 escalation full-shift recipe
.\cx why v3 -n        # explain A3 prompt diff (no LLM)
```

Artifacts land under `runs/` (gitignored). Scores and reviews are JSON next to those runs.

---

## The story (what I actually did)

### 1. Read the bundle, then built the foundation

I started with the README and `prompts/PROMPTS.md`. The system has one agent. It wakes on a schedule or when a guard sends a message. Its prompt is assembled from `core/` and `instructions/`. Each shift includes anonymized events and a historical record of what the production copilot did.

I first built the basic replay flow. It loads shifts, rebuilds the wake schedule, compiles prompts, and returns recorded tool data only when the tool name and input match exactly. If obligations data is missing, the harness reports it as unavailable instead of treating it as an empty list. DMs, notes, and escalations are recorded but never sent.

I used Fable 5 and GPT-5.6 high to review the design and check the data. For example, shift 53658 skips turn 11, and turns cannot be assigned using array order alone. I added tests for those cases before running prompt experiments.

### 2. First live runs exposed the wrong control

The first runs compared a new prompt with the **historical baseline**. That comparison mixed together a prompt change and a model change, so it could not show what caused the result.

I also tested **gpt-4.1-mini**. It sometimes described an action in text without calling the required tool. For example, it could say it would escalate without making the flag call. That made a run look quiet even though the model had not completed the task. I changed the experiment in two ways.

1. **Live control = original prompt on the same model**, not historical production output.
2. **Validate tool use**, not just prose. If the model doesn’t call the tools, you aren’t measuring the prompt.

**Claude Opus** completed turns and tool calls successfully. **gpt-5.6-sol** was used for the main comparisons because its tool calling was more reliable and it worked well for repeated runs. The provider tests show that both adapters work. They do not show that both providers behave the same way.

### 3. Goals for the two prompt edits

I made two small prompt changes. Each one changed a single instruction file so the result would be easier to explain.

| Variant | File changed | Intent |
|---------|--------------|--------|
| **B** | `guard_response.md` | Before affirming a patrol / work claim, check location (or job data). Don’t rubber-stamp. |
| **A** (then A2, A3) | `scheduled_check_in.md` | Be quieter when nothing is owed and avoid unnecessary check-in DMs. |

I defined a pass before running each edit.

- **Must preserve** the welcome, replies to guard messages, and escalations when they are due.
- **Must improve** the behavior targeted by the prompt change.
- **Must not happen** missed escalations, a soft DM in place of escalation, or a claim that nothing is owed when data is missing.

If must_not_happen failed, the variant was stop-ship / blocked even if the targeted win showed up on a few turns.

### 4. Variant B passed the targeted test

I selected work-claim turns on **56370** and **50737**, ran same-model Sol control vs B, and scored whether the arm called verification tools before affirming.

The verification rate went from roughly 0.75 to 1.0 on one set and 0.67 to 1.0 on the other. Both versions replied to every guard message and neither added an escalation. I repeated selected turns three times and the result held.

**Result** B provisionally passes the targeted test. The change worked on the selected turns. The sample is not large enough for production approval.

### 5. Variant A passed some turn checks but failed the full shift

Quietness was harder to test. Shift **55252** follows an escalation ladder for a silent guard. It is not a good example of when the copilot should stay quiet, but it is useful for checking safety.

In the **isolated turn tests**, A preserved most escalations. On **50737 t10**, the control sent a DM in all three runs and A stayed quiet in all three runs.

The **full shift** on 55252 gave a different result. The control escalated four to six times. A sent a DM or added a note on turns where the control escalated. Each of the three full-shift comparisons missed at least one important escalation in the middle of the shift. The exact turn changed between runs, but the same type of failure appeared each time.

This was the main finding. **A prompt can pass an isolated turn and still be less safe over a full conversation.** Earlier messages and actions affect later decisions, so the harness needs both replay modes.

I reviewed the missed turns. They still used `scheduled_check_in.md`, obligations data was unavailable, and the model chose a soft DM instead of a flag. I then followed the loop described. I changed the prompt, tested it, reviewed the regression, and revised it.

### 6. A2 was blocked and A3 repaired the mid-shift issue

**A2** stacked “be quiet” under a safety addendum. The missed escalation appeared in one of three runs. It also added escalations and lost the 50737 quietness result. **Blocked.**

**A3** replaced the competing instructions with one ordered rule. It checks the five required conditions first. If any condition needs action, it follows the escalation ladder. It only stays quiet after those checks are clear.

On 55252 ×3, A3 **cleared the missed-escalation check from t5 through t9**. Extra escalations compared with the control were earlier responses to repeated non-response. I reviewed them as necessary. The welcome also passed.

Two checks still failed.

- A stable quietness improvement over the original prompt (screened all labeled discretionary-DM candidates; no clean control-DM / A3-quiet 3/3).
- A clean ending. On 2/3 full shifts, t10 and t11 softened while the control still flagged (`default.md`, path after mid-shift escalations).

**Result** A3 repairs the mid-shift safety issue introduced by A. It does not pass the full quietness test.

### 7. Packaging

Recipes (`experiments/recipes.json`), short codes (`wl` / `cl` / `es` / `qt` / `vo`), an advisor that narrates diffs but **never** overrides pass/fail, and CI on staging for pytest + dry-run.

### 8. Variant C — the one that actually passes clean

(Last minute change I promise.)

Everything above is the harness catching problems: A is stop-ship, A2 is blocked, A3 is a partial repair, B is a provisional pass. Useful, but it's all one shape — the tool saying no. I wanted to show the other shape too: a change that the tool says **yes** to, cleanly, and for the right reason.

So Variant C. It changes one file, `core/comms_policy.md`, and hardens the voice rules that are already written there into a pre-send self-check: no em-dashes, no sign-off filler ("let me know," "feel free," "hope this helps"), one DM per turn. Nothing about *what* the agent decides, only *how* it reads.

**What it tests for.** A deterministic `voice` scorer (recipe `c-voice`, code `vo`) counts three things across delivered DMs: em-dash / en-dash characters, banned sign-off filler, and turns that sent more than one DM. The gate is a **guardrail**, not a lift: the variant passes only if it (1) still sends every welcome, (2) still replies to every guard message, (3) emits **no more** voice violations than the same-model control, and (4) doesn't move escalations. Full compliance (zero violations) and any lift over control are reported as extra credit, not required.

**Why I thought there was headroom.** The historical baseline breaks its own policy: of the 324 baseline DMs, **56 (17.3%) contain an em-dash** and **42 say "let me know."** The rule is right there in `comms_policy.md` and production still ignores it 17% of the time.

**What actually happened (the honest part).** On the same-model control the headroom evaporated. gpt-5.6-sol on the *original* prompt already emitted **zero** violations on these turns, and so did Claude Opus. So Variant C passes — welcomes and replies preserved, zero violations, no escalation drift — but the **lift over control is 0**, because a strong model already complies. The 17% only shows up against the historical model, which lives in the **reference lane**, not the live control. I tried gpt-4.1-mini to force headroom; the weak control behaved erratically (skipped welcomes) and even the hardened variant slipped two filler phrases. Lesson logged: on capable models, voice is a no-regression target, not an improvement target.

**How it improves the harness.** It adds a second *kind* of pass. B answers "did my improvement happen?" (a lift gate). C answers "did my change comply and break nothing?" (a no-regression gate). A real suite needs both, and C is the clean worked example of the second. It's also the best demonstration of the reference-lane / same-model-control split doing its job: the tool had every excuse to claim a 17% win and instead reported a 0 lift, because the honest comparison is against the control, not against production.

**Result** Pass, as a no-regression / compliance gate. Not a behavioral lift, and the scorer says so out loud.

(And yes, this writeup is full of em-dashes. The rule is for guard texts on a phone at 2am, not for me.)

---

## open questions

### 1. Prompt changes change the conversation. Historical guard replies may stop making sense. How do we handle that?

The historical conversation becomes less reliable after the copilot takes a different action.

The harness handles that in two ways.

- **Turn mode** freezes / rebuilds context from history for a selected wake. Good for cheap, isolatable decisions. Bad at catching path dependence.
- **Shift mode** keeps the variant’s earlier DMs, notes, and escalations in the conversation. Guard events remain historical because the harness does not make up new guard replies. This is still useful for finding issues caused by earlier copilot actions.
- A future version could branch when a new copilot message makes the next historical reply invalid. That branch could use a reviewed reply or a simulated reply. I did not build a full guard simulator for this version.

Historical baseline stays a **reference lane** (what production did), not the live control.

### 2. How do we measure success? How do we know the change did what we intended?

Success is a set of checks, not one score.

For each edit I do the following.

1. Name the intended improvement (must_improve).
2. Name what must stay true (must_preserve).
3. Name the stop-ships (must_not_happen).
4. Compare **same model, original prompt vs variant**, on frozen fixtures.
5. Check **behavior and tools**. For example, did it call `get_guard_locations` and did it make the flag call?
6. Use **reps** (×3) before calling something causal.
7. If turn mode and shift mode disagree on safety, **shift mode wins**.

An agent can look good on an output-only check while taking an unacceptable path. So I score **outcome and process** (e.g. verified before affirming; escalated instead of soft-DMing).

---

## Pass / fail summary

| Variant | Verdict | Why |
|---------|---------|-----|
| **B** | Provisional targeted pass | Verify rate up on claim turns; replies held; no esc regression on the set |
| **A** | Stop-ship | Full-shift 55252 under-escalation 3/3 |
| **A2** | Blocked | Still missed an escalate→DM case; quieter win lost; noisier esc |
| **A3** | Safety repair, not complete | Mid-shift miss class fixed; no stable quietness lift; ending miss 2/3 |
| **C** | Pass (no-regression) | Voice policy enforced; welcomes + replies held; 0 violations; no esc drift. Lift 0 vs modern control (already compliant); headroom only in the reference lane |

The main takeaway is simple. **A prompt can look fine turn by turn and still be less safe across a full conversation.** And the other side of it: a change can pass without moving the needle, and the honest gate is the one that admits when the lift is zero.

---

## Shifts I leaned on

| Shift | Why |
|-------|-----|
| **56370** | Rich conversation with useful claim turns and probes |
| **55252** | Silent guard with an escalation ladder and full-shift safety checks |
| **50737** | Incident-heavy shift with a discretionary quietness candidate |
| **50837** | Clear no-op scheduled turns (quietness no-regression) |

---

## Challenges and how I worked through them

| Problem | What I did |
|---------|-------------|
| Historical baseline as “control” looked like A dropped escalations | Separated the reference from the live same-model control and ran it again |
| gpt-4.1-mini narrating / weak tools | Switched causal runs to Sol (Opus for adapter proof) |
| Empty obligations would invent “nothing owed” | Default `get_open_obligations` to unavailable, not `[]` |
| Nearest-match fixtures could return the wrong data | Used exact `(tool, input)` matches only |
| A looked safe on turns and unsafe on the shift | Added full-shift mode and three repetitions, then stopped A |
| A2 still failed by stacking silence + safety | A3 replaced the section with an ordered mandatory-then-quiet rule |
| A3 did not show a stable quietness improvement | Screened every labeled candidate and reported the result |
| OpenAI credits ran out during the test | Paused, resumed when credits returned, and finished the screen |

fable 5 and GPT-5.6 helped with design review and diagnosis. We worked through what to measure, how to label turns, and how to read the differences. Pass and fail decisions still came from deterministic checks and human review of escalations. GPT-5.6 did not decide whether its own output was better.

---

## What I’d do next (more time)

1. Add **intervention labels** when the control and variant disagree. The reviewer could choose stay quiet, notify, or require approval, then save that decision as a regression case.
2. Fix or clearly scope **shift-ending** (`default.md`) after mid-shift escalations. This is A3’s remaining miss.
3. More shifts and a small **golden set** of must_preserve / must_not_happen cases in CI (offline assertions always; live recipes on demand).
4. Cheap **fork policy** when a variant DM would invalidate the next historical guard reply.
5. Cost/latency budgets per recipe so “run quickly and cheaply” stays true as the suite grows.

---

## Artifact index

- B files include `ctrl_b_*_claims` and `varb_*_claims`. Run `py -m harness.verify_b`.
- A full-shift files include `ctrl_sol_55252_full[_r2|_r3]` and `vara_sol_55252_full[_r2|_r3]`.
- A2 and A3 files include `vara2_sol_55252_full_r*` and `vara3_sol_55252_full_r*`. Scores are in `runs/a2_score_55252.json`, `runs/a3_score_55252.json`, and `runs/a3_complete_status.json`.
- Run `harness/diagnose_escalation.py` to create `runs/esc_diagnosis_55252.json`.
- C files include `ctrl_c_voice_*` and `var_c_voice_*`. Run `cx t vo` (scorer writes `runs/var_c_voice_*/recipe_score.json`).
- Labels are in `experiments/turn_sets.json` and `experiments/scheduled_turn_labels.json`.
- Variants are in `variants/variant_a`, `variant_a2`, `variant_a3`, `variant_b`, and `variant_c`. Each changes one file from `variants/baseline`.
