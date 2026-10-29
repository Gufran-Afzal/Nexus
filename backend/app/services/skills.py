"""Skills: things NEXUS does *inside* other apps and websites for you (message someone on WhatsApp, draft an email in Gmail,
add a calendar event, start a route, type into a window).

How they work, honestly: no screen-scraping and no login handling. A skill builds a *plan* of small, safe steps: open a
vetted deep link (`whatsapp://send?phone=...&text=...`, Gmail's compose URL...) that pre-fills the draft in your own signed-in
app, and optionally press Send afterwards. Pressing Send is a separate step that ALWAYS needs your confirmation ("send it"),
and only happens if the right window is in front. Nothing is ever sent silently.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus

from app.services import appcontrol
from app.services.contacts import ContactRef, ambiguous, find_contact, split_recipient, spoken_email
from app.services.preferences import Preferences
from app.services.when import describe, parse_when
from app.windows.sysctl import TERMINALS, SystemControl, SystemControlError

Launcher = Callable[[list[str] | str, str], None]

SKILLS: list[tuple[str, str, list[str]]] = [
    ("message", "Message someone", ["Message Mom on WhatsApp I'll be late", "Tell Alex I'm running ten minutes late"]),
    ("email", "Write an email (Gmail)", ["Write an email to Sam asking for the project update", "Email priya@example.com about Friday"]),
    ("calendar", "Add a calendar event", ["Schedule a meeting with Sam on Friday at 2pm", "Add dentist to my calendar tomorrow at 3pm"]),
    ("maps", "Directions and places", ["Directions to the airport", "Find coffee near me"]),
    ("youtube", "YouTube", ["Play lo-fi beats on YouTube", "Search YouTube for python tutorials"]),
    ("new_doc", "New Google Doc, Sheet or Slides", ["Create a new Google Doc", "New spreadsheet"]),
    ("type", "Type into an app", ["Type hello world in Notepad", "Type thanks, see you at five"]),
]


# ------------------------------------------------------------------ data
@dataclass
class Step:
    kind: str  # uri | url | url_in | app | focus | wait | paste | keys
    value: Any = None
    ref: appcontrol.AppRef | None = None


@dataclass
class SendStep:
    """Pressing Send in the app the draft was opened in. Runs only after the user says so."""

    title: str
    preview: str
    focus: list[str]  # window-title fragments that identify the right window
    keys: str = "enter"


@dataclass
class Plan:
    skill: str
    summary: str
    reply: str
    steps: list[Step] = field(default_factory=list)
    send: SendStep | None = None
    ask: str | None = None  # a yes/no question to answer before anything happens (e.g. "Did you mean Alexander?")
    draft: tuple[str, str, str] | None = None  # (recipient name, recipient email, instruction): the model writes the email
    autosend: bool = False  # the user said "...and send it" in the same breath
    risky: bool = True  # worth confirming when speech recognition was unsure


@dataclass
class Clarify:
    message: str


@dataclass
class SkillContext:
    prefs: Preferences
    contacts: list[ContactRef]
    now: dt.datetime
    confident: bool = True  # speech was clear (or the request was typed)


# ------------------------------------------------------------------ helpers
_CHANNELS = ("whatsapp", "teams")
_TRAIL_SEND = re.compile(r"[\s,]+(?:and\s+)?(?:then\s+)?send\s*(?:it|that|the message|the email)?(?:\s+(?:now|right away))?$", re.I)
_LEAD_SAY = re.compile(r"^(?:and\s+)?(?:(?:that|saying|says|to say|telling (?:him|her|them)|with the message|the message|say|message)\b[:,]?\s*|:\s*)", re.I)
_VIA = re.compile(r"^(?:on|via|using|in|through|with)\s+(?P<c>whatsapp|teams|telegram|sms|gmail|chrome|outlook)\b\s*", re.I)


def _enabled(ctx: SkillContext, skill: str) -> bool:
    return skill not in ctx.prefs.disabled_skills


def _strip_send(text: str) -> tuple[str, bool]:
    m = _TRAIL_SEND.search(text)
    return (text[: m.start()].strip(), True) if m else (text.strip(), False)


def _clean_message(raw: str) -> str:
    msg = raw.strip()
    prev = None
    while prev != msg:  # "saying that ..." / "and say ..." stack
        prev, msg = msg, _LEAD_SAY.sub("", msg).strip()
    return msg.strip(" ,")


def _preview(text: str, n: int = 140) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _whatsapp_ref(ctx: SkillContext) -> appcontrol.AppRef | None:
    return appcontrol.find(ctx.prefs, "whatsapp")


# ------------------------------------------------------------------ message
def plan_message(ctx: SkillContext, who: str, text: str, channel: str = "", autosend: bool = False,
                 contact: tuple[ContactRef, bool] | None = None) -> Plan | Clarify:
    text = text.strip()
    channel = (channel or ctx.prefs.default_message_app).lower()
    if channel in ("telegram", "sms"):
        return Clarify(f"I can't message on {channel.title()} yet: WhatsApp and Teams work. Say “…on WhatsApp”.")
    if contact is None:
        many = ambiguous(ctx.contacts, who)
        if len(many) > 1:
            return Clarify("Which " + who.title() + "? " + " or ".join(c.name for c in many[:4]) + ". Say the full name.")
        contact = find_contact(ctx.contacts, who)
    if contact is None:
        return Clarify(f"I don't have anyone called “{who}” in your people list. Add them on the People page, with a phone number.")
    c, exact = contact
    if not text:
        return Clarify(f"What should the message to {c.name} say?")
    if channel == "teams":
        if not c.email:
            return Clarify(f"I don't have an email address for {c.name}, which Teams needs. Add it on the People page.")
        steps = [Step("url", f"https://teams.microsoft.com/l/chat/0/0?users={quote(c.email)}&message={quote(text, safe='')}")]
        send = SendStep(f"Teams message to {c.name}", _preview(text), ["Teams"], "enter")
        reply = f"I've opened a Teams chat with {c.name} with your message: “{_preview(text)}”."
    else:
        if not c.phone:
            return Clarify(f"I don't have a phone number for {c.name}. Add it on the People page (with the country code).")
        wa = _whatsapp_ref(ctx)
        link = f"phone={c.phone}&text={quote(text, safe='')}"
        if wa is not None:
            steps = [Step("uri", f"whatsapp://send?{link}")]
            send = SendStep(f"WhatsApp message to {c.name}", _preview(text), ["WhatsApp"], "enter")
            reply = f"I've opened WhatsApp with your message to {c.name}: “{_preview(text)}”."
        else:  # no desktop app integrated: WhatsApp Web in the browser (a human has to press Send there)
            steps = [Step("url", f"https://wa.me/{c.phone}?text={quote(text, safe='')}")]
            send = None
            reply = (f"I've opened WhatsApp Web with your message to {c.name}: “{_preview(text)}”. "
                     "Press Send there (integrate the WhatsApp app on the Apps page and I can press it for you).")
    plan = Plan("message", f"Message {c.name}", reply, steps, send, autosend=autosend)
    if not exact:
        plan.ask = f"Did you mean {c.name}?"
    return plan


def _match_message(t: str, low: str, ctx: SkillContext) -> Plan | Clarify | None:
    if not ctx.contacts:
        m0 = re.match(r"^(?:message|text|whatsapp)\s+\S+", low)
        return Clarify("Add the people you message on the People page (name and phone number) and I can message them.") if m0 else None
    rest = None
    channel = ""
    m = re.match(r"^(?:send|write)\s+(?:a\s+|an\s+)?(?:(?P<c>whatsapp|teams)\s+)?(?:message|text|msg|whatsapp)\s+to\s+(?P<r>.+)$", t, re.I)
    if m:
        rest, channel = m.group("r"), (m.group("c") or "").lower()
    if rest is None:
        m = re.match(r"^send\s+(?P<r>.+?)\s+(?:a\s+)?(?:(?P<c>whatsapp|teams)\s+)?(?:message|text|msg|whatsapp)\b\s*(?P<tail>.*)$", t, re.I)
        if m:
            rest, channel = f"{m.group('r')} {m.group('tail')}", (m.group("c") or "").lower()
    if rest is None:
        m = re.match(r"^(?P<v>message|text|whatsapp|msg)\s+(?P<r>.+)$", t, re.I)
        if m:
            rest, channel = m.group("r"), ("whatsapp" if m.group("v").lower() == "whatsapp" else "")
    if rest is None:
        m = re.match(r"^(?:tell|ask)\s+(?P<r>.+)$", t, re.I)
        if m:
            rest = m.group("r")
    if rest is None:
        m = re.match(r"^let\s+(?P<r>.+?)\s+know\s+(?P<msg>.+)$", t, re.I)
        if m:
            rest = f"{m.group('r')} {m.group('msg')}"
    if rest is None:
        return None
    verb_ask = low.startswith("ask ")
    words = rest.split()
    hit = split_recipient(ctx.contacts, words)
    if hit is None:
        if re.match(r"^(?:message|text|whatsapp|msg|send)\b", low):
            who = " ".join(words[:2]) if words else ""
            return plan_message(ctx, who, "x") if who and not low.startswith(("tell", "ask")) else None
        return None  # "tell me a joke": not a message to anyone you know
    c, exact, tail = hit
    msg_text = " ".join(tail)
    via = _VIA.match(msg_text)
    if via:
        channel = via.group("c").lower()
        msg_text = msg_text[via.end():]
    msg_text, autosend = _strip_send(msg_text)
    msg_text = _clean_message(msg_text)
    if verb_ask and msg_text:
        msg_text = re.sub(r"^(?:to\s+)?", "", msg_text)
        msg_text = msg_text[:1].upper() + msg_text[1:]
        if not msg_text.endswith("?") and re.match(r"^(?:if|whether|when|what|where|how|why|who)\b", msg_text, re.I):
            msg_text += "?"
    return plan_message(ctx, c.name, msg_text, channel, autosend, contact=(c, exact))


# ------------------------------------------------------------------ email
def plan_email(ctx: SkillContext, to: str = "", to_name: str = "", subject: str = "", body: str = "", instruction: str = "",
               in_app: str = "") -> Plan | Clarify:
    gmail = ctx.prefs.email_app == "gmail" or in_app.lower() in ("gmail", "chrome")
    if in_app.lower() == "outlook":
        gmail = False
    label = to_name or to or "a new email"
    reply_to = f" to {to_name}" if to_name else (f" to {to}" if to else "")
    plan = Plan("email", f"Email {label}", "", draft=(to_name, to, instruction) if instruction and not body else None)
    plan.steps = _email_steps(ctx, to, subject, body, gmail, in_app)
    plan.reply = (f"I've started an email{reply_to} in {'Gmail' if gmail else 'your mail app'}"
                  + (f" with the subject “{_preview(subject, 70)}”" if subject else "") + ".")
    if gmail:
        plan.send = SendStep(f"Email{reply_to}", _preview(body or subject or instruction or "(empty draft)"),
                             ["Gmail", "Compose", "New Message", "Mail"], "ctrl+enter")
    return plan


def _email_steps(ctx: SkillContext, to: str, subject: str, body: str, gmail: bool, in_app: str) -> list[Step]:
    body = body[:1500]
    if gmail:
        url = (f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(to, safe='@,')}&su={quote(subject, safe='')}"
               f"&body={quote(body, safe='')}")
        ref = appcontrol.find(ctx.prefs, in_app) if in_app and in_app.lower() in ("chrome", "edge", "firefox", "brave") else None
        return [Step("url_in", url, ref)] if ref is not None and ref.is_browser else [Step("url", url)]
    return [Step("uri", f"mailto:{quote(to, safe='@,')}?subject={quote(subject, safe='')}&body={quote(body, safe='')}")]


def apply_draft(plan: Plan, ctx: SkillContext, subject: str, body: str) -> Plan:
    """The model wrote the email: rebuild the plan's steps with the real subject and body."""
    to_name, to, _instr = plan.draft or ("", "", "")
    gmail = ctx.prefs.email_app == "gmail" or plan.send is not None
    plan.steps = _email_steps(ctx, to, subject, body, gmail, "")
    plan.reply = (f"I wrote a draft{' to ' + to_name if to_name else ''} with the subject “{_preview(subject, 70)}” and opened it. "
                  "Read it through before sending.")
    if plan.send is not None:
        plan.send.preview = _preview(body, 200)
    plan.draft = None
    return plan


def _match_email(t: str, low: str, ctx: SkillContext) -> Plan | Clarify | None:
    m = re.match(r"^(?:write|compose|draft|send|create|start|make)\s+(?:me\s+)?(?:an?\s+|new\s+|another\s+)?(?:new\s+)?(?:e-?mail|mail|gmail)\b(?P<r>.*)$", t, re.I)
    if not m:
        m = re.match(r"^e-?mail\s+(?P<r>.+)$", t, re.I)
    if not m:
        return None
    rest = m.group("r").strip()
    in_app = ""
    vm = re.search(r"\s*\b(?:in|using|with|via|on)\s+(gmail|chrome|outlook)\b", rest, re.I)
    if vm:
        in_app = vm.group(1).lower()
        rest = (rest[: vm.start()] + " " + rest[vm.end():]).strip()
    rest = re.sub(r"^(?:to|for)\s+", "", rest, flags=re.I)
    to_name = to = ""
    instruction = rest
    words = rest.split()
    hit = None
    ask = None
    for k in (5, 4, 3, 1):  # "alex at example dot com ..." or "alex@example.com ..." beat a nickname that starts the same way
        cand = spoken_email(" ".join(words[:k])) if len(words) >= k else None
        if cand:
            to, instruction = cand, " ".join(words[k:])
            break
    else:
        hit = split_recipient(ctx.contacts, words) if rest else None
    if hit:
        c, exact, tail = hit
        if not c.email:
            return Clarify(f"I don't have an email address for {c.name}. Add it on the People page.")
        to_name, to, instruction = c.name, c.email, " ".join(tail)
        if not exact:
            ask = f"Did you mean {c.name}?"
    instruction = re.sub(r"^(?:about|regarding|re|saying|that says|asking|telling|to tell|with subject|subject|and)\b[:,]?\s*", "", instruction,
                         flags=re.I).strip() if instruction else ""
    instruction, autosend = _strip_send(instruction)
    plan = plan_email(ctx, to, to_name, instruction=instruction, in_app=in_app)
    if isinstance(plan, Plan):
        plan.ask = ask
        plan.autosend = autosend
    return plan


# ------------------------------------------------------------------ calendar, maps, youtube, docs
def plan_calendar(ctx: SkillContext, title: str, when_text: str, minutes: int | None = None) -> Plan | Clarify:
    from app.services.actions import parse_duration

    w = parse_when(when_text, ctx.now)
    if w is None:
        return Clarify("When is it? For example: “tomorrow at 3pm” or “on Friday at 2”.")
    if minutes is None:
        dur = parse_duration(when_text)
        minutes = max(5, min(dur // 60, 24 * 60)) if dur else 60
    title = (title or w.rest or "Event").strip()
    end = w.due + dt.timedelta(minutes=minutes)
    fmt = "%Y%m%dT%H%M%S"
    url = (f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={quote(title, safe='')}"
           f"&dates={w.due.strftime(fmt)}/{end.strftime(fmt)}")
    return Plan("calendar", f"Calendar: {title}", f"I've opened Google Calendar with “{title}” {describe(w.due, '', ctx.now)} "
                f"({minutes} min). Click Save to add it.", [Step("url", url)], risky=False)


def _match_calendar(t: str, low: str, ctx: SkillContext) -> Plan | Clarify | None:
    m = re.match(r"^(?:add|create|schedule|book|put|set up|make)\s+(?:me\s+)?(?:an?\s+)?(?P<r>.+)$", t, re.I)
    if not m or not re.search(r"\b(calendar|event|meeting|appointment|call with|catch ?up)\b", low):
        return None
    rest = m.group("r")
    rest = re.sub(r"\s*\b(?:to|on|in|into)\s+(?:my\s+)?(?:google\s+)?calendar\b", "", rest, flags=re.I)
    rest = re.sub(r"^(?:new\s+)?(?:calendar\s+)?(?:event|appointment)\s+(?:called|named|for|titled)?\s*", "", rest, flags=re.I)
    w = parse_when(rest, ctx.now)
    if w is None:
        return Clarify("When is it? For example: “tomorrow at 3pm” or “on Friday at 2”.")
    from app.services.actions import DUR

    title = re.sub(r"\bfor\s+" + DUR, "", w.rest, flags=re.I).strip(" ,") or "Event"
    return plan_calendar(ctx, title, rest)


def plan_maps(query: str, directions: bool) -> Plan:
    if directions:
        url = f"https://www.google.com/maps/dir/?api=1&destination={quote_plus(query)}"
        return Plan("maps", f"Directions to {query}", f"Opened directions to {query} in Google Maps.", [Step("url", url)], risky=False)
    return Plan("maps", f"Map: {query}", f"Searching Google Maps for {query}.",
                [Step("url", f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}")], risky=False)


def _match_maps(t: str, low: str, ctx: SkillContext) -> Plan | None:
    m = re.match(r"^(?:get\s+)?(?:directions?|navigate|route|take me|how do i get)\s+(?:to|me to)?\s*(?P<q>.+)$", t, re.I)
    if m and len(m.group("q")) > 2:
        return plan_maps(m.group("q").strip(), True)
    m = re.match(r"^(?:where is|where's|show me|find|search for)\s+(?P<q>.+?)\s+(?:on (?:the |google )?maps?|near (?:me|here)|nearby)$", t, re.I)
    if m:
        q = m.group("q").strip() + (" near me" if re.search(r"near", low) else "")
        return plan_maps(q, False)
    return None


def plan_youtube(query: str) -> Plan:
    return Plan("youtube", f"YouTube: {query}", f"Searching YouTube for {query}. (I can open the results, not press play.)",
                [Step("url", f"https://www.youtube.com/results?search_query={quote_plus(query)}")], risky=False)


def _match_youtube(t: str, low: str, ctx: SkillContext) -> Plan | None:
    m = re.match(r"^(?:play|search|find|watch|look up|search for)\s+(?P<q>.+?)\s+(?:on|in)\s+youtube$", t, re.I) or re.match(
        r"^(?:search\s+)?youtube\s+(?:for\s+)?(?P<q>.+)$", t, re.I)
    return plan_youtube(m.group("q").strip()) if m else None


_DOC_URL = {"doc": ("Google Docs", "https://docs.new"), "sheet": ("Google Sheets", "https://sheets.new"),
            "slides": ("Google Slides", "https://slides.new")}


def plan_doc(kind: str) -> Plan:
    name, url = _DOC_URL[kind]
    return Plan("new_doc", f"New {name}", f"Opened a new blank file in {name}.", [Step("url", url)], risky=False)


def _match_doc(t: str, low: str, ctx: SkillContext) -> Plan | None:
    m = re.match(r"^(?:create|make|start|open|new)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:(?P<g>google)\s+)?(?P<k>doc|document|sheet|spreadsheet|slides?|presentation)\b", low)
    if not m:
        m = re.match(r"^new\s+(?P<k>doc|document|sheet|spreadsheet|slides?|presentation)$", low)
        if not m:
            return None
    elif not m.group("g") and not re.match(r"^(?:create|make|start|new)\b", low):
        return None  # "open document" alone means a local file, not a new Google file
    k = m.group("k")
    kind = "sheet" if k in ("sheet", "spreadsheet") else "slides" if k.startswith(("slide", "presentation")) else "doc"
    return plan_doc(kind)


# ------------------------------------------------------------------ type into an app
def plan_type(ctx: SkillContext, text: str, app: str = "") -> Plan | Clarify:
    text = text.strip()
    if not text:
        return Clarify("What should I type?")
    steps: list[Step] = []
    where = "the window in front"
    if app:
        ref = appcontrol.find(ctx.prefs, app)
        if ref is None:
            return Clarify(f"“{app}” is not one of your integrated apps. Add it on the Apps page.")
        where = ref.name
        steps += [Step("focus_or_open", [ref.name, ref.exe_name[:-4] if ref.exe_name.endswith(".exe") else ""], ref)]
    steps += [Step("guard_terminal"), Step("paste", text)]
    return Plan("type", f"Type into {where}", f"Typed it into {where}: “{_preview(text, 100)}”.", steps)


def _match_type(t: str, low: str, ctx: SkillContext) -> Plan | Clarify | None:
    known = appcontrol.app_names(ctx.prefs)
    m = re.match(r"^(?:type|dictate)\s+(?P<x>.+)$", t, re.I)
    if m:
        body = m.group("x")
        app = ""
        am = re.search(r"\s+(?:in|into|on|to)\s+(?:the\s+)?(?P<app>[\w .+#-]{2,40})$", body, re.I)
        if am:
            from app.services.appintents import match_known

            hit = match_known(known, am.group("app")) if known else None
            if hit:
                app, body = hit, body[: am.start()]
        # Without a named app the text would land in whatever window is in front (even this chat): so it is not a command.
        return plan_type(ctx, body.strip(" ,"), app) if app else None
    m = re.match(r"^write\s+(?P<x>.+?)\s+(?:in|into)\s+(?:the\s+)?(?P<app>[\w .+#-]{2,40})$", t, re.I)
    if m and known and not re.match(r"^(?:a|an|the|some)\s", m.group("x"), re.I):
        from app.services.appintents import match_known

        hit = match_known(known, m.group("app"))
        if hit:
            return plan_type(ctx, m.group("x"), hit)
    return None


# ------------------------------------------------------------------ custom skills
def _match_custom(t: str, low: str, ctx: SkillContext) -> Plan | None:
    for cs in ctx.prefs.custom_skills:
        if cs.name in ctx.prefs.disabled_skills or "{q}" not in cs.url or not cs.url.startswith("https://") or "{q}" not in cs.phrase:
            continue
        rx = "^" + re.escape(cs.phrase.lower()).replace(re.escape("{q}"), "(?P<q>.+)") + "$"
        m = re.match(rx, low)
        if m:
            q = t[m.start("q"):m.end("q")].strip()
            return Plan("custom:" + cs.name, cs.name, f"{cs.name}: {q}.", [Step("url", cs.url.replace("{q}", quote_plus(q)))], risky=False)
    return None


# ------------------------------------------------------------------ entry points
MATCHERS: list[tuple[str, Callable[[str, str, SkillContext], Plan | Clarify | None]]] = [
    ("email", _match_email), ("message", _match_message), ("calendar", _match_calendar), ("new_doc", _match_doc),
    ("youtube", _match_youtube), ("maps", _match_maps), ("type", _match_type), ("custom", _match_custom),
]


def match(text: str, ctx: SkillContext) -> Plan | Clarify | None:
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+nexus[,.]?\s+|(?:please|can you|could you|would you|will you)\s+)+", "", text.strip().rstrip(".!?"),
               flags=re.I)
    low = t.lower()
    if len(t) > 600:
        return None
    for name, fn in MATCHERS:
        if name != "custom" and not _enabled(ctx, name):
            continue
        r = fn(t, low, ctx)
        if r is not None:
            return r
    return None


# ------------------------------------------------------------------ execution
class Runner:
    def __init__(self, launcher: Launcher, system: SystemControl, sleep: Callable[[float], None] = time.sleep) -> None:
        self.launcher, self.system, self.sleep = launcher, system, sleep

    def run_steps(self, plan: Plan, prefs: Preferences) -> None:
        from app.services.appintents import _launch

        for st in plan.steps:
            if st.kind == "uri":
                self.launcher(st.value, "uri")
            elif st.kind == "url":
                self.launcher(st.value, "url")
            elif st.kind == "url_in":
                if st.ref is not None:
                    _launch(st.ref, self.launcher, [st.value])
                else:
                    self.launcher(st.value, "url")
            elif st.kind == "focus_or_open":
                if not self.system.focus([n for n in st.value if n]):
                    if st.ref is not None:
                        _launch(st.ref, self.launcher)
                    self.sleep(max(2.0, prefs.send_key_delay_s - 0.5))
                    self.system.focus([n for n in st.value if n])
            elif st.kind == "guard_terminal":
                title, exe = self.system.foreground()
                if exe in TERMINALS:
                    raise SystemControlError(f"I won't type into a terminal ({exe}): it would run what I typed.")
            elif st.kind == "paste":
                self.system.paste_text(st.value)
            elif st.kind == "wait":
                self.sleep(float(st.value))

    def press_send(self, send: SendStep, prefs: Preferences, just_opened: bool = False) -> tuple[bool, str]:
        """Press Send, but only if the right window is in front. Returns (pressed, message)."""
        if just_opened:
            self.sleep(prefs.send_key_delay_s)
        needles = [n.lower() for n in send.focus]
        title, _exe = self.system.foreground()
        if not any(n in title.lower() for n in needles):
            if not self.system.focus(send.focus):
                return False, f"I couldn't find the window to send from, so I haven't sent it. Press Send yourself ({send.title})."
            self.sleep(0.5)
            title, _exe = self.system.foreground()
            if not any(n in title.lower() for n in needles):
                return False, f"The right window isn't in front, so I haven't sent it. Press Send yourself ({send.title})."
        self.system.send_keys(send.keys)
        return True, f"Pressed Send: {send.title}. (I can't see the result, so check it went.)"


@dataclass
class Pending:
    kind: str  # "ask" (confirm a guess / unsure hearing) | "send"
    title: str
    preview: str
    plan: Plan | None = None
    send: SendStep | None = None
    intent: Any = None  # an actions.Intent held back because speech recognition was unsure
    expires: float = 0.0
    question: str = ""
    run_id: int | None = None  # kind "run": an automation run waiting for the user's yes/no
    ttl_s: float = 0.0  # overrides the store's default lifetime


class PendingStore:
    def __init__(self, ttl_s: float = 90.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl_s, self.clock = ttl_s, clock
        self.item: Pending | None = None

    def set(self, p: Pending) -> None:
        p.expires = self.clock() + (p.ttl_s or self.ttl_s)
        self.item = p

    def get(self) -> Pending | None:
        if self.item is not None and self.clock() > self.item.expires:
            self.item = None
        return self.item

    def clear(self) -> None:
        self.item = None


YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|correct|right|do it|go ahead|go for it|send(?: it| that| the (?:message|email))?|"
                 r"confirm(?:ed)?|please do|that'?s right|that is right|affirmative|absolutely|of course)(?:\s+please)?(?:\s+nexus)?$", re.I)
NO = re.compile(r"^(?:no|nope|nah|cancel(?: it| that)?|don'?t(?: send)?(?: it)?|stop|never ?mind|abort|that'?s wrong|wrong|negative|"
                r"do not|forget it|scratch that)(?:\s+nexus)?$", re.I)


def verdict(text: str) -> str | None:
    t = text.strip().rstrip(".!?,")
    return "yes" if YES.match(t) else "no" if NO.match(t) else None


def execute_plan(plan: Plan, runner: Runner, prefs: Preferences, store: PendingStore) -> tuple[str, str]:
    """Run the plan's steps. If it ends in Send, hold that step for the user's confirmation (or run it when they asked
    for it in the same sentence). Returns (status, message)."""
    try:
        runner.run_steps(plan, prefs)
    except SystemControlError as e:
        return "unsupported", str(e)
    except Exception as e:
        return "failed", f"That didn't work: {e}"
    if plan.send is None:
        return "succeeded", plan.reply
    if plan.autosend:
        ok, msg = runner.press_send(plan.send, prefs, just_opened=True)
        return ("succeeded" if ok else "failed"), f"{plan.reply} {msg}"
    store.set(Pending("send", plan.send.title, plan.send.preview, send=plan.send))
    return "succeeded", f"{plan.reply} Say “send it” to send, or “cancel”."
