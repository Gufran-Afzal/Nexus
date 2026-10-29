"""The Apps panel: every app found on this PC, which of them NEXUS may drive, and test buttons."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_state
from app.core.state import NexusState
from app.services import appcontrol, appintents
from app.services.actions import DEFAULT_SYSTEM, default_launcher
from app.services.audit import audit
from app.services.preferences import IntegratedApp, Preferences, load_preferences, save_preferences
from app.windows.appcatalog import DiscoveredApp, app_id

router = APIRouter(prefix="/api/apps")


def _integrated_dict(a: IntegratedApp) -> dict[str, Any]:
    ref = appcontrol.AppRef(a.name, a.kind, a.target, a.args, a.process)
    return {**a.model_dump(), "id": app_id(a.name), "profile": appcontrol.profile(ref), "examples": appcontrol.examples(ref),
            "can_close": bool(ref.exe_name)}


def _migrate_legacy(s: Session) -> Preferences:
    """The old "name = exe" list becomes ordinary integrated apps (once)."""
    prefs = load_preferences(s)
    if prefs.allowed_apps:
        apps = list(prefs.integrated_apps)
        have = {appcontrol.norm(a.name) for a in apps}
        for name, exe in prefs.allowed_apps.items():
            if appcontrol.norm(name) not in have:
                apps.append(IntegratedApp(name=name.title(), kind="exe", target=exe,
                                          process=appcontrol.process_for(exe), source="legacy"))
        prefs = save_preferences(s, {"integrated_apps": [a.model_dump() for a in apps], "allowed_apps": {}})
    return prefs


def _payload(state: NexusState, s: Session) -> dict[str, Any]:
    prefs = _migrate_legacy(s)
    have = {app_id(a.name) for a in prefs.integrated_apps}
    cat = state.catalog
    return {
        "platform": cat.platform, "scanned_at": cat.scanned_at, "error": cat.error,
        "integrated": [_integrated_dict(a) for a in sorted(prefs.integrated_apps, key=lambda a: a.name.lower())],
        "discovered": [{**a.as_dict(), "integrated": a.id in have} for a in cat.apps],
    }


@router.get("")
def list_apps(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    return _payload(state, s)


@router.post("/scan")
async def scan(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    await asyncio.to_thread(state.catalog.scan)
    with state.db.session() as s:
        return _payload(state, s)


def _to_integrated(d: DiscoveredApp, aliases: list[str] | None = None) -> IntegratedApp:
    return IntegratedApp(name=d.name, aliases=aliases or [], kind=d.kind, target=d.target,  # type: ignore[arg-type]
                         args=d.args, process=d.process or appcontrol.process_for(d.target), source=d.source)


class IntegrateIn(BaseModel):
    id: str | None = None  # an app from the scan list
    name: str | None = Field(None, max_length=80)  # or just its name, typed by hand
    path: str | None = Field(None, max_length=500)  # only if NEXUS can't find it by name
    aliases: list[str] = Field(default_factory=list, max_length=10)


@router.post("/integrate")
def integrate(body: IntegrateIn, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    prefs = _migrate_legacy(s)
    d: DiscoveredApp | None = None
    if body.id:
        d = state.catalog.by_id(body.id)
        if not d:
            raise HTTPException(404, "That app is not in the scan list. Scan again.")
    elif body.path:
        p = Path(body.path.strip().strip('"'))
        if p.suffix.lower() != ".exe" or not p.is_file():
            raise HTTPException(422, f"'{p}' is not an existing .exe file.")
        d = DiscoveredApp(app_id(body.name or p.stem), body.name or p.stem, "exe", str(p), [], p.name, "manual")
    elif body.name:
        d = state.catalog.guess(body.name)
        if not d:
            raise HTTPException(422, f"I couldn't find an app called '{body.name}' on this PC. "
                                     "Scan again, or give its .exe path.")
    else:
        raise HTTPException(422, "Give an app id, a name, or a path.")
    app = _to_integrated(d, [a.strip() for a in body.aliases if a.strip()])
    save_preferences(s, {"integrated_apps": [a.model_dump() for a in appcontrol.add_or_update(prefs, app)]})
    audit(s, "user", "apps.integrate", app.name, kind=app.kind)
    return _integrated_dict(app)


class ManyIn(BaseModel):
    ids: list[str] = Field(..., max_length=500)


@router.post("/integrate-many")
def integrate_many(body: ManyIn, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    prefs = _migrate_legacy(s)
    apps = {appcontrol.norm(a.name): a for a in prefs.integrated_apps}
    added = 0
    for i in body.ids:
        d = state.catalog.by_id(i)
        if d and appcontrol.norm(d.name) not in apps:
            apps[appcontrol.norm(d.name)] = _to_integrated(d)
            added += 1
    save_preferences(s, {"integrated_apps": [a.model_dump() for a in apps.values()]})
    audit(s, "user", "apps.integrate_many", f"{added} apps")
    return {"added": added}


class AppPatch(BaseModel):
    aliases: list[str] | None = Field(None, max_length=10)
    enabled: bool | None = None
    name: str | None = Field(None, min_length=1, max_length=80)


def _find_by_id(prefs: Preferences, id_: str) -> IntegratedApp:
    a = next((x for x in prefs.integrated_apps if app_id(x.name) == id_), None)
    if not a:
        raise HTTPException(404, "That app is not integrated")
    return a


@router.patch("/integrated/{id_}")
def update(id_: str, body: AppPatch, s: Session = Depends(get_session)) -> dict[str, Any]:
    prefs = _migrate_legacy(s)
    a = _find_by_id(prefs, id_)
    if body.aliases is not None:
        a.aliases = [x.strip() for x in body.aliases if x.strip()]
    if body.enabled is not None:
        a.enabled = body.enabled
    if body.name:
        a.name = body.name.strip()
    save_preferences(s, {"integrated_apps": [x.model_dump() for x in prefs.integrated_apps]})
    return _integrated_dict(a)


@router.delete("/integrated/{id_}")
def remove(id_: str, s: Session = Depends(get_session)) -> dict[str, bool]:
    prefs = _migrate_legacy(s)
    a = _find_by_id(prefs, id_)
    save_preferences(s, {"integrated_apps": [x.model_dump() for x in prefs.integrated_apps if x is not a]})
    audit(s, "user", "apps.remove", a.name)
    return {"removed": True}


@router.post("/integrated/{id_}/open")
def open_app(id_: str, request_state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, str]:
    prefs = _migrate_legacy(s)
    a = _find_by_id(prefs, id_)
    ref = appcontrol.AppRef(a.name, a.kind, a.target, a.args, a.process)
    try:
        appintents.launch_app(ref, request_state.conversations.launcher)
    except Exception as e:
        raise HTTPException(409, f"Could not start {a.name}: {e}") from e
    audit(s, "user", "apps.open", a.name)
    return {"message": f"Started {a.name}."}


@router.post("/integrated/{id_}/close")
def close_app(id_: str, s: Session = Depends(get_session)) -> dict[str, str]:
    prefs = _migrate_legacy(s)
    a = _find_by_id(prefs, id_)
    status, msg = appintents.run("close_app", {"name": a.name}, prefs, default_launcher, DEFAULT_SYSTEM)
    if status != "succeeded":
        raise HTTPException(409, msg)
    audit(s, "user", "apps.close", a.name)
    return {"message": msg}
