"""Natural-language time for reminders and alarms: "in 20 minutes", "tomorrow morning", "every weekday at 9",
"on friday at 3pm", "next monday", "tonight". Pure functions (no I/O) so they are easy to test."""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_RE = "|".join(DAYS)
PART_OF_DAY = {"morning": (9, 0), "afternoon": (15, 0), "evening": (18, 0), "tonight": (20, 0), "noon": (12, 0),
               "midday": (12, 0), "midnight": (0, 0)}
REPEATS = ("daily", "weekdays", "weekly", "monthly")


@dataclass
class When:
    due: dt.datetime
    repeat: str = ""  # "" | daily | weekdays | weekly | monthly
    rest: str = ""  # the text with the time words removed


def _clock(h: int, m: int, ap: str) -> tuple[int, int] | None:
    ap = ap.lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or m > 59:
        return None
    return h, m


def next_occurrence(due: dt.datetime, repeat: str, now: dt.datetime) -> dt.datetime | None:
    """The next time after `now` for a repeating reminder that was due at `due` (None if it doesn't repeat)."""
    if repeat not in REPEATS:
        return None
    nxt = due
    for _ in range(4000):  # bounded: never loops forever on a bad value
        if repeat == "daily":
            nxt += dt.timedelta(days=1)
        elif repeat == "weekdays":
            nxt += dt.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
        elif repeat == "weekly":
            nxt += dt.timedelta(days=7)
        else:  # monthly: same day next month, clamped (31st -> 30th/28th)
            y, mth = nxt.year + (nxt.month // 12), nxt.month % 12 + 1
            nxt = nxt.replace(year=y, month=mth, day=min(due.day, calendar.monthrange(y, mth)[1]))
        if nxt > now:
            return nxt
    return None


def parse_when(text: str, now: dt.datetime, duration_re: str | None = None) -> When | None:
    """Find a time in `text`. Returns the due time, the repeat rule and the remaining text, or None if there is no time."""
    from app.services.actions import DUR, parse_duration  # local import: actions imports this module

    low = text
    repeat = ""
    weekday_anchor: int | None = None

    # relative: "in 20 minutes" / "in 1 hour and 30 minutes"
    rel = re.search(r"\bin\s+" + DUR + r"(?:\s+and\s+" + DUR.replace("?P<n>", "").replace("?P<u>", "") + ")?", low, re.I)
    if rel:
        secs = parse_duration(rel.group(0))
        if secs:
            rest = (low[: rel.start()] + low[rel.end():])
            return When(now + dt.timedelta(seconds=secs), "", _tidy(rest))

    # repeat rules
    m = re.search(rf"\bevery\s+(?:other\s+)?(?P<w>day|weekday|week|month|{_DAY_RE})s?\b|\b(?P<d>daily|weekly|monthly|weekdays)\b", low, re.I)  # noqa: E501
    if m:
        w = (m.group("w") or m.group("d")).lower()
        if w in ("day", "daily"):
            repeat = "daily"
        elif w in ("weekday", "weekdays"):
            repeat = "weekdays"
        elif w in ("week", "weekly"):
            repeat = "weekly"
        elif w in ("month", "monthly"):
            repeat = "monthly"
        else:
            repeat, weekday_anchor = "weekly", DAYS.index(w)
        low = low[: m.start()] + " " + low[m.end():]

    day: dt.date | None = None
    m = re.search(r"\bday after tomorrow\b", low, re.I)
    if m:
        day = now.date() + dt.timedelta(days=2)
        low = low[: m.start()] + " " + low[m.end():]
    else:
        m = re.search(r"\btomorrow\b", low, re.I)
        if m:
            day = now.date() + dt.timedelta(days=1)
            low = low[: m.start()] + " " + low[m.end():]
    if day is None:
        m = re.search(rf"\b(?:(?P<nx>next)\s+|on\s+)?(?P<w>{_DAY_RE})\b", low, re.I)
        if m:
            target = DAYS.index(m.group("w").lower())
            ahead = (target - now.weekday()) % 7
            if m.group("nx") and ahead == 0:
                ahead = 7
            day = now.date() + dt.timedelta(days=ahead)  # today if it is that weekday (rolled forward below if past)
            low = low[: m.start()] + " " + low[m.end():]
    if day is None and weekday_anchor is not None:
        day = now.date() + dt.timedelta(days=(weekday_anchor - now.weekday()) % 7)
    if day is None:
        m = re.search(r"\btoday\b", low, re.I)
        if m:
            day = now.date()
            low = low[: m.start()] + " " + low[m.end():]

    hm: tuple[int, int] | None = None
    m = re.search(r"\b(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)\b", low, re.I)
    if not m:
        m = re.search(r"\b(?:at\s+)(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\b(?!\s*(?:minutes?|mins?|hours?|hrs?|seconds?))", low, re.I)
    if not m:
        m = re.search(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b", low, re.I)
    if m:
        hm = _clock(int(m.group("h")), int(m.group("m") or 0), (m.groupdict().get("ap") or ""))
        if hm:
            if not m.groupdict().get("ap") and 1 <= int(m.group("h")) <= 11:
                h12 = int(m.group("h"))
                if day is None and not repeat:  # "at 5": the next 5 o'clock, whichever of am/pm comes first
                    cands = [dt.datetime.combine(now.date(), dt.time(hh, hm[1])) for hh in (h12, h12 + 12)]
                    best = min(c if c > now else c + dt.timedelta(days=1) for c in cands)
                    hm = (best.hour, best.minute)
                elif h12 <= 6:  # "tomorrow at 3" / "every day at 3": afternoon; "at 9": morning
                    hm = (h12 + 12, hm[1])
            low = low[: m.start()] + " " + low[m.end():]
    if hm is None:
        m = re.search(r"\b(?:in the\s+|this\s+)?(?P<p>morning|afternoon|evening|tonight|noon|midday|midnight)\b", low, re.I)
        if m:
            hm = PART_OF_DAY[m.group("p").lower()]
            if m.group("p").lower() == "tonight" and day is None:
                day = now.date()
            low = low[: m.start()] + " " + low[m.end():]

    if hm is None and day is None and not repeat:
        return None
    if hm is None:
        hm = (9, 0)  # a day or repeat with no time of day: 9 o'clock
    base = day or now.date()
    due = dt.datetime.combine(base, dt.time(*hm))
    if due <= now:
        if repeat == "weekly" and weekday_anchor is not None:
            due += dt.timedelta(days=7)
        elif day is not None and day > now.date():
            pass
        elif repeat in ("weekdays",):
            due += dt.timedelta(days=1)
            while due.weekday() >= 5:
                due += dt.timedelta(days=1)
        else:
            due += dt.timedelta(days=1)
    if repeat == "weekdays" and due.weekday() >= 5:
        while due.weekday() >= 5:
            due += dt.timedelta(days=1)
    return When(due, repeat, _tidy(low))


def _tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.-")
    t = re.sub(r"^(?:and|to|that)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:and|at|on|in|every|each)$", "", t, flags=re.I)
    return t.strip(" ,.-")


def describe(due: dt.datetime, repeat: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    if due.date() == now.date():
        day = "today"
    elif due.date() == now.date() + dt.timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{due:%a %d %b}"
    when = f"{day} at {due:%H:%M}"
    return when + {"daily": ", every day", "weekdays": ", every weekday", "weekly": f", every {due:%A}",
                   "monthly": f", monthly on the {due.day}"}.get(repeat, "")
