"""Find which native package crashes, without touching NEXUS data. Standard library only; read-only; no network.

Each test runs in its OWN Python process, so a crash (Windows exit code 0xC0000005 = access violation) is caught
and attributed to exactly one package. Run with the project's own interpreter:

    .\\.venv\\Scripts\\python.exe backend\\native_check.py            (all tests)
    .\\.venv\\Scripts\\python.exe backend\\native_check.py onnxruntime  (only tests whose name contains the word)

Exit code: 0 = every test passed, 1 = at least one test failed or crashed.
"""

# ruff: noqa: E501  (diagnostic snippets are one-liners on purpose)
from __future__ import annotations

import importlib.metadata as md
import platform
import subprocess
import sys
import time

PACKAGES = ["numpy", "scipy", "scikit-learn", "onnxruntime", "ctranslate2", "av", "faster-whisper", "tokenizers",
            "huggingface-hub", "openwakeword", "sounddevice", "cffi", "pystray", "Pillow", "psutil", "uvicorn"]

# Bytes of a tiny valid ONNX model (graph: y = Identity(x)), written by hand so no extra package is needed.
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, wire: int, payload: bytes) -> bytes:
    return _varint(num << 3 | wire) + payload


def _ld(num: int, data: bytes) -> bytes:
    return _field(num, 2, _varint(len(data)) + data)


def tiny_onnx_model() -> bytes:
    def value_info(name: str) -> bytes:
        dim = _ld(1, _field(1, 0, _varint(1)))  # dim { dim_value: 1 }
        tensor = _field(1, 0, _varint(1)) + _ld(2, dim)  # elem_type FLOAT, shape
        return _ld(1, name.encode()) + _ld(2, _ld(1, tensor))
    node = _ld(1, b"x") + _ld(2, b"y") + _ld(4, b"Identity")
    graph = _ld(1, node) + _ld(2, b"g") + _ld(11, value_info("x")) + _ld(12, value_info("y"))
    return _field(1, 0, _varint(8)) + _ld(8, _field(2, 0, _varint(13))) + _ld(7, graph)


LOADED_DLLS = r"""
import os, sys
if os.name == "nt":
    try:
        import psutil
        seen = sorted({m.path for m in psutil.Process().memory_maps() if m.path.lower().endswith(".dll")
                       and any(k in os.path.basename(m.path).lower() for k in ("msvcp", "vcruntime", "vcomp", "libiomp", "libomp", "concrt"))})
        print("LOADED_RUNTIME_DLLS:" + ";".join(seen))
    except Exception as e:
        print("LOADED_RUNTIME_DLLS:unavailable (%s)" % e)
"""

TESTS: dict[str, str] = {
    "baseline (sqlite, ctypes, ssl)": "import sqlite3, ctypes, ssl; print('ok')",
    "numpy": "import numpy as np; a=np.random.rand(200,200); print(float((a@a).sum())>0)",
    "scipy": "import scipy.signal, numpy as np; print(scipy.signal.convolve(np.ones(50), np.ones(5)).shape)",
    "scikit-learn": "import numpy as np; from sklearn.linear_model import LogisticRegression as L; "
                    "x=np.random.rand(40,3); y=(x[:,0]>.5).astype(int); print(L().fit(x,y).score(x,y))",
    "onnxruntime (import)": "import onnxruntime as o; print(o.__version__, o.get_available_providers())",
    "onnxruntime (run a model)": "import onnxruntime as o, numpy as np, native_check as n; "
                                 "s=o.InferenceSession(n.tiny_onnx_model(), providers=['CPUExecutionProvider']); "
                                 "print(s.run(None, {'x': np.ones((1,), dtype=np.float32)}))",
    "ctranslate2": "import ctranslate2 as c; print(c.__version__, c.get_supported_compute_types('cpu'))",
    "av (PyAV)": "import av; print(av.__version__, len(av.codecs_available) > 0)",
    "tokenizers": "import tokenizers; print(tokenizers.__version__)",
    "faster_whisper (import)": "import faster_whisper; print(faster_whisper.__version__)",
    "sounddevice (list microphones)": "import sounddevice as sd; print([d['name'] for d in sd.query_devices() if d['max_input_channels']>0])",
    "openwakeword (import)": "import openwakeword; print(getattr(openwakeword, '__version__', 'ok'))",
    "voice stack together": "import numpy, onnxruntime, ctranslate2, av, faster_whisper, openwakeword; print('ok')",
    "tray (pystray + Pillow)": "from PIL import Image, ImageDraw; im=Image.new('RGBA',(64,64)); ImageDraw.Draw(im).ellipse((4,4,60,60)); "
                               "import pystray; print(pystray.Icon.__name__)",
}


def whisper_model_dir() -> str | None:
    """A downloaded Whisper model directory, if any (NEXUS stores them under <data dir>\\models\\whisper\\<name>)."""
    import glob
    import os

    base = os.environ.get("NEXUS_DATA_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "NEXUS")
    hits = sorted(glob.glob(os.path.join(base, "models", "whisper", "*", "model.bin")))
    return os.path.dirname(hits[0]) if hits else None


def model_load_tests() -> dict[str, tuple[str, dict[str, str]]]:
    """One test per compatibility profile: load the real Whisper model with CTranslate2 (the call that crashed)."""
    path = whisper_model_dir()
    if not path:
        return {}
    sys.path.insert(0, _here())
    try:
        from app.voice.worker_client import PROFILES
    except Exception:
        return {}
    tests: dict[str, tuple[str, dict[str, str]]] = {}
    for prof in PROFILES:
        code = (f"import ctranslate2 as c; m = c.models.Whisper({path!r}, device='cpu', compute_type={prof['compute_type']!r}, "
                f"intra_threads={prof['cpu_threads']}); print('model loaded')")
        tests[f"ctranslate2 loads the Whisper model [{prof['name']}]"] = (code, dict(prof.get("env", {})))
    return tests


def run_test(name: str, code: str, timeout: float = 120.0, env: dict[str, str] | None = None) -> tuple[str, int | None, str]:
    import os

    full = code + "\n" + LOADED_DLLS
    try:
        p = subprocess.run([sys.executable, "-X", "faulthandler", "-c", full], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, cwd=_here(), env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        return "HANG", None, f"no answer after {timeout:.0f}s"
    text = (p.stdout + p.stderr).strip()
    if p.returncode == 0:
        return "PASS", 0, text
    if "ModuleNotFoundError" in text or "PortAudio library not found" in text:
        return "SKIP", p.returncode, text  # not installed: nothing to diagnose
    if p.returncode in (3221225477, -11, 139):
        return "CRASH", p.returncode, text
    return "FAIL", p.returncode, text


def _here() -> str:
    import os

    return os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str]) -> int:
    print(f"Python {sys.version.split()[0]} {platform.architecture()[0]} at {sys.executable}")
    print(f"Platform {platform.platform()}")
    print("\nInstalled versions:")
    for pkg in PACKAGES:
        try:
            print(f"  {pkg:16} {md.version(pkg)}")
        except md.PackageNotFoundError:
            print(f"  {pkg:16} (not installed)")
    wanted = [a.lower() for a in argv]
    results: list[tuple[str, str, int | None, str, float]] = []
    print("\nIsolated tests (each in its own process):")
    all_tests: dict[str, tuple[str, dict[str, str]]] = {n: (c, {}) for n, c in TESTS.items()}
    models = model_load_tests()
    all_tests.update(models)
    if not models:
        print("  (No downloaded Whisper model found, so the model-load tests are skipped.)")
    for name, (code, env) in all_tests.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        t0 = time.time()
        status, rc, text = run_test(name, code, env=env)
        results.append((name, status, rc, text, time.time() - t0))
        rc_s = "" if rc in (0, None) else f"  exit {rc} (0x{rc & 0xFFFFFFFF:08X})"
        print(f"  [{status:5}] {name}  ({results[-1][4]:.1f}s){rc_s}")
        if status not in ("PASS", "SKIP"):
            for line in text.splitlines()[-12:]:
                print(f"           {line}")
        else:
            for line in text.splitlines():
                if line.startswith("LOADED_RUNTIME_DLLS:") and len(line) > 20:
                    print(f"           runtime DLLs in this process: {line.split(':', 1)[1]}")
    bad = [r for r in results if r[1] not in ("PASS", "SKIP")]
    print("\nSummary:")
    skipped = [r[0] for r in results if r[1] == "SKIP"]
    if skipped:
        print(f"  Not installed (skipped): {', '.join(skipped)}")
    if not bad:
        print("  Every installed package loads and runs on its own. If NEXUS still crashes, send logs\\crash.log.")
        return 0
    crashed = [r for r in bad if r[1] == "CRASH"]
    for name, status, _rc, _t, _d in bad:
        print(f"  {status}: {name}")
    working = [r[0] for r in results if r[0].startswith("ctranslate2 loads") and r[1] == "PASS"]
    if working and any(r[0].startswith("ctranslate2 loads") and r[1] != "PASS" for r in results):
        print(f"  NEXUS falls back automatically to the first model setting that works: {working[0]}")
    if crashed:
        print(f"  First crash: {crashed[0][0]}. That package (or the C++ runtime it uses) is the cause; the packages")
        print("  listed as PASS are not. See docs/TROUBLESHOOTING.md for the fix steps.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
