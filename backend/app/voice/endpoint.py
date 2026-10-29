"""End-of-sentence judgement for spoken input.

A pause is not the same as "finished". People stop mid-sentence to think ("remind me to call the bank ... and
also buy milk"). NEXUS therefore does not stop at the first silence if what it heard so far reads as unfinished: it
keeps listening a little longer, and only gives up on a long, clear silence.
"""

from __future__ import annotations

import re

# Words a finished sentence almost never ends with.
CONTINUATION_WORDS = frozenset("""
and or but so because then if unless although while when whenever where whether that which who whom whose
to of for with without from by at in on into onto about as than like also plus except per via
the a an my your his her its our their this these those some any each every
is are was were be been am will would can could should shall may might do does did have has had
um uh er erm hmm well okay ok
i we you it they he she
not no
""".split())

TERMINAL = re.compile(r"[.!?][\"')\]]*$")
CONTINUING_PUNCT = re.compile(r"(,|;|:|-|—|…|\.\.\.)\s*$")


def looks_complete(text: str) -> bool:
    """True if the transcript reads like a finished utterance. Empty text counts as complete (nothing to wait for)."""
    t = text.strip()
    if not t:
        return True
    if CONTINUING_PUNCT.search(t):
        return False
    if TERMINAL.search(t):
        return True
    words = re.findall(r"[a-z']+", t.lower())
    if not words:
        return True
    return words[-1] not in CONTINUATION_WORDS
