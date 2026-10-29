"""Provider abstraction so other local inference engines (llama.cpp server, LM Studio...) can be added."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    """Base class for provider failures. `kind` lets the UI show a specific message."""

    kind = "error"


class ProviderUnavailable(ProviderError):
    kind = "unavailable"


class ModelNotFound(ProviderError):
    kind = "model_not_found"


class ToolsUnsupported(ProviderError):
    """The selected model cannot use native tool calls; callers fall back to a text protocol."""

    kind = "tools_unsupported"


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_name: str | None = None


@dataclass
class GenerationOptions:
    temperature: float | None = None
    top_p: float | None = None
    num_ctx: int | None = None
    max_output_tokens: int | None = None
    keep_alive: str | None = None
    # JSON schema (dict) or "json" to constrain output, where the provider supports it.
    response_format: dict[str, Any] | str | None = None
    # Native tool definitions (OpenAI/Ollama function schema); None = no tools.
    tools: list[dict[str, Any]] | None = None


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    stats: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ModelInfo:
    name: str
    size_bytes: int | None = None
    modified_at: str | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None


@dataclass
class HealthStatus:
    available: bool
    version: str | None = None
    error: str | None = None
    url: str = ""


class LLMProvider(Protocol):
    name: str

    async def health(self) -> HealthStatus: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def loaded_models(self) -> list[dict[str, Any]]: ...

    def chat_stream(
        self, model: str, messages: list[ChatMessage], options: GenerationOptions
    ) -> AsyncIterator[StreamChunk]: ...

    async def chat(self, model: str, messages: list[ChatMessage], options: GenerationOptions) -> str: ...

    def pull(self, model: str) -> AsyncIterator[dict[str, Any]]: ...

    async def unload(self, model: str) -> None: ...

    async def embed(self, model: str, texts: list[str], keep_alive: str | None = None) -> list[list[float]]: ...
