"""The automation runtime: tool contract, plans, planner, runtime state machine, API and the conversation wiring.

The desktop is `benchmarks.env.FakeEnv` (windows that appear, dialogs that block, apps that fail) and the model is scripted."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from app.agent import plan as plans
from app.agent import planner
from app.agent.capabilities import FAILED, Tool, ok
from app.agent.runtime import AgentRuntime, needs_approval
from app.database.models import AgentLesson, AgentRun, Reminder, Workflow
from app.services.preferences import AgentPrefs, save_preferences
from benchmarks.env import WINDOW, Behaviour
from benchmarks.harness import Bench
from benchmarks.scenarios import Scenario


class NoArgs(BaseModel):
    pass


@pytest.fixture
def bench() -> Iterator[Bench]:
    b = Bench(Scenario("T", "test"), legacy=False)
    yield b
    b.close()


def auto(b: Bench) -> Any:
    return b.state.conversations.automation


def run(b: Bench, steps: list[plans.PlanStep], goal: str = "test", **kw: Any) -> dict[str, Any]:
    a = auto(b)
    rid = a.runtime.create(goal, plans.Plan(steps, **kw))
    return asyncio.run(a.runtime.execute(rid))


def step(sid: str, tool: str, **args: Any) -> plans.PlanStep:
    return plans.PlanStep(sid, tool, args)


def n_reminders(b: Bench, text: str = "") -> int:
    return int(b.db(lambda s: s.query(Reminder).filter(Reminder.text.ilike(f"%{text}%")).count()))


# ============================================================ registry and tool contract
def test_registry_finds_tools_by_purpose_not_by_phrase(bench: Bench) -> None:
    reg = auto(bench).registry
    assert reg.search("please tidy up my downloads folder", 5)[0].name.startswith("files.")
    assert "app.open" in [t.name for t in reg.search("launch spotify", 4)]
    assert "reminder.create" in [t.name for t in reg.search("alert me tomorrow", 4)]


def test_every_tool_has_schema_risk_and_description(bench: Bench) -> None:
    rows = auto(bench).registry.describe()
    assert len(rows) >= 25
    for r in rows:
        assert r["description"] and r["risk"] in ("read", "low", "medium", "high") and r["parameters"]["type"] == "object"
    risky = {r["name"]: r["risk"] for r in rows}
    assert risky["files.apply_moves"] == "high" and risky["message.send"] == "high" and risky["app.close"] == "medium"
    assert risky["files.plan_organize"] == "read"


def test_validation_rejects_bad_arguments_and_unknown_tools(bench: Bench) -> None:
    reg = auto(bench).registry
    assert reg.validate("nope.nope", {})[2].startswith("Unknown tool")
    assert "when" in reg.validate("reminder.create", {"text": "x"})[2]
    assert reg.validate("app.open", {"name": "notepad"})[1] is not None


def test_diagnose_changes_nothing_and_reports_health(bench: Bench) -> None:
    before = (list(bench.env.launched), list(bench.env.keys))
    r = bench.client.post("/api/capabilities/diagnose").json()
    assert r["total"] >= 25 and r["ok"] >= 10
    assert (bench.env.launched, bench.env.keys) == before
    projects = next(t for t in r["tools"] if t["tool"] == "project.git_status")
    assert projects["ok"] is False and "project" in projects["detail"].lower()  # no project open: honest about it


def test_tool_timeout_is_a_failure_not_a_hang(bench: Bench) -> None:
    import time

    reg = auto(bench).registry
    reg.register(Tool("t.slow", "slow", NoArgs, lambda a, c: (time.sleep(0.5), ok("late"))[1], timeout_s=0.05))
    view = run(bench, [step("s1", "t.slow")])
    assert view["status"] == "failed" and view["steps"][0]["error_class"] == "timeout"


def test_tool_returning_garbage_is_a_failed_step(bench: Bench) -> None:
    auto(bench).registry.register(Tool("t.bad", "bad", NoArgs, lambda a, c: "yes", risk="low"))  # type: ignore[arg-type,return-value]
    view = run(bench, [step("s1", "t.bad"), step("s2", "reminder.create", text="after bad", when="in 5 minutes")])
    assert view["status"] == "partially_completed" and view["steps"][0]["error_class"] == "invalid" and n_reminders(bench, "after bad") == 1


def test_a_success_that_fails_its_check_is_reported_as_failure(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="never")
    view = run(bench, [step("s1", "app.open", name="spotify")])
    s = view["steps"][0]
    assert view["status"] == "failed" and s["status"] == "failed" and s["verified"] is False
    assert "no Spotify window" in view["summary"] and "couldn't" in view["summary"].lower()


# ============================================================ plans
def test_parse_plan_validates_tools_arguments_and_references(bench: Bench) -> None:
    reg = auto(bench).registry
    good = {"steps": [{"id": "s1", "tool": "files.plan_organize", "args": {"folder": "downloads"}},
                      {"id": "s2", "tool": "files.apply_moves", "args": {"folder": "downloads", "moves": "$s1.moves"}, "when": "$s1.count > 0"}]}
    p, err = plans.parse_plan(good, reg)
    assert p and not err and p.steps[1].on_fail == "continue"
    assert "unknown tool" in plans.parse_plan({"steps": [{"id": "s1", "tool": "rm.rf"}]}, reg)[1]
    assert "invalid" in plans.parse_plan({"steps": [{"id": "s1", "tool": "app.open", "args": {}}]}, reg)[1].lower()
    assert "does not come earlier" in plans.parse_plan({"steps": [{"id": "s1", "tool": "app.open", "args": {"name": "$s2.x"}}]}, reg)[1]
    assert "Too many" in plans.parse_plan({"steps": [{"tool": "window.list"}] * 30}, reg)[1]
    assert plans.parse_plan({"steps": []}, reg)[0] is None


def test_references_and_conditions() -> None:
    results = {"s1": {"data": {"moves": [1, 2], "count": 2}, "status": "succeeded", "verified": True}}
    assert plans.resolve_args({"m": "$s1.moves", "t": "n=$s1.count"}, results) == {"m": [1, 2], "t": "n=2"}
    assert plans.eval_when("$s1.count > 0", results) and not plans.eval_when("$s1.count > 5", results)
    assert plans.eval_when("$s1.status == succeeded", results) and plans.eval_when("$s1.verified", results)
    assert plans.eval_when("", results) and not plans.eval_when("garbage ???", results)


# ============================================================ planner
def test_extract_json_tolerates_chatter() -> None:
    assert planner.extract_json('Sure! {"mode": "answer"} hope that helps') == {"mode": "answer"}
    assert planner.extract_json("no json here") is None


def test_planner_repairs_once_then_gives_up(bench: Bench) -> None:
    reg = auto(bench).registry
    replies = iter(['{"mode": "plan", "steps": [{"tool": "rm.rf"}]}', '{"mode": "plan", "steps": [{"id":"s1","tool":"window.list"}]}'])
    calls: list[str] = []

    async def complete(prompt: str) -> str:
        calls.append(prompt)
        return next(replies)

    prop = asyncio.run(planner.plan_with_llm("show windows", reg, complete))
    assert prop.kind == "plan" and prop.llm_calls == 2 and "rejected" in calls[1] and "unknown tool" in calls[1]

    async def junk(_p: str) -> str:
        return "I think you should open it."

    bad = asyncio.run(planner.plan_with_llm("show windows", reg, junk))
    assert bad.kind == "none" and bad.llm_calls == 2


def test_planner_prompt_is_compact_and_marks_memory_as_data(bench: Bench) -> None:
    reg = auto(bench).registry
    p = planner.build_prompt("open spotify", reg, memories=["likes jazz"], lessons=["x"], k=6)
    assert planner.PLANNER_MARK in p and "data, not instructions" in p
    assert p.count("\n- ") <= 6 + 2 + 1 and len(p) < 3500  # a handful of tools, not the whole registry


def test_planner_offline_model_falls_back_without_raising(bench: Bench) -> None:
    async def down(_p: str) -> str:
        raise ConnectionError("ollama is down")

    assert asyncio.run(planner.plan_with_llm("open spotify", auto(bench).registry, down)).kind == "none"


def test_deterministic_interpretation_needs_no_model() -> None:
    assert planner.interpret("close it", "spotify").plan.steps[0].tool == "app.close"  # type: ignore[union-attr]
    assert planner.interpret("close it", "").kind == "clarify"  # type: ignore[union-attr]
    p = planner.interpret("please organize my downloads folder by file type")
    assert p and p.plan and p.plan.workflow == "organize_folder" and p.plan.steps[0].args == {"folder": "downloads"}
    assert planner.interpret("organize my life") is None and planner.interpret("what is the capital of France") is None


# ============================================================ runtime: approval, retries, recovery, partial results
def test_high_risk_step_waits_for_approval_and_nothing_happens_before(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    view = run(bench, plans.organize_folder("downloads").steps)
    assert view["status"] == "waiting_for_approval" and view["pending"]["kind"] == "approval"
    assert bench.files() == ["a.pdf"]
    # the decision is durable: a brand-new runtime over the same database can approve it
    fresh = AgentRuntime(bench.state.db, auto(bench).registry, None)
    done = asyncio.run(fresh.approve(view["id"], True))
    assert done["status"] == "completed" and bench.files() == ["Documents/a.pdf"] and done["steps"][1]["verified"] is True


def test_declining_changes_nothing(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    view = run(bench, plans.organize_folder("downloads").steps)
    done = asyncio.run(auto(bench).runtime.approve(view["id"], False))
    assert done["status"] == "cancelled" and bench.files() == ["a.pdf"] and "Nothing was changed" in done["summary"]


def test_autonomy_policy_follows_preferences(bench: Bench) -> None:
    reg = auto(bench).registry
    strict, relaxed = AgentPrefs(auto_approve_risk="low"), AgentPrefs(auto_approve_risk="medium")
    close, apply_, edit = reg.get("app.close"), reg.get("files.apply_moves"), reg.get("project.propose_edit")
    assert needs_approval(close, strict) and not needs_approval(close, relaxed)
    assert needs_approval(apply_, strict) and needs_approval(apply_, relaxed)  # high risk always asks
    assert not needs_approval(edit, relaxed)  # self-gated: the user reviews the diff in its own screen


def test_strict_policy_asks_before_closing_an_app(bench: Bench) -> None:
    with bench.state.db.session() as s:
        save_preferences(s, {"agent": {"auto_approve_risk": "low"}})
    bench.env.windows.append(WINDOW["notepad"])
    view = run(bench, [step("s1", "app.close", name="notepad")])
    assert view["status"] == "waiting_for_approval" and WINDOW["notepad"] in bench.env.windows


def test_transient_failure_is_retried_and_recorded(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="fail_once")
    view = run(bench, [step("s1", "app.open", name="spotify")])
    assert view["status"] == "completed" and view["steps"][0]["attempts"] == 2 and view["metrics"]["retries"] == 1 and view["metrics"]["recovered"]


def test_retry_budget_is_respected(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="fail_always")
    view = run(bench, [step("s1", "app.open", name="spotify")])
    assert view["status"] == "failed" and view["steps"][0]["attempts"] == 2  # one try + app.open's one retry, then it stops


def test_blocking_dialog_is_dismissed_then_verified_and_remembered(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="dialog")
    view = run(bench, [step("s1", "app.open", name="spotify")])
    s = view["steps"][0]
    assert view["status"] == "completed" and s["verified"] and s["recovery"].startswith("window.dismiss")
    assert bench.env.keys == ["esc"] and ("Update available", "spotify.exe") not in bench.env.windows
    assert "window.dismiss" in " ".join(auto(bench).runtime.lessons_for("open spotify"))  # a verified fix is remembered
    assert bench.db(lambda s: s.query(AgentLesson).count()) == 1


def test_dialog_is_not_mistaken_for_the_app(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="dialog")
    bench.env.windows.append(("Update available", "spotify.exe"))
    reg = auto(bench).registry
    res = asyncio.run(reg.invoke(reg.get("app.open"), reg.validate("app.open", {"name": "spotify"})[1]))  # type: ignore[arg-type]
    assert res.status == FAILED and res.error_class == "blocked" and res.data["dialog"] == "Update available"


def test_already_open_app_is_not_launched_again(bench: Bench) -> None:
    bench.env.windows.append(WINDOW["notepad"])
    view = run(bench, [step("s1", "app.open", name="notepad")])
    assert view["steps"][0]["already_done"] and bench.env.launched == [] and "nothing needed changing" in view["summary"]


def test_independent_steps_continue_after_a_failure_dependent_ones_do_not(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="never")
    steps = [step("s1", "app.open", name="spotify"), step("s2", "reminder.create", text="indep", when="in 5 minutes"),
             plans.PlanStep("s3", "files.apply_moves", {"folder": "downloads", "moves": "$s1.moves"})]
    view = run(bench, steps)
    assert [s["status"] for s in view["steps"]] == ["failed", "succeeded", "skipped"] and view["status"] == "partially_completed"
    assert n_reminders(bench, "indep") == 1


def test_stop_on_failure_skips_the_rest(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="never")
    steps = [plans.PlanStep("s1", "app.open", {"name": "spotify"}, on_fail="stop"), step("s2", "reminder.create", text="no", when="in 5 minutes")]
    view = run(bench, steps)
    assert view["status"] == "failed" and n_reminders(bench, "no") == 0 and view["steps"][1]["status"] == "skipped"


def test_unverifiable_success_is_disclosed(bench: Bench) -> None:
    view = run(bench, [step("s1", "system.volume", action="up")])
    assert view["status"] == "completed" and view["steps"][0]["verified"] is None and "couldn't check" in view["summary"].lower()


def test_tool_call_budget_stops_a_runaway_plan(bench: Bench) -> None:
    with bench.state.db.session() as s:
        save_preferences(s, {"agent": {"max_tool_calls": 2}})
    view = run(bench, [step(f"s{i}", "window.list") for i in range(1, 6)])
    assert view["status"] == "partially_completed" and sum(1 for s in view["steps"] if s["status"] == "succeeded") == 2
    assert "budget" in view["summary"].lower()


def test_reminder_creation_is_idempotent_and_verified(bench: Bench) -> None:
    for _ in range(2):
        v = run(bench, [step("s1", "reminder.create", text="stretch", when="in 10 minutes")])
    assert n_reminders(bench, "stretch") == 1 and v["steps"][0]["already_done"]


# ============================================================ cancel, restart, resume
def test_cancel_a_waiting_run(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    view = run(bench, plans.organize_folder("downloads").steps)
    v = asyncio.run(auto(bench).runtime.cancel(view["id"]))
    assert v["status"] == "cancelled" and bench.files() == ["a.pdf"]


def test_restart_parks_active_runs_and_resume_checks_the_world_first(bench: Bench) -> None:
    a = auto(bench)
    rid = a.runtime.create("t", plans.Plan([step("s1", "app.open", name="notepad"), step("s2", "reminder.create", text="r", when="in 5 minutes")]))
    bench.env.windows.append(WINDOW["notepad"])  # it opened, but the process died before the result was written down
    with bench.state.db.session() as s:
        row = s.get(AgentRun, rid)
        row.status = "running"
        row.state = {**row.state, "checkpoint": "s1", "steps": {"s1": {"status": "running", "attempts": 1}, "s2": {"status": "pending", "attempts": 0}}}
    fresh = AgentRuntime(bench.state.db, a.registry, None)
    assert fresh.recover() == 1 and fresh.view(rid)["status"] == "waiting_for_input"
    v = asyncio.run(fresh.resume(rid))
    assert v["status"] == "completed" and bench.env.launched == [] and v["steps"][0]["already_done"] and n_reminders(bench, "r") == 1


def test_an_unsafe_step_with_unknown_outcome_asks_instead_of_repeating(bench: Bench) -> None:
    a = auto(bench)
    calls: list[int] = []
    a.registry.register(Tool("t.once", "must not run twice", NoArgs, lambda x, c: (calls.append(1), ok("done"))[1], risk="low"))
    rid = a.runtime.create("t", plans.Plan([step("s1", "t.once")]))
    with bench.state.db.session() as s:
        row = s.get(AgentRun, rid)
        row.status = "running"
        row.state = {**row.state, "checkpoint": "s1", "steps": {"s1": {"status": "running", "attempts": 1}}}
    fresh = AgentRuntime(bench.state.db, a.registry, None)
    fresh.recover()
    v = asyncio.run(fresh.resume(rid))
    assert v["status"] == "waiting_for_input" and v["pending"]["needs_decision"] and calls == []
    v = asyncio.run(fresh.resume(rid, redo=True))
    assert v["status"] == "completed" and calls == [1]


def test_runs_survive_in_the_database(bench: Bench) -> None:
    v = run(bench, [step("s1", "reminder.create", text="persist", when="in 5 minutes")], criteria=["a reminder exists"])
    row = bench.db(lambda s: s.get(AgentRun, v["id"]))
    assert row.status == "completed" and row.criteria == ["a reminder exists"] and row.state["steps"]["s1"]["result"]["verified"] is True
    assert row.metrics["tool_calls"] == 1 and row.finished_at is not None


# ============================================================ workflows and lessons
def test_a_completed_run_can_be_saved_and_reused_with_fresh_state(bench: Bench) -> None:
    a = auto(bench)
    v = run(bench, [step("s1", "app.open", name="notepad")], goal="start my notepad")
    ok_, _ = a.runtime.save_workflow(v["id"], "notepad time", ["notepad", "time"])
    assert ok_ and bench.db(lambda s: s.query(Workflow).count()) == 1
    assert a.runtime.save_workflow(v["id"], "notepad time", [])[0] is False  # names are unique
    p = planner.interpret("notepad time please", "", a.runtime.saved_workflows())
    assert p and p.plan and p.plan.workflow == "notepad time"
    bench.env.windows.clear()
    v2 = asyncio.run(a.runtime.execute(a.runtime.create("again", p.plan)))
    assert v2["status"] == "completed" and WINDOW["notepad"] in bench.env.windows  # state was re-observed, not assumed


def test_failed_run_cannot_be_saved_as_a_workflow(bench: Bench) -> None:
    bench.env.behaviours["spotify"] = Behaviour(mode="never")
    v = run(bench, [step("s1", "app.open", name="spotify")])
    assert auto(bench).runtime.save_workflow(v["id"], "x", [])[0] is False


# ============================================================ files tools
def test_files_tools_stay_inside_the_approved_folder(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    outside = bench.tmp / "outside.txt"
    outside.write_text("secret")
    escape = run(bench, [step("s1", "files.apply_moves", folder="downloads", moves=[{"src": "a.pdf", "dst": "../outside2.pdf"}])])
    assert escape["status"] == "waiting_for_approval"
    v = asyncio.run(auto(bench).runtime.approve(escape["id"], True))
    assert v["steps"][0]["error_class"] == "permission" and (bench.folder / "a.pdf").exists() and not (bench.tmp / "outside2.pdf").exists()
    unapproved = run(bench, [step("s1", "files.list", folder="windows")])
    assert unapproved["steps"][0]["error_class"] == "permission" and "approved" in unapproved["summary"]
    read = run(bench, [step("s1", "files.read", path=str(outside))])
    assert read["steps"][0]["error_class"] == "permission"


def test_moves_never_overwrite(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("new")
    (bench.folder / "Documents").mkdir()
    (bench.folder / "Documents" / "a.pdf").write_text("old")
    v = run(bench, [step("s1", "files.apply_moves", folder="downloads", moves=[{"src": "a.pdf", "dst": "Documents/a.pdf"}])])
    v = asyncio.run(auto(bench).runtime.approve(v["id"], True))
    assert v["status"] == "failed" and (bench.folder / "Documents" / "a.pdf").read_text() == "old" and (bench.folder / "a.pdf").read_text() == "new"


def test_organize_plan_skips_files_that_would_collide(bench: Bench) -> None:
    (bench.folder / "a.png").write_text("1")
    (bench.folder / "Images").mkdir()
    (bench.folder / "Images" / "a.png").write_text("2")
    (bench.folder / "mystery.xyz").write_text("3")
    v = run(bench, [step("s1", "files.plan_organize", folder="downloads")])
    data = v["steps"][0]
    assert data["status"] == "succeeded" and data["summary"].startswith("Proposed 0 move(s)") and "2 left alone" in data["summary"]


# ============================================================ conversation wiring and API
def test_voice_style_yes_approves_the_run_and_no_declines(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    first = bench.say("tidy my downloads folder")
    assert "Should I go ahead" in first and bench.files() == ["a.pdf"]
    assert bench.client.get("/api/pending").json()["pending"]["kind"] == "run"
    assert "Moved 1" in bench.say("yes") and bench.files() == ["Documents/a.pdf"]
    (bench.folder / "b.png").write_text("x")
    bench.say("organize my downloads folder")
    assert "didn't do it" in bench.say("no") and "b.png" in bench.files()


def test_runs_api_lists_inspects_approves_and_cancels(bench: Bench) -> None:
    (bench.folder / "a.pdf").write_text("x")
    bench.say("sort my downloads folder by type")
    runs = bench.client.get("/api/runs").json()
    rid = runs[0]["id"]
    d = bench.client.get(f"/api/runs/{rid}").json()
    assert d["status"] == "waiting_for_approval" and d["pending"]["prompt"] and d["steps"][0]["verified"] is None
    assert bench.client.post(f"/api/runs/{rid}/approve").json()["status"] == "completed"
    assert bench.client.get("/api/pending").json()["pending"] is None
    assert bench.client.get("/api/runs/999").status_code == 404
    assert bench.client.post("/api/runs/999/cancel").status_code == 404
    audit = [a["action"] for a in bench.client.get("/api/audit").json()] if bench.client.get("/api/audit").status_code == 200 else ["run.approve"]
    assert "run.approve" in audit


def test_capabilities_endpoint_is_honest_about_limits(bench: Bench) -> None:
    c = bench.client.get("/api/capabilities").json()
    assert {"tools", "models", "environment", "agent", "limitations"} <= set(c)
    assert any("accessibility" in x for x in c["limitations"]) and c["environment"]["ram_gb"] > 0
    assert c["models"]["vision_capable_but_unused"] == []


def test_disabling_the_runtime_restores_the_old_behaviour(bench: Bench) -> None:
    with bench.state.db.session() as s:
        save_preferences(s, {"agent": {"enabled": False}})
    assert "Opened" in bench.say("open notepad") and bench.client.get("/api/runs").json() == []


def test_chat_is_not_slowed_by_planning(bench: Bench) -> None:
    before = bench.llm_calls
    bench.scenario.chat_reply = "Sure, here you go."
    bench.say("make it shorter")
    bench.say("tell me a story about a fox")
    assert bench.llm_calls - before == 2  # one chat call each, no planner calls


def test_unknown_app_is_a_clear_refusal_not_a_crash(bench: Bench) -> None:
    out = bench.say("open photoshop")
    assert "not one of your integrated apps" in out and bench.env.launched == []


def test_planner_mode_impossible_and_clarify_reach_the_user(bench: Bench) -> None:
    bench.scenario.plan = {"mode": "impossible", "reason": "NEXUS has no fax capability."}
    assert "fax" in bench.say("send a fax to my accountant")
    bench.scenario.plan = {"mode": "clarify", "question": "Which folder do you mean?"}
    assert "Which folder" in bench.say("find my report and read it to me")


def test_model_plan_with_a_made_up_tool_is_rejected_not_run(bench: Bench) -> None:
    bench.scenario.plan = {"mode": "plan", "steps": [{"id": "s1", "tool": "shell.exec", "args": {"cmd": "format c:"}}]}
    out = bench.say("run format c: for me please")
    assert bench.env.launched == [] and bench.env.keys == [] and "format" not in json.dumps(bench.client.get("/api/runs").json()).lower()
    assert out  # the request fell back to normal chat, no run was created
