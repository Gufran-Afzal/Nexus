import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import FakeProvider, parse_sse


def new_conv(client: TestClient, **kw) -> int:
    r = client.post("/api/conversations", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def send(client: TestClient, cid: int, content: str, **kw):
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": content, **kw})
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def test_health_reports_ollama_down(client: TestClient, provider: FakeProvider) -> None:
    provider.available = False
    h = client.get("/api/health").json()
    assert h["ollama"]["available"] is False
    assert h["database"]["schema_version"] >= 1
    models = client.get("/api/models").json()
    assert models["available"] is False and models["models"] == []


def test_guards_reject_foreign_host_origin_and_missing_header(app) -> None:
    with TestClient(app) as raw:
        assert raw.post("/api/conversations", json={}).status_code == 403  # no X-Nexus-Client
        assert raw.get("/api/health/live", headers={"host": "evil.example.com"}).status_code == 403
        r = raw.post("/api/conversations", json={},
                     headers={"origin": "https://evil.example.com", "X-Nexus-Client": "x"})
        assert r.status_code == 403
        ok = raw.post("/api/conversations", json={},
                      headers={"origin": "http://localhost:5173", "X-Nexus-Client": "x"})
        assert ok.status_code == 200


def test_streaming_chat_persists_messages(client: TestClient, provider: FakeProvider) -> None:
    provider.reply = "Two plus two is four."
    cid = new_conv(client)
    events = send(client, cid, "Why is the sky blue?")
    kinds = [e[0] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    text = "".join(e[1]["text"] for e in events if e[0] == "token")
    assert text == "Two plus two is four."
    conv = client.get(f"/api/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["status"] == "complete"
    assert conv["messages"][1]["content"] == text
    assert conv["title"].startswith("Why is the sky blue")
    # system prompt includes the honesty / safety rules
    system = provider.calls[0]["messages"][0].content
    assert "untrusted DATA" in system and "no internet access" in system


def test_missing_model_and_unavailable_errors(client: TestClient, provider: FakeProvider) -> None:
    cid = new_conv(client)
    events = send(client, cid, "explain closures", model="llama-not-installed")
    err = [e for e in events if e[0] == "error"]
    assert err and "not installed" in err[0][1]["message"]
    provider.available = False
    events = send(client, cid, "hi again")
    err = [e for e in events if e[0] == "error"]
    assert err and "Is Ollama running" in err[0][1]["message"]
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert msgs[-1]["status"] == "error"


def test_stop_generation(client: TestClient, provider: FakeProvider) -> None:
    provider.reply = " ".join(["word"] * 200)
    provider.token_delay = 0.02
    cid = new_conv(client)
    result: dict = {}

    def run() -> None:
        result["events"] = send(client, cid, "long answer please")

    t = threading.Thread(target=run)
    t.start()
    gen_id = None
    for _ in range(100):
        gens = client.get("/api/health").json()["generations"]
        if gens and gens[0]["state"] == "generating":
            gen_id = gens[0]["id"]
            break
        time.sleep(0.02)
    assert gen_id
    assert client.post(f"/api/generations/{gen_id}/stop").json()["stopped"] is True
    t.join(10)
    done = [e for e in result["events"] if e[0] == "done"][0][1]
    assert done["status"] == "stopped"
    msg = client.get(f"/api/conversations/{cid}").json()["messages"][-1]
    assert msg["status"] == "stopped"
    assert 0 < len(msg["content"].split()) < 200


def test_coding_mode_uses_workspace_context_and_reports_files(client: TestClient, provider: FakeProvider,
                                                              project: Path) -> None:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()
    cid = new_conv(client, mode="coding", workspace_id=ws["id"])
    events = send(client, cid, "Why does the add function in calc return the wrong value?")
    meta = events[0][1]
    paths = [f["path"] for f in meta["context_files"]]
    assert "src/calc.py" in paths
    prompt = "\n".join(m.content for m in provider.calls[-1]["messages"])
    assert "return a - b" in prompt
    assert "supersecretvalue" not in prompt
    assert "Use 4-space indentation" in prompt  # NEXUS.md guidance
    assert "senior software engineer" in prompt


def test_context_budget_is_respected(client: TestClient, provider: FakeProvider, project: Path) -> None:
    (project / "src" / "big.py").write_text("# add function\n" + "x = 1\n" * 40000, encoding="utf-8")
    client.patch("/api/preferences", json={"generation": {"num_ctx": 2048, "max_output_tokens": 256}})
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()
    prev = client.post(f"/api/workspaces/{ws['id']}/context-preview",
                       json={"query": "add function", "files": ["src/big.py"]}).json()
    assert prev["used_tokens"] <= prev["budget_tokens"]
    big = [f for f in prev["included"] if f["path"] == "src/big.py"]
    assert (big and big[0]["truncated"]) or any(o["path"] == "src/big.py" for o in prev["omitted"])


def test_assistant_actions_are_real(client: TestClient, launched: list) -> None:
    cid = new_conv(client)
    events = send(client, cid, "set a timer for 5 minutes")
    assert events[0][1]["action"] == {"kind": "timer", "status": "succeeded"}
    rems = client.get("/api/reminders").json()
    assert len(rems) == 1 and rems[0]["kind"] == "timer"
    events = send(client, cid, "open notepad")
    assert launched == [("notepad.exe", "app")]
    events = send(client, cid, "open regedit")
    assert events[0][1]["action"]["status"] == "unsupported"
    assert len(launched) == 1
    events = send(client, cid, "open evil.com")
    assert events[0][1]["action"]["status"] == "unsupported"
    acts = client.get("/api/actions").json()
    assert {a["status"] for a in acts} == {"succeeded", "unsupported"}


def test_conversation_management(client: TestClient) -> None:
    cid = new_conv(client)
    send(client, cid, "remember the word pineapple")
    assert client.get("/api/conversations", params={"q": "pineapple"}).json()[0]["id"] == cid
    client.patch(f"/api/conversations/{cid}", json={"title": "Fruit"})
    assert client.get(f"/api/conversations/{cid}").json()["title"] == "Fruit"
    md = client.get(f"/api/conversations/{cid}/export").text
    assert "# Fruit" in md and "pineapple" in md
    assert client.delete("/api/conversations").status_code == 400  # needs confirm
    assert client.delete(f"/api/conversations/{cid}").json()["deleted"]
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_paused_blocks_chat(client: TestClient) -> None:
    cid = new_conv(client)
    client.post("/api/system/pause")
    assert client.post(f"/api/conversations/{cid}/messages", json={"content": "hi"}).status_code == 409
    client.post("/api/system/resume")
    assert client.post(f"/api/conversations/{cid}/messages", json={"content": "hi"}).status_code == 200


def test_models_and_pull(client: TestClient, provider: FakeProvider) -> None:
    data = client.get("/api/models").json()
    assert data["active_installed"] is True
    r = client.post("/api/models/pull", json={"name": "phi3:mini"})
    ev = parse_sse(r.text)
    assert ev[-1][0] == "done"
    assert "phi3:mini" in [m["name"] for m in client.get("/api/models").json()["models"]]
    assert client.post("/api/models/pull", json={"name": "bad name; rm -rf"}).status_code == 422
    assert client.post("/api/models/active", json={"name": "phi3:mini"}).json()["active"] == "phi3:mini"


def test_preferences_validation_and_startup(client: TestClient) -> None:
    assert client.patch("/api/preferences", json={"nope": 1}).status_code == 400
    assert client.patch("/api/preferences", json={"generation": {"temperature": 9}}).status_code == 422
    p = client.patch("/api/preferences", json={"generation": {"temperature": 0.7}}).json()
    assert p["generation"]["temperature"] == 0.7 and p["generation"]["num_ctx"] == 4096
    st = client.post("/api/system/startup", json={"enabled": True}).json()
    assert st["enabled"] is True and "run_nexus.pyw" in st["command"]
    assert client.post("/api/system/startup", json={"enabled": False}).json()["enabled"] is False
    storage = client.get("/api/system/storage").json()
    assert "Raw audio is never stored" in " ".join(storage["what_is_stored"])


def test_prompt_failure_reports_error_event(client: TestClient, provider: FakeProvider, project: Path,
                                            monkeypatch) -> None:
    import app.services.conversation as conv_mod

    def boom(*a, **k):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(conv_mod, "build_prompt", boom)
    cid = new_conv(client)
    events = send(client, cid, "explain closures")
    assert [e[0] for e in events] == ["meta", "error", "done"]
    assert "Access is denied" in events[1][1]["message"]
    assert client.get(f"/api/conversations/{cid}").json()["messages"][-1]["status"] == "error"


def test_streaming_messages_marked_stopped_after_restart(settings, provider) -> None:
    from app.database.models import Conversation, Message
    from app.main import create_app
    from tests.conftest import FakeRegistry

    app1 = create_app(settings, provider=provider, startup_backend=FakeRegistry())
    with TestClient(app1, headers={"X-Nexus-Client": "t"}):
        with app1.state.nexus.db.session() as s:
            c = Conversation(title="x")
            s.add(c)
            s.flush()
            s.add(Message(conversation_id=c.id, role="assistant", content="half an ans", status="streaming"))
            cid = c.id
    app2 = create_app(settings, provider=provider, startup_backend=FakeRegistry())
    with TestClient(app2, headers={"X-Nexus-Client": "t"}) as c2:
        msg = c2.get(f"/api/conversations/{cid}").json()["messages"][0]
        assert msg["status"] == "stopped" and "stopped before" in msg["meta"]["error"]


def test_graceful_shutdown_endpoint(client: TestClient) -> None:
    import time as _t

    assert client.post("/api/system/shutdown").status_code == 409  # not under the launcher
    called = []
    client.app.state.nexus.request_shutdown = lambda: called.append(True)  # type: ignore[attr-defined]
    assert client.post("/api/system/shutdown").json() == {"shutting_down": True}
    _t.sleep(0.5)
    assert called == [True]
