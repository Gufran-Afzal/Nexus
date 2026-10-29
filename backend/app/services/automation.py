"""Connects the conversation to the automation runtime: decides whether a request is a job for the runtime, gets a plan
(deterministic first, the model second), runs it, and turns the run into a reply plus a pending yes/no when it needs one."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agent import plan as plans
from app.agent import planner
from app.agent.builtin_tools import build_registry
from app.agent.capabilities import ToolContext
from app.agent.runtime import TERMINAL, AgentRuntime
from app.core.events import EventBus
from app.database.db import Database
from app.providers.base import ChatMessage, GenerationOptions
from app.services import actions, quick, skills
from app.services.assistant import Assistant, Resolution
from app.services.llm import LLMService
from app.services.preferences import Preferences, model_for

FAST_KINDS = {"open_app": "app.open", "close_app": "app.close", "focus_app": "app.focus"}
_QUESTION = re.compile(r"^(?:what|why|how|who|when|where|which|is|are|do|does|did|can you explain|explain|tell me|define|compare|"
                       r"thank|thanks|hello|hi|hey)\b", re.I)
_CUE = re.compile(r"\b(open|close|quit|launch|start|organi[sz]e|sort|tidy|move|rename|find|search|read|list|show|check|fix|edit|change|"
                  r"run|test|send|remind|message|email|write|add|save|create|make|turn|set|switch|focus|clean|summari[sz]e|extract|"
                  r"download|look up|volume|mute|note|todo)\b", re.I)


_WORD = re.compile(r"[a-z0-9]+")
_GENERIC = {"a", "an", "the", "it", "me", "my", "to", "and", "of", "in", "on", "for", "this", "that", "please", "now", "then", "make",
            "set", "run", "start", "show", "list", "check", "find", "read", "write", "add", "save", "create", "change", "turn"}


def worth_planning(text: str, vocabulary: set[str] | None = None) -> bool:
    """Is this worth a (slow) planner call? It must sound like an action AND mention something a tool deals with, so ordinary
    chat ("make it shorter") never pays for planning."""
    n = len(text.split())
    if not (3 <= n <= 60 and _CUE.search(text)) or _QUESTION.match(text.strip()):
        return False
    if vocabulary is None:
        return True
    words = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD.findall(text.lower())} - _GENERIC
    return bool(words & vocabulary)


class AutomationService:
    def __init__(self, db: Database, settings: Any, llm: LLMService, assistant: Assistant, launcher: actions.Launcher,
                 commands: Any, bus: EventBus | None = None) -> None:
        self.db, self.llm, self.assistant = db, llm, assistant
        ctx = ToolContext(db=db, settings=settings, system=actions.DEFAULT_SYSTEM, launcher=launcher, commands=commands, bus=bus)
        self.registry = build_registry(ctx)
        self.runtime = AgentRuntime(db, self.registry, bus)
        self.runtime.replanner = self._replan
        self.prefs: Preferences | None = None
        self.last_kind = ""  # what the planner decided for the last request (so a plain "answer" skips the older router)

    def _vocabulary(self, prefs: Preferences) -> set[str]:
        """Words that tie a request to something NEXUS can act on: tool names and purposes, and the user's own apps and folders."""
        words: set[str] = set()
        for t in self.registry.tools.values():
            for w in [*t.name.replace(".", " ").split(), *t.tags]:
                words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
        for a in prefs.integrated_apps:
            words.update(_WORD.findall(a.name.lower()))
        for f in prefs.approved_folders:
            words.update(_WORD.findall(f.lower().rsplit("\\", 1)[-1].rsplit("/", 1)[-1]))
        return words - _GENERIC

    # ---------------------------------------------------------------- the model as planner
    def _complete(self, prefs: Preferences) -> Any:
        model = model_for(prefs, "fast")
        timeout = prefs.agent.planner_timeout_s

        async def complete(prompt: str) -> str:
            return await asyncio.wait_for(self.llm.complete(
                model, [ChatMessage("user", prompt)],
                GenerationOptions(temperature=0.0, max_output_tokens=500, response_format="json"), purpose="planner"), timeout=timeout)

        return complete

    async def _replan(self, goal: str, what: str, next_n: int) -> plans.Plan | None:
        prefs = self.prefs or Preferences()
        known = {f"s{i}" for i in range(1, next_n)}
        prop = await planner.plan_with_llm(
            f"{goal}\nSo far: {what}. Plan ONLY the remaining work, as a different way to reach the goal; "
            f"number new steps from s{next_n}.", self.registry, self._complete(prefs), max_steps=4, known=known, max_calls=1)
        return prop.plan if prop.kind == "plan" else None

    # ---------------------------------------------------------------- entry point from a conversation turn
    async def handle(self, text: str, intent: actions.Intent | None, prefs: Preferences, *, source: str, conversation_id: int,
                     workspace_id: int | None, coding: bool) -> Resolution | None:
        """Returns a reply when the runtime took the request; None to let the old paths / the chat model answer."""
        self.last_kind = ""
        if not prefs.agent.enabled:
            return None
        self.prefs = prefs
        self.registry.ctx.workspace_id = workspace_id
        proposal: planner.Proposal | None = None
        if intent is not None:
            tool = FAST_KINDS.get(intent.kind)
            if tool is None:
                return None
            proposal = planner.Proposal("plan", plans.Plan([plans.PlanStep("s1", tool, {"name": intent.params["name"]})]), source="deterministic")
        elif coding:
            return None
        else:
            last = await asyncio.to_thread(self.runtime.last_app, conversation_id)
            saved = await asyncio.to_thread(self.runtime.saved_workflows)
            proposal = planner.interpret(text, last, saved)
            if proposal is None:
                if not worth_planning(text, self._vocabulary(prefs)):
                    return None
                lessons = await asyncio.to_thread(self.runtime.lessons_for, text)
                history = await asyncio.to_thread(self.runtime.history, conversation_id)
                with self.db.session() as s:
                    memories = quick.load_memories(s)
                import datetime as dt

                proposal = await planner.plan_with_llm(text, self.registry, self._complete(prefs), memories=memories, lessons=lessons,
                                                       history=history, max_steps=prefs.agent.max_steps, now=f"{dt.datetime.now():%A %d %B %Y %H:%M}",
                                                       max_calls=min(2, prefs.agent.max_llm_calls))
        self.last_kind = proposal.kind
        if proposal.kind == "plan" and proposal.plan is not None:
            rid = self.runtime.create(text, proposal.plan, source=source, conversation_id=conversation_id, workspace_id=workspace_id,
                                      llm_calls=proposal.llm_calls)
            if proposal.plan.workflow:
                await asyncio.to_thread(self.runtime.note_workflow_use, proposal.plan.workflow)
            return self._resolution(await self.runtime.execute(rid))
        if proposal.kind == "clarify":
            return Resolution(proposal.message, "agent", "succeeded")
        if proposal.kind == "impossible":
            return Resolution(proposal.message, "agent", "unsupported")
        return None  # "answer" or "none": normal chat (or the old router) takes over

    # ---------------------------------------------------------------- run -> reply
    def _resolution(self, view: dict[str, Any]) -> Resolution:
        status, pend = view["status"], view.get("pending")
        if status == "waiting_for_approval" and pend and pend.get("kind") == "approval":
            done = [s["summary"] for s in view["steps"] if s["status"] == "succeeded" and s["summary"]]
            question = f"I'm ready to {pend['prompt'][:1].lower() + pend['prompt'][1:]}. Should I go ahead?"
            msg = " ".join([*done, question])
            self.assistant.store.set(skills.Pending("run", pend["prompt"][:80], pend["prompt"], question=question, run_id=view["id"], ttl_s=900))
            self.assistant._publish()
            return Resolution(msg + " Say yes or no.", "agent", "succeeded", pending=True)
        if status == "waiting_for_approval" and pend and pend.get("kind") == "external":
            return Resolution(f"{pend['prompt']} Nothing is applied until you approve it there.", "agent", "succeeded")
        if status == "waiting_for_input" and pend:
            return Resolution(str(pend.get("prompt") or "I need an answer to continue."), "agent", "succeeded")
        if status in ("completed", "cancelled"):
            kind = "succeeded"
        else:  # "I can't do that here" (unknown app, folder not approved...) is a refusal, not a malfunction
            classes = {s["error_class"] for s in view["steps"] if s["status"] in ("failed", "denied")}
            kind = "unsupported" if classes and classes <= {"not_found", "permission", "unavailable", "invalid", "ambiguous"} else "failed"
        return Resolution(view["summary"] or "Done.", "agent", kind)

    async def decide(self, run_id: int, yes: bool) -> Resolution:
        """The user said yes or no to a run's approval question (typed, spoken or from the Automation page)."""
        view = await self.runtime.approve(run_id, yes)
        return self._resolution(view)

    def finished(self, view: dict[str, Any]) -> bool:
        return view["status"] in TERMINAL
