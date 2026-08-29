"""Anthropic ModelAdapter — transport only."""

from __future__ import annotations

import os
import time
from typing import Any

from ..schemas import Usage
from .base import ModelMessage, ModelRequest, ModelResponse, ToolCallRequest, ToolDef


# Approximate pricing USD / MTok (input, output).
_PRICE = {
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (0.80, 4.0),
}


def _cost(model: str, inp: int, out: int, cached: int = 0) -> float:
    pin, pout = _PRICE.get(model, (3.0, 15.0))
    # Cached reads billed at ~0.1x input on Anthropic; approximate.
    return (inp - cached) * pin / 1e6 + cached * pin * 0.1 / 1e6 + out * pout / 1e6


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-6", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    @property
    def capabilities(self) -> dict:
        return {"prompt_caching": True, "seed": False, "network": True}

    def _client_or_raise(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        client = self._client_or_raise()
        tools = [_tool_to_anthropic(t) for t in request.tools]
        messages = [_msg_to_anthropic(m) for m in request.messages if m.role != "system"]

        params = {
            "model": self.model,
            "system": request.system,
            "messages": messages,
            "max_tokens": request.params.get("max_tokens", 4096),
        }
        # Newer Anthropic SDKs dropped top-level `temperature`; only pass it
        # when the installed client still accepts it.
        temp = request.params.get("temperature")
        if temp is not None:
            import inspect
            if "temperature" in inspect.signature(client.messages.create).parameters:
                params["temperature"] = temp
        if tools:
            params["tools"] = tools

        t0 = time.perf_counter()
        resp = client.messages.create(**params)
        latency = (time.perf_counter() - t0) * 1000

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    input=dict(block.input or {}),
                ))

        usage_in = getattr(resp.usage, "input_tokens", 0) or 0
        usage_out = getattr(resp.usage, "output_tokens", 0) or 0
        cached = getattr(resp.usage, "cache_read_input_tokens", 0) or 0

        return ModelResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "end_turn",
            usage=Usage(
                input_tokens=usage_in,
                output_tokens=usage_out,
                cached_tokens=cached,
                cost_usd=_cost(self.model, usage_in, usage_out, cached),
                latency_ms=latency,
            ),
            raw=resp,
        )


def _tool_to_anthropic(t: ToolDef) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_schema or {"type": "object", "properties": {}},
    }


def _msg_to_anthropic(m: ModelMessage) -> dict:
    if m.role == "tool":
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": m.content or "",
            }],
        }
    if m.role == "assistant" and m.tool_calls:
        content: list[Any] = []
        if m.content:
            content.append({"type": "text", "text": m.content})
        for tc in m.tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        return {"role": "assistant", "content": content}
    return {"role": m.role, "content": m.content or ""}
