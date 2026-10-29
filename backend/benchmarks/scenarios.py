"""The benchmark: 14 representative tasks. Chat scenarios run through the real conversation API (so they run on both the
0.6.0 pipeline and the new runtime); runtime scenarios exercise the runtime directly and have no 0.6.0 equivalent.

The model is scripted. That measures orchestration (routing, tool use, verification, recovery, honesty), NOT how good Qwen 2.5
is at writing plans; that must be measured on the real model (see docs/AUTOMATION.md)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.database.models import Reminder
from benchmarks.env import WINDOW, Behaviour
from benchmarks.harness import Bench

DEFAULT_CHAT = "I'm not sure how to do that from here."


@dataclass
class Scenario:
    id: str
    title: str
    turns: list[str] = field(default_factory=list)
    setup: Callable[[Bench], None] = lambda b: None
    check: Callable[[Bench], bool] = lambda b: True
    plan: dict[str, Any] | None = None  # what the (scripted) planner model answers
    router_reply: str = '{"tool": "none", "args": {}}'
    chat_reply: str = DEFAULT_CHAT
    expect_refusal: str = ""  # reply must match this (an honest "can't")
    expect_tools: list[str] = field(default_factory=list)
    expect_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_injected: bool = False
    runtime_only: bool = False
    duplicates: Callable[[Bench], int] | None = None
    mid: dict[int, Callable[[Bench], bool]] = field(default_factory=dict)
    run: Callable[[Bench], Any] | None = None  # runtime scenarios: custom driver returning a RuntimeResult


def has_window(b: Bench, key: str) -> bool:
    return WINDOW[key] in b.env.windows


def reminders(b: Bench, text: str = "") -> int:
    return int(b.db(lambda s: s.query(Reminder).filter(Reminder.text.ilike(f"%{text}%")).count()))


def make_files(b: Bench) -> None:
    for n in ("a.pdf", "b.png", "c.txt", "d.png"):
        (b.folder / n).write_text("x")


ORGANISED = ["Documents/a.pdf", "Documents/c.txt", "Images/b.png", "Images/d.png"]


def _desktop(b: Bench) -> None:
    d = b.tmp / "Desktop"
    d.mkdir()
    (d / "x.png").write_text("x")


def _fail(key: str, mode: str) -> Callable[[Bench], None]:
    def f(b: Bench) -> None:
        b.env.behaviours[key] = Behaviour(mode=mode)
    return f


CHAT: list[Scenario] = [
    Scenario("S01", "Single tool: open an app", ["open notepad"], check=lambda b: has_window(b, "notepad"),
             expect_tools=["app.open"]),
    Scenario("S02", "Multi-step workflow with approval: organise a folder", ["organize my downloads folder by file type", "yes"],
             setup=make_files, check=lambda b: b.files() == ORGANISED, expect_tools=["files.plan_organize", "files.apply_moves"],
             mid={0: lambda b: b.files() == sorted(["a.pdf", "b.png", "c.txt", "d.png"])}),
    Scenario("S03", "Several related actions: two apps and a reminder", ["open notepad and spotify, then remind me to stretch in 10 minutes"],
             plan={"mode": "plan", "criteria": ["Notepad and Spotify are open", "a reminder exists"],
                   "steps": [{"id": "s1", "tool": "app.open", "args": {"name": "notepad"}},
                             {"id": "s2", "tool": "app.open", "args": {"name": "spotify"}},
                             {"id": "s3", "tool": "reminder.create", "args": {"text": "stretch", "when": "in 10 minutes"}}]},
             check=lambda b: has_window(b, "notepad") and has_window(b, "spotify") and reminders(b, "stretch") == 1,
             expect_tools=["app.open", "app.open", "reminder.create"], expect_args={"reminder.create": {"text": "stretch"}}),
    Scenario("S04", "Follow-up that refers to earlier context", ["open spotify", "now close it"],
             check=lambda b: not has_window(b, "spotify") and "spotify.exe" in b.env.closed, expect_tools=["app.open", "app.close"]),
    Scenario("S05", "Missing capability: must say so", ["send a fax to my accountant"],
             plan={"mode": "impossible", "reason": "NEXUS has no fax capability."},
             chat_reply="I can't send faxes from here.", expect_refusal=r"can't|cannot|unable|no .*capab|not able|don't have",
             check=lambda b: not b.env.launched and not b.env.keys),
    Scenario("S07", "Recoverable application failure (starts on the second try)", ["open spotify"], setup=_fail("spotify", "fail_once"),
             check=lambda b: has_window(b, "spotify"), failure_injected=True, expect_tools=["app.open"]),
    Scenario("S08", "Unexpected GUI state: an update dialog covers the app", ["open spotify"], setup=_fail("spotify", "dialog"),
             check=lambda b: has_window(b, "spotify") and ("Update available", "spotify.exe") not in b.env.windows,
             failure_injected=True, expect_tools=["app.open"]),
    Scenario("S09", "Duplicate action: the same reminder asked for twice", ["remind me to stretch at 5pm", "remind me to stretch at 5pm"],
             check=lambda b: reminders(b, "stretch") == 1, duplicates=lambda b: max(0, reminders(b, "stretch") - 1)),
    Scenario("S11", "Denied permission: a folder that was never approved", ["organize my desktop folder by file type"], setup=_desktop,
             chat_reply="I don't have access to your files.", expect_refusal=r"approved|permission|not allowed|access",
             check=lambda b: (b.tmp / "Desktop" / "x.png").exists()),
    Scenario("S13", "Needs approval: sending a message", ["message mom on whatsapp I'll be late", "yes"],
             check=lambda b: "enter" in b.env.keys and any("15551230001" in t for t, _ in b.env.launched),
             mid={0: lambda b: "enter" not in b.env.keys}),
    Scenario("S14", "Cannot honestly be marked complete: the app never appears", ["open spotify and remind me to take a break in 5 minutes"],
             setup=_fail("spotify", "never"),
             plan={"mode": "plan", "steps": [{"id": "s1", "tool": "app.open", "args": {"name": "spotify"}},
                                             {"id": "s2", "tool": "reminder.create", "args": {"text": "take a break", "when": "in 5 minutes"}}]},
             expect_refusal=r"(couldn't|could not|didn't|did not|not|no window|never).{0,60}(spotify|window|open|appear)|spotify.{0,60}(not|n't|never|no window)",
             check=lambda b: reminders(b, "take a break") == 1 and not has_window(b, "spotify")),
]
