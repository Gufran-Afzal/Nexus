"""Instant answers that never touch the language model.

Time, date, arithmetic, unit conversion, timers/reminders/to-do/notes queries, a focus timer, system status and
a few small-talk replies are computed by plain code. They answer in milliseconds, are always exactly right, and keep
the (slow, CPU-bound) model free for questions that actually need it. Patterns are deliberately strict: anything
that does not match exactly goes to the model.
"""

# ruff: noqa: E501  (long regular expressions read better on one line)
from __future__ import annotations

import ast
import datetime as dt
import operator
import random
import re
import shutil
from typing import Any

from sqlalchemy.orm import Session

from app.database.models import Note, Reminder, Todo

KINDS = {
    "time", "date", "greeting", "thanks", "capabilities", "math", "convert", "list_reminders", "time_left",
    "cancel_reminders", "list_todos", "done_todo", "list_notes", "search_notes", "focus", "system", "coin", "dice",
    "remember", "recall", "forget",
}

MEMORY_TITLE = "Memory"  # notes with this title are facts the user asked NEXUS to remember; they are shown in Notes


def load_memories(s: Session, limit: int = 12) -> list[str]:
    rows = s.query(Note).filter(Note.title == MEMORY_TITLE).order_by(Note.id.desc()).limit(limit).all()
    return [r.body.strip()[:300] for r in reversed(rows) if r.body.strip()]

# ---------------------------------------------------------------- safe arithmetic
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow}


def safe_eval(expr: str) -> float:
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, int | float) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub | ast.UAdd):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e9):
                raise ValueError("That number is too large for me to work out.")
            return float(_BIN[type(n.op)](a, b))
        raise ValueError("unsupported expression")

    if len(expr) > 200:
        raise ValueError("expression too long")
    return ev(ast.parse(expr, mode="eval"))


def fmt_number(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.8g}" if abs(x) >= 1e-4 else f"{x:.6g}"


_WORD_OPS = [
    (r"\bto the power of\b|\braised to\b", "**"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
    (r"\bmultiplied by\b|\btimes\b", "*"), (r"\bdivided by\b", "/"), (r"\bplus\b", "+"), (r"\bminus\b", "-"),
    (r"\bmodulo\b|\bmod\b", "%"),
]
_MATH_LEAD = re.compile(r"^(?:please\s+)?(?:(?P<verb>calculate|compute|evaluate|solve|work out)\s+|"
                        r"(?P<q>what(?:'s| is)|how much is|whats)\s+)", re.I)


def parse_math(low: str) -> str | None:
    """Return a safe arithmetic expression if the sentence is clearly a calculation, else None."""
    m = _MATH_LEAD.match(low)
    body = low[m.end():] if m else low
    body = body.replace(",", "") if re.search(r"\d,\d{3}", body) else body
    pct = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d+(?:\.\d+)?)", body)
    if pct and (m or True):
        return f"{pct.group(1)}/100*{pct.group(2)}"
    for pat, rep in _WORD_OPS:
        body = re.sub(pat, rep, body)
    body = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", body)
    body = body.replace("×", "*").replace("÷", "/").replace("^", "**")
    if not re.fullmatch(r"[\d\s+\-*/%().]+", body) or not re.search(r"\d", body) or not re.search(r"[+\-*/%]", body[1:] or ""):
        return None
    if not m and not re.search(r"\d\s*[+\-*/%]\s*\(?\d", body):
        return None
    return body.strip()


# ---------------------------------------------------------------- units
_LEN: dict[str, float] = {"mm": 0.001, "millimeter": 0.001, "millimeters": 0.001, "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
        "m": 1, "meter": 1, "meters": 1, "metre": 1, "metres": 1, "km": 1000, "kilometer": 1000, "kilometers": 1000,
        "kilometre": 1000, "kilometres": 1000, "in": 0.0254, "inch": 0.0254, "inches": 0.0254, "ft": 0.3048,
        "foot": 0.3048, "feet": 0.3048, "yd": 0.9144, "yard": 0.9144, "yards": 0.9144, "mi": 1609.344, "mile": 1609.344,
        "miles": 1609.344}
_MASS: dict[str, float] = {"mg": 1e-6, "g": 0.001, "gram": 0.001, "grams": 0.001, "kg": 1, "kilogram": 1, "kilograms": 1, "kilo": 1,
         "kilos": 1, "oz": 0.028349523, "ounce": 0.028349523, "ounces": 0.028349523, "lb": 0.45359237,
         "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237}
_VOL: dict[str, float] = {"ml": 0.001, "milliliter": 0.001, "milliliters": 0.001, "l": 1, "liter": 1, "liters": 1, "litre": 1,
        "litres": 1, "gallon": 3.785411784, "gallons": 3.785411784, "cup": 0.2365882365, "cups": 0.2365882365,
        "tbsp": 0.01478676, "tsp": 0.00492892}
_TIME: dict[str, float] = {"second": 1, "seconds": 1, "sec": 1, "minute": 60, "minutes": 60, "min": 60, "hour": 3600, "hours": 3600,
         "day": 86400, "days": 86400, "week": 604800, "weeks": 604800}
_TEMP: dict[str, str] = {"c": "c", "celsius": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}
_CATS: list[dict[str, float]] = [_LEN, _MASS, _VOL, _TIME]
_CONV_A = re.compile(r"^(?:convert\s+)?(?P<n>-?\d+(?:\.\d+)?)\s*(?:degrees?\s+)?(?P<a>[a-z]+)\s+(?:to|in|into)\s+(?:degrees?\s+)?(?P<b>[a-z]+)$")
_CONV_B = re.compile(r"^how many\s+(?P<b>[a-z]+)\s+(?:are\s+)?(?:there\s+)?in\s+(?:a\s+|an\s+|one\s+)?(?P<n>-?\d+(?:\.\d+)?)?\s*(?P<a>[a-z]+)$")
_CONV_C = re.compile(r"^what(?:'s| is)\s+(?P<n>-?\d+(?:\.\d+)?)\s*(?:degrees?\s+)?(?P<a>[a-z]+)\s+in\s+(?:degrees?\s+)?(?P<b>[a-z]+)$")


def convert(n: float, a: str, b: str) -> str | None:
    if a in _TEMP and b in _TEMP:
        ta, tb = _TEMP[a], _TEMP[b]
        c = n if ta == "c" else (n - 32) * 5 / 9 if ta == "f" else n - 273.15
        out = c if tb == "c" else c * 9 / 5 + 32 if tb == "f" else c + 273.15
        sym = {"c": "°C", "f": "°F", "k": " K"}
        return f"{fmt_number(n)}{sym[ta]} is {fmt_number(round(out, 2))}{sym[tb]}."
    for cat in _CATS:
        if a in cat and b in cat:
            out = n * cat[a] / cat[b]
            return f"{fmt_number(n)} {a} is {fmt_number(round(out, 4))} {b}."
    return None


# ---------------------------------------------------------------- recognition
def _plain(low: str, *alts: str) -> bool:
    return any(re.fullmatch(a, low) for a in alts)


def recognise(low: str, text: str) -> tuple[str, dict[str, Any], str] | None:
    """low = lower-cased text without trailing punctuation. Returns (kind, params, summary) or None."""
    if _plain(low, r"(?:what(?:'s| is)?\s+)?(?:the\s+)?(?:current\s+)?time(?:\s+is\s+it)?(?:\s+now)?(?:\s+please)?",
              r"what time is it(?: now| right now)?", r"tell me the time", r"whats the time"):
        return "time", {}, "Current time"
    if _plain(low, r"(?:what(?:'s| is)?\s+)?(?:the\s+)?(?:today'?s\s+)?date(?:\s+today)?", r"what day is (?:it|today)(?: today)?",
              r"what'?s today'?s date", r"what is today'?s date", r"what day of the week is it"):
        return "date", {}, "Today's date"
    if _plain(low, r"(?:hi|hello|hey|hey there|yo|good (?:morning|afternoon|evening))(?:,?\s+nexus)?"):
        return "greeting", {}, "Greeting"
    if _plain(low, r"(?:thanks|thank you|thank you so much|thanks a lot|cheers|great,? thanks)(?:,?\s+nexus)?"):
        return "thanks", {}, "Thanks"
    if _plain(low, r"what can you do|what do you do|help|what are your (?:features|skills|capabilities)|what can i ask you"):
        return "capabilities", {}, "What NEXUS can do"
    if _plain(low, r"(?:flip|toss) a coin|heads or tails"):
        return "coin", {}, "Coin flip"
    m = re.fullmatch(r"roll (?:a |an )?(?:d(?P<d>\d{1,3})|die|dice)", low)
    if m:
        return "dice", {"sides": int(m.group("d") or 6)}, "Dice roll"
    expr = parse_math(low)
    if expr:
        return "math", {"expr": expr}, f"Calculate {expr}"
    for rx in (_CONV_A, _CONV_C, _CONV_B):
        m = rx.fullmatch(low)
        if m:
            n = float(m.group("n") or 1)
            a, b = m.group("a"), m.group("b")
            if convert(n, a, b) is not None:
                return "convert", {"n": n, "a": a, "b": b}, f"Convert {a} to {b}"
    m = re.fullmatch(r"(?:what|which|list|show)(?:'s| are| is)?\s*(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:the\s+)?"
                     r"(?:(?:active|running|pending|upcoming)\s+)?(?P<k>timers?|reminders?|alarms?)"
                     r"(?:\s+do i have|\s+i have|\s+are (?:set|running)|\s+left)?(?:\s+today)?", low)
    if m:
        return "list_reminders", {"timers": m.group("k").startswith(("timer", "alarm"))}, "List timers/reminders"
    if _plain(low, r"how (?:much time|long) (?:is )?(?:left|remaining)(?: on| for)?(?: my| the)?(?: timer| next timer)?",
              r"how much time is left", r"time left(?: on (?:my )?timer)?"):
        return "time_left", {}, "Time left on the timer"
    m = re.fullmatch(r"(?:cancel|stop|clear|delete|remove)\s+(?:all\s+)?(?:of\s+)?(?:my\s+)?(?:the\s+)?(?:running\s+)?"
                     r"(?P<k>timers?|reminders?)", low)
    if m:
        return "cancel_reminders", {"timers": m.group("k").startswith("timer")}, "Cancel timers/reminders"
    if _plain(low, r"what(?:'s| is) on my (?:to-?do|task)s?(?: list)?", r"(?:show|list|read)(?: me)?(?: my)?(?: the)? (?:to-?do|task)s?(?: list)?",
              r"what (?:tasks|to-?dos) do i have", r"my (?:to-?do|task)s?(?: list)?"):
        return "list_todos", {}, "List to-dos"
    m = (re.fullmatch(r"mark\s+(?P<b>.+?)\s+as\s+(?:done|complete|completed|finished)", low)
         or re.fullmatch(r"(?:check off|tick off|cross off|complete task|finish task)\s+(?P<b>.+?)(?:\s+(?:from|on) my (?:to-?do|task)s?(?: list)?)?", low))
    if m:
        return "done_todo", {"text": m.group("b").strip()}, "Complete a to-do"
    if _plain(low, r"(?:show|list|read)(?: me)?(?: my)?(?: the)?(?: latest| recent| last)? notes?", r"what are my notes",
              r"my notes"):
        return "list_notes", {}, "List notes"
    m = re.fullmatch(r"(?:search|find|look up)(?: in)?(?: my)? notes?(?: for| about)\s+(?P<q>.+)", low) or re.fullmatch(
        r"what did i (?:note|write down|save)(?: about)\s+(?P<q>.+)", low)
    if m:
        return "search_notes", {"q": m.group("q").strip()}, "Search notes"
    m = re.fullmatch(r"(?:start|begin|set)\s+(?:a\s+|my\s+)?(?P<k>focus|pomodoro|break)(?:\s+(?:session|timer|time))?", low) or (
        re.fullmatch(r"(?P<k>pomodoro)", low))
    if m:
        k = m.group("k")
        return "focus", {"minutes": 5 if k == "break" else 25, "label": "Break" if k == "break" else "Focus session"}, "Focus timer"
    m = re.fullmatch(r"(?:please\s+)?remember(?:\s+that)?\s+(?P<b>(?!to\b|when\b|how\b|what\b|why\b|if\b|the\b time).+)", low)
    if m and len(m.group("b").split()) >= 2:
        body = re.sub(r"^remember(?: that)?\s+", "", text.strip().rstrip(".!?"), flags=re.I)
        body = re.sub(r"^please\s+remember(?: that)?\s+", "", body, flags=re.I)
        return "remember", {"body": body}, "Remember something"
    if _plain(low, r"what do you (?:remember|know)(?: about me)?", r"(?:show|list) (?:my )?memor(?:y|ies)", r"what have i told you"):
        return "recall", {}, "Recall memories"
    m = re.fullmatch(r"forget(?: that| about)?\s+(?P<b>.+)", low)
    if m:
        return "forget", {"text": m.group("b").strip()}, "Forget something"
    if _plain(low, r"how(?:'s| is) (?:my )?(?:computer|pc|laptop|system|battery|cpu|memory|ram|disk)(?: doing| usage)?",
              r"(?:system|battery|cpu|memory|ram|disk) (?:status|usage|level|info)",
              r"(?:what(?:'s| is) )?my (?:battery|cpu|memory|ram|disk)(?: level| usage| status)?",
              r"how much (?:battery|memory|ram|disk space)(?: is)?(?: left| free| used| do i have)?", r"system status",
              r"what(?:'s| is) my battery at"):
        return "system", {}, "System status"
    return None


# ---------------------------------------------------------------- execution
def _remaining(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s and not h else "") if p) or "0 s"


def system_status() -> str:
    try:
        import psutil
    except Exception:
        return "System details aren't available (the psutil package is missing)."
    parts = [f"CPU {psutil.cpu_percent(interval=0.2):.0f}%"]
    vm = psutil.virtual_memory()
    parts.append(f"memory {vm.percent:.0f}% used ({vm.used / 2**30:.1f} of {vm.total / 2**30:.0f} GB)")
    try:
        root = shutil.disk_usage("C:\\" if psutil.WINDOWS else "/")
        parts.append(f"disk {root.free / 2**30:.0f} GB free")
    except Exception:  # noqa: S110 - disk info is optional
        pass
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is not None:
        parts.append(f"battery {b.percent:.0f}%" + (" (charging)" if b.power_plugged else ""))
    return "Your computer: " + ", ".join(parts) + "."


def run(s: Session, kind: str, params: dict[str, Any], now: dt.datetime) -> tuple[str, str]:
    """Returns (status, message)."""
    if kind == "time":
        return "succeeded", f"It's {now:%I:%M %p}.".replace("It's 0", "It's ")
    if kind == "date":
        return "succeeded", f"It's {now:%A}, {now.day} {now:%B %Y}."
    if kind == "greeting":
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
        return "succeeded", f"Good {part}. What can I do for you?"
    if kind == "thanks":
        return "succeeded", "Anytime."
    if kind == "capabilities":
        return "succeeded", (
            "I can set timers, alarms and repeating reminders (and snooze them), keep notes and to-dos, open, close and switch to "
            "the Windows apps you integrate, search the web, control music and volume, lock your PC, take screenshots, open "
            "Windows settings, start an email draft, remember things you tell me, do quick maths and unit conversions, tell you the time or your computer's status, and answer questions or "
            "help with code using the local model. Everything stays on this computer.")
    if kind == "coin":
        return "succeeded", random.choice(["Heads.", "Tails."])  # noqa: S311 - not security related
    if kind == "dice":
        sides = max(2, min(1000, int(params["sides"])))
        return "succeeded", f"You rolled a {random.randint(1, sides)} (d{sides})."  # noqa: S311
    if kind == "math":
        try:
            return "succeeded", f"{fmt_number(safe_eval(params['expr']))}."
        except ZeroDivisionError:
            return "succeeded", "That's a division by zero, which has no answer."
        except Exception as e:
            return "unsupported", str(e) if "too large" in str(e) else "I couldn't work that out."
    if kind == "convert":
        return "succeeded", convert(params["n"], params["a"], params["b"]) or "I can't convert those units."
    if kind == "system":
        return "succeeded", system_status()
    if kind in ("list_reminders", "time_left", "cancel_reminders", "focus"):
        return _reminders(s, kind, params, now)
    if kind == "list_todos":
        todos = s.query(Todo).filter(Todo.done.is_(False)).order_by(Todo.id).limit(15).all()
        if not todos:
            return "succeeded", "Your to-do list is empty."
        return "succeeded", "To-do: " + "; ".join(t.text for t in todos) + "."
    if kind == "done_todo":
        want = params["text"].lower()
        todos = s.query(Todo).filter(Todo.done.is_(False)).all()
        hits = [t for t in todos if want in t.text.lower() or t.text.lower() in want]
        if len(hits) == 1:
            hits[0].done = True
            return "succeeded", f"Marked done: {hits[0].text}."
        if not hits:
            return "unsupported", f"I couldn't find an open to-do matching '{params['text']}'."
        return "unsupported", "That matches several to-dos: " + "; ".join(t.text for t in hits[:4]) + ". Be more specific."
    if kind == "remember":
        s.add(Note(title=MEMORY_TITLE, body=params["body"], pinned=False))
        return "succeeded", f"I'll remember that: {params['body']}."
    if kind == "recall":
        mem = load_memories(s, 20)
        return "succeeded", ("Here's what I remember: " + "; ".join(mem) + ".") if mem else "You haven't asked me to remember anything yet."
    if kind == "forget":
        want = re.sub(r"^(?:the|my|that|about)\s+", "", params["text"].lower())
        found = [n for n in s.query(Note).filter(Note.title == MEMORY_TITLE).all() if want in n.body.lower()]
        if len(found) == 1:
            s.delete(found[0])
            return "succeeded", f"Forgotten: {found[0].body}."
        if not found:
            return "unsupported", f"I don't have a memory matching '{params['text']}'."
        return "unsupported", f"That matches {len(found)} memories. Be more specific, or edit them under Notes."
    if kind == "list_notes":
        notes = s.query(Note).order_by(Note.updated_at.desc()).limit(5).all()
        if not notes:
            return "succeeded", "You have no notes yet."
        return "succeeded", "Latest notes: " + " | ".join((n.title or n.body)[:80] for n in notes)
    if kind == "search_notes":
        q = params["q"].lower()
        notes = [n for n in s.query(Note).order_by(Note.updated_at.desc()).limit(500).all()
                 if q in (n.title or "").lower() or q in (n.body or "").lower()][:5]
        if not notes:
            return "succeeded", f"No notes mention '{params['q']}'."
        return "succeeded", f"Notes about {params['q']}: " + " | ".join((n.body or n.title)[:100] for n in notes)
    return "unsupported", "I can't do that."


def _reminders(s: Session, kind: str, params: dict[str, Any], now: dt.datetime) -> tuple[str, str]:
    if kind == "focus":
        mins = int(params["minutes"])
        s.add(Reminder(kind="timer", text=f"{params['label']} ({mins} min)", due_at=now + dt.timedelta(minutes=mins)))
        return "succeeded", f"{params['label']} started: {mins} minutes. I'll tell you when it's over."
    q = s.query(Reminder).filter(Reminder.status == "pending")
    if kind == "time_left":
        t = q.filter(Reminder.kind == "timer").order_by(Reminder.due_at).first()
        if not t:
            return "succeeded", "You have no running timers."
        return "succeeded", f"{t.text}: {_remaining((t.due_at - now).total_seconds())} left."
    want_timers = bool(params.get("timers"))
    rows = q.filter((Reminder.kind == "timer") if want_timers else (Reminder.kind != "timer")).order_by(Reminder.due_at).all()
    noun = "timers" if want_timers else "reminders"
    if kind == "cancel_reminders":
        for r in rows:
            r.status = "cancelled"
        return "succeeded", f"Cancelled {len(rows)} {noun}." if rows else f"You have no pending {noun}."
    if not rows:
        return "succeeded", f"You have no pending {noun}."
    shown = [f"{r.text} in {_remaining((r.due_at - now).total_seconds())}" if want_timers
             else f"{r.text} at {r.due_at:%a %H:%M}" for r in rows[:6]]
    return "succeeded", f"You have {len(rows)} {noun}: " + "; ".join(shown) + "."
