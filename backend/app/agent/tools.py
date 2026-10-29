"""Tools the coding agent may call.

Read-only tools run immediately but only inside the workspace and never on sensitive files.
Write-like tools never act: `propose_edit` creates a change set for the user to review, and
`propose_command` creates a command that waits for the user's approval. Every argument is validated
here, independently of the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.agent import edits
from app.database.db import Database
from app.database.models import Workspace
from app.tools.command_service import CommandManager
from app.workspaces import files as wf
from app.workspaces.security import AccessDenied, resolve_in_workspace

MAX_READ_LINES = 400
MAX_RESULT_CHARS = 12_000


class ListFilesArgs(BaseModel):
    path: str = Field(".", description="Folder inside the project, e.g. 'src'")
    limit: int = Field(200, ge=1, le=500)


class SearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=200, description="Text to find (case-insensitive)")
    in_content: bool = Field(True, description="Search file contents (true) or only file paths (false)")


class ReadFileArgs(BaseModel):
    path: str = Field(..., min_length=1, description="File path relative to the project root")
    start_line: int = Field(1, ge=1)
    end_line: int | None = Field(None, ge=1)


class NoArgs(BaseModel):
    pass


class ProposeEditArgs(BaseModel):
    path: str = Field(..., min_length=1, description="File to create or replace, relative to the project root")
    new_content: str = Field(..., max_length=400_000, description="The COMPLETE new content of the file")
    summary: str = Field("", max_length=500, description="One sentence: what this change does")


class ProposeCommandArgs(BaseModel):
    command: str = Field(..., min_length=1, max_length=1000, description="e.g. 'pytest -q' or 'npm test'")
    reason: str = Field("", max_length=300)


@dataclass
class ToolOutcome:
    """status: succeeded | failed | awaiting_permission"""

    status: str
    content: str  # what the model sees
    summary: str  # what the user sees
    ref: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any], ToolOutcome]
    gated: bool = False

    def schema(self) -> dict[str, Any]:
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


class Toolbox:
    def __init__(self, db: Database, commands: CommandManager, workspace_id: int, max_file_bytes: int) -> None:
        self.db, self.commands, self.workspace_id, self.max_file_bytes = db, commands, workspace_id, max_file_bytes
        with db.session() as s:
            ws = s.get(Workspace, workspace_id)
            if not ws:
                raise ValueError("workspace not found")
            self.root = Path(ws.path)
            self.excludes = list(ws.exclude_globs or [])
        self.specs: dict[str, ToolSpec] = {t.name: t for t in [
            ToolSpec("list_files", "List project files under a folder (secrets and build output are hidden).",
                     ListFilesArgs, self._list_files),
            ToolSpec("search_code", "Search the project for text. Returns matching paths and lines.",
                     SearchArgs, self._search),
            ToolSpec("read_file", f"Read a project file (up to {MAX_READ_LINES} lines per call, with line numbers).",
                     ReadFileArgs, self._read_file),
            ToolSpec("git_status", "Show the git branch and changed files.", NoArgs, self._git_status),
            ToolSpec("git_diff", "Show uncommitted changes (git diff).", NoArgs, self._git_diff),
            ToolSpec("propose_edit", "Propose replacing a file with new COMPLETE content. Nothing is written: "
                     "the user reviews the diff and decides.", ProposeEditArgs, self._propose_edit, gated=True),
            ToolSpec("propose_command", "Ask the user to approve running a command (tests, linters, type checks) "
                     "in the project. It does not run until the user approves.", ProposeCommandArgs,
                     self._propose_command, gated=True),
        ]}

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.specs.values()]

    def text_protocol(self) -> str:
        lines = ["To use a tool, reply with ONLY a fenced block like:",
                 '```tool\n{"name": "read_file", "arguments": {"path": "src/app.py"}}\n```',
                 "Then wait for the result. Available tools:"]
        for t in self.specs.values():
            props = t.args.model_json_schema().get("properties", {})
            lines.append(f"- {t.name}({', '.join(props)}): {t.description}")
        return "\n".join(lines)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        spec = self.specs.get(name)
        if not spec:
            return ToolOutcome("failed", f"Unknown tool '{name}'. Available: {', '.join(self.specs)}",
                               f"Unknown tool {name}")
        try:
            args = spec.args.model_validate(arguments or {})
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            return ToolOutcome("failed", f"Invalid arguments for {name}: {msg}", f"{name}: invalid arguments")
        try:
            return spec.run(args)
        except AccessDenied as e:
            return ToolOutcome("failed", f"Access denied: {e}", f"{name}: access denied ({e})")
        except FileNotFoundError:
            return ToolOutcome("failed", "File not found", f"{name}: file not found")
        except (edits.EditError, ValueError) as e:
            return ToolOutcome("failed", f"Error: {e}", f"{name}: {e}")

    # ---------------------------------------------------------------- read-only
    def _list_files(self, a: ListFilesArgs) -> ToolOutcome:
        base = resolve_in_workspace(self.root, a.path)
        prefix = "" if base == self.root.resolve() else wf.to_rel(self.root, base).rstrip("/") + "/"
        out = [e.rel for e in wf.walk_files(self.root, self.excludes, limit=20_000) if e.rel.startswith(prefix)]
        shown = out[: a.limit]
        more = f"\n... and {len(out) - len(shown)} more" if len(out) > len(shown) else ""
        return ToolOutcome("succeeded", "\n".join(shown) + more or "(no files)",
                           f"Listed {len(out)} files in {a.path}")

    def _search(self, a: SearchArgs) -> ToolOutcome:
        res = wf.search_files(self.root, a.query, self.excludes, content=a.in_content, max_results=60,
                              max_bytes=self.max_file_bytes)
        rows = [f"{r['path']}:{r['line']}: {r['text']}" if "line" in r else r["path"] for r in res["results"]]
        body = "\n".join(rows) or "No matches."
        return ToolOutcome("succeeded", _clip(body), f"Searched “{a.query}” → {len(rows)} match(es)")

    def _read_file(self, a: ReadFileArgs) -> ToolOutcome:
        fc = wf.read_text_file(self.root, a.path, self.max_file_bytes)
        lines = fc.text.splitlines()
        end = min(a.end_line or len(lines), len(lines), a.start_line + MAX_READ_LINES - 1)
        chunk = lines[a.start_line - 1 : end]
        numbered = "\n".join(f"{i:>5}| {ln}" for i, ln in enumerate(chunk, a.start_line))
        note = f"\n[lines {a.start_line}-{end} of {len(lines)}]" if end < len(lines) or a.start_line > 1 else ""
        return ToolOutcome("succeeded", _clip(f"{fc.rel}:\n{numbered}{note}"),
                           f"Read {fc.rel} (lines {a.start_line}-{end})", {"path": fc.rel})

    def _git_status(self, _a: NoArgs) -> ToolOutcome:
        st = wf.git_status(self.root)
        if not st.get("available"):
            return ToolOutcome("failed", f"Git unavailable: {st.get('reason')}", "Git status unavailable")
        return ToolOutcome("succeeded", json.dumps(st), f"Git status ({len(st['changes'])} changed)")

    def _git_diff(self, _a: NoArgs) -> ToolOutcome:
        d = wf.git_diff(self.root)
        if d is None:
            return ToolOutcome("failed", "Git diff unavailable", "Git diff unavailable")
        return ToolOutcome("succeeded", _clip(d or "(no uncommitted changes)"), "Read git diff")

    # ---------------------------------------------------------------- approval-gated
    def _propose_edit(self, a: ProposeEditArgs) -> ToolOutcome:
        with self.db.session() as s:
            ws = s.get(Workspace, self.workspace_id)
            assert ws
            cs = edits.propose(s, ws, a.summary or f"Edit {a.path}", a.summary,
                               [{"path": a.path, "new_content": a.new_content}], "model")
            f = cs.files[0]
            stats = edits.diff_stats(edits.unified_diff(f.rel_path, f.base_content, f.new_content))
            cs_id = cs.id
        return ToolOutcome(
            "awaiting_permission",
            f"Change set #{cs_id} created for {a.path} (+{stats['added']} -{stats['removed']}). It is NOT applied; "
            "the user must review and apply it. Do not claim the change is made.",
            f"Proposed change to {a.path} (+{stats['added']} −{stats['removed']}) — awaiting your review",
            {"changeset_id": cs_id},
        )

    def _propose_command(self, a: ProposeCommandArgs) -> ToolOutcome:
        with self.db.session() as s:
            ws = s.get(Workspace, self.workspace_id)
            assert ws
            s.expunge(ws)
        run = self.commands.request(ws, a.command, requested_by="model")
        if run.status == "rejected":
            return ToolOutcome("failed", f"Command refused by policy: {run.stderr}", f"Command refused: {a.command}",
                               {"command_id": run.id})
        return ToolOutcome(
            "awaiting_permission",
            f"Command #{run.id} ({run.risk}) is waiting for the user's approval. It has NOT run; do not invent its output.",
            f"Requested `{a.command}` — awaiting your approval", {"command_id": run.id},
        )
