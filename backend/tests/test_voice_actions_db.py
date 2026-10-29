import array
import datetime as dt
import math
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import Reminder
from app.main import create_app
from app.services.actions import parse_duration, recognise
from app.services.scheduler import fire_due
from app.voice.audio import FRAME_SAMPLES, SAMPLE_RATE, Utterance, pcm_to_wav, rms
from app.voice.tts import SapiTTS, speakable
from app.voice.wakeword import WhisperKeywordDetector, match_phrase
from app.windows.startup import StartupManager
from tests.conftest import FakeAudio, FakeRegistry, FakeSTT, FakeTTS


# ---------------------------------------------------------------- simulated audio
def tone(seconds: float, amp: int = 8000, freq: float = 220.0) -> list[bytes]:
    frames = []
    n_frames = int(seconds * SAMPLE_RATE / FRAME_SAMPLES)
    for f in range(n_frames):
        a = array.array("h", (int(amp * math.sin(2 * math.pi * freq * (f * FRAME_SAMPLES + i) / SAMPLE_RATE))
                              for i in range(FRAME_SAMPLES)))
        frames.append(a.tobytes())
    return frames


def silence(seconds: float) -> list[bytes]:
    return [bytes(FRAME_SAMPLES * 2)] * int(seconds * SAMPLE_RATE / FRAME_SAMPLES)


def test_rms_and_wav() -> None:
    assert rms(silence(0.08)[0]) == 0
    assert rms(tone(0.08)[0]) > 5000
    assert pcm_to_wav(b"\x00\x00" * 160)[:4] == b"RIFF"


def test_utterance_end_of_speech() -> None:
    u = Utterance(silence_s=0.5)
    done = False
    for f in tone(0.8) + silence(1.0):
        done = u.feed(f)
        if done:
            break
    assert done and u.has_speech
    u2 = Utterance(silence_s=0.5, max_s=1.0)
    assert not any(u2.feed(f) for f in silence(0.9))
    assert not u2.has_speech


@pytest.mark.parametrize("text,ok,rem", [
    ("Hey Nexus, what time is it?", True, "what time is it"),
    ("hey next us open notepad", True, "open notepad"),
    ("Hey Nexis", True, ""),
    ("um hey nexus set a timer", True, "set a timer"),
    ("the weather is nice today", False, None),
    ("hey Alexa", False, None),
    ("Hay, Nexus. What's the date?", True, "what's the date"),
    ("A nexus set a timer for five minutes", True, "set a timer for five minutes"),
    ("hi nexus", True, ""),
    ("Hey nexxus open notepad", True, "open notepad"),
    ("okay nexus what time is it", False, None),  # only "hey nexus" is configured here
    ("the next user logs in", False, None),
    ("he said next is fine", False, None),
    ("", False, None),
])
def test_wake_phrase_matching(text: str, ok: bool, rem: str | None) -> None:
    m = match_phrase("hey nexus", text)
    assert (m is not None) is ok
    if ok:
        assert m[1] == rem


def test_whisper_keyword_detector_only_transcribes_speech_bursts() -> None:
    stt = FakeSTT("hey nexus what's my agenda")
    det = WhisperKeywordDetector(stt, "hey nexus")
    hits = [det.process(f) for f in silence(2.0)]
    assert not any(hits) and stt.calls == 0  # silence costs no transcription
    hits = [det.process(f) for f in tone(1.0) + silence(0.8)]
    hit = next(h for h in hits if h)
    assert hit.remainder == "what's my agenda" and stt.calls == 1
    stt.text = "turn up the music"
    assert not any(det.process(f) for f in tone(1.0) + silence(0.8))


def test_speakable_strips_markdown() -> None:
    out = speakable("## Result\nHere is **bold** and `code`.\n```python\nprint(1)\n```\n- item")
    assert "```" not in out and "**" not in out and "on screen" in out
    assert SapiTTS.rate_to_sapi(180) == 0 and SapiTTS.rate_to_sapi(400) == 8 and SapiTTS.rate_to_sapi(80) == -9


def enable_voice(client: TestClient, **extra) -> None:
    r = client.patch("/api/preferences", json={"voice": {"enabled": True, **extra}})
    assert r.status_code == 200


def test_voice_disabled_by_default_and_text_still_works(client: TestClient) -> None:
    st = client.get("/api/voice/status").json()
    assert st["enabled"] is False and st["state"] == "disabled"
    assert client.post("/api/voice/ptt/start").status_code == 409
    cid = client.post("/api/conversations", json={}).json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"content": "hi"}).status_code == 200


def test_voice_transcribe_and_spoken_answer(client: TestClient, voice_parts, provider) -> None:
    enable_voice(client)
    voice_parts["stt"].text = "set a timer for 2 minutes"
    r = client.post("/api/voice/transcribe", content=b"fake-webm-bytes")
    assert r.json()["text"] == "set a timer for 2 minutes"
    app = client.app
    ans = client.portal.call(app.state.nexus.voice.respond, "set a timer for 2 minutes")  # type: ignore[attr-defined]
    assert "Timer set" in ans
    assert voice_parts["tts"].spoken and "Timer set" in voice_parts["tts"].spoken[-1]
    acts = client.get("/api/actions").json()
    assert acts[0]["source"] == "voice"


def test_wake_word_pipeline_with_simulated_audio(settings, provider) -> None:
    provider.reply = "It is time for a break."
    stt = FakeSTT("hey nexus what should I do now")
    tts = FakeTTS()
    audio = FakeAudio(frames=silence(0.5) + tone(1.0) + silence(1.0))
    app = create_app(settings, provider=provider, startup_backend=FakeRegistry(),
                     voice_options={"audio": audio, "stt_factory": lambda p: stt, "tts_factory": lambda p: tts,
                                    "ack": lambda: None})
    with TestClient(app, headers={"X-Nexus-Client": "t"}) as c:
        c.patch("/api/preferences", json={"voice": {"enabled": True, "listening_mode": "push_to_talk"}})
        assert c.post("/api/voice/listen/start").status_code == 200
        for _ in range(100):
            if tts.spoken:
                break
            time.sleep(0.05)
        c.post("/api/voice/listen/stop")
        assert tts.spoken == ["It is time for a break."]
        convs = c.get("/api/conversations").json()
        assert convs[0]["title"] == "Voice conversation"
        msgs = c.get(f"/api/conversations/{convs[0]['id']}").json()["messages"]
        assert msgs[0]["content"] == "what should i do now" and msgs[0]["meta"]["source"] == "voice"


def test_voice_missing_components_degrade_gracefully(settings, provider) -> None:
    app = create_app(settings, provider=provider, startup_backend=FakeRegistry(),
                     voice_options={"audio": FakeAudio(ok=False), "stt_factory": lambda p: FakeSTT(ok=False),
                                    "tts_factory": lambda p: FakeTTS(), "ack": lambda: None})
    with TestClient(app, headers={"X-Nexus-Client": "t"}) as c:
        c.patch("/api/preferences", json={"voice": {"enabled": True}})
        st = c.get("/api/voice/status").json()
        assert st["microphone"]["available"] is False and st["stt"]["available"] is False
        r = c.post("/api/voice/listen/start")
        assert r.status_code == 409 and "Push-to-talk and typing still work" in r.json()["detail"]
        assert c.post("/api/voice/transcribe", content=b"x").status_code == 409
        cid = c.post("/api/conversations", json={}).json()["id"]
        assert c.post(f"/api/conversations/{cid}/messages", json={"content": "hello"}).status_code == 200


def test_mute_and_pause(client: TestClient) -> None:
    enable_voice(client)
    client.post("/api/voice/mute")
    assert client.get("/api/voice/status").json()["muted"] is True
    assert client.post("/api/voice/ptt/start").status_code == 409
    client.post("/api/voice/unmute")
    client.post("/api/system/pause")
    assert client.get("/api/voice/status").json()["paused"] is True
    client.post("/api/system/resume")


# ---------------------------------------------------------------- actions & reminders
NOW = dt.datetime(2026, 10, 15, 14, 0, 0)


@pytest.mark.parametrize("text,kind", [
    ("set a timer for 10 minutes", "timer"), ("start a 2 minute timer", "timer"), ("timer for 30 seconds", "timer"),
    ("remind me to call mum in 20 minutes", "reminder"), ("remind me at 5pm to stretch", "reminder"),
    ("remind me to buy milk", "clarify"), ("take a note: buy milk", "note"), ("add eggs to my todo list", "todo"),
    ("what's my agenda", "agenda"), ("open notepad", "open_app"), ("open github.com", "open_url"),
    ("open my projects folder", "open_folder"),
])
def test_intent_recognition(text: str, kind: str) -> None:
    i = recognise(text, NOW)
    assert i is not None and i.kind == kind


@pytest.mark.parametrize("text", ["explain recursion", "open the file main.py and explain it",
                                  "why is my test failing?", "write a timer function in python"])
def test_non_actions_go_to_model(text: str) -> None:
    assert recognise(text, NOW) is None


def test_reminder_times() -> None:
    r = recognise("remind me to call mum in 1 hour and 30 minutes", NOW)
    assert r and r.params["text"] == "call mum"
    assert dt.datetime.fromisoformat(r.params["due_at"]) == NOW + dt.timedelta(minutes=90)
    r = recognise("remind me at 9am tomorrow to check the build", NOW)
    assert r and dt.datetime.fromisoformat(r.params["due_at"]) == dt.datetime(2026, 10, 16, 9, 0)
    assert "check the build" in r.params["text"]
    assert parse_duration("half an hour") == 1800


def test_scheduler_fires_due_reminders(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    bus = EventBus()
    seen = []
    bus.add_listener(seen.append)
    with db.session() as s:
        s.add(Reminder(text="stand up", due_at=NOW - dt.timedelta(minutes=5), repeat_daily=True))
        s.add(Reminder(text="later", due_at=NOW + dt.timedelta(hours=1)))
    fired = fire_due(db, bus, NOW)
    assert len(fired) == 1 and seen[0]["text"] == "stand up" and seen[0]["late"] is True
    with db.session() as s:
        pending = s.query(Reminder).filter(Reminder.status == "pending").order_by(Reminder.due_at).all()
        assert [p.text for p in pending] == ["later", "stand up"]  # daily repeat rescheduled
    assert fire_due(db, bus, NOW) == []


def test_reminder_api(client: TestClient) -> None:
    r = client.post("/api/reminders", json={"text": "tea", "kind": "timer", "in_seconds": 60}).json()
    assert r["status"] == "pending"
    assert client.post(f"/api/reminders/{r['id']}/dismiss").json()["status"] == "cancelled"
    n = client.post("/api/notes", json={"title": "Idea", "body": "local first"}).json()
    assert client.get("/api/notes", params={"q": "local"}).json()[0]["id"] == n["id"]
    t = client.post("/api/todos", json={"text": "ship"}).json()
    assert client.patch(f"/api/todos/{t['id']}", json={"done": True}).json()["done"] is True
    assert "local data only" in client.get("/api/agenda").json()["text"]


# ---------------------------------------------------------------- database
def test_restart_persists_data(settings, provider) -> None:
    app1 = create_app(settings, provider=provider, startup_backend=FakeRegistry())
    with TestClient(app1, headers={"X-Nexus-Client": "t"}) as c:
        cid = c.post("/api/conversations", json={"title": "Persist me"}).json()["id"]
    app2 = create_app(settings, provider=provider, startup_backend=FakeRegistry())
    with TestClient(app2, headers={"X-Nexus-Client": "t"}) as c:
        assert c.get(f"/api/conversations/{cid}").json()["title"] == "Persist me"


def test_corrupt_database_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "nexus.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    db = Database(path)
    assert db.recovered_from is not None and db.recovered_from.exists()
    assert db.current_version() >= 1
    with db.session() as s:
        s.add(Reminder(text="x", due_at=NOW))


def test_backup(client: TestClient) -> None:
    client.post("/api/conversations", json={"title": "backup me"})
    path = client.post("/api/system/backup").json()["path"]
    con = sqlite3.connect(path)
    assert con.execute("select title from conversations").fetchone()[0] == "backup me"
    con.close()


def test_startup_manager_fake_registry() -> None:
    reg = FakeRegistry()
    sm = StartupManager(reg)
    assert sm.status()["enabled"] is False
    assert sm.enable()["enabled"] is True and sm.status()["matches_this_install"] is True
    assert sm.disable()["enabled"] is False


def test_run_verbs_only_launch_approved_apps() -> None:
    assert recognise("run the tests", NOW, {"notepad"}) is None
    assert recognise("start the dev server", NOW, {"notepad"}) is None
    i = recognise("run notepad", NOW, {"notepad"})
    assert i is not None and i.kind == "open_app"
    i = recognise("open regedit", NOW, {"notepad"})
    assert i is not None and i.kind == "open_app"  # reported as not approved, never launched


def test_browser_webm_upload_decodes() -> None:
    """The push-to-talk upload path: WebM/Opus from MediaRecorder -> 16 kHz float32 (PyAV 19 compatible)."""
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    import io as _io

    from app.voice.stt import decode_audio_bytes

    buf = _io.BytesIO()
    out = av.open(buf, mode="w", format="webm")
    st = out.add_stream("libopus", rate=48000)
    st.layout = "mono"
    s = (np.sin(2 * math.pi * 440 * np.arange(48000) / 48000) * 0.3).astype(np.float32)
    for i in range(0, len(s), 960):
        f = av.AudioFrame.from_ndarray(s[i:i + 960].reshape(1, -1), format="flt", layout="mono")
        f.sample_rate = 48000
        for p in st.encode(f):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    audio = decode_audio_bytes(buf.getvalue())
    assert abs(len(audio) - 16000) < 400 and 0.2 < float(abs(audio).max()) < 0.4


# ---- crash-proof probing (the Voice tab must never be able to take the server down) ----
def test_package_check_never_imports(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    from app.voice.probe import package_installed

    sys.modules.pop("sounddevice", None)
    package_installed("sounddevice")
    assert "sounddevice" not in sys.modules  # find_spec only; no native code executed
    assert package_installed("definitely_not_a_package_xyz") is False


def test_device_probe_survives_child_crash_hang_and_garbage() -> None:
    from app.voice.probe import DeviceProbe

    crash = DeviceProbe("import os; os._exit(139)", ttl=0).run()
    assert crash["ok"] is False and "crashed" in crash["error"]
    hang = DeviceProbe("import time; time.sleep(30)", timeout=0.5, ttl=0).run()
    assert hang["ok"] is False and "in time" in hang["error"]
    junk = DeviceProbe("print('hello')", ttl=0).run()
    assert junk["ok"] is False
    good = DeviceProbe("import json; print('@@NEXUS@@' + json.dumps({'ok': True, 'devices': "
                       "[{'index': 1, 'name': 'Mic', 'default': True}]}))", ttl=0).run()
    assert good["ok"] and good["devices"][0]["name"] == "Mic"


def test_device_probe_is_cached_and_serialised() -> None:
    import threading

    from app.voice.probe import DeviceProbe

    p = DeviceProbe("import time, json; time.sleep(0.3); print('@@NEXUS@@' + json.dumps({'ok': True, 'devices': []}))")
    results: list[dict] = []  # type: ignore[type-arg]
    ts = [threading.Thread(target=lambda: results.append(p.run())) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 5 and all(r is results[0] for r in results)  # one child process served all callers


def test_sounddevice_input_reports_crash_as_unavailable() -> None:
    from app.voice.audio import MicUnavailable, SoundDeviceInput
    from app.voice.probe import DeviceProbe

    mic = SoundDeviceInput(DeviceProbe("import os; os._exit(3)", ttl=0))
    ok, why = mic.available()
    assert not ok and "crashed" in why
    try:
        mic.devices()
    except MicUnavailable:
        pass
    else:
        raise AssertionError("expected MicUnavailable")


def test_watchdog_restarts_after_crash_but_not_after_clean_exit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.windows import launcher

    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    codes = iter([3221225477, -11, 0])  # access violation, SIGSEGV, then the user chose Exit
    launched: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd: list[str]) -> None:
            launched.append(cmd)

        def wait(self) -> int:
            return next(codes)

    assert launcher.watchdog("run_nexus.pyw", [], spawn=FakeProc) == 0
    assert len(launched) == 3
    assert "--child" in launched[0] and "--background" not in launched[0]
    assert "--background" in launched[1]  # restarts stay quiet: no extra browser tab


def test_watchdog_gives_up_on_crash_loop_and_respects_port_conflict(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.windows import launcher

    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    shown: list[str] = []
    monkeypatch.setattr(launcher, "_show_error", shown.append)

    class Crashing:
        def __init__(self, cmd: list[str]) -> None:
            self.n = 0

        def wait(self) -> int:
            return 139

    assert launcher.watchdog("x.pyw", [], max_restarts=3, spawn=Crashing) == 139
    assert shown and "keeps crashing" in shown[0]

    class PortBusy(Crashing):
        def wait(self) -> int:
            return launcher.EXIT_PORT_IN_USE

    assert launcher.watchdog("x.pyw", [], spawn=PortBusy) == launcher.EXIT_PORT_IN_USE


# ---- speech worker: native-library crashes must stay inside the worker process ----
def _worker(**kw):  # type: ignore[no-untyped-def]
    from app.voice.worker_client import SpeechWorker

    return SpeechWorker(extra_env={"NEXUS_WORKER_TEST": "1"}, **kw)


def test_worker_round_trip_and_real_ping() -> None:
    w = _worker()
    try:
        r = w.request("transcribe_pcm", {"models_dir": "x", "model": "base.en"}, b"\x00" * 10)
        assert r["text"] == "fake:transcribe_pcm:10:int8"
        assert w.request("transcribe_file", {}, b"abc")["text"] == "fake:transcribe_file:3:int8"
    finally:
        w.close()
    from app.voice.worker_client import SpeechWorker

    real = SpeechWorker()  # no test mode: the real worker process starts, imports nothing heavy, and answers
    try:
        assert "pid" in real.request("ping")
    finally:
        real.close()


def test_worker_crash_is_contained_reported_and_recovers() -> None:
    from app.voice.worker_client import SpeechEngineError

    w = _worker()
    try:
        w.request("ping")
        try:
            w.request("crash", {"code": 3221225477})
        except SpeechEngineError as e:
            assert "crashed" in str(e) and "diagnose.ps1" in str(e)
        else:
            raise AssertionError("expected SpeechEngineError")
        assert w.request("transcribe_file", {}, b"hi")["text"] == "fake:transcribe_file:2:int8"  # fresh worker, same server
    finally:
        w.close()


def test_worker_hang_is_killed_and_breaker_stops_crash_loops() -> None:
    from app.voice.worker_client import SpeechEngineError

    w = _worker(breaker_after=2, breaker_pause_s=60)
    try:
        for _ in range(2):
            try:
                w.request("hang", timeout=0.5)
            except SpeechEngineError as e:
                assert "did not answer" in str(e)
        assert w.broken_reason and "disabled after repeated" in w.broken_reason
        try:
            w.request("ping")
        except SpeechEngineError as e:
            assert "disabled" in str(e)
        w.reset_breaker()
        assert w.request("transcribe_file", {}, b"x")["text"]
    finally:
        w.close()


def test_worker_reports_python_errors_without_dying(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.voice.worker_client import SpeechEngineError, SpeechWorker, WorkerSTT

    w = SpeechWorker()
    try:
        stt = WorkerSTT(tmp_path, "base.en", w)
        assert stt.available()[0] is False  # model not downloaded; answered without starting any process
        try:
            w.request("nonsense")
        except SpeechEngineError as e:
            assert "unknown operation" in str(e)
        assert "pid" in w.request("ping")  # still alive after the error
    finally:
        w.close()


def test_upload_transcription_failure_reaches_the_user(client, voice_parts) -> None:  # type: ignore[no-untyped-def]
    """A failing speech engine must produce an error response, not a silent empty transcript."""
    client.patch("/api/preferences", json={"voice": {"enabled": True}})

    def boom(_data: bytes) -> str:
        raise RuntimeError("engine exploded")

    voice_parts["stt"].transcribe_file = boom
    r = client.post("/api/voice/transcribe", content=b"RIFFxxxx", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 500 and "engine exploded" in r.json()["detail"]
    assert "engine exploded" in client.get("/api/voice/status").json()["last_error"]


def test_tray_menu_builds_with_real_pystray(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression: pystray rejects menu callbacks with more than 2 parameters (ValueError at start-up)."""
    import pytest

    monkeypatch.setenv("PYSTRAY_BACKEND", "dummy")
    pytest.importorskip("pystray")
    pytest.importorskip("PIL")
    from app.windows import launcher

    sup = launcher.Supervisor()
    monkeypatch.setattr(sup, "start", lambda: None)
    monkeypatch.setattr(sup, "wait_ready", lambda timeout=30: False)
    icon = launcher.build_tray(sup, open_after_start=False)
    labels = [i.text for i in icon.menu.items if getattr(i, "text", None)]
    assert "Open NEXUS" in [str(x) for x in labels] and any("Exit" in str(x) for x in labels)
    assert any(i.default for i in icon.menu.items)  # double-click opens the UI


def test_tray_failure_falls_back_to_running_without_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.windows import launcher

    started: list[str] = []

    class FakeSup:
        stopping = type("E", (), {"is_set": lambda self: False})()
        thread = None
        on_app = None
        url = "http://x"

        def start(self) -> None:
            started.append("start")
            self.thread = type("T", (), {"is_alive": lambda self: False, "join": lambda self, t=0: None})()

        def wait_ready(self, timeout: float = 30) -> bool:
            return False

        def stop(self) -> None:
            started.append("stop")

    monkeypatch.setattr(launcher, "Supervisor", FakeSup)
    monkeypatch.setattr(launcher, "run_tray", lambda sup, open_after_start: (_ for _ in ()).throw(ValueError("bad menu")))
    monkeypatch.setattr(launcher, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(launcher.SingleInstance, "acquire", lambda self: True)
    monkeypatch.setattr(launcher.SingleInstance, "release", lambda self: None)
    monkeypatch.setattr(launcher, "configure_logging", lambda d: None)
    monkeypatch.setattr(launcher, "_enable_crash_log", lambda d: None)
    assert launcher.main(["--background"]) == 0
    assert started == ["start", "stop"]  # server started, tray failure did not crash the launcher


def test_engine_walks_the_compatibility_ladder_and_remembers_what_works(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.voice.worker_client import SpeechWorker

    state = tmp_path / "engine-profile.json"
    env = {"NEXUS_WORKER_TEST": "1", "NEXUS_WORKER_CRASH_UNLESS_FLOAT32": "1"}
    w = SpeechWorker(extra_env=env, state_file=state)
    try:
        r = w.request("transcribe_file", {}, b"hello")  # int8 crashes twice, float32 works: one call, no error
        assert r["text"] == "fake:transcribe_file:5:float32"
        assert w.profile_idx == 2 and w.degraded
        assert state.is_file()
    finally:
        w.close()
    again = SpeechWorker(extra_env=env, state_file=state)  # next start: straight to the working profile
    try:
        assert again.profile_idx == 2
        assert again.request("transcribe_file", {}, b"x")["text"].endswith(":float32")
    finally:
        again.close()


def test_ladder_exhausted_trips_the_breaker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.voice.worker_client import SpeechEngineError, SpeechWorker

    profiles = [{"name": "a", "compute_type": "int8", "cpu_threads": 1, "env": {}},
                {"name": "b", "compute_type": "int8", "cpu_threads": 1, "env": {}}]
    w = SpeechWorker(extra_env={"NEXUS_WORKER_TEST": "1", "NEXUS_WORKER_CRASH_UNLESS_FLOAT32": "1"}, profiles=profiles,
                     breaker_after=2)
    try:
        for _ in range(3):
            try:
                w.request("transcribe_file", {}, b"x")
            except SpeechEngineError:
                pass
        assert w.broken_reason is not None
    finally:
        w.close()


# ---- end-of-sentence handling: a pause is not "finished" ----
@pytest.mark.parametrize("text,complete", [
    ("Set a timer for ten minutes.", True),
    ("What's on my agenda today?", True),
    ("open notepad", True),
    ("Remind me to call the bank and", False),
    ("I need to write the report because", False),
    ("Remind me tomorrow morning,", False),
    ("Add milk to the list, um", False),
    ("Take a note about the", False),
    ("Take a note...", False),
    ("", True),
])
def test_looks_complete(text: str, complete: bool) -> None:
    from app.voice.endpoint import looks_complete

    assert looks_complete(text) is complete


def test_adaptive_threshold_ignores_room_noise_and_hears_quiet_speech() -> None:
    noisy = Utterance(silence_s=0.5, adaptive=True)
    for f in tone(2.0, amp=300):  # constant fan noise: never counts as speech
        noisy.feed(f)
    assert not noisy.has_speech
    quiet_room = Utterance(silence_s=0.5, adaptive=True)
    for f in silence(0.4) + tone(0.8, amp=900):  # a quiet speaker in a quiet room is still heard
        quiet_room.feed(f)
    assert quiet_room.has_speech


class ScriptedSTT:
    def __init__(self, answers: list[str]) -> None:
        self.answers, self.calls = answers, 0

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]

    def transcribe_file(self, data: bytes) -> str:
        return self.transcribe_pcm(data)


def _voice_with(client, frames, stt):  # type: ignore[no-untyped-def]
    import threading

    voice = client.app.state.nexus.voice
    voice.audio = FakeAudio(frames=list(frames))
    voice._stt = stt
    voice.prefs.pause_tolerance_s = 1.0
    return voice, threading.Event()


def test_long_sentence_with_a_mid_sentence_pause_is_not_cut_off(client) -> None:  # type: ignore[no-untyped-def]
    stt = ScriptedSTT(["remind me to call mom and", "remind me to call mom and buy milk on the way home."])
    frames = tone(0.8) + silence(1.2) + tone(0.8) + silence(1.2)  # pause > pause_tolerance, sentence unfinished
    voice, ev = _voice_with(client, frames, stt)
    text = voice._record_utterance(ev)
    assert text == "remind me to call mom and buy milk on the way home."
    assert stt.calls == 2  # one check at the first pause, one on the whole sentence


def test_finished_sentence_is_dispatched_after_one_pause(client) -> None:  # type: ignore[no-untyped-def]
    stt = ScriptedSTT(["set a timer for ten minutes."])
    voice, ev = _voice_with(client, tone(0.8) + silence(1.3), stt)
    assert voice._record_utterance(ev) == "set a timer for ten minutes."
    assert stt.calls == 1


def test_unfinished_forever_still_ends_and_smart_endpoint_can_be_turned_off(client) -> None:  # type: ignore[no-untyped-def]
    stt = ScriptedSTT(["and"])
    voice, ev = _voice_with(client, tone(0.5) + silence(6.0), stt)
    assert voice._record_utterance(ev) == "and"  # gives up after the allowed extensions; never waits forever
    stt2 = ScriptedSTT(["remind me to"])
    voice2, ev2 = _voice_with(client, tone(0.8) + silence(1.3), stt2)
    voice2.prefs.smart_endpoint = False
    assert voice2._record_utterance(ev2) == "remind me to" and stt2.calls == 1


def test_words_after_the_wake_phrase_are_joined_with_the_continuation(client) -> None:  # type: ignore[no-untyped-def]
    stt = ScriptedSTT(["and then email the team."])
    voice, ev = _voice_with(client, silence(0.1) + tone(0.8) + silence(1.5), stt)
    assert voice._record_utterance(ev, prefix="remind me to review the budget") == (
        "remind me to review the budget and then email the team.")
    stt2 = ScriptedSTT(["never used"])
    voice2, ev2 = _voice_with(client, silence(2.0), stt2)  # nothing more said: the prefix alone is the request
    assert voice2._record_utterance(ev2, prefix="what time is it") == "what time is it" and stt2.calls == 0


def test_voice_defaults_listen_for_the_wake_phrase_once_enabled() -> None:
    from app.services.preferences import VoicePrefs

    v = VoicePrefs()
    assert v.enabled is False  # still opt-in
    assert v.listening_mode == "wake_word" and v.smart_endpoint and 0.6 <= v.pause_tolerance_s <= 5


def test_several_wake_phrases_can_be_configured() -> None:
    assert match_phrase("hey nexus, ok nexus", "okay nexus what time is it")  # "okay" is close enough to "ok"
    m = match_phrase("hey nexus, ok nexus", "ok nexus what time is it")
    assert m and m[1] == "what time is it"
    m = match_phrase("hey nexus, nexus", "Nexus, set a timer")
    assert m and m[1] == "set a timer"


def test_wake_gate_adapts_to_quiet_voices_and_steady_noise() -> None:
    quiet = FakeSTT("hey nexus open notepad")
    det = WhisperKeywordDetector(quiet, "hey nexus")
    hits = [det.process(f) for f in silence(1.0) + tone(1.0, amp=700) + silence(0.8)]  # soft voice: far below the old fixed gate
    assert any(hits) and quiet.calls == 1
    noisy = FakeSTT("hey nexus")
    det2 = WhisperKeywordDetector(noisy, "hey nexus")
    assert not any(det2.process(f) for f in tone(6.0, amp=300) + silence(1.0)) and noisy.calls == 0  # a steady hum is not speech


def test_wake_detector_keeps_pre_roll_and_ignores_clicks() -> None:
    seen: list[int] = []

    class Rec(FakeSTT):
        def transcribe_pcm(self, pcm: bytes) -> str:
            seen.append(len(pcm))
            return "hey nexus"

    det = WhisperKeywordDetector(Rec(""), "hey nexus")
    list(map(det.process, silence(1.0)))
    hits = [det.process(f) for f in tone(0.8) + silence(0.7)]
    assert any(hits)
    assert seen[0] >= int(16000 * 2 * (0.8 + 0.3))  # speech plus the audio from just before it started
    clicks = Rec("")
    det2 = WhisperKeywordDetector(clicks, "hey nexus")
    assert not any(det2.process(f) for f in silence(1.0) + tone(0.08) + silence(1.0)) and seen[1:] == []


def test_wake_attempts_are_reported_with_what_was_heard() -> None:
    log: list[tuple[str, float, bool]] = []
    stt = FakeSTT("hey next is open notepad")
    det = WhisperKeywordDetector(stt, "hey nexus", on_attempt=lambda t, sc, ok: log.append((t, sc, ok)))
    [det.process(f) for f in silence(0.5) + tone(1.0) + silence(0.8)]
    assert log and log[0][0] == "hey next is open notepad" and isinstance(log[0][2], bool)
    stt.text = "what a lovely day"
    [det.process(f) for f in tone(1.0) + silence(0.8)]
    assert log[-1] == ("what a lovely day", 0.0, False)


def test_wake_detector_prefers_the_wake_specific_decode_when_available() -> None:
    class Both(FakeSTT):
        def __init__(self) -> None:
            super().__init__("plain")
            self.wake_calls: list[str] = []

        def transcribe_wake(self, pcm: bytes, phrase: str) -> str:
            self.wake_calls.append(phrase)
            return "hey nexus"

    stt = Both()
    det = WhisperKeywordDetector(stt, "hey nexus, ok nexus")
    assert any(det.process(f) for f in tone(0.8) + silence(0.8)) and stt.wake_calls == ["hey nexus, ok nexus"] and stt.calls == 0


# ---------------------------------------------------------------- accuracy helpers, stream sharing, mic test
def test_normalize_audio_lifts_quiet_clips_and_leaves_silence_alone() -> None:
    np = pytest.importorskip("numpy")
    from app.voice.stt import normalize_audio

    t = np.arange(16000, dtype=np.float32)
    quiet = (0.02 * np.sin(t / 20)).astype(np.float32) + 0.1  # quiet, with a DC offset
    out = normalize_audio(quiet)
    assert abs(float(np.mean(out))) < 0.01 and 0.4 < float(np.max(np.abs(out))) <= 1.0
    assert float(np.max(np.abs(normalize_audio(np.zeros(100, dtype=np.float32))))) == 0.0
    assert normalize_audio(np.zeros(0, dtype=np.float32)).size == 0


def test_clean_transcript_drops_hallucinations_and_wake_noise() -> None:
    from app.voice.stt import clean_transcript

    assert clean_transcript("Thanks for watching!") == ""
    assert clean_transcript("you", "wake") == "" and clean_transcript("you", "final") == "you"
    assert clean_transcript(" Hey Nexus, open notepad ") == "Hey Nexus, open notepad"


def test_decode_options_are_valid_faster_whisper_arguments() -> None:
    import inspect

    from app.voice.stt import decode_options

    pytest.importorskip("faster_whisper")
    from faster_whisper import WhisperModel

    params = inspect.signature(WhisperModel.transcribe).parameters
    for mode in ("wake", "final"):
        opts = decode_options(mode, "hey nexus, ok nexus")
        assert set(opts) <= set(params), set(opts) - set(params)
    assert decode_options("wake", "hey nexus, ok nexus")["hotwords"] == "Hey Nexus"
    assert decode_options("final")["beam_size"] > decode_options("wake")["beam_size"] - 1


def test_frame_stream_can_be_resumed_drained_and_closed() -> None:
    import queue
    import threading

    from app.voice.audio import FrameStream

    class Dev:
        stopped = closed = False

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    q: queue.Queue[bytes] = queue.Queue()
    for i in range(5):
        q.put(bytes([i]))
    stop, dev = threading.Event(), Dev()
    fs = FrameStream(dev, q, stop)
    assert next(iter(fs)) == b"\x00"
    for f in fs:  # break and resume: the same stream keeps its place
        assert f == b"\x01"
        break
    assert next(fs) == b"\x02"
    assert fs.drain() == 2 and q.empty()
    stop.set()
    assert list(fs) == []
    fs.close()
    assert dev.stopped and dev.closed


def test_utterance_gives_up_when_nobody_speaks_but_not_while_someone_does() -> None:
    u = Utterance(silence_s=1.0, adaptive=True, wait_s=2.0)
    done = [u.feed(f) for f in silence(2.4)]
    assert done[-1] and not u.has_speech
    v = Utterance(silence_s=1.0, adaptive=True, wait_s=2.0)
    assert not any(v.feed(f) for f in silence(0.5) + tone(2.2))  # speaking past wait_s keeps going


def test_bare_wake_phrase_waits_for_a_command_then_gives_up(client) -> None:  # type: ignore[no-untyped-def]
    voice, ev = _voice_with(client, silence(0.3) + silence(7.0), ScriptedSTT(["x"]))
    t0 = time.time()
    assert voice._record_utterance(ev, prefix="") == ""
    assert time.time() - t0 < 5  # simulated audio is instant; it ended on the give-up timeout, not on max length


def test_record_utterance_can_read_from_an_already_open_stream(client) -> None:  # type: ignore[no-untyped-def]
    voice, ev = _voice_with(client, [], ScriptedSTT(["set a timer for five minutes."]))
    shared = iter(tone(0.8) + silence(1.3))
    assert voice._record_utterance(ev, frames=shared) == "set a timer for five minutes."
    assert voice.audio._frames == []  # no second microphone was opened


def test_microphone_test_reports_what_was_heard_and_gives_advice(client) -> None:  # type: ignore[no-untyped-def]
    enable_voice(client, listening_mode="push_to_talk")  # otherwise the wake loop would consume the simulated audio
    voice = client.app.state.nexus.voice
    voice.audio = FakeAudio(frames=silence(0.3) + tone(0.8, amp=600) + silence(1.3))
    voice._stt = ScriptedSTT(["hey nexus set a timer"])
    r = client.post("/api/voice/test")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["heard"] == "hey nexus set a timer" and 0 < d["peak"] < 0.05
    assert any("quiet" in a for a in d["advice"])
    voice.audio = FakeAudio(frames=silence(8.0))
    d2 = client.post("/api/voice/test").json()
    assert d2["heard"] == "" and any("didn't hear" in a for a in d2["advice"])


def test_microphone_test_without_voice_enabled_is_a_clean_error(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/api/voice/test")
    assert r.status_code == 409 and r.json()["detail"]


def test_status_includes_wake_attempts(client) -> None:  # type: ignore[no-untyped-def]
    voice = client.app.state.nexus.voice
    voice._note_wake_attempt("hey next", 0.7, False)
    a = client.get("/api/voice/status").json()["wake_attempts"]
    assert a[0]["heard"] == "hey next" and a[0]["accepted"] is False


# ---------------------------------------------------------------- follow-up listening (no wake phrase needed)
def test_follow_up_answers_replies_without_the_wake_phrase_then_ends_on_silence(client) -> None:  # type: ignore[no-untyped-def]
    stt = ScriptedSTT(["and what about tomorrow.", "and the day after."])
    frames = tone(0.8) + silence(1.3) + tone(0.8) + silence(1.3) + silence(10.0)
    voice, ev = _voice_with(client, frames, stt)
    voice.prefs.follow_up_s = 8
    heard: list[str] = []
    voice._dispatch = heard.append  # type: ignore[method-assign]
    voice._listen_stop = ev
    voice._follow_up(iter(list(voice.audio._frames)))
    assert heard == ["and what about tomorrow.", "and the day after."]


def test_follow_up_stops_on_thanks_and_on_noise_and_when_off(client) -> None:  # type: ignore[no-untyped-def]
    voice, ev = _voice_with(client, [], ScriptedSTT(["thanks."]))
    voice.prefs.follow_up_s = 8
    heard: list[str] = []
    voice._dispatch = heard.append  # type: ignore[method-assign]
    voice._listen_stop = ev
    voice._follow_up(iter(tone(0.8) + silence(1.3)))
    assert heard == []
    voice._stt = ScriptedSTT(["background tv chatter"])
    voice.last_confidence = None
    orig = voice._final_text

    def low(pcm: bytes) -> str:
        text = orig(pcm)
        voice.last_confidence = 0.1
        return text

    voice._final_text = low  # type: ignore[method-assign]
    voice._follow_up(iter(tone(0.8) + silence(1.3)))
    assert heard == []
    voice._final_text = orig  # type: ignore[method-assign]
    voice.prefs.follow_up_s = 0
    voice._follow_up(iter(tone(0.8) + silence(1.3)))
    assert heard == []


def test_live_captions_publish_partial_text_only_with_a_downloaded_light_model(client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    voice, ev = _voice_with(client, [], ScriptedSTT(["set a timer for five minutes."]))
    assert voice._partial_stt_for() is None  # tiny.en is not downloaded in the test environment: no captions, no download
    seen: list[dict] = []
    client.app.state.nexus.bus.add_listener(lambda e: seen.append(e) if e.get("type") == "caption" else None)
    voice._partial_stt = ScriptedSTT(["set a timer"])  # type: ignore[assignment]
    voice._partial_name = voice.prefs.partial_stt_model
    voice._caption(b"\x00\x00" * 16000, None)
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    assert seen and seen[0]["text"] == "set a timer"
