import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "../app/store";
import { ChatPage } from "../features/chat/ChatPage";

const health = {
  version: "0.1.0", uptime_s: 1, paused: false, active_model: "qwen2.5-coder:7b", onboarding_complete: true,
  ollama: { available: true, version: "0.12", url: "x" }, generations: [], database: { path: "", schema_version: 1, recovered_from: null },
  voice: { enabled: false, state: "disabled", muted: false },
  system: { platform: "Windows", python: "3.12", cpu_percent: 1, cpu_count: 8, memory_total: 1, memory_available: 1 },
  startup: { supported: true, enabled: false },
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

function sseResponse(events: string[]) {
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    start(c) {
      for (const e of events) c.enqueue(enc.encode(e));
      c.close();
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

afterEach(() => vi.restoreAllMocks());

describe("ChatPage", () => {
  it("creates a conversation, streams the reply and shows which files were used", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url === "/api/health") return json(health);
      if (url === "/api/conversations" && init?.method === "POST") return json({ id: 7, title: "New conversation", mode: "assistant", workspace_id: null, created_at: "2026-01-01T00:00:00", updated_at: "2026-01-01T00:00:00", messages: [] });
      if (url === "/api/conversations" || url === "/api/workspaces") return json([]);
      if (url === "/api/conversations/7/messages") {
        return sseResponse([
          'event: meta\ndata: {"generation_id":"g1","model":"qwen2.5-coder:7b","context_files":[{"path":"src/calc.py","tokens":20,"reason":"keyword match","truncated":false}],"omitted":[]}\n\n',
          'event: token\ndata: {"text":"The bug is "}\n\n',
          'event: token\ndata: {"text":"`a - b`."}\n\n',
          'event: done\ndata: {"status":"complete","stats":{"tokens_per_s":4.2}}\n\n',
        ]);
      }
      return json({});
    }));
    render(<StoreProvider><ChatPage /></StoreProvider>);
    const box = await screen.findByLabelText("Message");
    await userEvent.type(box, "why is add wrong?{Enter}");
    await waitFor(() => expect(screen.getByText(/The bug is/)).toBeInTheDocument());
    expect(screen.getByText("a - b")).toBeInTheDocument();
    expect(screen.getByText(/Answer used 1 file/)).toBeInTheDocument();
    expect(screen.getByText("4.2 tok/s")).toBeInTheDocument();
    const post = calls.find((c) => c.url === "/api/conversations/7/messages");
    expect(post?.init?.headers).toMatchObject({ "X-Nexus-Client": "ui" });
    expect(JSON.parse(String(post?.init?.body)).content).toBe("why is add wrong?");
  });

  it("shows a clear error when the model is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/health") return json({ ...health, ollama: { available: false, url: "x", error: "refused" } });
      if (url === "/api/conversations" && init?.method === "POST") return json({ id: 8, title: "t", mode: "assistant", workspace_id: null, created_at: "", updated_at: "" });
      if (url.startsWith("/api/conversations/8/messages")) {
        return sseResponse([
          'event: meta\ndata: {"generation_id":"g2","context_files":[],"omitted":[]}\n\n',
          'event: error\ndata: {"message":"Cannot reach Ollama. Is Ollama running?"}\n\n',
          'event: done\ndata: {"status":"error"}\n\n',
        ]);
      }
      return json([]);
    }));
    render(<StoreProvider><ChatPage /></StoreProvider>);
    expect(await screen.findByText(/Ollama is offline/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Is Ollama running?");
  });

  it("shows agent tool steps live, with links to review proposals", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/health") return json(health);
      if (url === "/api/conversations" && init?.method === "POST") return json({ id: 9, title: "t", mode: "coding", workspace_id: 1, created_at: "", updated_at: "" });
      if (url === "/api/conversations/9/messages") {
        return sseResponse([
          'event: meta\ndata: {"generation_id":"g3","context_files":[],"omitted":[]}\n\n',
          'event: tool\ndata: {"phase":"running","name":"read_file","arguments":{"path":"src/calc.py"}}\n\n',
          'event: tool\ndata: {"phase":"done","name":"read_file","arguments":{"path":"src/calc.py"},"status":"succeeded","summary":"Read src/calc.py (lines 1-2)","ref":{}}\n\n',
          'event: tool\ndata: {"phase":"done","name":"propose_edit","arguments":{"path":"src/calc.py"},"status":"awaiting_permission","summary":"Proposed change to src/calc.py","ref":{"changeset_id":4}}\n\n',
          'event: token\ndata: {"text":"I proposed a fix."}\n\n',
          'event: done\ndata: {"status":"complete","stats":{}}\n\n',
        ]);
      }
      return json([]);
    }));
    render(<StoreProvider><ChatPage /></StoreProvider>);
    await userEvent.type(await screen.findByLabelText("Message"), "fix add{Enter}");
    expect(await screen.findByText("Read src/calc.py (lines 1-2)")).toBeInTheDocument();
    expect(screen.getByText("Review change #4")).toBeInTheDocument();
    expect(screen.getByText("awaiting permission")).toBeInTheDocument();
  });
});
