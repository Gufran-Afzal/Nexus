"""Optional semantic search over a workspace using a local Ollama embedding model.

Off by default. When the user enables it for a project and builds the index, files are split into
overlapping line windows, embedded locally (e.g. `nomic-embed-text`), and stored as normalised
float32 vectors in SQLite. Rebuilds are incremental (only files whose mtime changed). Queries embed
the question once and rank chunks by cosine similarity; results are fused with keyword ranking.
The embedding model is requested with a short keep_alive so it doesn't stay in RAM next to the chat model.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import SemanticChunk, Workspace
from app.providers.base import LLMProvider
from app.retrieval.context import Hit
from app.workspaces.files import is_binary, walk_files

log = logging.getLogger(__name__)

CHUNK_LINES = 60
OVERLAP = 10
MAX_CHUNK_CHARS = 2400
BATCH = 16
MAX_FILE_BYTES = 300_000
TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".cs", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp",
    ".hpp", ".swift", ".scala", ".sql", ".sh", ".ps1", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json",
    ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".dart", ".r", ".m", ".ini", ".cfg", ".xml", ".gradle",
}
EMBED_KEEP_ALIVE = "2m"


def chunk_text(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        j = min(len(lines), i + CHUNK_LINES)
        body = "\n".join(lines[i:j]).strip()
        if body:
            out.append((i + 1, j, body[:MAX_CHUNK_CHARS]))
        if j == len(lines):
            break
        i = j - OVERLAP
    return out


def normalise(vec: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / norm for x in vec)).tobytes()


@dataclass
class Job:
    workspace_id: int
    state: str = "running"  # running | done | failed | cancelled
    files_total: int = 0
    files_done: int = 0
    chunks: int = 0
    error: str | None = None
    started: float = field(default_factory=time.time)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


class SemanticIndexer:
    def __init__(self, db: Database, provider: LLMProvider, bus: EventBus) -> None:
        self.db, self.provider, self.bus = db, provider, bus
        self.jobs: dict[int, Job] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._cache: dict[int, tuple[list[tuple[str, int, int]], Any, str]] = {}

    # ------------------------------------------------------------------ status
    def status(self, workspace_id: int) -> dict[str, Any]:
        with self.db.session() as s:
            n = s.scalar(select(func.count(SemanticChunk.id)).where(SemanticChunk.workspace_id == workspace_id)) or 0
            files = s.scalar(select(func.count(func.distinct(SemanticChunk.path)))
                             .where(SemanticChunk.workspace_id == workspace_id)) or 0
            model = s.scalar(select(SemanticChunk.model).where(SemanticChunk.workspace_id == workspace_id).limit(1))
        job = self.jobs.get(workspace_id)
        return {"chunks": n, "files": files, "model": model,
                "job": None if not job else {k: v for k, v in job.__dict__.items() if k != "cancel"}}

    # ------------------------------------------------------------------ build
    def start(self, workspace_id: int, model: str) -> Job:
        job = self.jobs.get(workspace_id)
        if job and job.state == "running":
            return job
        job = Job(workspace_id)
        self.jobs[workspace_id] = job
        self._tasks[workspace_id] = asyncio.create_task(self._build(job, model))
        return job

    def cancel(self, workspace_id: int) -> bool:
        job = self.jobs.get(workspace_id)
        if job and job.state == "running":
            job.cancel.set()
            return True
        return False

    def clear(self, workspace_id: int) -> None:
        self.cancel(workspace_id)
        with self.db.session() as s:
            s.execute(delete(SemanticChunk).where(SemanticChunk.workspace_id == workspace_id))
        self._cache.pop(workspace_id, None)

    async def wait(self, workspace_id: int) -> None:
        t = self._tasks.get(workspace_id)
        if t:
            await asyncio.shield(t)

    async def shutdown(self) -> None:
        for j in self.jobs.values():
            j.cancel.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _publish(self, job: Job) -> None:
        self.bus.publish("index", workspace_id=job.workspace_id, state=job.state, files_done=job.files_done,
                         files_total=job.files_total, chunks=job.chunks, error=job.error)

    async def _build(self, job: Job, model: str) -> None:
        try:
            with self.db.session() as s:
                ws = s.get(Workspace, job.workspace_id)
                if not ws:
                    raise ValueError("workspace not found")
                root, excludes = Path(ws.path), list(ws.exclude_globs or [])
                stored: dict[str, tuple[float, str]] = {
                    p: (m, mod) for p, m, mod in s.execute(
                        select(SemanticChunk.path, func.max(SemanticChunk.file_mtime), func.max(SemanticChunk.model))
                        .where(SemanticChunk.workspace_id == job.workspace_id).group_by(SemanticChunk.path))
                }
            entries = await asyncio.to_thread(
                lambda: [e for e in walk_files(root, excludes) if Path(e.rel).suffix.lower() in TEXT_EXT
                         and e.size <= MAX_FILE_BYTES])
            present = {e.rel for e in entries}
            todo = []
            for e in entries:
                mtime = (root / e.rel).stat().st_mtime
                prev = stored.get(e.rel)
                if not prev or prev[0] != mtime or prev[1] != model:
                    todo.append((e.rel, mtime))
            removed = [p for p in stored if p not in present]
            if removed:
                with self.db.session() as s:
                    s.execute(delete(SemanticChunk).where(SemanticChunk.workspace_id == job.workspace_id,
                                                          SemanticChunk.path.in_(removed)))
            job.files_total = len(todo)
            self._publish(job)
            for rel, mtime in todo:
                if job.cancel.is_set():
                    job.state = "cancelled"
                    break
                data = (root / rel).read_bytes()
                if is_binary(data):
                    job.files_done += 1
                    continue
                chunks = chunk_text(data.decode("utf-8", errors="replace"))
                vectors: list[bytes] = []
                for i in range(0, len(chunks), BATCH):
                    batch = chunks[i : i + BATCH]
                    vecs = await self.provider.embed(model, [f"{rel}\n{c[2]}" for c in batch], EMBED_KEEP_ALIVE)
                    vectors.extend(normalise(v) for v in vecs)
                with self.db.session() as s:
                    s.execute(delete(SemanticChunk).where(SemanticChunk.workspace_id == job.workspace_id,
                                                          SemanticChunk.path == rel))
                    for (a, b, _t), vec in zip(chunks, vectors, strict=True):
                        s.add(SemanticChunk(workspace_id=job.workspace_id, path=rel, start_line=a, end_line=b,
                                            file_mtime=mtime, model=model, vector=vec))
                job.files_done += 1
                job.chunks += len(chunks)
                if job.files_done % 5 == 0:
                    self._publish(job)
            if job.state == "running":
                job.state = "done"
        except Exception as e:
            log.warning("semantic index failed: %s", e)
            job.state, job.error = "failed", str(e)
        finally:
            self._cache.pop(job.workspace_id, None)
            self._publish(job)
            self._tasks.pop(job.workspace_id, None)

    # ------------------------------------------------------------------ query
    def _load(self, workspace_id: int) -> tuple[list[tuple[str, int, int]], Any, str] | None:
        cached = self._cache.get(workspace_id)
        if cached:
            return cached
        with self.db.session() as s:
            rows = s.execute(select(SemanticChunk.path, SemanticChunk.start_line, SemanticChunk.end_line,
                                    SemanticChunk.vector, SemanticChunk.model)
                             .where(SemanticChunk.workspace_id == workspace_id)).all()
        if not rows:
            return None
        meta = [(r[0], r[1], r[2]) for r in rows]
        try:
            import numpy as np

            matrix: Any = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        except ImportError:
            matrix = [array.array("f", r[3]) for r in rows]
        self._cache[workspace_id] = (meta, matrix, rows[0][4])
        return self._cache[workspace_id]

    async def query(self, workspace_id: int, text: str, k: int = 8) -> list[Hit]:
        loaded = await asyncio.to_thread(self._load, workspace_id)
        if not loaded:
            return []
        meta, matrix, model = loaded
        qv = (await self.provider.embed(model, [text], EMBED_KEEP_ALIVE))[0]
        q = array.array("f", normalise(qv))
        try:
            import numpy as np

            scores = (matrix @ np.frombuffer(q.tobytes(), dtype=np.float32)).tolist()
        except (ImportError, TypeError):
            scores = [sum(a * b for a, b in zip(row, q, strict=False)) for row in matrix]
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        hits: list[Hit] = []
        seen: set[str] = set()
        for i in order:
            path, a, b = meta[i]
            if path in seen:
                continue
            seen.add(path)
            hits.append(Hit(path, "semantic match", a, b))
            if len(hits) >= k:
                break
        return hits
