"""Conversation history reconstruction.

Guard messages arrive as threaded history, not in the turn message (verified
against turn_message/guard_message.md). History is the chronological interleave
of:

* guard_message events from the shift's event stream
* copilot messages recovered via the verified 1:1 DM text pairing

Turn-batch attribution is intentionally NOT attempted — rapid exchanges can
place a waking message inside the previous turn's array window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .loader import Shift, parse_ts


Role = Literal["guard", "copilot"]


@dataclass(frozen=True)
class ChatMessage:
    ts: datetime
    role: Role
    text: str
    image: str | None = None
    image_url: str | None = None
    image_meta: dict | None = None


def _paired_copilot_messages(shift: Shift) -> list[ChatMessage]:
    """Pair copilot_message rows to request_copilot_dm by sequential text match.

    Verified 324/324 across the bundle. Mirror rows are timestamped earlier than
    their tool-call records (streaming delivery); we use the mirror timestamp
    because that is when the guard would have seen the text.
    """
    mirrors = [e for e in shift.baseline if e.get("type") == "copilot_message"]
    dms = [
        e for e in shift.baseline
        if e.get("type") == "tool_call"
        and e.get("tool") == "mcp__calvis__request_copilot_dm"
    ]
    if len(mirrors) != len(dms):
        # Fall back to mirrors alone if pairing length mismatches (shouldn't).
        return [
            ChatMessage(ts=parse_ts(m["ts"]), role="copilot", text=m.get("text") or "")
            for m in mirrors
        ]
    out = []
    for mirror, dm in zip(mirrors, dms):
        body = (dm.get("input") or {}).get("body") or mirror.get("text") or ""
        out.append(ChatMessage(ts=parse_ts(mirror["ts"]), role="copilot", text=body))
    return out


def _guard_messages(shift: Shift) -> list[ChatMessage]:
    return [
        ChatMessage(
            ts=e.ts,
            role="guard",
            text=e.data.get("text") or "",
            image=e.data.get("image"),
            image_url=e.data.get("image_url") or e.data.get("imageUrl"),
            image_meta=e.data.get("image_meta") or e.data.get("imageMeta"),
        )
        for e in shift.events.as_of(
            # Use a far-future cutoff; callers filter with `as_of`.
            parse_ts("2099-01-01T00:00:00+00:00"),
            types={"guard_message"},
        )
    ]


class ThreadManager:
    """Builds provider-neutral chat history for a wake."""

    def __init__(self, shift: Shift):
        self.shift = shift
        self._guard = _guard_messages(shift)
        self._baseline_copilot = _paired_copilot_messages(shift)
        self._variant_copilot: list[ChatMessage] = []

    def reset_variant(self) -> None:
        self._variant_copilot.clear()

    def record_variant_message(self, ts: datetime, text: str) -> None:
        self._variant_copilot.append(ChatMessage(ts=ts, role="copilot", text=text))

    def record_guard_message(
        self,
        ts: datetime,
        text: str,
        *,
        image: str | None = None,
        image_url: str | None = None,
        image_meta: dict | None = None,
    ) -> None:
        """Inject a live (scripted) guard message into shift-mode history."""
        self._guard.append(
            ChatMessage(
                ts=ts,
                role="guard",
                text=text or "",
                image=image,
                image_url=image_url,
                image_meta=image_meta,
            )
        )
        self._guard.sort(key=lambda m: m.ts)

    def history_as_of(
        self,
        as_of: datetime,
        *,
        mode: Literal["turn", "shift", "baseline"] = "shift",
    ) -> list[ChatMessage]:
        """Return messages with ts <= as_of, chronologically.

        * turn / baseline modes thread the production (baseline) copilot messages
        * shift mode threads the variant's own accumulated messages
        """
        copilot = (
            self._baseline_copilot
            if mode in ("turn", "baseline")
            else self._variant_copilot
        )
        messages = [m for m in self._guard if m.ts <= as_of]
        messages += [m for m in copilot if m.ts <= as_of]
        messages.sort(key=lambda m: (m.ts, 0 if m.role == "guard" else 1))
        return messages

    def as_model_messages(
        self,
        as_of: datetime,
        *,
        mode: Literal["turn", "shift", "baseline"] = "shift",
    ) -> list[dict]:
        """OpenAI/Anthropic-style role/content list.

        Guard → user, copilot → assistant. Image placeholders become a text note.
        """
        out = []
        for m in self.history_as_of(as_of, mode=mode):
            content = m.text
            if m.image or m.image_url:
                photo = "[photo]"
                if m.image_url:
                    photo = f"[photo]\nimage_url: {m.image_url}"
                content = (content + "\n" + photo).strip() if content else photo
            role = "user" if m.role == "guard" else "assistant"
            out.append({"role": role, "content": content})
        return out
