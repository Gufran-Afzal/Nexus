import { Play, ShieldAlert, Square, Terminal } from "lucide-react";
import { useEffect, useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, Select, Spinner, StatusBadge } from "../../components/ui";
import { useAsync, useEvents, usePolling } from "../../hooks/useApi";
import { api, timeAgo } from "../../services/api";
import type { CommandRun } from "../../types";

const RISK_TONE: Record<string, string> = { verification: "green", unfamiliar: "amber", destructive: "red", package_install: "amber", network: "amber", blocked: "red" };

export function CommandsPage() {
  const { workspaces, navigate } = useStore();
  const runs = useAsync(() => api.get<CommandRun[]>("/api/commands"), []);
  const [wsId, setWsId] = useState<string>(workspaces[0] ? String(workspaces[0].id) : "");
  const [cmd, setCmd] = useState("");
  const [cwd, setCwd] = useState(".");
  const [preview, setPreview] = useState<{ risk: string; reasons: string[]; argv: string[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEvents((e) => e.type === "command" && void runs.reload());
  usePolling(runs.reload, 2000, Boolean(runs.data?.some((r) => r.status === "running")));

  useEffect(() => {
    if (!wsId && workspaces[0]) setWsId(String(workspaces[0].id));
  }, [workspaces, wsId]);

  useEffect(() => {
    if (!cmd.trim()) {
      setPreview(null);
      return;
    }
    const id = setTimeout(async () => {
      try {
        setPreview(await api.post("/api/commands/classify", { command: cmd }));
      } catch {
        setPreview(null);
      }
    }, 250);
    return () => clearTimeout(id);
  }, [cmd]);

  const request = async () => {
    setErr(null);
    try {
      await api.post("/api/commands", { workspace_id: Number(wsId), command: cmd, cwd });
      setCmd("");
      await runs.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  if (workspaces.length === 0)
    return (
      <div className="p-6">
        <PageHeader title="Commands" />
        <Empty icon={<Terminal className="h-8 w-8" />} title="Add a project first">
          Commands only run inside a project folder you've added. <button className="underline" onClick={() => navigate("workspaces")}>Add a project</button>
        </Empty>
      </div>
    );

  return (
    <div className="mx-auto max-w-5xl p-6">
      <PageHeader title="Commands" subtitle="Run tests, linters and type checks — only after you approve the exact command. No shell is used." />
      <Card title="Request a command">
        <div className="grid gap-3 md:grid-cols-[1fr_2fr_120px]">
          <Field label="Project">
            <Select value={wsId} onChange={setWsId} options={workspaces.map((w) => ({ value: String(w.id), label: w.name }))} ariaLabel="Project" />
          </Field>
          <Field label="Command">
            <Input value={cmd} onChange={(e) => setCmd(e.target.value)} placeholder="pytest -q" className="font-mono" onKeyDown={(e) => e.key === "Enter" && cmd && void request()} />
          </Field>
          <Field label="Folder">
            <Input value={cwd} onChange={(e) => setCwd(e.target.value)} className="font-mono" />
          </Field>
        </div>
        {preview && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={RISK_TONE[preview.risk]}>{preview.risk.replace("_", " ")}</Badge>
            <span className="text-slate-400">{preview.reasons.join(" · ")}</span>
            <span className="font-mono text-slate-500">argv: {JSON.stringify(preview.argv)}</span>
          </div>
        )}
        <div className="mt-3 flex items-center gap-2">
          <Button variant="primary" onClick={request} disabled={!cmd.trim() || !wsId}>Request (needs approval)</Button>
          {err && <ErrorBanner message={err} />}
        </div>
      </Card>

      <div className="mt-5 space-y-3">
        {runs.loading && !runs.data && <Spinner />}
        {runs.data?.length === 0 && <Empty title="No commands yet" />}
        {runs.data?.map((r) => <RunCard key={r.id} run={r} onChange={runs.reload} wsName={workspaces.find((w) => w.id === r.workspace_id)?.name} />)}
      </div>
    </div>
  );
}

function RunCard({ run, onChange, wsName }: { run: CommandRun; onChange: () => void; wsName?: string }) {
  const [confirm, setConfirm] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(run.status !== "succeeded");
  const risky = ["destructive", "package_install", "network"].includes(run.risk);

  const call = async (path: string, body?: unknown) => {
    setErr(null);
    try {
      await api.post(path, body);
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="rounded-xl border border-ink-700 bg-ink-900">
      <header className="flex flex-wrap items-center gap-2 px-4 py-2.5">
        <span className="text-xs text-slate-500">#{run.id}</span>
        <code className="min-w-0 flex-1 truncate rounded bg-ink-950 px-2 py-1 text-xs text-slate-100">{run.argv.join(" ")}</code>
        <Badge tone={RISK_TONE[run.risk]}>{run.risk.replace("_", " ")}</Badge>
        <Badge tone="gray">{run.requested_by}</Badge>
        <StatusBadge status={run.status} />
        {run.exit_code !== null && <span className="text-xs text-slate-400">exit {run.exit_code}</span>}
        <button className="text-xs text-slate-400 hover:text-fg" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "Details"}</button>
      </header>
      {open && (
        <div className="space-y-3 border-t border-ink-700 px-4 py-3">
          <p className="text-[11px] text-slate-500">{wsName} · folder <span className="font-mono">{run.cwd}</span> · timeout {run.timeout_s}s · {timeAgo(run.created_at)} · {run.reasons.join(" · ")}</p>
          {run.status === "pending_approval" && (
            <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
              <p className="flex items-center gap-1.5 text-sm text-amber-200"><ShieldAlert className="h-4 w-4" /> Approve running exactly this command?</p>
              {risky && (
                <label className="flex items-center gap-2 text-xs text-rose-200">
                  <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
                  I understand this is a {run.risk.replace("_", " ")} command
                </label>
              )}
              <div className="flex gap-2">
                <Button size="sm" variant="primary" disabled={risky && !confirm} onClick={() => call(`/api/commands/${run.id}/approve`, { confirm_risk: confirm })}>
                  <Play className="h-3.5 w-3.5" /> Approve & run
                </Button>
                <Button size="sm" onClick={() => call(`/api/commands/${run.id}/reject`)}>Reject</Button>
              </div>
            </div>
          )}
          {run.status === "running" && (
            <Button size="sm" variant="danger" onClick={() => call(`/api/commands/${run.id}/cancel`)}><Square className="h-3.5 w-3.5" /> Stop</Button>
          )}
          {err && <ErrorBanner message={err} />}
          {(run.stdout || run.stderr) && (
            <pre className="max-h-96 overflow-auto rounded-lg border border-ink-700 bg-ink-950 p-3 font-mono text-[12px] leading-5 text-slate-300">
              {run.stdout}
              {run.stderr && <span className="text-rose-300">{run.stderr}</span>}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
