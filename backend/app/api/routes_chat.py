from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_state, sse_stream
from app.core.state import NexusState
from app.database.models import Conversation, Message, Workspace, utcnow
from app.services.audit import audit
from app.services.conversation import TurnRequest

router = APIRouter(prefix="/api")


def conv_dict(c: Conversation, with_messages: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"id": c.id, "title": c.title, "mode": c.mode, "workspace_id": c.workspace_id,
                         "created_at": c.created_at, "updated_at": c.updated_at}
    if with_messages:
        d["messages"] = [msg_dict(m) for m in c.messages]
    return d


def msg_dict(m: Message) -> dict[str, Any]:
    return {"id": m.id, "role": m.role, "content": m.content, "model": m.model, "status": m.status,
            "context_files": m.context_files, "meta": m.meta, "created_at": m.created_at}


class ConvCreate(BaseModel):
    title: str | None = None
    mode: Literal["assistant", "coding"] = "assistant"
    workspace_id: int | None = None


class ConvPatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    mode: Literal["assistant", "coding"] | None = None
    workspace_id: int | None = None
    clear_workspace: bool = False


class MessageIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)
    model: str | None = None
    files: list[str] = Field(default_factory=list, max_length=50)
    auto_context: bool = True
    agent: bool = True


@router.get("/conversations")
def list_conversations(s: Session = Depends(get_session), q: str | None = None, workspace_id: int | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
    query = s.query(Conversation)
    if workspace_id is not None:
        query = query.filter(Conversation.workspace_id == workspace_id)
    if q:
        like = f"%{q}%"
        sub = s.query(Message.conversation_id).filter(Message.content.ilike(like))
        query = query.filter(or_(Conversation.title.ilike(like), Conversation.id.in_(sub)))
    rows = query.order_by(Conversation.updated_at.desc()).limit(min(limit, 1000)).all()
    return [conv_dict(c) for c in rows]


@router.post("/conversations")
def create_conversation(body: ConvCreate, s: Session = Depends(get_session)) -> dict[str, Any]:
    if body.workspace_id is not None and not s.get(Workspace, body.workspace_id):
        raise HTTPException(404, "Workspace not found")
    c = Conversation(title=body.title or "New conversation", mode=body.mode, workspace_id=body.workspace_id)
    s.add(c)
    s.flush()
    return conv_dict(c, True)


def _get(s: Session, cid: int) -> Conversation:
    c = s.get(Conversation, cid)
    if not c:
        raise HTTPException(404, "Conversation not found")
    return c


@router.get("/conversations/{cid}")
def get_conversation(cid: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    return conv_dict(_get(s, cid), True)


@router.patch("/conversations/{cid}")
def patch_conversation(cid: int, body: ConvPatch, s: Session = Depends(get_session)) -> dict[str, Any]:
    c = _get(s, cid)
    if body.title is not None:
        c.title = body.title.strip() or c.title
    if body.mode is not None:
        c.mode = body.mode
    if body.clear_workspace:
        c.workspace_id = None
    elif body.workspace_id is not None:
        if not s.get(Workspace, body.workspace_id):
            raise HTTPException(404, "Workspace not found")
        c.workspace_id = body.workspace_id
    c.updated_at = utcnow()
    return conv_dict(c)


@router.delete("/conversations/{cid}")
def delete_conversation(cid: int, s: Session = Depends(get_session)) -> dict[str, bool]:
    s.delete(_get(s, cid))
    audit(s, "user", "conversation.deleted", f"conversation:{cid}")
    return {"deleted": True}


@router.delete("/conversations")
def clear_conversations(confirm: bool = Query(False), workspace_id: int | None = None,
                        s: Session = Depends(get_session)) -> dict[str, int]:
    if not confirm:
        raise HTTPException(400, "Pass confirm=true to delete conversation history")
    q = s.query(Conversation)
    if workspace_id is not None:
        q = q.filter(Conversation.workspace_id == workspace_id)
    n = 0
    for c in q.all():
        s.delete(c)
        n += 1
    audit(s, "user", "memory.cleared", f"workspace:{workspace_id}" if workspace_id else "all", count=n)
    return {"deleted": n}


@router.get("/conversations/{cid}/export")
def export_conversation(cid: int, format: Literal["md", "json"] = "md", s: Session = Depends(get_session)) -> Response:
    c = _get(s, cid)
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in c.title)[:60] or "conversation"
    if format == "json":
        body = json.dumps(conv_dict(c, True), indent=2, default=str)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.json"'})
    lines = [f"# {c.title}", "", f"_Mode: {c.mode} · exported from NEXUS_", ""]
    for msg in c.messages:
        who = "You" if msg.role == "user" else "NEXUS"
        lines += [f"## {who} — {msg.created_at:%Y-%m-%d %H:%M}", "", msg.content, ""]
        if msg.context_files:
            lines += ["_Context files: " + ", ".join(f["path"] for f in msg.context_files) + "_", ""]
    return Response("\n".join(lines), media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})


@router.post("/conversations/{cid}/messages")
async def send_message(cid: int, body: MessageIn, state: NexusState = Depends(get_state)) -> StreamingResponse:
    with state.db.session() as s:
        _get(s, cid)
    if state.paused:
        raise HTTPException(409, "NEXUS is paused. Resume it from the tray or the header.")
    req = TurnRequest(conversation_id=cid, content=body.content, model=body.model, files=body.files,
                      auto_context=body.auto_context, agent=body.agent)
    return StreamingResponse(sse_stream(state.conversations.respond(req)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/generations/{gen_id}/stop")
def stop_generation(gen_id: str, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    return {"stopped": state.llm.stop(gen_id)}


@router.post("/generations/stop-all")
def stop_all(state: NexusState = Depends(get_state)) -> dict[str, int]:
    return {"stopped": state.llm.stop_all()}
