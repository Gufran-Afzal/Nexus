"""Incremental in-memory keyword index (BM25) per workspace.

Files are tokenised once and re-tokenised only when their modification time or size changes, so a
chat message on a large project costs one directory walk (stat calls) instead of reading every file.
Identifiers are split the way programmers search: `parseHttpRequest` -> parse, http, request.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.workspaces.files import is_binary, walk_files

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
STOP = {
    "the", "and", "for", "with", "this", "that", "from", "into", "what", "why", "how", "does", "can", "you",
    "please", "fix", "add", "make", "code", "file", "function", "error", "bug", "should", "when", "where", "are",
    "not", "have", "has", "use", "using", "want", "need", "explain", "self", "return", "import", "def", "class",
    "const", "let", "var", "none", "null", "true", "false", "else", "elif", "while", "public", "private", "new",
    "void", "int", "str", "string", "async", "await", "export", "default",
}
MAX_INDEX_BYTES = 300_000
K1, B = 1.4, 0.75


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for ident in _IDENT.findall(text):
        low = ident.lower()
        if len(low) > 2 and low not in STOP:
            out.append(low)
        if "_" in ident or any(c.isupper() for c in ident[1:]):
            for part in re.split(r"_+", ident):
                for sub in _CAMEL.findall(part):
                    s = sub.lower()
                    if len(s) > 2 and s not in STOP and s != low:
                        out.append(s)
    return out


@dataclass
class _Doc:
    mtime: float
    size: int
    terms: Counter[str]
    length: int
    path_terms: set[str]


class WorkspaceIndex:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.docs: dict[str, _Doc] = {}
        self.df: Counter[str] = Counter()
        self._lock = threading.Lock()

    def refresh(self, excludes: list[str], max_files: int = 20_000) -> int:
        """Bring the index up to date. Returns the number of files (re)tokenised."""
        with self._lock:
            seen: set[str] = set()
            changed = 0
            for entry in walk_files(self.root, excludes, limit=max_files):
                seen.add(entry.rel)
                full = self.root / entry.rel
                try:
                    mtime = full.stat().st_mtime
                except OSError:
                    continue
                old = self.docs.get(entry.rel)
                if old and old.mtime == mtime and old.size == entry.size:
                    continue
                terms: Counter[str] = Counter()
                if entry.size <= MAX_INDEX_BYTES:
                    try:
                        data = full.read_bytes()
                    except OSError:
                        continue
                    if not is_binary(data):
                        terms = Counter(tokenize(data.decode("utf-8", errors="replace")))
                if old:
                    self.df.subtract(old.terms.keys())
                self.docs[entry.rel] = _Doc(mtime, entry.size, terms, sum(terms.values()),
                                            set(tokenize(entry.rel.replace("/", " ").replace(".", " "))))
                self.df.update(terms.keys())
                changed += 1
            for rel in [r for r in self.docs if r not in seen]:
                self.df.subtract(self.docs.pop(rel).terms.keys())
            self.df = +self.df  # drop zero counts
            return changed

    def search(self, query: str, limit: int = 20) -> list[tuple[float, str]]:
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return []
        with self._lock:
            n = max(len(self.docs), 1)
            avg = (sum(d.length for d in self.docs.values()) / n) or 1.0
            scored: list[tuple[float, str]] = []
            for rel, d in self.docs.items():
                score = 0.0
                for t in q:
                    if t in d.path_terms:
                        score += 3.0
                    tf = d.terms.get(t, 0)
                    if tf:
                        idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                        score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * d.length / avg))
                if score > 0:
                    scored.append((score, rel))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[:limit]


_indexes: dict[str, WorkspaceIndex] = {}
_global_lock = threading.Lock()


def get_index(root: Path) -> WorkspaceIndex:
    key = str(root.resolve())
    with _global_lock:
        idx = _indexes.get(key)
        if idx is None:
            idx = _indexes[key] = WorkspaceIndex(root)
        return idx
