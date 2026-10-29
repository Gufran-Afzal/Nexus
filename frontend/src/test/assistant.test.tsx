import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "../app/store";
import { AssistantPage } from "../features/assistant/AssistantPage";

const health = {
  version: "0.3.0", uptime_s: 1, paused: false, active_model: "qwen2.5-coder:7b", onboarding_complete: true,
  ollama: { available: true, version: "0.12", url: "x" }, generations: [], database: { path: "", schema_version: 2, recovered_from: null },
  voice: { enabled: false, state: "disabled", muted: false },
  system: { platform: "Windows", python: "3.12", cpu_percent: 1, cpu_count: 8, memory_total: 1, memory_available: 1 },
  startup: { supported: true, enabled: false },
};
const voiceStatus = {
  enabled: false, state: "disabled", muted: false, paused: false, listening_mode: "push_to_talk", wake_listening: false,
  microphone: { available: false, detail: "No microphone found" }, stt: { available: false, detail: "Speech model 'base.en' has not been downloaded yet", model: "base.en" },
  tts: { available: true, detail: "ok", engine: "sapi" }, wake: { engine: "whisper_keyword", phrase: "hey nexus" },
  last_transcript: null, last_error: null, components: { faster_whisper: true, openwakeword: false }, stt_models: [],
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}
function sse(events: string[]) {
  const enc = new TextEncoder();
  return new Response(new ReadableStream({ start(c) { events.forEach((e) => c.enqueue(enc.encode(e))); c.close(); } }), {
    headers: { "content-type": "text/event-stream" },
  });
}

beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = () => null; // jsdom has no canvas; the orb is cosmetic
});

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("AssistantPage", () => {
  it("runs a typed command, shows what NEXUS did and lists the new timer", async () => {
    const posts: { url: string; body: string }[] = [];
    let timers: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/health") return json(health);
      if (url === "/api/voice/status") return json(voiceStatus);
      if (url === "/api/reminders") return json(timers);
      if (url === "/api/agenda") return json({ text: "Nothing today" });
      if (url === "/api/conversations" && init?.method === "POST") return json({ id: 5, title: "Assistant", mode: "assistant", workspace_id: null, created_at: "", updated_at: "" });
      if (url === "/api/conversations/5") return json({ id: 5, title: "Assistant", mode: "assistant", workspace_id: null, created_at: "", updated_at: "", messages: [] });
      if (url === "/api/conversations/5/messages") {
        posts.push({ url, body: String(init?.body) });
        timers = [{ id: 1, kind: "timer", text: "Timer (10 min)", due_at: new Date(Date.now() + 600000).toISOString(), status: "pending", repeat_daily: false }];
        return sse([
          'event: meta\ndata: {"message_id":2,"action":{"kind":"timer","status":"done"},"context_files":[],"omitted":[]}\n\n',
          'event: token\ndata: {"text":"Timer set for 10 min."}\n\n',
          'event: done\ndata: {"status":"complete"}\n\n',
        ]);
      }
      return json([]);
    }));
    render(<StoreProvider><AssistantPage /></StoreProvider>);
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(await screen.findByText(/Voice is off/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Message NEXUS"), "set a timer for 10 minutes{Enter}");
    expect(await screen.findByText("Timer set for 10 min.")).toBeInTheDocument();
    expect(screen.getByText("Timer set")).toBeInTheDocument(); // action badge
    await waitFor(() => expect(screen.getAllByText("Timer (10 min)").length).toBeGreaterThan(0)); // Coming up list
    expect(JSON.parse(posts[0].body)).toMatchObject({ content: "set a timer for 10 minutes", agent: false });
    expect(localStorage.getItem("nexus.assistantConversation")).toBe("5");
  });

  it("shows the offline state when Ollama is down", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/health") return json({ ...health, ollama: { available: false, url: "x", error: "refused" } });
      if (url === "/api/voice/status") return json(voiceStatus);
      if (url === "/api/conversations" && init?.method === "POST") return json({ id: 6, title: "Assistant", mode: "assistant", workspace_id: null, created_at: "", updated_at: "" });
      if (url === "/api/conversations/6") return json({ id: 6, title: "Assistant", mode: "assistant", workspace_id: null, created_at: "", updated_at: "", messages: [] });
      return json([]);
    }));
    render(<StoreProvider><AssistantPage /></StoreProvider>);
    expect(await screen.findByText("Ollama is offline")).toBeInTheDocument();
  });
});
