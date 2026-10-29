"""Instant (no-model) answers: they must be right, and must not hijack ordinary questions."""

from __future__ import annotations

import datetime as dt

import pytest

from app.database.db import Database
from app.database.models import Note, Reminder, Todo
from app.services import actions, quick
from app.services.preferences import Preferences

NOW = dt.datetime(2026, 10, 16, 14, 5, 9)


@pytest.fixture
def s(tmp_path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "q.db")
    db.migrate()
    with db.session() as session:
        yield session
    db.dispose()


def ask(s, text: str):  # type: ignore[no-untyped-def]
    intent = actions.recognise(text, NOW)
    assert intent is not None, f"not recognised: {text!r}"
    return intent.kind, actions.execute(s, intent, Preferences(), now=NOW).message


@pytest.mark.parametrize("text,kind,expect", [
    ("What time is it?", "time", "2:05 PM"),
    ("what's the time", "time", "2:05 PM"),
    ("what's today's date", "date", "Friday, 16 October 2026"),
    ("what day is it", "date", "Friday"),
    ("hello", "greeting", "Good afternoon"),
    ("thanks", "thanks", "Anytime"),
    ("what can you do", "capabilities", "timers"),
    ("calculate 12 times 7", "math", "84."),
    ("what is 15% of 240", "math", "36."),
    ("what's 2 + 2", "math", "4."),
    ("what is 1,000 divided by 8", "math", "125."),
    ("12*7", "math", "84."),
    ("what is 7 / 2", "math", "3.5."),
    ("what is 2 to the power of 10", "math", "1,024."),
    ("convert 5 km to miles", "convert", "3.1069 miles"),
    ("how many cm in 3 inches", "convert", "7.62 cm"),
    ("100 celsius in fahrenheit", "convert", "212°F"),
    ("what is 72 f in c", "convert", "22.22°C"),
    ("start a focus session", "focus", "25 minutes"),
    ("start a break", "focus", "5 minutes"),
])
def test_instant_answers(s, text, kind, expect) -> None:  # type: ignore[no-untyped-def]
    k, msg = ask(s, text)
    assert k == kind and expect in msg


@pytest.mark.parametrize("text", [
    "explain how a hash map works", "what is the capital of france", "what is recursion",
    "write a function to add 2 + 2 in python",
    "what is 5 apples", "what time zone is tokyo in", "help me debug this error", "convert this file to pdf",
    "how many users are in my database", "what is the date format in python", "can you calculate my taxes",
    "fix the timer for the build", "add logging to the project", "what's the meaning of life", "my notes are messy, tidy them up",
])
def test_ordinary_questions_still_go_to_the_model(text: str) -> None:
    assert actions.recognise(text, NOW) is None, text


def test_math_is_safe_and_bounded(s) -> None:  # type: ignore[no-untyped-def]
    assert ask(s, "what is 1 / 0")[1].startswith("That's a division by zero")
    assert "too large" in ask(s, "what is 9 ** 9 ** 9")[1] or actions.recognise("what is 9 ** 9 ** 9", NOW) is None
    for bad in ("__import__('os')", "calculate open('x')", "what is 2 + abs(3)"):
        assert actions.recognise(bad, NOW) is None
    with pytest.raises(ValueError):
        quick.safe_eval("(" * 5 + "1" + ")" * 5 + "+ __import__('os')")


def test_timers_reminders_todos_notes(s) -> None:  # type: ignore[no-untyped-def]
    s.add_all([Reminder(kind="timer", text="Timer (10 min)", due_at=NOW + dt.timedelta(minutes=7, seconds=20)),
               Reminder(kind="reminder", text="call mum", due_at=NOW + dt.timedelta(hours=3)),
               Todo(text="buy milk"), Todo(text="write report"),
               Note(title="wifi", body="the wifi password is on the fridge")])
    s.flush()
    assert "10 min" in ask(s, "what timers do I have")[1]
    assert "7 min 20 s left" in ask(s, "how much time is left")[1]
    assert "call mum" in ask(s, "list my reminders")[1]
    assert "buy milk" in ask(s, "what's on my to-do list")[1]
    assert "Marked done: buy milk" in ask(s, "mark buy milk as done")[1]
    assert "write report" in ask(s, "show my todo list")[1] and "buy milk" not in ask(s, "show my todo list")[1]
    assert "wifi" in ask(s, "search my notes for wifi")[1]
    assert "No notes mention" in ask(s, "search my notes for zebra")[1]
    assert "Cancelled 1 timers" in ask(s, "cancel my timers")[1]
    assert "no pending timers" in ask(s, "what timers do I have")[1]
    assert s.query(Reminder).filter(Reminder.kind == "reminder", Reminder.status == "pending").count() == 1


def test_system_status_reports_real_numbers() -> None:
    msg = quick.system_status()
    assert msg.startswith("Your computer: CPU ") and "memory" in msg


def test_quick_answers_do_not_call_the_model(client, provider) -> None:  # type: ignore[no-untyped-def]
    conv = client.post("/api/conversations", json={"mode": "assistant"}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "what is 15% of 240", "agent": False})
    assert "36." in r.text and provider.calls == [] if hasattr(provider, "calls") else "36." in r.text


def test_memory_remember_recall_forget_and_prompt(s) -> None:  # type: ignore[no-untyped-def]
    msg = ask(s, "Remember that my wifi password is on the fridge.")[1]
    assert "I'll remember that: my wifi password is on the fridge" in msg
    ask(s, "remember my sister's name is Priya")
    assert quick.load_memories(s) == ["my wifi password is on the fridge", "my sister's name is Priya"]
    assert "Priya" in ask(s, "what do you remember about me")[1]
    assert "Forgotten: my wifi password" in ask(s, "forget the wifi password")[1]
    assert "don't have a memory" in ask(s, "forget the moon landing")[1]
    s.flush()
    assert quick.load_memories(s) == ["my sister's name is Priya"]
    for q in ("remember to call mum", "remember when we fixed that bug", "do you remember how this works", "remember"):
        assert actions.recognise(q, NOW) is None, q


def test_memories_reach_the_assistant_prompt_but_not_coding(client, provider) -> None:  # type: ignore[no-untyped-def]
    cid = client.post("/api/conversations", json={"mode": "assistant"}).json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"content": "remember that my cat is called Miso"})
    client.post(f"/api/conversations/{cid}/messages", json={"content": "tell me a story about my pet"})
    system = provider.calls[-1]["messages"][0].content
    assert "Things the user asked you to remember" in system and "my cat is called Miso" in system
