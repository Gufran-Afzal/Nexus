from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.base import (
    ChatMessage,
    GenerationOptions,
    HealthStatus,
    ModelInfo,
    ModelNotFound,
    ProviderUnavailable,
    StreamChunk,
    ToolCall,
    ToolsUnsupported,
)


class FakeProvider:
    """In-memory stand-in for Ollama so tests never need a real model."""

    name = "fake"

    def __init__(self) -> None:
        self.available = True
        self.models = ["qwen2.5-coder:7b"]
        self.reply: Callable[[list[ChatMessage]], str] | str = "Hello from the fake model."
        self.calls: list[dict[str, Any]] = []
        self.token_delay = 0.0
        self.fail_times = 0  # raise ProviderUnavailable this many times first
        self.supports_tools = True

    async def health(self) -> HealthStatus:
        if not self.available:
            return HealthStatus(available=False, error="connection refused", url="fake")
        return HealthStatus(available=True, version="0.0-test", url="fake")

    async def list_models(self) -> list[ModelInfo]:
        if not self.available:
            raise ProviderUnavailable("connection refused")
        return [ModelInfo(name=n, size_bytes=4_700_000_000, parameter_size="7.6B", quantization="Q4_K_M")
                for n in self.models]

    async def loaded_models(self) -> list[dict[str, Any]]:
        return []

    async def chat_stream(self, model: str, messages: list[ChatMessage],
                          options: GenerationOptions) -> AsyncIterator[StreamChunk]:
        self.calls.append({"model": model, "messages": messages, "options": options})
        if not self.available:
            raise ProviderUnavailable("Cannot reach Ollama")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderUnavailable("temporary failure")
        if model not in self.models:
            raise ModelNotFound(f'model "{model}" not found, try pulling it first')
        if options.tools and not self.supports_tools:
            raise ToolsUnsupported("registry.ollama.ai/library/x does not support tools")
        result = self.reply(messages, options) if callable(self.reply) and _arity(self.reply) == 2 else (
            self.reply(messages) if callable(self.reply) else self.reply)
        if isinstance(result, dict):  # {"tool_calls": [(name, args), ...], "text": "..."}
            calls = [ToolCall(n, a) for n, a in result.get("tool_calls", [])]
            yield StreamChunk(text=result.get("text", ""), tool_calls=calls)
            yield StreamChunk(done=True, stats={"output_tokens": 5})
            return
        text = result
        words = text.split(" ")
        for i, w in enumerate(words):
            if self.token_delay:
                await asyncio.sleep(self.token_delay)
            yield StreamChunk(text=w + (" " if i < len(words) - 1 else ""))
        yield StreamChunk(done=True, stats={"prompt_tokens": 10, "output_tokens": len(words), "tokens_per_s": 5.0})

    async def chat(self, model: str, messages: list[ChatMessage], options: GenerationOptions) -> str:
        return "".join([c.text async for c in self.chat_stream(model, messages, options)])

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        yield {"status": "pulling manifest"}
        yield {"status": "downloading", "total": 100, "completed": 50}
        self.models.append(model)
        yield {"status": "success"}

    async def unload(self, model: str) -> None:
        return None

    async def embed(self, model: str, texts: list[str], keep_alive: str | None = None) -> list[list[float]]:
        """Deterministic bag-of-words vectors so semantic ranking is testable."""
        if not self.available:
            raise ProviderUnavailable("Cannot reach Ollama")
        if model not in self.models:
            raise ModelNotFound(f'model "{model}" not found')
        self.embed_calls = getattr(self, "embed_calls", 0) + 1
        vocab = ["money", "currency", "price", "total", "invoice", "user", "login", "password", "math", "add"]
        return [[float(t.lower().count(w)) + 0.01 for w in vocab] for t in texts]


def _arity(fn: Callable[..., Any]) -> int:
    import inspect

    try:
        return len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return 1


class FakeRegistry:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, command: str) -> None:
        self.value = command

    def delete(self) -> None:
        self.value = None


class FakeAudio:
    def __init__(self, frames: list[bytes] | None = None, ok: bool = True) -> None:
        self.ok = ok
        self._frames = frames or []

    def available(self) -> tuple[bool, str]:
        return (True, "ok") if self.ok else (False, "No microphone found")

    def devices(self) -> list[dict[str, Any]]:
        return [{"index": 0, "name": "Fake mic", "default": True}] if self.ok else []

    def frames(self, device: int | None, stop: Any) -> Iterator[bytes]:
        while self._frames and not stop.is_set():
            yield self._frames.pop(0)
        stop.wait(0.05)


class FakeSTT:
    def __init__(self, text: str = "what time is it", ok: bool = True) -> None:
        self.text, self.ok = text, ok
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return (True, "ok") if self.ok else (False, "model not downloaded")

    def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls += 1
        return self.text

    def transcribe_file(self, data: bytes) -> str:
        self.calls += 1
        return self.text


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = 0

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", background_tasks=False, frontend_dist=tmp_path / "nofrontend",
                    command_timeout_s=30)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def launched() -> list[tuple[Any, str]]:
    return []


@pytest.fixture
def voice_parts() -> dict[str, Any]:
    return {"audio": FakeAudio(), "stt": FakeSTT(), "tts": FakeTTS()}


@pytest.fixture
def app(settings: Settings, provider: FakeProvider, launched: list, voice_parts: dict[str, Any]):  # type: ignore[no-untyped-def]
    return create_app(
        settings,
        provider=provider,
        startup_backend=FakeRegistry(),
        launcher=lambda target, kind: launched.append((target, kind)),
        voice_options={
            "audio": voice_parts["audio"],
            "stt_factory": lambda p: voice_parts["stt"],
            "tts_factory": lambda p: voice_parts["tts"],
            "ack": lambda: None,
        },
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app, headers={"X-Nexus-Client": "test"}) as c:
        yield c


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=supersecretvalue123\n", encoding="utf-8")
    (root / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Calc\nA tiny calculator. The add function is buggy.\n", encoding="utf-8")
    (root / "NEXUS.md").write_text("Use 4-space indentation. Run tests with pytest.\n", encoding="utf-8")
    return root


def parse_sse(text: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in text.strip().split("\n\n"):
        name, data = "message", "{}"
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        events.append((name, json.loads(data)))
    return events
