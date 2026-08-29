"""Safe tool simulator for historical shift replay.

Policies (locked by architecture):

* Read tools that can be rebuilt from events are *reconstruction candidates*.
  Until field-level validation against recorded outputs passes, and as permanent
  fallback on miss, they serve time-scoped exact fixtures.
* Exact-fixture tools: exact (tool, input) match as-of timestamp, else
  data_unavailable. No nearest-match.
* Action tools: record proposed action, return synthesized success, never execute.
* Never-called tools (get_open_obligations, create_*): default data_unavailable.
  A synthetic empty obligations ledger exists only as an explicit scenario fixture.
* Workspace tools (Read/Write/Glob/Grep): in-memory tree seeded from fixtures.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .loader import FixtureCall, Shift, canonical_input, parse_ts
from .schemas import SchemaSource, ToolSource, ToolUseRecord


def _short(name: str) -> str:
    return name.replace("mcp__calvis__", "")


def _parse_output(output: Any) -> Any:
    if not isinstance(output, str):
        return output
    try:
        return json.loads(output)
    except (ValueError, TypeError):
        return output  # truncated or non-JSON string; serve as recorded


def unavailable(tool: str, reason: str, schema_source: SchemaSource = "recorded") -> ToolUseRecord:
    payload = {"status": "data_unavailable", "tool": tool, "reason": reason}
    return ToolUseRecord(
        tool=tool,
        input={},
        output=payload,
        source="unavailable",
        schema_source=schema_source,
        unavailable_reason=reason,
    )


# ---------------------------------------------------------------------------
# Geodesy helpers for reconstruction candidates
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _local_stamp(ts: datetime, tz_name: str) -> str:
    local = ts.astimezone(ZoneInfo(tz_name))
    return local.strftime("%a %b %-d, %-I:%M %p %Z").replace(" 0", " ") if False else (
        # Windows strftime lacks %-d / %-I; build manually.
        f"{local.strftime('%a %b')} {local.day}, "
        f"{local.strftime('%I').lstrip('0') or '0'}:{local.strftime('%M %p %Z')}"
    )


# ---------------------------------------------------------------------------
# In-memory workspace
# ---------------------------------------------------------------------------

class Workspace:
    """Simulated agent working directory for Read/Write/Glob/Grep."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def seed_from_fixtures(self, shift: Shift) -> None:
        for call in shift.fixtures.calls:
            short = _short(call.tool)
            if short != "Read":
                continue
            path = (call.input or {}).get("file_path") or (call.input or {}).get("path")
            if not path:
                continue
            out = call.output
            if isinstance(out, str):
                # Recorded Read outputs are often a JSON-encoded string.
                try:
                    decoded = json.loads(out)
                    if isinstance(decoded, str):
                        out = decoded
                except (ValueError, TypeError):
                    pass
            if path not in self.files:
                self.files[path] = out if isinstance(out, str) else json.dumps(out)

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def write(self, path: str, content: str) -> str:
        self.files[path] = content
        return f"Wrote {len(content)} chars to {path}"

    def glob(self, pattern: str) -> list[str]:
        # Minimal glob: supports trailing * and exact paths.
        if pattern.endswith("/*") or pattern.endswith("/*.json"):
            prefix = pattern.rsplit("/", 1)[0] + "/"
            suffix = ".json" if pattern.endswith(".json") else ""
            return sorted(
                p for p in self.files
                if p.startswith(prefix) and (not suffix or p.endswith(suffix))
            )
        if "*" not in pattern:
            return [pattern] if pattern in self.files else []
        # naive contains match on the non-star parts
        parts = pattern.split("*")
        return sorted(
            p for p in self.files
            if p.startswith(parts[0]) and p.endswith(parts[-1])
        )


# ---------------------------------------------------------------------------
# Reconstruction candidates
# ---------------------------------------------------------------------------

def reconstruct_guard_locations(shift: Shift, as_of: datetime, tool_input: dict) -> dict:
    """Build a get_guard_locations-shaped payload from events + shift.site.

    Uses shift.site coordinates (consistent with pings). Does NOT use job.json
    geography. Field coverage is intentionally partial; validation decides
    whether this becomes the primary strategy.
    """
    site = shift.context["site"]
    site_lat, site_lng = site["lat"], site["lng"]
    radius = site.get("geofence_radius_m") or 150
    tz = shift.timezone

    pings = shift.events.as_of(as_of, types={"location"})
    tele = shift.events.as_of(as_of, types={"telemetry"})
    job_logs = shift.events.as_of(as_of, types={"job_log"})

    roster = shift.fixtures.guard_roster()
    guard_name = (shift.context.get("guard") or {}).get("name") or (
        roster[0]["name"] if roster else "Unknown"
    )
    guard_id = roster[0]["id"] if roster else None

    include_pings = bool(tool_input.get("include_pings"))

    if not pings:
        guard = {
            "guard_id": guard_id,
            "guard_name": guard_name,
            "on_site": False,
            "current_distance_from_site_meters": None,
            "geofence_type": "radius",
            "geo_fence_radius_meters": radius,
            "last_ping_accuracy_meters": None,
            "last_ping_at": None,
            "last_ping_age_seconds": None,
            "stale_ping_duration_minutes": None,
            "ping_count": 0,
            "moved_distance_meters": 0.0,
            "is_stationary": None,
            "off_site_duration_minutes": None,
            "off_site_max_distance_meters": None,
            "last_geofence_event": None,
            "heartbeat_trend": [],
            "last_ping_at_local": None,
        }
    else:
        last = pings[-1]
        dist = haversine_m(last.data["lat"], last.data["lng"], site_lat, site_lng)
        on_site = dist <= radius
        moved = 0.0
        for a, b in zip(pings, pings[1:]):
            moved += haversine_m(a.data["lat"], a.data["lng"], b.data["lat"], b.data["lng"])

        # last geofence event from job_log categories
        last_geo = None
        for ev in reversed(job_logs):
            cat = (ev.data.get("category") or "").lower()
            if cat in ("entered geofence", "exited geofence"):
                last_geo = {
                    "event": "entered" if "entered" in cat else "exited",
                    "at": ev.ts.isoformat(),
                    "at_local": _local_stamp(ev.ts, tz),
                }
                break

        age_s = max(0.0, (as_of - last.ts).total_seconds())
        heartbeat = []
        for t in tele[-20:]:
            heartbeat.append({
                "at": t.ts.isoformat(),
                "battery_level": t.data.get("battery"),
                "battery_state": t.data.get("battery_state"),
                "scene_phase": None,
                "motion_state": t.data.get("motion"),
                "motion_confidence": "unknown",
                "pedometer_step_count": t.data.get("steps") or 0,
                "pedometer_distance_m": t.data.get("distance_m") or 0.0,
                "barometer_pressure_kpa": None,
                "barometer_altitude_m": t.data.get("altitude"),
                "heading_magnetic_deg": t.data.get("heading"),
                "heading_true_deg": None,
                "at_local": _local_stamp(t.ts, tz),
            })

        guard = {
            "guard_id": guard_id,
            "guard_name": guard_name,
            "on_site": on_site,
            "current_distance_from_site_meters": round(dist, 1),
            "geofence_type": "radius",
            "geo_fence_radius_meters": radius,
            "last_ping_accuracy_meters": last.data.get("accuracy_m"),
            "last_ping_at": last.ts.isoformat(),
            "last_ping_age_seconds": int(age_s),
            "stale_ping_duration_minutes": round(age_s / 60.0, 1),
            "ping_count": len(pings),
            "moved_distance_meters": round(moved, 1),
            "is_stationary": moved < 50 and len(pings) > 3,
            "off_site_duration_minutes": None if on_site else None,
            "off_site_max_distance_meters": None,
            "last_geofence_event": last_geo,
            "heartbeat_trend": heartbeat,
            "last_ping_at_local": _local_stamp(last.ts, tz),
        }

    locations: list[dict] = []
    if include_pings:
        locations = [
            {
                "lat": e.data["lat"],
                "lng": e.data["lng"],
                "accuracy_m": e.data.get("accuracy_m"),
                "at": e.ts.isoformat(),
            }
            for e in pings
        ]

    return {
        "locations": locations,
        "guards": [guard],
        "count": len(locations),
        "summary": None,
        "locations_omitted": not include_pings and len(pings) > 0,
        "pings_available": len(pings),
        "_reconstruction": True,
    }


def reconstruct_job_logs(shift: Shift, as_of: datetime) -> dict | None:
    """Filter the latest parseable get_job_logs fixture to records as-of `as_of`.

    Pre-shift records exist only in fixtures, not events, so pure event
    reconstruction is impossible. Returns None if no parseable fixture exists.
    """
    candidates = [
        c for c in shift.fixtures.calls
        if _short(c.tool) == "get_job_logs" and c.ts <= as_of
    ]
    if not candidates:
        # Fall back to any parseable fixture for the shift (pre-shift data only).
        candidates = [c for c in shift.fixtures.calls if _short(c.tool) == "get_job_logs"]
    parsed = None
    source_ts = None
    for c in reversed(candidates):
        out = _parse_output(c.output)
        if isinstance(out, dict) and "logs" in out:
            parsed = out
            source_ts = c.ts
            break
    if parsed is None:
        return None

    kept = []
    for log in parsed.get("logs") or []:
        dc = log.get("date_created")
        if not dc:
            kept.append(log)
            continue
        try:
            # Recorded stamps use trailing Z.
            ts = parse_ts(dc.replace("Z", "+00:00"))
        except ValueError:
            kept.append(log)
            continue
        if ts <= as_of:
            kept.append(log)
    return {
        "logs": kept,
        "count": len(kept),
        "_reconstruction": True,
        "_source_fixture_ts": source_ts.isoformat() if source_ts else None,
    }


# ---------------------------------------------------------------------------
# ToolSimulator
# ---------------------------------------------------------------------------

ACTION_TOOLS = {
    "request_copilot_dm",
    "add_copilot_note",
    "escalate_to_ops",
    "escalate_to_human",
    "flag_copilot_guard",
    "create_copilot_alert",
    "create_copilot_task",
    "create_feature_request",
}

WORKSPACE_TOOLS = {"Read", "Write", "Glob", "Grep"}

# Never called in the bundle; schemas partially from prompt text.
NEVER_CALLED = {
    "get_open_obligations": "prompt_text",
    "create_copilot_alert": "approximated",
    "create_copilot_task": "approximated",
    "create_feature_request": "approximated",
    "save_chat_image": "prompt_text",
}

RECONSTRUCTION_CANDIDATES = {"get_guard_locations", "get_job_logs"}

FIXTURE_READ_TOOLS = {
    "get_site_history",
    "get_copilot_message_history",
    "get_job_chat_messages",
    "get_copilot_context",
    "get_job_communications",
    "get_guard_status",
    "get_job_incidents",
    "get_entity_summary",
    "fetch_chat_image",
}


@dataclass
class ActionCapture:
    kind: str
    tool: str
    input: dict
    ts: datetime


@dataclass
class ToolSimulator:
    shift: Shift
    as_of: datetime
    workspace: Workspace = field(default_factory=Workspace)
    allow_empty_obligations: bool = False
    prefer_reconstruction: bool = False
    actions: list[ActionCapture] = field(default_factory=list)
    records: list[ToolUseRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.workspace.files:
            self.workspace.seed_from_fixtures(self.shift)

    def call(self, tool: str, tool_input: dict | None = None) -> ToolUseRecord:
        tool_input = tool_input or {}
        short = _short(tool)
        full = tool if tool.startswith("mcp__") or tool in WORKSPACE_TOOLS else f"mcp__calvis__{tool}"
        # Normalize: callers may pass short or full names.
        if short in WORKSPACE_TOOLS:
            full = short
        elif not tool.startswith("mcp__") and short not in WORKSPACE_TOOLS:
            full = f"mcp__calvis__{short}"

        record = self._dispatch(full, short, tool_input)
        # Fill input on the record if the helper left it empty.
        if not record.input:
            record.input = tool_input
        self.records.append(record)
        return record

    def _dispatch(self, full: str, short: str, tool_input: dict) -> ToolUseRecord:
        if short == "ToolSearch":
            return self._tool_search(tool_input)

        if short in WORKSPACE_TOOLS:
            return self._workspace(short, tool_input)

        if short in ACTION_TOOLS:
            return self._action(full, short, tool_input)

        if short == "get_open_obligations":
            return self._obligations(tool_input)

        if short in RECONSTRUCTION_CANDIDATES:
            return self._reconstruction_or_fixture(full, short, tool_input)

        if short in FIXTURE_READ_TOOLS or short.startswith("get_"):
            return self._exact_fixture(full, short, tool_input)

        return ToolUseRecord(
            tool=full,
            input=tool_input,
            output={
                "status": "data_unavailable",
                "tool": short,
                "reason": f"unknown tool `{short}` — no fixture and no simulator handler",
            },
            source="unavailable",
            schema_source="approximated",
            unavailable_reason="unknown_tool",
        )

    def _tool_search(self, tool_input: dict) -> ToolUseRecord:
        listing = sorted(
            FIXTURE_READ_TOOLS
            | RECONSTRUCTION_CANDIDATES
            | ACTION_TOOLS
            | WORKSPACE_TOOLS
            | set(NEVER_CALLED)
            | {"ToolSearch"}
        )
        return ToolUseRecord(
            tool="ToolSearch",
            input=tool_input,
            output={"tools": listing},
            source="workspace",
            schema_source="prompt_text",
        )

    def _workspace(self, short: str, tool_input: dict) -> ToolUseRecord:
        if short == "Read":
            path = tool_input.get("file_path") or tool_input.get("path") or ""
            content = self.workspace.read(path)
            if content is None:
                # Try exact fixture for this Read call.
                hit = self.shift.fixtures.exact("Read", tool_input, as_of=self.as_of)
                if hit is None:
                    hit = self.shift.fixtures.exact("Read", {"file_path": path}, as_of=self.as_of)
                if hit is not None:
                    out = hit.output
                    return ToolUseRecord(
                        tool="Read", input=tool_input, output=out,
                        source="fixture", schema_source="recorded",
                    )
                return ToolUseRecord(
                    tool="Read", input=tool_input,
                    output={"status": "data_unavailable", "reason": f"file not in workspace: {path}"},
                    source="unavailable", schema_source="recorded",
                    unavailable_reason="missing_workspace_file",
                )
            return ToolUseRecord(
                tool="Read", input=tool_input, output=content,
                source="workspace", schema_source="recorded",
            )
        if short == "Write":
            path = tool_input.get("file_path") or tool_input.get("path") or "workspace/analysis.md"
            content = tool_input.get("content", "")
            msg = self.workspace.write(path, content)
            self.actions.append(ActionCapture("write", "Write", tool_input, self.as_of))
            return ToolUseRecord(
                tool="Write", input=tool_input, output=msg,
                source="action_recorded", schema_source="recorded",
            )
        if short == "Glob":
            pattern = tool_input.get("pattern") or tool_input.get("glob_pattern") or "*"
            matches = self.workspace.glob(pattern)
            # Fall back to recorded Glob output if workspace empty for pattern.
            if not matches:
                hit = self.shift.fixtures.exact("Glob", tool_input, as_of=self.as_of)
                if hit is not None:
                    return ToolUseRecord(
                        tool="Glob", input=tool_input, output=hit.output,
                        source="fixture", schema_source="recorded",
                    )
            # Production returns a newline-joined string for single-line results.
            out: Any = "\n".join(matches) if matches else ""
            return ToolUseRecord(
                tool="Glob", input=tool_input, output=out,
                source="workspace", schema_source="recorded",
            )
        if short == "Grep":
            hit = self.shift.fixtures.exact("Grep", tool_input, as_of=self.as_of)
            if hit is not None:
                return ToolUseRecord(
                    tool="Grep", input=tool_input, output=hit.output,
                    source="fixture", schema_source="recorded",
                )
            return ToolUseRecord(
                tool="Grep", input=tool_input,
                output={"status": "data_unavailable", "reason": "no Grep fixture for this input"},
                source="unavailable", schema_source="recorded",
                unavailable_reason="no_grep_fixture",
            )
        raise AssertionError(short)

    def _action(self, full: str, short: str, tool_input: dict) -> ToolUseRecord:
        self.actions.append(ActionCapture(short, full, tool_input, self.as_of))
        schema: SchemaSource = "approximated" if short in NEVER_CALLED else "recorded"
        if short == "request_copilot_dm":
            out: Any = {
                "ok": True,
                "delivered_via": "streaming",
                "dm_request": {"status": "approved"},
            }
        elif short == "add_copilot_note":
            out = {"ok": True, "note_id": str(uuid.uuid4())}
        elif short == "escalate_to_human":
            out = {"posted": True, "status": "escalated", "severity": "critical"}
        elif short in ("escalate_to_ops", "flag_copilot_guard", "create_copilot_alert",
                       "create_copilot_task", "create_feature_request"):
            title = (
                tool_input.get("title")
                or tool_input.get("details")
                or tool_input.get("reason")
                or short
            )
            out = {
                "task_id": str(uuid.uuid4()),
                "status": "pending_approval",
                "title": str(title)[:120],
                "task_type": short,
            }
        else:
            out = {"ok": True}
        return ToolUseRecord(
            tool=full, input=tool_input, output=out,
            source="action_recorded", schema_source=schema,
        )

    def _obligations(self, tool_input: dict) -> ToolUseRecord:
        if self.allow_empty_obligations:
            return ToolUseRecord(
                tool="mcp__calvis__get_open_obligations",
                input=tool_input,
                output={"obligations": [], "count": 0, "_scenario": "empty_ledger"},
                source="synthetic_scenario",
                schema_source="prompt_text",
            )
        return ToolUseRecord(
            tool="mcp__calvis__get_open_obligations",
            input=tool_input,
            output={
                "status": "data_unavailable",
                "error": "tool_unavailable",
                "tool": "get_open_obligations",
                "reason": (
                    "No recorded fixture for get_open_obligations in this bundle "
                    "(the tool was never called on these historical shifts). "
                    "This is NOT an empty ledger — do not infer that nothing is owed."
                ),
                "fallback": (
                    "Follow the documented prompt fallback: read the Job Requirements "
                    "/ shift instructions in context (and analysis.md if present) for "
                    "what this post owes. Do not invent obligation windows. Continue "
                    "the turn's required actions (e.g. session_start welcome still sends)."
                ),
            },
            source="unavailable",
            schema_source="prompt_text",
            unavailable_reason="never_called_in_bundle",
        )

    def _exact_fixture(self, full: str, short: str, tool_input: dict) -> ToolUseRecord:
        hit = self.shift.fixtures.exact(full, tool_input, as_of=self.as_of)
        if hit is None and not full.startswith("mcp__"):
            hit = self.shift.fixtures.exact(f"mcp__calvis__{short}", tool_input, as_of=self.as_of)
        if hit is None:
            # Also try short-name keys if fixtures stored that way (they don't).
            return ToolUseRecord(
                tool=full, input=tool_input,
                output={
                    "status": "data_unavailable",
                    "tool": short,
                    "reason": (
                        f"No exact fixture for {short} with this input as of {self.as_of.isoformat()}"
                    ),
                },
                source="unavailable", schema_source="recorded",
                unavailable_reason="no_exact_fixture",
            )
        return ToolUseRecord(
            tool=full, input=tool_input, output=hit.output,
            source="fixture", schema_source="recorded",
        )

    def _reconstruction_or_fixture(self, full: str, short: str, tool_input: dict) -> ToolUseRecord:
        hit = self.shift.fixtures.exact(full, tool_input, as_of=self.as_of)
        if hit is None:
            hit = self.shift.fixtures.exact(f"mcp__calvis__{short}", tool_input, as_of=self.as_of)

        if not self.prefer_reconstruction and hit is not None:
            return ToolUseRecord(
                tool=full, input=tool_input, output=hit.output,
                source="fixture", schema_source="recorded",
            )

        if short == "get_guard_locations":
            rebuilt = reconstruct_guard_locations(self.shift, self.as_of, tool_input)
            return ToolUseRecord(
                tool=full, input=tool_input, output=rebuilt,
                source="reconstructed", schema_source="inferred",
            )
        if short == "get_job_logs":
            rebuilt = reconstruct_job_logs(self.shift, self.as_of)
            if rebuilt is not None:
                return ToolUseRecord(
                    tool=full, input=tool_input, output=rebuilt,
                    source="reconstructed", schema_source="inferred",
                )
            if hit is not None:
                return ToolUseRecord(
                    tool=full, input=tool_input, output=hit.output,
                    source="fixture", schema_source="recorded",
                )
            return ToolUseRecord(
                tool=full, input=tool_input,
                output={
                    "status": "data_unavailable",
                    "tool": short,
                    "reason": "No parseable get_job_logs fixture to filter",
                },
                source="unavailable", schema_source="recorded",
                unavailable_reason="no_parseable_job_logs_fixture",
            )

        if hit is not None:
            return ToolUseRecord(
                tool=full, input=tool_input, output=hit.output,
                source="fixture", schema_source="recorded",
            )
        return ToolUseRecord(
            tool=full, input=tool_input,
            output={"status": "data_unavailable", "tool": short, "reason": "no fixture"},
            source="unavailable", schema_source="recorded",
            unavailable_reason="no_exact_fixture",
        )


def compare_locations_fields(
    recorded: dict, rebuilt: dict, distance_tol_m: float = 50.0
) -> dict:
    """Field-level comparison for the reconstruction validation gate."""
    rg = (recorded.get("guards") or [None])[0] or {}
    bg = (rebuilt.get("guards") or [None])[0] or {}
    results = {}

    def check(name: str, ok: bool, detail: Any = None) -> None:
        results[name] = {"ok": ok, "detail": detail}

    if not rg:
        check("has_recorded_guard", False, "recorded missing guards")
        return results

    check("on_site", rg.get("on_site") == bg.get("on_site"),
          {"recorded": rg.get("on_site"), "rebuilt": bg.get("on_site")})

    rd, bd = rg.get("current_distance_from_site_meters"), bg.get("current_distance_from_site_meters")
    if rd is None or bd is None:
        check("distance", rd == bd, {"recorded": rd, "rebuilt": bd})
    else:
        check("distance", abs(float(rd) - float(bd)) <= distance_tol_m,
              {"recorded": rd, "rebuilt": bd, "tol": distance_tol_m})

    rp, bp = rg.get("ping_count"), bg.get("ping_count")
    # Rebuilt counts all pings as-of; recorded may subsample. Allow rebuilt >= recorded.
    if rp is None or bp is None:
        check("ping_count", False, {"recorded": rp, "rebuilt": bp})
    else:
        check("ping_count_rebuilt_gte", int(bp) >= int(rp),
              {"recorded": rp, "rebuilt": bp})

    check("geofence_radius",
          rg.get("geo_fence_radius_meters") == bg.get("geo_fence_radius_meters"),
          {"recorded": rg.get("geo_fence_radius_meters"),
           "rebuilt": bg.get("geo_fence_radius_meters")})

    return results
