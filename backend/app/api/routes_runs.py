"""Automation: runs (inspect, approve, decline, cancel, resume), the capability registry and its self-check, saved workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_state
from app.core.state import NexusState
from app.services.audit import audit
from app.services.preferences import load_preferences

router = APIRouter(prefix="/api")


def _auto(state: NexusState) -> Any:
    return state.conversations.automation


def _view(state: NexusState, run_id: int) -> dict[str, Any]:
    try:
        return _auto(state).runtime.view(run_id)
    except KeyError:
        raise HTTPException(404, "Run not found") from None


@router.get("/runs")
def list_runs(limit: int = 30, state: NexusState = Depends(get_state)) -> list[dict[str, Any]]:
    return _auto(state).runtime.list_runs(max(1, min(limit, 100)))


@router.get("/runs/{run_id}")
async def get_run(run_id: int, refresh: bool = False, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    if refresh:  # work the user decided elsewhere (a reviewed edit, an approved command) is picked up here
        return await _auto(state).runtime.refresh(run_id)
    return _view(state, run_id)


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: int, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    view = await _auto(state).runtime.approve(run_id, True)
    with state.db.session() as s:
        audit(s, "user", "run.approve", view["goal"][:200], outcome=view["status"])
    state.conversations.assistant.store.clear()
    state.conversations.assistant._publish()
    return view


@router.post("/runs/{run_id}/reject")
async def reject_run(run_id: int, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    view = await _auto(state).runtime.approve(run_id, False)
    with state.db.session() as s:
        audit(s, "user", "run.reject", view["goal"][:200], outcome=view["status"])
    state.conversations.assistant.store.clear()
    state.conversations.assistant._publish()
    return view


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    return await _auto(state).runtime.cancel(run_id)


class ResumeIn(BaseModel):
    redo: bool = False  # repeat a step whose outcome is unknown even though repeating might do it twice


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: int, body: ResumeIn | None = None, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    return await _auto(state).runtime.resume(run_id, redo=bool(body and body.redo))


class WorkflowIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    keywords: list[str] = Field(default_factory=list, max_length=6)


@router.post("/runs/{run_id}/save-workflow")
def save_workflow(run_id: int, body: WorkflowIn, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    _view(state, run_id)
    ok, msg = _auto(state).runtime.save_workflow(run_id, body.name, body.keywords)
    if not ok:
        raise HTTPException(409, msg)
    return {"message": msg}


@router.get("/workflows")
def workflows(state: NexusState = Depends(get_state)) -> list[dict[str, Any]]:
    from app.agent.plan import BUILTIN_WORKFLOWS

    built = [{"name": k, "description": v["description"], "keywords": v["keywords"], "builtin": True, "uses": 0, "steps": []}
             for k, v in BUILTIN_WORKFLOWS.items()]
    return built + _auto(state).runtime.saved_workflows()


@router.get("/capabilities")
async def capabilities(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    auto = _auto(state)
    with state.db.session() as s:
        prefs = load_preferences(s)
    return {"tools": auto.registry.describe(), "models": await _models(state), "environment": _environment(),
            "agent": prefs.agent.model_dump(),
            "limitations": ["Windows are observed through the window list only: no accessibility tree and no screenshots.",
                            "No vision model is used. Models that could read screenshots are only flagged.",
                            "A tool that times out is abandoned, not killed.",
                            "Sending a message cannot be confirmed as delivered."]}


@router.post("/capabilities/diagnose")
def diagnose(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Probes every tool's availability. Changes nothing on the computer."""
    rows = _auto(state).registry.diagnose()
    return {"ok": sum(1 for r in rows if r["ok"]), "total": len(rows), "tools": rows}


async def _models(state: NexusState) -> dict[str, Any]:
    try:
        names = [m.name for m in await state.provider.list_models()]
    except Exception:
        names = []
    vision = [n for n in names if any(k in n.lower() for k in ("llava", "qwen2.5vl", "minicpm-v", "llama3.2-vision"))]
    with state.db.session() as s:
        active = load_preferences(s).active_model
    return {"active": active, "installed": names, "vision_capable_but_unused": vision,
            "reachable": bool((state.ollama_status or {}).get("available"))}


def _environment() -> dict[str, Any]:
    import platform

    import psutil

    ram = round(psutil.virtual_memory().total / 2**30, 1)
    return {"platform": platform.system(), "ram_gb": ram, "cpus": psutil.cpu_count(logical=True)}
