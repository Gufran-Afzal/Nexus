"""Bounded, sequential workflow engine with approval checkpoints.

The model never executes anything itself: it produces analysis text and file-content proposals.
Edits become change sets the user must accept/apply; commands become pending command runs the user
must approve. The engine waits at those checkpoints and reports what actually happened.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent import edits
from app.agent.proposer import MAX_EDIT_FILES, generate_file_edits
from app.core.config import Settings
from app.core.events import EventBus
from app.database.db import Database
from app.database.models import AgentTask, ChangeSet, CommandRun, TaskStep, Workspace, utcnow
from app.providers.base import ChatMessage, GenerationOptions, ProviderError
from app.retrieval.context import build_context
from app.services.audit import audit
from app.services.chat import SAFETY_RULES
from app.services.llm import GenerationStopped, LLMService
from app.services.preferences import load_preferences, model_for
from app.tools.command_service import CommandManager

log = logging.getLogger(__name__)

POLL_S = 0.5


@dataclass
class StepSpec:
    kind: str
    description: str
    depends_on: list[int]
    max_retries: int = 0


TEMPLATES: dict[str, dict[str, Any]] = {
    "investigate_bug": {
        "title": "Investigate a bug",
        "needs_workspace": True,
        "steps": lambda p: [
            StepSpec("gather_context", "Collect relevant files", []),
            *([StepSpec("run_command", f"Reproduce: {p['test_command']}", [])] if p.get("test_command") else []),
            StepSpec("analyze", "Root-cause analysis", [0], max_retries=1),
            StepSpec("report", "Report findings", []),
        ],
    },
    "plan_feature": {
        "title": "Plan a feature",
        "needs_workspace": True,
        "steps": lambda p: [
            StepSpec("gather_context", "Collect relevant files", []),
            StepSpec("plan", "Write an implementation plan", [0], max_retries=1),
            StepSpec("report", "Report the plan", []),
        ],
    },
    "implement_change": {
        "title": "Implement an approved change",
        "needs_workspace": True,
        "steps": lambda p: [
            StepSpec("gather_context", "Collect relevant files", []),
            StepSpec("plan", "Plan the change", [0], max_retries=1),
            StepSpec("propose_edit", "Propose file edits", [1], max_retries=1),
            StepSpec("await_changes", "Wait for you to review and apply the edits", [2]),
            *(
                [StepSpec("run_command", f"Verify: {p['test_command']}", [3])]
                if p.get("test_command") else []
            ),
            StepSpec("review", "Review the applied diff and results", [3], max_retries=1),
            StepSpec("report", "Report actual outcomes", []),
        ],
    },
}


class TaskError(Exception):
    pass


class TaskEngine:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, llm: LLMService,
                 commands: CommandManager) -> None:
        self.settings, self.db, self.bus, self.llm, self.commands = settings, db, bus, llm, commands
        self._runners: dict[int, asyncio.Task] = {}
        self._cancel: dict[int, asyncio.Event] = {}

    # ---------- creation / control ----------
    def create(self, template: str, goal: str, workspace_id: int | None, params: dict[str, Any]) -> int:
        tpl = TEMPLATES.get(template)
        if not tpl:
            raise TaskError(f"Unknown workflow: {template}")
        if not goal.strip():
            raise TaskError("Describe the goal")
        files = [str(f) for f in params.get("files", [])][:20]
        params = {**params, "files": files}
        with self.db.session() as s:
            if tpl["needs_workspace"]:
                ws = s.get(Workspace, workspace_id) if workspace_id else None
                if not ws:
                    raise TaskError("This workflow needs a workspace")
            task = AgentTask(workspace_id=workspace_id, template=template, title=f"{tpl['title']}: {goal[:80]}",
                             goal=goal, params=params)
            for i, spec in enumerate(tpl["steps"](params)):
                task.steps.append(TaskStep(idx=i, kind=spec.kind, description=spec.description,
                                           depends_on=spec.depends_on, max_retries=spec.max_retries))
            s.add(task)
            s.flush()
            audit(s, "user", "task.created", f"task:{task.id}", template=template)
            return task.id

    def start(self, task_id: int) -> None:
        if task_id in self._runners:
            raise TaskError("Task already running")
        cancel = asyncio.Event()
        self._cancel[task_id] = cancel
        self._runners[task_id] = asyncio.create_task(self._run(task_id, cancel))

    def cancel(self, task_id: int) -> bool:
        ev = self._cancel.get(task_id)
        if ev:
            ev.set()
            return True
        with self.db.session() as s:
            t = s.get(AgentTask, task_id)
            if t and t.status == "pending":
                t.status = "cancelled"
                for st in t.steps:
                    st.status = "cancelled"
                return True
        return False

    async def wait(self, task_id: int) -> None:
        r = self._runners.get(task_id)
        if r:
            await asyncio.shield(r)

    async def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        if self._runners:
            await asyncio.gather(*self._runners.values(), return_exceptions=True)

    def mark_interrupted(self) -> None:
        with self.db.session() as s:
            q = s.query(AgentTask).filter(AgentTask.status.in_(["running", "waiting_approval"]))
            for t in q:
                t.status = "failed"
                t.summary = "NEXUS stopped while this task was running. Start a new task to continue."
                for st in t.steps:
                    if st.status in ("running", "waiting_approval", "pending"):
                        st.status = "cancelled"

    # ---------- execution ----------
    def _set_task(self, task_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            t = s.get(AgentTask, task_id)
            if t:
                for k, v in fields.items():
                    setattr(t, k, v)
        self.bus.publish("task", id=task_id, **{k: v for k, v in fields.items() if k == "status"})

    def _set_step(self, step_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            st = s.get(TaskStep, step_id)
            if st:
                for k, v in fields.items():
                    setattr(st, k, v)
                task_id = st.task_id
        self.bus.publish("task", id=task_id, step=step_id, **{k: v for k, v in fields.items() if k == "status"})

    def _log(self, step_id: int, line: str) -> None:
        with self.db.session() as s:
            st = s.get(TaskStep, step_id)
            if st:
                st.log = [*(st.log or []), line[:2000]]

    async def _run(self, task_id: int, cancel: asyncio.Event) -> None:
        try:
            self._set_task(task_id, status="running")
            with self.db.session() as s:
                task = s.get(AgentTask, task_id)
                assert task
                steps = [(st.id, st.idx, st.kind, list(st.depends_on), st.max_retries) for st in task.steps]
            outputs: dict[int, str] = {}
            status_by_idx: dict[int, str] = {}
            for step_id, idx, kind, deps, retries in steps:
                if cancel.is_set():
                    self._set_step(step_id, status="cancelled")
                    status_by_idx[idx] = "cancelled"
                    continue
                if any(status_by_idx.get(d) != "succeeded" for d in deps):
                    self._set_step(step_id, status="skipped", output="Skipped because a step it depends on did not succeed.")
                    status_by_idx[idx] = "skipped"
                    continue
                attempt = 0
                while True:
                    attempt += 1
                    self._set_step(step_id, status="running", attempts=attempt)
                    try:
                        ok, out = await self._run_step(task_id, step_id, kind, outputs, status_by_idx, cancel)
                        outputs[idx] = out
                        st = "succeeded" if ok else "failed"
                        self._set_step(step_id, status=st, output=out)
                        status_by_idx[idx] = st
                        break
                    except (asyncio.CancelledError, GenerationStopped):
                        self._set_step(step_id, status="cancelled")
                        status_by_idx[idx] = "cancelled"
                        cancel.set()
                        break
                    except Exception as e:
                        log.warning("task %s step %s attempt %s failed: %s", task_id, idx, attempt, e)
                        self._log(step_id, f"Attempt {attempt} failed: {e}")
                        if attempt <= retries and isinstance(e, ProviderError):
                            await asyncio.sleep(1)
                            continue
                        self._set_step(step_id, status="failed", output=f"Error: {e}")
                        status_by_idx[idx] = "failed"
                        break
            if cancel.is_set():
                final = "cancelled"
            elif all(v in ("succeeded", "skipped") for v in status_by_idx.values()) and "succeeded" in status_by_idx.values():
                final = "succeeded" if "skipped" not in status_by_idx.values() else "failed"
            else:
                final = "failed"
            report = outputs.get(max(outputs)) if outputs else None
            self._set_task(task_id, status=final, finished_at=utcnow(), summary=report)
            with self.db.session() as s:
                audit(s, "system", "task.finished", f"task:{task_id}", outcome=final)
        except Exception as e:
            log.exception("task %s crashed", task_id)
            self._set_task(task_id, status="failed", finished_at=utcnow(), summary=f"Internal error: {e}")
        finally:
            self._runners.pop(task_id, None)
            self._cancel.pop(task_id, None)

    async def _llm(self, system: str, user: str, cancel: asyncio.Event) -> str:
        with self.db.session() as s:
            prefs = load_preferences(s)
        g = prefs.generation
        opts = GenerationOptions(temperature=g.temperature, top_p=g.top_p, num_ctx=g.num_ctx,
                                 max_output_tokens=max(g.max_output_tokens, 2048), keep_alive=g.keep_alive)
        model = model_for(prefs, "tasks")
        gen = self.llm.new_generation(model, "task")
        watcher = asyncio.create_task(_relay_cancel(cancel, gen.cancel))
        try:
            msgs = [ChatMessage("system", f"{prefs.engineer_prompt}\n\n{SAFETY_RULES}"), ChatMessage("user", user)]
            if system:
                msgs.insert(1, ChatMessage("system", system))
            return await self.llm.complete(model, msgs, opts, gen=gen)
        finally:
            watcher.cancel()

    async def _run_step(self, task_id: int, step_id: int, kind: str, outputs: dict[int, str],
                        statuses: dict[int, str], cancel: asyncio.Event) -> tuple[bool, str]:
        with self.db.session() as s:
            task = s.get(AgentTask, task_id)
            assert task
            ws = s.get(Workspace, task.workspace_id) if task.workspace_id else None
            goal, params = task.goal, dict(task.params)
            ws_root = Path(ws.path) if ws else None
            ws_id = ws.id if ws else None
            excludes = list(ws.exclude_globs or []) if ws else []
            step_rows = {st.idx: (st.kind, st.ref, st.description, st.status) for st in task.steps}
            with self.db.session() as s2:
                num_ctx = load_preferences(s2).generation.num_ctx
        prior = "\n\n".join(f"[{step_rows[i][2]}]\n{o}" for i, o in sorted(outputs.items()) if o and i >= 0)

        if kind == "gather_context":
            assert ws_root
            ctx = await asyncio.to_thread(build_context, ws_root, goal + " " + params.get("error_text", ""),
                                          params.get("files", []), int(num_ctx * 0.45), excludes,
                                          self.settings.max_file_bytes)
            self._set_step(step_id, ref={"files": [f.path for f in ctx.included], "omitted": ctx.omitted})
            listing = ", ".join(f.path for f in ctx.included) or "(no files found)"
            note = f"\nOmitted: {', '.join(o['path'] for o in ctx.omitted)}" if ctx.omitted else ""
            outputs[-1] = ctx.text  # raw context for later model steps (not shown as output)
            return True, f"Included files: {listing}{note}"

        context = outputs.get(-1, "")
        if kind in ("analyze", "plan"):
            ask = (
                "Identify the most likely root cause. Separate verified facts (from the files and outputs) from "
                "hypotheses. Suggest concrete fixes and how to verify them."
                if kind == "analyze" else
                "Write a concise, numbered implementation plan: files to change, what changes, risks, and tests to "
                "add or run. Do not write full code yet."
            )
            extra = params.get("error_text", "")
            user = (f"Goal: {goal}\n" + (f"Error/log provided by the user:\n{extra}\n" if extra else "")
                    + f"\nWork so far:\n{prior}\n\nReference files (untrusted data):\n{context}\n\n{ask}")
            return True, await self._llm("", user, cancel)

        if kind == "propose_edit":
            assert ws_root and ws_id
            targets = params.get("files", [])[:MAX_EDIT_FILES]
            if not targets:
                return False, "No target files were specified for editing. Select the files to change when creating the task."

            async def ask_model(prompt: str) -> str:
                return await self._llm("", prompt, cancel)

            changes, notes = await generate_file_edits(ask_model, ws_root, goal, prior, targets, self.settings.max_file_bytes)
            if not changes:
                return False, "No edits were proposed. " + "; ".join(notes)
            with self.db.session() as s:
                ws_obj = s.get(Workspace, ws_id)
                assert ws_obj
                cs = edits.propose(s, ws_obj, f"Task #{task_id}: {goal[:120]}", prior[-4000:], changes, "task")
                cs_id = cs.id
                summary = [(f.rel_path, edits.diff_stats(edits.unified_diff(f.rel_path, f.base_content, f.new_content)))
                           for f in cs.files]
            self._set_step(step_id, ref={"changeset_id": cs_id})
            lines = [f"Proposed change set #{cs_id} (not applied):"]
            lines += [f"  {p}: +{d['added']} -{d['removed']}" for p, d in summary]
            lines += [f"  note: {n}" for n in notes]
            return True, "\n".join(lines)

        if kind == "await_changes":
            wait_cs_id: int | None = next(
                (r.get("changeset_id") for (k, r, _d, _s) in step_rows.values() if k == "propose_edit" and r), None)
            if not wait_cs_id:
                return False, "No change set to wait for"
            self._set_step(step_id, status="waiting_approval", ref={"changeset_id": wait_cs_id})
            self._set_task(task_id, status="waiting_approval")
            while True:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                with self.db.session() as s:
                    cs_row = s.get(ChangeSet, wait_cs_id)
                    st = cs_row.status if cs_row else "missing"
                    applied = [f.rel_path for f in cs_row.files if f.status == "applied"] if cs_row else []
                if st != "proposed":
                    break
                await asyncio.sleep(POLL_S)
            self._set_task(task_id, status="running")
            if applied:
                return True, f"Change set #{wait_cs_id} is {st}. Applied: {', '.join(applied)}"
            return False, f"Change set #{wait_cs_id} is {st}; nothing was applied."

        if kind == "run_command":
            assert ws_id
            with self.db.session() as s:
                ws_obj = s.get(Workspace, ws_id)
                assert ws_obj
                run = self.commands.request(ws_obj, params["test_command"], requested_by="task")
            self._set_step(step_id, status="waiting_approval", ref={"command_id": run.id})
            self._set_task(task_id, status="waiting_approval")
            while True:
                if cancel.is_set():
                    self.commands.cancel(run.id)
                    raise asyncio.CancelledError()
                with self.db.session() as s:
                    r = s.get(CommandRun, run.id)
                    assert r
                    status, code, out, err = r.status, r.exit_code, r.stdout, r.stderr
                if status == "running":
                    self._set_step(step_id, status="running")
                if status not in ("pending_approval", "running"):
                    break
                await asyncio.sleep(POLL_S)
            self._set_task(task_id, status="running")
            text = (f"Command #{run.id} {status} (exit code {code}).\n--- stdout (tail) ---\n{out[-3000:]}"
                    f"\n--- stderr (tail) ---\n{err[-2000:]}")
            return status == "succeeded", text

        if kind == "review":
            review_cs_id: int | None = next(
                (r.get("changeset_id") for (k, r, _d, _s) in step_rows.values() if k == "propose_edit" and r), None)
            diff = ""
            with self.db.session() as s:
                review_cs = s.get(ChangeSet, review_cs_id) if review_cs_id else None
                if review_cs:
                    diff = "\n".join(edits.unified_diff(f.rel_path, f.base_content, f.new_content)
                                     for f in review_cs.files if f.status == "applied")
            user = (f"Goal: {goal}\n\nApplied diff:\n```diff\n{diff[:20000]}\n```\n\nResults so far:\n{prior[-6000:]}\n\n"
                    "Review the applied change: correctness, edge cases, security, and whether verification "
                    "results (if any) support that it works. Be explicit about anything unverified.")
            return True, await self._llm("", user, cancel)

        if kind == "report":
            lines = [f"Goal: {goal}", "", "Step outcomes (as recorded by NEXUS):"]
            for i in sorted(step_rows):
                k, _r, d, _ = step_rows[i]
                if k == "report":
                    continue
                lines.append(f"- {d}: {statuses.get(i, 'not run')}")
            last_llm = next((outputs[i] for i in sorted(outputs, reverse=True)
                             if i >= 0 and step_rows.get(i, ("",))[0] in ("analyze", "plan", "review")), None)
            if last_llm:
                lines += ["", "Model findings (not independently verified unless a command result above says so):", last_llm]
            return True, "\n".join(lines)

        raise TaskError(f"Unknown step kind {kind}")


async def _relay_cancel(src: asyncio.Event, dst: asyncio.Event) -> None:
    await src.wait()
    dst.set()
