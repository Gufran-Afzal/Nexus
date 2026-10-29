"""Finding the person a spoken request means. Wrong-person mistakes are the costly ones, so matching is explicit about how
sure it is: an exact name/nickname is trusted, a close spelling is only a *suggestion* that must be confirmed."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


@dataclass
class ContactRef:
    id: int
    name: str
    aliases: list[str] = field(default_factory=list)
    phone: str = ""
    email: str = ""


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", s.lower()).replace("'s", "").split())


def normalize_phone(raw: str) -> str | None:
    """Digits with country code, no plus sign (what WhatsApp links want). None if it can't be a phone number."""
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    return digits if 7 <= len(digits) <= 15 else None


EMAIL_RX = re.compile(r"^[\w.+'-]+@[\w-]+(?:\.[\w-]+)+$")


def spoken_email(text: str) -> str | None:
    """"alex at example dot com" / "alex@example.com" -> "alex@example.com"."""
    t = text.strip().lower().rstrip(".,!?")
    t = re.sub(r"\s+at\s+", "@", t) if "@" not in t else t
    t = re.sub(r"\s+dot\s+", ".", t)
    t = t.replace(" underscore ", "_").replace(" dash ", "-")
    t = t.replace(" ", "")
    return t if EMAIL_RX.match(t) else None


def _names(c: ContactRef) -> list[str]:
    return [n for n in [norm(c.name), *[norm(a) for a in c.aliases]] if n]


def find_contact(contacts: list[ContactRef], spoken: str) -> tuple[ContactRef, bool] | None:
    """(contact, exact). Exact = name or nickname, or a first name that only one person has. Not exact = a close spelling,
    to be confirmed with the user before anything is sent."""
    q = norm(spoken)
    if not q:
        return None
    for c in contacts:
        if q in _names(c):
            return c, True
    first = [c for c in contacts if q == norm(c.name).split(" ")[0]]
    if len(first) == 1:
        return first[0], True
    if len(first) > 1:
        return None  # "Alex" with two Alexes: ask rather than guess (the caller reports it as unknown)
    best: tuple[float, ContactRef] | None = None
    for c in contacts:
        for n in _names(c):
            r = difflib.SequenceMatcher(None, q, n).ratio()
            if r >= 0.8 and (best is None or r > best[0]):
                best = (r, c)
    return (best[1], False) if best else None


def split_recipient(contacts: list[ContactRef], words: list[str]) -> tuple[ContactRef, bool, list[str]] | None:
    """Take the longest start of `words` that names a contact: ["mom","i'm","late"] -> (Mom, exact, ["i'm","late"]).
    Exact names are tried first at every length; only then a close spelling, which must line up word for word with a real
    name (so "Alexander hello" can never be read as the two-word name "Alexander Hill")."""
    for k in (3, 2, 1):
        if len(words) < k:
            continue
        q = norm(" ".join(words[:k]))
        for c in contacts:
            if q in _names(c):
                return c, True, words[k:]
    for k in (3, 2, 1):
        if len(words) < k:
            continue
        hit = find_contact(contacts, " ".join(words[:k])) if k == 1 else None
        if hit and hit[1]:
            return hit[0], True, words[k:]
    best: tuple[float, ContactRef, int] | None = None
    for c in contacts:
        for n in _names(c):
            parts = n.split(" ")
            k = len(parts)
            if len(words) < k:
                continue
            spoken = norm(" ".join(words[:k])).split(" ")
            if len(spoken) != k:
                continue
            scores = [difflib.SequenceMatcher(None, a, b).ratio() for a, b in zip(spoken, parts, strict=True)]
            if min(scores) >= 0.75 and sum(scores) / k >= 0.8 and (best is None or sum(scores) / k > best[0]):
                best = (sum(scores) / k, c, k)
    return (best[1], False, words[best[2]:]) if best else None


def ambiguous(contacts: list[ContactRef], spoken: str) -> list[ContactRef]:
    q = norm(spoken)
    return [c for c in contacts if q and q == norm(c.name).split(" ")[0]] if q else []
