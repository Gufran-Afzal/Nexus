"""Latency work: persistent speech host, speaking while the model writes, caching-friendly prompts, warm-up."""

from __future__ import annotations

import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.voice.tts import SapiTTS, SentenceChunker

HOST = textwrap.dedent('''
    import base64, sys, time
    out = sys.argv[1]
    print("ready", flush=True)
    for line in sys.stdin:
        text = base64.b64decode(line.strip()).decode("utf-8")
        with open(out, "a", encoding="utf-8") as f:
            f.write(text + "\\n")
        if text.startswith("SLOW"):
            time.sleep(30)
        print("done", flush=True)
''')


@pytest.fixture
def host(tmp_path: Path):  # type: ignore[no-untyped-def]
    script, out = tmp_path / "host.py", tmp_path / "spoken.txt"
    script.write_text(HOST)
    tts = SapiTTS(command=[sys.executable, str(script), str(out)])
    yield tts, out
    tts.close()


def spoken(out: Path) -> list[str]:
    return out.read_text(encoding="utf-8").splitlines() if out.exists() else []


def test_persistent_host_speaks_many_utterances_with_one_process(host) -> None:  # type: ignore[no-untyped-def]
    tts, out = host
    tts.speak("Hello there. Ünïcode ✓ works.")
    first = tts._proc
    tts.speak("Second sentence.")
    assert tts._proc is first  # no new process per utterance
    assert spoken(out) == ["Hello there. Ünïcode ✓ works.", "Second sentence."]


def test_stop_interrupts_speech_immediately_and_recovers(host) -> None:  # type: ignore[no-untyped-def]
    tts, out = host
    t = threading.Thread(target=lambda: tts.speak("SLOW utterance"), daemon=True)
    t0 = time.time()
    t.start()
    for _ in range(100):
        if spoken(out):
            break
        time.sleep(0.02)
    tts.stop()
    t.join(timeout=5)
    assert not t.is_alive() and time.time() - t0 < 5  # did not wait the 30 s the host would have taken
    tts.speak("after interrupt")
    assert spoken(out)[-1] == "after interrupt"


def test_dead_host_is_restarted_for_the_next_utterance(host) -> None:  # type: ignore[no-untyped-def]
    tts, out = host
    tts.speak("one")
    assert tts._proc is not None
    tts._proc.kill()
    tts._proc.wait()
    tts.speak("two")  # the dead host is replaced
    assert spoken(out) == ["one", "two"]


def test_chunker_releases_sentences_as_they_complete() -> None:
    c = SentenceChunker()
    got: list[str] = []
    for tok in ["Sure", ", I can ", "help with that.", " The answer is", " 3.5 percent", ". And that", " is it."]:
        got += c.feed(tok)
    assert got == ["Sure, I can help with that.", "The answer is 3.5 percent."]  # "3.5" is not a sentence end
    assert c.flush() == ["And that is it."]


def test_chunker_starts_early_at_a_comma_for_long_first_sentences() -> None:
    c = SentenceChunker(first_soft_at=30)
    out = c.feed("Well, when you think about how the whole system fits together, ")
    assert out and out[0].startswith("Well, when you think")
    assert "." not in out[0]


def test_chunker_never_splits_or_reads_out_code() -> None:
    c = SentenceChunker()
    got = c.feed("Here is the fix.\n```python\nx = 1.\ny = 2\n")  # fence still open: code is held back
    assert got == ["Here is the fix."]
    got = c.feed("```\nThat should do it. ")
    assert got == ["I've put the code on screen. That should do it."]
    assert "x = 1" not in " ".join(got)
    c2 = SentenceChunker()
    c2.feed("Look:\n```js\nlet a")
    assert all("let a" not in p for p in c2.flush())


def test_chunker_caps_total_speech() -> None:
    c = SentenceChunker(cap=60)
    out: list[str] = []
    for _ in range(10):
        out += c.feed("This is a fairly long sentence of filler words. ")
    out += c.flush()
    assert out[-1] == "The rest is on screen." and len(out) <= 3


def test_voice_service_speaks_while_streaming_and_interrupt_drops_the_rest(client, voice_parts) -> None:  # type: ignore[no-untyped-def]
    voice = client.app.state.nexus.voice
    said: list[str] = []

    class RecTTS:
        def available(self): return True, "ok"
        def speak(self, t): said.append(t)
        def stop(self): pass

    voice._tts = RecTTS()
    voice.say("One.")
    voice.say("Two.")
    voice.wait_speech()
    assert said == ["One.", "Two."]
    voice._speech_epoch += 1  # what interrupt() does to anything queued before it
    voice._speech_q.put((voice._speech_epoch - 1, "stale"))
    with voice._speech_cv:
        voice._speech_pending += 1
    voice.say("Fresh.")
    voice.wait_speech()
    assert "stale" not in said and said[-1] == "Fresh."


def test_assistant_prompt_is_stable_so_the_model_cache_is_reused(client, provider) -> None:  # type: ignore[no-untyped-def]
    cid = client.post("/api/conversations", json={"mode": "assistant"}).json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"content": "tell me a story"})
    client.post(f"/api/conversations/{cid}/messages", json={"content": "make it shorter"})
    first, second = (provider.calls[-2]["messages"], provider.calls[-1]["messages"])
    assert first[0].content == second[0].content and "Local" not in first[0].content  # no clock in the system prompt
    assert "[Local time:" in second[-1].content  # the clock rides on the newest user message only
    assert second[1].content == "tell me a story"  # earlier turns unchanged: same prefix, so cached
    opts = provider.calls[-1]["options"]
    assert opts.num_ctx == 4096 and opts.keep_alive == "60m" and opts.max_output_tokens <= 400


def test_fast_role_is_used_for_assistant_but_not_coding(client, provider, project) -> None:  # type: ignore[no-untyped-def]
    provider.models.append("qwen2.5:3b")
    client.patch("/api/preferences", json={"model_roles": {"fast": "qwen2.5:3b"}})
    cid = client.post("/api/conversations", json={"mode": "assistant"}).json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"content": "tell me a story"})
    assert provider.calls[-1]["model"] == "qwen2.5:3b"
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    cc = client.post("/api/conversations", json={"mode": "coding", "workspace_id": ws}).json()["id"]
    client.post(f"/api/conversations/{cc}/messages", json={"content": "explain this code", "agent": False})
    assert provider.calls[-1]["model"] == "qwen2.5-coder:7b"


def test_warm_endpoint_loads_the_model_with_the_same_options(client, provider) -> None:  # type: ignore[no-untyped-def]
    before = len(provider.calls)
    r = client.post("/api/models/warm").json()
    assert r["ok"] is True and len(provider.calls) == before + 1
    call = provider.calls[-1]
    assert call["options"].max_output_tokens == 1 and call["options"].num_ctx == 4096
    assert call["options"].keep_alive == "60m"


def test_light_wake_model_is_used_only_when_already_downloaded(client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.voice.wakeword import WhisperKeywordDetector

    voice = client.app.state.nexus.voice
    stt = voice._stt
    d = voice._default_wake(voice.prefs, stt)
    assert isinstance(d, WhisperKeywordDetector) and d.stt is stt  # default: the main (more accurate) model
    voice.prefs.wake_stt_model = "tiny.en"
    d = voice._default_wake(voice.prefs, stt)
    assert d.stt is stt  # tiny.en chosen but not downloaded: nothing is fetched
    (voice.models_dir / "whisper" / "tiny.en").mkdir(parents=True)
    (voice.models_dir / "whisper" / "tiny.en" / "model.bin").write_bytes(b"x")
    d2 = voice._default_wake(voice.prefs, stt)
    assert d2.stt is not stt and d2.stt.model_name == "tiny.en"


def test_speak_endpoint_can_queue_without_waiting(client) -> None:  # type: ignore[no-untyped-def]
    voice = client.app.state.nexus.voice
    said: list[str] = []

    class RecTTS:
        def available(self): return True, "ok"
        def speak(self, t): said.append(t)
        def stop(self): pass

    voice._tts = RecTTS()
    assert client.post("/api/voice/speak", json={"text": "First sentence.", "wait": False}).json() == {"ok": True}
    assert client.post("/api/voice/speak", json={"text": "Second sentence."}).json() == {"ok": True}
    assert said == ["First sentence.", "Second sentence."]


def test_dashboard_endpoints(client) -> None:  # type: ignore[no-untyped-def]
    st = client.get("/api/system/stats").json()
    assert 0 <= st["cpu_percent"] <= 100 and st["memory"]["total_gb"] > 0
    assert st["model"]["name"] == "qwen2.5-coder:7b" and "loaded" in st["model"]
    assert client.get("/api/assistant/peek", params={"q": "15% of 240"}).json()["answer"] == "36."
    assert client.get("/api/assistant/peek", params={"q": "convert 5 km to miles"}).json()["answer"].startswith("5 km is 3.1069")
    # side-effecting or ordinary requests are never executed by a peek
    assert client.get("/api/assistant/peek", params={"q": "set a timer for 5 minutes"}).json()["answer"] is None
    assert client.get("/api/assistant/peek", params={"q": "cancel my timers"}).json()["answer"] is None
    assert client.get("/api/assistant/peek", params={"q": "explain closures"}).json()["answer"] is None
