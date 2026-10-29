"""Deterministic recognition and execution of personal-assistant actions.

Timers, reminders, notes and launches are handled by real application code, not by the language
model, so their results are always true. Anything not recognised here goes to the model as chat.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.database.models import AssistantAction, Note, Reminder, Todo
from app.services import appcontrol, appintents, quick
from app.services.audit import audit
from app.services.preferences import Preferences
from app.services.when import describe, parse_when
from app.windows.sysctl import SystemControl

UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
         "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
WORD_NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "forty-five": 45, "sixty": 60, "half": 0.5}

DUR = (
    r"\b(?P<n>\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty"
    r"|forty-five|sixty|half)\s*(?:an\s+)?(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh])\b"
)


@dataclass
class Intent:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


def _num(tok: str) -> float:
    return WORD_NUMS[tok] if tok in WORD_NUMS else float(tok)


def parse_duration(text: str) -> int | None:
    total = 0.0
    found = False
    for m in re.finditer(DUR, text, re.I):
        total += _num(m.group("n").lower()) * UNITS[m.group("u").lower()]
        found = True
    return int(total) if found and total > 0 else None


def parse_clock(text: str, now: dt.datetime) -> dt.datetime | None:
    m = re.search(r"\b(?:at\s+)(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?", text, re.I)
    if not m:
        m = re.search(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>am|pm)?\b", text, re.I)
    if not m:
        return None
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    day = now.date()
    if re.search(r"\btomorrow\b", text, re.I):
        day = day + dt.timedelta(days=1)
    target = dt.datetime.combine(day, dt.time(h, mi))
    if target <= now and not re.search(r"\btomorrow\b", text, re.I):
        target += dt.timedelta(days=1)
    return target


def recognise(text: str, now: dt.datetime | None = None, known_apps: set[str] | None = None) -> Intent | None:
    """known_apps: approved app names. "run X"/"start X" only means launching when X is one of them,
    so requests like "run the tests" reach the model instead."""
    now = now or dt.datetime.now()
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+nexus[,.]?\s+|(?:please|can you|could you|would you|will you)\s+)+", "",
               text.strip().rstrip(".!?"), flags=re.I)
    low = t.lower()

    if re.match(r"^(?:snooze|remind me again)(?:\s+(?:it|that|the alarm|the reminder))?(?:\s+(?:for|in)\s+.+)?$", low):
        snooze_s = parse_duration(low) or 600
        return Intent("snooze", {"seconds": snooze_s}, f"Snooze for {fmt_duration(snooze_s)}")
    if re.match(r"^(?:dismiss|stop|silence|turn off|cancel|clear)\s+(?:the\s+|this\s+|that\s+)?(?:alarm|alarms|reminder|timer|ringing|alert)$|"  # noqa: E501
                r"^(?:i'?m|i am)\s+(?:up|awake)$|^dismiss$", low):
        return Intent("dismiss_alarm", {}, "Dismiss the ringing reminder")

    m = re.match(r"^(?:set\s+(?:an?\s+)?alarm|wake me(?:\s+up)?|alarm)\s*(?P<body>.*)$", low)
    if m:
        w = parse_when(m.group("body") or "", now)
        if w:
            label = w.rest.strip() or "Alarm"
            return Intent("reminder", {"due_at": w.due.isoformat(timespec="seconds"), "text": label, "kind": "alarm",
                                       "repeat": w.repeat}, f"Alarm {describe(w.due, w.repeat, now)}")
        return Intent("clarify", {}, "What time should I set the alarm for? For example: 'set an alarm for 7am'.")

    if re.match(r"^(?:set|start|create)\s+(?:a\s+)?(?:\w+\s+)*?timer\b|^timer\b", low) or re.search(r"\btimer for\b", low):
        secs = parse_duration(low)
        if secs:
            lab = re.search(r"\btimer\s+(?:called|for the|named)\s+([a-z ]{2,30}?)(?:\s+for\b|$)", low) or re.search(
                r"\b(?:set|start)\s+(?:a\s+)?([a-z]{3,20})\s+timer\b", low)
            label = (lab.group(1).strip() if lab and lab.group(1) not in ("a", "an", "the", "new") else "") or "Timer"
            return Intent("timer", {"seconds": secs, "text": label}, f"Timer for {fmt_duration(secs)}")

    m = re.match(r"^remind me(?:\s+to)?\s+(?P<body>.+)$", t, re.I)
    if m:
        body = m.group("body")
        act = re.search(r"\s*(?:,|and|then)\s+(?:open|launch|start)\s+(?P<app>[\w .+#-]{2,40})$", body, re.I)
        action = None
        if act and known_apps and appintents.match_known(known_apps, act.group("app")):
            action = {"type": "open_app", "name": act.group("app").strip().lower()}
            body = body[: act.start()]
        w = parse_when(body, now)
        if w:
            text_ = w.rest or "Reminder"
            return Intent("reminder", {"due_at": w.due.isoformat(timespec="seconds"), "text": text_, "repeat": w.repeat,
                                       "action": action}, f"Reminder '{text_}' {describe(w.due, w.repeat, now)}")
        return Intent("clarify", {}, "I can set that reminder, but I need a time, e.g. 'in 20 minutes', 'at 5pm' or 'every weekday at 9'.")  # noqa: E501

    m = re.match(r"^(?:take|make|add|save)\s+(?:a\s+)?note[:,]?\s+(?:that\s+)?(?P<b>.+)$|^note[:]\s*(?P<b2>.+)$", t, re.I)
    if m:
        body = (m.group("b") or m.group("b2")).strip()
        return Intent("note", {"body": body}, "Save a note")

    m = re.match(r"^add\s+(?P<b>.+?)\s+to\s+(?:my\s+)?(?:to-?do|task)s?(?:\s+list)?$", t, re.I) or re.match(
        r"^(?:add\s+(?:a\s+)?(?:to-?do|task)[:]?)\s+(?P<b>.+)$", t, re.I)
    if m:
        return Intent("todo", {"text": m.group("b").strip()}, "Add a to-do")

    if re.search(r"\b(my agenda|what'?s on (?:my )?(?:schedule|agenda)?\s*today|today'?s agenda|daily (?:summary|brief))\b", low):
        return Intent("agenda", {}, "Today's agenda from local data")

    if known_apps:
        hit = appintents.recognise(t, low, known_apps)
        if hit:
            return Intent(*hit)
    elif (hit := appintents.recognise(t, low, set())):
        return Intent(*hit)

    m = re.match(r"^(?:please\s+)?(?P<verb>open|launch|start|run)\s+(?:the\s+)?(?P<target>.+)$", t, re.I)
    if m:
        target = m.group("target").strip().strip('"')
        tl = target.lower()
        app = re.sub(r"\s+(app|application|program)$", "", tl)
        known = appintents.match_known(known_apps, app) if known_apps else None
        if known and not re.match(r"^https?://", tl):
            return Intent("open_app", {"name": known}, f"Open {known}")
        if m.group("verb").lower() in ("run", "start"):
            m = None
    if m:
        if re.match(r"^https?://\S+$", tl) or re.match(
            r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|edu|gov|co|ai|app|uk)(?:/\S*)?$", tl
        ):
            url = target if tl.startswith("http") else "https://" + target
            return Intent("open_url", {"url": url}, f"Open {url}")
        fm = re.match(r"^(?:my\s+)?(?P<name>.+?)\s+folder$|^folder\s+(?P<name2>.+)$", tl)
        if fm:
            return Intent("open_folder", {"name": (fm.group("name") or fm.group("name2")).strip()}, f"Open folder {target}")
        if len(app.split()) <= 2 and "." not in app:
            return Intent("open_app", {"name": app}, f"Open {app}")
    q = quick.recognise(low, t)
    if q:
        return Intent(q[0], q[1], q[2])
    return None


def fmt_duration(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s else ""]
    return " ".join(p for p in parts if p)


Launcher = Callable[[list[str] | str, str], None]


def default_launcher(target: list[str] | str, kind: str) -> None:
    if kind == "url":
        webbrowser.open(str(target), new=2)
        return
    if kind in ("uwp", "shortcut", "uri"):
        if sys.platform == "win32":
            if kind == "uwp":
                subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + str(target)], close_fds=True)  # noqa: S603, S607
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        else:
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                raise RuntimeError("Opening this needs Windows")
            subprocess.Popen([opener, str(target)])
        return
    if kind == "folder":
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        else:
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                raise RuntimeError("No folder opener available")
            subprocess.Popen([opener, str(target)])
        return
    argv = target if isinstance(target, list) else [target]
    exe = shutil.which(argv[0])
    if exe is None:
        raise FileNotFoundError(f"{argv[0]} was not found")
    subprocess.Popen([exe, *argv[1:]], close_fds=True)


@dataclass
class ActionOutcome:
    action_id: int
    status: str
    message: str


DEFAULT_SYSTEM = SystemControl()


def execute(s: Session, intent: Intent, prefs: Preferences, source: str = "text",
            launcher: Launcher = default_launcher, now: dt.datetime | None = None,
            system: SystemControl | None = None) -> ActionOutcome:
    now = now or dt.datetime.now()
    system = system or DEFAULT_SYSTEM
    status, msg = "succeeded", ""
    try:
        if intent.kind == "clarify":
            status, msg = "unsupported", intent.summary
        elif intent.kind == "timer":
            due = now + dt.timedelta(seconds=intent.params["seconds"])
            label = intent.params.get("text") or "Timer"
            text_ = f"{label.capitalize()} timer ({fmt_duration(intent.params['seconds'])})" if label != "Timer" else \
                f"Timer ({fmt_duration(intent.params['seconds'])})"
            r = Reminder(kind="timer", text=text_, due_at=due)
            s.add(r)
            s.flush()
            msg = f"{'Timer' if label == 'Timer' else label.capitalize() + ' timer'} set for {fmt_duration(intent.params['seconds'])} (ends at {due:%H:%M:%S})."  # noqa: E501
        elif intent.kind == "reminder":
            due = dt.datetime.fromisoformat(intent.params["due_at"])
            repeat = intent.params.get("repeat") or ""
            kind_ = intent.params.get("kind") or "reminder"
            action = intent.params.get("action")
            note = ""
            if action and not appcontrol.find(prefs, action["name"]):
                action, note = None, f" (I couldn't start {intent.params.get('text')!r}'s app: it isn't integrated.)"
            dup = next((r for r in s.query(Reminder).filter(Reminder.status == "pending", Reminder.kind == kind_).all()
                        if r.text.strip().lower() == intent.params["text"].strip().lower()
                        and abs((r.due_at - due).total_seconds()) <= 90), None)
            if dup is not None:  # asked twice (or heard twice): one reminder, and say so
                when_ = describe(dup.due_at, dup.repeat, now)
                msg = f"You already have that reminder (\"{dup.text}\" {when_}), so I didn't add another."
            else:
                s.add(Reminder(kind=kind_, text=intent.params["text"], due_at=due, repeat=repeat, repeat_daily=repeat == "daily",
                               action=action))
                what = "Alarm" if kind_ == "alarm" else "Reminder saved"
                msg = f"{what}: \"{intent.params['text']}\" {describe(due, repeat, now)}." + (
                    f" I'll open {action['name']} when it rings." if action else "") + note
        elif intent.kind == "snooze":
            ringing = s.query(Reminder).filter(Reminder.status == "fired").all()
            if not ringing:
                status, msg = "unsupported", "Nothing is ringing right now."
            else:
                for r in ringing:
                    r.status, r.due_at, r.ring_count = "pending", now + dt.timedelta(seconds=intent.params["seconds"]), 0
                msg = f"Snoozed for {fmt_duration(intent.params['seconds'])}."
        elif intent.kind == "dismiss_alarm":
            ringing = s.query(Reminder).filter(Reminder.status == "fired").all()
            if not ringing:
                status, msg = "unsupported", "Nothing is ringing right now."
            else:
                for r in ringing:
                    r.status = "dismissed"
                msg = "Dismissed."
        elif intent.kind == "note":
            body = intent.params["body"]
            if s.query(Note).filter(Note.body == body).first():
                msg = "You already have that note, so I didn't save it twice."
            else:
                s.add(Note(title=body[:60], body=body))
                msg = "Note saved."
        elif intent.kind == "todo":
            if s.query(Todo).filter(Todo.text == intent.params["text"], Todo.done.is_(False)).first():
                msg = f"“{intent.params['text']}” is already on your to-do list."
            else:
                s.add(Todo(text=intent.params["text"]))
                msg = f"Added to your to-do list: {intent.params['text']}"
        elif intent.kind == "agenda":
            msg = agenda_text(s, now)
        elif intent.kind in quick.KINDS:
            status, msg = quick.run(s, intent.kind, intent.params, now)
        elif intent.kind == "open_app":
            ref = appcontrol.find(prefs, intent.params["name"])
            if not ref:
                status = "unsupported"
                msg = (f"'{intent.params['name']}' is not one of your integrated apps. Add it on the Apps page "
                       "(Windows apps are listed there; tick the ones NEXUS may use).")
            else:
                appintents.launch_app(ref, launcher)
                msg = f"Opened {ref.name}."
        elif intent.kind in appintents.KINDS:
            status, msg = appintents.run(intent.kind, intent.params, prefs, launcher, system)
        elif intent.kind == "open_url":
            url = intent.params["url"]
            host = (urlparse(url).hostname or "").lower()
            ok = urlparse(url).scheme == "https" and any(
                host == d or host.endswith("." + d) for d in prefs.allowed_url_domains)
            if not ok:
                status = "unsupported"
                msg = f"{host or url} is not an approved website. Add it under Settings → Assistant."
            else:
                launcher(url, "url")
                msg = f"Opened {url} in your browser."
        elif intent.kind == "open_folder":
            name = intent.params["name"].lower()
            match = next((f for f in prefs.approved_folders if Path(f).name.lower() == name), None)
            if not match:
                status = "unsupported"
                msg = f"No approved folder named '{intent.params['name']}'. Approve folders under Settings → Assistant."
            elif not Path(match).is_dir():
                status, msg = "failed", f"Approved folder no longer exists: {match}"
            else:
                launcher(match, "folder")
                msg = f"Opened {match}."
        else:
            status, msg = "unsupported", "I can't perform that action."
    except Exception as e:
        status, msg = "failed", f"The action failed: {e}"
    rec = AssistantAction(kind=intent.kind, summary=intent.summary, params=intent.params, status=status,
                          result=msg, source=source)
    s.add(rec)
    s.flush()
    audit(s, source if source == "voice" else "user", f"assistant.{intent.kind}", intent.summary, outcome=status)
    return ActionOutcome(rec.id, status, msg)


def agenda_text(s: Session, now: dt.datetime) -> str:
    start = dt.datetime.combine(now.date(), dt.time.min)
    end = start + dt.timedelta(days=1)
    rems = (s.query(Reminder).filter(Reminder.status == "pending", Reminder.due_at >= start, Reminder.due_at < end)
            .order_by(Reminder.due_at).all())
    todos = s.query(Todo).filter(Todo.done.is_(False)).order_by(Todo.id).limit(10).all()
    lines = [f"Agenda for {now:%A %d %B} (from NEXUS's local data only — no calendar is connected):"]
    lines.append("Reminders today: " + ("; ".join(f"{r.due_at:%H:%M} {r.text}" for r in rems) if rems else "none"))
    lines.append("Open to-dos: " + ("; ".join(t.text for t in todos) if todos else "none"))
    return "\n".join(lines)
