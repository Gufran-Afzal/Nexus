import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CommandPalette } from "../app/CommandPalette";
import { DiffView } from "../components/DiffView";
import { StatusBadge, Toggle, statusTone } from "../components/ui";

describe("DiffView", () => {
  it("marks added and removed lines with line numbers", () => {
    const diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n";
    render(<DiffView diff={diff} />);
    expect(screen.getByLabelText("removed line")).toHaveTextContent("- return a - b", { normalizeWhitespace: true });
    expect(screen.getByLabelText("added line")).toHaveTextContent("+ return a + b", { normalizeWhitespace: true });
  });
  it("shows an empty state", () => {
    render(<DiffView diff="" />);
    expect(screen.getByText("No textual changes.")).toBeInTheDocument();
  });
});

describe("Toggle", () => {
  it("is an accessible switch", async () => {
    const onChange = vi.fn();
    render(<Toggle checked={false} onChange={onChange} label="Allow edits" />);
    const sw = screen.getByRole("switch", { name: "Allow edits" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });
});

describe("status tones", () => {
  it("distinguishes lifecycle states", () => {
    expect(statusTone("pending_approval")).toBe("amber");
    expect(statusTone("running")).toBe("blue");
    expect(statusTone("succeeded")).toBe("green");
    expect(statusTone("failed")).toBe("red");
    render(<StatusBadge status="waiting_approval" />);
    expect(screen.getByText("waiting approval")).toBeInTheDocument();
  });
});

describe("CommandPalette", () => {
  it("filters and runs the selected command with Enter", async () => {
    const run = vi.fn();
    const onClose = vi.fn();
    render(<CommandPalette items={[{ id: "a", label: "Go to Models", run: vi.fn() }, { id: "b", label: "New chat", run }]} onClose={onClose} />);
    await userEvent.type(screen.getByLabelText("Command"), "new");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(screen.getByLabelText("Command"), { key: "Enter" });
    expect(run).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
