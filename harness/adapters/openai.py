"""OpenAI ModelAdapter — transport only."""

from __future__ import annotations

import inspect
import json
import os
import time
from typing import Any

from ..schemas import Usage
from .base import ModelMessage, ModelRequest, ModelResponse, ToolCallRequest, ToolDef


# USD per million tokens (input, output). Keep approximate; update as needed.
_PRICE = {
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5.6": (4.0, 20.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.50, 15.0),
    "gpt-5.6-luna": (1.0, 6.0),
}


def _cost(model: str, inp: int, out: int) -> float:
    pin, pout = _PRICE.get(model, (4.0, 20.0))
    return inp * pin / 1e6 + out * pout / 1e6


def _is_gpt5_family(model: str) -> bool:
    return model.startswith("gpt-5")


class OpenAIAdapter:
    name = "openai"

    def __init__(self, model: str = "gpt-5.6-sol", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = None

    @property
    def capabilities(self) -> dict:
        return {
            "prompt_caching": False,
            "seed": not _is_gpt5_family(self.model),
            "network": True,
            "reasoning_effort": _is_gpt5_family(self.model),
        }

    def _client_or_raise(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("OPENAI_API_KEY not set")
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        client = self._client_or_raise()
        messages = [{"role": "system", "content": request.system}]
        messages += [_msg_to_openai(m) for m in request.messages]

        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }

        # GPT-5.x chat completions often reject temperature / prefer reasoning_effort.
        sig = inspect.signature(client.chat.completions.create)
        if _is_gpt5_family(self.model):
            # Function tools on Chat Completions require reasoning_effort=none
            # for gpt-5.6-sol; use Responses API later for efforted tool loops.
            effort = request.params.get("reasoning_effort", "none")
            if request.tools and effort not in (None, "none"):
                effort = "none"
            if "reasoning_effort" in sig.parameters:
                params["reasoning_effort"] = effort or "none"
            max_out = request.params.get("max_tokens", 4096)
            if "max_completion_tokens" in sig.parameters:
                params["max_completion_tokens"] = max_out
            elif "max_tokens" in sig.parameters:
                params["max_tokens"] = max_out
        else:
            if "temperature" in sig.parameters:
                params["temperature"] = request.params.get("temperature", 0)
            if "seed" in request.params and "seed" in sig.parameters:
                params["seed"] = request.params["seed"]

        if request.tools:
            params["tools"] = [_tool_to_openai(t) for t in request.tools]

        t0 = time.perf_counter()
        resp = client.chat.completions.create(**params)
        latency = (time.perf_counter() - t0) * 1000

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in msg.tool_calls or []:
            args = tc.function.arguments
            try:
                parsed = json.loads(args) if isinstance(args, str) else (args or {})
            except json.JSONDecodeError:
                parsed = {"_raw": args}
            tool_calls.append(ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                input=parsed,
            ))

        usage_in = getattr(resp.usage, "prompt_tokens", 0) or 0
        usage_out = getattr(resp.usage, "completion_tokens", 0) or 0

        return ModelResponse(
            text=msg.content,
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "stop",
            usage=Usage(
                input_tokens=usage_in,
                output_tokens=usage_out,
                cost_usd=_cost(self.model, usage_in, usage_out),
                latency_ms=latency,
            ),
            raw=resp,
        )


def _tool_to_openai(t: ToolDef) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema or {"type": "object", "properties": {}},
        },
    }


def _msg_to_openai(m: ModelMessage) -> dict:
    if m.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": m.tool_call_id,
            "content": m.content or "",
        }
    if m.role == "assistant" and m.tool_calls:
        return {
            "role": "assistant",
            "content": m.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.input),
                    },
                }
                for tc in m.tool_calls
            ],
        }
    return {"role": m.role, "content": m.content or ""}
