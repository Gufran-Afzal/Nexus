import os
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.database.db import Database
from app.retrieval.context import build_context
from app.retrieval.index import WorkspaceIndex, tokenize
from app.retrieval.semantic import chunk_text
from tests.conftest import FakeProvider, parse_sse


def test_tokenize_splits_identifiers() -> None:
    toks = tokenize("def parseHttpRequest(user_id): return HTTPServer")
    assert {"parsehttprequest", "parse", "http", "request", "user_id", "user", "httpserver", "server"} <= set(toks)
    assert "def" not in toks and "return" not in toks


def test_index_is_incremental_and_ranks_by_bm25(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "src" / "billing.py").write_text("def compute_invoice_total(items):\n    return sum(i.price for i in items)\n")
    (root / "src" / "auth.py").write_text("def login(user, password):\n    return check(password)\n")
    (root / "README.md").write_text("Invoice tool. invoice invoice.")
    idx = WorkspaceIndex(root)
    assert idx.refresh([]) == 3
    assert idx.refresh([]) == 0  # nothing re-read when nothing changed
    top = [rel for _s, rel in idx.search("where is the invoice total computed?")]
    assert top[0] == "src/billing.py"
    assert "src/auth.py" not in top
    time.sleep(0.01)
    (root / "src" / "auth.py").write_text("def login(user, password):\n    invoice_total = 0\n")
    os.utime(root / "src" / "auth.py", None)
    assert idx.refresh([]) == 1
    (root / "README.md").unlink()
    idx.refresh([])
    assert "README.md" not in idx.docs


def test_large_file_included_as_excerpt_from_semantic_hit(tmp_path: Path) -> None:
    from app.retrieval.context import Hit

    root = tmp_path / "p"
    root.mkdir()
    lines = [f"x{i} = {i}" for i in range(3000)]
    lines[1500] = "def apply_discount(price): return price * 0.9"
    (root / "big.py").write_text("\n".join(lines))
    ctx = build_context(root, "apply discount", [], 600, [], 1_000_000,
                        semantic=[Hit("big.py", "semantic match", 1495, 1505)])
    assert ctx.included and ctx.included[0].truncated
    assert "apply_discount" in ctx.text and "lines 1480-1520" in ctx.included[0].reason
    assert ctx.tokens <= 600


def test_chunking_overlaps() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 131))
    chunks = chunk_text(text)
    assert [(a, b) for a, b, _ in chunks] == [(1, 60), (51, 110), (101, 130)]


def test_semantic_index_build_query_and_chat(client: TestClient, provider: FakeProvider, project: Path) -> None:
    (project / "src" / "money.py").write_text("def to_currency(amount):\n    '''format money and currency'''\n")
    provider.models.append("nomic-embed-text")
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    st = client.get(f"/api/workspaces/{ws}/semantic-index").json()
    assert st["chunks"] == 0 and st["enabled"] is False
    client.post(f"/api/workspaces/{ws}/semantic-index")
    for _ in range(100):
        st = client.get(f"/api/workspaces/{ws}/semantic-index").json()
        if st["job"] and st["job"]["state"] != "running":
            break
        time.sleep(0.05)
    assert st["job"]["state"] == "done" and st["chunks"] >= 3 and st["enabled"] is True
    assert ".env" not in str(st)
    # rebuild is incremental: no new embeddings when nothing changed
    before = provider.embed_calls
    client.post(f"/api/workspaces/{ws}/semantic-index")
    time.sleep(0.3)
    assert provider.embed_calls == before
    cid = client.post("/api/conversations", json={"mode": "coding", "workspace_id": ws}).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": "how is currency shown to users"})
    meta = parse_sse(r.text)[0][1]
    reasons = {f["path"]: f["reason"] for f in meta["context_files"]}
    assert "semantic" in reasons.get("src/money.py", "")


def test_semantic_requires_installed_embedding_model(client: TestClient, project: Path) -> None:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    r = client.post(f"/api/workspaces/{ws}/semantic-index")
    assert r.status_code == 409 and "ollama pull nomic-embed-text" in r.json()["detail"]


def test_semantic_failure_falls_back_to_keywords(client: TestClient, provider: FakeProvider, project: Path) -> None:
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    client.patch(f"/api/workspaces/{ws}", json={"preferences": {"semantic_search": True}})
    cid = client.post("/api/conversations", json={"mode": "coding", "workspace_id": ws}).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": "what does add in calc do"})
    events = parse_sse(r.text)
    assert events[-1][1]["status"] == "complete"
    assert "src/calc.py" in [f["path"] for f in events[0][1]["context_files"]]


def test_model_roles(client: TestClient, provider: FakeProvider, project: Path) -> None:
    provider.models.append("qwen2.5-coder:3b")
    client.patch("/api/preferences", json={"model_roles": {"coding": "qwen2.5-coder:3b"}})
    ws = client.post("/api/workspaces", json={"path": str(project)}).json()["id"]
    coding = client.post("/api/conversations", json={"mode": "coding", "workspace_id": ws}).json()["id"]
    client.post(f"/api/conversations/{coding}/messages", json={"content": "explain this code"})
    assert provider.calls[-1]["model"] == "qwen2.5-coder:3b"
    chat = client.post("/api/conversations", json={"mode": "assistant"}).json()["id"]
    client.post(f"/api/conversations/{chat}/messages", json={"content": "tell me a story"})
    assert provider.calls[-1]["model"] == "qwen2.5-coder:7b"


def test_migration_from_v1_database(tmp_path: Path) -> None:
    """A database created by the first release (schema v1, no semantic table) upgrades cleanly."""
    path = tmp_path / "old.db"
    db = Database(path)
    db.dispose()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE semantic_chunks")
    con.execute("DELETE FROM schema_version WHERE version > 1")
    con.commit()
    con.close()
    db2 = Database(path)
    from app.database.db import LATEST_VERSION

    assert db2.current_version() == LATEST_VERSION
    con = sqlite3.connect(path)
    assert con.execute("SELECT name FROM sqlite_master WHERE name='semantic_chunks'").fetchone()
    con.close()
