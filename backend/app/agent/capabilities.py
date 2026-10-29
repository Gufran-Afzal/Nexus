"""The tool contract and the capability registry.

A *tool* is a capability the agent can use to reach a goal, not a function mapped to a sentence. Each one declares what it
needs (schema, permissions), how risky it is, how long it may take, whether repeating it is harmless, and how to check that
it really worked (a postcondition observed from the environment, never just "it did not raise").

The registry is the single place that lists what NEXUS can actually do right now. The planner reads it instead of
inventing capabilities, the UI shows it, and `diagnose()` probes it without changing anything.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

RISKS = ("read", "low", "medium", "high")  # read: no side effects; low: reversible; medium: noticeable; high: external/destructive

SUCCEEDED, FAILED, AWAITING, DENIED, UNAVAILABLE = "succeeded", "failed", "awaiting_approval", "denied", "unavailable"
# Error classes drive failure analysis: what to do next depends on WHY a step failed.
TRANSIENT, TIMEOUT, NOT_FOUND, BLOCKED, INVALID, PERMISSION, MISSING, VERIFY = (
    "transient", "timeout", "not_found", "blocked", "invalid", "permission", "unavailable", "verify_failed")


@dataclass
class ToolResult:
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_class: str = ""
    verified: bool | None = None  # True/False once checked against the environment; None = no way to observe it
    observed: str = ""  # what the verification saw
    ref: dict[str, Any] = field(default_factory=dict)  # e.g. {"changeset_id": 4} for work waiting on the user
    already_done: bool = False  # the world already matched the goal, so nothing was changed

    @property
    def ok(self) -> bool:
        return self.status == SUCCEEDED

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "data": _jsonable(self.data), "error": self.error,
                "error_class": self.error_class, "verified": self.verified, "observed": self.observed, "ref": self.ref,
                "already_done": self.already_done}


def _jsonable(v: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(v)[:200]
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in list(v.items())[:60]}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in list(v)[:200]]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v[:4000] if isinstance(v, str) else v
    return str(v)[:500]


def fail(summary: str, error_class: str = "", **data: Any) -> ToolResult:
    return ToolResult(FAILED, summary, data, error=summary, error_class=error_class)


def ok(summary: str, **data: Any) -> ToolResult:
    return ToolResult(SUCCEEDED, summary, data)


@dataclass
class ToolContext:
    """Everything a tool may touch. Tests (and the benchmark) swap `system`, `launcher` and `sleep`."""

    db: Any
    settings: Any
    system: Any  # SystemControl
    launcher: Callable[[Any, str], None]
    commands: Any = None
    bus: Any = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], Any] = field(default=lambda: __import__("datetime").datetime.now())
    workspace_id: int | None = None
    verify_timeout_s: float = 6.0  # how long to wait for an app to show up
    poll_s: float = 0.5

    def prefs(self) -> Any:
        from app.services.preferences import load_preferences

        with self.db.session() as s:
            return load_preferences(s)


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any, ToolContext], ToolResult]
    risk: str = "read"
    needs: tuple[str, ...] = ()  # "apps" | "windows" | "files" | "workspace" | "network" | "contacts" | "model"
    tags: tuple[str, ...] = ()  # purpose words used for discovery ("launch start app program")
    timeout_s: float = 20.0
    retries: int = 0  # how many times a transient failure may be retried
    idempotent: bool = False  # repeating it after an unknown outcome is harmless
    already_done: Callable[[Any, ToolContext], ToolResult | None] | None = None  # pre-check: is the goal already true?
    verify: Callable[[Any, ToolResult, ToolContext], tuple[bool | None, str]] | None = None  # postcondition
    poll: Callable[[dict[str, Any], ToolContext], ToolResult] | None = None  # outcome of work the user is deciding on
    recovery: dict[str, str] = field(default_factory=dict)  # error_class -> tool that may clear the obstacle
    available: Callable[[ToolContext], tuple[bool, str]] | None = None
    self_gated: bool = False  # only creates a proposal the user reviews elsewhere (so no second approval)
    limits: str = ""  # honest limitations shown to the planner and the user

    def schema(self) -> dict[str, Any]:
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": params}

    def brief(self) -> str:
        props = self.args.model_json_schema().get("properties", {})
        req = set(self.args.model_json_schema().get("required", []))
        sig = ", ".join(f"{k}{'' if k in req else '?'}" for k in props)
        return f"{self.name}({sig}): {self.description} [risk={self.risk}]" + (f" Limit: {self.limits}" if self.limits else "")


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _WORD.findall(text.lower())}


class Registry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.risk not in RISKS:
            raise ValueError(f"{tool.name}: unknown risk {tool.risk}")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def search(self, goal: str, k: int = 10) -> list[Tool]:
        """Tools relevant to a goal, found by purpose (name, tags, description words), not by a fixed phrase."""
        want = _tokens(goal)
        scored: list[tuple[float, str, Tool]] = []
        for t in self.tools.values():
            ok_, _why = self.is_available(t)
            haystack = _tokens(t.name.replace(".", " ")) | _tokens(" ".join(t.tags)) | _tokens(t.description)
            score = len(want & haystack) + 2 * len(want & _tokens(" ".join(t.tags + (t.name.replace('.', ' '),))))
            if score or t.risk == "read":
                scored.append((score + (0.0 if ok_ else -0.5), t.name, t))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [t for _s, _n, t in scored[:k]]

    def is_available(self, tool: Tool) -> tuple[bool, str]:
        if tool.available is None:
            return True, ""
        try:
            return tool.available(self.ctx)
        except Exception as e:  # a broken health check must not break planning
            return False, f"health check failed: {e}"

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tools.values():
            av, why = self.is_available(t)
            out.append({"name": t.name, "description": t.description, "risk": t.risk, "needs": list(t.needs), "tags": list(t.tags),
                        "timeout_s": t.timeout_s, "retries": t.retries, "idempotent": t.idempotent,
                        "verifies": t.verify is not None, "available": av, "unavailable_reason": why, "limits": t.limits,
                        "parameters": t.schema()["parameters"]})
        return out

    def diagnose(self) -> list[dict[str, Any]]:
        """Probe every tool's availability without performing any action (health checks only read state)."""
        rows = []
        for t in self.tools.values():
            av, why = self.is_available(t)
            rows.append({"tool": t.name, "ok": av, "detail": why or "ready", "risk": t.risk, "verifies": t.verify is not None})
        return rows

    # ---------------------------------------------------------------- execution
    def validate(self, name: str, arguments: dict[str, Any]) -> tuple[Tool | None, BaseModel | None, str]:
        tool = self.tools.get(name)
        if tool is None:
            return None, None, f"Unknown tool '{name}'."
        try:
            return tool, tool.args.model_validate(arguments or {}), ""
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            return tool, None, f"Invalid arguments for {name}: {msg}"

    async def invoke(self, tool: Tool, args: BaseModel, cancel: asyncio.Event | None = None) -> ToolResult:
        """Run one tool with its timeout and verify the postcondition. Never raises: failures are results."""
        av, why = self.is_available(tool)
        if not av:
            return ToolResult(UNAVAILABLE, f"{tool.name} is not available: {why}", error=why, error_class=MISSING)
        if cancel is not None and cancel.is_set():
            return fail("Cancelled before it started.", "cancelled")
        t0 = time.time()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(tool.run, args, self.ctx), timeout=tool.timeout_s)
        except TimeoutError:
            return ToolResult(FAILED, f"{tool.name} did not finish within {tool.timeout_s:g}s.", error="timeout", error_class=TIMEOUT)
        except Exception as e:
            return ToolResult(FAILED, f"{tool.name} failed: {e}", error=str(e), error_class=TRANSIENT)
        if not isinstance(res, ToolResult):  # a tool that returns the wrong thing is a failed tool, not a success
            return ToolResult(FAILED, f"{tool.name} returned an invalid result.", error="invalid result", error_class=INVALID)
        res.data.setdefault("elapsed_s", round(time.time() - t0, 3))
        if res.status == SUCCEEDED and tool.verify is not None and res.verified is None and not res.already_done:
            try:
                res.verified, res.observed = await asyncio.wait_for(asyncio.to_thread(tool.verify, args, res, self.ctx),
                                                                    timeout=max(tool.timeout_s, self.ctx.verify_timeout_s + 5))
            except Exception as e:
                res.verified, res.observed = None, f"could not verify: {e}"
            if res.verified is False:
                res.status, res.error_class = FAILED, res.error_class or VERIFY
                res.error = res.error or f"{res.summary.rstrip('.')}, but the check failed: {res.observed}."
                res.summary = res.error
        return res

    def get_precheck(self, tool: Tool, args: BaseModel) -> ToolResult | None:
        if tool.already_done is None:
            return None
        try:
            return tool.already_done(args, self.ctx)
        except Exception:
            return None
