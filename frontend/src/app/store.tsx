import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useEvents } from "../hooks/useApi";
import { api } from "../services/api";
import type { AlarmEvent, Conversation, Health, PendingInfo, Reminder, Workspace } from "../types";

export interface Toast {
  id: number;
  title: string;
  body?: string;
  tone?: "info" | "error" | "success";
}

interface Store {
  health: Health | null;
  healthError: string | null;
  refreshHealth: () => Promise<void>;
  conversations: Conversation[];
  refreshConversations: () => Promise<void>;
  workspaces: Workspace[];
  refreshWorkspaces: () => Promise<void>;
  route: string[];
  navigate: (path: string) => void;
  toasts: Toast[];
  toast: (t: Omit<Toast, "id">) => void;
  dismissToast: (id: number) => void;
  backendConnected: boolean;
  voiceState: string;
  alarms: AlarmEvent[];
  snoozeAlarm: (id: number, minutes: number) => Promise<void>;
  dismissAlarm: (id: number) => Promise<void>;
  pending: PendingInfo | null;
  answerPending: (yes: boolean) => Promise<void>;
  caption: string;
}

const Ctx = createContext<Store | null>(null);

function parseRoute(): string[] {
  const h = window.location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").filter(Boolean);
  return parts.length ? parts : ["assistant"];
}

let toastSeq = 1;

export function StoreProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [route, setRoute] = useState<string[]>(parseRoute);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [backendConnected, setConnected] = useState(true);
  const [voiceState, setVoiceState] = useState("disabled");
  const [alarms, setAlarms] = useState<AlarmEvent[]>([]);
  const [pending, setPending] = useState<PendingInfo | null>(null);
  const [caption, setCaption] = useState("");

  const refreshHealth = useCallback(async () => {
    try {
      const h = await api.get<Health>("/api/health");
      setHealth(h);
      setVoiceState(h.voice.muted ? "muted" : h.voice.state);
      setHealthError(null);
      setConnected(true);
    } catch (e) {
      setHealthError(e instanceof Error ? e.message : String(e));
      setConnected(false);
    }
  }, []);
  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.get<Conversation[]>("/api/conversations"));
    } catch {
      /* surfaced via health */
    }
  }, []);
  const refreshWorkspaces = useCallback(async () => {
    try {
      setWorkspaces(await api.get<Workspace[]>("/api/workspaces"));
    } catch {
      /* surfaced via health */
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    void refreshConversations();
    void refreshWorkspaces();
    const id = setInterval(refreshHealth, 15000);
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    return () => {
      clearInterval(id);
      window.removeEventListener("hashchange", onHash);
    };
  }, [refreshHealth, refreshConversations, refreshWorkspaces]);

  const toast = useCallback((t: Omit<Toast, "id">) => {
    const id = toastSeq++;
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), t.tone === "error" ? 9000 : 6000);
  }, []);
  const dismissToast = useCallback((id: number) => setToasts((ts) => ts.filter((x) => x.id !== id)), []);

  useEvents((ev) => {
    if (ev.type === "connection") {
      setConnected(Boolean(ev.ok));
      if (ev.ok) void refreshHealth();
    } else if (ev.type === "reminder") {
      const a = ev as unknown as AlarmEvent;
      // A ringing reminder stays on screen (with Snooze / Done) until it is answered; each re-ring updates the same card.
      setAlarms((list) => [...list.filter((x) => x.id !== a.id), a]);
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("NEXUS", { body: String(ev.text ?? "") });
      }
    } else if (ev.type === "reminder_action") {
      toast({ title: ev.ok ? "Reminder action" : "Reminder action failed", body: String(ev.message ?? ""), tone: ev.ok ? "success" : "error" });
    } else if (ev.type === "ollama" || (ev.type === "system" && (ev.event === "resumed" || ev.event === "paused" || ev.event === "resumed_by_user"))) {
      void refreshHealth();
    } else if (ev.type === "pending") {
      setPending((ev.pending as PendingInfo | null) ?? null);
    } else if (ev.type === "caption") {
      setCaption(String(ev.text ?? ""));
    } else if (ev.type === "voice" && typeof ev.state === "string" && !["transcribed", "answered", "wake_detected", "follow_up_ended"].includes(ev.state)) {
      setVoiceState(ev.muted ? "muted" : ev.state);
      if (ev.state !== "recording") setCaption("");
    }
  });

  // A message or email is waiting for "send it": show it, in case the page was opened after it was asked.
  useEffect(() => {
    api.get<{ pending: PendingInfo | null }>("/api/pending").then((r) => setPending(r.pending)).catch(() => undefined);
  }, []);
  const answerPending = useCallback(async (yes: boolean) => {
    setPending(null);
    try {
      const r = await api.post<{ message: string; status: string }>(yes ? "/api/pending/confirm" : "/api/pending/cancel");
      toast({ title: yes ? "NEXUS" : "Cancelled", body: r.message, tone: r.status === "failed" ? "error" : "info" });
    } catch (e) {
      toast({ title: "Couldn't do that", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  }, [toast]);

  const removeAlarm = useCallback((id: number) => setAlarms((l) => l.filter((a) => a.id !== id)), []);
  const snoozeAlarm = useCallback(async (id: number, minutes: number) => {
    removeAlarm(id);
    try {
      await api.post(`/api/reminders/${id}/snooze`, { minutes });
      toast({ title: "Snoozed", body: `I'll remind you again in ${minutes} min.`, tone: "success" });
    } catch (e) {
      toast({ title: "Couldn't snooze", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  }, [removeAlarm, toast]);
  const dismissAlarm = useCallback(async (id: number) => {
    removeAlarm(id);
    try {
      await api.post(`/api/reminders/${id}/dismiss`);
    } catch {
      /* already dismissed (e.g. by voice) */
    }
  }, [removeAlarm]);

  // Something answered it elsewhere (voice: "stop the alarm"): drop cards whose reminder is no longer ringing.
  useEffect(() => {
    if (alarms.length === 0) return;
    const id = setInterval(async () => {
      try {
        const rows = await api.get<Reminder[]>("/api/reminders");
        const ringing = new Set(rows.filter((r) => r.status === "fired").map((r) => r.id));
        setAlarms((l) => (l.some((a) => !ringing.has(a.id)) ? l.filter((a) => ringing.has(a.id)) : l));
      } catch {
        /* keep showing it */
      }
    }, 4000);
    return () => clearInterval(id);
  }, [alarms.length]);

  const navigate = useCallback((path: string) => {
    window.location.hash = path.startsWith("/") ? path : `/${path}`;
  }, []);

  const value = useMemo(
    () => ({
      health, healthError, refreshHealth, conversations, refreshConversations, workspaces, refreshWorkspaces,
      route, navigate, toasts, toast, dismissToast, backendConnected, voiceState, alarms, snoozeAlarm, dismissAlarm, pending, answerPending, caption,
    }),
    [health, healthError, refreshHealth, conversations, refreshConversations, workspaces, refreshWorkspaces, route, navigate, toasts, toast, dismissToast, backendConnected, voiceState, alarms, snoozeAlarm, dismissAlarm, pending, answerPending, caption],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("useStore outside StoreProvider");
  return s;
}
