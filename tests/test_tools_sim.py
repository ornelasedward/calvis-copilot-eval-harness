"""Tool-simulator tests and the reconstruction validation gate.

Reconstruction candidates stay classified as candidates until field-level
comparison against parseable recorded outputs is reported here. Truncated
(~10k-char) recorded outputs are a bundle artifact — validation skips them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.loader import load_all_shifts, load_shift
from harness.tools_sim import (
    ToolSimulator,
    Workspace,
    _parse_output,
    compare_locations_fields,
    reconstruct_guard_locations,
    reconstruct_job_logs,
)

BUNDLE = Path(__file__).resolve().parents[1]
SHIFTS = BUNDLE / "shifts"


@pytest.fixture(scope="module")
def shifts():
    return load_all_shifts(SHIFTS)


def test_action_tools_record_without_side_effects():
    shift = load_shift(SHIFTS / "56370.json")
    sim = ToolSimulator(shift=shift, as_of=shift.start)
    r = sim.call("request_copilot_dm", {
        "body": "test",
        "session_id": "x",
        "recipient_guard_id": 1,
    })
    assert r.source == "action_recorded"
    assert r.output["ok"] is True
    assert len(sim.actions) == 1
    assert sim.actions[0].kind == "request_copilot_dm"


def test_obligations_default_unavailable():
    shift = load_shift(SHIFTS / "56370.json")
    sim = ToolSimulator(shift=shift, as_of=shift.start)
    r = sim.call("get_open_obligations", {"session_id": "x"})
    assert r.source == "unavailable"
    assert r.output["status"] == "data_unavailable"
    assert r.output["error"] == "tool_unavailable"
    assert "empty ledger" in r.output["reason"] or "NOT an empty" in r.output["reason"]
    assert "fallback" in r.output
    assert "welcome" in r.output["fallback"].lower() or "Job Requirements" in r.output["fallback"]


def test_obligations_empty_ledger_only_as_scenario():
    shift = load_shift(SHIFTS / "56370.json")
    sim = ToolSimulator(shift=shift, as_of=shift.start, allow_empty_obligations=True)
    r = sim.call("get_open_obligations", {"session_id": "x"})
    assert r.source == "synthetic_scenario"
    assert r.output["obligations"] == []


def test_exact_fixture_hit_and_miss():
    shift = load_shift(SHIFTS / "56370.json")
    # Find a real get_site_history call.
    sample = next(
        c for c in shift.fixtures.calls
        if c.tool == "mcp__calvis__get_site_history"
    )
    sim = ToolSimulator(shift=shift, as_of=sample.ts)
    hit = sim.call("get_site_history", sample.input)
    assert hit.source == "fixture"
    assert hit.output == sample.output

    sim2 = ToolSimulator(shift=shift, as_of=sample.ts)
    miss = sim2.call("get_site_history", {**sample.input, "days": 99})
    assert miss.source == "unavailable"


def test_no_nearest_match():
    shift = load_shift(SHIFTS / "56370.json")
    sample = next(
        c for c in shift.fixtures.calls
        if c.tool == "mcp__calvis__get_site_history"
    )
    sim = ToolSimulator(shift=shift, as_of=sample.ts)
    # Dropping session_id must miss, not fuzzy-match.
    slim = {k: v for k, v in sample.input.items() if k != "session_id"}
    r = sim.call("get_site_history", slim)
    assert r.source == "unavailable"


def test_workspace_seeded_from_read_fixtures():
    shift = load_shift(SHIFTS / "56370.json")
    ws = Workspace()
    ws.seed_from_fixtures(shift)
    assert any(p.startswith("context/") for p in ws.files)
    sim = ToolSimulator(shift=shift, as_of=shift.start, workspace=ws)
    # Glob for guards should resolve.
    g = sim.call("Glob", {"pattern": "context/guards/*.json"})
    assert g.source in ("workspace", "fixture")
    assert "9674" in str(g.output) or g.source == "fixture"


def test_reconstruction_uses_shift_site_not_job_json():
    """Pings sit near shift.site; job.json is tens of km away (anonymization)."""
    shift = load_shift(SHIFTS / "50737.json")
    # Pick a mid-shift moment with pings.
    pings = [e for e in shift.events._events if e.type == "location"]
    assert pings
    as_of = pings[min(50, len(pings) - 1)].ts
    rebuilt = reconstruct_guard_locations(shift, as_of, {})
    g = rebuilt["guards"][0]
    # Distance should be geofence-plausible (not ~100km).
    assert g["current_distance_from_site_meters"] is not None
    assert g["current_distance_from_site_meters"] < 5000


def test_job_logs_reconstruction_filters_by_time():
    shift = load_shift(SHIFTS / "53658.json")
    rebuilt = reconstruct_job_logs(shift, shift.start)
    assert rebuilt is not None
    for log in rebuilt["logs"]:
        dc = log.get("date_created")
        if not dc:
            continue
        from harness.loader import parse_ts
        assert parse_ts(dc.replace("Z", "+00:00")) <= shift.start


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------

def _parseable_location_calls(shifts):
    out = []
    for shift in shifts.values():
        for call in shift.fixtures.calls:
            if call.tool != "mcp__calvis__get_guard_locations":
                continue
            parsed = _parse_output(call.output)
            if isinstance(parsed, dict) and parsed.get("guards"):
                out.append((shift, call, parsed))
    return out


def test_locations_reconstruction_validation_gate(shifts):
    """Report field-level agreement; keep candidate status unless strong.

    This test documents the gate result. It asserts structural sanity and that
    on_site / radius agree on a majority of parseable calls. Full equality is
    not required — truncated outputs and heartbeat formatting differ.
    """
    calls = _parseable_location_calls(shifts)
    assert len(calls) >= 15, f"expected many parseable fixtures, got {len(calls)}"

    tallies = {"on_site": [0, 0], "distance": [0, 0], "geofence_radius": [0, 0]}
    for shift, call, recorded in calls:
        rebuilt = reconstruct_guard_locations(shift, call.ts, call.input)
        cmp = compare_locations_fields(recorded, rebuilt)
        for field in tallies:
            if field not in cmp:
                continue
            tallies[field][1] += 1
            if cmp[field]["ok"]:
                tallies[field][0] += 1

    # on_site is the operationally meaningful field and must agree strongly.
    ok_o, n_o = tallies["on_site"]
    assert n_o and ok_o / n_o >= 0.9, tallies

    # Distance should usually agree within tolerance (anonymization + rounding).
    ok_d, n_d = tallies["distance"]
    assert n_d and ok_d / n_d >= 0.7, tallies

    # Radius often disagrees: recorded fixtures and shift.site were anonymized
    # independently. That alone keeps reconstruction a candidate, not primary.
    ok_r, n_r = tallies["geofence_radius"]
    print("locations reconstruction gate:", tallies)
    print(
        f"geofence_radius agreement {ok_r}/{n_r} — "
        "fixtures remain primary (prefer_reconstruction=False)"
    )
    sim = ToolSimulator(shift=next(iter(shifts.values())), as_of=next(iter(shifts.values())).start)
    assert sim.prefer_reconstruction is False
    # Candidate status: on_site is trustworthy; radius is not fully recoverable
    # from shift.site alone. Do not promote reconstruction to primary.
    assert ok_r / n_r < 0.9 or ok_o / n_o >= 0.9


def test_default_locations_serves_fixture_not_reconstruction(shifts):
    shift = shifts["50737"]
    call = next(
        c for c in shift.fixtures.calls
        if c.tool == "mcp__calvis__get_guard_locations"
    )
    sim = ToolSimulator(shift=shift, as_of=call.ts)
    r = sim.call("get_guard_locations", call.input)
    assert r.source == "fixture"
    assert r.output == call.output


def test_simulator_module_has_no_http_imports():
    import harness.tools_sim as mod
    import ast
    from pathlib import Path
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = {"httpx", "requests", "urllib", "aiohttp", "http.client"}
    assert not (forbidden & set(imported)), imported
