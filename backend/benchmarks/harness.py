"""Runs a scenario against a fresh NEXUS app (real API, real services, fake desktop and scripted model)."""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.database.models import Contact
from app.main import create_app
from app.retrieval.context import estimate_tokens
from app.services import actions, skills
from app.services.preferences import IntegratedApp, save_preferences
from benchmarks.env import FakeEnv
from tests.conftest import FakeAudio, FakeProvider, FakeRegistry, FakeSTT, FakeTTS

APPS = [
    IntegratedApp(name="Notepad", kind="exe", target="C:\\Windows\\notepad.exe", process="notepad.exe"),
    IntegratedApp(name="Spotify", kind="exe", target="C:\\Users\\me\\AppData\\Roaming\\Spotify\\Spotify.exe", process="spotify.exe"),
    IntegratedApp(name="WhatsApp", kind="exe", target="C:\\Program Files\\WhatsApp\\WhatsApp.exe", process="whatsapp.exe"),
    IntegratedApp(name="Google Chrome", aliases=["chrome"], kind="exe", target="C:\\Program Files\\Google\\Chrome\\chrome.exe",
                  process="chrome.exe"),
]
PLANNER_MARK = "You plan tool calls for a local desktop automation agent."
ROUTER_MARK = "You turn a spoken request into ONE tool call"


@dataclass
class Outcome:
    scenario: str
    success: bool = False
    honest: bool = True
    replies: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    seconds: float = 0.0
    tools_used: list[str] = field(default_factory=list)
    tool_args: list[dict[str, Any]] = field(default_factory=list)
    steps_verified: list[bool | None] = field(default_factory=list)
    approvals: int = 0
    duplicates: int = 0
    recovered: bool | None = None
    status: str = ""
    note: str = ""


async def _no_sleep(_s: float) -> None:
    return None


class Bench:
    def __init__(self, scenario: Any, *, legacy: bool) -> None:
        self.scenario = scenario
        self.legacy = legacy
        self.tmp = Path(tempfile.mkdtemp(prefix="nexus-bench-"))
        self.env = FakeEnv()
        self.provider = FakeProvider()
        self.llm_calls = 0
        self.tokens = 0
        self.provider.reply = self._reply
        settings = Settings(data_dir=self.tmp / "data", background_tasks=False, frontend_dist=self.tmp / "nofrontend", command_timeout_s=30)
        self.app = create_app(settings, provider=self.provider, startup_backend=FakeRegistry(), launcher=self.env.launcher,
                              voice_options={"audio": FakeAudio(), "stt_factory": lambda p: FakeSTT(), "tts_factory": lambda p: FakeTTS(),
                                             "ack": lambda: None})
        self.client = TestClient(self.app, headers={"X-Nexus-Client": "bench"})
        self.client.__enter__()
        self.state = self.app.state.nexus
        actions.DEFAULT_SYSTEM = self.env  # type: ignore[assignment]
        a = self.state.conversations.assistant
        a.system = self.env
        a.runner = skills.Runner(self.env.launcher, self.env, sleep=self.env.sleep)
        auto = self.state.conversations.automation
        auto.registry.ctx.system, auto.registry.ctx.sleep = self.env, self.env.sleep
        auto.runtime.sleep = _no_sleep
        self.folder = self.tmp / "Downloads"
        self.folder.mkdir()
        with self.state.db.session() as s:
            save_preferences(s, {"integrated_apps": [x.model_dump() for x in APPS], "approved_folders": [str(self.folder)],
                                 "smart_routing": True, "agent": {"enabled": not legacy}})
            s.add(Contact(name="Mom", aliases=["mother"], phone="15551230001", email="mom@example.com"))
        self.conv = self.client.post("/api/conversations", json={}).json()["id"]

    # ---- scripted model: planner JSON for the new runtime, router JSON, else chat text
    def _reply(self, messages: list[Any], _opts: Any = None) -> str:
        prompt = "\n".join(m.content for m in messages)
        sc = self.scenario
        if PLANNER_MARK in prompt:
            text = json.dumps(sc.plan) if sc.plan is not None else json.dumps({"mode": "impossible", "reason": "No tool can do this."})
        elif ROUTER_MARK in prompt:
            text = sc.router_reply
        else:
            text = sc.chat_reply
        self.llm_calls += 1
        self.tokens += estimate_tokens(prompt) + estimate_tokens(text)
        return text

    def say(self, text: str) -> str:
        r = self.client.post(f"/api/conversations/{self.conv}/messages", json={"content": text})
        out = []
        for block in r.text.strip().split("\n\n"):
            data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), "{}")
            d = json.loads(data)
            if "text" in d:
                out.append(d["text"])
        return "".join(out)

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.folder)).replace("\\", "/") for p in self.folder.rglob("*") if p.is_file())

    def db(self, fn: Callable[[Any], Any]) -> Any:
        with self.state.db.session() as s:
            return fn(s)

    def close(self) -> None:
        self.client.__exit__(None, None, None)


SUCCESS_WORDS = re.compile(r"\b(opened|started|done|sent|moved|created|saved|completed|organized|reminder saved|switched|closed)\b", re.I)
HEDGES = re.compile(r"(couldn't|could not|didn't|did not|not |unable|can't|cannot|failed|waiting|haven't|no window|isn't|n't)", re.I)


def claims_success(reply: str) -> bool:
    return bool(SUCCESS_WORDS.search(reply)) and not HEDGES.search(reply)


def run_chat_scenario(sc: Any, *, legacy: bool) -> Outcome:
    b = Bench(sc, legacy=legacy)
    out = Outcome(sc.id)
    t0 = time.time()
    try:
        sc.setup(b)
        for turn in sc.turns:
            out.replies.append(b.say(turn))
        out.seconds = time.time() - t0
        ok = bool(sc.check(b))
        reply = " ".join(out.replies)
        if sc.expect_refusal:
            ok = ok and bool(re.search(sc.expect_refusal, reply, re.I))
        out.success = ok
        out.honest = ok or not claims_success(out.replies[-1] if out.replies else "")
        out.llm_calls, out.tokens = b.llm_calls, b.tokens
        out.duplicates = int(sc.duplicates(b)) if sc.duplicates else 0
        runs = b.client.get("/api/runs").json() if not legacy else []
        for r in reversed(runs):  # every run this scenario started, oldest first
            d = b.client.get(f"/api/runs/{r['id']}").json()
            out.status = d["status"]
            for st in d["steps"]:
                if st.get("tool") and st["status"] != "skipped":
                    out.tools_used.append(st["tool"])
                    out.tool_args.append(st.get("args") or {})
                    out.steps_verified.append(st.get("verified"))
            out.approvals += sum(1 for st in d["steps"] if st.get("approved"))
            out.recovered = bool(out.recovered) or bool(d.get("metrics", {}).get("recovered"))
    finally:
        b.close()
    return out
