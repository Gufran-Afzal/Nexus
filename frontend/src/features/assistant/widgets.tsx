import { Bell, BatteryCharging, BatteryFull, Brain, Check, Cpu, Flame, MemoryStick, Plus, StickyNote, Timer, X, Zap } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Button, Ring, cn } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { Note, Reminder, Todo } from "../../types";

/** One glass tile of the dashboard. */
export function Widget({ title, icon, action, children, className }: { title: string; icon: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("card-surface rounded-3xl p-4", className)} aria-label={title}>
      <header className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-[13px] font-semibold text-slate-300">
          <span className="text-accent" aria-hidden>{icon}</span> {title}
        </h2>
        {action}
      </header>
      {children}
    </section>
  );
}

function useNow(everyMs: number): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}

export function ClockWidget() {
  const now = useNow(1000);
  return (
    <section className="card-surface rounded-3xl p-5" aria-label="Clock">
      <p className="font-display text-[56px] font-semibold leading-none tracking-[-0.04em] text-fg tabular">
        {now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
      </p>
      <p className="mt-1.5 text-[15px] font-medium text-accent-soft">{now.toLocaleDateString([], { weekday: "long" })}</p>
      <p className="text-sm text-slate-400">{now.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" })}</p>
    </section>
  );
}

/** Total length of a timer, read from its label ("Timer (10 min)", "Focus session (25 min)"). */
export function timerSeconds(text: string): number | null {
  const m = /\(([^)]*)\)\s*$/.exec(text);
  if (!m) return null;
  let total = 0;
  for (const [, n, u] of m[1].matchAll(/(\d+)\s*(h|min|s)\b/g)) total += Number(n) * (u === "h" ? 3600 : u === "min" ? 60 : 1);
  return total || null;
}

export function formatRemaining(secs: number): string {
  const s = Math.max(0, Math.round(secs));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`;
}

export function TimersWidget({ reminders, reload, onStart }: { reminders: Reminder[]; reload: () => void; onStart: (phrase: string) => void }) {
  const now = useNow(1000);
  const list = (Array.isArray(reminders) ? reminders : []).filter((r) => r.status === "pending" || r.status === "fired").slice(0, 4);
  const dismiss = async (id: number) => {
    await api.post(`/api/reminders/${id}/dismiss`);
    reload();
  };
  return (
    <Widget
      title="Timers & reminders"
      icon={<Timer className="h-4 w-4" />}
      action={<button onClick={() => onStart("start a focus session")} className="flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-1 text-[11px] font-medium text-accent-soft hover:bg-accent/25"><Flame className="h-3 w-3" aria-hidden /> Focus 25</button>}
    >
      {list.length === 0 ? (
        <p className="py-3 text-sm text-slate-500">Nothing scheduled. Try “timer for 10 minutes”.</p>
      ) : (
        <ul className="space-y-2.5">
          {list.map((r) => {
            const due = new Date(r.due_at).getTime();
            const left = (due - now.getTime()) / 1000;
            const total = r.kind === "timer" ? timerSeconds(r.text) : null;
            return (
              <li key={r.id} className="flex items-center gap-3">
                {r.kind === "timer" && total ? (
                  <Ring size={44} stroke={5} value={left / total} color={left < 60 ? "rgb(var(--orange))" : "rgb(var(--accent))"}>
                    <Timer className="h-3.5 w-3.5 text-slate-400" aria-hidden />
                  </Ring>
                ) : (
                  <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-ink-800 text-slate-400"><Bell className="h-4 w-4" aria-hidden /></span>
                )}
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-100">{r.text}</p>
                  <p className="text-xs tabular text-slate-400">
                    {r.status === "fired" ? "Ready" : r.kind === "timer" ? `${formatRemaining(left)} left` : new Date(r.due_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}
                  </p>
                </div>
                <button onClick={() => void dismiss(r.id)} aria-label={`Dismiss ${r.text}`} className="rounded-full p-1.5 text-slate-500 hover:bg-ink-700 hover:text-fg"><X className="h-4 w-4" /></button>
              </li>
            );
          })}
        </ul>
      )}
    </Widget>
  );
}

export function TodoWidget() {
  const list = useAsync(() => api.get<Todo[]>("/api/todos"), []);
  const [text, setText] = useState("");
  useEvents((e) => { if (e.type === "voice" || e.type === "reminder") void list.reload(); });
  const open = arr(list.data).filter((t) => !t.done);
  const toggle = async (t: Todo) => {
    await api.patch(`/api/todos/${t.id}`, { done: !t.done });
    void list.reload();
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    await api.post("/api/todos", { text: text.trim() });
    setText("");
    void list.reload();
  };
  return (
    <Widget title="To-do" icon={<Check className="h-4 w-4" />} action={<span className="text-xs tabular text-slate-500">{open.length} open</span>}>
      <ul className="space-y-1">
        {open.slice(0, 5).map((t) => (
          <li key={t.id}>
            <button onClick={() => void toggle(t)} className="group flex w-full items-center gap-3 rounded-xl px-1 py-1.5 text-left hover:bg-ink-800/60" aria-label={`Complete ${t.text}`}>
              <span className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full border-[1.5px] border-slate-500 group-hover:border-accent">
                <Check className="h-3 w-3 text-accent opacity-0 group-hover:opacity-100" aria-hidden />
              </span>
              <span className="min-w-0 flex-1 truncate text-sm text-slate-100">{t.text}</span>
            </button>
          </li>
        ))}
        {open.length === 0 && <li className="py-2 text-sm text-slate-500">All clear.</li>}
      </ul>
      <form onSubmit={add} className="mt-2 flex items-center gap-2 rounded-xl bg-ink-800/60 px-2.5">
        <Plus className="h-4 w-4 text-slate-500" aria-hidden />
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Add a to-do" aria-label="Add a to-do"
          className="h-8 min-w-0 flex-1 bg-transparent text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none" />
      </form>
    </Widget>
  );
}

interface Stats {
  cpu_percent: number;
  memory: { percent: number; used_gb: number; total_gb: number };
  battery: { percent: number; charging: boolean } | null;
  model: { name: string; ollama: boolean; loaded: boolean; tokens_per_s: number | null; last_load_s: number | null };
}

function useStats(everyMs = 5000) {
  const stats = useAsync(() => api.get<Stats>("/api/system/stats"), []);
  const { reload } = stats;
  useEffect(() => {
    const tick = () => { if (document.visibilityState === "visible") void reload(); };
    const id = setInterval(tick, everyMs);
    return () => clearInterval(id);
  }, [reload, everyMs]);
  // Tolerate an unexpected reply (older backend, error object) instead of crashing the dashboard.
  const data = stats.data && typeof stats.data === "object" && "memory" in stats.data ? stats.data : undefined;
  return { ...stats, data };
}

const arr = <T,>(v: T[] | undefined): T[] => (Array.isArray(v) ? v : []);

function Gauge({ label, value, display, color, icon }: { label: string; value: number; display: string; color: string; icon: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-1.5">
      <Ring size={62} stroke={7} value={value} color={color}>
        <span className="text-slate-300">{icon}</span>
      </Ring>
      <p className="text-sm font-semibold tabular text-fg">{display}</p>
      <p className="text-[11px] text-slate-500">{label}</p>
    </div>
  );
}

export function SystemWidget() {
  const { data } = useStats();
  const cpu = data?.cpu_percent ?? 0;
  const mem = data?.memory.percent ?? 0;
  const bat = data?.battery;
  return (
    <Widget title="This computer" icon={<Cpu className="h-4 w-4" />}>
      <div className="grid grid-cols-3 gap-2">
        <Gauge label="Processor" value={cpu / 100} display={data ? `${Math.round(cpu)}%` : "–"} icon={<Cpu className="h-4 w-4" />}
          color={cpu > 85 ? "rgb(var(--red))" : "rgb(var(--green))"} />
        <Gauge label="Memory" value={mem / 100} display={data ? `${Math.round(mem)}%` : "–"} icon={<MemoryStick className="h-4 w-4" />}
          color={mem > 90 ? "rgb(var(--red))" : mem > 75 ? "rgb(var(--orange))" : "rgb(var(--accent))"} />
        {bat ? (
          <Gauge label={bat.charging ? "Charging" : "Battery"} value={bat.percent / 100} display={`${Math.round(bat.percent)}%`}
            icon={bat.charging ? <BatteryCharging className="h-4 w-4" /> : <BatteryFull className="h-4 w-4" />}
            color={bat.percent < 20 && !bat.charging ? "rgb(var(--red))" : "rgb(var(--green))"} />
        ) : (
          <Gauge label="Power" value={1} display="AC" icon={<Zap className="h-4 w-4" />} color="rgb(var(--purple))" />
        )}
      </div>
    </Widget>
  );
}

export function ModelWidget({ onWarm }: { onWarm: () => Promise<void> }) {
  const { data, reload } = useStats(4000);
  const [warming, setWarming] = useState(false);
  const m = data?.model;
  const warm = useCallback(async () => {
    setWarming(true);
    try {
      await onWarm();
    } finally {
      setWarming(false);
      void reload();
    }
  }, [onWarm, reload]);
  const state = !m ? "Checking…" : !m.ollama ? "Ollama is offline" : m.loaded ? "Loaded and ready" : "Sleeping — loads on first use";
  return (
    <Widget title="Intelligence" icon={<Brain className="h-4 w-4" />}>
      <div className="flex items-center gap-3">
        <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", !m?.ollama ? "bg-ios-red" : m.loaded ? "bg-ios-green" : "bg-ios-orange")} aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-slate-100">{m?.name ?? "—"}</p>
          <p className="text-xs text-slate-400">{state}</p>
        </div>
        {m?.ollama && !m.loaded && <Button size="sm" variant="subtle" loading={warming} onClick={() => void warm()}>Wake up</Button>}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-2 text-center">
        <div className="rounded-2xl bg-ink-800/60 px-2 py-2">
          <dd className="font-display text-xl font-semibold tabular text-fg">{m?.tokens_per_s ? m.tokens_per_s.toFixed(1) : "–"}</dd>
          <dt className="text-[11px] text-slate-500">words per second</dt>
        </div>
        <div className="rounded-2xl bg-ink-800/60 px-2 py-2">
          <dd className="font-display text-xl font-semibold tabular text-fg">{m?.last_load_s != null ? `${m.last_load_s}s` : "–"}</dd>
          <dt className="text-[11px] text-slate-500">last load time</dt>
        </div>
      </dl>
    </Widget>
  );
}

export function NotesWidget() {
  const notes = useAsync(() => api.get<Note[]>("/api/notes"), []);
  const [text, setText] = useState("");
  useEvents((e) => { if (e.type === "voice") void notes.reload(); });
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const body = text.trim();
    if (!body) return;
    await api.post("/api/notes", { title: body.slice(0, 60), body });
    setText("");
    void notes.reload();
  };
  return (
    <Widget title="Quick notes" icon={<StickyNote className="h-4 w-4" />}>
      <form onSubmit={add} className="flex items-center gap-2 rounded-xl bg-ink-800/60 px-2.5">
        <Plus className="h-4 w-4 text-slate-500" aria-hidden />
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Jot something down" aria-label="Jot a note"
          className="h-8 min-w-0 flex-1 bg-transparent text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none" />
      </form>
      <ul className="mt-2 space-y-1.5">
        {arr(notes.data).slice(0, 3).map((n) => (
          <li key={n.id} className="rounded-xl bg-ios-orange/10 px-3 py-2 text-[13px] leading-snug text-slate-200">
            <span className="line-clamp-2">{n.body || n.title}</span>
          </li>
        ))}
        {arr(notes.data).length === 0 && <li className="py-1 text-sm text-slate-500">No notes yet.</li>}
      </ul>
    </Widget>
  );
}
