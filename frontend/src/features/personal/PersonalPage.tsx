import { Bell, Pin, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, Input, PageHeader, Select, Textarea, Toggle, cn } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { AppsResponse, Note, Reminder, Todo } from "../../types";

function localTime(s: string) {
  return new Date(s).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
}

export function PersonalPage() {
  const agenda = useAsync(() => api.get<{ text: string }>("/api/agenda"), []);
  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <PageHeader
        title="Notes & reminders"
        subtitle="Stored only on this computer. You can also just say “remind me to … in 10 minutes” in chat."
        actions={"Notification" in window && Notification.permission === "default" ? (
          <Button size="sm" onClick={() => void Notification.requestPermission()}><Bell className="h-3.5 w-3.5" /> Enable desktop notifications</Button>
        ) : undefined}
      />
      <Card title="Today">
        <pre className="whitespace-pre-wrap font-sans text-sm text-slate-300">{agenda.data?.text ?? "…"}</pre>
      </Card>
      <div className="grid gap-5 lg:grid-cols-2">
        <Reminders onChange={agenda.reload} />
        <Todos onChange={agenda.reload} />
      </div>
      <Notes />
    </div>
  );
}

const REPEAT_LABEL: Record<string, string> = { daily: "every day", weekdays: "every weekday", weekly: "every week", monthly: "every month" };

function Reminders({ onChange }: { onChange: () => void }) {
  const { toast, snoozeAlarm } = useStore();
  const list = useAsync(() => api.get<Reminder[]>("/api/reminders"), []);
  const apps = useAsync(() => api.get<AppsResponse>("/api/apps"), []);
  const [quick, setQuick] = useState("");
  const [openApp, setOpenApp] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("timer");
  const [minutes, setMinutes] = useState("10");
  const [at, setAt] = useState("");
  const [repeat, setRepeat] = useState("");
  const [persistent, setPersistent] = useState(true);
  useEvents((e) => (e.type === "reminder" || e.type === "reminder_action") && void list.reload());

  const done = async () => {
    await list.reload();
    onChange();
  };
  const addQuick = async () => {
    try {
      await api.post("/api/reminders/natural", { text: quick, open_app: openApp || undefined });
      setQuick("");
      await done();
    } catch (e) {
      toast({ title: "Reminder", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const add = async () => {
    const body: Record<string, unknown> = { text: text || (kind === "timer" ? `Timer (${minutes} min)` : kind === "alarm" ? "Alarm" : "Reminder"), kind, repeat, persistent, open_app: openApp || undefined };
    if (kind === "timer") body.in_seconds = Math.round(Number(minutes) * 60);
    else body.due_at = at;
    try {
      await api.post("/api/reminders", body);
      setText("");
      await done();
    } catch (e) {
      toast({ title: "Reminder", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const appOptions = [{ value: "", label: "Don't open an app" }, ...(apps.data?.integrated ?? []).filter((a) => a.enabled).map((a) => ({ value: a.name, label: `Open ${a.name}` }))];

  return (
    <Card title="Reminders, alarms & timers">
      <div className="flex flex-wrap gap-2">
        <Input className="min-w-60 flex-1" value={quick} onChange={(e) => setQuick(e.target.value)} onKeyDown={(e) => e.key === "Enter" && quick.trim() && void addQuick()}
          placeholder="Say it naturally: call mum tomorrow at 5pm · stand up every weekday at 9" aria-label="Quick reminder" />
        <div className="w-44"><Select value={openApp} onChange={setOpenApp} ariaLabel="Open an app when it rings" options={appOptions} /></div>
        <Button variant="primary" onClick={addQuick} disabled={quick.trim().length < 3}><Plus className="h-4 w-4" /></Button>
      </div>
      <button className="mt-2 text-xs text-accent-soft hover:underline" onClick={() => setAdvanced((v) => !v)}>{advanced ? "Hide options" : "More options (timer, alarm, repeat…)"}</button>
      {advanced && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-2xl bg-ink-800/50 p-3">
          <div className="w-28"><Select value={kind} onChange={setKind} ariaLabel="Type" options={[{ value: "timer", label: "Timer" }, { value: "reminder", label: "Reminder" }, { value: "alarm", label: "Alarm" }]} /></div>
          <Input className="min-w-40 flex-1" value={text} onChange={(e) => setText(e.target.value)} placeholder={kind === "timer" ? "Label (optional)" : "What to remember"} aria-label="Text" />
          {kind === "timer" ? (
            <Input className="w-24" type="number" min="0.1" step="0.5" value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="Minutes" />
          ) : (
            <>
              <Input className="w-52" type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} aria-label="When" />
              <div className="w-36"><Select value={repeat} onChange={setRepeat} ariaLabel="Repeat" options={[{ value: "", label: "Once" }, { value: "daily", label: "Every day" }, { value: "weekdays", label: "Weekdays" }, { value: "weekly", label: "Every week" }, { value: "monthly", label: "Every month" }]} /></div>
            </>
          )}
          <Toggle checked={persistent} onChange={setPersistent} label="Keep ringing until I answer" />
          <Button variant="primary" onClick={add} disabled={kind === "timer" ? !(Number(minutes) > 0) : !at || (kind === "reminder" && !text)}><Plus className="h-4 w-4" /> Add</Button>
        </div>
      )}
      <ul className="mt-3 divide-y divide-ink-700">
        {list.data?.length === 0 && <li className="py-3 text-xs text-slate-500">Nothing scheduled. Try saying “remind me to stretch in 30 minutes”.</li>}
        {list.data?.map((r) => {
          const rep = r.repeat || (r.repeat_daily ? "daily" : "");
          return (
            <li key={r.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
              <Badge tone={r.status === "fired" ? "amber" : "gray"}>{r.kind}</Badge>
              <span className={cn("min-w-0 flex-1", r.status === "fired" && "text-amber-200")}>{r.text}</span>
              <span className="text-xs text-slate-500">
                {r.status === "fired" ? "ringing" : localTime(r.due_at)}{rep ? ` · ${REPEAT_LABEL[rep] ?? rep}` : ""}{r.action ? ` · opens ${r.action.name}` : ""}
              </span>
              {r.status === "fired" && <Button size="sm" variant="subtle" onClick={async () => { await snoozeAlarm(r.id, 10); await done(); }}>Snooze 10</Button>}
              <Button size="sm" variant="ghost" onClick={async () => { await api.post(`/api/reminders/${r.id}/dismiss`); await done(); }}>
                {r.status === "fired" ? "Dismiss" : "Cancel"}
              </Button>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function Todos({ onChange }: { onChange: () => void }) {
  const list = useAsync(() => api.get<Todo[]>("/api/todos"), []);
  const [text, setText] = useState("");
  const add = async () => {
    if (!text.trim()) return;
    await api.post("/api/todos", { text });
    setText("");
    await list.reload();
    onChange();
  };
  return (
    <Card title="To-do list">
      <div className="flex gap-2">
        <Input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void add()} placeholder="Add a task…" aria-label="New to-do" />
        <Button variant="primary" onClick={add} disabled={!text.trim()}><Plus className="h-4 w-4" /></Button>
      </div>
      <ul className="mt-3 space-y-1">
        {list.data?.length === 0 && <li className="py-2 text-xs text-slate-500">All clear.</li>}
        {list.data?.map((t) => (
          <li key={t.id} className="group flex items-center gap-2 text-sm">
            <input type="checkbox" checked={t.done} aria-label={t.text} onChange={async () => { await api.patch(`/api/todos/${t.id}`, { done: !t.done }); void list.reload(); onChange(); }} />
            <span className={cn("flex-1", t.done && "text-slate-500 line-through")}>{t.text}</span>
            <button aria-label={`Delete ${t.text}`} className="text-slate-600 opacity-0 hover:text-rose-300 group-hover:opacity-100" onClick={async () => { await api.del(`/api/todos/${t.id}`); void list.reload(); }}>
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Notes() {
  const [q, setQ] = useState("");
  const list = useAsync(() => api.get<Note[]>(`/api/notes${q ? `?q=${encodeURIComponent(q)}` : ""}`), [q]);
  const [editing, setEditing] = useState<Partial<Note> | null>(null);

  const save = async () => {
    if (!editing) return;
    const body = { title: editing.title ?? "", body: editing.body ?? "", pinned: editing.pinned ?? false };
    if (editing.id) await api.put(`/api/notes/${editing.id}`, body);
    else await api.post("/api/notes", body);
    setEditing(null);
    await list.reload();
  };

  return (
    <Card title="Notes" actions={<Button size="sm" onClick={() => setEditing({ title: "", body: "" })}><Plus className="h-3.5 w-3.5" /> New note</Button>}>
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search notes" aria-label="Search notes" />
      {editing && (
        <div className="mt-3 space-y-2 rounded-lg border border-accent/40 p-3">
          <Input value={editing.title ?? ""} onChange={(e) => setEditing({ ...editing, title: e.target.value })} placeholder="Title" aria-label="Note title" />
          <Textarea rows={6} value={editing.body ?? ""} onChange={(e) => setEditing({ ...editing, body: e.target.value })} placeholder="Write…" aria-label="Note body" />
          <div className="flex gap-2">
            <Button variant="primary" size="sm" onClick={save}>Save</Button>
            <Button variant="ghost" size="sm" onClick={() => setEditing(null)}>Cancel</Button>
            {editing.id && (
              <Button variant="danger" size="sm" className="ml-auto" onClick={async () => { await api.del(`/api/notes/${editing.id}`); setEditing(null); void list.reload(); }}>Delete</Button>
            )}
          </div>
        </div>
      )}
      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {list.data?.map((n) => (
          <button key={n.id} onClick={() => setEditing(n)} className="rounded-lg border border-ink-700 bg-ink-850 p-3 text-left hover:border-accent/50">
            <p className="flex items-center gap-1 truncate text-sm font-medium text-fg">{n.pinned && <Pin className="h-3 w-3" />} {n.title || "Untitled"}</p>
            <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-xs text-slate-400">{n.body}</p>
          </button>
        ))}
      </div>
      {list.data?.length === 0 && !editing && <Empty title="No notes yet">Try saying “take a note: …” in chat.</Empty>}
    </Card>
  );
}
