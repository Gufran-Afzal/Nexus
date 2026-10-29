"""Understanding free-form commands that no fixed phrase matched (“ping my mum that dinner is at seven”).

The model only *chooses* one skill from a short allowlist and fills in its arguments as JSON; the arguments are validated and then
go through the same plan builders and the same confirmations as a fixed phrase. The model never gets to run anything itself."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.services import skills

log = logging.getLogger(__name__)

CompleteFn = Callable[[str], Awaitable[str]]  # system+user prompt in, raw JSON text out

_CUE = re.compile(r"\b(message|text|whatsapp|ping|tell|email|mail|gmail|send|write|schedule|calendar|meeting|appointment|book|"
                  r"directions?|navigate|map|youtube|play|let .* know|ask|remind)\b", re.I)
_QUESTION = re.compile(r"^(?:what|why|how|who|when|where|which|is|are|do|does|did|"
                       r"can you explain|explain|tell me|define|compare)\b",
                       re.I)

TOOLS = {
    "message": "send a chat message. args: person (name from the people list), text (what to say, first person, as "
               "the user would write it), app (whatsapp or teams, optional)",
    "email": "write an email. args: person (name from the people list, optional), address (optional), "
             "about (what the email should say)",
    "calendar": "add an event to Google Calendar. args: title, when (natural words, e.g. 'tomorrow at 3pm')",
    "maps": "directions or a place search. args: query, directions (true/false)",
    "youtube": "search YouTube. args: query",
    "none": "none of these (a question, chat, or something else)",
}


def worth_trying(text: str) -> bool:
    n = len(text.split())
    return 3 <= n <= 40 and bool(_CUE.search(text)) and not _QUESTION.match(text.strip())


def build_prompt(text: str, people: list[str], now: str) -> str:
    tools = "\n".join(f"- {k}: {v}" for k, v in TOOLS.items())
    return ("You turn a spoken request into ONE tool call for a desktop assistant. Reply with JSON only, like "
            '{"tool": "message", "args": {"person": "Mom", "text": "Running late"}}.\n'
            f"Tools:\n{tools}\nPeople the user knows: {', '.join(people[:40]) or '(none)'}\nNow: {now}\n"
            "Use only names from the people list. If the request is not clearly one of the tools, "
            'answer {"tool": "none", "args": {}}.\n'
            f"Request: {text}")


def _s(v: Any, n: int = 600) -> str:
    return v.strip()[:n] if isinstance(v, str) else ""


def to_plan(call: dict[str, Any], ctx: skills.SkillContext) -> skills.Plan | skills.Clarify | None:
    """Validate the model's answer and turn it into a plan. Anything unexpected returns None (the model then just chats)."""
    tool, args = call.get("tool"), call.get("args")
    if tool not in TOOLS or tool == "none" or not isinstance(args, dict):
        return None
    if tool == "message":
        who, text = _s(args.get("person"), 80), _s(args.get("text"))
        app = _s(args.get("app"), 20).lower()
        return skills.plan_message(ctx, who, text, app if app in ("whatsapp", "teams") else "") if who else None
    if tool == "email":
        from app.services.contacts import find_contact, spoken_email

        about = _s(args.get("about"))
        name, to = "", spoken_email(_s(args.get("address"), 120)) or ""
        who = _s(args.get("person"), 80)
        ask = None
        if who and not to:
            hit = find_contact(ctx.contacts, who)
            if hit is None:
                return skills.Clarify(f"I don't have anyone called “{who}” in your people list.")
            if not hit[0].email:
                return skills.Clarify(f"I don't have an email address for {hit[0].name}.")
            name, to = hit[0].name, hit[0].email
            ask = None if hit[1] else f"Did you mean {name}?"
        plan = skills.plan_email(ctx, to, name, instruction=about)
        if isinstance(plan, skills.Plan):
            plan.ask = ask
        return plan
    if tool == "calendar":
        return skills.plan_calendar(ctx, _s(args.get("title"), 120), _s(args.get("when"), 120))
    if tool == "maps":
        q = _s(args.get("query"), 160)
        return skills.plan_maps(q, bool(args.get("directions"))) if q else None
    if tool == "youtube":
        q = _s(args.get("query"), 160)
        return skills.plan_youtube(q) if q else None
    return None


async def route(text: str, ctx: skills.SkillContext, complete: CompleteFn,
                timeout: float = 25.0) -> skills.Plan | skills.Clarify | None:
    if not ctx.prefs.smart_routing or not worth_trying(text):
        return None
    prompt = build_prompt(text, [c.name for c in ctx.contacts], ctx.now.strftime("%A %Y-%m-%d %H:%M"))
    try:
        raw = await asyncio.wait_for(complete(prompt), timeout=timeout)
        call = json.loads(raw)
    except (TimeoutError, json.JSONDecodeError, Exception) as e:  # model offline, slow or not JSON: fall back to chat
        log.info("router skipped: %s", e.__class__.__name__)
        return None
    return to_plan(call, ctx) if isinstance(call, dict) else None
