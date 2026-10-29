/**
 * Cuts a streaming reply into sentences so the browser can ask NEXUS to speak the first sentence while the model is
 * still writing the rest. Code blocks are never read aloud. Mirrors SentenceChunker in the backend.
 */
export interface Chunker {
  feed(text: string): string[];
  flush(): string[];
}

const BOUNDARY = /(?:[.!?…]+["')\]]*(?=\s)|\n+)/g;

export function plainSpeech(t: string): string {
  return t
    .replace(/```[\s\S]*?```/g, " I've put the code on screen. ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s*/gm, "")
    .replace(/[*_~>|]+/g, "")
    .replace(/^\s*[-+]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function makeChunker(cap = 900, minChunk = 8): Chunker {
  let raw = "";
  let done = 0;
  let said = 0;
  let capped = false;

  const release = (piece: string): string | null => {
    if (capped) return null;
    const spoken = plainSpeech(piece);
    if (!spoken) return null;
    if (said + spoken.length > cap) {
      capped = true;
      return "The rest is on screen.";
    }
    said += spoken.length;
    return spoken;
  };

  return {
    feed(text) {
      raw += text;
      const out: string[] = [];
      for (;;) {
        const fence = raw.indexOf("```", done);
        const open = fence !== -1 && ((raw.slice(fence).match(/```/g) ?? []).length % 2 === 1);
        const stableEnd = open ? fence : raw.length;
        const segment = raw.slice(done, stableEnd).replace(/```[\s\S]*?```/g, (m) => "X".repeat(m.length));
        let end = -1;
        for (const m of segment.matchAll(BOUNDARY)) end = (m.index ?? 0) + m[0].length;
        if (end <= 0 || segment.slice(0, end).trim().length < minChunk) return out;
        const piece = raw.slice(done, done + end);
        done += end;
        const r = release(piece);
        if (r) out.push(r);
      }
    },
    flush() {
      let rest = raw.slice(done);
      done = raw.length;
      if (((rest.match(/```/g) ?? []).length % 2) === 1) rest += "\n```";
      const r = rest.trim() ? release(rest) : null;
      return r ? [r] : [];
    },
  };
}
