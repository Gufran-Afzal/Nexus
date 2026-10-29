"""Model access layer: concurrency limits, cancellation and real generation state."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.providers.base import ChatMessage, GenerationOptions, LLMProvider, StreamChunk


@dataclass
class Generation:
    id: str
    model: str
    purpose: str
    started: float = field(default_factory=time.time)
    state: str = "queued"  # queued | loading | generating | tools | done | stopped | error
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    multi: bool = False  # several model calls (agent rounds) share this generation; caller calls finish()


class GenerationStopped(Exception):
    pass


class LLMService:
    def __init__(self, provider: LLMProvider, max_concurrent: int = 1) -> None:
        self.provider = provider
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self.active: dict[str, Generation] = {}

    def new_generation(self, model: str, purpose: str) -> Generation:
        g = Generation(id=uuid.uuid4().hex[:12], model=model, purpose=purpose)
        self.active[g.id] = g
        return g

    def stop(self, gen_id: str) -> bool:
        g = self.active.get(gen_id)
        if not g:
            return False
        g.cancel.set()
        return True

    def stop_all(self) -> int:
        for g in self.active.values():
            g.cancel.set()
        return len(self.active)

    def status(self) -> list[dict[str, object]]:
        return [
            {"id": g.id, "model": g.model, "purpose": g.purpose, "state": g.state, "elapsed_s": time.time() - g.started}
            for g in self.active.values()
        ]

    async def stream(
        self, gen: Generation, messages: list[ChatMessage], options: GenerationOptions
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion. Raises GenerationStopped if cancelled; always unregisters the generation."""
        try:
            async with self._sem:
                if gen.cancel.is_set():
                    gen.state = "stopped"
                    raise GenerationStopped()
                gen.state = "loading"
                agen = self.provider.chat_stream(gen.model, messages, options)
                try:
                    async for chunk in agen:
                        if gen.cancel.is_set():
                            gen.state = "stopped"
                            raise GenerationStopped()
                        gen.state = "generating"
                        yield chunk
                finally:
                    aclose = getattr(agen, "aclose", None)
                    if aclose is not None:
                        await aclose()  # closes the HTTP stream so Ollama stops generating
                gen.state = "done"
        except GenerationStopped:
            raise
        except Exception:
            gen.state = "error"
            raise
        finally:
            if not gen.multi or gen.cancel.is_set():
                self.active.pop(gen.id, None)

    def finish(self, gen: Generation) -> None:
        self.active.pop(gen.id, None)

    async def complete(self, model: str, messages: list[ChatMessage], options: GenerationOptions,
                       purpose: str = "agent", gen: Generation | None = None) -> str:
        gen = gen or self.new_generation(model, purpose)
        parts = [c.text async for c in self.stream(gen, messages, options)]
        return "".join(parts)
