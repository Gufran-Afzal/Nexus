"""Entry point used by the Start Menu/sign-in shortcut. Works from any working directory."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Under pythonw.exe there is no console: sys.stdout/sys.stderr are None and any library that prints
# (e.g. model-download progress bars) would crash. Send such output to a file instead.
if sys.stdout is None or sys.stderr is None:
    _logdir = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "NEXUS", "logs")
    os.makedirs(_logdir, exist_ok=True)
    _stream = open(os.path.join(_logdir, "console.log"), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or _stream
    sys.stderr = sys.stderr or _stream
sys.path.insert(0, HERE)
os.chdir(HERE)

from app.windows.launcher import main  # noqa: E402

sys.exit(main(watchdog_script=os.path.abspath(__file__)))
