"""python -m benchmarks.demo > ../docs/DEMOS.md : end-to-end transcripts through the real API (fake desktop, scripted model)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.agent import plan as plans
from app.agent.runtime import AgentRuntime
from app.database.models import AgentRun
from benchmarks.env import WINDOW, Behaviour
from benchmarks.harness import Bench
from benchmarks.scenarios import Scenario


def steps_table(view: dict[str, Any]) -> str:
    rows = ["| step | tool | result | checked |", "|---|---|---|---|"]
    for s in view["steps"]:
        chk = {True: "yes: " + (s["observed"] or "")[:50], False: "FAILED: " + (s["observed"] or s["summary"])[:60], None: "not checkable"}[s["verified"]]
        if s["status"] in ("skipped", "pending"):
            chk = "-"
        extra = f" (recovery: {s['recovery'].split(':')[0]})" if s["recovery"] else ""
        rows.append(f"| {s['id']} | `{s['tool']}` | {s['status']}, {s['attempts']} attempt(s){extra} | {chk} |")
    return "\n".join(rows)


def latest(b: Bench) -> dict[str, Any]:
    return b.client.get(f"/api/runs/{b.client.get('/api/runs').json()[0]['id']}").json()


def talk(b: Bench, text: str) -> None:
    print(f"\n**You:** {text}\n\n**NEXUS:** {b.say(text)}")


def demo_a() -> None:
    print("## A. A multi-step job with an approval: organise Downloads\n")
    b = Bench(Scenario("A", "a"), legacy=False)
    for n in ("report.pdf", "holiday.png", "notes.txt", "logo.png", "setup.exe", "weird.xyz"):
        (b.folder / n).write_text("x")
    print("Folder before:", ", ".join(b.files()))
    talk(b, "organize my downloads folder by file type")
    print("\nFolder while waiting for the answer:", ", ".join(b.files()), "(nothing moved)")
    run = latest(b)
    print(f"\nRun #{run['id']} is **{run['status']}**, pending: _{run['pending']['prompt'][:110]}_")
    talk(b, "yes")
    print("\nFolder after:", ", ".join(b.files()))
    print("\n" + steps_table(latest(b)))
    b.close()


def demo_b() -> None:
    print("\n## B. An unexpected dialog covers the app: dismiss, re-check, remember\n")
    b = Bench(Scenario("B", "b"), legacy=False)
    b.env.behaviours["spotify"] = Behaviour(mode="dialog")
    talk(b, "open spotify")
    print("\n" + steps_table(latest(b)))
    print("\nWindows now:", b.env.windows, "| keys pressed:", b.env.keys)
    print("Lesson stored for the planner:", b.state.conversations.automation.runtime.lessons_for("open spotify"))
    b.close()


def demo_c() -> None:
    print("\n## C. It cannot honestly claim success: the app never appears\n")
    b = Bench(Scenario("C", "c", plan={"mode": "plan", "steps": [
        {"id": "s1", "tool": "app.open", "args": {"name": "spotify"}},
        {"id": "s2", "tool": "reminder.create", "args": {"text": "take a break", "when": "in 5 minutes"}}]}), legacy=False)
    b.env.behaviours["spotify"] = Behaviour(mode="never")
    talk(b, "open spotify and remind me to take a break in 5 minutes")
    v = latest(b)
    print(f"\nRun status: **{v['status']}**\n\n" + steps_table(v))
    b.close()


def demo_d() -> None:
    print("\n## D. NEXUS restarts in the middle of a run\n")
    b = Bench(Scenario("D", "d"), legacy=False)
    a = b.state.conversations.automation
    plan = plans.Plan([plans.PlanStep("s1", "app.open", {"name": "notepad"}), plans.PlanStep("s2", "app.open", {"name": "spotify"}),
                       plans.PlanStep("s3", "reminder.create", {"text": "stand up", "when": "in 20 minutes"})])
    rid = a.runtime.create("open notepad and spotify then remind me to stand up", plan)
    b.env.windows[:] = [WINDOW["notepad"], WINDOW["spotify"]]
    with b.state.db.session() as s:
        row = s.get(AgentRun, rid)
        row.status = "running"
        st = dict(row.state)
        st.update(checkpoint="s2", steps={"s1": {"status": "succeeded", "attempts": 1, "result": {"status": "succeeded", "summary": "Started Notepad.", "verified": True, "data": {}}},
                                          "s2": {"status": "running", "attempts": 1}, "s3": {"status": "pending", "attempts": 0}})
        row.state = st
    print("The process died while step s2 (open Spotify) was running; Spotify had already opened but the result was never written down.\n")
    fresh = AgentRuntime(b.state.db, a.registry, None, sleep=a.runtime.sleep)
    print(f"After restart, `recover()` parked {fresh.recover()} run: status **{fresh.view(rid)['status']}**, "
          f"asking: _{fresh.view(rid)['pending']['prompt']}_")
    v = asyncio.run(fresh.resume(rid))
    print(f"\nAfter `resume`: status **{v['status']}**. Spotify launched again: **{len(b.env.launched) > 0}**.\n\n" + steps_table(v))
    b.close()


def demo_e() -> None:
    print("\n## E. Planning with the (scripted) model: several actions from one sentence\n")
    plan = {"mode": "plan", "criteria": ["Notepad and Spotify are open", "a reminder exists"], "steps": [
        {"id": "s1", "tool": "app.open", "args": {"name": "notepad"}}, {"id": "s2", "tool": "app.open", "args": {"name": "spotify"}},
        {"id": "s3", "tool": "reminder.create", "args": {"text": "stretch", "when": "in 10 minutes"}}]}
    b = Bench(Scenario("E", "e", plan=plan), legacy=False)
    talk(b, "open notepad and spotify, then remind me to stretch in 10 minutes")
    talk(b, "now close it")
    print("\n" + steps_table(latest(b)))
    print("\nModel calls for the whole conversation:", b.llm_calls, "(one planner call; \"close it\" needed none)")
    b.close()


if __name__ == "__main__":
    import logging

    logging.disable(logging.CRITICAL)
    print("# End-to-end demonstrations (0.7.0)\n\nGenerated by `python -m benchmarks.demo`. The desktop is a fake (windows that appear, "
          "dialogs that block); the model is scripted; everything else is the real code path (HTTP API, conversation, planner validation, "
          "runtime, tools, database).")
    for d in (demo_a, demo_b, demo_c, demo_d, demo_e):
        d()
    _ = json
