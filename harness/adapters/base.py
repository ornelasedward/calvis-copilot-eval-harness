"""ModelAdapter protocol — transport only.

The replay engine, tool simulator, traces, evals, and dashboard must not know
which provider is running. Provider SDKs stay behind this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..schemas import Usage


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultMessage:
    tool_call_id: str
    name: str
    content: Any


@dataclass
class ModelMessage:
    role: str  # system|user|assistant|tool
    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict


@dataclass
class ModelRequest:
    system: str
    messages: list[ModelMessage]
    tools: list[ToolDef] = field(default_factory=list)
    params: dict = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCallRequest]
    stop_reason: str
    usage: Usage
    raw: Any = None


@runtime_checkable
class ModelAdapter(Protocol):
    name: str

    @property
    def capabilities(self) -> dict:
        ...

    def complete(self, request: ModelRequest) -> ModelResponse:
        ...
