from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.database.models import AssistantAction, Note, Reminder, Todo, utcnow
from app.services.actions import agenda_text
from app.services.preferences import load_preferences

router = APIRouter(prefix="/api")


# ---------------- notes
class NoteIn(BaseModel):
    title: str = Field("", max_length=300)
    body: str = Field("", max_length=200_000)
    pinned: bool = False


def note_dict(n: Note) -> dict[str, Any]:
    return {"id": n.id, "title": n.title, "body": n.body, "pinned": n.pinned, "created_at": n.created_at,
            "updated_at": n.updated_at}


@router.get("/notes")
def list_notes(q: str | None = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    query = s.query(Note)
    if q:
        query = query.filter(Note.title.ilike(f"%{q}%") | Note.body.ilike(f"%{q}%"))
    return [note_dict(n) for n in query.order_by(Note.pinned.desc(), Note.updated_at.desc()).all()]


@router.post("/notes")
def create_note(body: NoteIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    n = Note(title=body.title or body.body[:60], body=body.body, pinned=body.pinned)
    s.add(n)
    s.flush()
    return note_dict(n)


@router.put("/notes/{nid}")
def update_note(nid: int, body: NoteIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    n = s.get(Note, nid)
    if not n:
        raise HTTPException(404, "Note not found")
    n.title, n.body, n.pinned, n.updated_at = body.title, body.body, body.pinned, utcnow()
    return note_dict(n)


@router.delete("/notes/{nid}")
def delete_note(nid: int, s: Session = Depends(get_session)) -> dict[str, bool]:
    n = s.get(Note, nid)
    if not n:
        raise HTTPException(404, "Note not found")
    s.delete(n)
    return {"deleted": True}


# ---------------- todos
class TodoIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    due_date: dt.date | None = None


class TodoPatch(BaseModel):
    text: str | None = None
    done: bool | None = None
    due_date: dt.date | None = None


def todo_dict(t: Todo) -> dict[str, Any]:
    return {"id": t.id, "text": t.text, "done": t.done, "due_date": t.due_date, "created_at": t.created_at}


@router.get("/todos")
def list_todos(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [todo_dict(t) for t in s.query(Todo).order_by(Todo.done, Todo.id.desc()).all()]


@router.post("/todos")
def create_todo(body: TodoIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    t = Todo(text=body.text, due_date=body.due_date)
    s.add(t)
    s.flush()
    return todo_dict(t)


@router.patch("/todos/{tid}")
def patch_todo(tid: int, body: TodoPatch, s: Session = Depends(get_session)) -> dict[str, Any]:
    t = s.get(Todo, tid)
    if not t:
        raise HTTPException(404, "To-do not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    return todo_dict(t)


@router.delete("/todos/{tid}")
def delete_todo(tid: int, s: Session = Depends(get_session)) -> dict[str, bool]:
    t = s.get(Todo, tid)
    if not t:
        raise HTTPException(404, "To-do not found")
    s.delete(t)
    return {"deleted": True}


# ---------------- reminders / timers
class ReminderIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    kind: Literal["reminder", "timer", "alarm"] = "reminder"
    due_at: dt.datetime | None = None  # local time
    in_seconds: int | None = Field(None, ge=1, le=60 * 60 * 24 * 365)
    repeat_daily: bool = False
    repeat: Literal["", "daily", "weekdays", "weekly", "monthly"] = ""
    persistent: bool = True  # keep ringing (every minute, up to 6 times) until dismissed or snoozed
    open_app: str | None = Field(None, max_length=80)  # an integrated app to open when it rings


def rem_dict(r: Reminder) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "text": r.text, "due_at": r.due_at,
            "repeat": r.repeat or ("daily" if r.repeat_daily else ""), "repeat_daily": r.repeat_daily,
            "persistent": r.persistent, "ring_count": r.ring_count, "action": r.action, "status": r.status,
            "fired_at": r.fired_at, "created_at": r.created_at}


@router.get("/reminders")
def list_reminders(include_done: bool = False, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = s.query(Reminder)
    if not include_done:
        q = q.filter(Reminder.status.in_(["pending", "fired"]))
    return [rem_dict(r) for r in q.order_by(Reminder.due_at).all()]


def _action_for(s: Session, app: str | None) -> dict[str, Any] | None:
    if not app:
        return None
    from app.services import appcontrol

    if not appcontrol.find(load_preferences(s), app):
        raise HTTPException(422, f"'{app}' is not one of your integrated apps. Add it on the Apps page first.")
    return {"type": "open_app", "name": app.strip().lower()}


@router.post("/reminders")
def create_reminder(body: ReminderIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    if body.in_seconds:
        due = dt.datetime.now() + dt.timedelta(seconds=body.in_seconds)
    elif body.due_at:
        due = body.due_at.replace(tzinfo=None) if body.due_at.tzinfo is None else \
            body.due_at.astimezone().replace(tzinfo=None)
    else:
        raise HTTPException(422, "Provide due_at or in_seconds")
    repeat = body.repeat or ("daily" if body.repeat_daily else "")
    r = Reminder(text=body.text, kind=body.kind, due_at=due, repeat=repeat, repeat_daily=repeat == "daily",
                 persistent=body.persistent, action=_action_for(s, body.open_app))
    s.add(r)
    s.flush()
    return rem_dict(r)


class NaturalIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=500)
    open_app: str | None = Field(None, max_length=80)


@router.post("/reminders/natural")
def create_reminder_natural(body: NaturalIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """"call mum tomorrow at 5pm", "stand up every weekday at 9", "pay rent on friday": one sentence in, reminder out."""
    from app.services.when import parse_when

    now = dt.datetime.now()
    w = parse_when(body.text, now)
    if not w:
        raise HTTPException(422, "I couldn't find a time in that. Try 'in 20 minutes', 'at 5pm', "
                                 "'tomorrow morning' or 'every weekday at 9'.")
    r = Reminder(text=w.rest or body.text.strip(), kind="reminder", due_at=w.due, repeat=w.repeat,
                 repeat_daily=w.repeat == "daily", action=_action_for(s, body.open_app))
    s.add(r)
    s.flush()
    return rem_dict(r)


class SnoozeIn(BaseModel):
    minutes: float = Field(10, ge=0.5, le=24 * 60)


@router.post("/reminders/{rid}/snooze")
def snooze_reminder(rid: int, body: SnoozeIn | None = None, s: Session = Depends(get_session)) -> dict[str, Any]:
    r = s.get(Reminder, rid)
    if not r or r.status not in ("fired", "pending"):
        raise HTTPException(404, "Reminder not found")
    r.status, r.ring_count = "pending", 0
    r.due_at = dt.datetime.now() + dt.timedelta(minutes=(body or SnoozeIn()).minutes)
    return rem_dict(r)


@router.post("/reminders/{rid}/dismiss")
def dismiss(rid: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    r = s.get(Reminder, rid)
    if not r:
        raise HTTPException(404, "Reminder not found")
    r.status = "dismissed" if r.status == "fired" else "cancelled"
    return rem_dict(r)


@router.get("/agenda")
def agenda(s: Session = Depends(get_session)) -> dict[str, Any]:
    now = dt.datetime.now()
    return {"text": agenda_text(s, now), "generated_at": now}


@router.get("/actions")
def list_actions(s: Session = Depends(get_session), limit: int = 100) -> list[dict[str, Any]]:
    rows = s.query(AssistantAction).order_by(AssistantAction.id.desc()).limit(min(limit, 500)).all()
    return [{"id": a.id, "kind": a.kind, "summary": a.summary, "status": a.status, "result": a.result,
             "source": a.source, "created_at": a.created_at} for a in rows]


PEEK_KINDS = {"math", "convert", "time", "date", "system"}


@router.get("/assistant/peek")
def peek(q: str = "", s: Session = Depends(get_session)) -> dict[str, Any]:
    """Instant answer for the command palette (calculator, converter, time...). Read-only; never calls the model."""
    from app.services import quick

    low = q.strip().rstrip(".!?").lower()
    hit = quick.recognise(low, q) if 0 < len(low) <= 200 else None
    if not hit or hit[0] not in PEEK_KINDS:
        return {"answer": None}
    status, msg = quick.run(s, hit[0], hit[1], dt.datetime.now())
    return {"answer": msg if status == "succeeded" else None, "kind": hit[0]}
