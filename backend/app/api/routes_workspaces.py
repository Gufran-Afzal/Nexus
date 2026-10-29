from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_state, get_workspace
from app.core.state import NexusState
from app.database.models import Workspace, utcnow
from app.providers.base import ChatMessage, GenerationOptions, ProviderError
from app.retrieval.context import build_context
from app.services.audit import audit
from app.services.preferences import load_preferences, model_for
from app.workspaces import files as wf
from app.workspaces.security import AccessDenied, validate_workspace_root

router = APIRouter(prefix="/api/workspaces")


def ws_dict(w: Workspace) -> dict[str, Any]:
    root = Path(w.path)
    return {"id": w.id, "name": w.name, "path": w.path, "exists": root.is_dir(), "allow_edits": w.allow_edits,
            "allow_commands": w.allow_commands, "exclude_globs": w.exclude_globs, "summary": w.summary,
            "preferences": w.preferences, "created_at": w.created_at, "last_opened_at": w.last_opened_at,
            "has_instructions": (root / wf.PROJECT_INSTRUCTIONS_FILE).is_file()}


def _root(w: Workspace) -> Path:
    root = Path(w.path)
    if not root.is_dir():
        raise HTTPException(410, f"The workspace folder no longer exists: {w.path}")
    return root


class WsCreate(BaseModel):
    path: str = Field(..., min_length=1, max_length=4000)
    name: str | None = None


class WsPatch(BaseModel):
    name: str | None = None
    allow_edits: bool | None = None
    allow_commands: bool | None = None
    exclude_globs: list[str] | None = Field(None, max_length=200)
    summary: str | None = Field(None, max_length=20_000)
    preferences: dict[str, Any] | None = None


@router.get("")
def list_ws(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [ws_dict(w) for w in s.query(Workspace).order_by(Workspace.last_opened_at.desc().nullslast()).all()]


@router.post("")
def add_ws(body: WsCreate, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Explicitly add a project folder. Nothing is read until you open it."""
    try:
        root = validate_workspace_root(body.path.strip().strip('"'))
    except AccessDenied as e:
        raise HTTPException(400, str(e)) from e
    existing = s.query(Workspace).filter(Workspace.path == str(root)).first()
    if existing:
        return ws_dict(existing)
    w = Workspace(name=(body.name or root.name)[:200], path=str(root))
    s.add(w)
    s.flush()
    audit(s, "user", "workspace.added", str(root))
    return ws_dict(w)


@router.get("/{ws_id}")
def get_ws(ws_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    w.last_opened_at = utcnow()
    return ws_dict(w)


@router.patch("/{ws_id}")
def patch_ws(ws_id: int, body: WsPatch, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(w, k, v)
    if "allow_edits" in changes or "allow_commands" in changes:
        audit(s, "user", "workspace.permissions", f"workspace:{ws_id}", allow_edits=w.allow_edits,
              allow_commands=w.allow_commands)
    return ws_dict(w)


@router.delete("/{ws_id}")
def remove_ws(ws_id: int, s: Session = Depends(get_session)) -> dict[str, bool]:
    """Forget the workspace in NEXUS (its files on disk are not touched)."""
    w = get_workspace(s, ws_id)
    s.delete(w)
    audit(s, "user", "workspace.removed", w.path)
    return {"removed": True}


@router.get("/{ws_id}/tree")
def tree(ws_id: int, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    return wf.build_tree(_root(w), list(w.exclude_globs or []), state.settings.max_tree_entries)


@router.get("/{ws_id}/file")
def read_file(ws_id: int, path: str, state: NexusState = Depends(get_state),
              s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    try:
        fc = wf.read_text_file(_root(w), path, state.settings.max_file_bytes)
    except AccessDenied as e:
        audit(s, "user", "file.read_denied", f"workspace:{ws_id}:{path}", outcome="denied", reason=str(e))
        raise HTTPException(403, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, "File not found") from e
    return {"path": fc.rel, "content": fc.text, "sha256": fc.sha256, "size": fc.size}


@router.get("/{ws_id}/search")
def search(ws_id: int, q: str, content: bool = False, state: NexusState = Depends(get_state),
           s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    return wf.search_files(_root(w), q, list(w.exclude_globs or []), content=content,
                           max_files=state.settings.max_search_files, max_bytes=state.settings.max_file_bytes)


@router.get("/{ws_id}/git")
def git(ws_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    root = _root(w)
    st = wf.git_status(root)
    if st.get("available"):
        st["diff"] = wf.git_diff(root)
    return st


@router.get("/{ws_id}/instructions")
def instructions(ws_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    w = get_workspace(s, ws_id)
    return {"file": wf.PROJECT_INSTRUCTIONS_FILE, "content": wf.project_instructions(_root(w))}


class ContextQuery(BaseModel):
    query: str = ""
    files: list[str] = Field(default_factory=list, max_length=50)
    auto: bool = True


@router.post("/{ws_id}/context-preview")
def context_preview(ws_id: int, body: ContextQuery, state: NexusState = Depends(get_state),
                    s: Session = Depends(get_session)) -> dict[str, Any]:
    """Show exactly which files would be sent to the model for this question."""
    w = get_workspace(s, ws_id)
    prefs = load_preferences(s)
    budget = int((prefs.generation.num_ctx - prefs.generation.max_output_tokens) * 0.55)
    ctx = build_context(_root(w), body.query, body.files, budget, list(w.exclude_globs or []),
                        state.settings.max_file_bytes, auto_retrieve=body.auto)
    return {"budget_tokens": budget, "used_tokens": ctx.tokens, "included": [f.__dict__ for f in ctx.included],
            "omitted": ctx.omitted}


@router.post("/{ws_id}/summarize")
async def summarize(ws_id: int, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Draft a project summary with the local model. It is NOT saved until you approve it (PATCH summary)."""
    with state.db.session() as s:
        w = get_workspace(s, ws_id)
        root = _root(w)
        prefs = load_preferences(s)
        excludes = list(w.exclude_globs or [])
    tree = wf.build_tree(root, excludes, 400)
    paths: list[str] = []

    def walk(n: dict) -> None:
        if n["type"] == "file":
            paths.append(n["path"])
        for c in n.get("children", []):
            walk(c)

    walk(tree["tree"])
    key_files = [p for p in paths if Path(p).name.lower() in (
        "readme.md", "package.json", "pyproject.toml", "requirements.txt", "cargo.toml", "go.mod", "nexus.md")][:5]
    ctx = build_context(root, "", key_files, int(prefs.generation.num_ctx * 0.5), excludes,
                        state.settings.max_file_bytes, auto_retrieve=False)
    prompt = ("Write a concise project summary (under 200 words): purpose, main technologies, structure, and how "
              "to run/test it, based ONLY on the file list and files below. Mark anything uncertain.\n\n"
              "Files:\n" + "\n".join(paths[:300]) + f"\n\n{ctx.text}")
    g = prefs.generation
    try:
        text = await state.llm.complete(prefs.active_model, [ChatMessage("user", prompt)],
                                        GenerationOptions(temperature=0.2, num_ctx=g.num_ctx, max_output_tokens=600,
                                                          keep_alive=g.keep_alive), purpose="summary")
    except ProviderError as e:
        raise HTTPException(502, f"Model unavailable: {e}") from e
    return {"draft": text.strip(), "based_on": [f.path for f in ctx.included], "file_count": len(paths)}


# ---------------------------------------------------------------- optional semantic index
@router.get("/{ws_id}/semantic-index")
def semantic_status(ws_id: int, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict:
    w = get_workspace(s, ws_id)
    return {**state.indexer.status(ws_id), "enabled": bool((w.preferences or {}).get("semantic_search")),
            "embedding_model": model_for(load_preferences(s), "embedding")}


@router.post("/{ws_id}/semantic-index")
async def semantic_build(ws_id: int, state: NexusState = Depends(get_state)) -> dict:
    """Build/refresh the embedding index with the local embedding model (explicit user action)."""
    with state.db.session() as s:
        w = get_workspace(s, ws_id)
        _root(w)
        model = model_for(load_preferences(s), "embedding")
        w.preferences = {**(w.preferences or {}), "semantic_search": True}
        audit(s, "user", "semantic_index.build", f"workspace:{ws_id}", model=model)
    models = {m.name for m in await _safe_models(state)}
    if models and model not in models and f"{model}:latest" not in models:
        raise HTTPException(409, f"Embedding model '{model}' is not installed. Run: ollama pull {model}")
    state.indexer.start(ws_id, model)
    return state.indexer.status(ws_id)


@router.post("/{ws_id}/semantic-index/cancel")
def semantic_cancel(ws_id: int, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    return {"cancelled": state.indexer.cancel(ws_id)}


@router.delete("/{ws_id}/semantic-index")
def semantic_clear(ws_id: int, state: NexusState = Depends(get_state), s: Session = Depends(get_session)) -> dict:
    w = get_workspace(s, ws_id)
    w.preferences = {**(w.preferences or {}), "semantic_search": False}
    state.indexer.clear(ws_id)
    return {"cleared": True}


async def _safe_models(state: NexusState) -> list:
    try:
        return await state.provider.list_models()
    except ProviderError as e:
        raise HTTPException(503, f"Ollama is not available: {e}") from e
