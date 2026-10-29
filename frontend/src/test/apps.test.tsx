import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "../app/store";
import { AlarmModal } from "../components/AlarmModal";
import { AppsPage } from "../features/apps/AppsPage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const spotify = { id: "spotify", name: "Spotify", aliases: ["music"], kind: "uwp", target: "x", enabled: true, source: "store", profile: "spotify",
  examples: ["Open Spotify", "Close Spotify", "Play lo-fi beats on Spotify"], can_close: false };
const base = {
  platform: "win32", scanned_at: 1760000000, error: null,
  integrated: [spotify],
  discovered: [
    { id: "spotify", name: "Spotify", kind: "uwp", source: "store", integrated: true },
    { id: "google-chrome", name: "Google Chrome", kind: "exe", source: "start-menu", integrated: false },
    { id: "notepad", name: "Notepad", kind: "exe", source: "essentials", integrated: false },
  ],
};

beforeAll(() => {
  // The event stream is not under test here.
  vi.stubGlobal("EventSource", undefined);
});
afterEach(() => vi.restoreAllMocks());

describe("AppsPage", () => {
  it("lists apps found on the PC, adds one with a click and shows what to say", async () => {
    const calls: { url: string; method: string; body: string }[] = [];
    let apps = structuredClone(base);
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      if (url === "/api/health") return json({});
      if (url === "/api/apps/integrate") {
        apps = { ...apps, discovered: apps.discovered.map((d) => (d.id === "google-chrome" ? { ...d, integrated: true } : d)) };
        return json({ ...spotify, id: "google-chrome", name: "Google Chrome", examples: ["Open Google Chrome"] });
      }
      if (url === "/api/apps") return json(apps);
      return json([]);
    }));
    render(<StoreProvider><AppsPage /></StoreProvider>);
    expect(await screen.findByText("Integrated apps (1)")).toBeInTheDocument();
    expect(within(screen.getByTestId("integrated-app")).getByText(/Play lo-fi beats on Spotify/)).toBeInTheDocument();
    const list = screen.getByTestId("discovered-list");
    expect(within(list).queryByText("Spotify")).not.toBeInTheDocument(); // already integrated: hidden by default
    expect(within(list).getByText("Google Chrome")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search apps"), "note");
    expect(within(list).queryByText("Google Chrome")).not.toBeInTheDocument();
    expect(within(list).getByText("Notepad")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Search apps"));
    await userEvent.click(screen.getByLabelText("Add Google Chrome"));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/apps/integrate" && c.body === JSON.stringify({ id: "google-chrome" }))).toBe(true));
  });

  it("saves aliases and surfaces a scan error", async () => {
    const calls: { url: string; method: string; body: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      if (url === "/api/apps") return json({ ...base, error: "Scanning for apps failed: PowerShell was not found" });
      return json({});
    }));
    render(<StoreProvider><AppsPage /></StoreProvider>);
    expect(await screen.findByText(/PowerShell was not found/)).toBeInTheDocument();
    const box = screen.getByLabelText("Other names for Spotify");
    await userEvent.clear(box);
    await userEvent.type(box, "music, tunes");
    await userEvent.tab();
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.url === "/api/apps/integrated/spotify"
      && c.body === JSON.stringify({ aliases: ["music", "tunes"] }))).toBe(true));
  });
});

describe("AlarmModal", () => {
  it("shows a ringing reminder and snoozes it through the API", async () => {
    const calls: { url: string; body: string }[] = [];
    class FakeES {
      static last: FakeES | null = null;
      handlers: Record<string, (e: MessageEvent) => void> = {};
      onerror: (() => void) | null = null;
      onopen: (() => void) | null = null;
      constructor() { FakeES.last = this; }
      addEventListener(name: string, fn: (e: MessageEvent) => void) { this.handlers[name] = fn; }
      close() { /* noop */ }
    }
    vi.stubGlobal("EventSource", FakeES);
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: String(init?.body ?? "") });
      if (url === "/api/reminders") return json([{ id: 7, status: "fired" }]);
      return json({});
    }));
    render(<StoreProvider><AlarmModal /></StoreProvider>);
    await waitFor(() => expect(FakeES.last).not.toBeNull());
    const ev = { type: "reminder", id: 7, kind: "reminder", text: "Call the dentist", late: false, ring: 1, repeat: "", action: null };
    act(() => FakeES.last!.handlers.update(new MessageEvent("update", { data: JSON.stringify(ev) })));
    const card = await screen.findByTestId("alarm");
    expect(within(card).getByText("Call the dentist")).toBeInTheDocument();
    act(() => FakeES.last!.handlers.update(new MessageEvent("update", { data: JSON.stringify({ ...ev, ring: 2 }) })));
    expect(screen.getAllByTestId("alarm")).toHaveLength(1); // a re-ring updates the same card
    expect(screen.getByText(/ring 2/)).toBeInTheDocument();
    await userEvent.click(within(screen.getByTestId("alarm")).getByRole("button", { name: "10 min" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/reminders/7/snooze" && c.body === JSON.stringify({ minutes: 10 }))).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("alarm")).not.toBeInTheDocument());
  });
});
