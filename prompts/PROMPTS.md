# The prompt system

The agent that supervises a shift is driven entirely by the markdown in this
directory. There is no prompt logic anywhere else. Changing behaviour means
changing these files.

Two directories hold prompt text you edit: `core/` and `instructions/`. That is
the whole mutable surface — a prompt variant is a variant of those files.
Everything else here is generated from them. `ASSEMBLED_SYSTEM_PROMPT.md` and
all of `turn_message/` are derived artifacts, checked in so you can read what
the model actually receives; edits to them are overwritten on the next render
and change nothing at runtime. Read `turn_message/` to see the shape your
harness has to reproduce, then point your variants at `core/` and
`instructions/`.

Two layers assemble at runtime, and a third sits in front of both.

## Layer 1 — the system prompt

Built once when the session starts, and fixed for the life of the shift.
The six files in `core/` are concatenated in this order, joined by a blank
line:

```
identity  →  context  →  holding_the_post  →  obligations  →  comms_policy  →  tools
```

`ASSEMBLED_SYSTEM_PROMPT.md` is that concatenation, generated from the same
files, so you can read what the model receives in one pass. It is derived —
edit the individual files, not the assembled copy.

One placeholder is substituted at build time:

| Placeholder | Filled with |
|---|---|
| `{COPILOT_CONTEXT}` | The shift briefing: times, site, address, the guard roster, job instructions, and what the agent already knows about this site and this guard. Everything under `shift` in a shift JSON is the same material. |

The clock is deliberately not in here. It goes stale on a twelve-hour shift,
so it rides the turn header instead and is refreshed every turn.

## Layer 2 — the turn message

Every time the agent wakes it gets a fresh message. `turn_message/` holds one
rendered example per trigger, produced by running the production builder — read
those first, they are the ground truth for this section. Like
`ASSEMBLED_SYSTEM_PROMPT.md` they are derived output: edit `instructions/`, not
the rendered copies. In order:

| Section | Present when |
|---|---|
| Session preamble — session id, job id, guard roster | Every turn |
| Job context — job id, title, location | First turn only |
| Previous session analysis | First turn, if the job ran before |
| Turn history | First turn, if there is any |
| **Exactly one file from `instructions/`** | Every turn |
| Operator messages | New operator messages this turn |
| Approval decisions | New approval decisions this turn |
| Job events | Events batched since the last turn |
| Actions taken | If history carries any |
| Why you woke this cycle | Auto-cycle wakes the gate assigned a posture |
| Turn header — turn number, trigger, **Current time**, **Time left on shift** | Every turn |

That instruction file is embedded whole and unmodified — the bytes in
`instructions/<file>.md` are the bytes in that section of the turn message.
Nothing is trimmed, reworded, or templated per trigger, so whatever you write in
an instruction file is what the model reads, verbatim.

Two orderings are not arbitrary. The instruction file sits ahead of the
per-turn content so the prefix stays cache-eligible across turns of the same
trigger, and the turn header sits last for the same reason — the turn number
changes every call.

The header is what `core/context.md` means by "the latest turn header is your
clock." Both time lines are computed server-side, in the site's local zone,
and the prompt tells the model to trust them over anything it could work out
itself.

Which instruction file arrives is chosen by what woke the agent:

| Wake trigger | File |
|---|---|
| `session_start` | `session_start.md` |
| `guard_message` | `guard_response.md` |
| `operator_message` | `operator_message.md` |
| `approval_decision` | `approval_decision.md` |
| `scheduled_check_in` | `scheduled_check_in.md` |
| `obligation_due` | `obligation_due.md` |
| `job_event` | `job_event.md` |
| `guard_in_transit`, `guard_checked_in` | `job_event.md` |
| anything else — `shift_ending` is the common one | `default.md` |

The `trigger` field on each `turn_start` entry in a shift's `baseline` is this
same value, so you can see which instruction file was in play for any turn
that actually ran.

One thing the examples make visible: a `guard_message` turn does not contain
the guard's message. The runner threads conversation history separately, so
the turn message carries only the instruction and the header. Anything
replaying these shifts has to supply that history itself.

Seven of the ten examples were rendered from real turns of shift 56370 in this
bundle, so the guard and site names in them match the ones you'll read in
`shifts/56370.json`, and the header comment on each names the turn it came from.
The other three — `approval_decision`, `obligation_due`, `operator_message` —
cover triggers that shift never fired. They were rendered from synthetic inputs
wearing the same guard and site, and say so in their headers. Don't go hunting
for a matching turn in `shifts/56370.json` for those three; there isn't one.

## Layer 0 — the deliberation gate

A scheduled wake does not automatically become a turn. A separate, much
smaller model call looks at the trigger and the recent context first and
decides whether the turn is worth running at all. It answers in around a
second, and if it fails or times out it fails open — the turn runs.

When it does let a turn through it can attach a posture (`act`, `check_in`,
`curious`) and a one-line reason, which arrive as the "Why you woke this
cycle" section. `scheduled_check_in.md` refers to this.

When it suppresses a wake, the baseline records a `turn_skipped` entry with
the gate's reason — but **not on these ten shifts**. The durable record of
suppressed wakes was added on 2026-08-24, after all of these shifts ran, so
their gate decisions were emitted and lost. Expect no `turn_skipped` entries
here.

What the timestamps do show is that the scheduled cadence on these shifts was
hourly, landing within a minute of the hour, and that essentially every
scheduled wake produced a turn. So on this data a scheduled gap is a gap in
the schedule, not a hidden suppressed turn. Don't read the absence of
`turn_skipped` as proof the gate never fires — on shifts recorded after that
date it does, and any harness meant to outlive this bundle should handle the
entry.

The gate is a fixed given for this exercise. It is not part of what you are
asked to change, and its prompt is not in this bundle.

## Layer 3 — files the agent reads

The prompts refer to `context/job.json` and `context/guards/`. Those are files
placed in the agent's working directory at session start, holding the full job
record and one profile per guard on the shift. The agent reads them with a file
tool rather than receiving them inline, which is why the system prompt stays
small even on a complex site.

## Notes

`tools.md` describes tools that exist in our infrastructure and will not exist
in yours. Treat it as a description of the capabilities the agent is expected
to have, not as a contract to reimplement. Every tool call the copilot actually
made is in each shift's `baseline` with both its `input` and what it returned.

Timestamps reaching the agent are already converted to the site's local
timezone. The agent is told not to do timezone math itself.
