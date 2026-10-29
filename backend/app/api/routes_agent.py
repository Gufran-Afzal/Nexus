"""Change sets, commands and tasks. These endpoints are the ONLY way edits and commands get executed,
and they are only called by the user's UI actions — the model has no access to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import edits
from app.agent.proposer import generate_file_edits
from app.agent.tasks import TEMPLATES, TaskError
from app.api.deps import get_session, get_state, get_workspace
from app.core.state import NexusState
from app.database.models import AgentTask, ChangeSet, CommandRun
from app.providers.base import ChatMessage, GenerationOptions, ProviderError
from app.services.chat import SAFETY_RULES
from app.services.preferences import load_preferences
from app.tools.command_service import CommandError, serialize
from app.tools.commands import classify, parse_command
from app.workspaces.security import AccessDenied

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ change sets
def cs_dict(cs: ChangeSet, with_files: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {"id": cs.id, "workspace_id": cs.workspace_id, "title": cs.title,
                         "description": cs.description, "status": cs.status, "source": cs.source,
                         "created_at": cs.created_at, "applied_at": cs.applied_at, "file_count": len(cs.files)}
    if with_files:
        files = []
        for f in cs.files:
            diff = edits.unified_diff(f.rel_path, f.base_content, f.new_content)
            files.append({"id": f.id, "path": f.rel_path, "status": f.status, "error": f.error,
                          "is_new": f.base_content is None, "is_delete": f.new_content is None,
                          "diff": diff, **edits.diff_stats(diff)})
        d["files"] = files
    return d


class FileProposal(BaseModel):
    path: str
    new_content: str | None


class CsCreate(BaseModel):
    workspace_id: int
    title: str = Field(..., min_length=1, max_length=300)
    description: str = ""
    files: list[FileProposal] = Field(..., min_length=1, max_length=50)


class CsGenerate(BaseModel):
    workspace_id: int
    instruction: str = Field(..., min_length=3, max_length=20_000)
    files: list[str] = Field(..., min_length=1, max_length=3)


class Decision(BaseModel):
    decision: Literal["accepted", "rejected", "proposed"]


def _cs(s: Session, cs_id: int) -> ChangeSet:
    cs = s.get(ChangeSet, cs_id)
    if not cs:
        raise HTTPException(404, "Change set not found")
    return cs


@router.get("/changesets")
def list_cs(workspace_id: int | None = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = s.query(ChangeSet)
    if workspace_id is not None:
        q = q.filter(ChangeSet.workspace_id == workspace_id)
    return [cs_dict(c, with_files=False) for c in q.order_by(ChangeSet.id.desc()).limit(200)]


@router.post("/changesets")
def create_cs(body: CsCreate, s: Session = Depends(get_session)) -> dict[str, Any]:
    ws = get_workspace(s, body.workspace_id)
    try:
        cs = edits.propose(s, ws, body.title, body.description, [f.model_dump() for f in body.files], "user")
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    except edits.EditError as e:
        raise HTTPException(400, str(e)) from e
    return cs_dict(cs)


@router.post("/changesets/generate")
async def generate_cs(body: CsGenerate, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Ask the local model to propose edits. The result is a PROPOSAL; nothing is written to disk."""
    with state.db.session() as s:
        ws = get_workspace(s, body.workspace_id)
        root = Path(ws.path)
        prefs = load_preferences(s)
    g = prefs.generation
    opts = GenerationOptions(temperature=0.2, top_p=g.top_p, num_ctx=g.num_ctx,
                             max_output_tokens=max(g.max_output_tokens, 4096), keep_alive=g.keep_alive)

    async def ask(prompt: str) -> str:
        msgs = [ChatMessage("system", f"{prefs.engineer_prompt}\n\n{SAFETY_RULES}"), ChatMessage("user", prompt)]
        return await state.llm.complete(prefs.active_model, msgs, opts, purpose="edit")

    try:
        changes, notes = await generate_file_edits(ask, root, body.instruction, "", body.files,
                                                   state.settings.max_file_bytes)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    except ProviderError as e:
        raise HTTPException(502, f"Model unavailable: {e}") from e
    if not changes:
        return {"changeset": None, "notes": notes}
    with state.db.session() as s:
        ws = get_workspace(s, body.workspace_id)
        try:
            cs = edits.propose(s, ws, body.instruction[:120], body.instruction, changes, "model")
        except edits.EditError as e:
            raise HTTPException(400, str(e)) from e
        return {"changeset": cs_dict(cs), "notes": notes}


@router.get("/changesets/{cs_id}")
def get_cs(cs_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    return cs_dict(_cs(s, cs_id))


@router.post("/changesets/{cs_id}/files/{file_id}/decision")
def decide(cs_id: int, file_id: int, body: Decision, s: Session = Depends(get_session)) -> dict[str, Any]:
    cs = _cs(s, cs_id)
    try:
        edits.set_file_decision(s, cs, file_id, body.decision)
    except edits.EditError as e:
        raise HTTPException(400, str(e)) from e
    return cs_dict(cs)


@router.post("/changesets/{cs_id}/decide-all")
def decide_all(cs_id: int, body: Decision, s: Session = Depends(get_session)) -> dict[str, Any]:
    cs = _cs(s, cs_id)
    for f in cs.files:
        if f.status in ("proposed", "accepted", "rejected"):
            f.status = body.decision
    if body.decision == "rejected" and all(f.status == "rejected" for f in cs.files):
        cs.status = "rejected"
    return cs_dict(cs)


@router.post("/changesets/{cs_id}/apply")
def apply_cs(cs_id: int, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict:
    cs = _cs(s, cs_id)
    ws = get_workspace(s, cs.workspace_id)
    try:
        result = edits.apply_changeset(s, ws, cs, state.settings.backups_dir)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    except edits.EditError as e:
        raise HTTPException(400, str(e)) from e
    state.bus.publish("changeset", id=cs_id, status=cs.status)
    return {**result, "changeset_detail": cs_dict(cs)}


@router.post("/changesets/{cs_id}/revert")
def revert_cs(cs_id: int, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict:
    cs = _cs(s, cs_id)
    ws = get_workspace(s, cs.workspace_id)
    try:
        result = edits.revert_changeset(s, ws, cs)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    state.bus.publish("changeset", id=cs_id, status=cs.status)
    return {**result, "changeset_detail": cs_dict(cs)}


# ------------------------------------------------------------------ commands
class CmdCreate(BaseModel):
    workspace_id: int
    command: str | list[str]
    cwd: str = "."
    timeout_s: int | None = Field(None, ge=1, le=3600)


class CmdApprove(BaseModel):
    confirm_risk: bool = False


class CmdClassify(BaseModel):
    command: str | list[str]


@router.post("/commands/classify")
def classify_cmd(body: CmdClassify) -> dict[str, Any]:
    try:
        argv = parse_command(body.command)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    c = classify(argv)
    return {"argv": argv, "risk": c.risk, "reasons": c.reasons, "blocked": c.blocked}


@router.get("/commands")
def list_cmds(workspace_id: int | None = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = s.query(CommandRun)
    if workspace_id is not None:
        q = q.filter(CommandRun.workspace_id == workspace_id)
    return [serialize(r) for r in q.order_by(CommandRun.id.desc()).limit(200)]


@router.post("/commands")
def create_cmd(body: CmdCreate, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Records the command as awaiting approval. Nothing runs yet."""
    with state.db.session() as s:
        ws = get_workspace(s, body.workspace_id)
        s.expunge(ws)
    try:
        run = state.commands.request(ws, body.command, body.cwd, "user", body.timeout_s)
    except (ValueError, AccessDenied) as e:
        raise HTTPException(400, str(e)) from e
    return serialize(run)


@router.get("/commands/{run_id}")
def get_cmd(run_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    r = s.get(CommandRun, run_id)
    if not r:
        raise HTTPException(404, "Command not found")
    return serialize(r)


@router.post("/commands/{run_id}/approve")
async def approve_cmd(run_id: int, body: CmdApprove, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    try:
        state.commands.approve(run_id, body.confirm_risk)
    except AccessDenied as e:
        raise HTTPException(403, str(e)) from e
    except CommandError as e:
        raise HTTPException(400, str(e)) from e
    with state.db.session() as s:
        r = s.get(CommandRun, run_id)
        assert r
        return serialize(r)


@router.post("/commands/{run_id}/reject")
def reject_cmd(run_id: int, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    try:
        state.commands.reject(run_id)
    except CommandError as e:
        raise HTTPException(400, str(e)) from e
    return {"rejected": True}


@router.post("/commands/{run_id}/cancel")
def cancel_cmd(run_id: int, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    return {"cancelled": state.commands.cancel(run_id)}


# ------------------------------------------------------------------ tasks
def task_dict(t: AgentTask) -> dict[str, Any]:
    return {"id": t.id, "workspace_id": t.workspace_id, "template": t.template, "title": t.title, "goal": t.goal,
            "params": t.params, "status": t.status, "summary": t.summary, "created_at": t.created_at,
            "finished_at": t.finished_at,
            "steps": [{"id": st.id, "idx": st.idx, "kind": st.kind, "description": st.description,
                       "depends_on": st.depends_on, "status": st.status, "attempts": st.attempts,
                       "max_retries": st.max_retries, "output": st.output, "ref": st.ref, "log": st.log}
                      for st in t.steps]}


class TaskCreate(BaseModel):
    template: str
    goal: str = Field(..., min_length=3, max_length=20_000)
    workspace_id: int | None = None
    files: list[str] = Field(default_factory=list, max_length=20)
    test_command: str | None = None
    error_text: str | None = Field(None, max_length=50_000)


@router.get("/tasks/templates")
def templates() -> list[dict[str, Any]]:
    return [{"id": k, "title": v["title"], "needs_workspace": v["needs_workspace"],
             "steps": [st.description for st in v["steps"]({"test_command": "<test command>"})]}
            for k, v in TEMPLATES.items()]


@router.get("/tasks")
def list_tasks(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [task_dict(t) for t in s.query(AgentTask).order_by(AgentTask.id.desc()).limit(100)]


@router.post("/tasks")
async def create_task(body: TaskCreate, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    if state.paused:
        raise HTTPException(409, "NEXUS is paused")
    params: dict[str, Any] = {"files": body.files}
    if body.test_command:
        try:
            parse_command(body.test_command)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        params["test_command"] = body.test_command
    if body.error_text:
        params["error_text"] = body.error_text
    try:
        task_id = state.tasks.create(body.template, body.goal, body.workspace_id, params)
        state.tasks.start(task_id)
    except TaskError as e:
        raise HTTPException(400, str(e)) from e
    with state.db.session() as s:
        t = s.get(AgentTask, task_id)
        assert t
        return task_dict(t)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    t = s.get(AgentTask, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return task_dict(t)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    return {"cancelled": state.tasks.cancel(task_id)}
