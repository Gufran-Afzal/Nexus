"""Desktop launcher: single instance, supervised backend server, system tray, clean exit.

Run with:  pythonw run_nexus.pyw            (opens the UI)
           pythonw run_nexus.pyw --background  (used at sign-in; starts quietly in the tray)
           python  run_nexus.pyw --no-tray     (console mode, e.g. for debugging)
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

import httpx
import uvicorn

from app.core.config import get_settings
from app.core.logging_setup import configure_logging

log = logging.getLogger("nexus.launcher")


# ---------------------------------------------------------------- single instance
class SingleInstance:
    def __init__(self, name: str = "NEXUS-single-instance") -> None:
        self.name = name
        self._handle: Any = None
        self._fh: Any = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            self._handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
        import fcntl

        path = get_settings().data_dir / "nexus.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        if sys.platform == "win32" and self._handle:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)  # type: ignore[attr-defined]
        if self._fh:
            self._fh.close()


# ---------------------------------------------------------------- server supervisor
class Supervisor:
    """Runs uvicorn in a thread and restarts it after an unexpected crash (bounded)."""

    def __init__(self, on_app: Any = None) -> None:
        self.settings = get_settings()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()
        self.on_app = on_app
        self.crashes: list[float] = []

    @property
    def url(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    def _run(self) -> None:
        from app.main import create_app

        while not self.stopping.is_set():
            app = create_app()
            if self.on_app:
                self.on_app(app)
            config = uvicorn.Config(app, host=self.settings.host, port=self.settings.port, log_config=None,
                                    access_log=False)
            self.server = uvicorn.Server(config)
            try:
                self.server.run()
            except (Exception, SystemExit):  # uvicorn exits via SystemExit on e.g. a failed port bind
                log.exception("backend crashed")
            if self.stopping.is_set():
                return
            now = time.time()
            self.crashes = [t for t in self.crashes if now - t < 600] + [now]
            if len(self.crashes) > 5:
                log.error("backend crashed too often; giving up")
                return
            log.warning("backend stopped unexpectedly; restarting in %ss", 2 * len(self.crashes))
            self.stopping.wait(2 * len(self.crashes))

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="nexus-server", daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(self.url + "/api/health/live", timeout=1, trust_env=False).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        self.stopping.set()
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=15)


def api(sup: Supervisor, method: str, path: str, **kw: Any) -> tuple[bool, str]:
    """Call the local API. Returns (ok, detail) so the tray can report real outcomes."""
    try:
        r = httpx.request(method, sup.url + path, headers={"X-Nexus-Client": "tray"}, timeout=10,
                          trust_env=False, **kw)
    except httpx.HTTPError as e:
        log.warning("tray action %s %s failed: %s", method, path, e)
        return False, "NEXUS backend is not responding"
    if r.status_code >= 400:
        try:
            detail = str(r.json().get("detail", r.text))
        except Exception:
            detail = r.text
        return False, detail[:240]
    return True, ""


def port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _show_error(msg: str) -> None:
    """Visible error even under pythonw (no console)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, msg, "NEXUS", 0x10)  # type: ignore[attr-defined]
    else:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------- tray
def make_icon_image(active: bool = True) -> Any:
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (99, 102, 241, 255) if active else (120, 120, 130, 255)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=color)
    d.line((20, 46, 20, 18, 44, 46, 44, 18), fill=(255, 255, 255, 255), width=6, joint="curve")
    return img


def build_tray(sup: Supervisor, open_after_start: bool) -> Any:
    """Create the tray icon and start the server. pystray only accepts menu callbacks with 0-2 positional
    parameters (icon, item); a 3-parameter callback raises ValueError at start-up, so every callback here has <= 2."""
    import pystray

    state = {"listening": False, "muted": False, "paused": False}
    icon_holder: dict[str, Any] = {}

    def open_path(path: str = "/") -> None:
        webbrowser.open(sup.url + path)

    def open_ui(_icon: Any = None, _item: Any = None) -> None:
        open_path("/")

    def toggle(key: str, on_path: str, off_path: str) -> Any:
        def _fn(icon: Any, _item: Any) -> None:
            want = not state[key]
            ok, detail = api(sup, "POST", on_path if want else off_path)
            if ok:
                state[key] = want
            else:
                with contextlib.suppress(Exception):
                    icon.notify(detail or "Action failed", "NEXUS")
            icon.update_menu()
        return _fn

    def view_logs(_icon: Any, _item: Any) -> None:
        logs = get_settings().logs_dir
        if sys.platform == "win32":
            os.startfile(str(logs))  # type: ignore[attr-defined]  # noqa: S606
        else:
            open_path("/#/health")

    def exit_all(icon: Any, _item: Any) -> None:
        icon.notify("NEXUS is shutting down", "NEXUS")
        sup.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open NEXUS", open_ui, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _i: "Stop listening" if state["listening"] else "Start listening",
                         toggle("listening", "/api/voice/listen/start", "/api/voice/listen/stop")),
        pystray.MenuItem(lambda _i: "Unmute microphone" if state["muted"] else "Mute microphone",
                         toggle("muted", "/api/voice/mute", "/api/voice/unmute")),
        pystray.MenuItem(lambda _i: "Resume assistant" if state["paused"] else "Pause assistant",
                         toggle("paused", "/api/system/pause", "/api/system/resume")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings", lambda _icon, _item: open_path("/#/settings")),
        pystray.MenuItem("View logs", view_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit NEXUS completely", exit_all),
    )
    icon = pystray.Icon("NEXUS", make_icon_image(), "NEXUS — Local AI assistant", menu)
    icon_holder["icon"] = icon

    def on_event(ev: dict[str, Any]) -> None:
        if ev.get("type") == "reminder":
            with contextlib.suppress(Exception):
                icon.notify(ev.get("text", "Reminder"), "NEXUS " + ("timer" if ev.get("kind") == "timer" else "reminder"))
        elif ev.get("type") == "voice" and "state" in ev:
            if ev["state"] == "listening_for_wake":
                state["listening"] = True
            elif ev["state"] in ("idle", "disabled", "muted", "paused"):
                state["listening"] = False
            if "muted" in ev:
                state["muted"] = bool(ev["muted"])
            if "paused" in ev:
                state["paused"] = bool(ev["paused"])
            with contextlib.suppress(Exception):
                icon.update_menu()
        elif ev.get("type") == "system" and ev.get("event") in ("paused", "resumed_by_user"):
            state["paused"] = ev["event"] == "paused"
            with contextlib.suppress(Exception):
                icon.update_menu()

    def on_app(app: Any) -> None:
        app.state.nexus.bus.add_listener(on_event)
        app.state.nexus.request_shutdown = lambda: exit_all(icon, None)

    sup.on_app = on_app
    sup.start()

    def after_start() -> None:
        if not sup.wait_ready():
            with contextlib.suppress(Exception):
                icon.notify("NEXUS backend did not start. See View logs.", "NEXUS")
        elif open_after_start:
            open_ui()

    threading.Thread(target=after_start, daemon=True).start()
    return icon


def run_tray(sup: Supervisor, open_after_start: bool) -> None:
    build_tray(sup, open_after_start).run()


_crash_file: Any = None


def _enable_crash_log(logs_dir: Any) -> None:
    """If a native library (audio driver, speech engine) ever hard-crashes the process, Python cannot log it.
    faulthandler writes the Python stack at the moment of the crash to crash.log so it can be diagnosed."""
    global _crash_file
    import faulthandler

    try:
        _crash_file = open(logs_dir / "crash.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        log.debug("could not enable crash log", exc_info=True)


EXIT_PORT_IN_USE = 2


def watchdog(script: str, argv: list[str], max_restarts: int = 5, window_s: float = 600.0,
             spawn: Any = None) -> int:
    """Outer process: runs the real launcher as a child and restarts it if it dies abnormally.

    A native-library crash (audio driver, speech engine) kills a Python process outright; nothing inside that
    process can recover from it. This tiny parent never loads those libraries, so it survives and brings NEXUS
    back (tray, server and all). A clean exit (code 0, e.g. "Exit NEXUS completely") or a port conflict ends
    the watchdog too.
    """
    spawn = spawn or subprocess.Popen
    crashes: list[float] = []
    args = [a for a in argv if a != "--child"]
    while True:
        proc = spawn([sys.executable, script, "--child", *args])
        code = proc.wait()
        if code in (0, EXIT_PORT_IN_USE):
            return int(code)
        now = time.time()
        crashes = [t for t in crashes if now - t < window_s] + [now]
        log.error("NEXUS exited unexpectedly (code %s); restart %d of %d", code, len(crashes), max_restarts)
        if len(crashes) > max_restarts:
            _show_error("NEXUS keeps crashing and was stopped. See crash.log and nexus.log in the logs folder "
                        "(tray > View logs, or %LOCALAPPDATA%\\NEXUS\\logs).")
            return int(code)
        if "--background" not in args:
            args.append("--background")  # a restart must not open another browser tab; the open tab reconnects
        time.sleep(min(2.0 * len(crashes), 10.0))


def main(argv: list[str] | None = None, watchdog_script: str | None = None) -> int:
    parser = argparse.ArgumentParser(description="NEXUS launcher")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background", action="store_true", help="start in the tray without opening the UI")
    parser.add_argument("--no-tray", action="store_true", help="run without a tray icon (Ctrl+C to exit)")
    args = parser.parse_args(argv)
    if watchdog_script and not args.child:
        configure_logging(get_settings().logs_dir)
        return watchdog(watchdog_script, list(sys.argv[1:] if argv is None else argv))
    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.logs_dir)
    _enable_crash_log(settings.logs_dir)

    single = SingleInstance()
    if not single.acquire():
        log.info("NEXUS is already running; opening the existing window")
        if not args.background:
            webbrowser.open(f"http://{settings.host}:{settings.port}/")
        return 0

    if port_in_use(settings.host, settings.port):
        msg = (f"Port {settings.port} is already used by another program. Close it, or set NEXUS_PORT=8766 in a "
               ".env file in the NEXUS folder.")
        log.error(msg)
        _show_error(msg)
        return EXIT_PORT_IN_USE

    sup = Supervisor()
    try:
        tray_ok = not args.no_tray
        if tray_ok:
            try:
                import PIL  # noqa: F401
                import pystray  # noqa: F401
            except Exception as e:  # ImportError, or a backend failure (e.g. no desktop session)
                log.warning("Tray icon unavailable (%s); running without it. Use scripts\\stop.ps1 to exit.", e)
                tray_ok = False
        if tray_ok:
            try:
                run_tray(sup, open_after_start=not args.background)  # blocks until "Exit"
            except Exception:  # a tray problem must never take the assistant down: keep running without the icon
                log.exception("Tray icon failed; continuing without it. Use scripts\\stop.ps1 to exit.")
                tray_ok = False
                if sup.stopping.is_set():
                    return 0
        if not tray_ok:
            done = threading.Event()

            def on_app_console(app: Any) -> None:
                app.state.nexus.request_shutdown = done.set

            sup.on_app = on_app_console
            if not (sup.thread and sup.thread.is_alive()):
                sup.start()
            if sup.wait_ready() and not args.background:
                webbrowser.open(sup.url + "/")
            print(f"NEXUS running at {sup.url}  (Ctrl+C to stop).")
            print("Console mode has no tray icon; start without --no-tray to get one.")
            try:
                while sup.thread and sup.thread.is_alive() and not done.is_set():
                    sup.thread.join(0.5)
            except KeyboardInterrupt:
                pass
    finally:
        sup.stop()
        single.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
