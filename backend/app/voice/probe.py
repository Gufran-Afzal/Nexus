"""Crash-proof probing of optional voice components.

Why this exists: the Voice page asks "is the microphone usable? is faster-whisper installed?". Answering that by
importing PortAudio / ctranslate2 / onnxruntime inside the server can hard-crash the whole process on some
Windows setups (native access violations and duplicate-OpenMP aborts cannot be caught by `try/except`). So:

- "Is package X installed?" is answered with `importlib.util.find_spec`, which never executes package code.
- "Which microphones exist?" is answered by a short-lived child process. If it crashes, hangs or prints
  garbage, the server only learns "microphone unavailable" and keeps running.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from typing import Any

PROBE_TIMEOUT_S = 12.0
CACHE_TTL_S = 20.0

DEVICE_SCRIPT = r"""
import json, sys
try:
    import sounddevice as sd
    default_in = sd.default.device[0] if sd.default.device else None
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": str(d["name"]), "default": i == default_in})
    sys.stdout.write("\n@@NEXUS@@" + json.dumps({"ok": True, "devices": out}))
except Exception as e:
    sys.stdout.write("\n@@NEXUS@@" + json.dumps({"ok": False, "error": f"{e.__class__.__name__}: {e}"}))
"""
MARK = "@@NEXUS@@"


def package_installed(name: str) -> bool:
    """True if the package can be found. Never imports it (so it cannot crash the server)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class DeviceProbe:
    """Lists input devices in a child process, cached briefly so page reloads stay cheap."""

    def __init__(self, script: str = DEVICE_SCRIPT, timeout: float = PROBE_TIMEOUT_S, ttl: float = CACHE_TTL_S) -> None:
        self.script, self.timeout, self.ttl = script, timeout, ttl
        self._lock = threading.Lock()  # one probe at a time: concurrent PortAudio initialisation is not safe
        self._cached: tuple[float, dict[str, Any]] | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def run(self) -> dict[str, Any]:
        """Returns {"ok": bool, "devices": [...], "error": str}. Never raises."""
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self.ttl:
                return self._cached[1]
            result = self._run_child()
            self._cached = (time.monotonic(), result)
            return result

    def _run_child(self) -> dict[str, Any]:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        try:
            proc = subprocess.run([sys.executable, "-I", "-c", self.script], capture_output=True, text=True,
                                  timeout=self.timeout, creationflags=flags, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"ok": False, "devices": [], "error": "The audio system did not answer in time"}
        except OSError as e:
            return {"ok": False, "devices": [], "error": f"Could not start the audio check: {e}"}
        if proc.returncode != 0:
            return {"ok": False, "devices": [],
                    "error": f"The audio system crashed while listing microphones (exit code {proc.returncode}). "
                             "NEXUS kept running; try another audio driver or unplug unusual devices."}
        _, _, tail = proc.stdout.rpartition(MARK)
        try:
            data = json.loads(tail)
        except ValueError:
            return {"ok": False, "devices": [], "error": "The audio check returned an unreadable answer"}
        if not data.get("ok"):
            return {"ok": False, "devices": [], "error": str(data.get("error", "unknown error"))}
        return {"ok": True, "devices": data.get("devices", []), "error": ""}
