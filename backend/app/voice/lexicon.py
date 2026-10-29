"""Your personal vocabulary for speech recognition.

Whisper is good at ordinary English and bad at *your* words: app names ("Spotify", "WhatsApp"), the names of the people you
message, your own custom words. Two cheap, deterministic fixes (no model needed, so they also work offline and instantly):

1. `prompt()` / `hotwords()` tell Whisper those words are likely, which makes it spell them right in the first place.
2. `snap()` repairs what still comes out wrong afterwards: known mishearings ("what's app" -> "WhatsApp") and near-misses of
   your own words ("spotifi" -> "Spotify"). It only touches words that are not ordinary English, so "mom" never becomes "Tom".
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

# Words people say to an assistant that Whisper often splits or misspells. (pattern, replacement)
KNOWN_FIXES: list[tuple[str, str]] = [
    (r"what'?s\s*app|whats\s*up\s+app|watts\s*app|what\s+sapp|whatsup", "WhatsApp"),
    (r"g\s*mail|gee\s*mail|jee\s*mail", "Gmail"),
    (r"you\s+tube", "YouTube"),
    (r"e[\s-]+mail", "email"),
    (r"spot\s*(?:ify|ifi|ifye|a\s*fi|a\s*five|e\s*fi)", "Spotify"),
    (r"v\.?\s*s\.?\s*code|vs\s+code|visual\s+studio\s+code", "VS Code"),
    (r"power\s+point", "PowerPoint"),
    (r"one\s+drive", "OneDrive"),
    (r"blue\s+tooth", "Bluetooth"),
    (r"why\s*-?\s*fi|wi\s*-?\s*fi|wife\s*i", "Wi-Fi"),
    (r"note\s+pad", "Notepad"),
    (r"tele\s*gram", "Telegram"),
    (r"zoom\s+call", "Zoom call"),
    (r"google\s+calender", "Google Calendar"),
    (r"(?<![a-z])to[\s-]+do\s+list", "to-do list"),
]
_FIX_RX = [(re.compile(rf"\b(?:{p})\b", re.I), r) for p, r in KNOWN_FIXES]

# Ordinary words that must never be "corrected" into one of your terms (a small stop list; real English is far larger, so
# snapping also requires a very close match).
COMMON = set("""a about above after again all also am an and any are as at be because been before being below between both but
by can could did do does doing down during each few for from further had has have having he her here hers him his how i if in
into is it its just me more most my no nor not now of off on once only or other our out over own same she should so some such than
that the their them then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yes yeah okay ok please open close send message text email mail call set
remind reminder timer alarm play pause next stop start search find show tell write type note notes add make take turn volume mute
lock screen
mum mom dad""".split())

PHRASES = ["Open Spotify.", "Send a message to Mom on WhatsApp.", "Write an email to Sam in Gmail.",
           "Remind me tomorrow at 9 a.m. to call the dentist.", "Set a timer for ten minutes."]


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _skeleton(s: str) -> str:
    """A rough sound key: drops vowels after the first letter, collapses doubles, folds common sound-alikes."""
    s = _key(s)
    for a, b in (("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("x", "ks"), ("wh", "w")):
        s = s.replace(a, b)
    if not s:
        return ""
    out = s[0]
    for ch in s[1:]:
        if ch not in "aeiouyhw" and ch != out[-1]:
            out += ch
    return out


class Lexicon:
    def __init__(self, terms: Iterable[str] = (), names: Iterable[str] = ()) -> None:
        """`terms` (apps, custom words) are repaired by snap(). `names` (people) only help Whisper spell them: turning
        one real name into another ("Alexandra" -> "Alexander") would send a message to the wrong person, so the message
        skills match names themselves and ask when they are not sure."""
        seen: dict[str, str] = {}
        for t in [*terms, *names]:
            t = " ".join(str(t).split()).strip()
            if 2 <= len(t) <= 40 and _key(t):
                seen.setdefault(t.lower(), t)
        self.terms = list(seen.values())
        protected = {" ".join(str(n).split()).lower() for n in names}
        self._keys = [(_key(t), _skeleton(t), t) for t in self.terms if _key(t) not in COMMON and t.lower() not in protected]

    # ---- hints for Whisper
    def prompt(self, limit: int = 420) -> str:
        """Initial prompt: a few realistic sentences plus the user's own words. Whisper reads the *style* and spelling."""
        names = ", ".join(self.terms[:40])
        text = " ".join(PHRASES[:3])
        if names:
            text += f" Names and apps: {names}."
        return text[:limit]

    def hotwords(self, limit: int = 24) -> str:
        return " ".join(self.terms[:limit])

    # ---- repair afterwards
    def snap(self, text: str) -> str:
        """Fix known mishearings, then near-misses of the user's own words. Cheap, deterministic, conservative."""
        out = text
        for rx, rep in _FIX_RX:
            out = rx.sub(rep, out)
        if not self._keys:
            return out
        words = out.split(" ")
        i, result = 0, []
        while i < len(words):
            replaced = False
            for n in (3, 2, 1):  # prefer the longest span ("space ify" is two words)
                if i + n > len(words):
                    continue
                span = words[i:i + n]
                cores = [re.sub(r"^\W+|\W+$", "", w) for w in span]
                # Joining words ("spot a fi") is only plausible when every piece is a real fragment, never an everyday
                # word ("on", "i'm"): otherwise "on WhatsApp" would swallow the "on".
                if n > 1 and any(len(_key(c)) < 3 or _key(c) in COMMON for c in cores):
                    continue
                core = " ".join(cores)
                hit = self._match(core)
                if hit:
                    lead = re.match(r"^\W*", span[0]).group(0)  # type: ignore[union-attr]
                    trail = re.search(r"\W*$", span[-1]).group(0)  # type: ignore[union-attr]
                    result.append(f"{lead}{hit}{trail}")
                    i += n
                    replaced = True
                    break
            if not replaced:
                result.append(words[i])
                i += 1
        return " ".join(result)

    def _match(self, core: str) -> str | None:
        k = _key(core)
        if len(k) < 4 or k in COMMON:
            return None
        sk = _skeleton(core)
        best: tuple[float, str] | None = None
        for tk, ts, term in self._keys:
            if k == tk:
                return None  # right word already (case is irrelevant to matching, so it is left alone)
            if abs(len(k) - len(tk)) > 3:
                continue
            ratio = difflib.SequenceMatcher(None, k, tk).ratio()
            need = 0.84 if len(tk) >= 6 else 0.9
            # Same sound skeleton ("dmitry" / "Dmitri") is accepted at a lower spelling similarity, but never so low that
            # one real name turns into another ("Alexandra" must not become "Alexander").
            if ratio >= need or (ts and ts == sk and len(tk) >= 5 and ratio >= 0.8 and abs(len(k) - len(tk)) <= 1):
                if best is None or ratio > best[0]:
                    best = (ratio, term)
        return best[1] if best else None


def build_lexicon(app_names: Iterable[str], contact_names: Iterable[str], custom_words: Iterable[str] = (),
                  assistant_name: str = "Nexus") -> Lexicon:
    base = ["WhatsApp", "Gmail", "Spotify", "YouTube", "Outlook", "Chrome", "Teams", "Zoom", "Notepad", "Telegram"]
    return Lexicon([assistant_name, *custom_words, *app_names, *base], names=contact_names)
