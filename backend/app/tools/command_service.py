"""Lifecycle of command runs: request -> (approve | reject) -> running -> result."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.core.config import Settings
from app.core.events import EventBus
from app.database.db import Database
from app.database.models import CommandRun, Workspace, utcnow
from app.services.audit import audit
from app.tools.commands import classify, parse_command, project_bin_dirs, run_command
from app.workspaces.security import AccessDenied, resolve_in_workspace

log = logging.getLogger(__name__)


class CommandError(Exception):
    pass


def serialize(run: CommandRun) -> dict:
    return {
        "id": run.id, "workspace_id": run.workspace_id, "argv": run.argv, "cwd": run.cwd, "risk": run.risk,
        "reasons": run.reasons, "requested_by": run.requested_by, "status": run.status,
        "exit_code": run.exit_code, "stdout": run.stdout, "stderr": run.stderr, "truncated": run.truncated,
        "timeout_s": run.timeout_s, "created_at": run.created_at, "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


class CommandManager:
    def __init__(self, settings: Settings, db: Database, bus: EventBus) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self._running: dict[int, asyncio.Event] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def request(self, ws: Workspace, command: str | list[str], cwd: str = ".", requested_by: str = "user",
                timeout_s: int | None = None) -> CommandRun:
        """Create a pending command. Nothing is executed until `approve` is called by the user."""
        argv = parse_command(command)
        cls = classify(argv)
        resolve_in_workspace(Path(ws.path), cwd)  # validate working directory
        timeout = min(timeout_s or self.settings.command_timeout_s, 3600)
        with self.db.session() as s:
            run = CommandRun(
                workspace_id=ws.id, argv=argv, cwd=cwd, risk=cls.risk, reasons=cls.reasons,
                requested_by=requested_by, timeout_s=timeout,
                status="rejected" if cls.blocked else "pending_approval",
                stderr="Blocked by NEXUS policy: " + "; ".join(cls.reasons) if cls.blocked else "",
            )
            s.add(run)
            s.flush()
            audit(s, requested_by, "command.requested", f"workspace:{ws.id}", outcome=run.status,
                  argv=argv, risk=cls.risk)
            s.refresh(run)
        self.bus.publish("command", id=run.id, status=run.status)
        return run

    def approve(self, run_id: int, confirm_risk: bool = False) -> CommandRun:
        with self.db.session() as s:
            run = s.get(CommandRun, run_id)
            if not run:
                raise CommandError("Command not found")
            if run.status != "pending_approval":
                raise CommandError(f"Command is {run.status}, not awaiting approval")
            ws = s.get(Workspace, run.workspace_id)
            if not ws or not ws.allow_commands:
                raise AccessDenied("Command execution is disabled for this workspace. Enable it in workspace settings.")
            if run.risk in ("destructive", "package_install", "network") and not confirm_risk:
                raise CommandError(f"This is a {run.risk.replace('_', ' ')} command; explicit confirmation required")
            run.status = "running"
            run.started_at = utcnow()
            audit(s, "user", "command.approved", f"workspace:{ws.id}", argv=run.argv, risk=run.risk)
            cwd = resolve_in_workspace(Path(ws.path), run.cwd)
            argv, timeout = list(run.argv), run.timeout_s
            bins = project_bin_dirs(Path(ws.path))
        cancel = asyncio.Event()
        self._running[run_id] = cancel
        self._tasks[run_id] = asyncio.create_task(self._execute(run_id, argv, cwd, timeout, cancel, bins))
        self.bus.publish("command", id=run_id, status="running")
        return run

    async def _execute(self, run_id: int, argv: list[str], cwd: Path, timeout: int, cancel: asyncio.Event,
                       bins: list[Path]) -> None:
        try:
            res = await run_command(argv, cwd, timeout, self.settings.command_output_limit, cancel, bins)
            status, code, out, err, trunc = res.status, res.exit_code, res.stdout, res.stderr, res.truncated
        except Exception as e:
            log.exception("command %s failed to start", run_id)
            status, code, out, err, trunc = "error", None, "", f"Failed to start: {e}", False
        finally:
            self._running.pop(run_id, None)
            self._tasks.pop(run_id, None)
        with self.db.session() as s:
            run = s.get(CommandRun, run_id)
            if run:
                run.status, run.exit_code, run.stdout, run.stderr, run.truncated = status, code, out, err, trunc
                run.finished_at = utcnow()
                audit(s, "system", "command.finished", f"command:{run_id}", outcome=status, exit_code=code)
        self.bus.publish("command", id=run_id, status=status, exit_code=code)

    def reject(self, run_id: int) -> None:
        with self.db.session() as s:
            run = s.get(CommandRun, run_id)
            if not run or run.status != "pending_approval":
                raise CommandError("Command is not awaiting approval")
            run.status = "rejected"
            run.finished_at = utcnow()
            audit(s, "user", "command.rejected", f"command:{run_id}")
        self.bus.publish("command", id=run_id, status="rejected")

    def cancel(self, run_id: int) -> bool:
        ev = self._running.get(run_id)
        if not ev:
            return False
        ev.set()
        return True

    async def wait(self, run_id: int) -> None:
        t = self._tasks.get(run_id)
        if t:
            await asyncio.shield(t)

    async def shutdown(self) -> None:
        for ev in self._running.values():
            ev.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def mark_interrupted(self) -> None:
        """On startup, runs that were 'running' when the app stopped can't be resumed."""
        with self.db.session() as s:
            for run in s.query(CommandRun).filter(CommandRun.status == "running"):
                run.status = "error"
                run.stderr += "\n[NEXUS stopped while this command was running]"
                run.finished_at = utcnow()
