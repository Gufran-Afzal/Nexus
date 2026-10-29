"""Bounded, transparent context construction from approved workspace files.

Strategy:
  1. Files the user explicitly selected are included first.
  2. Remaining budget goes to the best-ranked files: BM25 keyword ranking from the cached index,
     fused (reciprocal rank fusion) with semantic hits when the optional embedding index is enabled.
  3. A ranked file that is too large is included as an excerpt around the relevant lines when known.
  4. Files that don't fit are reported as omitted, with a reason.
Token counts are estimated at ~4 characters per token, which is conservative for code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from app.retrieval.index import get_index, tokenize
from app.workspaces.files import read_text_file
from app.workspaces.security import AccessDenied

CHARS_PER_TOKEN = 4
RRF_K = 30


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def keywords(query: str) -> list[str]:
    return list(dict.fromkeys(tokenize(query)))[:20]


@dataclass
class ContextFile:
    path: str
    tokens: int
    reason: str
    truncated: bool = False


@dataclass
class BuiltContext:
    text: str
    included: list[ContextFile] = field(default_factory=list)
    omitted: list[dict] = field(default_factory=list)
    tokens: int = 0


@dataclass
class Hit:
    path: str
    reason: str
    start_line: int | None = None
    end_line: int | None = None


def rank_files(root: Path, query: str, excludes: list[str], semantic: list[Hit] | None = None,
               limit: int = 20) -> list[Hit]:
    idx = get_index(root)
    idx.refresh(excludes)
    keyword = idx.search(query, limit=limit)
    scores: dict[str, float] = {}
    hits: dict[str, Hit] = {}
    for rank, (_s, rel) in enumerate(keyword):
        scores[rel] = scores.get(rel, 0) + 1 / (RRF_K + rank)
        hits[rel] = Hit(rel, "keyword match")
    for rank, h in enumerate(semantic or []):
        scores[h.path] = scores.get(h.path, 0) + 1 / (RRF_K + rank)
        prev = hits.get(h.path)
        hits[h.path] = Hit(h.path, "keyword + semantic match" if prev else "semantic match", h.start_line, h.end_line)
    order = sorted(scores, key=lambda r: (-scores[r], r))
    return [hits[r] for r in order[:limit]]


def build_context(
    root: Path,
    query: str,
    explicit_files: list[str],
    budget_tokens: int,
    excludes: list[str],
    max_file_bytes: int,
    auto_retrieve: bool = True,
    max_auto_files: int = 6,
    semantic: list[Hit] | None = None,
) -> BuiltContext:
    ctx = BuiltContext(text="")
    blocks: list[str] = []
    remaining = budget_tokens

    def add(rel: str, reason: str, start: int | None = None, end: int | None = None) -> None:
        nonlocal remaining
        try:
            fc = read_text_file(root, rel, max_file_bytes)
        except (AccessDenied, FileNotFoundError, OSError) as e:
            ctx.omitted.append({"path": rel, "reason": str(e) or e.__class__.__name__})
            return
        header = f'<file path="{fc.rel}">\n'
        footer = "\n</file>"
        body = fc.text
        truncated = False
        if estimate_tokens(header + body + footer) > remaining:
            lines = fc.text.splitlines()
            if start is not None and remaining > 300:
                # Excerpt around the relevant lines (semantic hit), as much as fits.
                pad = 15
                a, b = max(1, start - pad), min(len(lines), (end or start) + pad)
                excerpt = "\n".join(f"{i}| {ln}" for i, ln in enumerate(lines[a - 1 : b], a))
                header = f'<file path="{fc.rel}" lines="{a}-{b}" note="excerpt">\n'
                body = excerpt[: (remaining - 60) * CHARS_PER_TOKEN]
                truncated = True
                reason = f"{reason} (lines {a}-{b})"
            elif reason == "selected" and remaining > 400:
                body = fc.text[: (remaining - 50) * CHARS_PER_TOKEN] + "\n... [truncated to fit the context window]"
                truncated = True
            else:
                need = estimate_tokens(header + fc.text + footer)
                ctx.omitted.append({"path": fc.rel, "reason": f"does not fit the context budget (~{need} tokens)"})
                return
        block = header + body + footer
        t = estimate_tokens(block)
        blocks.append(block)
        remaining -= t
        ctx.included.append(ContextFile(path=fc.rel, tokens=t, reason=reason, truncated=truncated))

    for rel in explicit_files:
        add(rel, "selected")
    if auto_retrieve and query.strip():
        chosen = {f.path for f in ctx.included} | {o["path"] for o in ctx.omitted}
        n = 0
        for hit in rank_files(root, query, excludes, semantic):
            if hit.path in chosen:
                continue
            if n >= max_auto_files or remaining < 200:
                break
            add(hit.path, hit.reason, hit.start_line, hit.end_line)
            n += 1
    ctx.text = "\n\n".join(blocks)
    ctx.tokens = budget_tokens - remaining
    return ctx
