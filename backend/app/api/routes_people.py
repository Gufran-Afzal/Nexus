"""People (who NEXUS may message), skills (what it can do inside other apps) and the pending-confirmation card."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_state
from app.core.state import NexusState
from app.database.models import Contact
from app.services import actions, skills
from app.services.audit import audit
from app.services.contacts import EMAIL_RX, normalize_phone
from app.services.preferences import CustomSkill, load_preferences, save_preferences

router = APIRouter(prefix="/api")


class ContactIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    phone: str = Field("", max_length=40)
    email: str = Field("", max_length=200)
    notes: str = Field("", max_length=1000)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not v.strip():
            return ""
        digits = normalize_phone(v)
        if digits is None:
            raise ValueError("Enter the phone number with its country code, e.g. +44 7700 900123.")
        return digits

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if v and not EMAIL_RX.match(v):
            raise ValueError("That doesn't look like an email address.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Give the person a name.")
        return v


def _dict(c: Contact) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "aliases": list(c.aliases or []), "phone": c.phone, "email": c.email, "notes": c.notes}


def _clean_aliases(a: list[str]) -> list[str]:
    return [x for x in dict.fromkeys(" ".join(i.split()) for i in a) if x][:10]


@router.get("/contacts")
def list_contacts(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_dict(c) for c in s.scalars(select(Contact).order_by(Contact.name))]


@router.post("/contacts", status_code=201)
def add_contact(body: ContactIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    if not body.phone and not body.email:
        raise HTTPException(422, "Add a phone number (for WhatsApp) or an email address.")
    if s.scalar(select(Contact).where(Contact.name == body.name)):
        raise HTTPException(409, f"{body.name} is already in your list.")
    c = Contact(name=body.name, aliases=_clean_aliases(body.aliases), phone=body.phone, email=body.email, notes=body.notes)
    s.add(c)
    s.flush()
    audit(s, "user", "contacts.add", c.name)
    return _dict(c)


@router.put("/contacts/{cid}")
def update_contact(cid: int, body: ContactIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    c = s.get(Contact, cid)
    if not c:
        raise HTTPException(404, "No such person.")
    if not body.phone and not body.email:
        raise HTTPException(422, "Keep a phone number (for WhatsApp) or an email address.")
    other = s.scalar(select(Contact).where(Contact.name == body.name, Contact.id != cid))
    if other:
        raise HTTPException(409, f"{body.name} is already in your list.")
    c.name, c.aliases, c.phone, c.email, c.notes = body.name, _clean_aliases(body.aliases), body.phone, body.email, body.notes
    audit(s, "user", "contacts.update", c.name)
    return _dict(c)


@router.delete("/contacts/{cid}")
def delete_contact(cid: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    c = s.get(Contact, cid)
    if not c:
        raise HTTPException(404, "No such person.")
    audit(s, "user", "contacts.delete", c.name)
    s.delete(c)
    return {"ok": True}


# ------------------------------------------------------------------ skills
def _skills_payload(state: NexusState, s: Session) -> dict[str, Any]:
    prefs = load_preferences(s)
    return {
        "skills": [{"id": sid, "title": title, "examples": ex, "enabled": sid not in prefs.disabled_skills}
                   for sid, title, ex in skills.SKILLS],
        "custom": [c.model_dump() for c in prefs.custom_skills],
        "default_message_app": prefs.default_message_app, "email_app": prefs.email_app, "smart_routing": prefs.smart_routing,
        "send_key_delay_s": prefs.send_key_delay_s, "pending": state.conversations.assistant.snapshot(),
    }


@router.get("/skills")
def get_skills(state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    return _skills_payload(state, s)


class ToggleIn(BaseModel):
    enabled: bool


@router.put("/skills/{skill_id}")
def toggle_skill(skill_id: str, body: ToggleIn, state: NexusState = Depends(get_state),
                 s: Session = Depends(get_session)) -> dict[str, Any]:
    if skill_id not in {x[0] for x in skills.SKILLS}:
        raise HTTPException(404, "No such skill.")
    off = set(load_preferences(s).disabled_skills)
    (off.discard if body.enabled else off.add)(skill_id)
    save_preferences(s, {"disabled_skills": sorted(off)})
    return _skills_payload(state, s)


@router.post("/skills/custom", status_code=201)
def add_custom(body: CustomSkill, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    prefs = load_preferences(s)
    rest = [c for c in prefs.custom_skills if c.name.lower() != body.name.lower()]
    save_preferences(s, {"custom_skills": [c.model_dump() for c in [*rest, body]]})
    audit(s, "user", "skills.custom.add", body.name)
    return _skills_payload(state, s)


@router.delete("/skills/custom/{name}")
def delete_custom(name: str, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    prefs = load_preferences(s)
    save_preferences(s, {"custom_skills": [c.model_dump() for c in prefs.custom_skills if c.name.lower() != name.lower()]})
    return _skills_payload(state, s)


# ------------------------------------------------------------------ pending confirmation
@router.get("/pending")
def get_pending(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return {"pending": state.conversations.assistant.snapshot()}


async def _answer(state: NexusState, yes: bool) -> dict[str, Any]:
    conv = state.conversations
    with state.db.session() as s:
        prefs = load_preferences(s)
    res = await conv.assistant.confirm(prefs, yes)
    message = res.message
    if res.intent is not None:
        with state.db.session() as s:
            out = actions.execute(s, res.intent, prefs, source="text", launcher=conv.launcher)
            message = out.message
    return {"message": message, "status": res.status, "pending": conv.assistant.snapshot()}


@router.post("/pending/confirm")
async def confirm_pending(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return await _answer(state, True)


@router.post("/pending/cancel")
async def cancel_pending(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return await _answer(state, False)
