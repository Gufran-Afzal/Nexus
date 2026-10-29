from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core.state import NexusState
from app.database.models import Workspace


def get_state(request: Request) -> NexusState:
    return request.app.state.nexus


def get_session(request: Request) -> Iterator[Session]:
    with get_state(request).db.session() as s:
        yield s


def get_workspace(s: Session, ws_id: int) -> Workspace:
    ws = s.get(Workspace, ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


def sse(event: dict[str, Any]) -> str:
    kind = event.get("event", "message")
    payload = {k: v for k, v in event.items() if k != "event"}
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"


async def sse_stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for ev in events:
        yield sse(ev)
