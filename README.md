# Calvis prompt-change eval

Here is my completed take-home assessment.

- My notes, results, and answers to the open questions are in [`WRITEUP.md`](WRITEUP.md).
- Setup instructions and CLI examples are in [`HARNESS.md`](HARNESS.md).
- The prompt variants are under [`variants/`](variants/).

The harness runs the original prompt and a prompt variant against the same historical guard shifts. It then compares their decisions, tool calls, messages, escalations, and cost at either the individual-turn level or across a full shift.

The short CLI is the easiest way to try it.

```powershell
py -m pip install -r requirements.txt
py -m pytest tests -q
.\cx
.\cx t cl -n
```

Add the API keys listed in `HARNESS.md` when you are ready to run a live model comparison.

---

## Original project brief

**Calvis guard copilot**

Every shift runs with our AI copilot supervising it. It watches all of the data around the guard/shift as it happens (location stream, guard actions, chat messages, photos, etc) and decides whether to message the guard, what to say, how to say it, and when to escalate to a human (Calvis Overwatch). All of this behavior is currently determined by the prompt engineering we’ve done on top of the out-of-the-box LLMs.

**Problem**

We test in prod. When we make changes to the copilot’s code, we don’t know how that affects the agent’s behavior until it’s live. We hope it works, then monitor closely.

**Ask**

Building a system that lets us change the copilot and clearly understand how those changes affect the agent’s behavior. We should be able to see versions of different copilots and how they behaved against the same set of shifts.

By the end we want to see something that runs. At least two prompt variants of your own, against shifts you choose out of the bundle, and a way to look at the results and understand how the behavior differed between them.

**Requirements**

- The user is a developer in a loop: change a prompt, run it, look at what changed, change it again
- Runs against the real historical shifts in the bundle
- Should run quickly and cheaply
- We should be able to point it at a new prompt and get an answer

**Open questions**

These are unsolved. We don’t expect you to solve them in a few hours. Tell us where you land on each and why, and build toward it as far as your time allows

- Changes to the agent will change the conversation, meaning the historical guard responses won’t always make sense anymore. How do we handle this?
- How do we measure success on a prompt change? How do we know the changes affected the system in the way that we intended?

**How the copilot agent is architected today**

- Single agent i.e. there is no coordinator + sub-agent structure
- The agent lives inside of a session that is created a few hours before the guard shift starts.
- Baseline context is injected into the session on start: shift information (start/end time, job instructions, address), notes about the site (open action items, recent incidents), notes about the guard (how they respond, quirks).
- The agents wakes up every 30 minutes on schedule, scans all of shift data, and decides whether or not to take any action (send a message to the guard, escalate to ops)
- Certain actions will wake up the agent outside of its scheduled wake. Most obvious is the guard sending the copilot a message. When this happens, the agent will try to respond instantly. If it feels it needs more context to respond (answer to a complex question, for example) then it will gather that context through tool calls before responding

**What’s in the bundle**

Ten real shifts, one JSON file each, under `shifts/`. These shifts cover a variety of situations: guards who hold a good back and forth conversation with the copilot, guards who go quiet or stop streaming data, guards who take a long time to answer, guards who answer with something other than what was asked, and guards who get short or dismissive.

Each shift file has three parts:

- `shift` is the context the agent starts with: times, site, job instructions, the guard, notes on the account and the site.
- `events` is everything that happened around the guard while the shift ran, in time order. Location pings, device telemetry, job events like check-in and geofence crossings, and every message the guard sent.
- `baseline` is what our copilot actually did about all of it. Every time it woke and why, every message it sent, and every tool call it made.

Photos the guard sent have been removed. Where one was sent, both the message event and the copilot’s `fetch_chat_image` call carry `[photo]` in place of the file, so you can still see that a photo arrived and what the copilot chose to do about it.

Our current prompt system is under `prompts/`. Read `prompts/PROMPTS.md` first; it explains how the pieces assemble at runtime. The prompt text you edit lives in `prompts/core/` and `prompts/instructions/`; `prompts/ASSEMBLED_SYSTEM_PROMPT.md` and `prompts/turn_message/` are generated from those and are there to be read, not changed. `prompts/turn_message/` holds a rendered example of the message the agent receives on each kind of wake, one per trigger — seven rendered from real turns of shift 56370 in this bundle, three from synthetic inputs for triggers that shift never fired.

There is far more here than you need. Some shifts run to well over a thousand location pings and telemetry rows, and the busiest one woke the copilot 133 times while the quietest woke it 11. We left all of it in on purpose. If something in the data or the prompts doesn’t add up, please ask us!

**A note on the data**

These are real shifts, but every guard name, account name, address, phone number, and site coordinate has been replaced with a stand-in. The replacements are consistent: the same guard is the same guard across shifts, the same site is the same site, and the geofence math still works because a site and its pings were moved together. Anything that looks like a real person, company, or place is not one.

**What to send back**

- The code, however you’d normally share it.
- A short writeup: what you built, how to run it, where you landed on the open questions, and what you’d do next with more time.
- Whatever output convinced you that your two variants actually behaved differently.
