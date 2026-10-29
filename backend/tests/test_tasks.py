import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import FakeProvider


def wait_for(client: TestClient, task_id: int, pred, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if pred(t):
            return t
        time.sleep(0.05)
    raise AssertionError(f"timeout; last state: {t}")


def smart_reply(messages) -> str:
    prompt = messages[-1].content
    if "COMPLETE new content" in prompt:
        return "Here is the fix:\n```python\ndef add(a, b):\n    return a + b\n```"
    if "Review the applied change" in prompt:
        return "The change replaces subtraction with addition. Tests passed according to the command result."
    return "Plan: change `-` to `+` in src/calc.py and run the tests."


def test_implement_change_workflow_end_to_end(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.reply = smart_reply
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    client.patch(f"/api/workspaces/{ws}", json={"allow_edits": True, "allow_commands": True})
    test_cmd = f'"{sys.executable}" -m pytest -q' if sys.platform == "win32" else f"{sys.executable} -m pytest -q"
    t = client.post("/api/tasks", json={"template": "implement_change", "goal": "Fix the add function",
                                        "workspace_id": ws, "files": ["src/calc.py"],
                                        "test_command": test_cmd}).json()
    tid = t["id"]
    # 1) Engine stops at the edit checkpoint; nothing was written.
    t = wait_for(client, tid, lambda t: t["status"] == "waiting_approval")
    assert "a - b" in (project / "src/calc.py").read_text()
    propose_step = next(s for s in t["steps"] if s["kind"] == "propose_edit")
    cs_id = propose_step["ref"]["changeset_id"]
    # 2) User reviews and applies.
    client.post(f"/api/changesets/{cs_id}/decide-all", json={"decision": "accepted"})
    assert client.post(f"/api/changesets/{cs_id}/apply").json()["status"] == "applied"
    # 3) Engine requests the verification command and waits for approval.
    t = wait_for(client, tid, lambda t: any(s["kind"] == "run_command" and s["ref"].get("command_id")
                                            and s["status"] == "waiting_approval" for s in t["steps"]))
    cmd_id = next(s for s in t["steps"] if s["kind"] == "run_command")["ref"]["command_id"]
    assert client.post(f"/api/commands/{cmd_id}/approve", json={}).status_code == 200
    # 4) Real tests run (and now pass), then review + report.
    t = wait_for(client, tid, lambda t: t["status"] in ("succeeded", "failed", "cancelled"), timeout=60)
    assert t["status"] == "succeeded", t
    statuses = {s["kind"]: s["status"] for s in t["steps"]}
    assert statuses["run_command"] == "succeeded"
    assert "succeeded" in t["summary"] and "not independently verified" in t["summary"]


def test_task_reports_rejected_edits_honestly(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.reply = smart_reply
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    t = client.post("/api/tasks", json={"template": "implement_change", "goal": "Fix add", "workspace_id": ws,
                                        "files": ["src/calc.py"]}).json()
    t = wait_for(client, t["id"], lambda t: t["status"] == "waiting_approval")
    cs_id = next(s for s in t["steps"] if s["kind"] == "propose_edit")["ref"]["changeset_id"]
    client.post(f"/api/changesets/{cs_id}/decide-all", json={"decision": "rejected"})
    t = wait_for(client, t["id"], lambda t: t["status"] in ("succeeded", "failed"))
    assert t["status"] == "failed"
    steps = {s["kind"]: s["status"] for s in t["steps"]}
    assert steps["await_changes"] == "failed" and steps["review"] == "skipped"
    assert "a - b" in (project / "src/calc.py").read_text()


def test_task_cancellation(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.reply = smart_reply
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    t = client.post("/api/tasks", json={"template": "implement_change", "goal": "Fix add", "workspace_id": ws,
                                        "files": ["src/calc.py"]}).json()
    wait_for(client, t["id"], lambda t: t["status"] == "waiting_approval")
    assert client.post(f"/api/tasks/{t['id']}/cancel").json()["cancelled"]
    t = wait_for(client, t["id"], lambda t: t["status"] == "cancelled")
    assert all(s["status"] in ("succeeded", "cancelled") for s in t["steps"])


def test_retry_then_fail_when_model_unavailable(client: TestClient, provider: FakeProvider, project: Path) -> None:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    provider.fail_times = 1  # first attempt fails, retry succeeds
    t = client.post("/api/tasks", json={"template": "plan_feature", "goal": "Add a multiply function",
                                        "workspace_id": ws}).json()
    t = wait_for(client, t["id"], lambda t: t["status"] in ("succeeded", "failed"))
    plan = next(s for s in t["steps"] if s["kind"] == "plan")
    assert t["status"] == "succeeded" and plan["attempts"] == 2
    provider.available = False
    t = client.post("/api/tasks", json={"template": "plan_feature", "goal": "Add division", "workspace_id": ws}).json()
    t = wait_for(client, t["id"], lambda t: t["status"] in ("succeeded", "failed"))
    assert t["status"] == "failed"
    assert next(s for s in t["steps"] if s["kind"] == "plan")["status"] == "failed"


def test_task_validation(client: TestClient) -> None:
    assert client.post("/api/tasks", json={"template": "nope", "goal": "abc"}).status_code == 400
    assert client.post("/api/tasks", json={"template": "plan_feature", "goal": "abc"}).status_code == 400
    assert len(client.get("/api/tasks/templates").json()) == 3
