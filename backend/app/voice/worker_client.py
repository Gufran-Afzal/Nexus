"""Client for the speech worker process (see worker.py). Thread-safe; one request at a time."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.voice.probe import package_installed
from app.voice.stt import _SAFE_NAME, KNOWN_MODELS
from app.voice.wakeword import WakeHit

log = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).with_name("worker.py")
CRASH_HINT = (
    "NEXUS kept running. A native speech library crashed: this is most often an outdated Microsoft Visual C++ "
    "runtime or an incompatible package. Run scripts\\diagnose.ps1 to find which one."
)


class SpeechEngineError(Exception):
    """The speech worker failed (crashed, hung or reported an error)."""


class SpeechEngineCrash(SpeechEngineError):
    """The worker process died (native crash), as opposed to hanging or reporting a Python error."""


# Settings tried in order when the speech engine crashes while loading/running the model. The first that works is
# remembered. CTranslate2 picks CPU kernels at run time (CT2_FORCE_CPU_ISA); a bad kernel/threading/runtime
# combination on one machine is a plausible cause of an access violation, and these rungs avoid each in turn.
PROFILES: list[dict[str, Any]] = [
    {"name": "fast (int8)", "compute_type": "int8", "cpu_threads": 4, "env": {}},
    {"name": "int8, 1 thread, AVX2 kernels", "compute_type": "int8", "cpu_threads": 1, "env": {"CT2_FORCE_CPU_ISA": "AVX2"}},
    {"name": "float32, AVX2 kernels", "compute_type": "float32", "cpu_threads": 2, "env": {"CT2_FORCE_CPU_ISA": "AVX2"}},
    {"name": "float32, generic kernels", "compute_type": "float32", "cpu_threads": 1,
     "env": {"CT2_FORCE_CPU_ISA": "GENERIC", "CT2_USE_MKL": "0"}},
]
STT_OPS = ("transcribe_pcm", "transcribe_file", "download")


def describe_exit(code: int | None) -> str:
    if code is None:
        return "unknown exit"
    if code < 0:
        return f"signal {-code}"
    return f"exit code {code} (0x{code & 0xFFFFFFFF:08X})"


def python_for_worker() -> str:
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("pythonw.exe"):
        sibling = Path(exe).with_name("python.exe")  # pythonw has no usable std handles for pipes on some setups
        if sibling.exists():
            return str(sibling)
    return exe


class SpeechWorker:
    def __init__(self, logs_dir: Path | None = None, extra_env: dict[str, str] | None = None,
                 breaker_after: int = 3, breaker_pause_s: float = 600.0,
                 profiles: list[dict[str, Any]] | None = None, state_file: Path | None = None) -> None:
        self.logs_dir = logs_dir
        self.profiles = profiles or PROFILES
        self.state_file = state_file
        self.profile_idx = self._load_profile_idx()
        self._saved_idx = self.profile_idx
        self.extra_env = extra_env or {}
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._crashes = 0  # consecutive
        self._broken_until = 0.0
        self.breaker_after, self.breaker_pause_s = breaker_after, breaker_pause_s
        self.last_failure: str | None = None

    # ---- engine profile (compatibility ladder)
    def _load_profile_idx(self) -> int:
        try:
            if self.state_file and self.state_file.is_file():
                return max(0, min(int(json.loads(self.state_file.read_text())["profile"]), len(self.profiles) - 1))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return 0

    @property
    def profile(self) -> dict[str, Any]:
        return self.profiles[self.profile_idx]

    @property
    def degraded(self) -> bool:
        return self.profile_idx > 0

    def _advance_profile(self) -> bool:
        with self._lock:
            if self.profile_idx >= len(self.profiles) - 1:
                return False
            self.profile_idx += 1
            self._crashes = 0
            self._broken_until = 0.0
            log.warning("speech engine crashed; switching to compatibility profile '%s'", self.profile["name"])
            return True

    def _remember_profile(self) -> None:
        if self.state_file and self.profile_idx != self._saved_idx:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"profile": self.profile_idx, "name": self.profile["name"]}))
                self._saved_idx = self.profile_idx
            except OSError:
                log.debug("could not save speech profile", exc_info=True)

    # ---- lifecycle
    def _spawn(self) -> subprocess.Popen[bytes]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", **self.profile.get("env", {}),
               **self.extra_env}
        if self.logs_dir:
            env["NEXUS_WORKER_CRASH_LOG"] = str(self.logs_dir / "speech-worker-crash.log")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        stderr: Any = subprocess.DEVNULL
        if self.logs_dir:
            stderr = open(self.logs_dir / "speech-worker.log", "ab")  # noqa: SIM115
        try:
            return subprocess.Popen([python_for_worker(), "-I", str(WORKER_PATH)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr, env=env, creationflags=flags)
        finally:
            if hasattr(stderr, "close"):
                stderr.close()  # the child keeps its own handle

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.kill()
        if p:
            for s in (p.stdin, p.stdout):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def reset_breaker(self) -> None:
        self._crashes = 0
        self._broken_until = 0.0

    @property
    def broken_reason(self) -> str | None:
        if time.monotonic() < self._broken_until:
            return f"Speech engine disabled after repeated crashes ({self.last_failure}). {CRASH_HINT}"
        return None

    # ---- requests
    def request(self, op: str, params: dict[str, Any] | None = None, body: bytes = b"", timeout: float = 120.0) -> dict[str, Any]:
        """Run one operation. If the engine hard-crashes during speech recognition, retry once per remaining
        compatibility profile before giving up (the working profile is remembered)."""
        while True:
            try:
                return self._request_once(op, params, body, timeout)
            except SpeechEngineCrash as e:
                if op not in STT_OPS or not self._advance_profile():
                    raise SpeechEngineError(str(e)) from e

    def _request_once(self, op: str, params: dict[str, Any] | None, body: bytes, timeout: float) -> dict[str, Any]:
        if op in STT_OPS:
            params = {**(params or {}), "compute_type": self.profile["compute_type"], "cpu_threads": self.profile["cpu_threads"]}
        with self._lock:
            reason = self.broken_reason
            if reason:
                raise SpeechEngineError(reason)
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn()
            proc = self._proc
            assert proc.stdin is not None and proc.stdout is not None
            result: dict[str, Any] = {}
            err: list[BaseException] = []

            def io() -> None:
                try:
                    header = json.dumps({"op": op, "params": params or {}, "len": len(body)}).encode() + b"\n"
                    assert proc.stdin is not None and proc.stdout is not None
                    proc.stdin.write(header + body)
                    proc.stdin.flush()
                    line = proc.stdout.readline()
                    if not line:
                        raise EOFError("worker closed the pipe")
                    result.update(json.loads(line))
                except BaseException as e:  # noqa: BLE001 - reported below
                    err.append(e)

            t = threading.Thread(target=io, daemon=True, name="nexus-worker-io")
            t.start()
            t.join(timeout)
            if t.is_alive():  # hung: kill it, which unblocks the reader thread
                self._kill()
                t.join(5)
                self._note_failure(f"timed out after {timeout:.0f}s")
                raise SpeechEngineError(f"The speech engine did not answer within {timeout:.0f}s and was restarted.")
            if err:
                code = None
                try:
                    code = proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                self._kill()
                what = describe_exit(code)
                self._note_failure(what, op in STT_OPS)  # speech crashes first try the other profiles
                raise SpeechEngineCrash(f"The speech engine crashed ({what}). {CRASH_HINT}") from err[0]
            self._crashes = 0
            if op in STT_OPS and result.get("ok"):
                self._remember_profile()
            if not result.get("ok"):
                raise SpeechEngineError(str(result.get("error", "unknown speech engine error")))
            return result

    def _note_failure(self, what: str, laddered: bool = False) -> None:
        self.last_failure = what
        self._crashes += 1
        log.error("speech worker failed: %s (consecutive failures: %d)", what, self._crashes)
        if self._crashes >= self.breaker_after and (not laddered or self.profile_idx >= len(self.profiles) - 1):
            self._broken_until = time.monotonic() + self.breaker_pause_s


def whisper_model_downloaded(models_dir: Path, name: str) -> bool:
    return _SAFE_NAME.match(name) is not None and (models_dir / "whisper" / name / "model.bin").exists()


class WorkerSTT:
    """STT that runs faster-whisper inside the speech worker. Availability checks never import native code."""

    def __init__(self, models_dir: Path, model_name: str, worker: SpeechWorker) -> None:
        if not _SAFE_NAME.match(model_name):
            raise ValueError("Invalid model name")
        self.models_dir, self.model_name, self.worker = models_dir, model_name, worker
        self.vocab = ""  # the user's own words (apps, people): sent with every full transcription

    def set_vocab(self, vocab: str) -> None:
        self.vocab = vocab

    def downloaded(self) -> bool:
        return whisper_model_downloaded(self.models_dir, self.model_name)

    def available(self) -> tuple[bool, str]:
        if not package_installed("faster_whisper"):
            return False, "faster-whisper is not installed (run scripts\\setup.ps1 -Voice)"
        if not self.downloaded():
            return False, f"Speech model '{self.model_name}' has not been downloaded yet"
        broken = self.worker.broken_reason
        if broken:
            return False, broken
        if self.worker.degraded:
            return True, f"ok (compatibility mode: {self.worker.profile['name']})"
        return True, "ok"

    def _params(self) -> dict[str, Any]:
        return {"models_dir": str(self.models_dir), "model": self.model_name}

    def transcribe_detail(self, pcm: bytes) -> tuple[str, float | None]:
        """Text plus how sure Whisper was (0..1, None if unknown)."""
        params = {**self._params(), "vocab": self.vocab}
        r = self.worker.request("transcribe_pcm", params, pcm, timeout=180)
        conf = r.get("conf")
        return str(r["text"]), (float(conf) if conf is not None else None)

    def transcribe_pcm(self, pcm: bytes) -> str:
        return self.transcribe_detail(pcm)[0]

    def transcribe_wake(self, pcm: bytes, phrase: str) -> str:
        """Short clip that may contain the wake phrase: cheap decode, primed with the phrase."""
        params = {**self._params(), "mode": "wake", "phrase": phrase}
        return str(self.worker.request("transcribe_pcm", params, pcm, timeout=60)["text"])

    def transcribe_file(self, data: bytes) -> str:
        return str(self.worker.request("transcribe_file", self._params(), data, timeout=180)["text"])

    def download(self) -> Path:
        if not package_installed("faster_whisper"):
            raise SpeechEngineError("faster-whisper is not installed")
        r = self.worker.request("download", self._params(), timeout=3600)
        return Path(str(r["path"]))


class WorkerOpenWakeWord:
    """openWakeWord running inside the speech worker, one 80 ms frame per request."""

    def __init__(self, worker: SpeechWorker, model: str, threshold: float = 0.5) -> None:
        if not package_installed("openwakeword"):
            raise RuntimeError("openwakeword is not installed (run scripts\\setup.ps1 -WakeWord)")
        self.worker = worker
        worker.request("oww_load", {"model": model, "threshold": threshold}, timeout=120)

    def process(self, frame: bytes) -> WakeHit | None:
        score = self.worker.request("oww_process", body=frame, timeout=10).get("score")
        return None if score is None else WakeHit(score=float(score))

    def reset(self) -> None:
        self.worker.request("oww_reset", timeout=10)


__all__ = ["KNOWN_MODELS", "SpeechEngineError", "SpeechWorker", "WorkerOpenWakeWord", "WorkerSTT",
           "whisper_model_downloaded"]
