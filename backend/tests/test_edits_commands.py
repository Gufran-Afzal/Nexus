import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.tools.commands import classify, parse_command, run_command


def add_ws(client: TestClient, project: Path, **perms) -> int:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()
    if perms:
        client.patch(f"/api/workspaces/{ws['id']}", json=perms)
    return ws["id"]


FIXED = "def add(a, b):\n    return a + b\n"


def propose(client: TestClient, ws_id: int, files=None) -> dict:
    r = client.post("/api/changesets", json={
        "workspace_id": ws_id, "title": "Fix add",
        "files": files or [{"path": "src/calc.py", "new_content": FIXED}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_proposal_is_not_applied_and_has_diff(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project)
    cs = propose(client, ws)
    f = cs["files"][0]
    assert cs["status"] == "proposed" and f["status"] == "proposed"
    assert "-    return a - b" in f["diff"] and "+    return a + b" in f["diff"]
    assert f["added"] == 1 and f["removed"] == 1
    assert "a - b" in (project / "src/calc.py").read_text()


def test_proposals_cannot_target_secrets_or_escape(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project)
    for path in (".env", "../escape.py", "node_modules/pkg/index.js"):
        r = client.post("/api/changesets", json={"workspace_id": ws, "title": "x",
                                                  "files": [{"path": path, "new_content": "x"}]})
        assert r.status_code in (400, 403), path


def test_apply_requires_permission_and_acceptance(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project)
    cs = propose(client, ws)
    fid = cs["files"][0]["id"]
    assert client.post(f"/api/changesets/{cs['id']}/apply").status_code == 403  # read-only by default
    client.patch(f"/api/workspaces/{ws}", json={"allow_edits": True})
    assert client.post(f"/api/changesets/{cs['id']}/apply").status_code == 400  # nothing accepted
    client.post(f"/api/changesets/{cs['id']}/files/{fid}/decision", json={"decision": "accepted"})
    r = client.post(f"/api/changesets/{cs['id']}/apply").json()
    assert r["status"] == "applied"
    assert (project / "src/calc.py").read_text() == FIXED
    audit = client.get("/api/audit").json()
    assert any(a["action"] == "change.applied" for a in audit)
    # revert restores the original
    rv = client.post(f"/api/changesets/{cs['id']}/revert").json()
    assert rv["status"] == "reverted"
    assert "a - b" in (project / "src/calc.py").read_text()


def test_stale_edit_detected_and_partial_failure_reported(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project, allow_edits=True)
    cs = propose(client, ws, [
        {"path": "src/calc.py", "new_content": FIXED},
        {"path": "src/new_module.py", "new_content": "X = 1\n"},
    ])
    client.post(f"/api/changesets/{cs['id']}/decide-all", json={"decision": "accepted"})
    # The user edits the file after the proposal was made.
    (project / "src/calc.py").write_text("def add(a, b):\n    return b - a  # user edit\n")
    r = client.post(f"/api/changesets/{cs['id']}/apply").json()
    statuses = {x["path"]: x["status"] for x in r["results"]}
    assert statuses == {"src/calc.py": "conflict", "src/new_module.py": "applied"}
    assert r["status"] == "partially_applied"
    assert "user edit" in (project / "src/calc.py").read_text()  # user's work untouched
    assert (project / "src/new_module.py").read_text() == "X = 1\n"


def test_backup_written_before_overwrite(client: TestClient, project: Path, settings) -> None:
    ws = add_ws(client, project, allow_edits=True)
    cs = propose(client, ws)
    client.post(f"/api/changesets/{cs['id']}/decide-all", json={"decision": "accepted"})
    client.post(f"/api/changesets/{cs['id']}/apply")
    backups = list(settings.backups_dir.rglob("calc.py"))
    assert backups and "a - b" in backups[0].read_text()


# ---------------------------------------------------------------- command policy
@pytest.mark.parametrize("cmd,risk", [
    ("pytest -q", "verification"), ("python -m pytest", "verification"), ("npm run lint", "verification"),
    ("ruff check .", "verification"), ("git status", "verification"),
    ("rm -rf build", "destructive"), ("del /s /q *", "destructive"), ("git reset --hard", "destructive"),
    ("pip install requests", "package_install"), ("npm install", "package_install"),
    ("python -m pip install x", "package_install"), ("npx create-react-app x", "package_install"),
    ("curl https://example.com", "network"), ("git push", "network"),
    ("sudo apt install x", "blocked"), ("powershell -Command Get-Process", "blocked"),
    ("cmd /c dir", "blocked"), ("python -c print(1)", "blocked"), ("node -e 1", "blocked"),
    ("make build", "unfamiliar"),
])
def test_command_classification(cmd: str, risk: str) -> None:
    assert classify(parse_command(cmd)).risk == risk


def py(code: str) -> list[str]:
    """Run a helper script file (inline -c is blocked by policy, so tests use files)."""
    return [sys.executable, code]


async def test_runner_captures_output_and_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text("import sys\nprint('hello')\nprint('API_KEY=abc123456789', file=sys.stderr)\nsys.exit(3)\n")
    res = await run_command(py(str(script)), tmp_path, 20, 10_000)
    assert res.status == "failed" and res.exit_code == 3
    assert "hello" in res.stdout
    assert "abc123456789" not in res.stderr  # redacted


async def test_runner_timeout_and_output_limit(tmp_path: Path) -> None:
    slow = tmp_path / "slow.py"
    slow.write_text("import time\nprint('x'*5000, flush=True)\ntime.sleep(30)\n")
    t0 = time.time()
    res = await run_command(py(str(slow)), tmp_path, 1, 1000)
    assert res.status == "timed_out" and time.time() - t0 < 15
    assert res.truncated and len(res.stdout) <= 1000


async def test_runner_cancellation(tmp_path: Path) -> None:
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    ev = asyncio.Event()
    task = asyncio.create_task(run_command(py(str(slow)), tmp_path, 60, 1000, ev))
    await asyncio.sleep(0.5)
    ev.set()
    res = await asyncio.wait_for(task, 15)
    assert res.status == "cancelled"


async def test_runner_missing_program(tmp_path: Path) -> None:
    res = await run_command(["definitely-not-a-program-xyz"], tmp_path, 5, 1000)
    assert res.status == "error" and "not found" in res.stderr


def wait_cmd(client: TestClient, run_id: int) -> dict:
    for _ in range(200):
        r = client.get(f"/api/commands/{run_id}").json()
        if r["status"] not in ("pending_approval", "running"):
            return r
        time.sleep(0.05)
    raise AssertionError("command did not finish")


def test_command_approval_flow(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project)
    run = client.post("/api/commands", json={"workspace_id": ws, "command": [sys.executable, "-m", "pytest", "-q"]}).json()
    assert run["status"] == "pending_approval" and run["risk"] == "verification"
    # Nothing runs until approved, and approval needs workspace permission.
    assert client.post(f"/api/commands/{run['id']}/approve", json={}).status_code == 403
    client.patch(f"/api/workspaces/{ws}", json={"allow_commands": True})
    assert client.post(f"/api/commands/{run['id']}/approve", json={}).status_code == 200
    done = wait_cmd(client, run["id"])
    # The buggy add() makes the real test fail, and NEXUS reports that honestly.
    assert done["status"] == "failed" and done["exit_code"] == 1
    assert "assert" in done["stdout"] or "failed" in done["stdout"].lower()


def test_blocked_and_risky_commands(client: TestClient, project: Path) -> None:
    ws = add_ws(client, project, allow_commands=True)
    blocked = client.post("/api/commands", json={"workspace_id": ws, "command": "powershell -Command whoami"}).json()
    assert blocked["status"] == "rejected"
    assert client.post(f"/api/commands/{blocked['id']}/approve", json={}).status_code == 400
    risky = client.post("/api/commands", json={"workspace_id": ws, "command": "rm -rf src"}).json()
    assert risky["risk"] == "destructive"
    assert client.post(f"/api/commands/{risky['id']}/approve", json={}).status_code == 400  # needs confirm_risk
    assert client.post(f"/api/commands/{risky['id']}/reject").json()["rejected"]
    assert (project / "src" / "calc.py").exists()
    bad_cwd = client.post("/api/commands", json={"workspace_id": ws, "command": "pytest", "cwd": "../.."})
    assert bad_cwd.status_code == 400


def test_crlf_files_keep_crlf(client: TestClient, project: Path) -> None:
    (project / "src" / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    ws = add_ws(client, project, allow_edits=True)
    cs = propose(client, ws, [{"path": "src/win.py", "new_content": "a = 1\nb = 3\n"}])
    f = cs["files"][0]
    assert f["added"] == 1 and f["removed"] == 1  # not a whole-file rewrite
    client.post(f"/api/changesets/{cs['id']}/decide-all", json={"decision": "accepted"})
    client.post(f"/api/changesets/{cs['id']}/apply")
    assert (project / "src" / "win.py").read_bytes() == b"a = 1\r\nb = 3\r\n"


async def test_project_venv_tools_are_found(tmp_path: Path) -> None:
    from app.tools.commands import project_bin_dirs

    bindir = tmp_path / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bindir.mkdir(parents=True)
    tool = bindir / "projtool"
    tool.write_text("#!/bin/sh\necho from-project-venv\n")
    tool.chmod(0o755)
    res = await run_command(["projtool"], tmp_path, 10, 1000, extra_path=project_bin_dirs(tmp_path))
    if sys.platform != "win32":
        assert res.status == "succeeded" and "from-project-venv" in res.stdout


async def test_runner_works_on_selector_loop(tmp_path: Path) -> None:
    """uvicorn --reload on Windows uses a Selector loop, where asyncio subprocesses are unsupported."""
    import asyncio as aio

    script = tmp_path / "ok.py"
    script.write_text("print('fine')\n")
    loop = aio.SelectorEventLoop()
    try:
        res = await aio.wrap_future(aio.run_coroutine_threadsafe(
            run_command(py(str(script)), tmp_path, 10, 1000), _start(loop)))
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert res.status == "succeeded" and "fine" in res.stdout


def _start(loop):
    import threading

    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop
