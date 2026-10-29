"""In-process publish/subscribe used to push live state (voice, reminders, tasks) to the UI over SSE."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Synchronous listener (e.g. the Windows tray, for toast notifications)."""
        self._listeners.append(fn)

    def publish(self, event_type: str, /, **data: Any) -> None:
        event = {"type": event_type, "ts": time.time(), **data}
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # listener failures must never break the publisher
                log.exception("event listener failed")
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for q in list(self._subscribers):
            if running is not None and (loop is None or running is loop):
                self._put(q, event)
            elif loop is not None:
                loop.call_soon_threadsafe(self._put, q, event)

    @staticmethod
    def _put(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)
