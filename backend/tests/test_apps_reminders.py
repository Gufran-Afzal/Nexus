"""Smart reminders (recurrence, ringing, snooze, actions), natural-language time, app discovery/integration and the
voice/text commands that drive apps and Windows. Windows-only effects are checked through fakes."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import Reminder, utcnow
from app.services import actions, appcontrol, appintents
from app.services.alerts import AlertHandler, spoken_text
from app.services.preferences import IntegratedApp, Preferences
from app.services.scheduler import fire_due, ring_again, snooze
from app.services.when import describe, next_occurrence, parse_when
from app.windows.appcatalog import SCAN_SCRIPT, AppCatalog, parse_scan
from app.windows.sysctl import SystemControl, SystemControlError
from tests.test_api_chat import new_conv, send

NOW = dt.datetime(2026, 10, 15, 14, 0, 0)  # a Thursday


# ---------------------------------------------------------------- natural-language time
@pytest.mark.parametrize("text,due,repeat,rest", [
    ("call mum in 20 minutes", dt.datetime(2026, 10, 15, 14, 20), "", "call mum"),
    ("check the build at 9am tomorrow", dt.datetime(2026, 10, 16, 9, 0), "", "check the build"),
    ("stretch at 5pm", dt.datetime(2026, 10, 15, 17, 0), "", "stretch"),
    ("meeting at 5", dt.datetime(2026, 10, 15, 17, 0), "", "meeting"),  # the next 5 o'clock
    ("every weekday at 9 standup", dt.datetime(2026, 10, 16, 9, 0), "weekdays", "standup"),
    ("every monday at 9am stand up", dt.datetime(2026, 10, 19, 9, 0), "weekly", "stand up"),
    ("gym on friday at 6", dt.datetime(2026, 10, 16, 18, 0), "", "gym"),
    ("dentist next monday at 10:30am", dt.datetime(2026, 10, 19, 10, 30), "", "dentist"),
    ("call mom tonight", dt.datetime(2026, 10, 15, 20, 0), "", "call mom"),
    ("sync tomorrow morning", dt.datetime(2026, 10, 16, 9, 0), "", "sync"),
    ("drink water every day", dt.datetime(2026, 10, 16, 9, 0), "daily", "drink water"),
    ("water the plants every day at 3", dt.datetime(2026, 10, 15, 15, 0), "daily", "water the plants"),
])
def test_parse_when(text: str, due: dt.datetime, repeat: str, rest: str) -> None:
    w = parse_when(text, NOW)
    assert w is not None and (w.due, w.repeat, w.rest) == (due, repeat, rest)


def test_parse_when_needs_a_time_and_next_occurrence_rules() -> None:
    assert parse_when("buy milk", NOW) is None
    fri = dt.datetime(2026, 10, 16, 9, 0)
    assert next_occurrence(fri, "weekdays", fri) == dt.datetime(2026, 10, 19, 9, 0)  # skips the weekend
    assert next_occurrence(dt.datetime(2026, 1, 31, 9), "monthly", dt.datetime(2026, 2, 1)) == dt.datetime(2026, 2, 28, 9)
    assert next_occurrence(fri, "", fri) is None
    assert describe(dt.datetime(2026, 10, 16, 9, 0), "weekdays", NOW) == "tomorrow at 09:00, every weekday"


# ---------------------------------------------------------------- recognition
KNOWN = {"chrome", "google chrome", "spotify", "visual studio code", "vs code", "outlook", "teams"}


@pytest.mark.parametrize("text,kind", [
    ("remind me every weekday at 9 to check email", "reminder"), ("remind me to stretch tomorrow morning", "reminder"),
    ("set an alarm for 7am", "reminder"), ("wake me up at 6:30", "reminder"), ("snooze for 10 minutes", "snooze"),
    ("snooze", "snooze"), ("stop the alarm", "dismiss_alarm"), ("dismiss", "dismiss_alarm"),
    ("set a pasta timer for 10 minutes", "timer"), ("can you set a timer for 5 minutes", "timer"),
    ("close chrome", "close_app"), ("switch to spotify", "focus_app"), ("search for cats in chrome", "app_search"),
    ("play lo-fi beats on spotify", "app_play"), ("open my projects folder in vs code", "app_open_folder"),
    ("google python dataclasses", "web_search"), ("search the web for best pizza", "web_search"),
    ("write an email to alex@example.com about the report", "compose_email"),
    ("pause the music", "media"), ("next song", "media"), ("previous track", "media"),
    ("volume up", "volume"), ("turn it up", "none"), ("set the volume to 30", "volume"), ("mute the sound", "volume"),
    ("lock my computer", "lock"), ("take a screenshot", "screenshot"), ("show the desktop", "show_desktop"),
    ("open bluetooth settings", "settings_page"), ("open settings", "settings_page"),
    ("open spotify", "open_app"), ("open visual studio code", "open_app"), ("launch teams", "open_app"),
    ("start spotifi", "open_app"),  # a close spelling still finds the app
])
def test_recognises_assistant_commands(text: str, kind: str) -> None:
    i = actions.recognise(text, NOW, known_apps=KNOWN)
    assert (i.kind if i else "none") == kind, (text, i)


@pytest.mark.parametrize("text", [
    "search for a bug in my code", "close the loop on that issue", "play the guitar like a pro", "open a pull request for this",
    "switch to a different approach", "show me how recursion works", "start the server and run the tests",
    "find the memory leak in chrome devtools", "why does my volume slider not update", "next steps for the project",
    "stop using global state", "pause and think about the design", "lock the file before writing", "run the tests",
    "go to the next section of the readme", "mute",
])
def test_ordinary_requests_still_reach_the_model(text: str) -> None:
    assert actions.recognise(text, NOW, known_apps=KNOWN) is None


def test_reminder_with_app_action_and_labels() -> None:
    i = actions.recognise("remind me to join the standup at 9:55 tomorrow and open teams", NOW, known_apps=KNOWN)
    assert i and i.params["action"] == {"type": "open_app", "name": "teams"} and i.params["text"] == "join the standup"
    j = actions.recognise("remind me to join the call at 3pm and open notepad", NOW, known_apps=KNOWN)
    assert j and j.params["action"] is None  # not an integrated app -> no action; the reminder still works
    t = actions.recognise("set a pasta timer for 10 minutes", NOW)
    assert t and t.params["text"] == "pasta"
    assert actions.recognise("remind me to buy milk", NOW).kind == "clarify"  # type: ignore[union-attr]


# ---------------------------------------------------------------- scheduler: repeat, ring, snooze
def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "t.db")


def test_firing_reschedules_repeats_and_reports_details(tmp_path: Path) -> None:
    db, bus, seen = _db(tmp_path), EventBus(), []
    bus.add_listener(seen.append)
    with db.session() as s:
        s.add(Reminder(text="standup", due_at=dt.datetime(2026, 10, 16, 9, 0), repeat="weekdays",
                       action={"type": "open_app", "name": "teams"}))
    assert len(fire_due(db, bus, dt.datetime(2026, 10, 16, 9, 0, 5))) == 1
    ev = seen[0]
    assert ev["repeat"] == "weekdays" and ev["ring"] == 1 and ev["persistent"] and ev["action"]["name"] == "teams"
    with db.session() as s:
        nxt = s.query(Reminder).filter(Reminder.status == "pending").one()
        assert nxt.due_at == dt.datetime(2026, 10, 19, 9, 0) and nxt.action == {"type": "open_app", "name": "teams"}


def test_unanswered_reminder_rings_again_then_stops(tmp_path: Path) -> None:
    db, bus, seen = _db(tmp_path), EventBus(), []
    bus.add_listener(seen.append)
    with db.session() as s:
        s.add(Reminder(text="pills", due_at=NOW - dt.timedelta(minutes=1)))
    fire_due(db, bus, NOW)
    t = utcnow()
    assert ring_again(db, bus, t + dt.timedelta(seconds=10)) == []  # too soon
    for n in range(2, 7):
        t += dt.timedelta(seconds=61)
        assert len(ring_again(db, bus, t)) == 1
        assert seen[-1]["ring"] == n
    t += dt.timedelta(seconds=61)
    assert ring_again(db, bus, t) == []  # six rings in total, then it stays quiet
    with db.session() as s:
        assert s.query(Reminder).filter(Reminder.text == "pills").one().status == "fired"


def test_non_persistent_and_dismissed_reminders_do_not_ring_again(tmp_path: Path) -> None:
    db, bus = _db(tmp_path), EventBus()
    with db.session() as s:
        s.add(Reminder(text="once", due_at=NOW - dt.timedelta(minutes=1), persistent=False))
        s.add(Reminder(text="done", due_at=NOW - dt.timedelta(minutes=1)))
    fire_due(db, bus, NOW)
    with db.session() as s:
        s.query(Reminder).filter(Reminder.text == "done").one().status = "dismissed"
    assert ring_again(db, bus, utcnow() + dt.timedelta(minutes=5)) == []


def test_snooze_moves_it_back_to_pending(tmp_path: Path) -> None:
    db, bus = _db(tmp_path), EventBus()
    with db.session() as s:
        s.add(Reminder(text="tea", due_at=NOW - dt.timedelta(minutes=1)))
    rid = fire_due(db, bus, NOW)[0]
    r = snooze(db, rid, 10, NOW)
    assert r and r.status == "pending" and r.due_at == NOW + dt.timedelta(minutes=10) and r.ring_count == 0
    assert snooze(db, 9999, 5) is None


def test_database_upgrade_keeps_existing_reminders(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    Database(path).dispose()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE reminders")
    con.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, kind VARCHAR(20), text TEXT, due_at DATETIME, "
                "repeat_daily BOOLEAN, status VARCHAR(20), fired_at DATETIME, created_at DATETIME)")
    con.execute("INSERT INTO reminders VALUES (1,'reminder','keep me','2026-10-16 09:00:00',1,'pending',NULL,'2026-10-15 10:00:00')")  # noqa: E501
    con.execute("DELETE FROM schema_version WHERE version > 2")
    con.commit()
    con.close()
    db = Database(path)
    with db.session() as s:
        r = s.query(Reminder).one()
        assert r.text == "keep me" and r.repeat == "daily" and r.persistent is True and r.ring_count == 0 and r.action is None


# ---------------------------------------------------------------- alerts
class SpyVoice:
    def __init__(self) -> None:
        self.said: list[str] = []

    def announce(self, text: str) -> bool:
        self.said.append(text)
        return True


def test_alert_handler_speaks_and_runs_the_action(tmp_path: Path) -> None:
    db, bus = _db(tmp_path), EventBus()
    launched: list[Any] = []
    events: list[dict] = []
    bus.add_listener(events.append)
    voice, played = SpyVoice(), []
    with db.session() as s:
        from app.services.preferences import save_preferences
        save_preferences(s, {"integrated_apps": [IntegratedApp(name="Teams", kind="uwp", target="MSTeams!MSTeams").model_dump()]})
    h = AlertHandler(db, bus, voice, lambda t, k: launched.append((t, k)), play=lambda: played.append(1))
    ev = {"type": "reminder", "id": 1, "kind": "reminder", "text": "join standup", "ring": 1, "action": {"type": "open_app", "name": "teams"}}  # noqa: E501
    h._handle(ev)
    assert voice.said == ["Reminder: join standup"] and played and launched == [("MSTeams!MSTeams", "uwp")]
    assert any(e["type"] == "reminder_action" and e["ok"] for e in events)
    h._handle({**ev, "ring": 2})  # ringing again never re-runs the action
    assert len(launched) == 1 and voice.said[-1].startswith("Reminder, again")
    h._handle({**ev, "action": {"type": "open_app", "name": "gone"}, "ring": 1})
    assert any(e["type"] == "reminder_action" and not e["ok"] for e in events)
    assert spoken_text({"kind": "timer", "text": "Timer (10 min)", "ring": 1}).startswith("Your timer is done")
    assert spoken_text({"kind": "alarm", "text": "Alarm", "ring": 1}).strip() == "Alarm."


# ---------------------------------------------------------------- app discovery
SAMPLE = [
    {"k": "lnk", "name": "Google Chrome", "target": r"C:\Program Files\Google\Chrome\Application\chrome.exe", "args": "", "path": r"C:\x\Google Chrome.lnk"},  # noqa: E501
    {"k": "lnk", "name": "Visual Studio Code", "target": r"C:\Users\a\AppData\Local\Programs\Microsoft VS Code\Code.exe", "args": "", "path": "p"},  # noqa: E501
    {"k": "lnk", "name": "Uninstall Foo", "target": r"C:\Foo\unins000.exe", "args": "", "path": "p"},
    {"k": "lnk", "name": "Foo Documentation", "target": r"C:\Foo\doc.exe", "args": "", "path": "p"},
    {"k": "lnk", "name": "Control Panel", "target": r"C:\Windows\System32\control.exe", "args": "", "path": "p"},
    {"k": "lnk", "name": "Disk Cleanup", "target": r"C:\Windows\cleanmgr.msc", "args": "", "path": r"C:\x\Disk Cleanup.lnk"},
    {"k": "lnk", "name": "Some Site", "target": "https://example.com", "args": "", "path": r"C:\x\Some Site.lnk"},
    {"k": "start", "name": "Spotify", "target": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
    {"k": "start", "name": "Google Chrome", "target": r"Chrome"},
    {"k": "reg", "name": "chrome", "target": r"C:\Program Files\Google\Chrome\Application\chrome.exe"},
    {"k": "reg", "name": "winword", "target": r"C:\Office\WINWORD.EXE"},
    {"k": "lnk", "name": "", "target": "x", "args": "", "path": "p"},
]


def test_parse_scan_cleans_and_dedupes() -> None:
    apps = {a.name: a for a in parse_scan(SAMPLE)}
    assert apps["Google Chrome"].kind == "exe" and apps["Google Chrome"].process == "chrome.exe"
    assert apps["Visual Studio Code"].process == "Code.exe"
    assert apps["Spotify"].kind == "uwp" and apps["Spotify"].process == ""
    assert apps["Disk Cleanup"].kind == "shortcut" and apps["Disk Cleanup"].target.endswith(".lnk")
    assert "Winword" in apps and apps["Winword"].source == "registry"
    for bad in ("Uninstall Foo", "Foo Documentation", "Some Site"):
        assert bad not in apps
    assert [a.name for a in parse_scan(SAMPLE)].count("Google Chrome") == 1  # lnk + start-apps + registry = one entry
    assert {"Notepad", "Calculator", "File Explorer", "Windows Settings"} <= set(apps)  # essentials are always offered


def test_catalog_scan_cache_and_failure_modes(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(script: str) -> str:
        calls.append(script)
        return json.dumps(SAMPLE)

    cat = AppCatalog(tmp_path / "c.json", runner=runner, platform="win32")
    assert cat.scanned_at is None and any(a.name == "Notepad" for a in cat.apps)  # essentials before the first scan
    cat.scan()
    assert calls == [SCAN_SCRIPT] and cat.error is None and cat.by_id("spotify")
    assert AppCatalog(tmp_path / "c.json", runner=runner, platform="win32").by_id("spotify")  # cache survives a restart
    one = AppCatalog(tmp_path / "one.json", runner=lambda s: json.dumps(SAMPLE[0]), platform="win32")
    one.scan()  # PowerShell prints a bare object (not a list) when there is exactly one result
    assert one.by_id("google-chrome")

    def boom(_s: str) -> str:
        raise RuntimeError("PowerShell was not found")

    bad = AppCatalog(tmp_path / "b.json", runner=boom, platform="win32")
    bad.scan()
    assert bad.error and "failed" in bad.error and bad.by_id("notepad")  # still usable
    lin = AppCatalog(tmp_path / "l.json", runner=runner, platform="linux")
    lin.scan()
    assert lin.error and "only works on Windows" in lin.error and len(calls) == 1  # never runs PowerShell off Windows
    assert cat.guess("chrome") and cat.guess("chrome").name == "Google Chrome"  # type: ignore[union-attr]
    assert cat.guess("visual studio") and cat.guess("zzzz nonexistent") is None


# ---------------------------------------------------------------- app matching and commands
def _prefs(*apps: IntegratedApp, **kw: Any) -> Preferences:
    return Preferences(integrated_apps=list(apps), allowed_apps={}, **kw)


CHROME = IntegratedApp(name="Google Chrome", aliases=["browser"], kind="exe", target=r"C:\Chrome\chrome.exe", process="chrome.exe")  # noqa: E501
SPOTIFY = IntegratedApp(name="Spotify", kind="uwp", target="Spotify!App")
CODE = IntegratedApp(name="Visual Studio Code", aliases=["vs code", "code"], kind="exe", target=r"C:\VSCode\Code.exe", process="Code.exe")  # noqa: E501


def test_app_matching() -> None:
    p = _prefs(CHROME, SPOTIFY, CODE, IntegratedApp(name="Microsoft Word", kind="exe", target="winword.exe"),
               IntegratedApp(name="WordPad", kind="exe", target="write.exe"))
    for said, want in [("chrome", "Google Chrome"), ("the browser", "Google Chrome"), ("spotifi", "Spotify"),
                       ("vs code", "Visual Studio Code"), ("word", "Microsoft Word"), ("microsoft word", "Microsoft Word"),
                       ("code app", "Visual Studio Code")]:
        ref = appcontrol.find(p, said)
        assert ref and ref.name == want, (said, ref)
    assert appcontrol.find(p, "photoshop") is None
    disabled = _prefs(IntegratedApp(name="Spotify", kind="uwp", target="x", enabled=False))
    assert appcontrol.find(disabled, "spotify") is None and appcontrol.app_names(disabled) == set()
    assert appcontrol.find(Preferences(), "notepad").target == "notepad.exe"  # type: ignore[union-attr]  # old list still works


class FakeSystem(SystemControl):
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.focus_result: str | None = "Spotify Premium"
        self.close_result = (True, "SUCCESS")

    def key(self, name: str, times: int = 1) -> None:
        self.calls.append(("key", name, times))

    def set_volume(self, percent: int) -> None:
        self.calls.append(("volume", percent))

    def lock(self) -> None:
        self.calls.append(("lock",))

    def open_uri(self, uri: str) -> None:
        self.calls.append(("uri", uri))

    def show_desktop(self) -> None:
        self.calls.append(("desktop",))

    def focus(self, needles: list[str]) -> str | None:
        self.calls.append(("focus", needles))
        return self.focus_result

    def close_process(self, image: str) -> tuple[bool, str]:
        self.calls.append(("close", image))
        return self.close_result


def run(text: str, prefs: Preferences, system: FakeSystem | None = None) -> tuple[str, str, list, FakeSystem]:
    system = system or FakeSystem()
    launched: list[tuple[Any, str]] = []
    intent = actions.recognise(text, NOW, known_apps=appcontrol.app_names(prefs))
    assert intent is not None, text
    out_status, out_msg = appintents.run(intent.kind, intent.params, prefs, lambda t, k: launched.append((t, k)), system) \
        if intent.kind in appintents.KINDS else ("?", "?")
    return out_status, out_msg, launched, system


def test_open_close_and_switch() -> None:
    p = _prefs(CHROME, SPOTIFY)
    st, msg, _l, sysm = run("close chrome", p)
    assert st == "succeeded" and sysm.calls == [("close", "chrome.exe")]
    sysm2 = FakeSystem()
    sysm2.close_result = (False, "ERROR: The process \"chrome.exe\" not found.")
    st, msg, *_ = run("close chrome", p, sysm2)
    assert st == "failed" and "doesn't seem to be running" in msg
    st, msg, *_ = run("close spotify", p)
    assert st == "unsupported" and "Store apps" in msg  # honest: no process name to close by
    st, msg, launched, sysm = run("switch to spotify", p)
    assert st == "succeeded" and sysm.calls[0][0] == "focus" and launched == []
    s2 = FakeSystem()
    s2.focus_result = None
    st, msg, launched, _ = run("switch to spotify", p, s2)
    assert "wasn't open" in msg and launched == [("Spotify!App", "uwp")]


def test_app_specific_commands() -> None:
    p = _prefs(CHROME, SPOTIFY, CODE, approved_folders=["C:\\Users\\a\\projects"])
    st, msg, launched, _ = run("search for cats and dogs in chrome", p)
    assert st == "succeeded" and launched == [([r"C:\Chrome\chrome.exe", "https://duckduckgo.com/?q=cats+and+dogs"], "app")]
    st, msg, launched, _ = run("play lo-fi beats on spotify", p)
    assert launched == [("spotify:search:lo-fi%20beats", "uri")] and "can't press play" in msg
    st, msg, launched, _ = run("open my projects folder in vs code", p)
    assert st == "succeeded" and launched == [([r"C:\VSCode\Code.exe", r"C:\Users\a\projects"], "app")]
    st, msg, launched, _ = run("open my secrets folder in vs code", p)
    assert st == "unsupported" and launched == []  # only approved folders
    st, msg, launched, _ = run("google python dataclasses", p)
    assert launched == [("https://duckduckgo.com/?q=python+dataclasses", "url")]
    q = _prefs(CHROME, search_url="https://www.bing.com/search?q={q}")
    assert run("google x y", q)[2] == [("https://www.bing.com/search?q=x+y", "url")]
    bad = _prefs(CHROME, search_url="http://evil.test/?q={q}")  # a non-https template is ignored
    assert run("google x", bad)[2][0][0].startswith("https://duckduckgo.com")
    st, msg, launched, _ = run("write an email to alex@example.com about the q3 report", p)
    assert launched == [("mailto:alex%40example.com?subject=the%20q3%20report", "uri")] and "Nothing is sent" in msg
    p2 = _prefs(SPOTIFY)
    assert run("search for cats in spotify", p2)[0] == "unsupported"  # not a browser


def test_system_commands() -> None:
    p = _prefs()
    cases = {"pause the music": ("key", "play_pause", 1), "next song": ("key", "next", 1), "previous track": ("key", "prev", 1),
             "volume up": ("key", "up", 5), "volume down": ("key", "down", 5), "mute the sound": ("key", "mute", 1),
             "turn the volume up by 20": ("key", "up", 10), "set the volume to 30": ("volume", 30),
             "lock my computer": ("lock",), "show the desktop": ("desktop",),
             "take a screenshot": ("uri", "ms-screenclip:"), "open bluetooth settings": ("uri", "ms-settings:bluetooth"),
             "open settings": ("uri", "ms-settings:"), "open wifi settings": ("uri", "ms-settings:network-wifi")}
    for text, call in cases.items():
        st, _msg, _l, sysm = run(text, p)
        assert st == "succeeded" and sysm.calls == [call], (text, sysm.calls)
    assert actions.recognise("set the volume to 300", NOW).params["percent"] == 100  # type: ignore[union-attr]


def test_system_control_refuses_cleanly_off_windows() -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("this checks the non-Windows behaviour")
    with pytest.raises(SystemControlError):
        SystemControl().key("mute")
    st, msg = appintents.run("lock", {}, _prefs(), lambda t, k: None, SystemControl())
    assert st == "unsupported" and "only works on Windows" in msg


# ---------------------------------------------------------------- HTTP API end to end
@pytest.fixture
def win_catalog(client: TestClient, tmp_path: Path) -> AppCatalog:
    cat = AppCatalog(tmp_path / "cat.json", runner=lambda s: json.dumps(SAMPLE), platform="win32")
    client.app.state.nexus.catalog = cat
    return cat


SP = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"


def test_apps_panel_flow(client: TestClient, win_catalog: AppCatalog, launched: list) -> None:
    d = client.get("/api/apps").json()
    assert {a["name"] for a in d["integrated"]} == {"Notepad", "Calculator", "Paint"}  # the old list became real entries
    assert client.get("/api/preferences").json()["allowed_apps"] == {}
    d = client.post("/api/apps/scan").json()
    assert d["error"] is None and any(a["name"] == "Spotify" for a in d["discovered"])
    assert next(a for a in d["discovered"] if a["name"] == "Notepad")["integrated"] is True
    r = client.post("/api/apps/integrate", json={"id": "spotify", "aliases": ["music"]})
    assert r.status_code == 200 and r.json()["profile"] == "spotify" and "Play lo-fi beats on Spotify" in r.json()["examples"]
    assert client.post("/api/apps/integrate", json={"name": "chrome"}).json()["name"] == "Google Chrome"  # by name only
    assert client.post("/api/apps/integrate", json={"name": "no such app"}).status_code == 422
    assert client.post("/api/apps/integrate", json={"id": "missing"}).status_code == 404
    assert client.post("/api/apps/integrate", json={"path": "C:/nope/x.exe"}).status_code == 422
    assert client.post("/api/apps/integrate", json={}).status_code == 422
    assert client.post("/api/apps/integrate-many", json={"ids": ["google-chrome", "spotify", "winword", "bogus"]}).json()["added"] == 1  # noqa: E501
    # the integrated app works from chat by voice or text, including its alias
    cid = new_conv(client)
    send(client, cid, "open spotify")
    send(client, cid, "open music")
    assert launched[-2:] == [(SP, "uwp")] * 2
    ev = send(client, cid, "open photoshop")
    assert ev[0][1]["action"]["status"] == "unsupported" and len(launched) == 2
    # disable / rename aliases / remove
    assert client.patch("/api/apps/integrated/spotify", json={"enabled": False}).json()["enabled"] is False
    ev = send(client, cid, "open spotify")
    assert ev[0][1]["action"]["status"] == "unsupported"
    assert client.patch("/api/apps/integrated/spotify", json={"enabled": True, "aliases": ["tunes"]}).json()["aliases"] == ["tunes"]  # noqa: E501
    assert client.post("/api/apps/integrated/spotify/open").status_code == 200 and launched[-1] == (SP, "uwp")
    assert client.post("/api/apps/integrated/spotify/close").status_code == 409  # Store app: no process name
    assert client.delete("/api/apps/integrated/spotify").json() == {"removed": True}
    assert client.delete("/api/apps/integrated/spotify").status_code == 404


def test_integrating_by_path_requires_a_real_exe(client: TestClient, win_catalog: AppCatalog, tmp_path: Path) -> None:
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ")
    r = client.post("/api/apps/integrate", json={"path": str(exe), "name": "My Tool"})
    assert r.status_code == 200 and r.json()["process"] == "tool.exe" and r.json()["source"] == "manual"
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    assert client.post("/api/apps/integrate", json={"path": str(txt)}).status_code == 422


def test_reminder_api_repeat_snooze_natural_and_action(client: TestClient, win_catalog: AppCatalog) -> None:
    r = client.post("/api/reminders", json={"text": "stand up", "in_seconds": 600, "repeat": "weekdays"}).json()
    assert r["repeat"] == "weekdays" and r["persistent"] is True
    assert client.post("/api/reminders", json={"text": "x", "in_seconds": 60, "open_app": "teams"}).status_code == 422
    n = client.post("/api/reminders/natural", json={"text": "call mum every monday at 9am"})
    assert n.status_code == 200 and n.json()["repeat"] == "weekly" and n.json()["text"] == "call mum"
    assert client.post("/api/reminders/natural", json={"text": "buy milk"}).status_code == 422
    sn = client.post(f"/api/reminders/{r['id']}/snooze", json={"minutes": 5}).json()
    assert sn["status"] == "pending" and sn["ring_count"] == 0
    assert client.post("/api/reminders/9999/snooze").status_code == 404


def test_voice_style_snooze_and_dismiss_through_chat(client: TestClient) -> None:
    state = client.app.state.nexus
    with state.db.session() as s:
        s.add(Reminder(text="ringing", due_at=NOW, status="fired", fired_at=utcnow(), ring_count=2))
    cid = new_conv(client)
    assert send(client, cid, "snooze for 15 minutes")[0][1]["action"]["status"] == "succeeded"
    with state.db.session() as s:
        r = s.query(Reminder).one()
        assert r.status == "pending" and (r.due_at - dt.datetime.now()).total_seconds() > 14 * 60
        r.status = "fired"
    assert send(client, cid, "stop the alarm")[0][1]["action"]["status"] == "succeeded"
    with state.db.session() as s:
        assert s.query(Reminder).one().status == "dismissed"
    assert send(client, cid, "dismiss")[0][1]["action"]["status"] == "unsupported"  # nothing ringing
