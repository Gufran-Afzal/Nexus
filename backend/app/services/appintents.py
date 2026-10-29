"""Voice/text commands that drive integrated apps and Windows itself.

`recognise` is strict (it only claims a sentence when it clearly is such a command and, for app commands, when the
app is one the user integrated), so ordinary questions still reach the model. `run` performs the command through two
injectable seams (`launcher` and `SystemControl`), so everything is testable without Windows.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, quote_plus, urlparse

from app.services import appcontrol
from app.services.preferences import Preferences
from app.windows.sysctl import SETTINGS_PAGES, SystemControl, SystemControlError

KINDS = {
    "close_app", "focus_app", "app_search", "app_play", "app_open_folder", "web_search", "compose_email", "media",
    "volume", "lock", "screenshot", "show_desktop", "settings_page",
}

Launcher = Callable[[list[str] | str, str], None]
_SETTINGS_WORDS = "|".join(sorted((k for k in SETTINGS_PAGES if k), key=len, reverse=True))


def match_known(known: set[str], spoken: str) -> str | None:
    """Which known app name does `spoken` mean? (exact, whole-word containment, then close spelling)"""
    import difflib

    q = appcontrol.norm(spoken)
    if not q:
        return None
    if q in known:
        return q
    qt = set(q.split())
    extra_ok = {"browser", "editor", "player", "client", "desktop", "studio"}  # "chrome browser" still means chrome
    hits = [n for n in known if qt <= set(n.split()) or (set(n.split()) <= qt and len(n) >= 4 and (qt - set(n.split())) <= extra_ok)]  # noqa: E501
    if hits:
        return min(hits, key=len)
    best = max(((difflib.SequenceMatcher(None, q.replace(" ", ""), n.replace(" ", "")).ratio(), n) for n in known),
               default=(0.0, ""))
    return best[1] if best[0] >= 0.84 else None


def _app(known: set[str], phrase: str) -> str | None:
    """The app a command refers to. "in my code" / "in my notes" are places, not apps, so a leading "my" is rejected."""
    return None if re.match(r"^my\b", phrase.strip().lower()) else match_known(known, phrase)


def recognise(t: str, low: str, known: set[str]) -> tuple[str, dict[str, Any], str] | None:
    # ---- app commands (the app must be integrated)
    m = re.match(r"^(?:close|quit|exit|kill|shut down|shutdown)\s+(?P<app>.+)$", low)
    if m:
        app = _app(known, m.group("app"))
        if app:
            return "close_app", {"name": app}, f"Close {app}"
    m = re.match(r"^(?:switch to|go to|focus(?: on)?|bring up|show me|show)\s+(?P<app>.+)$", low)
    if m:
        app = _app(known, m.group("app"))
        if app:
            return "focus_app", {"name": app}, f"Switch to {app}"
    m = re.match(r"^(?:search(?: for)?|look up|find|google)\s+(?P<q>.+?)\s+(?:on|in|with|using)\s+(?P<app>.+)$", low)
    if m:
        app = _app(known, m.group("app"))
        if app:
            q = re.sub(r"^(?:for\s+)?", "", t[m.start("q"):m.end("q")], flags=re.I).strip()
            return "app_search", {"name": app, "query": q}, f"Search {q} in {app}"
    m = re.match(r"^play\s+(?P<q>.+?)\s+(?:on|in|with|using)\s+(?P<app>.+)$", low)
    if m:
        app = _app(known, m.group("app"))
        if app:
            return "app_play", {"name": app, "query": t[m.start("q"):m.end("q")].strip()}, f"Play {m.group('q')} on {app}"
    m = re.match(r"^(?:open|launch|start)\s+(?:my\s+|the\s+)?(?P<f>.+?)\s+(?:folder\s+)?(?:in|with|using)\s+(?P<app>.+)$", low)
    if m and not re.search(r"\.[a-z]{2,4}(?:/|$)", m.group("f")):
        app = _app(known, m.group("app"))
        if app:
            return "app_open_folder", {"name": app, "folder": m.group("f").strip()}, f"Open {m.group('f')} in {app}"

    # ---- web search and email (default browser / mail app; never sends anything)
    m = re.match(r"^(?:google|bing|duckduckgo)\s+(?P<q>.+)$", low) or re.match(
        r"^search (?:the )?(?:web|internet|online)(?: for)?\s+(?P<q>.+)$", low) or re.match(
        r"^(?:search for|look up)\s+(?P<q>.+?)\s+(?:online|on the web|on google)$", low)
    if m:
        q = t[m.start("q"):m.end("q")].strip()
        return "web_search", {"query": q}, f"Search the web for {q}"
    m = re.match(r"^(?:compose|write|draft|start|create|send)\s+(?:me\s+)?(?:a|an|new)\s+(?:e-?mail|mail)"
                 r"(?:\s+to\s+(?P<to>[\w.+-]+@[\w-]+(?:\.[\w-]+)+))?(?:\s+(?:about|regarding|with subject|subject)\s+(?P<s>.+))?$", low)  # noqa: E501
    if m:
        subj = t[m.start("s"):m.end("s")].strip() if m.group("s") else ""
        return "compose_email", {"to": m.group("to") or "", "subject": subj}, "Start a new email draft"

    # ---- media keys, volume, lock, screenshot, settings
    media_words = "music|media|song|songs|video|playback|track|audio"
    if re.match(rf"^(?:pause|resume|stop|play|continue)(?:\s+(?:the|my))?\s+(?:{media_words})$", low) \
            or low in ("play pause", "play/pause", "pause", "pause it", "resume playback"):
        return "media", {"key": "play_pause"}, "Play/pause"
    if re.match(r"^(?:next|skip)(?:\s+(?:this|the))?(?:\s+(?:song|track))?$", low) and low != "next":
        return "media", {"key": "next"}, "Next track"
    if re.match(r"^(?:previous|last|go back)(?:\s+a)?\s+(?:song|track)$|^previous$", low):
        return "media", {"key": "prev"}, "Previous track"
    m = re.match(r"^(?:set\s+(?:the\s+)?volume\s+to|volume\s+to|volume\s+at)\s+(?P<n>\d{1,3})(?:\s*(?:%|percent))?$", low)
    if m:
        return "volume", {"action": "set", "percent": min(100, int(m.group("n")))}, f"Volume {min(100, int(m.group('n')))}%"
    m = re.match(r"^(?:(?:turn|volume)\s+(?:the\s+)?(?:volume\s+)?(?P<d>up|down)|(?P<d2>increase|raise|lower|decrease|reduce)\s+(?:the\s+)?volume|"  # noqa: E501
                 r"(?P<d3>louder|quieter))(?:\s+by\s+(?P<n>\d{1,2}))?$", low)
    if m:
        word = m.group("d") or m.group("d2") or m.group("d3") or ""
        up = word in ("up", "increase", "raise", "louder")
        return "volume", {"action": "up" if up else "down", "steps": max(1, min(25, int(m.group("n") or 10) // 2))}, \
            "Volume " + ("up" if up else "down")
    if re.match(r"^volume (?:up|down)$", low):
        up = low.endswith("up")
        return "volume", {"action": "up" if up else "down", "steps": 5}, "Volume " + ("up" if up else "down")
    if re.match(r"^(?:mute|unmute)(?:\s+(?:the|my))?(?:\s+(?:sound|volume|audio|computer|pc|speakers|laptop))?$", low) \
            and low not in ("mute", "unmute"):
        return "volume", {"action": "mute"}, "Mute / unmute"
    if re.match(r"^lock(?:\s+(?:my|the))?\s+(?:computer|pc|screen|laptop|windows)$", low):
        return "lock", {}, "Lock the computer"
    if re.match(r"^(?:take\s+(?:a\s+)?)?(?:screenshot|screen shot|snip)(?:\s+of(?:\s+the)?\s+screen)?$|^take a screenshot$", low):
        return "screenshot", {}, "Open the screenshot tool"
    if re.match(r"^(?:show|go to|minimi[sz]e everything and show)\s+(?:the\s+)?desktop$|^minimi[sz]e (?:all|everything)$", low):
        return "show_desktop", {}, "Show the desktop"
    m = re.match(rf"^open\s+(?:the\s+)?(?:windows\s+)?(?P<p>{_SETTINGS_WORDS})\s+settings$|^open\s+(?:windows\s+)?settings$", low)
    if m:
        return "settings_page", {"page": (m.group("p") or "").strip()}, "Open Windows settings"
    return None


def _launch(ref: appcontrol.AppRef, launcher: Launcher, extra: list[str] | None = None) -> None:
    extra = extra or []
    if ref.kind == "exe":
        argv = [ref.target, *ref.args, *extra]
        launcher(argv if len(argv) > 1 else ref.target, "app")
    elif ref.kind == "uwp":
        launcher(ref.target, "uwp")
    elif ref.kind == "shortcut":
        launcher(ref.target, "shortcut")
    else:
        launcher(ref.target, "uri")


def launch_app(ref: appcontrol.AppRef, launcher: Launcher) -> None:
    _launch(ref, launcher)


def run(kind: str, p: dict[str, Any], prefs: Preferences, launcher: Launcher, system: SystemControl) -> tuple[str, str]:
    """Returns (status, message). Messages say what NEXUS actually did, not what it hopes happened."""
    try:
        if kind in ("close_app", "focus_app", "app_search", "app_play", "app_open_folder"):
            ref = appcontrol.find(prefs, p["name"])
            if ref is None:
                return "unsupported", f"'{p['name']}' is not one of your integrated apps. Add it on the Apps page."
            prof = appcontrol.profile(ref)
            if kind == "close_app":
                if not ref.exe_name:
                    return "unsupported", f"I can't close {ref.name} by name (Store apps have no program name I can use). Close it yourself."  # noqa: E501
                ok, out = system.close_process(ref.exe_name)
                return ("succeeded", f"Asked {ref.name} to close (it may ask to save first).") if ok else (
                    "failed", f"{ref.name} doesn't seem to be running." if "not found" in out.lower() else f"Couldn't close {ref.name}: {out}")  # noqa: E501
            if kind == "focus_app":
                stem = ref.exe_name[:-4] if ref.exe_name.endswith(".exe") else ""
                title = system.focus([ref.name, stem])
                if title:
                    return "succeeded", f"Switched to {ref.name}."
                _launch(ref, launcher)
                return "succeeded", f"{ref.name} wasn't open, so I started it."
            if kind == "app_search":
                if prof != "browser":
                    return "unsupported", f"{ref.name} isn't a web browser, so I can't search in it. Try \"search the web for ...\"."  # noqa: E501
                url = _search_url(prefs, p["query"])
                _launch(ref, launcher, [url])
                return "succeeded", f"Searching for \"{p['query']}\" in {ref.name}."
            if kind == "app_play":
                if prof != "spotify":
                    return "unsupported", f"I only know how to search and play in Spotify, not {ref.name}."
                launcher("spotify:search:" + quote(p["query"]), "uri")
                return "succeeded", (f"Opened Spotify's results for \"{p['query']}\". I can't press play inside it; say "
                                     "\"play music\" to send the play key.")
            if kind == "app_open_folder":
                if prof not in ("vscode", "app"):
                    return "unsupported", f"I don't know how to open a folder in {ref.name}."
                name = p["folder"].lower()
                match = next((f for f in prefs.approved_folders if _leaf(f) == name), None)
                if not match:
                    return "unsupported", f"No approved folder named '{p['folder']}'. Approve folders in Settings."
                _launch(ref, launcher, [match])
                return "succeeded", f"Opened {_leaf(match)} in {ref.name}."
        if kind == "web_search":
            launcher(_search_url(prefs, p["query"]), "url")
            return "succeeded", f"Searching the web for \"{p['query']}\" in your browser."
        if kind == "compose_email":
            uri = "mailto:" + quote(p.get("to", ""))
            if p.get("subject"):
                uri += "?subject=" + quote(p["subject"])
            launcher(uri, "uri")
            return "succeeded", "Opened a new email draft in your mail app. Nothing is sent until you press Send."
        if kind == "media":
            system.key(p["key"])
            return "succeeded", {"play_pause": "Sent play/pause.", "next": "Next track.", "prev": "Previous track."}[p["key"]]
        if kind == "volume":
            if p["action"] == "set":
                system.set_volume(p["percent"])
                return "succeeded", f"Volume set to about {p['percent']}%."
            if p["action"] == "mute":
                system.key("mute")
                return "succeeded", "Toggled mute."
            system.key(p["action"], p["steps"])
            return "succeeded", "Volume " + p["action"] + "."
        if kind == "lock":
            system.lock()
            return "succeeded", "Locking the computer."
        if kind == "screenshot":
            system.open_uri("ms-screenclip:")
            return "succeeded", "Opened the screenshot tool. Drag to capture; it goes to your clipboard."
        if kind == "show_desktop":
            system.show_desktop()
            return "succeeded", "Showing the desktop."
        if kind == "settings_page":
            page = p.get("page", "")
            system.open_uri(SETTINGS_PAGES.get(page, "ms-settings:"))
            return "succeeded", f"Opened Windows {page + ' ' if page else ''}settings."
    except SystemControlError as e:
        return "unsupported", str(e)
    return "unsupported", "I can't perform that action."


def _leaf(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1].lower()


def _search_url(prefs: Preferences, query: str) -> str:
    tpl = prefs.search_url if "{q}" in prefs.search_url and urlparse(prefs.search_url).scheme == "https" else "https://duckduckgo.com/?q={q}"
    return tpl.replace("{q}", quote_plus(query[:300]))
