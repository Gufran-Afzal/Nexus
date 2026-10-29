"""Scenarios only the new runtime can express: they drive the runtime directly (there is no 0.6.0 equivalent to compare to)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel

from app.agent import plan as plans
from app.agent.capabilities import Tool, ToolContext, ToolResult, ok
from app.agent.runtime import AgentRuntime
from app.database.models import AgentRun, Reminder
from benchmarks.env import WINDOW
from benchmarks.harness import Bench, Outcome, claims_success
from benchmarks.scenarios import Scenario, reminders


class _NoArgs(BaseModel):
    pass


def _bad_tool(_a: Any, _c: ToolContext) -> Any:
    return {"this": "is not a ToolResult"}  # a tool that violates the contract


def _step(sid: str, tool: str, **args: Any) -> plans.PlanStep:
    return plans.PlanStep(sid, tool, args)


async def s06(b: Bench) -> tuple[bool, dict[str, Any], str]:
    auto = b.state.conversations.automation
    auto.registry.register(Tool("test.bad", "A tool that returns garbage.", _NoArgs, _bad_tool, risk="low"))
    plan = plans.Plan([_step("s1", "test.bad"), _step("s2", "reminder.create", text="drink water", when="in 5 minutes")])
    view = await auto.runtime.execute(auto.runtime.create("run a broken tool then set a reminder", plan))
    s1 = view["steps"][0]
    good = (s1["status"] == "failed" and s1["error_class"] == "invalid" and view["status"] == "partially_completed"
            and reminders(b, "drink water") == 1 and "couldn't" in view["summary"].lower())
    return good, view, view["summary"]


async def s10(b: Bench) -> tuple[bool, dict[str, Any], str]:
    """A restart in the middle of a run: Spotify was launched but the result was never recorded. After restart it must be
    noticed as already open (not launched a second time), and the rest of the plan must still happen."""
    auto = b.state.conversations.automation
    plan = plans.Plan([_step("s1", "app.open", name="notepad"), _step("s2", "app.open", name="spotify"),
                       _step("s3", "reminder.create", text="stand up", when="in 20 minutes")])
    rid = auto.runtime.create("open notepad and spotify then remind me to stand up", plan)
    # ---- before the crash: s1 finished and was recorded; s2's launch happened but the process died before recording it
    b.env.launcher(["C:\\Windows\\notepad.exe"], "app")
    b.env.launcher(["C:\\Users\\me\\AppData\\Roaming\\Spotify\\Spotify.exe"], "app")
    b.env.windows[:] = [WINDOW["notepad"], WINDOW["spotify"]]
    launches_before = len(b.env.launched)
    with b.state.db.session() as s:
        row = s.get(AgentRun, rid)
        row.status = "running"
        st = dict(row.state)
        steps = {k: dict(v) for k, v in st["steps"].items()}
        steps["s1"] = {"status": "succeeded", "attempts": 1, "result": ToolResult("succeeded", "Started Notepad.", verified=True).as_dict()}
        steps["s2"] = {"status": "running", "attempts": 1}
        st.update(steps=steps, checkpoint="s2")
        row.state = st
    # ---- restart: a brand new runtime over the same database
    fresh = AgentRuntime(b.state.db, auto.registry, None, sleep=auto.runtime.sleep)
    parked = fresh.recover()
    parked_view = fresh.view(rid)
    view = await fresh.resume(rid)
    relaunched = len(b.env.launched) - launches_before
    good = (parked == 1 and parked_view["status"] == "waiting_for_input" and view["status"] == "completed" and relaunched == 0
            and reminders(b, "stand up") == 1 and view["steps"][1]["already_done"])
    return good, view, view["summary"]


async def s12(b: Bench) -> tuple[bool, dict[str, Any], str]:
    """The user cancels while the run is in progress: later steps must not happen and the report must say what did."""
    auto = b.state.conversations.automation
    holder: dict[str, int] = {}

    def press_cancel(_a: Any, _c: ToolContext) -> ToolResult:
        auto.runtime.live[holder["rid"]].cancel.set()  # as if the Cancel button was pressed during this step
        return ok("Working…")

    auto.registry.register(Tool("test.work", "A step during which the user cancels.", _NoArgs, press_cancel, risk="low"))
    plan = plans.Plan([_step("s1", "app.open", name="notepad"), _step("s2", "test.work"),
                       _step("s3", "reminder.create", text="never happens", when="in 5 minutes")])
    holder["rid"] = auto.runtime.create("open notepad, work, then remind me", plan)
    view = await auto.runtime.execute(holder["rid"])
    good = (view["status"] == "cancelled" and reminders(b, "never happens") == 0 and view["steps"][2]["status"] == "skipped"
            and "notepad" in view["summary"].lower())
    return good, view, view["summary"]


RUNTIME = [
    Scenario("S06", "Invalid tool result (a tool breaks the contract)", runtime_only=True, run=s06, failure_injected=True),
    Scenario("S10", "Resume after a restart without repeating a launch", runtime_only=True, run=s10),
    Scenario("S12", "Cancellation in the middle of a run", runtime_only=True, run=s12),
]


def run_runtime_scenario(sc: Scenario) -> Outcome:
    b = Bench(sc, legacy=False)
    out = Outcome(sc.id)
    t0 = time.time()
    try:
        good, view, reply = asyncio.run(sc.run(b))  # type: ignore[misc]
        out.seconds = time.time() - t0
        out.success, out.replies, out.status = good, [reply], view["status"]
        out.honest = good or not claims_success(reply)
        for st in view["steps"]:
            out.tools_used.append(st["tool"])
            out.tool_args.append(st["args"])
            out.steps_verified.append(st["verified"])
        out.recovered = bool(view["metrics"].get("recovered"))
        out.duplicates = max(0, int(b.db(lambda s: s.query(Reminder).count())) - 1) if sc.id == "S10" else 0
    finally:
        b.close()
    return out
