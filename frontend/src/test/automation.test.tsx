import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "../app/store";
import { AutomationPage } from "../features/automation/AutomationPage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeAll(() => vi.stubGlobal("EventSource", undefined));
afterEach(() => vi.restoreAllMocks());

const step = (over: Record<string, unknown>) => ({
  id: "s1", tool: "app.open", args: { name: "spotify" }, when: "", on_fail: "continue", note: "", status: "succeeded", attempts: 1,
  summary: "Started Spotify.", verified: true, observed: "Spotify Free", error: "", error_class: "", already_done: false, approved: null, recovery: "", ...over,
});
const run = (over: Record<string, unknown>) => ({
  id: 1, goal: "open spotify and tidy downloads", status: "completed", source: "text", workflow: "", criteria: [], steps: [step({})],
  pending: null, metrics: { tool_calls: 1, retries: 0, llm_calls: 0, seconds: 0.1 }, summary: "Started Spotify.", caveats: [], created_at: "", finished_at: "", ...over,
});
const caps = {
  tools: [
    { name: "app.open", description: "Open an app.", risk: "low", needs: [], tags: [], timeout_s: 15, retries: 1, idempotent: true, verifies: true, available: true, unavailable_reason: "", limits: "" },
    { name: "project.git_status", description: "Git.", risk: "read", needs: [], tags: [], timeout_s: 20, retries: 0, idempotent: false, verifies: false, available: false, unavailable_reason: "Open a project first.", limits: "" },
  ],
  models: { active: "qwen2.5-coder:7b", installed: [], vision_capable_but_unused: ["llava:7b"], reachable: true },
  environment: { platform: "Windows", ram_gb: 32, cpus: 8 }, agent: {}, limitations: ["No vision model is used."],
};

function mock(runs: unknown[], calls: string[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/runs") return json(runs);
    if (url === "/api/capabilities") return json(caps);
    if (url === "/api/capabilities/diagnose") return json({ ok: 1, total: 2, tools: [{ tool: "project.git_status", ok: false, detail: "Open a project first.", risk: "read", verifies: false }] });
    if (url === "/api/preferences" && init?.method === "PATCH") return json({ agent: { enabled: true, auto_approve_risk: "low" } });
    if (url === "/api/preferences") return json({ agent: { enabled: true, auto_approve_risk: "medium" } });
    if (url === "/api/workflows") return json([{ name: "organize_folder", description: "Sort a folder.", keywords: [], builtin: true, uses: 0 }]);
    if (url.startsWith("/api/runs/") && init?.method === "POST") return json({});
    return json({});
  }));
}

describe("AutomationPage", () => {
  it("shows steps with what was checked, and says plainly when a step could not be checked", async () => {
    mock([run({ status: "partially_completed", summary: "I couldn't finish all of it.", steps: [
      step({}), step({ id: "s2", tool: "system.volume", verified: null, summary: "Volume up." }),
      step({ id: "s3", tool: "files.apply_moves", status: "failed", verified: false, error_class: "verify_failed", summary: "Check failed." }),
    ] })]);
    render(<StoreProvider><AutomationPage /></StoreProvider>);
    const rows = await screen.findAllByTestId("run-step");
    expect(within(rows[0]).getByText("checked")).toBeInTheDocument();
    expect(within(rows[1]).getByText("not checked")).toBeInTheDocument();
    expect(within(rows[2]).getByText("check failed")).toBeInTheDocument();
    expect(screen.getByTestId("run-incomplete")).toHaveTextContent("files.apply_moves");
  });

  it("approves a waiting step with a button and sends nothing else", async () => {
    const calls: string[] = [];
    mock([run({ status: "waiting_for_approval", summary: "", steps: [step({ status: "awaiting_approval", verified: null, summary: "" })],
      pending: { kind: "approval", step: "s1", tool: "files.apply_moves", risk: "high", prompt: "Move 4 file(s) in Downloads" } })], calls);
    render(<StoreProvider><AutomationPage /></StoreProvider>);
    const box = await screen.findByTestId("run-approval");
    expect(box).toHaveTextContent("Move 4 file(s) in Downloads");
    expect(box).toHaveTextContent("Nothing has been done for this step yet");
    await userEvent.click(within(box).getByRole("button", { name: /Approve and run/ }));
    await waitFor(() => expect(calls).toContain("POST /api/runs/1/approve"));
    expect(calls.filter((c) => c.startsWith("POST") && !c.includes("/approve"))).toEqual([]);
  });

  it("offers to resume a run interrupted by a restart", async () => {
    const calls: string[] = [];
    mock([run({ status: "waiting_for_input", pending: { kind: "resume", step: "s2", prompt: "NEXUS restarted while this was running. Resume it?" } })], calls);
    render(<StoreProvider><AutomationPage /></StoreProvider>);
    await userEvent.click(await screen.findByRole("button", { name: /Resume/ }));
    await waitFor(() => expect(calls).toContain("POST /api/runs/1/resume"));
  });

  it("lists what NEXUS can do, flags unavailable tools and runs a self-check", async () => {
    mock([]);
    render(<StoreProvider><AutomationPage /></StoreProvider>);
    expect(await screen.findByText("Nothing yet")).toBeInTheDocument();
    const rows = await screen.findAllByTestId("tool-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getByText("unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Vision models installed but not used: llava:7b/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Run self-check/ }));
    expect(await screen.findByTestId("diagnose-result")).toHaveTextContent("1 of 2 tools ready. Nothing on your computer was changed.");
    expect(screen.getByText("organize folder")).toBeInTheDocument();
  });

  it("changes how much NEXUS may do alone", async () => {
    const calls: string[] = [];
    mock([], calls);
    render(<StoreProvider><AutomationPage /></StoreProvider>);
    await userEvent.selectOptions(await screen.findByLabelText("Autonomy"), "low");
    await waitFor(() => expect(calls).toContain("PATCH /api/preferences"));
  });
});
