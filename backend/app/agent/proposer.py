"""Ask the model for whole-file rewrites and turn them into change proposals.

Small local models are far more reliable at "return the full updated file" than at producing valid
unified diffs, so NEXUS asks for complete file content and computes the diff itself.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.workspaces.files import read_text_file
from app.workspaces.security import resolve_in_workspace

MAX_EDIT_FILES = 3
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def extract_code_block(text: str) -> str | None:
    blocks = FENCE.findall(text)
    if not blocks:
        return None
    return max(blocks, key=len)


async def generate_file_edits(
    ask: Callable[[str], Awaitable[str]],
    root: Path,
    goal: str,
    plan: str,
    targets: list[str],
    max_file_bytes: int,
) -> tuple[list[dict], list[str]]:
    changes: list[dict] = []
    notes: list[str] = []
    for rel in targets[:MAX_EDIT_FILES]:
        path = resolve_in_workspace(root, rel)  # validates before anything is sent to the model
        current = read_text_file(root, rel, max_file_bytes).text if path.exists() else ""
        prompt = (
            f"Goal: {goal}\n\n" + (f"Plan / context:\n{plan[-6000:]}\n\n" if plan else "")
            + f"Current content of `{rel}`{' (file does not exist yet)' if not path.exists() else ''}:\n"
            f"```\n{current}\n```\n\n"
            "Return the COMPLETE new content of this file in ONE fenced code block, preserving every line that "
            "does not need to change. If this file needs no change, reply exactly NO_CHANGE."
        )
        reply = await ask(prompt)
        if reply.strip() == "NO_CHANGE":
            notes.append(f"{rel}: no change needed")
            continue
        code = extract_code_block(reply)
        if code is None:
            notes.append(f"{rel}: the model did not return a code block; skipped")
            continue
        if current.endswith("\n") and not code.endswith("\n"):
            code += "\n"
        if code != current:
            changes.append({"path": rel, "new_content": code})
        else:
            notes.append(f"{rel}: unchanged")
    if len(targets) > MAX_EDIT_FILES:
        notes.append(f"Only the first {MAX_EDIT_FILES} files were processed")
    return changes, notes
