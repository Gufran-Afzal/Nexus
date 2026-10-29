"""Opt-in, per-user start-at-sign-in via HKCU\\...\\Run (no administrator rights needed).

The entry is visible and removable in Windows Settings → Apps → Startup and Task Manager → Startup.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NEXUS"


class StartupUnsupported(Exception):
    pass


class RegistryBackend(Protocol):
    def get(self) -> str | None: ...
    def set(self, command: str) -> None: ...
    def delete(self) -> None: ...


class WinRegistry:
    def _winreg(self) -> Any:
        if sys.platform != "win32":
            raise StartupUnsupported("Start at sign-in is only available on Windows")
        import winreg

        return winreg

    def get(self) -> str | None:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
                value, _ = winreg.QueryValueEx(k, VALUE_NAME)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, command: str) -> None:
        winreg = self._winreg()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        winreg = self._winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            pass


def launch_command() -> str:
    """pythonw (no console window) running the tray launcher from this installation."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    script = Path(__file__).resolve().parents[2] / "run_nexus.pyw"
    return f'"{interpreter}" "{script}" --background'


class StartupManager:
    def __init__(self, backend: RegistryBackend | None = None) -> None:
        self.backend = backend or WinRegistry()

    def status(self) -> dict[str, Any]:
        try:
            current = self.backend.get()
        except StartupUnsupported as e:
            return {"supported": False, "enabled": False, "detail": str(e)}
        expected = launch_command()
        return {
            "supported": True,
            "enabled": current is not None,
            "command": current,
            "matches_this_install": current == expected,
        }

    def enable(self) -> dict[str, Any]:
        self.backend.set(launch_command())
        return self.status()

    def disable(self) -> dict[str, Any]:
        self.backend.delete()
        return self.status()
