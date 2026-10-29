import os
import sys
from pathlib import Path

import pytest

from app.core.redact import redact, scrub_env
from app.retrieval.context import build_context
from app.workspaces.files import read_text_file, search_files, walk_files
from app.workspaces.security import AccessDenied, is_sensitive, resolve_in_workspace, validate_workspace_root


@pytest.mark.parametrize("rel", ["../outside.txt", "src/../../outside.txt", "..\\outside.txt", "/etc/passwd",
                                 "C:/Windows/win.ini", "//server/share/x", "src/\x00.py"])
def test_traversal_and_absolute_paths_rejected(project: Path, rel: str) -> None:
    with pytest.raises(AccessDenied):
        resolve_in_workspace(project, rel)


def test_normal_paths_allowed(project: Path) -> None:
    assert resolve_in_workspace(project, "src/calc.py") == (project / "src" / "calc.py").resolve()
    assert resolve_in_workspace(project, "src\\calc.py") == (project / "src" / "calc.py").resolve()
    assert resolve_in_workspace(project, "new_dir/new.py").name == "new.py"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin/dev mode on Windows")
def test_symlink_escape_rejected(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret_outside.txt"
    outside.write_text("top secret")
    os.symlink(outside, project / "link.txt")
    os.symlink(tmp_path, project / "linkdir")
    with pytest.raises(AccessDenied):
        resolve_in_workspace(project, "link.txt")
    with pytest.raises(AccessDenied):
        resolve_in_workspace(project, "linkdir/secret_outside.txt")
    assert "link.txt" not in [e.rel for e in walk_files(project)]


@pytest.mark.parametrize("rel,expected", [
    (".env", True), ("config/.env.production", True), ("prod.env", True), (".env.example", False),
    ("id_rsa", True), ("keys/server.pem", True), ("cert.pfx", True), (".git/config", True),
    (".ssh/known_hosts", True), ("credentials.json", True), (".npmrc", True), ("secrets.yaml", True),
    ("src/tokenizer.py", False), ("src/keyboard.py", False), ("README.md", False),
])
def test_sensitive_detection(rel: str, expected: bool) -> None:
    assert is_sensitive(rel) is expected


def test_sensitive_files_cannot_be_read(project: Path) -> None:
    with pytest.raises(AccessDenied):
        read_text_file(project, ".env", 1_000_000)
    assert read_text_file(project, ".env.example", 1_000_000).text.startswith("API_KEY")


def test_walk_excludes_sensitive_and_ignored(project: Path) -> None:
    rels = [e.rel for e in walk_files(project)]
    assert "src/calc.py" in rels
    assert ".env" not in rels
    assert not any(r.startswith("node_modules") for r in rels)
    assert ".env.example" in rels


def test_search_never_returns_secrets(project: Path) -> None:
    res = search_files(project, "supersecretvalue", [], content=True)
    assert res["results"] == []


def test_context_never_includes_secrets(project: Path) -> None:
    ctx = build_context(project, "API_KEY secret", [".env"], 4000, [], 1_000_000)
    assert "supersecretvalue" not in ctx.text
    assert ctx.omitted and ctx.omitted[0]["path"] == ".env"


def test_workspace_root_validation(project: Path, tmp_path: Path) -> None:
    assert validate_workspace_root(str(project)) == project.resolve()
    with pytest.raises(AccessDenied):
        validate_workspace_root(str(Path(project.anchor)))
    with pytest.raises(AccessDenied):
        validate_workspace_root(str(Path.home()))
    with pytest.raises(AccessDenied):
        validate_workspace_root(str(tmp_path / "missing"))
    with pytest.raises(AccessDenied):
        validate_workspace_root("relative/path")


def test_redaction() -> None:
    text = ("API_KEY=abcd1234efgh token: ghp_abcdefghijklmnopqrstuvwxyz0123 AKIAABCDEFGHIJKLMNOP "
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY----- normal text")
    out = redact(text)
    for secret in ("abcd1234efgh", "ghp_abcdefghijkl", "AKIAABCDEFGHIJKLMNOP", "MIIE"):
        assert secret not in out
    assert "normal text" in out
    assert scrub_env({"PATH": "x", "GITHUB_TOKEN": "y", "MY_API_KEY": "z"}) == {"PATH": "x"}


@pytest.mark.skipif(sys.platform == "win32", reason="covered by junction check on Windows")
def test_symlinked_directory_not_walked(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "leak.py").write_text("x")
    os.symlink(outside, project / "src" / "linked", target_is_directory=True)
    assert not any("leak.py" in e.rel for e in walk_files(project))


def test_batch_file_argument_injection_refused(tmp_path: Path) -> None:
    import asyncio

    from app.tools.commands import run_command

    bat = tmp_path / "tool.cmd"
    bat.write_text("@echo off\n")
    bat.chmod(0o755)
    res = asyncio.run(run_command([str(bat), "x & calc.exe"], tmp_path, 5, 1000))
    assert res.status == "error" and "Refused" in res.stderr
