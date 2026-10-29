"""What happens when a reminder rings, beyond the pop-up in the browser and the tray notification:
a sound, the reminder spoken aloud (also repeated when ignored), and an optional "then open this app" action."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.core.events import EventBus
from app.database.db import Database
from app.services import appcontrol, appintents
from app.services.actions import Launcher
from app.services.preferences import load_preferences

log = logging.getLogger(__name__)


def spoken_text(ev: dict[str, Any]) -> str:
    text = str(ev.get("text") or "").strip()
    again = int(ev.get("ring") or 1) > 1
    if ev.get("kind") == "timer":
        return ("Your timer is still going off. " if again else "Your timer is done. ") + text
    if ev.get("kind") == "alarm":
        return ("Alarm, still ringing. " if again else "Alarm. ") + (text if text.lower() != "alarm" else "")
    return ("Reminder, again: " if again else "Reminder: ") + text


class AlertHandler:
    """Bus listener: call it with every event; it reacts to `reminder` events only. Runs on the scheduler thread, so
    anything slow is pushed to its own thread."""

    def __init__(self, db: Database, bus: EventBus, voice: Any, launcher: Launcher,
                 play: Callable[[], None] | None = None) -> None:
        self.db, self.bus, self.voice, self.launcher, self.play = db, bus, voice, launcher, play

    def __call__(self, ev: dict[str, Any]) -> None:
        if ev.get("type") != "reminder":
            return
        threading.Thread(target=self._handle, args=(ev,), name="nexus-alert", daemon=True).start()

    def _handle(self, ev: dict[str, Any]) -> None:
        try:
            with self.db.session() as s:
                prefs = load_preferences(s)
            if self.play:
                try:
                    self.play()
                except Exception:  # noqa: S110 - a missing sound must not stop the spoken alert
                    pass
            if prefs.reminders_speak:
                self.voice.announce(spoken_text(ev))
            action = ev.get("action")
            if action and int(ev.get("ring") or 1) == 1 and action.get("type") == "open_app":
                ref = appcontrol.find(prefs, str(action.get("name", "")))
                if ref is None:
                    self.bus.publish("reminder_action", id=ev.get("id"), ok=False,
                                     message=f"{action.get('name')} is no longer one of your integrated apps.")
                else:
                    appintents.launch_app(ref, self.launcher)
                    self.bus.publish("reminder_action", id=ev.get("id"), ok=True, message=f"Opened {ref.name}.")
        except Exception as e:
            log.exception("reminder alert failed")
            self.bus.publish("reminder_action", id=ev.get("id"), ok=False, message=f"The reminder's action failed: {e}")
