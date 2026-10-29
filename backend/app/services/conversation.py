"""One code path for answering a user turn (typed or spoken): actions first, then the model."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.loop import run_agent
from app.agent.tools import Toolbox
from app.core.config import Settings
from app.database.db import Database
from app.database.models import Conversation, Message, PerfSample, Workspace, utcnow
from app.providers.base import ChatMessage, GenerationOptions, ModelNotFound, ProviderError, ProviderUnavailable, StreamChunk
from app.retrieval.context import Hit
from app.retrieval.semantic import SemanticIndexer
from app.services import actions, appcontrol, quick
from app.services.assistant import Assistant, Resolution
from app.services.automation import AutomationService
from app.services.chat import build_prompt, title_from
from app.services.llm import GenerationStopped, LLMService
from app.services.preferences import load_preferences, model_for
from app.tools.command_service import CommandManager

log = logging.getLogger(__name__)


@dataclass
class TurnRequest:
    conversation_id: int
    content: str
    model: str | None = None
    files: list[str] = field(default_factory=list)
    auto_context: bool = True
    source: str = "text"  # text | voice
    allow_actions: bool = True
    agent: bool = True  # coding mode with a project: let the model use read-only tools + propose actions
    confidence: float | None = None  # how sure speech recognition was (0..1); None for typed text


class ConversationService:
    def __init__(self, settings: Settings, db: Database, llm: LLMService,
                 launcher: actions.Launcher = actions.default_launcher, commands: CommandManager | None = None) -> None:
        self.settings, self.db, self.llm, self.launcher, self.commands = settings, db, llm, launcher, commands
        self.indexer: SemanticIndexer | None = None
        self.assistant = Assistant(db, launcher)
        self.assistant.drafter = self.draft_email
        self.automation = AutomationService(db, settings, llm, self.assistant, launcher, commands)
        self.assistant.agent = self.automation

    def _route_complete(self, prefs: Any) -> Any:
        model = model_for(prefs, "fast")

        async def complete(prompt: str) -> str:
            return await self.llm.complete(model, [ChatMessage("user", prompt)],
                                           GenerationOptions(temperature=0.0, max_output_tokens=200, response_format="json"),
                                           purpose="router")

        return complete

    async def draft_email(self, instruction: str, to_name: str) -> tuple[str, str]:
        """Have the model write the subject and body. Raises if the model is unavailable or answers badly."""
        with self.db.session() as s:
            prefs = load_preferences(s)
        prompt = ("Write a short, natural email for the user to send. Reply with JSON only: "
                  '{"subject": "...", "body": "..."}. Plain text, no placeholders like [Name], greet the recipient'
                  f"{' (' + to_name + ')' if to_name else ''}, end with a short sign-off and no name.\n"
                  f"What the email should say: {instruction or 'a short friendly message'}")
        raw = await asyncio.wait_for(self.llm.complete(model_for(prefs, "fast"), [ChatMessage("user", prompt)],
                                                       GenerationOptions(temperature=0.4, max_output_tokens=450,
                                                                         response_format="json"),
                                                       purpose="email"), timeout=90)
        data = json.loads(raw)
        subject, body = str(data.get("subject", "")).strip()[:150], str(data.get("body", "")).strip()[:1500]
        if not body:
            raise ValueError("empty draft")
        return subject, body

    async def respond(self, req: TurnRequest) -> AsyncIterator[dict[str, Any]]:
        text = req.content.strip()
        if not text:
            yield {"event": "error", "message": "Empty message"}
            return
        with self.db.session() as s:
            conv = s.get(Conversation, req.conversation_id)
            if not conv:
                yield {"event": "error", "message": "Conversation not found"}
                return
            prefs = load_preferences(s)
            history = list(conv.messages)
            mode = conv.mode
            memories = quick.load_memories(s) if conv.mode != "coding" else []
            ws = s.get(Workspace, conv.workspace_id) if conv.workspace_id else None
            if ws:
                s.expunge(ws)
            user_msg = Message(conversation_id=conv.id, role="user", content=text, meta={"source": req.source})
            s.add(user_msg)
            if not history and conv.title == "New conversation":
                conv.title = title_from(text)
            conv.updated_at = utcnow()
            s.flush()
            user_id = user_msg.id

            # Skills (message / email / calendar...), confirmations, then deterministic local actions.
            res: Resolution | None = None
            intent: actions.Intent | None = None
            if req.allow_actions:
                prefs_snapshot = prefs
                s.commit()  # the user's message is saved; skills can be slow (drafting an email) and must not hold the database
                res = await self.assistant.resolve(text, prefs_snapshot, req.source, req.confidence)
                if res is not None and res.intent is not None:
                    intent, res = res.intent, None
                elif res is None:
                    intent = actions.recognise(text, known_apps=appcontrol.app_names(prefs))
                    if intent is not None:
                        res = self.assistant.gate_intent(intent, text, prefs, req.source, req.confidence)
                        if res is not None:
                            intent = None
                if res is None:
                    # The automation runtime: plans, acts, observes, verifies. It takes app commands and anything that
                    # needs several steps; a plain answer, or a model that can't plan, falls through to the older paths below.
                    res = await self.automation.handle(text, intent, prefs, source=req.source, conversation_id=conv.id,
                                                       workspace_id=conv.workspace_id, coding=mode == "coding")
                    if res is not None:
                        intent = None
                    elif intent is None and self.automation.last_kind != "answer":
                        res = await self.assistant.route(text, prefs, req.source, req.confidence, self._route_complete(prefs))
            if res is not None:
                reply = Message(conversation_id=conv.id, role="assistant", content=res.message,
                                model=None, meta={"action": {"kind": res.kind, "status": res.status, "pending": res.pending}})
                s.add(reply)
                s.flush()
                yield {"event": "meta", "user_message_id": user_id, "message_id": reply.id, "context_files": [],
                       "omitted": [], "action": {"kind": res.kind, "status": res.status, "pending": res.pending}}
                yield {"event": "token", "text": res.message}
                yield {"event": "done", "status": "complete", "message_id": reply.id}
                return
            if intent:
                outcome = actions.execute(s, intent, prefs, source=req.source, launcher=self.launcher)
                reply = Message(conversation_id=conv.id, role="assistant", content=outcome.message,
                                model=None, meta={"action": {"id": outcome.action_id, "kind": intent.kind,
                                                             "status": outcome.status}})
                s.add(reply)
                s.flush()
                yield {"event": "meta", "user_message_id": user_id, "message_id": reply.id, "context_files": [],
                       "omitted": [], "action": {"kind": intent.kind, "status": outcome.status}}
                yield {"event": "token", "text": outcome.message}
                yield {"event": "done", "status": "complete", "message_id": reply.id}
                return

        model = req.model or (model_for(prefs, "coding") if mode == "coding" else model_for(prefs, "fast"))
        semantic: list[Hit] | None = None
        semantic_note = None
        if ws is not None and req.auto_context and (ws.preferences or {}).get("semantic_search") and self.indexer:
            try:
                semantic = await asyncio.wait_for(self.indexer.query(ws.id, text), timeout=20)
            except Exception as e:  # embedding model missing / Ollama down: keyword retrieval still works
                semantic_note = f"Semantic search unavailable ({e.__class__.__name__}); used keyword search only."
        # File walking/indexing is blocking I/O: keep it off the event loop.
        try:
            plan = await asyncio.to_thread(build_prompt, prefs, mode, history, text, ws, req.files,
                                           req.auto_context and ws is not None, self.settings.max_file_bytes, semantic, memories)
        except Exception as e:  # e.g. project folder became unreadable
            log.exception("building the prompt failed")
            msg = f"Could not prepare the request: {e}"
            with self.db.session() as s:
                err = Message(conversation_id=req.conversation_id, role="assistant", content="", model=model,
                              status="error", meta={"error": msg, "source": req.source})
                s.add(err)
                s.flush()
                err_id = err.id
            yield {"event": "meta", "user_message_id": user_id, "message_id": err_id, "context_files": [], "omitted": []}
            yield {"event": "error", "message": msg, "message_id": err_id}
            yield {"event": "done", "status": "error", "message_id": err_id, "stats": {}}
            return
        with self.db.session() as s:
            reply = Message(conversation_id=req.conversation_id, role="assistant", content="", model=model,
                            status="streaming", context_files=plan.context_files,
                            meta={"semantic_note": semantic_note, "omitted": plan.omitted,
                                  "history_dropped": plan.history_dropped,
                                  "prompt_tokens_est": plan.estimated_prompt_tokens, "source": req.source})
            s.add(reply)
            s.flush()
            reply_id = reply.id

        gen = self.llm.new_generation(model, "chat")
        yield {"event": "meta", "user_message_id": user_id, "message_id": reply_id, "generation_id": gen.id,
               "model": model, "context_files": plan.context_files, "omitted": plan.omitted,
               "history_dropped": plan.history_dropped, "prompt_tokens_est": plan.estimated_prompt_tokens}
        g = prefs.generation
        out_cap = g.max_output_tokens if mode == "coding" else min(g.max_output_tokens, g.assistant_max_tokens)
        opts = GenerationOptions(temperature=g.temperature, top_p=g.top_p, num_ctx=g.num_ctx,
                                 max_output_tokens=out_cap, keep_alive=g.keep_alive)
        parts: list[str] = []
        trace: list[dict[str, Any]] = []
        status, error = "complete", None
        stats: dict[str, Any] = {}
        use_agent = (req.agent and mode == "coding" and ws is not None and self.commands is not None
                     and Path(ws.path).is_dir())
        stream: AsyncIterator[dict[str, Any]] = _plain_events(self.llm.stream(gen, plan.messages, opts))
        try:
            if use_agent:
                assert ws is not None and self.commands is not None
                toolbox = Toolbox(self.db, self.commands, ws.id, self.settings.max_file_bytes)
                await stream.aclose()  # type: ignore[attr-defined]  # not started; replace with the agent loop
                stream = run_agent(self.llm, gen, toolbox, plan.messages, opts, g.num_ctx)
            async for ev in stream:
                if ev["event"] == "token":
                    parts.append(ev["text"])
                    yield ev
                elif ev["event"] == "stats":
                    stats = ev["stats"] or {"completed": True}
                elif ev["event"] == "tool":
                    if ev.get("phase") == "done":
                        trace.append({k: ev.get(k) for k in ("name", "arguments", "status", "summary", "ref")})
                    yield ev
                elif ev["event"] == "notice":
                    yield ev
        except GenerationStopped:
            status = "stopped"
        except ModelNotFound:
            status, error = "error", (f"The model '{model}' is not installed in Ollama. "
                                      f"Install it from the Models page or run: ollama pull {model}")
        except ProviderUnavailable as e:
            status, error = "error", f"{e}. Is Ollama running? Start it from the Start menu or run 'ollama serve'."
        except ProviderError as e:
            status, error = "error", f"Model error: {e}"
        except Exception as e:  # stream interrupted (client disconnect -> GeneratorExit is not caught here)
            log.exception("chat generation failed")
            status, error = "error", f"Unexpected error: {e}"
        finally:
            gen.cancel.set()  # no-op if finished; stops the model if the client disconnected
            await stream.aclose()  # type: ignore[attr-defined]
            self.llm.finish(gen)
            with self.db.session() as s:
                m = s.get(Message, reply_id)
                if m:
                    m.content = "".join(parts)
                    # No final stats means the stream never completed (client disconnect / interruption).
                    m.status = "stopped" if (status == "complete" and not stats) else status
                    if error:
                        m.meta = {**(m.meta or {}), "error": error}
                    if stats:
                        m.meta = {**(m.meta or {}), "stats": stats}
                    if trace:
                        m.meta = {**(m.meta or {}), "tool_trace": trace}
                if stats.get("output_tokens"):
                    s.add(PerfSample(model=model, prompt_tokens=stats.get("prompt_tokens"),
                                     output_tokens=stats.get("output_tokens"), tokens_per_s=stats.get("tokens_per_s"),
                                     load_s=stats.get("load_s"), total_s=stats.get("total_s")))
        if error:
            yield {"event": "error", "message": error, "message_id": reply_id}
        yield {"event": "done", "status": status, "message_id": reply_id, "stats": stats}


async def _plain_events(stream: AsyncIterator[StreamChunk]) -> AsyncIterator[dict[str, Any]]:
    try:
        async for chunk in stream:
            if chunk.text:
                yield {"event": "token", "text": chunk.text}
            if chunk.done:
                yield {"event": "stats", "stats": chunk.stats}
    finally:
        await stream.aclose()  # type: ignore[attr-defined]


def mark_interrupted_messages(db: Database) -> int:
    """Replies that were still streaming when NEXUS stopped (crash, shutdown, sleep) can't resume."""
    with db.session() as s:
        rows = s.query(Message).filter(Message.status == "streaming").all()
        for m in rows:
            m.status = "stopped"
            m.meta = {**(m.meta or {}), "error": "NEXUS stopped before this answer finished."}
        return len(rows)


