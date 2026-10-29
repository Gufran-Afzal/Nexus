"""Read-only workspace operations: walking, tree, previews, search, git status, project instructions."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.workspaces.security import AccessDenied, is_ignored, is_sensitive, resolve_in_workspace, to_rel

PROJECT_INSTRUCTIONS_FILE = "NEXUS.md"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _is_link_or_junction(path: str) -> bool:
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    if isjunction is not None and isjunction(path):
        return True
    if sys.platform == "win32":  # older Pythons: a reparse point that resolves elsewhere
        try:
            return os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.abspath(path))
        except OSError:
            return True
    return False


@dataclass
class WalkEntry:
    rel: str
    size: int


def walk_files(root: Path, excludes: list[str] | None = None, limit: int = 20_000) -> Iterator[WalkEntry]:
    """Yield non-sensitive, non-ignored regular files. Never follows symlinked directories."""
    root = root.resolve()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = to_rel(root, Path(dirpath))
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not is_sensitive(rel_dir + d + "/x")
            and not is_ignored(rel_dir + d + "/x", excludes)
            and not _is_link_or_junction(os.path.join(dirpath, d))
        )
        for f in sorted(filenames):
            rel = rel_dir + f
            if is_sensitive(rel) or is_ignored(rel, excludes):
                continue
            full = os.path.join(dirpath, f)
            if os.path.islink(full):
                try:
                    resolve_in_workspace(root, rel)
                except AccessDenied:
                    continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            yield WalkEntry(rel=rel, size=size)
            count += 1
            if count >= limit:
                return


def build_tree(root: Path, excludes: list[str], limit: int) -> dict:
    tree: dict = {"name": root.name, "path": "", "type": "dir", "children": []}
    index: dict[str, dict] = {"": tree}
    truncated = False
    n = 0
    for entry in walk_files(root, excludes, limit=limit + 1):
        n += 1
        if n > limit:
            truncated = True
            break
        parts = entry.rel.split("/")
        cur = ""
        for part in parts[:-1]:
            nxt = f"{cur}/{part}" if cur else part
            if nxt not in index:
                node = {"name": part, "path": nxt, "type": "dir", "children": []}
                index[cur]["children"].append(node)
                index[nxt] = node
            cur = nxt
        index[cur]["children"].append({"name": parts[-1], "path": entry.rel, "type": "file", "size": entry.size})
    return {"tree": tree, "truncated": truncated, "file_count": min(n, limit)}


@dataclass
class FileContent:
    rel: str
    text: str
    sha256: str
    size: int


def read_text_file(root: Path, rel: str, max_bytes: int) -> FileContent:
    path = resolve_in_workspace(root, rel)
    if is_ignored(to_rel(root, path)):
        raise AccessDenied("File is excluded from the workspace")
    if not path.is_file():
        raise FileNotFoundError(rel)
    size = path.stat().st_size
    if size > max_bytes:
        raise AccessDenied(f"File is too large to read ({size} bytes > {max_bytes})")
    data = path.read_bytes()
    if is_binary(data):
        raise AccessDenied("Binary file")
    text = data.decode("utf-8", errors="replace")
    return FileContent(rel=to_rel(root, path), text=text, sha256=sha256_text(text), size=size)


def search_files(root: Path, query: str, excludes: list[str], *, content: bool, max_results: int = 200,
                 max_files: int = 20_000, max_bytes: int = 1_000_000) -> dict:
    q = query.lower().strip()
    results: list[dict] = []
    scanned = 0
    if not q:
        return {"results": [], "scanned": 0, "truncated": False}
    for entry in walk_files(root, excludes, limit=max_files):
        scanned += 1
        if not content:
            if q in entry.rel.lower():
                results.append({"path": entry.rel})
        elif entry.size <= max_bytes:
            try:
                data = (root / entry.rel).read_bytes()
            except OSError:
                continue
            if is_binary(data):
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if q in line.lower():
                    results.append({"path": entry.rel, "line": i, "text": line.strip()[:240]})
                    if len(results) >= max_results:
                        break
        if len(results) >= max_results:
            return {"results": results, "scanned": scanned, "truncated": True}
    return {"results": results, "scanned": scanned, "truncated": False}


def git_status(root: Path) -> dict:
    git = shutil.which("git")
    if not git:
        return {"available": False, "reason": "git not found on PATH"}
    if not (root / ".git").exists():
        return {"available": False, "reason": "not a git repository"}
    try:
        r = subprocess.run(
            [git, "-C", str(root), "status", "--porcelain=v1", "-b"],
            capture_output=True, text=True, timeout=15, check=False, creationflags=NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "reason": str(e)}
    if r.returncode != 0:
        return {"available": False, "reason": r.stderr.strip()[:300]}
    lines = r.stdout.splitlines()
    branch = lines[0][3:] if lines and lines[0].startswith("## ") else None
    changes = [{"status": ln[:2], "path": ln[3:]} for ln in lines[1:] if ln]
    return {"available": True, "branch": branch, "changes": changes}


def git_diff(root: Path, max_chars: int = 100_000) -> str | None:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return None
    r = subprocess.run([git, "-C", str(root), "diff"], capture_output=True, text=True, timeout=30, check=False,
                       creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
    return r.stdout[:max_chars] if r.returncode == 0 else None


def project_instructions(root: Path, max_bytes: int = 50_000) -> str | None:
    p = root / PROJECT_INSTRUCTIONS_FILE
    if not p.is_file():
        return None
    try:
        resolve_in_workspace(root, PROJECT_INSTRUCTIONS_FILE)
    except AccessDenied:
        return None
    return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
