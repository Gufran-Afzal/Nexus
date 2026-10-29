"""The tools NEXUS really has, expressed in the tool contract. Each wraps existing, tested code (app control, skills, reminders,
workspace tools, approved folders) and adds what the old paths lacked: an observation after acting and a pre-check that avoids
repeating work that is already done."""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.agent.capabilities import (
    AWAITING,
    BLOCKED,
    FAILED,
    INVALID,
    MISSING,
    NOT_FOUND,
    PERMISSION,
    TRANSIENT,
    Registry,
    Tool,
    ToolContext,
    ToolResult,
    fail,
    ok,
)
from app.database.models import ChangeSet, CommandRun, Note, Reminder, Todo, Workspace
from app.services import appcontrol, appintents, skills
from app.services.contacts import ContactRef, find_contact
from app.services.when import describe, parse_when
from app.windows.sysctl import SystemControlError

AMBIGUOUS = "ambiguous"  # the request could mean more than one thing: ask the user
BLOCKING = re.compile(r"\b(update|updating|error|sign[- ]?in|log[- ]?in|license|crash|not responding|warning|what'?s new|permission)\b", re.I)


# ======================================================================= argument models
class AppArgs(BaseModel):
    name: str = Field(..., min_length=1, max_length=80, description="Name of an integrated app, e.g. 'spotify'")


class QueryArgs(BaseModel):
    query: str = Field("", max_length=120)


class NoArgs(BaseModel):
    pass


class ReminderArgs(BaseModel):
    text: str = Field(..., min_length=1, max_length=300, description="What to remind about")
    when: str = Field(..., min_length=1, max_length=120, description="When, in words: 'in 10 minutes', 'tomorrow at 9am'")


class NoteArgs(BaseModel):
    title: str = Field("", max_length=300)
    body: str = Field(..., min_length=1, max_length=5000)


class TodoArgs(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)


class MessageArgs(BaseModel):
    person: str = Field(..., min_length=1, max_length=80, description="A person from the People list")
    text: str = Field(..., min_length=1, max_length=1000)
    channel: Literal["", "whatsapp", "teams"] = ""


class SendArgs(BaseModel):
    target: Literal["whatsapp", "teams", "gmail"]
    preview: str = Field("", max_length=300)


class EmailArgs(BaseModel):
    person: str = Field("", max_length=80)
    to: str = Field("", max_length=200)
    subject: str = Field("", max_length=150)
    body: str = Field("", max_length=1500)


class CalendarArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    when: str = Field(..., min_length=1, max_length=120)
    minutes: int | None = Field(None, ge=5, le=1440)


class UrlArgs(BaseModel):
    url: str = Field(..., min_length=8, max_length=500, description="An https:// address")


class VolumeArgs(BaseModel):
    action: Literal["up", "down", "mute", "set"]
    percent: int = Field(50, ge=0, le=100)


class FolderArgs(BaseModel):
    folder: str = Field(..., min_length=1, max_length=300, description="An approved folder: its name (Downloads) or full path")
    pattern: str = Field("", max_length=60, description="Only names containing this text")
    limit: int = Field(100, ge=1, le=500)


class ReadArgs(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    max_chars: int = Field(6000, ge=100, le=20000)


class Move(BaseModel):
    src: str = Field(..., min_length=1, max_length=400)
    dst: str = Field(..., min_length=1, max_length=400)


class OrganizeArgs(BaseModel):
    folder: str = Field(..., min_length=1, max_length=300)


class ApplyMovesArgs(BaseModel):
    folder: str = Field(..., min_length=1, max_length=300)
    moves: list[Move] = Field(..., min_length=1, max_length=500)


class ProjectListArgs(BaseModel):
    path: str = Field(".", max_length=300)
    limit: int = Field(100, ge=1, le=500)


class ProjectSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)


class ProjectReadArgs(BaseModel):
    path: str = Field(..., min_length=1, max_length=400)
    start_line: int = Field(1, ge=1)
    end_line: int | None = Field(None, ge=1)


class ProposeEditArgs(BaseModel):
    path: str = Field(..., min_length=1, max_length=400)
    new_content: str = Field(..., max_length=400_000)
    summary: str = Field("", max_length=500)


class ProposeCommandArgs(BaseModel):
    command: str = Field(..., min_length=1, max_length=1000)
    reason: str = Field("", max_length=300)


# ======================================================================= helpers
def _observe(ctx: ToolContext) -> list[tuple[str, str]] | None:
    try:
        return ctx.system.list_windows() if ctx.system.can_observe() else None
    except SystemControlError:
        return None


def _matches(ref: appcontrol.AppRef, title: str, exe: str) -> bool:
    exe_name = ref.exe_name
    if exe_name and exe == exe_name:
        return True
    low = title.lower()
    stem = exe_name[:-4] if exe_name.endswith(".exe") else ""
    return bool(ref.name.lower() in low or (stem and stem in low))


def _find_windows(ref: appcontrol.AppRef, ctx: ToolContext) -> list[tuple[str, str]] | None:
    """The app's real windows. A dialog the app put up ("Update available") is not the app being open, so it is left out
    unless it is all there is."""
    wins = _observe(ctx)
    if wins is None:
        return None
    mine = [w for w in wins if _matches(ref, *w)]
    main = [w for w in mine if not BLOCKING.search(w[0])]
    return main


def _ref(ctx: ToolContext, name: str) -> appcontrol.AppRef | None:
    return appcontrol.find(ctx.prefs(), name)


def _wait_for(ctx: ToolContext, check: Any) -> Any:
    """Poll `check()` until it returns something truthy, for at most ctx.verify_timeout_s of (injectable) sleeping."""
    waited = 0.0
    while True:
        got = check()
        if got or waited >= ctx.verify_timeout_s:
            return got
        ctx.sleep(ctx.poll_s)
        waited += ctx.poll_s


def _skill_ctx(ctx: ToolContext) -> skills.SkillContext:
    from sqlalchemy import select

    from app.database.models import Contact

    with ctx.db.session() as s:
        people = [ContactRef(r.id, r.name, list(r.aliases or []), r.phone or "", r.email or "") for r in s.scalars(select(Contact))]
    return skills.SkillContext(ctx.prefs(), people, ctx.now())


def _approved_folder(ctx: ToolContext, spec: str) -> Path | None:
    """An approved folder named by its leaf ('downloads') or its path. Anything else is not allowed."""
    prefs = ctx.prefs()
    want = spec.strip().strip('"').rstrip("\\/")
    low = re.split(r"[\\/]", want)[-1].lower()
    for f in prefs.approved_folders:
        p = Path(f)
        if want == f or p.name.lower() == low or want.lower() == f.lower():
            return p if p.is_dir() else None
    return None


def _inside(root: Path, rel: str) -> Path | None:
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    full = (root / Path(*p.parts)).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    return full


def _no_folder(spec: str) -> ToolResult:
    return ToolResult(FAILED, f"“{spec}” is not one of your approved folders, so I can't touch it. Approve it in Settings first.",
                      error="not approved", error_class=PERMISSION)


# ======================================================================= apps and windows
def _apps_available(ctx: ToolContext) -> tuple[bool, str]:
    return (True, "") if appcontrol.app_names(ctx.prefs()) else (False, "No integrated apps yet (add them on the Apps page).")


def _windows_available(ctx: ToolContext) -> tuple[bool, str]:
    return (True, "") if ctx.system.can_observe() else (False, "Seeing windows needs Windows.")


def open_already(a: AppArgs, ctx: ToolContext) -> ToolResult | None:
    ref = _ref(ctx, a.name)
    wins = _find_windows(ref, ctx) if ref else None
    if ref and wins:
        r = ok(f"{ref.name} is already open.", app=ref.name, window=wins[0][0])
        r.already_done, r.verified, r.observed = True, True, wins[0][0]
        return r
    return None


def open_run(a: AppArgs, ctx: ToolContext) -> ToolResult:
    ref = _ref(ctx, a.name)
    if ref is None:
        return ToolResult(FAILED, f"“{a.name}” is not one of your integrated apps. Add it on the Apps page.", error="not integrated",
                          error_class=NOT_FOUND)
    try:
        appintents.launch_app(ref, ctx.launcher)
    except Exception as e:
        return fail(f"Couldn't start {ref.name}: {e}", TRANSIENT)
    return ok(f"Started {ref.name}.", app=ref.name)


def open_verify(a: AppArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    ref = _ref(ctx, a.name)
    if ref is None:
        return None, "app unknown"
    if _observe(ctx) is None:
        return None, "windows can't be observed on this system"
    wins = _wait_for(ctx, lambda: _find_windows(ref, ctx))
    if wins:
        return True, wins[0][0]
    stray = [w for w in (_observe(ctx) or []) if BLOCKING.search(w[0]) and (not ref.exe_name or w[1] == ref.exe_name)]
    if stray:
        res.error_class = BLOCKED
        res.data["dialog"] = stray[0][0]
        return False, f"no {ref.name} window; a dialog is in the way: “{stray[0][0]}”"
    return False, f"no {ref.name} window appeared within {ctx.verify_timeout_s:g}s"


def focus_run(a: AppArgs, ctx: ToolContext) -> ToolResult:
    ref = _ref(ctx, a.name)
    if ref is None:
        return fail(f"“{a.name}” is not one of your integrated apps.", NOT_FOUND)
    stem = ref.exe_name[:-4] if ref.exe_name.endswith(".exe") else ""
    try:
        title = ctx.system.focus([ref.name, stem])
    except SystemControlError as e:
        return fail(str(e), MISSING)
    return ok(f"Switched to {ref.name}.", app=ref.name, window=title) if title else fail(f"{ref.name} isn't open.", NOT_FOUND)


def focus_verify(a: AppArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    ref = _ref(ctx, a.name)
    try:
        title, exe = ctx.system.foreground()
    except SystemControlError:
        return None, "foreground window can't be read here"
    return (bool(ref and _matches(ref, title, exe)), title)


def close_already(a: AppArgs, ctx: ToolContext) -> ToolResult | None:
    ref = _ref(ctx, a.name)
    wins = _find_windows(ref, ctx) if ref else None
    if ref is not None and wins is not None and not wins:
        r = ok(f"{ref.name} is not running.")
        r.already_done, r.verified, r.observed = True, True, "no window"
        return r
    return None


def close_run(a: AppArgs, ctx: ToolContext) -> ToolResult:
    ref = _ref(ctx, a.name)
    if ref is None:
        return fail(f"“{a.name}” is not one of your integrated apps.", NOT_FOUND)
    if not ref.exe_name:
        return ToolResult(FAILED, f"I can't close {ref.name} by name (Store apps have no program name). Close it yourself.",
                          error="no process name", error_class=MISSING)
    try:
        done, out = ctx.system.close_process(ref.exe_name)
    except SystemControlError as e:
        return fail(str(e), MISSING)
    if not done:
        return fail(f"{ref.name} doesn't seem to be running." if "not found" in out.lower() else f"Couldn't close {ref.name}: {out}", NOT_FOUND)
    return ok(f"Asked {ref.name} to close.", app=ref.name)


def close_verify(a: AppArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    ref = _ref(ctx, a.name)
    if ref is None or _observe(ctx) is None:
        return None, "windows can't be observed here"
    gone = _wait_for(ctx, lambda: _find_windows(ref, ctx) == [])
    return (True, "window closed") if gone else (False, f"{ref.name} is still open (it may be asking to save)")


def list_windows_run(a: QueryArgs, ctx: ToolContext) -> ToolResult:
    wins = _observe(ctx)
    if wins is None:
        return ToolResult(FAILED, "Windows can't be listed on this system.", error="unobservable", error_class=MISSING)
    q = a.query.lower()
    rows = [{"title": t, "program": e} for t, e in wins if not q or q in t.lower() or q in e]
    return ok(f"{len(rows)} window(s).", windows=rows[:40], count=len(rows))


def dismiss_run(_a: NoArgs, ctx: ToolContext) -> ToolResult:
    try:
        title, exe = ctx.system.foreground()
    except SystemControlError as e:
        return fail(str(e), MISSING)
    if not BLOCKING.search(title):
        return fail(f"The front window (“{title}”) doesn't look like a dialog I should dismiss.", NOT_FOUND)
    ctx.system.send_keys("esc")
    return ok(f"Pressed Esc on “{title}”.", dismissed=title, program=exe)


def dismiss_verify(_a: NoArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    wins = _observe(ctx)
    if wins is None:
        return None, "windows can't be observed here"
    gone = res.data.get("dismissed") not in {t for t, _ in wins}
    return gone, "dialog closed" if gone else "dialog is still there"


def volume_run(a: VolumeArgs, ctx: ToolContext) -> ToolResult:
    try:
        if a.action == "set":
            ctx.system.set_volume(a.percent)
        elif a.action == "mute":
            ctx.system.key("mute")
        else:
            ctx.system.key(a.action, 5)
    except SystemControlError as e:
        return fail(str(e), MISSING)
    return ok(f"Volume {a.action}" + (f" to about {a.percent}%." if a.action == "set" else "."))


# ======================================================================= personal data
def _db_check(ctx: ToolContext, model: Any, rid: int, pred: Any = None) -> tuple[bool | None, str]:
    with ctx.db.session() as s:
        row = s.get(model, rid)
        good = row is not None and (pred is None or pred(row))
    return good, "saved" if good else "not found in the database after saving"


def reminder_due(a: ReminderArgs, ctx: ToolContext) -> tuple[dt.datetime, str] | None:
    w = parse_when(a.when, ctx.now())
    return (w.due, w.repeat) if w else None


def reminder_already(a: ReminderArgs, ctx: ToolContext) -> ToolResult | None:
    due = reminder_due(a, ctx)
    if due is None:
        return None
    with ctx.db.session() as s:
        for r in s.query(Reminder).filter(Reminder.status == "pending").all():
            if r.text.strip().lower() == a.text.strip().lower() and abs((r.due_at - due[0]).total_seconds()) <= 90:
                res = ok(f"That reminder already exists (#{r.id}).", id=r.id)
                res.already_done, res.verified, res.observed = True, True, f"reminder #{r.id}"
                return res
    return None


def reminder_run(a: ReminderArgs, ctx: ToolContext) -> ToolResult:
    due = reminder_due(a, ctx)
    if due is None:
        return fail("I couldn't understand that time. Try 'in 10 minutes' or 'tomorrow at 9am'.", INVALID)
    with ctx.db.session() as s:
        r = Reminder(kind="reminder", text=a.text.strip(), due_at=due[0], repeat=due[1], repeat_daily=due[1] == "daily")
        s.add(r)
        s.flush()
        rid = r.id
    return ok(f"Reminder saved: “{a.text}” {describe(due[0], due[1], ctx.now())}.", id=rid, due=due[0].isoformat())


def note_already(a: NoteArgs, ctx: ToolContext) -> ToolResult | None:
    with ctx.db.session() as s:
        n = s.query(Note).filter(Note.body == a.body.strip()).first()
        if n:
            res = ok(f"That note already exists (#{n.id}).", id=n.id)
            res.already_done, res.verified, res.observed = True, True, f"note #{n.id}"
            return res
    return None


def note_run(a: NoteArgs, ctx: ToolContext) -> ToolResult:
    with ctx.db.session() as s:
        n = Note(title=(a.title or a.body)[:60], body=a.body.strip())
        s.add(n)
        s.flush()
        nid = n.id
    return ok("Note saved.", id=nid)


def todo_already(a: TodoArgs, ctx: ToolContext) -> ToolResult | None:
    with ctx.db.session() as s:
        t = s.query(Todo).filter(Todo.text == a.text.strip(), Todo.done.is_(False)).first()
        if t:
            res = ok(f"That to-do is already on your list (#{t.id}).", id=t.id)
            res.already_done, res.verified, res.observed = True, True, f"to-do #{t.id}"
            return res
    return None


def todo_run(a: TodoArgs, ctx: ToolContext) -> ToolResult:
    with ctx.db.session() as s:
        t = Todo(text=a.text.strip())
        s.add(t)
        s.flush()
        tid = t.id
    return ok(f"Added to your to-do list: {a.text}", id=tid)


def list_reminders_run(_a: NoArgs, ctx: ToolContext) -> ToolResult:
    with ctx.db.session() as s:
        rows = [{"id": r.id, "text": r.text, "due": r.due_at.isoformat(), "repeat": r.repeat}
                for r in s.query(Reminder).filter(Reminder.status == "pending").order_by(Reminder.due_at).limit(50)]
    return ok(f"{len(rows)} pending reminder(s).", reminders=rows, count=len(rows))


def search_notes_run(a: QueryArgs, ctx: ToolContext) -> ToolResult:
    with ctx.db.session() as s:
        q = s.query(Note)
        if a.query:
            q = q.filter(Note.body.ilike(f"%{a.query}%") | Note.title.ilike(f"%{a.query}%"))
        rows = [{"id": n.id, "title": n.title, "body": n.body[:300]} for n in q.order_by(Note.id.desc()).limit(20)]
    return ok(f"{len(rows)} note(s).", notes=rows, count=len(rows))


# ======================================================================= messages, email, calendar, web
def _open_plan(plan: skills.Plan, ctx: ToolContext) -> ToolResult | None:
    try:
        skills.Runner(ctx.launcher, ctx.system, ctx.sleep).run_steps(plan, ctx.prefs())
    except Exception as e:
        return fail(f"Couldn't open it: {e}", TRANSIENT)
    return None


def message_run(a: MessageArgs, ctx: ToolContext) -> ToolResult:
    sc = _skill_ctx(ctx)
    plan = skills.plan_message(sc, a.person, a.text, a.channel)
    if isinstance(plan, skills.Clarify):
        return ToolResult(FAILED, plan.message, error=plan.message, error_class=NOT_FOUND)
    if plan.ask:
        return ToolResult(FAILED, plan.ask, error=plan.ask, error_class=AMBIGUOUS, data={"question": plan.ask})
    err = _open_plan(plan, ctx)
    if err:
        return err
    target = "teams" if "teams" in plan.reply.lower() else "whatsapp"
    return ok(plan.reply, recipient=plan.summary.removeprefix("Message ").strip(), target=target, can_send=plan.send is not None,
              preview=a.text[:140])


def message_verify(a: MessageArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    if not res.data.get("can_send"):
        return None, "opened in the browser; can't observe it"
    needle = "teams" if res.data.get("target") == "teams" else "whatsapp"
    if _observe(ctx) is None:
        return None, "windows can't be observed here"
    wins = _wait_for(ctx, lambda: [w for w in (_observe(ctx) or []) if needle in w[0].lower()])
    return (True, wins[0][0]) if wins else (False, f"no {needle} window appeared")


SEND = {"whatsapp": (["WhatsApp"], "enter"), "teams": (["Teams"], "enter"),
        "gmail": (["Gmail", "Compose", "New Message", "Mail"], "ctrl+enter")}


def send_run(a: SendArgs, ctx: ToolContext) -> ToolResult:
    focus, keys = SEND[a.target]
    runner = skills.Runner(ctx.launcher, ctx.system, ctx.sleep)
    done, msg = runner.press_send(skills.SendStep(f"{a.target} message", a.preview, focus, keys), ctx.prefs())
    if not done:
        return ToolResult(FAILED, msg, error=msg, error_class=BLOCKED)
    return ok(msg, target=a.target)


def email_run(a: EmailArgs, ctx: ToolContext) -> ToolResult:
    sc = _skill_ctx(ctx)
    to, name = a.to.strip(), ""
    if a.person and not to:
        hit = find_contact(sc.contacts, a.person)
        if hit is None:
            return fail(f"I don't have anyone called “{a.person}” in your people list.", NOT_FOUND)
        c, exact = hit
        if not c.email:
            return fail(f"I don't have an email address for {c.name}.", NOT_FOUND)
        if not exact:
            q = f"Did you mean {c.name}?"
            return ToolResult(FAILED, q, error=q, error_class=AMBIGUOUS, data={"question": q})
        to, name = c.email, c.name
    plan = skills.plan_email(sc, to, name, subject=a.subject, body=a.body)
    if isinstance(plan, skills.Clarify):
        return fail(plan.message, INVALID)
    err = _open_plan(plan, ctx)
    return err or ok(plan.reply, to=to, can_send=plan.send is not None)


def calendar_run(a: CalendarArgs, ctx: ToolContext) -> ToolResult:
    plan = skills.plan_calendar(_skill_ctx(ctx), a.title, a.when, a.minutes)
    if isinstance(plan, skills.Clarify):
        return fail(plan.message, INVALID)
    return _open_plan(plan, ctx) or ok(plan.reply)


def web_available(ctx: ToolContext) -> tuple[bool, str]:
    return True, ""


def url_allowed(prefs: Any, url: str) -> str | None:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return "Only https:// addresses can be opened."
    if prefs.allowed_url_domains and not any(u.hostname == d or u.hostname.endswith("." + d) for d in prefs.allowed_url_domains):
        return f"{u.hostname} is not in your allowed web domains (Settings)."
    return None


def web_open_run(a: UrlArgs, ctx: ToolContext) -> ToolResult:
    why = url_allowed(ctx.prefs(), a.url)
    if why:
        return ToolResult(FAILED, why, error=why, error_class=PERMISSION)
    try:
        ctx.launcher(a.url, "url")
    except Exception as e:
        return fail(f"Couldn't open the browser: {e}", TRANSIENT)
    return ok(f"Opened {urlparse(a.url).hostname} in your browser.")


def web_search_run(a: QueryArgs, ctx: ToolContext) -> ToolResult:
    if not a.query.strip():
        return fail("What should I search for?", INVALID)
    try:
        ctx.launcher(appintents._search_url(ctx.prefs(), a.query), "url")
    except Exception as e:
        return fail(f"Couldn't open the browser: {e}", TRANSIENT)
    return ok(f"Searching the web for “{a.query}”.")


# ======================================================================= files in approved folders
CATEGORIES = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".heic"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"},
    "Music": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "Video": {".mp4", ".mkv", ".avi", ".mov", ".webm"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Installers": {".exe", ".msi"},
}


def files_list_run(a: FolderArgs, ctx: ToolContext) -> ToolResult:
    root = _approved_folder(ctx, a.folder)
    if root is None:
        return _no_folder(a.folder)
    names = sorted(p.name for p in root.iterdir() if (not a.pattern or a.pattern.lower() in p.name.lower()))
    return ok(f"{len(names)} item(s) in {root.name}.", folder=str(root), names=names[: a.limit], count=len(names))


def files_read_run(a: ReadArgs, ctx: ToolContext) -> ToolResult:
    for f in ctx.prefs().approved_folders:
        root = Path(f)
        target = _inside(root, a.path) if not Path(a.path).is_absolute() else Path(a.path).resolve()
        if target is None:
            continue
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")[: a.max_chars]
        except OSError as e:
            return fail(f"Couldn't read it: {e}", TRANSIENT)
        return ok(f"Read {target.name} ({len(text)} characters).", path=str(target), text=text, untrusted=True)
    return ToolResult(FAILED, "That file isn't inside an approved folder (or doesn't exist).", error="not approved", error_class=PERMISSION)


def organize_run(a: OrganizeArgs, ctx: ToolContext) -> ToolResult:
    root = _approved_folder(ctx, a.folder)
    if root is None:
        return _no_folder(a.folder)
    moves, skipped = [], []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        cat = next((c for c, ext in CATEGORIES.items() if p.suffix.lower() in ext), None)
        if cat is None:
            skipped.append(p.name)
        elif (root / cat / p.name).exists():
            skipped.append(f"{p.name} (already exists in {cat})")
        else:
            moves.append({"src": p.name, "dst": f"{cat}/{p.name}"})
    return ok(f"Proposed {len(moves)} move(s) in {root.name}; {len(skipped)} left alone. Nothing has been moved.",
              folder=str(root), moves=moves[:500], skipped=skipped[:100], count=len(moves))


def _resolved_moves(root: Path, moves: list[Move]) -> tuple[list[tuple[Path, Path, Move]], str]:
    out = []
    for m in moves:
        src, dst = _inside(root, m.src), _inside(root, m.dst)
        if src is None or dst is None:
            return [], f"“{m.src}” → “{m.dst}” leaves the folder."
        out.append((src, dst, m))
    return out, ""


def apply_already(a: ApplyMovesArgs, ctx: ToolContext) -> ToolResult | None:
    root = _approved_folder(ctx, a.folder)
    if root is None:
        return None
    pairs, err = _resolved_moves(root, a.moves)
    if err or not pairs:
        return None
    if all(d.exists() and not s.exists() for s, d, _m in pairs):
        r = ok("Those files are already in place.", moved=len(pairs))
        r.already_done, r.verified, r.observed = True, True, f"{len(pairs)} file(s) at their destination"
        return r
    return None


def apply_run(a: ApplyMovesArgs, ctx: ToolContext) -> ToolResult:
    root = _approved_folder(ctx, a.folder)
    if root is None:
        return _no_folder(a.folder)
    pairs, err = _resolved_moves(root, a.moves)
    if err:
        return ToolResult(FAILED, err, error=err, error_class=PERMISSION)
    done: list[dict[str, str]] = []
    for src, dst, m in pairs:
        if not src.exists():
            continue  # a half-finished earlier attempt: skip what is already moved, never overwrite anything
        if dst.exists():
            return ToolResult(FAILED, f"“{m.dst}” already exists; I won't overwrite it.", error="exists", error_class=INVALID,
                              data={"moved": done})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        done.append({"src": m.src, "dst": m.dst})
    return ok(f"Moved {len(done)} file(s) in {root.name}.", moved=done, undo=[{"src": d["dst"], "dst": d["src"]} for d in done])


def apply_verify(a: ApplyMovesArgs, res: ToolResult, ctx: ToolContext) -> tuple[bool | None, str]:
    root = _approved_folder(ctx, a.folder)
    if root is None:
        return None, "folder not found"
    pairs, _ = _resolved_moves(root, a.moves)
    bad = [m.dst for s, d, m in pairs if not d.exists() or s.exists()]
    return (not bad), (f"{len(pairs)} file(s) are where they should be" if not bad else f"not in place: {', '.join(bad[:5])}")


# ======================================================================= projects (existing workspace toolbox)
def _toolbox(ctx: ToolContext) -> Any:
    from app.agent.tools import Toolbox

    return Toolbox(ctx.db, ctx.commands, ctx.workspace_id or 0, ctx.settings.max_file_bytes)


def _workspace_available(ctx: ToolContext) -> tuple[bool, str]:
    return (True, "") if ctx.workspace_id else (False, "Open a project first (Projects page).")


def _via_toolbox(name: str) -> Any:
    def run(a: BaseModel, ctx: ToolContext) -> ToolResult:
        out = _toolbox(ctx).call(name, a.model_dump())
        if out.status == "awaiting_permission":
            return ToolResult(AWAITING, out.summary, data={"note": out.content}, ref=dict(out.ref))
        if out.status == "failed":
            return fail(out.summary or out.content, NOT_FOUND if "not found" in out.content.lower() else PERMISSION
                        if "denied" in out.content.lower() else INVALID, detail=out.content)
        return ok(out.summary, content=out.content, untrusted=True, **out.ref)
    return run


def edit_poll(ref: dict[str, Any], ctx: ToolContext) -> ToolResult:
    cs_id = ref.get("changeset_id")
    with ctx.db.session() as s:
        cs = s.get(ChangeSet, cs_id) if cs_id else None
        if cs is None:
            return fail("The proposed change no longer exists.", NOT_FOUND)
        if cs.status == "proposed":
            return ToolResult(AWAITING, f"Change set #{cs.id} is waiting for your review.", ref=ref)
        if cs.status in ("applied", "partially_applied"):
            root = Path(s.get(Workspace, cs.workspace_id).path)  # type: ignore[union-attr]
            bad = [f.rel_path for f in cs.files if f.status == "applied" and f.new_content is not None
                   and (not (root / f.rel_path).is_file() or (root / f.rel_path).read_text(encoding="utf-8", errors="replace") != f.new_content)]
            res = ok(f"Change set #{cs.id} was applied.", changeset_id=cs.id, files=[f.rel_path for f in cs.files if f.status == "applied"])
            res.verified, res.observed = (not bad), ("files on disk match the proposal" if not bad else f"differs on disk: {', '.join(bad)}")
            if bad:
                res.status, res.error_class, res.error = FAILED, "verify_failed", res.observed
            return res
        return ToolResult(FAILED, f"Change set #{cs.id} was {cs.status}; nothing was applied.", error=cs.status, error_class=PERMISSION)


def command_poll(ref: dict[str, Any], ctx: ToolContext) -> ToolResult:
    with ctx.db.session() as s:
        run = s.get(CommandRun, ref.get("command_id"))
        if run is None:
            return fail("The command no longer exists.", NOT_FOUND)
        if run.status in ("pending_approval", "running"):
            return ToolResult(AWAITING, f"Command #{run.id} is {run.status.replace('_', ' ')}.", ref=ref)
        tail = (run.stdout or "")[-1500:] + (("\n" + run.stderr[-800:]) if run.stderr else "")
        if run.status == "succeeded":
            res = ok(f"Command #{run.id} succeeded (exit code {run.exit_code}).", output=tail, exit_code=run.exit_code)
            res.verified, res.observed = True, f"exit code {run.exit_code}"
            return res
        return ToolResult(FAILED, f"Command #{run.id} {run.status} (exit code {run.exit_code}).", data={"output": tail},
                          error=run.status, error_class=PERMISSION if run.status in ("rejected", "cancelled") else INVALID)


# ======================================================================= registry
def build_registry(ctx: ToolContext) -> Registry:
    r = Registry(ctx)
    add = r.register
    add(Tool("app.open", "Open an integrated desktop app and confirm its window appears.", AppArgs, open_run, risk="low", needs=("apps",),
             tags=("open", "launch", "start", "run", "app", "program", "spotify", "chrome", "notepad"), timeout_s=15, retries=1,
             idempotent=True, already_done=open_already, verify=open_verify, recovery={BLOCKED: "window.dismiss"},
             available=_apps_available, limits="Needs Windows to confirm the window; elsewhere the launch can't be verified."))
    add(Tool("app.focus", "Bring an open app to the front.", AppArgs, focus_run, risk="low", needs=("apps", "windows"),
             tags=("switch", "focus", "front", "show", "app"), idempotent=True, verify=focus_verify, available=_apps_available))
    add(Tool("app.close", "Ask an app to close (it may offer to save first) and confirm its window is gone.", AppArgs, close_run,
             risk="medium", needs=("apps", "windows"), tags=("close", "quit", "exit", "stop", "app"), idempotent=True,
             already_done=close_already, verify=close_verify, available=_apps_available, limits="Store apps can't be closed by name."))
    add(Tool("window.list", "List the windows currently open (to see what state the desktop is in).", QueryArgs, list_windows_run,
             risk="read", needs=("windows",), tags=("window", "windows", "desktop", "state", "check", "running", "open"),
             available=_windows_available))
    add(Tool("window.dismiss", "Press Esc on a front window that is a blocking dialog (update, error, sign-in...).", NoArgs,
             dismiss_run, risk="medium", needs=("windows",), tags=("dialog", "dismiss", "popup", "update", "error"),
             verify=dismiss_verify, available=_windows_available, limits="Only acts on windows whose title looks like a dialog."))
    add(Tool("system.volume", "Change the speaker volume.", VolumeArgs, volume_run, risk="low", tags=("volume", "sound", "mute", "louder", "quieter"),
             limits="Cannot read the current volume, so the result isn't verified."))
    add(Tool("reminder.create", "Save a reminder or alarm for a time (in words).", ReminderArgs, reminder_run, risk="low",
             tags=("remind", "reminder", "alarm", "later", "tomorrow", "schedule", "alert"), idempotent=True, already_done=reminder_already,
             verify=lambda a, res, ctx: _db_check(ctx, Reminder, res.data["id"], lambda r: r.status == "pending")))
    add(Tool("reminder.list", "List pending reminders.", NoArgs, list_reminders_run, risk="read", tags=("reminders", "upcoming", "list", "schedule")))
    add(Tool("note.create", "Save a note.", NoteArgs, note_run, risk="low", tags=("note", "write", "remember", "save", "jot"),
             idempotent=True, already_done=note_already, verify=lambda a, res, ctx: _db_check(ctx, Note, res.data["id"])))
    add(Tool("note.search", "Search saved notes.", QueryArgs, search_notes_run, risk="read", tags=("note", "notes", "find", "search", "recall")))
    add(Tool("todo.add", "Add an item to the to-do list.", TodoArgs, todo_run, risk="low", tags=("todo", "task", "list", "add"),
             idempotent=True, already_done=todo_already, verify=lambda a, res, ctx: _db_check(ctx, Todo, res.data["id"])))
    add(Tool("message.prepare", "Open a WhatsApp/Teams chat to a known person with the message filled in (does NOT send).", MessageArgs,
             message_run, risk="low", needs=("contacts", "apps"), tags=("message", "text", "whatsapp", "teams", "tell", "chat", "send"),
             verify=message_verify, limits="Person must be on the People page. Can't read replies."))
    add(Tool("message.send", "Press Send in the chat/compose window that was prepared. Always needs approval.", SendArgs, send_run,
             risk="high", needs=("windows",), tags=("send", "message", "email", "submit"),
             limits="Only presses Send if the right window is in front. Delivery can't be observed."))
    add(Tool("email.draft", "Open a Gmail (or mail app) draft to a known person or address.", EmailArgs, email_run, risk="low",
             needs=("contacts",), tags=("email", "mail", "gmail", "write", "draft", "compose"), limits="Opens a draft; can't read mail."))
    add(Tool("calendar.event", "Open Google Calendar with a new event filled in (you click Save).", CalendarArgs, calendar_run, risk="low",
             tags=("calendar", "event", "meeting", "appointment", "schedule", "book"), limits="You confirm the event in Calendar."))
    add(Tool("web.open", "Open an https address in the browser (domain allow-list applies).", UrlArgs, web_open_run, risk="low",
             needs=("network",), tags=("open", "website", "url", "page", "browse", "link")))
    add(Tool("web.search", "Search the web in the browser.", QueryArgs, web_search_run, risk="low", needs=("network",),
             tags=("search", "google", "web", "look", "find", "lookup")))
    add(Tool("files.list", "List the names in an approved folder.", FolderArgs, files_list_run, risk="read", needs=("files",),
             tags=("files", "folder", "list", "directory", "downloads", "documents", "desktop", "contents")))
    add(Tool("files.read", "Read a text file inside an approved folder.", ReadArgs, files_read_run, risk="read", needs=("files",),
             tags=("read", "file", "open", "contents", "document", "report", "extract")))
    add(Tool("files.plan_organize", "Preview how the files in an approved folder would be sorted into folders by type. Moves nothing.",
             OrganizeArgs, organize_run, risk="read", needs=("files",), tags=("organize", "organise", "sort", "tidy", "clean", "folder", "files", "type")))
    add(Tool("files.apply_moves", "Move files within one approved folder (never overwrites). Always needs approval.", ApplyMovesArgs, apply_run,
             risk="high", needs=("files",), tags=("move", "organize", "sort", "files", "folder"), idempotent=True, already_done=apply_already,
             verify=apply_verify, limits="Only inside the approved folder; returns an undo list."))
    for name, desc, model, risk in (("list_files", "List project files under a folder.", ProjectListArgs, "read"),
                                    ("search_code", "Search the project for text.", ProjectSearchArgs, "read"),
                                    ("read_file", "Read a project file with line numbers.", ProjectReadArgs, "read"),
                                    ("git_status", "Show the git branch and changed files.", NoArgs, "read"),
                                    ("git_diff", "Show uncommitted changes.", NoArgs, "read")):
        add(Tool(f"project.{name}", desc, model, _via_toolbox(name), risk=risk, needs=("workspace",), available=_workspace_available,
                 tags=("project", "code", "file", "search", "read", "git", "diff", "bug", "repository", name.replace("_", " "))))
    add(Tool("project.propose_edit", "Propose a whole-file change in the project. It is applied only after the user reviews the diff.",
             ProposeEditArgs, _via_toolbox("propose_edit"), risk="high", needs=("workspace",), available=_workspace_available, self_gated=True,
             poll=edit_poll, tags=("edit", "change", "fix", "modify", "write", "code", "implement", "patch")))
    add(Tool("project.propose_command", "Ask to run a command (tests, linter) in the project. It runs only after approval.", ProposeCommandArgs,
             _via_toolbox("propose_command"), risk="high", needs=("workspace",), available=_workspace_available, self_gated=True,
             poll=command_poll, tags=("run", "test", "tests", "command", "pytest", "lint", "build", "verify")))
    add(Tool("agent.diagnose", "Check which tools are available right now (changes nothing).", NoArgs,
             lambda a, c: ok("Checked.", tools=r.diagnose()), risk="read", tags=("diagnose", "health", "available", "capabilities", "status")))
    return r
