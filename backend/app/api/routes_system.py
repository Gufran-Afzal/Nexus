from __future__ import annotations

import datetime as dt
import json
import os
import platform
import sys
import threading
import time
from typing import Any

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_session, get_state, sse
from app.core.state import NexusState
from app.database import models as m
from app.services.audit import audit
from app.services.preferences import load_preferences, save_preferences
from app.windows.startup import StartupUnsupported

router = APIRouter(prefix="/api")


async def refresh_ollama(state: NexusState) -> dict[str, Any]:
    h = await state.provider.health()
    status: dict[str, Any] = {"available": h.available, "version": h.version, "error": h.error, "url": h.url,
                              "checked_at": time.time()}
    if h.available:
        try:
            status["loaded"] = await state.provider.loaded_models()
        except Exception as e:  # /api/ps unsupported or failed; not fatal
            status["loaded_error"] = str(e)
    if status.get("available") != state.ollama_status.get("available"):
        state.bus.publish("ollama", available=h.available, error=h.error)
    state.ollama_status = status
    return status


@router.get("/health/live")
def live() -> dict[str, bool]:
    return {"ok": True}


@router.get("/health")
async def health(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    ollama = await refresh_ollama(state)
    with state.db.session() as s:
        prefs = load_preferences(s)
        db_version = state.db.current_version()
    vm = psutil.virtual_memory()
    return {
        "version": __version__,
        "uptime_s": time.time() - state.started_at,
        "paused": state.paused,
        "ollama": ollama,
        "active_model": prefs.active_model,
        "onboarding_complete": prefs.onboarding_complete,
        "generations": state.llm.status(),
        "database": {"path": str(state.db.path), "schema_version": db_version,
                     "recovered_from": str(state.db.recovered_from) if state.db.recovered_from else None},
        "voice": {k: v for k, v in state.voice.status().items() if k in ("enabled", "state", "muted")},
        "system": {
            "platform": platform.platform(), "python": sys.version.split()[0],
            "cpu_percent": psutil.cpu_percent(interval=None), "cpu_count": psutil.cpu_count(),
            "memory_total": vm.total, "memory_available": vm.available,
        },
        "startup": state.startup.status(),
    }


@router.get("/events")
async def events(request: Request, state: NexusState = Depends(get_state)) -> StreamingResponse:
    async def gen():  # type: ignore[no-untyped-def]
        yield sse({"event": "hello", "paused": state.paused})
        async for ev in state.bus.subscribe():
            if await request.is_disconnected():
                break
            yield sse({"event": "update", **ev})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/system/pause")
def pause(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, bool]:
    state.paused = True
    state.voice.set_paused(True)
    state.llm.stop_all()
    audit(s, "user", "assistant.paused")
    state.bus.publish("system", event="paused")
    return {"paused": True}


@router.post("/system/resume")
def resume(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, bool]:
    state.paused = False
    state.voice.set_paused(False)
    audit(s, "user", "assistant.resumed")
    state.bus.publish("system", event="resumed_by_user")
    return {"paused": False}


@router.post("/system/shutdown")
def shutdown(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, bool]:
    """Graceful full exit: stops running commands, saves state, closes the tray. Used by scripts\\stop.ps1."""
    if state.request_shutdown is None:
        raise HTTPException(409, "NEXUS was not started by the desktop launcher; stop the server process directly.")
    audit(s, "user", "system.shutdown")
    s.commit()
    threading.Timer(0.2, state.request_shutdown).start()  # respond first, then exit
    return {"shutting_down": True}


class StartupBody(BaseModel):
    enabled: bool


@router.get("/system/startup")
def startup_status(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return state.startup.status()


@router.post("/system/startup")
def set_startup(body: StartupBody, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict:
    try:
        result = state.startup.enable() if body.enabled else state.startup.disable()
    except StartupUnsupported as e:
        raise HTTPException(400, str(e)) from e
    audit(s, "user", "startup.enabled" if body.enabled else "startup.disabled")
    return result


def _dir_size(path: Any) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for dirpath, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


@router.get("/system/storage")
def storage(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    st = state.settings
    counts = {
        "conversations": s.query(func.count(m.Conversation.id)).scalar(),
        "messages": s.query(func.count(m.Message.id)).scalar(),
        "workspaces": s.query(func.count(m.Workspace.id)).scalar(),
        "change_sets": s.query(func.count(m.ChangeSet.id)).scalar(),
        "command_runs": s.query(func.count(m.CommandRun.id)).scalar(),
        "tasks": s.query(func.count(m.AgentTask.id)).scalar(),
        "notes": s.query(func.count(m.Note.id)).scalar(),
        "reminders": s.query(func.count(m.Reminder.id)).scalar(),
        "audit_records": s.query(func.count(m.AuditLog.id)).scalar(),
    }
    return {
        "data_dir": str(st.data_dir),
        "database": {"path": str(st.db_path), "bytes": st.db_path.stat().st_size if st.db_path.exists() else 0},
        "logs": {"path": str(st.logs_dir), "bytes": _dir_size(st.logs_dir)},
        "backups": {"path": str(st.backups_dir), "bytes": _dir_size(st.backups_dir)},
        "models": {"path": str(st.models_dir), "bytes": _dir_size(st.models_dir)},
        "counts": counts,
        "what_is_stored": [
            "Conversation text (your messages and NEXUS's replies), including transcribed voice requests",
            "Which workspace files were used as context (paths only, not file contents)",
            "Proposed and applied change sets (file contents before/after, so changes can be reviewed and reverted)",
            "Command runs with their output (secrets redacted)",
            "Notes, to-dos, reminders, preferences, approved project summaries",
            "An audit log of security-relevant actions",
            "Raw audio is never stored. Full source files are only stored as part of change sets you create.",
        ],
    }


@router.post("/system/backup")
def backup(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, str]:
    dest = state.settings.backups_dir / f"nexus-db-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    state.db.backup(dest)
    audit(s, "user", "database.backup", str(dest))
    return {"path": str(dest)}


@router.get("/system/diagnostics")
async def diagnostics(state: NexusState = Depends(get_state)) -> Response:
    data: dict[str, Any] = {"health": await health(state), "voice": state.voice.status(),
            "settings": {"ollama_url": state.settings.ollama_url, "host": state.settings.host,
                         "port": state.settings.port, "data_dir": str(state.settings.data_dir)}}
    log_tail = ""
    log_file = state.settings.logs_dir / "nexus.log"
    if log_file.exists():
        log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-20000:]
    data["log_tail"] = log_tail  # already redacted by the logging filter
    body = json.dumps(data, indent=2, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="nexus-diagnostics.json"'})


@router.get("/system/logs")
def logs(state: NexusState = Depends(get_state), lines: int = 300) -> dict[str, Any]:
    log_file = state.settings.logs_dir / "nexus.log"
    if not log_file.exists():
        return {"path": str(log_file), "lines": []}
    text = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(log_file), "lines": text[-min(lines, 2000):]}


@router.get("/audit")
def audit_log(s: Session = Depends(get_session), limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    rows = s.query(m.AuditLog).order_by(m.AuditLog.id.desc()).offset(offset).limit(min(limit, 500)).all()
    return [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action, "target": r.target,
             "outcome": r.outcome, "detail": r.detail} for r in rows]


@router.get("/preferences")
def get_prefs(s: Session = Depends(get_session)) -> dict[str, Any]:
    return load_preferences(s).model_dump()


@router.patch("/preferences")
def patch_prefs(body: dict[str, Any], state: NexusState = Depends(get_state),
                s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        prefs = save_preferences(s, body)
    except KeyError as e:
        raise HTTPException(400, f"Unknown preference: {e}") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    audit(s, "user", "preferences.updated", ",".join(body.keys()))
    s.commit()
    if "voice" in body:
        state.voice.reload()
    return prefs.model_dump()


psutil.cpu_percent(None)  # prime the counter so the first /system/stats call has a real interval to measure


@router.get("/system/stats")
async def system_stats(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    """Live numbers for the dashboard widgets: computer load plus how the model is performing."""
    vm = psutil.virtual_memory()
    out: dict[str, Any] = {
        "cpu_percent": psutil.cpu_percent(None),
        "memory": {"percent": vm.percent, "used_gb": round(vm.used / 2**30, 1), "total_gb": round(vm.total / 2**30, 1)},
        "battery": None,
    }
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is not None:
        out["battery"] = {"percent": b.percent, "charging": bool(b.power_plugged)}
    prefs = load_preferences(s)
    loaded = (state.ollama_status or {}).get("loaded") or []
    names = {str(x.get("name")) for x in loaded}
    from app.services.preferences import model_for

    model = model_for(prefs, "fast")
    recent = s.query(m.PerfSample).order_by(m.PerfSample.id.desc()).limit(5).all()
    speeds = [r.tokens_per_s for r in recent if r.tokens_per_s]
    out["model"] = {
        "name": model,
        "ollama": bool((state.ollama_status or {}).get("available")),
        "loaded": model in names or f"{model}:latest" in names,
        "tokens_per_s": round(sum(speeds) / len(speeds), 1) if speeds else None,
        "last_load_s": next((round(r.load_s, 1) for r in recent if r.load_s), None),
    }
    return out
