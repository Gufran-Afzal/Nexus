"""A fake Windows desktop for benchmarking: windows that appear (or not), dialogs that block, apps that fail to start.

Everything that touches the real machine in NEXUS goes through `SystemControl` and the launcher, so replacing those two is
enough to exercise the real orchestration code deterministically."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.windows.sysctl import SystemControl

WINDOW = {"notepad": ("Untitled - Notepad", "notepad.exe"), "spotify": ("Spotify Free", "spotify.exe"),
          "whatsapp": ("WhatsApp", "whatsapp.exe"), "chrome": ("New Tab - Google Chrome", "chrome.exe")}
DIALOG = {"spotify": ("Update available", "spotify.exe")}


@dataclass
class Behaviour:
    mode: str = "ok"  # ok | slow | never | dialog | fail_once | fail_always
    slow_polls: int = 2
    attempts: int = 0


@dataclass
class FakeEnv(SystemControl):
    windows: list[tuple[str, str]] = field(default_factory=list)
    behaviours: dict[str, Behaviour] = field(default_factory=dict)
    launched: list[tuple[str, str]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    polls: int = 0
    clock: float = 0.0
    fg: tuple[str, str] = ("", "")
    _pending: list[tuple[int, tuple[str, str]]] = field(default_factory=list)

    # ---- time (verification loops use env.sleep, so they are instant and deterministic)
    def sleep(self, s: float) -> None:
        self.clock += s

    # ---- launcher
    def key_of(self, target: object) -> str:
        t = " ".join(target) if isinstance(target, list) else str(target)
        t = t.lower()
        return next((k for k in WINDOW if k in t), t)

    def launcher(self, target: object, kind: str) -> None:
        self.launched.append((str(target), kind))
        key = self.key_of(target)
        b = self.behaviours.setdefault(key, Behaviour())
        b.attempts += 1
        if b.mode == "fail_always" or (b.mode == "fail_once" and b.attempts == 1):
            raise RuntimeError(f"{key} failed to start")
        if key not in WINDOW or b.mode == "never":
            return
        if b.mode == "dialog" and key in DIALOG:
            self.windows.append(DIALOG[key])
            self.fg = DIALOG[key]
        elif b.mode == "slow":
            self._pending.append((self.polls + b.slow_polls, WINDOW[key]))
        else:
            self.windows.append(WINDOW[key])
            self.fg = WINDOW[key]

    # ---- SystemControl
    def can_observe(self) -> bool:
        return True

    def list_windows(self) -> list[tuple[str, str]]:
        self.polls += 1
        for due, w in list(self._pending):
            if self.polls >= due:
                self.windows.append(w)
                self._pending.remove((due, w))
        return list(self.windows)

    def foreground(self) -> tuple[str, str]:
        return self.fg

    def focus(self, needles: list[str]) -> str | None:
        for title, exe in self.windows:
            if any(n and n.lower() in title.lower() for n in needles):
                self.fg = (title, exe)
                return title
        return None

    def send_keys(self, combo: str) -> None:
        self.keys.append(combo)
        if combo == "esc" and self.fg in DIALOG.values():
            self.windows = [w for w in self.windows if w != self.fg]
            self.fg = ("", "")
            for k, d in DIALOG.items():  # the app was waiting behind the dialog
                if d in {("Update available", "spotify.exe")} and self.behaviours.get(k, Behaviour()).mode == "dialog":
                    self.windows.append(WINDOW[k])
                    self.fg = WINDOW[k]

    def close_process(self, image: str) -> tuple[bool, str]:
        before = len(self.windows)
        self.windows = [w for w in self.windows if w[1] != image.lower()]
        self.closed.append(image)
        return (len(self.windows) < before, "" if len(self.windows) < before else "process not found")

    def key(self, name: str, times: int = 1) -> None:
        self.keys.append(name)

    def set_volume(self, percent: int) -> None:
        self.keys.append(f"volume={percent}")

    def paste_text(self, text: str) -> None:
        self.keys.append("paste:" + text)

    def open_uri(self, uri: str) -> None:
        self.launched.append((uri, "uri"))
