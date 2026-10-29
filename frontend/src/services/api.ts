// Thin client for the local NEXUS backend. All requests go to the same origin (or the Vite proxy in dev).

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const HEADERS = { "X-Nexus-Client": "ui" };

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  get: <T>(path: string) => fetch(path, { headers: HEADERS }).then((r) => handle<T>(r)),
  send: <T>(method: string, path: string, body?: unknown) =>
    fetch(path, {
      method,
      headers: { ...HEADERS, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }).then((r) => handle<T>(r)),
  post: <T>(path: string, body?: unknown) => api.send<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: unknown) => api.send<T>("PATCH", path, body),
  put: <T>(path: string, body: unknown) => api.send<T>("PUT", path, body),
  del: <T>(path: string) => api.send<T>("DELETE", path),
  upload: <T>(path: string, blob: Blob) =>
    fetch(path, { method: "POST", headers: { ...HEADERS, "Content-Type": blob.type || "application/octet-stream" }, body: blob }).then(
      (r) => handle<T>(r),
    ),
};

export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

/** Parse a text/event-stream body incrementally. Exported for tests. */
export function createSseParser(onEvent: (e: SseEvent) => void) {
  let buffer = "";
  return (chunk: string) => {
    buffer += chunk.replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) {
        try {
          onEvent({ event, data: JSON.parse(data.join("\n")) });
        } catch {
          onEvent({ event, data: { raw: data.join("\n") } });
        }
      }
    }
  };
}

/** POST that returns a server-sent event stream (chat, model pulls). */
export async function postStream(
  path: string,
  body: unknown,
  onEvent: (e: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(path, {
    method: "POST",
    headers: { ...HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) await handle(res);
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  const feed = createSseParser(onEvent);
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    feed(decoder.decode(value, { stream: true }));
  }
}

export function formatBytes(n?: number | null): string {
  if (!n && n !== 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function timeAgo(iso: string): string {
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return d.toLocaleDateString();
}
