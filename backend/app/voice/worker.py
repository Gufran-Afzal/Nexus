"""Speech worker: the ONLY place where faster-whisper / CTranslate2 / onnxruntime / openWakeWord run.

It is a separate process on purpose. These libraries are native code; if one of them hard-crashes (for example an
access violation caused by an outdated Microsoft Visual C++ runtime), only this process dies. The NEXUS server
notices, reports a clear error, and starts a fresh worker on the next request.

Protocol (over stdin/stdout, binary): the client sends one JSON line {"op":..., "params":{...}, "len":N} followed by N
raw bytes; the worker answers with one JSON line. Anything libraries print to stdout is redirected to stderr so it
can never corrupt the protocol.

Run as a script:  python -I worker.py      (no imports from the NEXUS server are needed except app.voice.stt/wakeword)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
TEST_MODE = os.environ.get("NEXUS_WORKER_TEST") == "1"  # enables fake engines and crash/hang ops (tests only)


class _Engines:
    def __init__(self) -> None:
        self.stt: dict[tuple[str, str, str, int], Any] = {}
        self.oww: Any = None

    def whisper(self, params: dict[str, Any]) -> Any:
        from pathlib import Path

        from app.voice.stt import FasterWhisperSTT

        compute, threads = str(params.get("compute_type", "int8")), int(params.get("cpu_threads", 4))
        key = (params["models_dir"], params["model"], compute, threads)
        if key not in self.stt:
            self.stt[key] = FasterWhisperSTT(Path(params["models_dir"]), params["model"], compute, threads)
        return self.stt[key]


def handle(engines: _Engines, op: str, params: dict[str, Any], body: bytes) -> dict[str, Any]:
    if TEST_MODE:
        if op == "crash":
            code = int(params.get("code", 3221225477))  # simulate a native access violation
            os._exit(code if sys.platform == "win32" else code & 0xFF)
        if op == "hang":
            time.sleep(60)
        if op in ("transcribe_pcm", "transcribe_file"):
            if os.environ.get("NEXUS_WORKER_CRASH_UNLESS_FLOAT32") and params.get("compute_type") != "float32":
                os._exit(3)  # simulate a native crash that only the compatible settings avoid
            return {"text": f"fake:{op}:{len(body)}:{params.get('compute_type')}"}
        if op == "download":
            return {"path": "fake"}
    if op == "ping":
        return {"python": sys.version.split()[0], "pid": os.getpid()}
    if op == "transcribe_pcm":
        mode, phrase, vocab = str(params.get("mode", "final")), str(params.get("phrase", "")), str(params.get("vocab", ""))
        text, conf = engines.whisper(params).transcribe_detail(body, mode, phrase, vocab)
        return {"text": text, "conf": conf}
    if op == "transcribe_file":
        return {"text": engines.whisper(params).transcribe_file(body)}
    if op == "download":
        return {"path": str(engines.whisper(params).download())}
    if op == "oww_load":
        from app.voice.wakeword import OpenWakeWordDetector

        engines.oww = OpenWakeWordDetector(params["model"], float(params.get("threshold", 0.5)))
        return {}
    if op == "oww_process":
        if engines.oww is None:
            raise RuntimeError("wake-word model not loaded")
        hit = engines.oww.process(body)
        return {"score": None if hit is None else hit.score}
    if op == "oww_reset":
        if engines.oww is not None:
            engines.oww.reset()
        return {}
    raise ValueError(f"unknown operation: {op}")


def main() -> int:
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    # Keep the real stdout for the protocol; send everything libraries print to stderr.
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    crash_log = os.environ.get("NEXUS_WORKER_CRASH_LOG")
    if crash_log:
        import faulthandler

        fh = open(crash_log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        fh.write(f"\n--- speech worker started (pid {os.getpid()}) ---\n")
        faulthandler.enable(file=fh, all_threads=True)
    stdin = sys.stdin.buffer if sys.stdin is not None else open(0, "rb")  # noqa: SIM115
    engines = _Engines()
    while True:
        line = stdin.readline()
        if not line:
            return 0  # the server closed the pipe: exit quietly
        try:
            req = json.loads(line)
            body = stdin.read(int(req.get("len", 0))) if req.get("len") else b""
            result = {"ok": True, **handle(engines, req["op"], req.get("params", {}), body)}
        except Exception as e:  # report, keep serving; only a hard crash ends this process
            result = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
        proto.write((json.dumps(result) + "\n").encode("utf-8"))


if __name__ == "__main__":
    sys.exit(main())
