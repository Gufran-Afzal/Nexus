"""Change proposals, unified diffs, and safe application with stale-content detection and backups."""

from __future__ import annotations

import datetime as dt
import difflib
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.database.models import ChangeSet, FileChange, Workspace, utcnow
from app.services.audit import audit
from app.workspaces.files import is_binary, sha256_text
from app.workspaces.security import AccessDenied, is_ignored, resolve_in_workspace, to_rel


class EditError(Exception):
    pass


def _current(path: Path) -> str | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    if is_binary(data):
        raise EditError("Refusing to edit a binary file")
    return data.decode("utf-8", errors="replace")


def match_line_endings(original: str, new: str) -> str:
    """Keep CRLF files CRLF (Windows) even when the new text arrived with LF only."""
    if "\r\n" in original and "\r\n" not in new:
        return new.replace("\n", "\r\n")
    if "\r\n" not in original and "\r\n" in new:
        return new.replace("\r\n", "\n")
    return new


def unified_diff(rel: str, before: str | None, after: str | None) -> str:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n\\ No newline at end of file\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n\\ No newline at end of file\n"
    fromfile = f"a/{rel}" if before is not None else "/dev/null"
    tofile = f"b/{rel}" if after is not None else "/dev/null"
    return "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile, n=3))


def diff_stats(diff: str) -> dict[str, int]:
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return {"added": added, "removed": removed}


def propose(
    s: Session, ws: Workspace, title: str, description: str, changes: list[dict], source: str
) -> ChangeSet:
    """changes: [{"path": rel, "new_content": str | None}] (None = delete the file)."""
    if not changes:
        raise EditError("A change set needs at least one file")
    root = Path(ws.path)
    cs = ChangeSet(workspace_id=ws.id, title=title[:300], description=description, source=source)
    seen: set[str] = set()
    for ch in changes:
        path = resolve_in_workspace(root, ch["path"])
        rel = to_rel(root, path)
        if rel in seen:
            raise EditError(f"Duplicate file in change set: {rel}")
        if is_ignored(rel):
            raise AccessDenied(f"{rel} is in an excluded location")
        seen.add(rel)
        if path.exists() and not path.is_file():
            raise EditError(f"{rel} is not a regular file")
        before = _current(path)
        new = ch.get("new_content")
        if new is not None and before is not None:
            new = match_line_endings(before, new)
        if before is None and new is None:
            raise EditError(f"Cannot delete {rel}: it does not exist")
        if before == new:
            continue
        cs.files.append(
            FileChange(
                rel_path=rel,
                base_hash=sha256_text(before) if before is not None else None,
                base_content=before,
                new_content=new,
            )
        )
    if not cs.files:
        raise EditError("The proposed content is identical to the current files")
    s.add(cs)
    s.flush()
    audit(s, source, "change.proposed", f"workspace:{ws.id}", changeset=cs.id, files=[f.rel_path for f in cs.files])
    return cs


def set_file_decision(s: Session, cs: ChangeSet, file_id: int, decision: str) -> FileChange:
    if decision not in ("accepted", "rejected", "proposed"):
        raise EditError("Invalid decision")
    fc = next((f for f in cs.files if f.id == file_id), None)
    if not fc:
        raise EditError("File not in change set")
    if fc.status not in ("proposed", "accepted", "rejected", "conflict"):
        raise EditError(f"File is already {fc.status}")
    fc.status = decision
    return fc


def apply_changeset(s: Session, ws: Workspace, cs: ChangeSet, backups_dir: Path, actor: str = "user") -> dict:
    """Apply accepted files. Called only from an explicit user approval endpoint.

    Each file is checked against the content it was proposed against; if the file changed since, it is
    marked `conflict` and left untouched. Originals are backed up before writing.
    """
    if not ws.allow_edits:
        raise AccessDenied("Editing is disabled for this workspace. Enable 'Allow edits' in workspace settings.")
    if cs.status not in ("proposed", "partially_applied"):
        raise EditError(f"Change set is {cs.status}")
    root = Path(ws.path)
    targets = [f for f in cs.files if f.status == "accepted"]
    if not targets:
        raise EditError("No accepted files to apply. Accept at least one file first.")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = backups_dir / f"changeset-{cs.id}-{stamp}"
    results = []
    for fc in targets:
        try:
            path = resolve_in_workspace(root, fc.rel_path)
            current = _current(path)
            cur_hash = sha256_text(current) if current is not None else None
            if cur_hash != fc.base_hash:
                fc.status = "conflict"
                fc.error = "The file changed on disk after this change was proposed. Re-propose against the latest version."
                results.append({"path": fc.rel_path, "status": "conflict"})
                continue
            if current is not None:
                bpath = bdir / fc.rel_path
                bpath.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, bpath)
                fc.backup_path = str(bpath)
            if fc.new_content is None:
                path.unlink()
            else:
                _atomic_write(path, fc.new_content)
            fc.status = "applied"
            fc.error = None
            results.append({"path": fc.rel_path, "status": "applied"})
        except Exception as e:  # report per-file failures; continue with the rest
            fc.status = "failed"
            fc.error = str(e)
            results.append({"path": fc.rel_path, "status": "failed", "error": str(e)})
    statuses = {f.status for f in cs.files}
    if statuses <= {"applied", "rejected"} and "applied" in statuses:
        cs.status = "applied"
    elif "applied" in statuses:
        cs.status = "partially_applied"
    elif statuses & {"failed", "conflict"}:
        cs.status = "failed" if "failed" in statuses else cs.status
    cs.applied_at = utcnow()
    audit(s, actor, "change.applied", f"workspace:{ws.id}", changeset=cs.id, results=results)
    return {"changeset": cs.id, "status": cs.status, "results": results}


def revert_changeset(s: Session, ws: Workspace, cs: ChangeSet, actor: str = "user") -> dict:
    """Restore originals for applied files, unless they were modified after NEXUS applied them."""
    if not ws.allow_edits:
        raise AccessDenied("Editing is disabled for this workspace.")
    root = Path(ws.path)
    results = []
    for fc in cs.files:
        if fc.status != "applied":
            continue
        path = resolve_in_workspace(root, fc.rel_path)
        current = _current(path)
        if current != fc.new_content:
            results.append({"path": fc.rel_path, "status": "skipped", "error": "modified since applied"})
            continue
        if fc.base_content is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, fc.base_content)
        fc.status = "reverted"
        results.append({"path": fc.rel_path, "status": "reverted"})
    if any(r["status"] == "reverted" for r in results):
        cs.status = "reverted"
    audit(s, actor, "change.reverted", f"workspace:{ws.id}", changeset=cs.id, results=results)
    return {"changeset": cs.id, "status": cs.status, "results": results}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".nexus-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
