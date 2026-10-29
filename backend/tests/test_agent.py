from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.loop import _VisibleFilter, parse_text_tool_calls
from tests.conftest import FakeProvider, parse_sse

FIXED = "def add(a, b):\n    return a + b\n"


def scripted(steps):
    """Return a reply function that plays the given responses in order (one per model call)."""
    calls = {"n": 0}

    def reply(messages, options):
        i = min(calls["n"], len(steps) - 1)
        calls["n"] += 1
        step = steps[i]
        return step(messages, options) if callable(step) else step

    return reply


def coding_conv(client: TestClient, project: Path) -> int:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    return client.post("/api/conversations", json={"mode": "coding", "workspace_id": ws}).json()["id"]


def send(client, cid, text):
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": text, "auto_context": False})
    assert r.status_code == 200
    return parse_sse(r.text)


def test_agent_reads_files_then_proposes_edit_without_applying(client: TestClient, provider: FakeProvider,
                                                               project: Path) -> None:
    seen_tool_results = []

    def final(messages, options):
        seen_tool_results.extend(m.content for m in messages if m.role == "tool")
        return "The bug is `a - b`. I proposed change set #1 for your review."

    provider.reply = scripted([
        {"tool_calls": [("search_code", {"query": "def add"})]},
        {"tool_calls": [("read_file", {"path": "src/calc.py"})]},
        {"tool_calls": [("propose_edit", {"path": "src/calc.py", "new_content": FIXED, "summary": "Fix add"})]},
        final,
    ])
    cid = coding_conv(client, project)
    events = send(client, cid, "Fix the add function")
    tools = [e[1] for e in events if e[0] == "tool" and e[1]["phase"] == "done"]
    assert [t["name"] for t in tools] == ["search_code", "read_file", "propose_edit"]
    assert tools[2]["status"] == "awaiting_permission" and tools[2]["ref"]["changeset_id"]
    assert any("return a - b" in r for r in seen_tool_results)
    # The proposal exists but nothing was written.
    assert "a - b" in (project / "src/calc.py").read_text()
    cs = client.get(f"/api/changesets/{tools[2]['ref']['changeset_id']}").json()
    assert cs["status"] == "proposed" and cs["source"] == "model"
    msg = client.get(f"/api/conversations/{cid}").json()["messages"][-1]
    assert [t["name"] for t in msg["meta"]["tool_trace"]] == ["search_code", "read_file", "propose_edit"]
    assert msg["status"] == "complete"


def test_agent_cannot_read_secrets_or_escape(client: TestClient, provider: FakeProvider, project: Path) -> None:
    results = []

    def final(messages, options):
        results.extend(m.content for m in messages if m.role == "tool")
        return "done"

    provider.reply = scripted([
        {"tool_calls": [("read_file", {"path": ".env"}), ("read_file", {"path": "../../etc/passwd"}),
                        ("read_file", {"start_line": 1}), ("delete_everything", {})]},
        final,
    ])
    cid = coding_conv(client, project)
    events = send(client, cid, "show me the env file")
    statuses = [e[1]["status"] for e in events if e[0] == "tool" and e[1]["phase"] == "done"]
    assert statuses == ["failed", "failed", "failed", "failed"]
    joined = "\n".join(results)
    assert "supersecretvalue" not in joined
    assert "Unknown tool" in joined and "Invalid arguments" in joined


def test_agent_command_requires_approval(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.reply = scripted([
        {"tool_calls": [("propose_command", {"command": "pytest -q"}),
                        ("propose_command", {"command": "powershell -Command rm -r C:/"})]},
        "I asked to run the tests; please approve command #1.",
    ])
    cid = coding_conv(client, project)
    events = send(client, cid, "run the tests")
    done = [e[1] for e in events if e[0] == "tool" and e[1]["phase"] == "done"]
    assert done[0]["status"] == "awaiting_permission" and done[1]["status"] == "failed"
    runs = client.get("/api/commands").json()
    statuses = {r["argv"][0]: r["status"] for r in runs}
    assert statuses == {"pytest": "pending_approval", "powershell": "rejected"}
    assert all(r["requested_by"] == "model" for r in runs)


def test_text_protocol_fallback_when_model_lacks_tools(client: TestClient, provider: FakeProvider,
                                                       project: Path) -> None:
    provider.supports_tools = False
    provider.reply = scripted([
        'Let me look.\n```tool\n{"name": "read_file", "arguments": {"path": "src/calc.py"}}\n```',
        lambda m, o: "Found it: " + ("subtraction" if any("return a - b" in x.content for x in m) else "nothing"),
    ])
    cid = coding_conv(client, project)
    events = send(client, cid, "what does add do")
    text = "".join(e[1]["text"] for e in events if e[0] == "token")
    assert "```tool" not in text  # tool blocks are hidden from the user
    assert "Found it: subtraction" in text
    assert any(e[0] == "notice" for e in events)


def test_agent_round_limit(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.reply = lambda messages, options: (
        {"tool_calls": [("list_files", {})]} if options.tools else "Final answer after limit.")
    cid = coding_conv(client, project)
    events = send(client, cid, "loop forever")
    n_tools = sum(1 for e in events if e[0] == "tool" and e[1]["phase"] == "done")
    assert n_tools == 6
    assert "Final answer after limit." in "".join(e[1]["text"] for e in events if e[0] == "token")


def test_assistant_mode_has_no_tools(client: TestClient, provider: FakeProvider, project: Path) -> None:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    cid = client.post("/api/conversations", json={"mode": "assistant", "workspace_id": ws}).json()["id"]
    send(client, cid, "explain what this project does")
    assert provider.calls[-1]["options"].tools is None


def test_text_parsing_helpers() -> None:
    calls = parse_text_tool_calls('x ```tool\n{"name":"search_code","arguments":{"query":"a"}}\n``` y ```tool\nnot json```')
    assert [(c.name, c.arguments) for c in calls] == [("search_code", {"query": "a"})]
    f = _VisibleFilter()
    out = f.feed("Hello ``") + f.feed("`tool\n{...}") + f.feed("```") + f.flush()
    assert out == "Hello "


def test_qwen_style_text_tool_calls_are_parsed_and_hidden(client: TestClient, provider: FakeProvider,
                                                          project: Path) -> None:
    provider.reply = scripted([
        'Checking.<tool_call>\n{"name": "read_file", "arguments": {"path": "src/calc.py"}}\n</tool_call>',
        "It subtracts.",
    ])
    cid = coding_conv(client, project)
    events = send(client, cid, "what does add do")
    text = "".join(e[1]["text"] for e in events if e[0] == "token")
    assert "<tool_call>" not in text and "Checking." in text and "It subtracts." in text
    assert [e[1]["name"] for e in events if e[0] == "tool" and e[1]["phase"] == "done"] == ["read_file"]


def test_tool_call_arguments_count_toward_budget() -> None:
    from app.agent.loop import _tokens
    from app.providers.base import ChatMessage, ToolCall

    m = ChatMessage("assistant", "", tool_calls=[ToolCall("propose_edit", {"path": "a.py", "new_content": "x" * 4000})])
    assert _tokens(m) > 1000
