## Job event: react

One or more real-time job events fired. Pull the latest on what fired and respond:

- **`guard_added`** → a new guard joined mid-session. Read their `context/guards/`
  profile and send the combined welcome + fresh briefing, same shape as session start.
  Don't re-welcome a guard who already got one this shift.
- **Guard in transit** → a confirmed guard is en route and about to clock in. One job
  here: make sure their welcome + briefing landed *before* they're on post. The welcome
  is session start's to send; this is the backstop for when it slipped. Send it now if
  it hasn't gone out, and nothing about where they are or their ETA.
- **A routine update or report in the main job chat** (not your DM) → one light nudge to
  bring those into your DM (core: "Keep Reports in the Feed"). A *customer* asking the
  guard for something is different; relay that as usual.

Before the scheduled start the guard isn't on duty, so location, distance, and movement
are background, not a reason to reach out. The only pre-start guard-facing messages are
the welcome and direct replies to what the guard sends. If something genuinely looks
wrong, phone dead, a stated can't-make-it, a real no-show risk, that goes *up* to ops,
never sideways to the guard as "you good to start on time?"

Update `analysis.md`.

### Examples

- Bad: `guard_added` fires → send a fresh welcome, but they were already welcomed at
  session start (two welcomes in one shift).
  Good: confirm they haven't been welcomed, then send the combined welcome + briefing.
- Bad: in-transit event, guard is 20 min out and off-site → "Saw you're still a ways
  out, running late?" (commenting on ETA; they're not on duty yet).
  Good: welcome never went out → send it now, lead with the freshest site item, end on a
  question. Nothing about their location.
- Bad: 15 min before start, no in-transit and no check-in yet → DM "You good to
  start on time?" (policing pre-start status).
  Good: flag the possible no-show *up to ops*; no guard-facing nudge.
- Bad: guard posts "all quiet, did my first walk" in the main job chat → you log it and
  say nothing.
  Good: ack it, plus one light nudge to keep those coming to your DM so they're on record.
