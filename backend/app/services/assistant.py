"""The step before the model answers: confirmations (“send it”), skills (WhatsApp, Gmail, calendar...) and the
“I'm not sure I heard that right” gate. Returns a finished reply, an intent for the caller to run, or nothing."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import Contact
from app.services import actions, router, skills
from app.services.contacts import ContactRef
from app.services.preferences import Preferences
from app.windows.sysctl import SystemControl

log = logging.getLogger(__name__)

# Intents that are harmless even if misheard: everything else is read back first when speech recognition was unsure.
SAFE_KINDS = {"timer", "reminder", "note", "todo", "agenda", "snooze", "dismiss_alarm", "clarify", "open_app"}


@dataclass
class Resolution:
    message: str = ""
    kind: str = "skill"
    status: str = "succeeded"
    intent: actions.Intent | None = None  # run this one (the caller owns the database session it needs)
    pending: bool = False  # waiting for a yes/no


class Assistant:
    def __init__(self, db: Database, launcher: actions.Launcher, bus: EventBus | None = None,
                 system: SystemControl | None = None) -> None:
        self.db, self.bus = db, bus
        self.system = system or actions.DEFAULT_SYSTEM
        self.runner = skills.Runner(launcher, self.system)
        self.store = skills.PendingStore()

    # ---- pending state shown in the UI
    def snapshot(self) -> dict[str, Any] | None:
        p = self.store.get()
        if p is None:
            return None
        return {"kind": p.kind, "title": p.title, "preview": p.preview, "question": p.question,
                "expires_in": max(0, int(p.expires - self.store.clock()))}

    def _publish(self) -> None:
        if self.bus is not None:
            self.bus.publish("pending", pending=self.snapshot())

    def contacts(self) -> list[ContactRef]:
        with self.db.session() as s:
            rows = s.scalars(select(Contact).order_by(Contact.name)).all()
            return [ContactRef(r.id, r.name, list(r.aliases or []), r.phone or "", r.email or "") for r in rows]

    # ---- the pre-step
    async def resolve(self, text: str, prefs: Preferences, source: str, confidence: float | None,
                      drafter: Any = None) -> Resolution | None:
        drafter = drafter or self.drafter
        pend = self.store.get()
        if pend is not None:
            v = skills.verdict(text)
            if v == "no":
                self.store.clear()
                self._publish()
                if pend.kind == "run" and pend.run_id is not None and self.agent is not None:
                    return await self.agent.decide(pend.run_id, False)
                return Resolution("Okay, cancelled. Nothing was sent.", "confirm", "succeeded")
            if v == "yes":
                self.store.clear()
                self._publish()
                return await self._confirmed(pend, prefs)
            self.store.clear()  # a new command replaces the question
            self._publish()
        ctx = skills.SkillContext(prefs, await asyncio.to_thread(self.contacts), dt.datetime.now(),
                                  confident=not self._unsure(prefs, source, confidence))
        found = skills.match(text, ctx)
        if isinstance(found, skills.Clarify):
            return Resolution(found.message, "skill", "unsupported")
        if isinstance(found, skills.Plan):
            return await self._start(found, ctx, text, drafter)
        return None

    async def route(self, text: str, prefs: Preferences, source: str, confidence: float | None, complete: router.CompleteFn,
                    ) -> Resolution | None:
        """Free-form commands: let the model choose a skill (only reached when no fixed phrase matched)."""
        ctx = skills.SkillContext(prefs, await asyncio.to_thread(self.contacts), dt.datetime.now(),
                                  confident=not self._unsure(prefs, source, confidence))
        found = await router.route(text, ctx, complete)
        if isinstance(found, skills.Clarify):
            return Resolution(found.message, "skill", "unsupported")
        if isinstance(found, skills.Plan):
            return await self._start(found, ctx, text, self.drafter)
        return None

    def _unsure(self, prefs: Preferences, source: str, confidence: float | None) -> bool:
        return (source == "voice" and prefs.voice.confirm_when_unsure and confidence is not None
                and confidence < prefs.voice.unsure_threshold)

    def gate_intent(self, intent: actions.Intent, text: str, prefs: Preferences, source: str,
                    confidence: float | None) -> Resolution | None:
        """Hold back a command that was heard with low confidence and could do something real."""
        if intent.kind in SAFE_KINDS or not self._unsure(prefs, source, confidence):
            return None
        q = f"I heard “{text}”. Should I do that?"
        self.store.set(skills.Pending("ask", intent.summary or intent.kind, text, intent=intent, question=q))
        self._publish()
        return Resolution(q + " Say yes or no.", "confirm", "succeeded", pending=True)

    async def _start(self, plan: skills.Plan, ctx: skills.SkillContext, text: str, drafter: Any) -> Resolution:
        if plan.ask or (plan.risky and not ctx.confident):
            q = plan.ask or f"I heard “{text}”. Should I do that?"
            self.store.set(skills.Pending("ask", plan.summary, text, plan=plan, question=q))
            self._publish()
            return Resolution(q + " Say yes or no.", "confirm", "succeeded", pending=True)
        return await self._run(plan, ctx.prefs, ctx, drafter)

    async def _run(self, plan: skills.Plan, prefs: Preferences, ctx: skills.SkillContext | None, drafter: Any) -> Resolution:
        if plan.draft is not None and drafter is not None and ctx is not None:
            _name, _to, instruction = plan.draft
            try:
                subject, body = await drafter(instruction, plan.draft[0])
                plan = skills.apply_draft(plan, ctx, subject, body)
            except Exception as e:  # model offline: still open an empty draft rather than failing the request
                log.info("email drafting failed: %s", e)
                plan.reply += " (I couldn't write the text, so the draft is empty.)"
        status, msg = await asyncio.to_thread(skills.execute_plan, plan, self.runner, prefs, self.store)
        self._publish()
        return Resolution(msg, "skill", status, pending=self.store.get() is not None)

    async def _confirmed(self, pend: skills.Pending, prefs: Preferences) -> Resolution:
        if pend.kind == "run" and pend.run_id is not None and self.agent is not None:
            return await self.agent.decide(pend.run_id, True)
        if pend.kind == "send" and pend.send is not None:
            ok, msg = await asyncio.to_thread(self.runner.press_send, pend.send, prefs)
            return Resolution(msg, "skill", "succeeded" if ok else "failed")
        if pend.intent is not None:
            return Resolution("", "confirm", "succeeded", intent=pend.intent)
        if pend.plan is not None:
            plan = pend.plan
            plan.ask = None
            ctx = skills.SkillContext(prefs, await asyncio.to_thread(self.contacts), dt.datetime.now())
            return await self._run(plan, prefs, ctx, None if plan.draft is None else self.drafter)
        return Resolution("Nothing to confirm.", "confirm", "unsupported")

    drafter: Any = None  # set by the conversation service (needs the model)
    agent: Any = None  # the conversation service: decides automation runs (approve / decline)

    # ---- buttons in the UI
    async def confirm(self, prefs: Preferences, yes: bool) -> Resolution:
        pend = self.store.get()
        self.store.clear()
        self._publish()
        if pend is None:
            return Resolution("Nothing is waiting for confirmation.", "confirm", "unsupported")
        if not yes:
            if pend.kind == "run" and pend.run_id is not None and self.agent is not None:
                return await self.agent.decide(pend.run_id, False)
            return Resolution("Cancelled.", "confirm", "succeeded")
        return await self._confirmed(pend, prefs)
