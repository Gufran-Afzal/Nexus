import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "../app/store";
import { LiveCaption } from "../components/LiveCaption";
import { PendingCard } from "../components/PendingCard";
import { PeoplePage } from "../features/people/PeoplePage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

class FakeES {
  static last: FakeES | null = null;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  onmessage: ((e: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { FakeES.last = this; }
  addEventListener(name: string, fn: (e: MessageEvent) => void) { (this.listeners[name] ??= []).push(fn); }
  close() { /* unused */ }
  emit(data: unknown) { (this.listeners.update ?? []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent)); }
}

beforeAll(() => vi.stubGlobal("EventSource", undefined));
afterEach(() => vi.restoreAllMocks());

const skillsBody = {
  skills: [{ id: "message", title: "Message someone", examples: ["Message Mom on WhatsApp I'll be late"], enabled: true }],
  custom: [], default_message_app: "whatsapp", email_app: "gmail", smart_routing: true, send_key_delay_s: 3, pending: null,
};

describe("PeoplePage", () => {
  it("adds a person with a validated phone number and lists them", async () => {
    const calls: { url: string; method: string; body: string }[] = [];
    let people: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      if (url === "/api/contacts" && init?.method === "POST") {
        const b = JSON.parse(String(init.body));
        if (b.phone === "12") return json({ detail: "Enter the phone number with its country code" }, 422);
        people = [{ id: 1, name: b.name, aliases: b.aliases, phone: "15551230001", email: "", notes: "" }];
        return json(people[0], 201);
      }
      if (url === "/api/contacts") return json(people);
      if (url === "/api/skills") return json(skillsBody);
      if (url === "/api/preferences") return json({ email_app: "gmail", smart_routing: true, send_key_delay_s: 3 });
      return json({});
    }));
    render(<StoreProvider><PeoplePage /></StoreProvider>);
    expect(await screen.findByText("Nobody yet")).toBeInTheDocument();
    const add = screen.getByRole("button", { name: /Add person/ });
    expect(add).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Name"), "Mom");
    await userEvent.type(screen.getByLabelText("Other names"), "mother, mum");
    await userEvent.type(screen.getByLabelText("Phone"), "12");
    await userEvent.click(add);
    expect(await screen.findByRole("alert")).toHaveTextContent("country code");
    await userEvent.clear(screen.getByLabelText("Phone"));
    await userEvent.type(screen.getByLabelText("Phone"), "+1 555 123 0001");
    await userEvent.click(add);
    const row = await screen.findByTestId("person");
    expect(within(row).getByText("Mom")).toBeInTheDocument();
    const post = calls.filter((c) => c.url === "/api/contacts" && c.method === "POST").pop()!;
    expect(JSON.parse(post.body)).toMatchObject({ name: "Mom", aliases: ["mother", "mum"], phone: "+1 555 123 0001" });
  });

  it("shows the skills and can switch one off", async () => {
    const calls: { url: string; method: string; body: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      if (url === "/api/skills/message") return json({ ...skillsBody, skills: [{ ...skillsBody.skills[0], enabled: false }] });
      if (url === "/api/skills") return json(skillsBody);
      if (url === "/api/contacts") return json([]);
      if (url === "/api/preferences") return json({ email_app: "gmail", smart_routing: true, send_key_delay_s: 3 });
      return json({});
    }));
    render(<StoreProvider><PeoplePage /></StoreProvider>);
    const skill = await screen.findByTestId("skill");
    expect(within(skill).getByText(/Message Mom on WhatsApp/)).toBeInTheDocument();
    await userEvent.click(within(skill).getByRole("switch"));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/skills/message" && c.body === JSON.stringify({ enabled: false }))).toBe(true));
  });
});

describe("pending confirmation and live captions", () => {
  it("shows what is waiting and sends the answer from the card", async () => {
    const calls: string[] = [];
    vi.stubGlobal("EventSource", FakeES);
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/pending") return json({ pending: { kind: "send", title: "WhatsApp message to Mom", preview: "I'll be late", question: "", expires_in: 80 } });
      if (url === "/api/pending/confirm") return json({ message: "Pressed Send.", status: "succeeded", pending: null });
      return json({});
    }));
    render(<StoreProvider><PendingCard /></StoreProvider>);
    const card = await screen.findByTestId("pending");
    expect(card).toHaveTextContent("WhatsApp message to Mom");
    expect(card).toHaveTextContent("I'll be late");
    await userEvent.click(within(card).getByRole("button", { name: "Send it" }));
    await waitFor(() => expect(calls).toContain("POST /api/pending/confirm"));
    await waitFor(() => expect(screen.queryByTestId("pending")).not.toBeInTheDocument());
    vi.stubGlobal("EventSource", undefined);
  });

  it("shows the live caption only while recording", async () => {
    vi.stubGlobal("EventSource", FakeES);
    vi.stubGlobal("fetch", vi.fn(async () => json({ pending: null })));
    render(<StoreProvider><LiveCaption /></StoreProvider>);
    await waitFor(() => expect(FakeES.last).not.toBeNull());
    expect(screen.queryByTestId("caption")).not.toBeInTheDocument();
    await act(async () => { FakeES.last!.emit({ type: "voice", state: "recording" }); FakeES.last!.emit({ type: "caption", text: "set a timer for" }); });
    expect(await screen.findByTestId("caption")).toHaveTextContent("set a timer for");
    await act(async () => { FakeES.last!.emit({ type: "voice", state: "thinking" }); });
    await waitFor(() => expect(screen.queryByTestId("caption")).not.toBeInTheDocument());
    vi.stubGlobal("EventSource", undefined);
  });
});
