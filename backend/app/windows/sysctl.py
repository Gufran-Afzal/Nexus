"""Windows system controls used by voice/text commands: media keys, volume, lock, screenshots, focusing and closing
windows, opening settings pages. Everything is a plain method on one class so tests can substitute a fake; the real
implementation uses ctypes/user32 and is only usable on Windows (elsewhere every call raises SystemControlError)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

VK = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "prev": 0xB1, "stop": 0xB2, "play_pause": 0xB3}

SETTINGS_PAGES = {
    "": "ms-settings:", "wifi": "ms-settings:network-wifi", "wi-fi": "ms-settings:network-wifi",
    "network": "ms-settings:network", "bluetooth": "ms-settings:bluetooth", "display": "ms-settings:display",
    "sound": "ms-settings:sound", "battery": "ms-settings:batterysaver", "apps": "ms-settings:appsfeatures",
    "default apps": "ms-settings:defaultapps", "privacy": "ms-settings:privacy", "update": "ms-settings:windowsupdate",
    "windows update": "ms-settings:windowsupdate", "notifications": "ms-settings:notifications",
    "storage": "ms-settings:storagesense", "personalization": "ms-settings:personalization",
    "time": "ms-settings:dateandtime", "date": "ms-settings:dateandtime", "microphone": "ms-settings:privacy-microphone",
}


# Keys the assistant may press (an allowlist: nothing else can be sent, so a skill can never type shortcuts like Alt+F4).
KEY_CODES = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "v": 0x56, "a": 0x41, "c": 0x43,
             "down": 0x28, "up": 0x26}
ALLOWED_COMBOS = {"enter", "ctrl+enter", "tab", "esc", "ctrl+v", "ctrl+a", "ctrl+c", "down", "up"}

# Programs a "type this" command must never type into: it would run what was typed.
TERMINALS = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "conhost.exe", "openconsole.exe",
             "bash.exe", "wsl.exe", "mintty.exe", "putty.exe", "alacritty.exe", "wezterm-gui.exe", "kitty.exe",
             "git-bash.exe", "ubuntu.exe"}


class SystemControlError(Exception):
    pass


class SystemControl:
    """Interface plus the Windows implementation. Subclass and override in tests."""

    def _need_windows(self) -> None:
        if sys.platform != "win32":
            raise SystemControlError("This only works on Windows.")

    def _user32(self) -> Any:
        self._need_windows()
        import ctypes

        return ctypes.windll.user32  # type: ignore[attr-defined]

    def key(self, name: str, times: int = 1) -> None:
        vk = VK[name]
        u = self._user32()
        for _ in range(times):
            u.keybd_event(vk, 0, 0, 0)
            u.keybd_event(vk, 0, 2, 0)  # key up

    def set_volume(self, percent: int) -> None:
        """Windows has no simple 'set volume' key: go to zero, then up in 2% steps (each key press is 2%)."""
        self.key("down", 50)
        self.key("up", max(0, min(100, percent)) // 2)

    def lock(self) -> None:
        self._need_windows()
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False, timeout=10)  # noqa: S603, S607

    def open_uri(self, uri: str) -> None:
        """Open a protocol link (ms-settings:, spotify:, mailto:, ms-screenclip:) with its default handler."""
        self._need_windows()
        os.startfile(uri)  # type: ignore[attr-defined]  # noqa: S606

    def show_desktop(self) -> None:
        u = self._user32()
        for vk, up in ((0x5B, 0), (0x44, 0), (0x44, 2), (0x5B, 2)):  # Win+D
            u.keybd_event(vk, 0, up, 0)

    def focus(self, needles: list[str]) -> str | None:
        """Bring the first visible window whose title contains any needle to the front. Returns its title or None."""
        import ctypes
        from ctypes import wintypes

        u = self._user32()
        wanted = [n.lower() for n in needles if n]
        found: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]
        def cb(hwnd: Any, _lp: Any) -> bool:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if any(w in title.lower() for w in wanted) and "nexus" not in title.lower():
                found.append((hwnd, title))
            return True

        u.EnumWindows(cb, 0)
        if not found:
            return None
        hwnd, title = found[0]
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.keybd_event(0x12, 0, 0, 0)  # a tapped Alt lets SetForegroundWindow succeed
        u.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.05)
        u.SetForegroundWindow(hwnd)
        return title

    def foreground(self) -> tuple[str, str]:
        """(window title, program file name in lower case) of the window that has the keyboard focus."""
        import ctypes
        from ctypes import wintypes

        u = self._user32()
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        exe = ""
        if h:
            size = wintypes.DWORD(520)
            path = ctypes.create_unicode_buffer(size.value)
            if k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                exe = path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            k.CloseHandle(h)
        return buf.value, exe

    def can_observe(self) -> bool:
        """True where windows can be listed (Windows). Elsewhere observation is unavailable and callers must say so."""
        return sys.platform == "win32"

    def list_windows(self) -> list[tuple[str, str]]:
        """(title, program file name) of every visible top-level window that has a title."""
        import ctypes
        from ctypes import wintypes

        u = self._user32()
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        out: list[tuple[str, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]
        def cb(hwnd: Any, _lp: Any) -> bool:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            k.OpenProcess.restype = wintypes.HANDLE
            h = k.OpenProcess(0x1000, False, pid.value)
            exe = ""
            if h:
                size = wintypes.DWORD(520)
                path = ctypes.create_unicode_buffer(size.value)
                if k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                    exe = path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
                k.CloseHandle(h)
            out.append((buf.value, exe))
            return True

        u.EnumWindows(cb, 0)
        return out

    def send_keys(self, combo: str) -> None:
        """Press one of the allowed key combinations (see ALLOWED_COMBOS)."""
        if combo not in ALLOWED_COMBOS:
            raise SystemControlError(f"Pressing {combo!r} is not allowed.")
        u = self._user32()
        codes = [KEY_CODES[p] for p in combo.split("+")]
        for c in codes:
            u.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            u.keybd_event(c, 0, 2, 0)

    def set_clipboard(self, text: str) -> None:
        """Put text on the clipboard. The text goes in through stdin (never the command line), as UTF-8."""
        self._need_windows()
        ps = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], input=text.encode("utf-8"),  # noqa: S603, S607
                           capture_output=True, timeout=20, check=False)
        if r.returncode != 0:
            raise SystemControlError("Could not use the clipboard: " + (r.stderr or b"").decode("utf-8", "replace")[:200])

    def paste_text(self, text: str) -> None:
        """Type text into the focused window by pasting it (works for any language and any length). Overwrites the clipboard."""
        self.set_clipboard(text)
        time.sleep(0.15)
        self.send_keys("ctrl+v")

    def close_process(self, image: str) -> tuple[bool, str]:
        """Ask a program to close (WM_CLOSE, so it can offer to save). Never force-kills."""
        self._need_windows()
        r = subprocess.run(["taskkill", "/IM", image], capture_output=True, text=True, timeout=15, check=False)  # noqa: S603, S607
        out = (r.stdout or r.stderr or "").strip()
        return r.returncode == 0, out
