import { describe, expect, it } from "vitest";
import { createSseParser, formatBytes } from "../services/api";

describe("SSE parser", () => {
  it("handles events split across chunks", () => {
    const got: { event: string; data: Record<string, unknown> }[] = [];
    const feed = createSseParser((e) => got.push(e));
    feed('event: meta\ndata: {"message_id": 1}\n\nevent: tok');
    feed('en\ndata: {"text": "Hel"}\n\n');
    feed('event: token\r\ndata: {"text": "lo"}\r\n\r\n');
    expect(got.map((g) => g.event)).toEqual(["meta", "token", "token"]);
    expect(got.slice(1).map((g) => g.data.text).join("")).toBe("Hello");
  });
  it("formats bytes", () => {
    expect(formatBytes(4_700_000_000)).toBe("4.4 GB");
    expect(formatBytes(null)).toBe("—");
  });
});
