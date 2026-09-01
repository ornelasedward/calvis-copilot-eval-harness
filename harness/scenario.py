"""Scripted-guard scenario runner.

Drives the copilot turn-by-turn against a deterministic guard state machine
(experiments/fixtures/*.json `script` section). Records trajectories in the
same store format execute_recipe consumes. Dry mode uses CannedAdapter (zero
API). Live mode uses the same adapters as historical recipes.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.engine import EngineConfig, ReplayEngine
from harness.lexicon import is_ask, is_close, asks_photo, asks_note, tool_short, turn_escalated_to_ops
from harness.loader import Shift, load_shift, parse_ts
from harness.schemas import RunManifest, TurnResult, Usage
from harness.store import ExperimentStore, prompt_dir_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "experiments" / "fixtures"


def _full_tool(name: str) -> str:
    if name in ("Read", "Write", "Glob", "Grep", "ToolSearch"):
        return name
    if name.startswith("mcp__"):
        return name
    return f"mcp__calvis__{name}"


class CannedAdapter:
    """Zero-network adapter: a list of per-turn tool-call sequences."""

    name = "canned"

    def __init__(self, turns: list[list[dict]]):
        self.turns = turns
        self.turn_i = -1
        self.step_i = 0

    @property
    def capabilities(self) -> dict:
        return {"prompt_caching": False, "seed": True, "network": False}

    def next_turn(self) -> None:
        self.turn_i += 1
        self.step_i = 0

    def complete(self, request):  # noqa: ARG002 — protocol
        from harness.adapters.base import ModelResponse, ToolCallRequest

        steps = self.turns[self.turn_i] if 0 <= self.turn_i < len(self.turns) else []
        if self.step_i < len(steps):
            step = steps[self.step_i]
            self.step_i += 1
            tool = step.get("tool")
            if tool:
                return ModelResponse(
                    text=None,
                    tool_calls=[
                        ToolCallRequest(
                            id=f"canned-{self.turn_i}-{self.step_i}",
                            name=_full_tool(tool),
                            input=step.get("input") or {},
                        )
                    ],
                    stop_reason="tool_use",
                    usage=Usage(),
                    raw=step,
                )
        return ModelResponse(
            text=None,
            tool_calls=[],
            stop_reason="end_turn",
            usage=Usage(),
        )


def load_fixture(path: str | Path) -> Shift:
    path = Path(path)
    if not path.is_absolute() and not path.exists():
        alt = ROOT / path
        if alt.exists():
            path = alt
        elif (FIXTURES_DIR / path.name).exists():
            path = FIXTURES_DIR / path.name
    return load_shift(path)


def _fresh_ledger(script: dict) -> list[dict]:
    rows = script.get("obligations")
    if rows is None and script.get("obligation"):
        rows = [script["obligation"]]
    return copy.deepcopy(rows or [])


def _observe(result: TurnResult) -> dict[str, Any]:
    bodies = [m.body for m in result.messages if m.body]
    tools = [tool_short(r.tool) for r in result.tools_used]
    tagged = any((m.meta or {}).get("copilot_action") for m in result.messages)
    joined = "\n".join(bodies)
    return {
        "bodies": bodies,
        "text": joined,
        "asked": tagged or is_ask(joined) or bool(bodies),
        "asked_photo": asks_photo(joined),
        "asked_note": asks_note(joined),
        "closed": is_close(joined),
        "escalated": turn_escalated_to_ops(result.to_dict()),
        "tools": tools,
        "dm_count": len(bodies),
        "tagged": tagged,
    }


def _bump_asks(ledger: list[dict], active_ids: list[str], obs: dict) -> None:
    if not (obs.get("asked_photo") or obs.get("asked_note") or obs.get("tagged")):
        return
    for row in ledger:
        if row.get("id") in active_ids and not row.get("satisfied"):
            row["asks_sent"] = int(row.get("asks_sent") or 0) + 1
            if obs.get("escalated"):
                row["escalated"] = True


def _maybe_satisfy(ledger: list[dict], provided: set[str], obs: dict, required: list[str]) -> None:
    if not obs.get("closed"):
        return
    if not set(required) <= provided:
        return
    for row in ledger:
        if not row.get("satisfied"):
            row["satisfied"] = True


def _advance_guard(persona: str, script: dict, state: dict, obs: dict) -> dict | None:
    """Return a guard reply dict or None. Mutates state."""
    gold = script.get("gold") or {}
    replies = script.get("guard_replies") or {}
    phase = state.get("phase") or "await_ask"

    if persona == "partial":
        branch = gold.get("branch") or "text_no_photo"
        if phase == "await_ask" and (obs.get("asked") or obs.get("tagged") or obs.get("dm_count")):
            state["phase"] = "partial_sent"
            if branch == "photo_no_note":
                state["provided"].add("photo")
                return {
                    "text": gold.get("photo_caption") or "",
                    "image": "[photo]",
                    "image_url": gold.get("photo_url"),
                    "image_meta": {"has_note": False},
                }
            state["provided"].add("note")
            return {"text": gold.get("partial_text") or "", "image_url": None}
        if phase == "partial_sent" and (obs.get("asked_photo") or obs.get("asked_note") or obs.get("asked")):
            state["phase"] = "complete"
            if branch == "photo_no_note":
                state["provided"].add("note")
                return {"text": gold.get("completing_note") or "all quiet on the east lot", "image_url": None}
            state["provided"].add("photo")
            return {
                "text": gold.get("photo_caption") or "",
                "image": "[photo]",
                "image_url": gold.get("photo_url"),
                "image_meta": {"has_note": False},
            }
        return None

    if persona == "pushback":
        if phase == "await_ask" and (obs.get("asked") or obs.get("dm_count")):
            state["phase"] = "pushed"
            return {"text": gold.get("pushback_text") or "stop babysitting me, I know my job."}
        return None

    if persona == "hostile":
        if phase == "await_ask" and (obs.get("asked") or obs.get("dm_count")):
            state["phase"] = "hostile"
            return {
                "text": gold.get("hostile_text") or "this is surveillance, back off",
            }
        return None

    # Generic on_ask / on_followup (photo-gamer compatible).
    if phase == "await_ask" and (obs.get("asked") or obs.get("dm_count")):
        state["phase"] = "asked"
        reply = replies.get("on_ask")
        return dict(reply) if isinstance(reply, dict) else None
    if phase == "asked" and (obs.get("asked") or obs.get("dm_count")):
        state["phase"] = "followup"
        reply = replies.get("on_followup")
        if reply == "same_url_again":
            first = replies.get("on_ask") or {}
            return dict(first) if isinstance(first, dict) else None
        return dict(reply) if isinstance(reply, dict) else None
    return None


def _open_ids_for_wake(wake: dict, ledger: list[dict]) -> list[str]:
    ids = list(wake.get("obligation_ids") or [])
    if wake.get("obligation_id"):
        ids.append(wake["obligation_id"])
    if ids:
        return ids
    return [r["id"] for r in ledger if r.get("id") and not r.get("satisfied")]


def _mark_open_windows(ledger: list[dict], open_ids: list[str], wake: dict) -> None:
    """Ensure listed windows exist / are open for this wake."""
    by_id = {r.get("id"): r for r in ledger}
    for oid in open_ids:
        row = by_id.get(oid)
        if row is None:
            continue
        if wake.get("reset_asks"):
            row["asks_sent"] = 0
            row["escalated"] = False
            row["satisfied"] = False


def run_scenario(
    *,
    fixture: Shift,
    variant_dir: Path,
    adapter,
    run_id: str,
    store: ExperimentStore | None = None,
    repetition: int = 0,
    canned: CannedAdapter | None = None,
) -> list[TurnResult]:
    script = fixture.script or {}
    persona = (script.get("gold") or {}).get("persona") or script.get("persona") or ""
    gold = script.get("gold") or {}
    required = list(gold.get("requires") or ["photo", "note"] if persona == "partial" else gold.get("requires") or [])
    ledger = _fresh_ledger(script)
    images = copy.deepcopy(script.get("images") or {})
    wakes = list(script.get("wakes") or [])
    if not wakes:
        raise ValueError(f"fixture {fixture.id} has no script.wakes")

    engine = ReplayEngine(
        shift=fixture,
        adapter=adapter,
        config=EngineConfig(
            variant_dir=variant_dir,
            mode="shift",
            synthetic_obligations=ledger,
            synthetic_images=images or None,
        ),
        run_id=run_id,
    )

    state: dict[str, Any] = {"phase": "await_ask", "provided": set()}
    results: list[TurnResult] = []
    pending_guard: dict | None = None

    for i, wake in enumerate(wakes):
        turn_n = int(wake.get("turn") or (i + 1))
        trigger = wake.get("trigger") or "obligation_due"
        ts = parse_ts(wake["ts"]) if wake.get("ts") else fixture.start + timedelta(minutes=15 * i)
        open_ids = _open_ids_for_wake(wake, ledger)
        _mark_open_windows(ledger, open_ids, wake)

        if pending_guard:
            delay = int(wake.get("guard_delay_s") or 90)
            gts = ts - timedelta(seconds=min(delay, 30) if ts else 90)
            # Guard must land before this wake.
            if gts >= ts:
                gts = ts - timedelta(seconds=5)
            engine.thread.record_guard_message(
                gts,
                pending_guard.get("text") or "",
                image=pending_guard.get("image"),
                image_url=pending_guard.get("image_url"),
                image_meta=pending_guard.get("image_meta"),
            )
            pending_guard = None

        if trigger == "guard_message" and not any(
            m.role == "guard" and m.ts <= ts for m in engine.thread.history_as_of(ts, mode="shift")
        ):
            # No scripted reply to react to — skip this wake.
            continue

        if canned is not None:
            canned.next_turn()

        result = engine.run_turn(turn_n, trigger, ts)
        result.mode = "scenario"
        result.repetition = repetition

        obs = _observe(result)
        _bump_asks(ledger, open_ids, obs)
        if obs.get("escalated"):
            for row in ledger:
                if row.get("id") in open_ids:
                    row["escalated"] = True
        _maybe_satisfy(ledger, state["provided"], obs, required)

        new_ob = bool(wake.get("new_obligation"))
        active = open_ids[0] if open_ids else None
        result.raw_events.append(
            {
                "type": "scenario_state",
                "persona": persona,
                "phase": state.get("phase"),
                "provided": sorted(state["provided"]),
                "satisfied": any(r.get("satisfied") for r in ledger if r.get("id") == active),
                "obligation": copy.deepcopy(next((r for r in ledger if r.get("id") == active), {})),
                "open_ids": open_ids,
                "active_obligation": active,
                "new_obligation": new_ob,
                "observables": {
                    "asked": obs["asked"],
                    "asked_photo": obs["asked_photo"],
                    "asked_note": obs["asked_note"],
                    "dm_count": obs["dm_count"],
                    "escalated": obs["escalated"],
                },
            }
        )

        pending_guard = _advance_guard(persona, script, state, obs)

        if store is not None:
            store.append_turn(run_id, fixture.id, result)
        results.append(result)

        if obs.get("escalated") and wake.get("stop_on_escalate"):
            break

    return results


def _make_live_adapter(adapter: str, model: str):
    from harness.adapters.anthropic import AnthropicAdapter
    from harness.adapters.openai import OpenAIAdapter

    if adapter == "anthropic":
        return AnthropicAdapter(model=model)
    if adapter == "openai":
        return OpenAIAdapter(model=model)
    raise SystemExit(f"unknown adapter: {adapter}")


def _model_params(adapter: str, model: str) -> dict:
    if adapter == "openai" and str(model).startswith("gpt-5"):
        return {"reasoning_effort": "none"}
    return {"temperature": 0}


def execute_scenario_recipe(
    *,
    name: str,
    recipe: dict,
    plan: dict,
    dry_run: bool,
    adapter: str,
    model: str,
    candidate_variant: str,
    repeat: int,
    root: Path,
) -> dict[str, Any]:
    """Run a mode=scenario recipe: k scripted reps, then the deterministic scorer."""
    from harness.recipes import SCORERS

    root = root or ROOT
    jobs = recipe.get("jobs") or []
    if not jobs:
        raise ValueError(f"scenario recipe {name} has no jobs")
    fixture_rel = jobs[0].get("fixture")
    if not fixture_rel:
        raise ValueError(f"scenario recipe {name} job missing fixture")
    fixture = load_fixture(root / fixture_rel)
    script = fixture.script or {}
    k = max(int(recipe.get("repetitions") or repeat or 1), 1)
    variant_dir = Path(candidate_variant)
    if not variant_dir.is_absolute():
        variant_dir = root / variant_dir

    canned_script = ((script.get("canned") or {}).get("passing") or [])
    if dry_run and not canned_script:
        raise SystemExit(f"dry scenario {name} needs script.canned.passing")

    variant_id = plan["variant_run_id"]
    store = ExperimentStore(root / "runs")
    manifest = RunManifest(
        run_id=variant_id,
        variant_name=variant_dir.name,
        prompt_hash=prompt_dir_hash(variant_dir),
        model="canned" if dry_run else model,
        model_params=_model_params(adapter, model) if not dry_run else {},
        adapter="canned" if dry_run else adapter,
        data_version="scenario",
        code_version="scenario",
        mode="scenario",
        shifts=[fixture.id],
        repetitions=k,
        created_at=datetime.now(timezone.utc).isoformat(),
        tool_fixture_mode="synthetic_scenario",
    )
    store.create_run(manifest)

    print(f"=== recipe {name}: scenario ({candidate_variant}) x{k} -> {variant_id} ===")
    grand = {"turns": 0, "cost_usd": 0.0}
    for rep in range(k):
        if dry_run:
            ad = CannedAdapter(copy.deepcopy(canned_script))
        else:
            ad = _make_live_adapter(adapter, model)
        results = run_scenario(
            fixture=load_fixture(root / fixture_rel),
            variant_dir=variant_dir,
            adapter=ad,
            run_id=variant_id,
            store=store,
            repetition=rep,
            canned=ad if dry_run else None,
        )
        grand["turns"] += len(results)
        grand["cost_usd"] += sum(r.usage.cost_usd for r in results)
        print(f"  rep {rep}: {len(results)} turns  cost=${grand['cost_usd']:.4f}")

    store.seal_run(variant_id, grand)

    scorer_name = recipe.get("scorer") or name
    scorer = SCORERS.get(scorer_name)
    if not scorer:
        raise KeyError(f"unknown scorer: {scorer_name}")
    # Scenario recipes have a single arm; control_id is unused by these scorers.
    score = scorer(store, variant_id, variant_id, recipe)
    result = {"plan": plan, "score": score, "pass": bool(score.get("pass"))}
    out_dir = root / "runs" / variant_id
    (out_dir / "recipe_score.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print("\n=== score card ===")
    print(json.dumps(score, indent=2, default=str))
    print(f"GATE: {'PASS' if result['pass'] else 'FAIL'}")
    print(f"wrote {out_dir / 'recipe_score.json'}")
    return result
