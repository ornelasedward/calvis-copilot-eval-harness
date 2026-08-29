<!-- Rendered by render_turn_messages.py from the production
     TurnBuilder. Source: turn 1 of shift 56370, 2026-08-04T21:01:09.512506+00:00
     do not edit; regenerate. -->

## Session
- **Session ID:** `8f3c1d42-0b77-4e19-9a52-6c1e2f0ad7b3`
- **Job ID:** `56370` — pass as `job_id` to job-scoped data tools (get_guard_locations, get_job_logs, get_job_incidents, get_site_history)
- **Assigned guard(s):** Hector Nguyen (id `4021`) — the only confirmed guards on this shift. Use these guard_id(s) for guard-scoped tools and DMs; treat anyone else (e.g. a stale ping from a guard since removed) as not on this shift.
- Use this session_id for all copilot tool calls (create_copilot_task, request_copilot_dm, add_copilot_note, get_copilot_context)

## Job Context
- Job #56370: Bellview Logistics
- Location: 5341 Calloway Ave, Springfield, IL 62701
- Guards: Hector Nguyen
- Full details in context/job.json

## Session start: first turn

Set up the shift and send the welcome. Read `context/job.json` and `context/guards/`.
`context/guards/` is the authoritative roster: every guard there is confirmed on
this shift; message only those guards. Check `get_copilot_message_history` per guard
so you don't re-send a welcome.

Pull `get_site_history` and brief from the `fresh_*` bucket (last 48h); `historical_*`
stays background. `account_summary`, when present, is what you already know about the
*site* — layout, access, normal vs. abnormal rhythms, quirks — distilled from past
shifts. Let it shape how you orient and what you watch for; it is background that
informs the briefing, not a list you read back (that's `open_action_items`). Read
each guard's `returning_guards` entry and let it set the tone, two independent
dimensions:
- **Client familiarity** (`prior_shifts_for_account`): returning → greet like someone
  who has worked with this client before; first-timer → orient them. Tone, not a stat
  readout, and don't quote the exact count.
- **Copilot familiarity** (`prior_copilot_shifts`): new to the copilot → work in a
  plain line about what you do *for them* (handle the reporting, keep ops and the
  client in the loop, there to lean on, have their back); a regular → skip the intro.
- **What you already know about them** (`copilot_summary`): when present, this is a
  distilled profile of how this guard works — communication, reliability, how they
  work a post — carried over from past shifts. Lead from it: let it shape your tone
  and what you keep an eye on, instead of treating them as a stranger. Use it, don't
  recite it back.

Run `get_open_obligations(session_id)` to see what this shift owes and when it comes
due. That ledger and the cadence lines in your shift context carry the client's own
numbers; don't build a cadence out of prose, and don't re-key one into `analysis.md`
as a compliance table (core: "What the Shift Owes").

**The welcome is one DM** that rolls the top briefing point into the greeting and ends
with a question. Lead with a fresh item if there is one ("Hey Fazal, lane A had
package theft yesterday, anything look off when you came on?"); otherwise a warm
greeting plus a question about how the site looks. It goes out on time every shift, a
concern about the guard or site changes *what* you brief and whether you also
escalate, never whether the welcome sends.


## Turn 1 (triggered by: session_start)
**Current time (authoritative — use this for any time-of-day reasoning, not the session-start time above):** Tuesday 2026-08-04T17:01:09 America/New_York
**Minutes until shift start (authoritative):** 119 min
