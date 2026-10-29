import { CheckCircle2, Circle, CircleDashed, ListChecks, Loader2, PauseCircle, XCircle } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Markdown } from "../../components/Markdown";
import { Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, Select, Spinner, StatusBadge, Textarea } from "../../components/ui";
import { useAsync, useEvents, usePolling } from "../../hooks/useApi";
import { api, timeAgo } from "../../services/api";
import type { AgentTask, TaskStep } from "../../types";

interface Template { id: string; title: string; needs_workspace: boolean; steps: string[] }

export function TasksPage({ id }: { id?: number }) {
  const tasks = useAsync(() => api.get<AgentTask[]>("/api/tasks"), []);
  useEvents((e) => e.type === "task" && void tasks.reload());
  const { navigate } = useStore();
  if (id) return <TaskDetail id={id} />;
  return (
    <div className="mx-auto max-w-5xl p-6">
      <PageHeader title="Tasks" subtitle="Bounded, step-by-step workflows. They pause for your approval before any edit or command." />
      <NewTask onCreated={(t) => navigate(`tasks/${t.id}`)} />
      <div className="mt-5 space-y-2">
        {tasks.loading && !tasks.data && <Spinner />}
        {tasks.data?.length === 0 && <Empty icon={<ListChecks className="h-8 w-8" />} title="No tasks yet" />}
        {tasks.data?.map((t) => (
          <button key={t.id} onClick={() => navigate(`tasks/${t.id}`)} className="flex w-full items-center gap-3 rounded-xl border border-ink-700 bg-ink-900 px-4 py-3 text-left hover:border-accent/50">
            <span className="text-xs text-slate-500">#{t.id}</span>
            <span className="min-w-0 flex-1 truncate text-sm text-fg">{t.title}</span>
            <span className="text-xs text-slate-500">{t.steps.filter((s) => s.status === "succeeded").length}/{t.steps.length} steps</span>
            <StatusBadge status={t.status} />
            <span className="w-20 text-right text-[11px] text-slate-500">{timeAgo(t.created_at)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function NewTask({ onCreated }: { onCreated: (t: AgentTask) => void }) {
  const { workspaces } = useStore();
  const templates = useAsync(() => api.get<Template[]>("/api/tasks/templates"), []);
  const [template, setTemplate] = useState("implement_change");
  const [wsId, setWsId] = useState(workspaces[0] ? String(workspaces[0].id) : "");
  const [goal, setGoal] = useState("");
  const [files, setFiles] = useState("");
  const [testCmd, setTestCmd] = useState("");
  const [errorText, setErrorText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const tpl = templates.data?.find((t) => t.id === template);

  const create = async () => {
    setBusy(true);
    setErr(null);
    try {
      const t = await api.post<AgentTask>("/api/tasks", {
        template, goal, workspace_id: wsId ? Number(wsId) : null,
        files: files.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
        test_command: testCmd.trim() || null, error_text: errorText.trim() || null,
      });
      onCreated(t);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Start a task">
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Workflow">
          <Select value={template} onChange={setTemplate} ariaLabel="Workflow" options={(templates.data ?? []).map((t) => ({ value: t.id, label: t.title }))} />
        </Field>
        <Field label="Project">
          <Select value={wsId} onChange={setWsId} ariaLabel="Project" options={[{ value: "", label: "Select…" }, ...workspaces.map((w) => ({ value: String(w.id), label: w.name }))]} />
        </Field>
        <div className="md:col-span-2">
          <Field label="Goal"><Textarea rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="e.g. Fix the add() function so it adds instead of subtracting" /></Field>
        </div>
        <Field label="Files to focus on / edit" hint={template === "implement_change" ? "Required: up to 3 files will be edited." : "Optional, comma or newline separated."}>
          <Input value={files} onChange={(e) => setFiles(e.target.value)} placeholder="src/calc.py" className="font-mono" />
        </Field>
        <Field label="Verification command" hint="Optional. You'll approve it before it runs.">
          <Input value={testCmd} onChange={(e) => setTestCmd(e.target.value)} placeholder="pytest -q" className="font-mono" />
        </Field>
        {template === "investigate_bug" && (
          <div className="md:col-span-2">
            <Field label="Error message / stack trace"><Textarea rows={4} value={errorText} onChange={(e) => setErrorText(e.target.value)} className="font-mono text-xs" /></Field>
          </div>
        )}
      </div>
      {tpl && <p className="mt-3 text-[11px] text-slate-500">Steps: {tpl.steps.join(" → ")}</p>}
      <div className="mt-3 flex items-center gap-3">
        <Button variant="primary" onClick={create} loading={busy} disabled={goal.trim().length < 3 || (tpl?.needs_workspace && !wsId)}>Start task</Button>
        {err && <ErrorBanner message={err} />}
      </div>
    </Card>
  );
}

function StepIcon({ s }: { s: string }) {
  if (s === "succeeded") return <CheckCircle2 className="h-4 w-4 text-emerald-400" />;
  if (s === "failed") return <XCircle className="h-4 w-4 text-rose-400" />;
  if (s === "running") return <Loader2 className="h-4 w-4 animate-spin text-sky-400" />;
  if (s === "waiting_approval") return <PauseCircle className="h-4 w-4 text-amber-400" />;
  if (s === "skipped" || s === "cancelled") return <CircleDashed className="h-4 w-4 text-slate-500" />;
  return <Circle className="h-4 w-4 text-slate-600" />;
}

function TaskDetail({ id }: { id: number }) {
  const { navigate } = useStore();
  const task = useAsync(() => api.get<AgentTask>(`/api/tasks/${id}`), [id]);
  const live = task.data && ["pending", "running", "waiting_approval"].includes(task.data.status);
  useEvents((e) => e.type === "task" && e.id === id && void task.reload());
  usePolling(task.reload, 2500, Boolean(live));
  if (task.loading && !task.data) return <div className="p-8"><Spinner /></div>;
  if (task.error) return <div className="p-6"><ErrorBanner message={task.error} /></div>;
  const t = task.data!;
  return (
    <div className="mx-auto max-w-4xl p-6">
      <button onClick={() => navigate("tasks")} className="mb-2 text-xs text-slate-400 hover:text-fg">← All tasks</button>
      <PageHeader
        title={`#${t.id} ${t.title}`}
        subtitle={t.goal}
        actions={<>
          <StatusBadge status={t.status} />
          {live && <Button size="sm" variant="danger" onClick={async () => { await api.post(`/api/tasks/${id}/cancel`); void task.reload(); }}>Cancel</Button>}
        </>}
      />
      <ol className="space-y-3">
        {t.steps.map((s) => <StepView key={s.id} s={s} />)}
      </ol>
      {t.summary && t.status !== "running" && (
        <Card title="Final report" className="mt-5"><Markdown text={t.summary} /></Card>
      )}
    </div>
  );
}

function StepView({ s }: { s: TaskStep }) {
  const { navigate } = useStore();
  const [open, setOpen] = useState(s.status === "waiting_approval" || s.status === "failed");
  return (
    <li className="rounded-xl border border-ink-700 bg-ink-900">
      <button className="flex w-full items-center gap-3 px-4 py-2.5 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <StepIcon s={s.status} />
        <span className="flex-1 text-sm text-slate-100">{s.description}</span>
        {s.attempts > 1 && <span className="text-[11px] text-slate-500">attempt {s.attempts}</span>}
        <StatusBadge status={s.status} />
      </button>
      {s.status === "waiting_approval" && (
        <div className="flex items-center gap-2 border-t border-ink-700 bg-amber-500/5 px-4 py-2 text-xs text-amber-200">
          Waiting for you:
          {s.ref.changeset_id && <Button size="sm" variant="primary" onClick={() => navigate(`changes/${s.ref.changeset_id}`)}>Review change set #{s.ref.changeset_id}</Button>}
          {s.ref.command_id && <Button size="sm" variant="primary" onClick={() => navigate("commands")}>Approve command #{s.ref.command_id}</Button>}
        </div>
      )}
      {open && (s.output || s.log.length > 0) && (
        <div className="border-t border-ink-700 px-4 py-3">
          {s.log.length > 0 && <ul className="mb-2 space-y-0.5 text-[11px] text-rose-300/80">{s.log.map((l, i) => <li key={i}>{l}</li>)}</ul>}
          {s.output && (["analyze", "plan", "review"].includes(s.kind) ? <Markdown text={s.output} /> : <pre className="whitespace-pre-wrap text-xs text-slate-300">{s.output}</pre>)}
        </div>
      )}
    </li>
  );
}
