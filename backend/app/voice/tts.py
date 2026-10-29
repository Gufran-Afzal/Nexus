"""Local text-to-speech.

- "sapi": Windows' built-in speech synthesizer (System.Speech via PowerShell). No download, fully offline.
  Runs as a child process so speech can be interrupted instantly by terminating it.
- "piper": a user-installed Piper executable + voice model (see docs/VOICE.md). Run as a separate program.
- "none": speech output disabled.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class TTSUnavailable(Exception):
    pass


def speakable(text: str, max_chars: int = 1200) -> str:
    """Turn markdown into something reasonable to say aloud."""
    t = re.sub(r"```.*?```", " I've put the code on screen. ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_~>|]+", "", t)
    t = re.sub(r"^\s*[-+]\s+", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        t = cut[: cut.rfind(".") + 1] or cut
        t += " The rest is on screen."
    return t


_FENCE = re.compile(r"```.*?```", re.S)
_BOUNDARY = re.compile(r"(?:[.!?…]+[\"')\]]*(?=\s)|\n+)")
_SOFT = re.compile(r"[,;:](?=\s)")


class SentenceChunker:
    """Cuts a streaming answer into speakable pieces so speech can start while the model is still writing.

    Text is released at sentence ends (never inside a code block). To start talking sooner the very first piece may
    end at a comma once it is long enough. Total speech is capped; the rest stays on screen.
    """

    def __init__(self, first_soft_at: int = 45, min_chunk: int = 8, cap: int = 900) -> None:
        self.raw = ""
        self.done = 0  # raw[:done] has been released
        self.said = 0
        self.first_soft_at, self.min_chunk, self.cap = first_soft_at, min_chunk, cap
        self.capped = False

    def _release(self, piece: str) -> str | None:
        if self.capped:
            return None
        spoken = speakable(piece, max_chars=10_000)
        if not spoken:
            return None
        if self.said + len(spoken) > self.cap:
            self.capped = True
            return "The rest is on screen."
        self.said += len(spoken)
        return spoken

    def feed(self, text: str) -> list[str]:
        self.raw += text
        out: list[str] = []
        while True:
            cut = self.raw.find("```", self.done)
            fenced_open = cut != -1 and len(re.findall("```", self.raw[cut:])) % 2 == 1
            stable_end = cut if fenced_open else len(self.raw)
            masked = _FENCE.sub(lambda m: "X" * len(m.group(0)), self.raw[self.done:stable_end])
            end = -1
            for m in _BOUNDARY.finditer(masked):
                end = m.end()
            if end == -1 and self.said == 0 and not out and len(masked) >= self.first_soft_at:
                softs = [m.end() for m in _SOFT.finditer(masked) if m.end() >= self.first_soft_at // 2]
                end = softs[-1] if softs else -1
            if end <= 0 or len(masked[:end].strip()) < self.min_chunk:
                return out
            piece = self.raw[self.done:self.done + end]
            self.done += end
            r = self._release(piece)
            if r:
                out.append(r)

    def flush(self) -> list[str]:
        rest = self.raw[self.done:]
        self.done = len(self.raw)
        if rest.count("```") % 2 == 1:
            rest += "\n```"
        r = self._release(rest) if rest.strip() else None
        return [r] if r else []


class TTS(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def speak(self, text: str) -> None: ...  # blocking; call from a worker thread
    def stop(self) -> None: ...


class NullTTS:
    def available(self) -> tuple[bool, str]:
        return False, "Speech output is disabled"

    def speak(self, text: str) -> None:
        return None

    def stop(self) -> None:
        return None


class _ProcTTS:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def _run(self, argv: list[str], stdin: bytes, env: dict[str, str] | None = None) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        with self._lock:
            self._stopped = False
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, creationflags=flags, env=env)
        proc = self._proc
        try:
            _, err = proc.communicate(stdin, timeout=600)
            if proc.returncode not in (0, None) and not self._stopped:
                raise TTSUnavailable(err.decode(errors="replace")[-300:])
        finally:
            with self._lock:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()


# One long-lived PowerShell hosts the synthesizer. Starting PowerShell and loading System.Speech takes about a
# second, so doing it once (and ahead of time) removes that delay from every spoken reply. Protocol: one line per
# request (UTF-8 text, base64-encoded); the host answers "done" when it has finished speaking it.
SAPI_SERVER = (
    "[Console]::InputEncoding = [Text.Encoding]::UTF8;"
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.Rate = [int]$env:NEXUS_TTS_RATE;"
    "if ($env:NEXUS_TTS_VOICE) { try { $s.SelectVoice($env:NEXUS_TTS_VOICE) } catch {} };"
    "[Console]::Out.WriteLine('ready'); [Console]::Out.Flush();"
    "while (($l = [Console]::In.ReadLine()) -ne $null) {"
    " $t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($l));"
    " try { $s.Speak($t) } catch {};"
    " [Console]::Out.WriteLine('done'); [Console]::Out.Flush() }"
)


class SapiTTS:
    """Windows speech through a persistent PowerShell host. `command` overrides the process (used by tests)."""

    def __init__(self, rate_wpm: int = 180, voice: str = "", command: list[str] | None = None) -> None:
        self.rate_wpm, self.voice = rate_wpm, voice
        self._command = command
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()  # guards self._proc
        self._speak_lock = threading.Lock()  # one utterance at a time
        self._stopped = False
        self._closed = False

    def available(self) -> tuple[bool, str]:
        if self._command:
            return True, "ok"
        if sys.platform != "win32":
            return False, "Windows speech (SAPI) is only available on Windows"
        if not shutil.which("powershell"):
            return False, "PowerShell not found"
        return True, "ok"

    @staticmethod
    def rate_to_sapi(wpm: int) -> int:
        # SAPI rate is -10..10 with 0 ~= 180 wpm; each step is roughly 10% faster/slower.
        return max(-10, min(10, round(math.log(max(wpm, 1) / 180) / math.log(1.1))))

    def _start(self) -> subprocess.Popen[bytes]:
        argv = self._command or ["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_SERVER]
        env = {**os.environ, "NEXUS_TTS_RATE": str(self.rate_to_sapi(self.rate_wpm)), "NEXUS_TTS_VOICE": self.voice}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=flags, env=env)
        watchdog = threading.Timer(30.0, proc.kill)  # a host that never says "ready" is killed, not waited on forever
        watchdog.start()
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
        finally:
            watchdog.cancel()
        if line.strip() != b"ready":
            proc.kill()
            if self._stopped or self._closed:
                raise TTSUnavailable("stopped")
            raise TTSUnavailable("The speech engine did not start. Check that Windows has a speech voice installed.")
        return proc

    def _ensure(self) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
        proc = self._start()
        with self._lock:
            self._proc = proc
        return proc

    def prewarm(self) -> None:
        """Start the speech host in the background so the first reply does not wait for it."""
        def go() -> None:
            try:
                if self.available()[0] and not self._closed:
                    self._ensure()
            except Exception:
                log.debug("speech prewarm failed", exc_info=True)

        threading.Thread(target=go, name="nexus-tts-prewarm", daemon=True).start()

    def speak(self, text: str) -> None:
        ok, why = self.available()
        if not ok:
            raise TTSUnavailable(why)
        import base64

        with self._speak_lock:
            self._stopped = False
            try:
                proc = self._ensure()
            except TTSUnavailable:
                if self._stopped or self._closed:
                    return
                raise
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(base64.b64encode(text.encode("utf-8")) + b"\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (BrokenPipeError, OSError):
                line = b""
            if line.strip() != b"done" and not (self._stopped or self._closed):
                with self._lock:
                    self._proc = None
                raise TTSUnavailable("The speech engine stopped unexpectedly")

    def stop(self) -> None:
        """Cut speech off immediately by killing the host; a fresh one is started in the background."""
        self._stopped = True
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            if not self._closed:
                self.prewarm()

    def close(self) -> None:
        self._closed = True
        self._stopped = True
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()

    @staticmethod
    def voices() -> list[str]:
        if sys.platform != "win32":
            return []
        script = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                  ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                               timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            return [v.strip() for v in r.stdout.splitlines() if v.strip()]
        except Exception:
            return []


class PiperTTS(_ProcTTS):
    def __init__(self, exe: str, voice_model: str, rate_wpm: int = 180) -> None:
        super().__init__()
        self.exe, self.voice_model, self.rate_wpm = exe, voice_model, rate_wpm

    def available(self) -> tuple[bool, str]:
        if not self.exe or not Path(self.exe).is_file():
            return False, "Set the path to piper.exe in Voice settings"
        if not self.voice_model or not Path(self.voice_model).is_file():
            return False, "Set the path to a Piper voice model (.onnx) in Voice settings"
        return True, "ok"

    def speak(self, text: str) -> None:
        ok, why = self.available()
        if not ok:
            raise TTSUnavailable(why)
        length_scale = max(0.5, min(2.0, 180 / max(self.rate_wpm, 1)))
        with tempfile.TemporaryDirectory(prefix="nexus-tts-") as d:
            out = Path(d) / "speech.wav"
            self._run([self.exe, "--model", self.voice_model, "--output_file", str(out),
                       "--length_scale", f"{length_scale:.2f}"], text.encode("utf-8"))
            if not self._stopped and out.exists():
                play_wav_blocking(out.read_bytes(), self)


def play_wav_blocking(data: bytes, owner: _ProcTTS | None = None) -> None:
    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(data, winsound.SND_MEMORY)
        return
    player = shutil.which("aplay") or shutil.which("paplay")
    if player and owner is not None:
        owner._run([player, "-"], data)


def beep_wav(freq: float = 880.0, ms: int = 120, rate: int = 22050) -> bytes:
    n = int(rate * ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate) * min(1, (n - i) / (n * 0.3))))
            for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()


def play_ack() -> None:
    if sys.platform == "win32":
        import winsound

        # winsound cannot play from memory asynchronously; the beep is only 120 ms.
        winsound.PlaySound(beep_wav(), winsound.SND_MEMORY)


def play_alert() -> None:
    """A short attention sound for a ringing reminder (Windows only; elsewhere silent)."""
    if sys.platform == "win32":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
