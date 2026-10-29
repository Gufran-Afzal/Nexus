"""Which applications NEXUS may drive, how a spoken name is matched to one, and what you can say to each.

Only apps in Preferences.integrated_apps (plus the old `allowed_apps` name = exe list) can be launched, closed or
focused. Arguments come from the discovered shortcut or from fixed templates here, never from free text, and nothing
is ever run through a shell.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from app.services.preferences import IntegratedApp, Preferences

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe"}
EXTRA_OK = {"browser", "editor", "player", "client", "desktop", "studio"}  # "chrome browser" still means chrome
_NOISE = re.compile(r"\b(the|my|app|application|program|please)\b")


@dataclass
class AppRef:
    name: str
    kind: str
    target: str
    args: list[str] = field(default_factory=list)
    process: str = ""

    @property
    def exe_name(self) -> str:
        if self.process:
            return self.process.lower()
        if self.kind == "exe":
            base = PureWindowsPath(self.target).name.lower()
            base = base if base.endswith(".exe") else base + ".exe"
            return PROCESS_ALIASES.get(base, base).lower()
        return ""

    @property
    def is_browser(self) -> bool:
        return self.exe_name in BROWSERS


def norm(s: str) -> str:
    s = _NOISE.sub(" ", s.lower().replace("&", " and "))
    return " ".join(re.sub(r"[^a-z0-9+#. ]+", " ", s).split())


def _refs(prefs: Preferences) -> list[tuple[AppRef, list[str]]]:
    out: list[tuple[AppRef, list[str]]] = []
    seen: set[str] = set()
    for a in prefs.integrated_apps:
        if not a.enabled:
            continue
        ref = AppRef(a.name, a.kind, a.target, list(a.args), a.process)
        out.append((ref, [norm(a.name), *[norm(x) for x in a.aliases]]))
        seen.add(norm(a.name))
    for name, exe in prefs.allowed_apps.items():  # the old "name = exe" list keeps working
        if norm(name) not in seen:
            out.append((AppRef(name, "exe", exe), [norm(name)]))
    return out


def app_names(prefs: Preferences) -> set[str]:
    """Every spoken name that should mean 'an app': names and aliases of enabled apps."""
    return {n for _ref, names in _refs(prefs) for n in names if n}


def find(prefs: Preferences, spoken: str) -> AppRef | None:
    """Match what the user said to one integrated app: exact name/alias, then whole-word containment ("chrome" in
    "google chrome"; the shortest name wins), then a close spelling ("spotifi")."""
    q = norm(spoken)
    if not q:
        return None
    refs = _refs(prefs)
    for ref, names in refs:
        if q in names:
            return ref
    q_tokens = set(q.split())
    contain = [(len(min(names, key=len)), ref) for ref, names in refs
               for n in names if n and (q_tokens <= set(n.split()) or (
                   set(n.split()) <= q_tokens and len(n) >= 4 and (q_tokens - set(n.split())) <= EXTRA_OK))]
    if contain:
        return min(contain, key=lambda t: t[0])[1]
    best: tuple[float, AppRef] | None = None
    for ref, names in refs:
        for n in names:
            if not n:
                continue
            score = difflib.SequenceMatcher(None, q.replace(" ", ""), n.replace(" ", "")).ratio()
            if score >= 0.84 and (best is None or score > best[0]):
                best = (score, ref)
    return best[1] if best else None


def profile(ref: AppRef) -> str:
    """A rough role for an app, used for the "You can say" hints and for app-specific commands."""
    n = norm(ref.name)
    if ref.is_browser or any(b in n for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")):
        return "browser"
    if "spotify" in n:
        return "spotify"
    if "visual studio code" in n or n in ("code", "vs code", "vscode") or ref.exe_name == "code.exe":
        return "vscode"
    if any(k in n for k in ("outlook", "thunderbird", "mail")):
        return "mail"
    if any(k in n for k in ("teams", "zoom", "discord", "slack", "whatsapp", "telegram", "skype")):
        return "chat"
    if any(k in n for k in ("vlc", "media player", "groove", "itunes", "music", "netflix", "youtube")):
        return "media"
    return "app"


def examples(ref: AppRef) -> list[str]:
    n = ref.name
    ex = [f"Open {n}", f"Close {n}", f"Switch to {n}"]
    p = profile(ref)
    if p == "browser":
        ex.append(f"Search for cats in {n}")
    elif p == "spotify":
        ex.append(f"Play lo-fi beats on {n}")
    elif p == "vscode":
        ex.append(f"Open my projects folder in {n}")
    elif p == "mail":
        ex.append("Write an email to alex@example.com about the report")
    elif p == "media":
        ex.append("Pause the music · Next song · Volume up")
    return ex


PROCESS_ALIASES = {"calc.exe": "CalculatorApp.exe"}  # the launcher stub exits; the real window belongs to another process


def process_for(target: str) -> str:
    """The image name to close an exe by (`C:\\...\\chrome.exe` -> `chrome.exe`)."""
    base = PureWindowsPath(target).name
    if not base.lower().endswith(".exe"):
        return ""
    return PROCESS_ALIASES.get(base.lower(), base)


def add_or_update(prefs: Preferences, app: IntegratedApp) -> list[IntegratedApp]:
    """The integrated list with `app` added (or replacing the one of the same name)."""
    others = [a for a in prefs.integrated_apps if norm(a.name) != norm(app.name)]
    return [*others, app]
