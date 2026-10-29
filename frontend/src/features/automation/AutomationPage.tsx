import { CheckCircle2, CircleDashed, HelpCircle, MinusCircle, Play, ShieldCheck, Square, Wrench, XCircle } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, ErrorBanner, Field, PageHeader, Select, Spinner, StatusBadge, Toggle } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { CapabilitiesResponse, DiagnoseResponse, RunStep, RunView, WorkflowInfo } from "../../types";

const RISK_TONE: Record<string, string> = { read: "gray", low: "green", medium: "amber", high: "red" };
const STEP_ICON: Record<string, JSX.Element> = {
  succeeded: <CheckCircle2 className="h-4 w-4 text-ios-green" aria-hidden />,
  failed: <XCircle className="h-4 w-4 text-ios-red" aria-hidden />,
  denied: <XCircle className="h-4 w-4 text-ios-red" aria-hidden />,
  skipped: <MinusCircle className="h-4 w-4 text-slate-500" aria-hidden />,
};

/** What the check after a step found. "Not checked" is shown on purpose: success that could not be observed is not shown as confirmed. */
function Verification({ step }: { step: RunStep }) {
  if (step.status !== "succeeded" && step.status !== "failed") return null;
  if (step.verified === true) return <Badge tone="green"><ShieldCheck className="h-3 w-3" />{step.already_done ? "already true" : "checked"}</Badge>;
  if (step.verified === false) return <Badge tone="red">check failed</Badge>;
  return <Badge tone="gray"><HelpCircle className="h-3 w-3" />not checked</Badge>;
}

function StepRow({ step }: { step: RunStep }) {
  const args = Object.entries(step.args).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
  return (
    <li className="py-2" data-testid="run-step">
      <div className="flex items-center gap-2">
        {STEP_ICON[step.status] ?? <CircleDashed className="h-4 w-4 text-slate-500" aria-hidden />}
        <code className="text-xs text-slate-200">{step.tool}</code>
        <StatusBadge status={step.status} />
        <Verification step={step} />
        {step.attempts > 1 && <Badge tone="amber">{step.attempts} attempts</Badge>}
        {step.approved && <Badge tone="blue">you approved</Badge>}
      </div>
      {args && <p className="mt-0.5 truncate pl-6 text-[11px] text-slate-500" title={args}>{args.slice(0, 160)}</p>}
      {step.summary && <p className="mt-0.5 pl-6 text-xs text-slate-300">{step.summary}</p>}
      {step.observed && <p className="pl-6 text-[11px] text-slate-500">saw: {step.observed}</p>}
      {step.recovery && <p className="pl-6 text-[11px] text-amber-300">recovery: {step.recovery}</p>}
      {step.status === "failed" && step.error_class && <p className="pl-6 text-[11px] text-rose-300">failed because: {step.error_class.replace(/_/g, " ")}</p>}
    </li>
  );
}

function RunDetail({ run, reload }: { run: RunView; reload: () => Promise<void> }) {
  const { toast } = useStore();
  const [busy, setBusy] = useState(false);
  const act = async (path: string, body?: unknown) => {
    setBusy(true);
    try {
      await api.post(`/api/runs/${run.id}/${path}`, body);
      await reload();
    } catch (e) {
      toast({ title: "That didn't work", body: e instanceof Error ? e.message : String(e), tone: "error" });
    } finally {
      setBusy(false);
    }
  };
  const p = run.pending;
  const live = !["completed", "partially_completed", "failed", "cancelled"].includes(run.status);
  const unfinished = run.steps.filter((s) => ["skipped", "failed", "denied"].includes(s.status));
  return (
    <div data-testid="run-detail">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-100">{run.goal}</h3>
        <StatusBadge status={run.status} />
        {run.workflow && <Badge tone="violet">workflow: {run.workflow}</Badge>}
      </div>
      {p && p.kind === "approval" && (
        <div className="mt-3 rounded-2xl border border-ios-orange/40 bg-ios-orange/10 p-3" role="alert" data-testid="run-approval">
          <p className="text-sm font-medium text-amber-200">Waiting for your approval</p>
          <p className="mt-1 text-xs text-slate-300">{p.prompt}</p>
          <p className="mt-1 text-[11px] text-slate-500">Nothing has been done for this step yet.</p>
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="primary" loading={busy} onClick={() => void act("approve")}>Approve and run</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act("reject")}>Decline</Button>
          </div>
        </div>
      )}
      {p && p.kind === "external" && (
        <div className="mt-3 rounded-2xl border border-ink-600 p-3 text-xs text-slate-300" data-testid="run-external">
          {p.prompt} <span className="text-slate-500">Review it on the Changes or Commands page, then check again.</span>
          <div className="mt-2"><Button size="sm" variant="outline" loading={busy} onClick={() => void api.get(`/api/runs/${run.id}?refresh=true`).then(reload)}>Check again</Button></div>
        </div>
      )}
      {p && (p.kind === "resume" || p.kind === "input") && (
        <div className="mt-3 rounded-2xl border border-ink-600 p-3" role="alert" data-testid="run-resume">
          <p className="text-sm text-slate-200">{p.prompt}</p>
          <div className="mt-2 flex gap-2">
            {p.kind === "resume" && <Button size="sm" variant="primary" loading={busy} onClick={() => void act("resume", { redo: Boolean(p.needs_decision) })}><Play className="h-3.5 w-3.5" /> {p.needs_decision ? "Repeat it" : "Resume"}</Button>}
          </div>
        </div>
      )}
      {run.criteria.length > 0 && <p className="mt-3 text-[11px] text-slate-500">Done means: {run.criteria.join(" · ")}</p>}
      <ul className="mt-2 divide-y divide-ink-700">{run.steps.map((s) => <StepRow key={s.id} step={s} />)}</ul>
      {run.summary && !live && (
        <p className="mt-3 rounded-xl bg-ink-850 p-3 text-xs text-slate-200" data-testid="run-summary">{run.summary}</p>
      )}
      {run.status === "partially_completed" || run.status === "failed" ? (
        <p className="mt-2 text-xs text-amber-300" data-testid="run-incomplete">Not finished: {unfinished.map((s) => s.tool).join(", ") || "see the steps above"}.</p>
      ) : null}
      {run.caveats.length > 0 && <ul className="mt-2 list-disc pl-5 text-[11px] text-slate-400">{run.caveats.map((c) => <li key={c}>{c}</li>)}</ul>}
      <p className="mt-2 text-[11px] text-slate-500">
        {String(run.metrics.tool_calls ?? 0)} tool call(s) · {String(run.metrics.retries ?? 0)} retr{run.metrics.retries === 1 ? "y" : "ies"} · {String(run.metrics.llm_calls ?? 0)} model call(s) · {String(run.metrics.seconds ?? 0)} s
      </p>
      <div className="mt-2 flex gap-2">
        {live && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act("cancel")}><Square className="h-3.5 w-3.5" /> Cancel run</Button>}
      </div>
    </div>
  );
}

function RunsCard() {
  const runs = useAsync(() => api.get<RunView[]>("/api/runs"), []);
  const [sel, setSel] = useState<number | null>(null);
  useEvents((ev) => { if (ev.type === "run") void runs.reload(); });
  if (!runs.data) return <Card title="Tasks NEXUS ran">{runs.error ? <ErrorBanner message={runs.error} onRetry={runs.reload} /> : <Spinner />}</Card>;
  const open = runs.data.find((r) => r.id === sel) ?? runs.data[0];
  return (
    <Card title="Tasks NEXUS ran">
      {runs.data.length === 0 ? (
        <Empty icon={<Wrench className="h-6 w-6" />} title="Nothing yet">Ask for something like “organize my downloads folder” or “open Spotify and remind me to stretch in 10 minutes”.</Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-[16rem_1fr]">
          <ul className="max-h-96 space-y-1 overflow-y-auto" aria-label="Runs">
            {runs.data.map((r) => (
              <li key={r.id}>
                <button type="button" onClick={() => setSel(r.id)} data-testid="run-item" aria-current={open?.id === r.id}
                  className={`w-full rounded-xl p-2 text-left text-xs ${open?.id === r.id ? "bg-accent/15" : "hover:bg-ink-800"}`}>
                  <span className="line-clamp-2 text-slate-200">{r.goal}</span>
                  <span className="mt-1 block"><StatusBadge status={r.status} /></span>
                </button>
              </li>
            ))}
          </ul>
          {open && <RunDetail run={open} reload={runs.reload} />}
        </div>
      )}
    </Card>
  );
}

function CapabilitiesCard() {
  const caps = useAsync(() => api.get<CapabilitiesResponse>("/api/capabilities"), []);
  const [diag, setDiag] = useState<DiagnoseResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setDiag(await api.post<DiagnoseResponse>("/api/capabilities/diagnose")); } finally { setBusy(false); }
  };
  if (!caps.data) return <Card title="What NEXUS can do right now">{caps.error ? <ErrorBanner message={caps.error} onRetry={caps.reload} /> : <Spinner />}</Card>;
  const c = caps.data;
  return (
    <Card title="What NEXUS can do right now" actions={<Button size="sm" variant="outline" loading={busy} onClick={() => void run()}>Run self-check</Button>}>
      <p className="text-xs text-slate-400">
        Model <code>{c.models.active}</code> ({c.models.reachable ? "reachable" : "not reachable"}) · {c.environment.cpus} CPUs · {c.environment.ram_gb} GB RAM.
        {c.models.vision_capable_but_unused.length > 0 && ` Vision models installed but not used: ${c.models.vision_capable_but_unused.join(", ")}.`}
      </p>
      {diag && (
        <p className="mt-2 rounded-xl bg-ink-850 p-2 text-xs text-slate-200" data-testid="diagnose-result" role="status">
          Self-check: {diag.ok} of {diag.total} tools ready. Nothing on your computer was changed.
          {diag.tools.filter((t) => !t.ok).map((t) => <span key={t.tool} className="block text-slate-400">{t.tool}: {t.detail}</span>)}
        </p>
      )}
      <ul className="mt-3 divide-y divide-ink-700">
        {c.tools.map((t) => (
          <li key={t.name} className="py-2" data-testid="tool-row">
            <div className="flex flex-wrap items-center gap-2">
              <code className="text-xs text-slate-100">{t.name}</code>
              <Badge tone={RISK_TONE[t.risk]}>{t.risk === "read" ? "read only" : `${t.risk} risk`}</Badge>
              {t.verifies && <Badge tone="green">checks its result</Badge>}
              {!t.available && <Badge tone="red">unavailable</Badge>}
            </div>
            <p className="mt-0.5 text-xs text-slate-400">{t.description}{t.limits ? ` Limit: ${t.limits}` : ""}{!t.available ? ` (${t.unavailable_reason})` : ""}</p>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-[11px] font-medium text-slate-300">Known limits</p>
      <ul className="list-disc pl-5 text-[11px] text-slate-500">{c.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
    </Card>
  );
}

interface AgentPrefs { enabled: boolean; auto_approve_risk: "low" | "medium" }

function AutonomyCard() {
  const { toast } = useStore();
  const prefs = useAsync(() => api.get<{ agent: AgentPrefs }>("/api/preferences"), []);
  if (!prefs.data) return null;
  const a = prefs.data.agent;
  const save = async (agent: Partial<AgentPrefs>) => {
    try {
      prefs.setData(await api.patch<{ agent: AgentPrefs }>("/api/preferences", { agent }));
    } catch (e) {
      toast({ title: "Couldn't save", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  return (
    <Card title="How much NEXUS may do on its own">
      <Toggle checked={a.enabled} onChange={(v) => void save({ enabled: v })} label="Plan multi-step tasks" description="Off: NEXUS goes back to its simple one-command behaviour." />
      <div className="mt-3 max-w-sm">
        <Field label="Runs without asking" hint="Moving files, sending a message, editing code or running a command always asks, whatever you pick here.">
          <Select ariaLabel="Autonomy" value={a.auto_approve_risk} onChange={(v) => void save({ auto_approve_risk: v as AgentPrefs["auto_approve_risk"] })} options={[
            { value: "low", label: "Only harmless things (opening apps, reminders, reading)" },
            { value: "medium", label: "Also closing apps and dismissing dialogs" }]} />
        </Field>
      </div>
    </Card>
  );
}

function WorkflowsCard() {
  const wf = useAsync(() => api.get<WorkflowInfo[]>("/api/workflows"), []);
  if (!wf.data) return null;
  return (
    <Card title="Workflows">
      <ul className="divide-y divide-ink-700">
        {wf.data.map((w) => (
          <li key={w.name} className="py-2 text-xs" data-testid="workflow">
            <span className="font-medium text-slate-100">{w.name.replace(/_/g, " ")}</span> {w.builtin && <Badge>built in</Badge>}
            <p className="text-slate-400">{w.description}</p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function AutomationPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
      <PageHeader title="Automation" subtitle="Every task NEXUS ran for you: what it did, what it checked, and what still needs you." />
      <RunsCard />
      <AutonomyCard />
      <CapabilitiesCard />
      <WorkflowsCard />
    </div>
  );
}
