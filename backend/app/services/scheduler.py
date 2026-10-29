"""Background loop: fires due reminders/timers, detects sleep/resume, and applies retention."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import Conversation, Reminder, utcnow
from app.services.preferences import load_preferences
from app.services.when import next_occurrence

log = logging.getLogger(__name__)


NAG_SECONDS = 60  # a reminder nobody has answered rings again this often...
MAX_RINGS = 6  # ...up to this many times in total


def alert_event(r: Reminder, *, late: bool = False, ring: int = 1) -> dict[str, object]:
    return {"id": r.id, "kind": r.kind, "text": r.text, "late": late, "ring": ring, "persistent": r.persistent,
            "repeat": r.repeat or ("daily" if r.repeat_daily else ""), "action": r.action}


def fire_due(db: Database, bus: EventBus, now: dt.datetime | None = None) -> list[int]:
    now = now or dt.datetime.now()
    fired: list[int] = []
    with db.session() as s:
        due = s.query(Reminder).filter(Reminder.status == "pending", Reminder.due_at <= now).all()
        for r in due:
            late_s = (now - r.due_at).total_seconds()
            r.status = "fired"
            r.fired_at = utcnow()
            r.ring_count = 1
            fired.append(r.id)
            bus.publish("reminder", **alert_event(r, late=late_s > 60, ring=1))
            repeat = r.repeat or ("daily" if r.repeat_daily else "")
            nxt = next_occurrence(r.due_at, repeat, now)
            if nxt:
                s.add(Reminder(kind=r.kind, text=r.text, due_at=nxt, repeat=repeat, repeat_daily=repeat == "daily",
                               persistent=r.persistent, action=r.action))
    return fired


def ring_again(db: Database, bus: EventBus, now_utc: dt.datetime | None = None, nag_s: float = NAG_SECONDS,
               max_rings: int = MAX_RINGS) -> list[int]:
    """Reminders that rang and were not dismissed or snoozed ring again, so a missed one is not just a toast
    you looked away from. Stops after `max_rings`; the reminder then stays in the list as 'fired'."""
    now_utc = now_utc or utcnow()
    rung: list[int] = []
    with db.session() as s:
        rows = s.query(Reminder).filter(Reminder.status == "fired", Reminder.persistent.is_(True),
                                        Reminder.ring_count < max_rings).all()
        for r in rows:
            if r.fired_at is not None and (now_utc - r.fired_at).total_seconds() >= nag_s:
                r.ring_count += 1
                r.fired_at = now_utc
                rung.append(r.id)
                bus.publish("reminder", **alert_event(r, ring=r.ring_count))
    return rung


def snooze(db: Database, rid: int, minutes: float, now: dt.datetime | None = None) -> Reminder | None:
    """Ring again in `minutes`. The reminder goes back to pending, so it appears in lists with its new time."""
    now = now or dt.datetime.now()
    with db.session() as s:
        r = s.get(Reminder, rid)
        if r is None or r.status not in ("fired", "pending"):
            return None
        r.status = "pending"
        r.due_at = now + dt.timedelta(minutes=max(0.5, min(minutes, 24 * 60)))
        r.ring_count = 0
        s.flush()
        return r


def apply_retention(db: Database) -> int:
    with db.session() as s:
        days = load_preferences(s).retention_days
        if days <= 0:
            return 0
        cutoff = utcnow() - dt.timedelta(days=days)
        old = s.query(Conversation).filter(Conversation.updated_at < cutoff).all()
        for c in old:
            s.delete(c)
        return len(old)


class Scheduler:
    def __init__(self, db: Database, bus: EventBus, on_resume: Callable[[], Awaitable[None]] | None = None,
                 tick_s: float = 1.0) -> None:
        self.db, self.bus, self.on_resume, self.tick_s = db, bus, on_resume, tick_s
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        last_wall = time.time()
        last_retention = 0.0
        while True:
            await asyncio.sleep(self.tick_s)
            now_wall = time.time()
            # A large wall-clock jump means the machine slept (or the clock changed).
            if now_wall - last_wall > max(30.0, self.tick_s * 20):
                log.info("Detected resume after %.0fs", now_wall - last_wall)
                self.bus.publish("system", event="resumed", gap_s=now_wall - last_wall)
                if self.on_resume:
                    try:
                        await self.on_resume()
                    except Exception:
                        log.exception("resume handler failed")
            last_wall = now_wall
            try:
                await asyncio.to_thread(fire_due, self.db, self.bus)
                await asyncio.to_thread(ring_again, self.db, self.bus)
                if now_wall - last_retention > 3600:
                    last_retention = now_wall
                    n = await asyncio.to_thread(apply_retention, self.db)
                    if n:
                        log.info("Retention removed %s old conversations", n)
            except Exception:
                log.exception("scheduler tick failed")
