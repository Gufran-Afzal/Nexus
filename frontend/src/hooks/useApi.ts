import { useCallback, useEffect, useRef, useState } from "react";
import type { BusEvent } from "../types";

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, reload, setData };
}

type Listener = (e: BusEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;

function ensureSource() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource("/api/events");
  source.addEventListener("update", (msg) => {
    try {
      const ev = JSON.parse((msg as MessageEvent).data) as BusEvent;
      listeners.forEach((l) => l(ev));
    } catch {
      /* ignore malformed */
    }
  });
  source.onerror = () => {
    // EventSource reconnects automatically; notify so the UI can show a disconnected state.
    listeners.forEach((l) => l({ type: "connection", ts: Date.now() / 1000, ok: false }));
  };
  source.onopen = () => listeners.forEach((l) => l({ type: "connection", ts: Date.now() / 1000, ok: true }));
}

/** Subscribe to live backend events (voice state, reminders, commands, tasks...). */
export function useEvents(handler: Listener) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    ensureSource();
    const l: Listener = (e) => ref.current(e);
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);
}

export function usePolling(fn: () => void, ms: number, active = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => ref.current(), ms);
    return () => clearInterval(id);
  }, [ms, active]);
}
