"""Filesystem boundary enforcement. Every workspace file access goes through `resolve_in_workspace`."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath


class AccessDenied(Exception):
    pass


# Files that are never read, listed, sent to the model or edited.
SENSITIVE_NAME_PATTERNS = [
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials", "credentials.*", "*credentials*.json", "secrets.*", "*secret*.json", "*secrets*.y*ml",
    "token.json", "*.token", ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials",
    "*.sqlite-wal", "service-account*.json", "*.tfstate", "*.tfvars",
]
# Explicitly safe templates that look like .env files.
SENSITIVE_ALLOW = [".env.example", ".env.sample", ".env.template", ".env.dist"]
SENSITIVE_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"}

# Not secret, but noise: excluded from tree, search and context.
IGNORED_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".nuxt", "out",
    "target", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode-test", "coverage",
    ".gradle", ".cache", "bin", "obj", ".terraform", ".parcel-cache", ".turbo", "site-packages",
}
IGNORED_FILE_PATTERNS = ["*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "*.pyc", "*.log"]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def is_sensitive(rel_path: str) -> bool:
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if any(part.lower() in SENSITIVE_DIRS for part in parts[:-1]):
        return True
    if parts and parts[-1].lower() in SENSITIVE_DIRS:
        return True
    name = parts[-1].lower() if parts else ""
    if name in SENSITIVE_ALLOW:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_NAME_PATTERNS)


def is_ignored(rel_path: str, extra_excludes: list[str] | None = None) -> bool:
    posix = rel_path.replace("\\", "/")
    parts = PurePosixPath(posix).parts
    if any(part in IGNORED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if any(fnmatch.fnmatch(name, p) for p in IGNORED_FILE_PATTERNS):
        return True
    return any(fnmatch.fnmatch(posix, g) or fnmatch.fnmatch(name, g) for g in (extra_excludes or []))


def validate_workspace_root(path: str) -> Path:
    """Validate a user-selected project folder. Refuse drive roots and the home directory itself."""
    if not path or "\x00" in path:
        raise AccessDenied("Invalid path")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise AccessDenied("Workspace path must be absolute")
    try:
        real = p.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise AccessDenied("Folder does not exist") from e
    if not real.is_dir():
        raise AccessDenied("Path is not a folder")
    if real.parent == real:
        raise AccessDenied("Refusing to use a drive/filesystem root as a workspace")
    if _norm(str(real)) == _norm(str(Path.home().resolve())):
        raise AccessDenied("Refusing to use your entire home folder as a workspace; choose a project folder")
    if is_sensitive(real.name):
        raise AccessDenied("Refusing to use a sensitive folder as a workspace")
    return real


def resolve_in_workspace(root: Path, rel_path: str, *, allow_sensitive: bool = False) -> Path:
    """Resolve rel_path inside root, rejecting traversal, absolute paths, symlink escapes and secrets."""
    if rel_path is None or "\x00" in rel_path:
        raise AccessDenied("Invalid path")
    rel = rel_path.replace("\\", "/").strip()
    if rel in ("", "."):
        rel = "."
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or rel.startswith("//"):
        raise AccessDenied("Absolute paths are not allowed")
    root_real = root.resolve(strict=True)
    candidate = (root_real / rel).resolve(strict=False)  # follows symlinks that exist
    root_n, cand_n = _norm(str(root_real)), _norm(str(candidate))
    try:
        inside = os.path.commonpath([root_n, cand_n]) == root_n
    except ValueError:  # different drives on Windows
        inside = False
    if not inside:
        raise AccessDenied("Path escapes the workspace")
    if not allow_sensitive and cand_n != root_n:
        rel_resolved = os.path.relpath(candidate, root_real)
        if is_sensitive(rel_resolved) or is_sensitive(rel):
            raise AccessDenied("This file is protected as potentially sensitive")
    return candidate


def to_rel(root: Path, p: Path) -> str:
    return os.path.relpath(p, root.resolve()).replace("\\", "/")
