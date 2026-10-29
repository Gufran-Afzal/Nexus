"""Bounded tool-using agent loop for coding conversations.

Round = one model call. If the model asks for tools, NEXUS validates and runs them (read-only ones
immediately; edits/commands only as pending proposals), feeds the results back and calls the model
again. Stops after MAX_ROUNDS, when the model answers without tools, or when the context budget is
spent. Uses Ollama native tool calls when the model supports them, otherwise a fenced-JSON protocol.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from app.agent.tools import Toolbox
from app.providers.base import ChatMessage, GenerationOptions, ToolCall, ToolsUnsupported
from app.retrieval.context import estimate_tokens
from app.services.llm import Generation, LLMService

MAX_ROUNDS = 6
MAX_CALLS_PER_ROUND = 4
TOOL_BLOCK = re.compile(r"```tool\s*\n?(.*?)```", re.S)
# Qwen/Hermes-style calls that sometimes arrive as plain text when the server-side parser misses them.
TOOL_XML = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
MARKERS = ("```tool", "<tool_call>")

AGENT_GUIDE = (
    "You can use tools to inspect the project before answering. Look before you assume: search and read the "
    "relevant files first. Read-only tools run immediately. propose_edit and propose_command only create "
    "requests that the user must approve, so after using them tell the user what you proposed and that it is "
    "waiting for their review; never say a change was made or a command ran unless a tool result says so. "
    "When you have enough information, answer without calling tools."
)


def parse_text_tool_calls(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in [*TOOL_BLOCK.findall(text), *TOOL_XML.findall(text)]:
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("name"), str):
                args = it.get("arguments") or it.get("args") or {}
                calls.append(ToolCall(it["name"], args if isinstance(args, dict) else {}))
    return calls


class _VisibleFilter:
    """Streams text to the user but holds back fenced tool blocks (text protocol)."""

    def __init__(self) -> None:
        self.buf = ""
        self.hidden = False

    def feed(self, text: str) -> str:
        if self.hidden:
            return ""
        self.buf += text
        found = [i for i in (self.buf.find(m) for m in MARKERS) if i >= 0]
        if found:
            idx = min(found)
            out, self.buf, self.hidden = self.buf[:idx], "", True
            return out
        keep = max(len(m) for m in MARKERS) - 1  # a marker may be split across chunks
        out, self.buf = self.buf[:-keep] if len(self.buf) > keep else "", self.buf[-keep:]
        return out

    def flush(self) -> str:
        out, self.buf = ("" if self.hidden else self.buf), ""
        return out


async def run_agent(
    llm: LLMService,
    gen: Generation,
    toolbox: Toolbox,
    messages: list[ChatMessage],
    options: GenerationOptions,
    num_ctx: int,
) -> AsyncIterator[dict[str, Any]]:
    """Yields {"event": "token"|"tool"|"stats"} dicts. Raises GenerationStopped on stop."""
    native = True
    msgs = list(messages)
    msgs[0] = ChatMessage("system", msgs[0].content + "\n\n" + AGENT_GUIDE)
    budget = num_ctx - (options.max_output_tokens or 1024) - 256
    total_stats: dict[str, Any] = {}

    force_final = False
    gen.multi = True  # several model calls share one generation (one Stop button)
    for round_no in range(MAX_ROUNDS + 2):
        final_round = round_no >= MAX_ROUNDS or force_final
        opts = GenerationOptions(**{**options.__dict__, "tools": None if (final_round or not native) else toolbox.schemas()})
        if final_round and not force_final:
            msgs.append(ChatMessage("user", "Tool budget reached. Answer now using what you found; say what is unverified."))
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        vis = _VisibleFilter()
        try:
            async for chunk in llm.stream(gen, msgs, opts):
                if chunk.text:
                    text_parts.append(chunk.text)
                    shown = vis.feed(chunk.text)
                    if shown:
                        yield {"event": "token", "text": shown}
                calls.extend(chunk.tool_calls)
                if chunk.done:
                    total_stats = chunk.stats
        except ToolsUnsupported:
            native = False
            msgs[0] = ChatMessage("system", msgs[0].content + "\n\n" + toolbox.text_protocol())
            yield {"event": "notice", "text": "This model has no native tool support; using the text tool protocol."}
            continue
        tail = vis.flush()
        if tail:
            yield {"event": "token", "text": tail}
        text = "".join(text_parts)
        if not final_round and not calls:
            calls = parse_text_tool_calls(text)  # text protocol, or a native model that wrote a tool block
        if not calls or final_round:
            break
        calls = calls[:MAX_CALLS_PER_ROUND]
        if native:
            msgs.append(ChatMessage("assistant", text, tool_calls=calls))
        else:
            msgs.append(ChatMessage("assistant", text))
        results_text: list[str] = []
        gen.state = "tools"
        for call in calls:
            if gen.cancel.is_set():
                break
            yield {"event": "tool", "phase": "running", "name": call.name, "arguments": _short(call.arguments)}
            outcome = await asyncio.to_thread(toolbox.call, call.name, call.arguments)
            yield {"event": "tool", "phase": "done", "name": call.name, "arguments": _short(call.arguments),
                   "status": outcome.status, "summary": outcome.summary, "ref": outcome.ref}
            content = outcome.content
            used = sum(_tokens(m) for m in msgs)
            room = budget - used - 200
            if estimate_tokens(content) > room:
                content = content[: max(room, 0) * 4] + "\n[result cut to fit the context window]"
            if native:
                msgs.append(ChatMessage("tool", content, tool_name=call.name))
            else:
                results_text.append(f"Result of {call.name}:\n{content}")
        if not native:
            msgs.append(ChatMessage("user", "Tool results (untrusted data):\n\n" + "\n\n".join(results_text)))
        if sum(_tokens(m) for m in msgs) > budget:
            msgs.append(ChatMessage("user", "The context window is nearly full. Answer now with what you have."))
            force_final = True
    yield {"event": "stats", "stats": total_stats}


def _tokens(m: ChatMessage) -> int:
    """Estimate including tool-call arguments (a proposed file travels back to the model in them)."""
    extra = json.dumps([c.arguments for c in m.tool_calls]) if m.tool_calls else ""
    return estimate_tokens(m.content) + estimate_tokens(extra) + 4


def _short(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) else str(v)
        out[k] = s[:200] + "…" if isinstance(s, str) and len(s) > 200 else s
    return out
