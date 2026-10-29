import { describe, expect, it } from "vitest";
import { makeChunker } from "../features/assistant/chunker";

describe("speech chunker", () => {
  it("releases sentences as they complete and keeps decimals together", () => {
    const c = makeChunker();
    const got: string[] = [];
    for (const t of ["Sure", ", I can ", "help with that.", " It is 3.5", " percent. And", " that is it."]) got.push(...c.feed(t));
    expect(got).toEqual(["Sure, I can help with that.", "It is 3.5 percent."]);
    expect(c.flush()).toEqual(["And that is it."]);
  });

  it("never reads code aloud", () => {
    const c = makeChunker();
    const a = c.feed("Here is the fix.\n```ts\nconst x = 1.\nconst y = 2\n");
    expect(a).toEqual(["Here is the fix."]);
    const b = c.feed("```\nThat should do it. ");
    expect(b.join(" ")).toContain("I've put the code on screen");
    expect(b.join(" ")).not.toContain("const");
  });

  it("stops after the cap and says the rest is on screen", () => {
    const c = makeChunker(60);
    const out: string[] = [];
    for (let i = 0; i < 8; i++) out.push(...c.feed("This is a fairly long sentence of filler words. "));
    out.push(...c.flush());
    expect(out[out.length - 1]).toBe("The rest is on screen.");
  });
});
