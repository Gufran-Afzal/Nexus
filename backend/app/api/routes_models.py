from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_state, sse
from app.core.state import NexusState
from app.database.models import PerfSample
from app.providers.base import ProviderError, ProviderUnavailable
from app.services.audit import audit
from app.services.preferences import load_preferences, save_preferences

router = APIRouter(prefix="/api/models")
MODEL_NAME = re.compile(r"^[A-Za-z0-9._\-/:]{1,200}$")


class ModelName(BaseModel):
    name: str


def _check_name(name: str) -> str:
    if not MODEL_NAME.match(name):
        raise HTTPException(422, "Invalid model name")
    return name


@router.get("")
async def list_models(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    with state.db.session() as s:
        active = load_preferences(s).active_model
    try:
        models = await state.provider.list_models()
    except ProviderUnavailable as e:
        return {"available": False, "error": str(e), "models": [], "active": active, "active_installed": False}
    except ProviderError as e:
        return {"available": True, "error": str(e), "models": [], "active": active, "active_installed": False}
    try:
        loaded = await state.provider.loaded_models()
    except ProviderError:
        loaded = []
    names = {mi.name for mi in models}
    return {
        "available": True,
        "models": [mi.__dict__ for mi in models],
        "loaded": loaded,
        "active": active,
        "active_installed": active in names or f"{active}:latest" in names,
    }


@router.post("/active")
async def set_active(body: ModelName, state: NexusState = Depends(get_state),
                     s: Session = Depends(get_session)) -> dict[str, str]:
    name = _check_name(body.name)
    save_preferences(s, {"active_model": name})
    audit(s, "user", "model.selected", name)
    return {"active": name}


@router.post("/pull")
async def pull(body: ModelName, state: NexusState = Depends(get_state)) -> StreamingResponse:
    """Explicit, user-initiated model download through Ollama. Streams progress."""
    name = _check_name(body.name)
    with state.db.session() as s:
        audit(s, "user", "model.pull_started", name)

    async def gen():  # type: ignore[no-untyped-def]
        try:
            async for p in state.provider.pull(name):
                yield sse({"event": "progress", **p})
            yield sse({"event": "done", "status": "success"})
            with state.db.session() as s:
                audit(s, "user", "model.pulled", name)
        except ProviderError as e:
            yield sse({"event": "error", "message": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/unload")
async def unload(body: ModelName, state: NexusState = Depends(get_state)) -> dict[str, str]:
    try:
        await state.provider.unload(_check_name(body.name))
    except ProviderError as e:
        raise HTTPException(502, str(e)) from e
    return {"status": "unloaded"}


@router.get("/performance")
def performance(s: Session = Depends(get_session), limit: int = 50) -> list[dict[str, Any]]:
    rows = s.query(PerfSample).order_by(PerfSample.id.desc()).limit(min(limit, 500)).all()
    return [{"ts": r.ts, "model": r.model, "prompt_tokens": r.prompt_tokens, "output_tokens": r.output_tokens,
             "tokens_per_s": r.tokens_per_s, "load_s": r.load_s, "total_s": r.total_s} for r in rows]


@router.post("/warm")
async def warm(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Load the model now (the UI calls this when it opens). Never downloads anything."""
    from app.services.warmup import warm_up

    with state.db.session() as s:
        prefs = load_preferences(s)
    return await warm_up(state.llm, prefs)
