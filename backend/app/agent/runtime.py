"""The automation runtime: runs a validated plan step by step, observes the result of each step, verifies it against the
environment, recovers or stops honestly, and keeps everything in one durable row so a restart can resume.

States: queued, planning, running, waiting_for_approval, waiting_for_input, retrying, completed, partially_completed,
failed, cancelled. Nothing runs in the background: a run executes while a request (or approve/resume) is awaiting it, and
a run that needs a human is simply parked in the database until they answer."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent import plan as plans
from app.agent.capabilities import (
    AWAITING,
    BLOCKED,
    FAILED,
    INVALID,
    PERMISSION,
    SUCCEEDED,
    TIMEOUT,
    TRANSIENT,
    VERIFY,
    Registry,
    Tool,
    ToolResult,
)
from app.database.db import Database
from app.database.models import AgentLesson, AgentRun, Workflow, utcnow
from app.services.preferences import AgentPrefs

log = logging.getLogger(__name__)

TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}
PAUSED = {"waiting_for_approval", "waiting_for_input"}
ACTIVE = {"queued", "planning", "running", "retrying"}
AMBIGUOUS = "ambiguous"
RETRYABLE = {TRANSIENT, TIMEOUT, VERIFY}

ReplanFn = Callable[[str, str, int], Awaitable[plans.Plan | None]]  # goal, what happened, first free step number -> new steps


@dataclass
class Live:
    """Per-drive working state (the database row holds the durable copy)."""

    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def needs_approval(tool: Tool, prefs: AgentPrefs) -> bool:
    """Autonomy policy. Reading and reversible things run alone; consequential ones ask. Self-gated tools only create a proposal
    that the user reviews in its own screen, so they do not ask twice."""
    if tool.self_gated:
        return False
    if tool.risk == "high":
        return True
    return tool.risk == "medium" and prefs.auto_approve_risk != "medium"


def describe_call(tool: Tool, args: dict[str, Any]) -> str:
    if tool.name == "files.apply_moves":
        moves = args.get("moves") or []
        sample = "; ".join(f"{m.get('src')} → {m.get('dst')}" for m in moves[:4] if isinstance(m, dict))
        return f"Move {len(moves)} file(s) in {args.get('folder')}: {sample}{' …' if len(moves) > 4 else ''}"
    if tool.name == "message.send":
        return f"Press Send in {args.get('target')}: “{str(args.get('preview', ''))[:100]}”"
    brief = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())
    return f"{tool.description.rstrip('.')} ({brief})" if brief else tool.description.rstrip(".")


class AgentRuntime:
    def __init__(self, db: Database, registry: Registry, bus: Any = None, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, replanner: ReplanFn | None = None) -> None:
        self.db, self.registry, self.bus, self.sleep, self.replanner = db, registry, bus, sleep, replanner
        self.live: dict[int, Live] = {}
        self.prefs_fn: Callable[[], AgentPrefs] = lambda: AgentPrefs()

    @property
    def ctx(self) -> Any:
        return self.registry.ctx

    def agent_prefs(self) -> AgentPrefs:
        try:
            return self.registry.ctx.prefs().agent  # type: ignore[no-any-return]
        except Exception:
            return AgentPrefs()

    # ================================================================= creating and reading runs
    def create(self, goal: str, plan: plans.Plan, *, source: str = "text", conversation_id: int | None = None,
               workspace_id: int | None = None, llm_calls: int = 0) -> int:
        ap = self.agent_prefs()
        state: dict[str, Any] = {"steps": {s.id: {"status": "pending", "attempts": 0} for s in plan.steps}, "pending": None, "checkpoint": "",
                 "history": [], "caveats": []}
        constraints = ap.model_dump()
        metrics = {"llm_calls": llm_calls, "tool_calls": 0, "retries": 0, "replans": 0, "recovered": False, "seconds": 0.0}
        with self.db.session() as s:
            row = AgentRun(goal=goal[:1000], status="queued", source=source, workspace_id=workspace_id, conversation_id=conversation_id,
                           workflow=plan.workflow, constraints=constraints, criteria=plan.criteria, plan=[st.as_dict() for st in plan.steps],
                           state=state, metrics=metrics)
            s.add(row)
            s.flush()
            rid = row.id
        self._emit(rid)
        return rid

    def _load(self, rid: int) -> tuple[dict[str, Any], plans.Plan]:
        with self.db.session() as s:
            row = s.get(AgentRun, rid)
            if row is None:
                raise KeyError(rid)
            d = {"id": row.id, "goal": row.goal, "status": row.status, "source": row.source, "workflow": row.workflow,
                 "conversation_id": row.conversation_id, "workspace_id": row.workspace_id,
                 "constraints": copy.deepcopy(row.constraints or {}), "criteria": list(row.criteria or []),
                 "plan": copy.deepcopy(row.plan or []), "state": copy.deepcopy(row.state or {}),
                 "metrics": copy.deepcopy(row.metrics or {}), "summary": row.summary or "",
                 "created_at": row.created_at.isoformat() if row.created_at else "",
                 "finished_at": row.finished_at.isoformat() if row.finished_at else ""}
        return d, plans.plan_from_dict({"steps": d["plan"], "criteria": d["criteria"], "workflow": d["workflow"]})

    def _save(self, run: dict[str, Any], plan: plans.Plan | None = None) -> None:
        with self.db.session() as s:
            row = s.get(AgentRun, run["id"])
            if row is None:
                return
            row.status, row.state, row.metrics, row.summary = run["status"], copy.deepcopy(run["state"]), copy.deepcopy(run["metrics"]), run["summary"]
            if plan is not None:
                row.plan = [st.as_dict() for st in plan.steps]
            row.updated_at = utcnow()
            if run["status"] in TERMINAL and row.finished_at is None:
                row.finished_at = utcnow()
        self._emit(run["id"])

    def _emit(self, rid: int) -> None:
        if self.bus is not None:
            try:
                self.bus.publish("run", id=rid)
            except Exception:  # an event listener must never break a run
                log.debug("run event failed", exc_info=True)

    def view(self, rid: int) -> dict[str, Any]:
        run, plan = self._load(rid)
        st = run["state"]
        steps = []
        for ps in plan.steps:
            ss = st.get("steps", {}).get(ps.id, {})
            r = ss.get("result") or {}
            steps.append({"id": ps.id, "tool": ps.tool, "args": ss.get("args") or ps.args, "when": ps.when, "on_fail": ps.on_fail,
                          "note": ps.note, "status": ss.get("status", "pending"), "attempts": ss.get("attempts", 0),
                          "summary": r.get("summary", ss.get("reason", "")), "verified": r.get("verified"),
                          "observed": r.get("observed", ""), "error": r.get("error", ""), "error_class": r.get("error_class", ""),
                          "already_done": bool(r.get("already_done")), "approved": ss.get("approved"), "recovery": ss.get("recovery", "")})
        return {"id": run["id"], "goal": run["goal"], "status": run["status"], "source": run["source"], "workflow": run["workflow"],
                "criteria": run["criteria"], "steps": steps, "pending": st.get("pending"), "metrics": run["metrics"],
                "summary": run["summary"], "caveats": st.get("caveats", []), "created_at": run["created_at"],
                "finished_at": run["finished_at"], "constraints": run["constraints"]}

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.session() as s:
            ids = [r.id for r in s.query(AgentRun).order_by(AgentRun.id.desc()).limit(limit)]
        return [self.view(i) for i in ids]

    def last_app(self, conversation_id: int | None) -> str:
        """The app most recently opened or focused in this conversation (so "close it" means something)."""
        if conversation_id is None:
            return ""
        with self.db.session() as s:
            rows = s.query(AgentRun).filter(AgentRun.conversation_id == conversation_id).order_by(AgentRun.id.desc()).limit(6).all()
            for r in rows:
                for ps in reversed(r.plan or []):
                    ss = (r.state or {}).get("steps", {}).get(ps["id"], {})
                    if ps["tool"] in ("app.open", "app.focus") and ss.get("status") == "succeeded":
                        return str((ss.get("args") or ps["args"]).get("name") or "")
        return ""

    def history(self, conversation_id: int | None, n: int = 4) -> list[str]:
        out: list[str] = []
        if conversation_id is None:
            return out
        with self.db.session() as s:
            for r in s.query(AgentRun).filter(AgentRun.conversation_id == conversation_id).order_by(AgentRun.id.desc()).limit(n):
                out.append(f"{r.goal[:80]} → {r.status}")
        return list(reversed(out))

    # ================================================================= driving a run
    def _live(self, rid: int) -> Live:
        return self.live.setdefault(rid, Live())

    async def execute(self, rid: int) -> dict[str, Any]:
        """Run (or continue) a run until it finishes or needs a person. Returns the run view."""
        live = self._live(rid)
        async with live.lock:
            run, plan = self._load(rid)
            if run["status"] in TERMINAL:
                return self.view(rid)
            if live.cancel.is_set():
                await self._finish(run, plan, "cancelled", "Cancelled before it started.")
                return self.view(rid)
            run["status"] = "running"
            run["state"]["pending"] = None
            self.registry.ctx.workspace_id = run["workspace_id"] or self.registry.ctx.workspace_id
            self._save(run)
            t0 = time.monotonic()
            try:
                await self._drive(run, plan, live, t0)
            except Exception as e:  # a bug in the runtime must end the run visibly, not hang or vanish
                log.exception("run %s crashed", rid)
                await self._finish(run, plan, "failed", f"The run stopped on an internal error: {e}")
            run["metrics"]["seconds"] = round(run["metrics"].get("seconds", 0.0) + time.monotonic() - t0, 3)
            self._save(run, plan)
        return self.view(rid)

    async def _drive(self, run: dict[str, Any], plan: plans.Plan, live: Live, t0: float) -> None:
        ap = AgentPrefs.model_validate(run["constraints"] or {})
        results: dict[str, dict[str, Any]] = {k: v.get("result", {}) for k, v in run["state"]["steps"].items() if v.get("result")}
        i = 0
        while i < len(plan.steps):
            ps = plan.steps[i]
            i += 1
            ss = run["state"]["steps"].setdefault(ps.id, {"status": "pending", "attempts": 0})
            if ss["status"] in ("succeeded", "skipped", "failed", "denied"):
                continue
            if live.cancel.is_set():
                await self._finish(run, plan, "cancelled", "Cancelled.")
                return
            if time.monotonic() - t0 > ap.max_seconds or run["metrics"]["tool_calls"] >= ap.max_tool_calls:
                ss.update(status="skipped", reason="the time or tool-call budget ran out")
                self._caveat(run, f"Stopped early: the budget ran out before {ps.id} ({ps.tool}).")
                self._skip_rest(run, plan, i, "the budget ran out")
                break
            blocked = [d for d in plans.dependencies(ps) if run["state"]["steps"].get(d, {}).get("status") in ("failed", "skipped", "denied")]
            if blocked:
                ss.update(status="skipped", reason=f"needs {blocked[0]}, which did not succeed")
                continue
            if not plans.eval_when(ps.when, results):
                ss.update(status="skipped", reason=f"condition not met: {ps.when}")
                continue
            outcome = await self._run_step(run, plan, ps, ss, results, live, ap)
            self._save(run)
            if outcome == "pause":
                return
            if outcome == "cancelled":
                await self._finish(run, plan, "cancelled", "Cancelled.")
                return
            if outcome == "stop":
                self._skip_rest(run, plan, i, f"{ps.id} failed and the plan says to stop")
                break
            if outcome == "replan":
                new = await self._replan(run, plan, ps, ss)
                if new:
                    plan.steps = plan.steps[:i] + new
                    for st in new:
                        run["state"]["steps"][st.id] = {"status": "pending", "attempts": 0}
                    self._save(run, plan)
                else:
                    self._skip_rest(run, plan, i, "no usable alternative plan")
                    break
        await self._conclude(run, plan)

    # ---------------------------------------------------------------- one step
    async def _run_step(self, run: dict[str, Any], plan: plans.Plan, ps: plans.PlanStep, ss: dict[str, Any],
                        results: dict[str, dict[str, Any]], live: Live, ap: AgentPrefs) -> str:
        """Returns: ok | continue | stop | replan | pause | cancelled."""
        tool = self.registry.get(ps.tool)
        if tool is None:
            return self._fail_step(ss, ps, ToolResult(FAILED, f"Unknown tool {ps.tool}.", error="unknown tool", error_class=INVALID))
        args_raw = plans.resolve_args(ps.args, results)
        _t, parsed, err = self.registry.validate(ps.tool, args_raw if isinstance(args_raw, dict) else {})
        ss["args"] = args_raw
        if parsed is None:
            return self._fail_step(ss, ps, ToolResult(FAILED, err, error=err, error_class=INVALID))
        args = parsed.model_dump()

        # work the user was deciding on elsewhere (an edit proposal, a command): ask how it ended instead of repeating it
        if ss["status"] == "waiting" and tool.poll is not None:
            res = await asyncio.to_thread(tool.poll, ss.get("ref") or {}, self.registry.ctx)
            return self._after(run, ss, ps, tool, res, results)

        if needs_approval(tool, ap) and not ss.get("approved"):
            ss["status"] = "awaiting_approval"
            run["state"]["pending"] = {"kind": "approval", "step": ps.id, "tool": tool.name, "risk": tool.risk,
                                       "prompt": describe_call(tool, args)}
            run["status"] = "waiting_for_approval"
            return "pause"

        pre = self.registry.get_precheck(tool, parsed)
        if pre is not None:  # the world already matches the goal: change nothing
            return self._after(run, ss, ps, tool, pre, results)

        max_retries = min(ps.retries if ps.retries is not None else tool.retries, ap.max_retries)
        recovered_with = ""
        while True:
            if live.cancel.is_set():
                return "cancelled"
            ss["attempts"] += 1
            ss["status"] = "running"
            run["state"]["checkpoint"] = ps.id
            run["metrics"]["tool_calls"] += 1
            self._save(run)
            res = await self.registry.invoke(tool, parsed, live.cancel)
            if res.status == SUCCEEDED or res.status == AWAITING:
                break
            action = self._analyse(tool, res, ss["attempts"] - 1, max_retries, recovered_with)
            if action == "retry":
                run["metrics"]["retries"] += 1
                run["status"] = "retrying"
                ss["status"] = "retrying"
                self._save(run)
                await self.sleep(min(2.0, 0.4 * ss["attempts"]))
                run["status"] = "running"
                continue
            if action.startswith("recover:"):
                rtool = self.registry.get(action[8:])
                recovered_with = action[8:]
                if rtool is not None:
                    _t2, rargs, _e = self.registry.validate(rtool.name, {})
                    rres = await self.registry.invoke(rtool, rargs) if rargs is not None else None
                    ss["recovery"] = f"{rtool.name}: {(rres.summary if rres else 'could not run')}"
                    if rres is not None and rres.ok:
                        run["metrics"]["retries"] += 1
                        now_true = self.registry.get_precheck(tool, parsed)  # the obstacle was all that was wrong: is it done now?
                        if now_true is not None:
                            now_true.already_done = False
                            now_true.summary = now_true.summary.replace("already ", "").rstrip(".") + " (after clearing a dialog)."
                            res = now_true
                            break
                        continue
            break
        if res.status == SUCCEEDED and ss.get("recovery"):
            run["metrics"]["recovered"] = True
            if ap.remember_lessons and recovered_with:
                self._lesson(ps.tool, BLOCKED, f"If {ps.tool} is blocked by a dialog, use {recovered_with} and try again.")
        elif res.status == SUCCEEDED and ss["attempts"] > 1:
            run["metrics"]["recovered"] = True
        return self._after(run, ss, ps, tool, res, results)

    def _analyse(self, tool: Tool, res: ToolResult, retries_used: int, max_retries: int, recovered_with: str) -> str:
        """Failure analysis: what to do depends on WHY it failed."""
        ec = res.error_class
        if ec == BLOCKED and tool.recovery.get(BLOCKED) and not recovered_with:
            return "recover:" + tool.recovery[BLOCKED]
        if ec in RETRYABLE and retries_used < max_retries and (tool.idempotent or ec in (TRANSIENT, TIMEOUT) and tool.risk in ("read", "low")):
            return "retry"
        return "stop"

    def _after(self, run: dict[str, Any], ss: dict[str, Any], ps: plans.PlanStep, tool: Tool, res: ToolResult,
               results: dict[str, dict[str, Any]]) -> str:
        d = res.as_dict()
        ss["result"] = d
        results[ps.id] = d
        if res.status == AWAITING:
            ss["status"], ss["ref"] = "waiting", res.ref
            run["state"]["pending"] = {"kind": "external", "step": ps.id, "tool": tool.name, "prompt": res.summary, "ref": res.ref}
            run["status"] = "waiting_for_approval"
            return "pause"
        if res.status == SUCCEEDED:
            ss["status"] = "succeeded"
            if res.verified is None and tool.risk != "read":
                self._caveat(run, f"I couldn't check that “{res.summary.rstrip('.')}” really happened.")
            return "ok"
        if res.error_class == AMBIGUOUS:
            ss["status"] = "waiting"
            run["state"]["pending"] = {"kind": "input", "step": ps.id, "tool": tool.name, "prompt": res.summary}
            run["status"] = "waiting_for_input"
            return "pause"
        return self._fail_step(ss, ps, res)

    def _fail_step(self, ss: dict[str, Any], ps: plans.PlanStep, res: ToolResult) -> str:
        ss["status"] = "denied" if res.error_class == PERMISSION else "failed"
        ss.setdefault("result", res.as_dict())
        if ps.on_fail == "stop":
            return "stop"
        return "replan" if ps.on_fail == "replan" and res.error_class != PERMISSION else "continue"

    async def _replan(self, run: dict[str, Any], plan: plans.Plan, ps: plans.PlanStep, ss: dict[str, Any]) -> list[plans.PlanStep] | None:
        ap = AgentPrefs.model_validate(run["constraints"] or {})
        if self.replanner is None or run["metrics"]["replans"] >= ap.max_replans or run["metrics"]["llm_calls"] >= ap.max_llm_calls:
            return None
        run["metrics"]["replans"] += 1
        run["metrics"]["llm_calls"] += 1
        what = f"{ps.id} ({ps.tool}) failed: {(ss.get('result') or {}).get('summary', '')}"
        try:
            new = await self.replanner(run["goal"], what, len(plan.steps) + 1)
        except Exception:
            return None
        return list(new.steps) if new else None

    @staticmethod
    def _skip_rest(run: dict[str, Any], plan: plans.Plan, start: int, why: str) -> None:
        for later in plan.steps[start:]:
            st = run["state"]["steps"].setdefault(later.id, {"status": "pending", "attempts": 0})
            if st["status"] == "pending":
                st.update(status="skipped", reason=why)

    @staticmethod
    def _caveat(run: dict[str, Any], text: str) -> None:
        if text not in run["state"]["caveats"]:
            run["state"]["caveats"].append(text)

    # ================================================================= finishing
    async def _conclude(self, run: dict[str, Any], plan: plans.Plan) -> None:
        steps = run["state"]["steps"]
        status_of = [steps.get(p.id, {}).get("status") for p in plan.steps]
        done = "succeeded" in status_of
        bad = any(s in ("failed", "denied") for s in status_of)
        cut = any("budget" in (steps.get(p.id, {}).get("reason") or "") for p in plan.steps)
        status = ("partially_completed" if done else "failed") if (bad or cut) else "completed"
        await self._finish(run, plan, status, self.report(run, plan, status))

    async def _finish(self, run: dict[str, Any], plan: plans.Plan, status: str, summary: str, keep_summary: bool = False) -> None:
        run["status"] = status
        run["state"]["pending"] = None
        if status == "cancelled" and not keep_summary:
            for p in plan.steps:
                st = run["state"]["steps"].setdefault(p.id, {"status": "pending", "attempts": 0})
                if st["status"] in ("pending", "awaiting_approval", "waiting", "running", "retrying"):
                    st.update(status="skipped", reason="cancelled")
            summary = self.report(run, plan, status)
        run["summary"] = summary
        self._save(run, plan)

    def report(self, run: dict[str, Any], plan: plans.Plan, status: str) -> str:
        """What happened, in plain words: done, not done, and anything that could not be checked."""
        steps = run["state"]["steps"]
        done, failed, skipped = [], [], []
        changed = any(steps.get(p.id, {}).get("status") == "succeeded" and self._changes_things(p.tool) for p in plan.steps)
        for p in plan.steps:
            st = steps.get(p.id, {})
            r = st.get("result") or {}
            line = (r.get("summary") or "").strip()
            if st.get("status") == "succeeded":
                if not self._changes_things(p.tool) and changed:
                    continue  # a look-around that led to a change: the change is what the person wants to hear about
                if r.get("already_done"):
                    line = f"{line.rstrip('.')} (nothing needed changing)"
                elif r.get("verified") and r.get("observed") and p.tool.startswith(("files.", "app.", "project.")):
                    line = f"{line.rstrip('.')}; checked: {r['observed'][:80]}"
                done.append(line)
            elif st.get("status") in ("failed", "denied"):
                failed.append(line or f"{p.tool} failed")
            elif st.get("status") == "skipped" and st.get("reason", "").startswith(("needs", "condition")):
                skipped.append(f"{p.tool} was skipped ({st['reason']})")
        if status == "cancelled":
            return "Cancelled." + (f" Already done before that: {' '.join(done)}" if done else " Nothing was changed.")
        out = []
        if failed:
            out.append("I couldn't finish all of it. " + " ".join(_sentence(f) for f in failed))
            if done:
                out.append("What did work: " + " ".join(_sentence(d) for d in done))
        elif done:
            out.append(" ".join(_sentence(d) for d in done))
        else:
            out.append("Nothing needed doing." if status == "completed" else "Nothing was done.")
        out.extend(skipped)
        out.extend(run["state"]["caveats"][:3])
        return " ".join(o for o in out if o).strip()

    def _changes_things(self, tool_name: str) -> bool:
        t = self.registry.get(tool_name)
        return t is not None and t.risk != "read"

    # ================================================================= human decisions
    async def approve(self, rid: int, yes: bool = True) -> dict[str, Any]:
        run, plan = self._load(rid)
        pend = run["state"].get("pending")
        if run["status"] != "waiting_for_approval" or not pend or pend.get("kind") != "approval":
            return self.view(rid)
        step = run["state"]["steps"][pend["step"]]
        if yes:
            step["approved"] = True
            self._save(run)
            return await self.execute(rid)
        step.update(status="denied", approved=False, result=ToolResult("denied", "You declined, so I didn't do it.", error="declined",
                                                                       error_class=PERMISSION).as_dict())
        self._skip_rest(run, plan, [p.id for p in plan.steps].index(pend["step"]) + 1, "you declined an earlier step")
        did = any(run["state"]["steps"].get(p.id, {}).get("status") == "succeeded" and self._changes_things(p.tool) for p in plan.steps)
        await self._finish(run, plan, "partially_completed" if did else "cancelled",
                           "Okay, I didn't do it. Nothing was changed." if not did else "Okay, I stopped there.", keep_summary=True)
        return self.view(rid)

    async def answer(self, rid: int, answer: str) -> dict[str, Any]:
        """A person's reply to a question the run asked (waiting_for_input): the run restarts that step with the answer."""
        run, _plan = self._load(rid)
        pend = run["state"].get("pending")
        if run["status"] != "waiting_for_input" or not pend or pend.get("kind") != "input":
            return self.view(rid)
        st = run["state"]["steps"][pend["step"]]
        st.update(status="failed", result=ToolResult(FAILED, f"Needed an answer: {pend.get('prompt', '')}", error="unanswered").as_dict())
        self._save(run)
        return await self.execute(rid)

    async def cancel(self, rid: int) -> dict[str, Any]:
        live = self._live(rid)
        live.cancel.set()
        run, plan = self._load(rid)
        if run["status"] in PAUSED or run["status"] == "queued":
            await self._finish(run, plan, "cancelled", "Cancelled.")
        return self.view(rid)

    async def refresh(self, rid: int) -> dict[str, Any]:
        """Check on work the user was deciding elsewhere (a proposed edit, a command) and continue if it has finished."""
        run, _plan = self._load(rid)
        pend = run["state"].get("pending")
        if run["status"] == "waiting_for_approval" and pend and pend.get("kind") == "external":
            return await self.execute(rid)
        return self.view(rid)

    # ================================================================= restart and resume
    def recover(self) -> int:
        """After a restart: runs that were mid-flight cannot be assumed to be running. Park them for a decision."""
        n = 0
        with self.db.session() as s:
            ids = [r.id for r in s.query(AgentRun).filter(AgentRun.status.in_(ACTIVE)).all()]
        for rid in ids:
            run, plan = self._load(rid)
            cur = run["state"].get("checkpoint") or ""
            st = run["state"]["steps"].get(cur, {})
            if st.get("status") in ("running", "retrying"):
                st["status"] = "unknown"
            run["status"] = "waiting_for_input"
            run["state"]["pending"] = {"kind": "resume", "step": cur, "prompt": "NEXUS restarted while this was running. Resume it?"}
            self._save(run, plan)
            n += 1
        return n

    async def resume(self, rid: int, redo: bool = False) -> dict[str, Any]:
        """Continue an interrupted run. A step whose outcome is unknown is checked against the world first; it is repeated only
        when repeating is harmless (idempotent) or the user said so."""
        run, plan = self._load(rid)
        pend = run["state"].get("pending")
        if run["status"] != "waiting_for_input" or not pend or pend.get("kind") != "resume":
            return self.view(rid)
        sid = pend.get("step") or ""
        st = run["state"]["steps"].get(sid)
        if st and st.get("status") == "unknown":
            ps = next((p for p in plan.steps if p.id == sid), None)
            tool = self.registry.get(ps.tool) if ps else None
            results = {k: v.get("result", {}) for k, v in run["state"]["steps"].items() if v.get("result")}
            if ps and tool:
                _t, parsed, _e = self.registry.validate(ps.tool, plans.resolve_args(ps.args, results))
                pre = self.registry.get_precheck(tool, parsed) if parsed is not None else None
                if pre is not None:
                    st["result"], st["status"] = pre.as_dict(), "succeeded"
                elif tool.idempotent or tool.risk == "read" or redo:
                    st["status"] = "pending"
                else:
                    run["state"]["pending"] = {"kind": "resume", "step": sid, "needs_decision": True,
                                               "prompt": f"I don't know whether “{tool.description.rstrip('.')}” finished before the restart, "
                                                         "and repeating it could do it twice. Repeat it anyway?"}
                    self._save(run)
                    return self.view(rid)
            else:
                st["status"] = "pending"
        self._save(run)
        return await self.execute(rid)

    # ================================================================= memory of what worked
    def _lesson(self, tool: str, error_class: str, fix: str) -> None:
        with self.db.session() as s:
            row = s.query(AgentLesson).filter(AgentLesson.tool == tool, AgentLesson.error_class == error_class).first()
            if row is None:
                s.add(AgentLesson(tool=tool, error_class=error_class, fix=fix[:300], uses=1))
            else:
                row.uses += 1

    def lessons_for(self, goal: str, k: int = 4) -> list[str]:
        names = {t.name for t in self.registry.search(goal, 8)}
        with self.db.session() as s:
            return [r.fix for r in s.query(AgentLesson).filter(AgentLesson.tool.in_(names)).order_by(AgentLesson.uses.desc()).limit(k)]

    def saved_workflows(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            return [{"name": w.name, "description": w.description, "keywords": list(w.keywords or []), "steps": list(w.steps or []),
                     "criteria": list(w.criteria or []), "builtin": w.builtin, "uses": w.uses}
                    for w in s.query(Workflow).order_by(Workflow.name)]

    def save_workflow(self, rid: int, name: str, keywords: list[str]) -> tuple[bool, str]:
        """Keep a run that completed and verified as a reusable template. Its steps are re-checked against the world every time."""
        run, plan = self._load(rid)
        if run["status"] != "completed":
            return False, "Only a run that completed can be saved as a workflow."
        if any(self.registry.get(p.tool) is None for p in plan.steps):
            return False, "That run uses a tool that no longer exists."
        name = name.strip()[:80]
        if not name:
            return False, "Give the workflow a name."
        with self.db.session() as s:
            if s.query(Workflow).filter(Workflow.name == name).first():
                return False, f"A workflow called “{name}” already exists."
            s.add(Workflow(name=name, description=run["goal"][:300], keywords=[k.lower() for k in keywords][:6],
                           steps=[p.as_dict() for p in plan.steps], criteria=run["criteria"]))
        return True, f"Saved workflow “{name}”."

    def note_workflow_use(self, name: str) -> None:
        with self.db.session() as s:
            w = s.query(Workflow).filter(Workflow.name == name).first()
            if w:
                w.uses += 1
                w.last_used = dt.datetime.now(dt.UTC).replace(tzinfo=None)
