import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, PageHeader, StatusBadge, cn } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api, formatBytes, timeAgo } from "../../services/api";

interface Audit { id: number; ts: string; actor: string; action: string; target: string; outcome: string }
interface Action { id: number; kind: string; summary: string; status: string; result: string; source: string; created_at: string }

export function HealthPage() {
  const { health, refreshHealth } = useStore();
  const audit = useAsync(() => api.get<Audit[]>("/api/audit?limit=100"), []);
  const actions = useAsync(() => api.get<Action[]>("/api/actions?limit=50"), []);
  const logs = useAsync(() => api.get<{ path: string; lines: string[] }>("/api/system/logs?lines=200"), []);
  const [tab, setTab] = useState<"audit" | "actions" | "logs">("audit");

  const h = health;
  const memUsed = h ? h.system.memory_total - h.system.memory_available : 0;

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <PageHeader title="System health" subtitle="Live diagnostics. All values are measured, nothing is simulated." actions={
        <>
          <Button size="sm" onClick={() => { void refreshHealth(); void audit.reload(); void logs.reload(); void actions.reload(); }}><RefreshCw className="h-3.5 w-3.5" /> Refresh</Button>
          <a href="/api/system/diagnostics" className="inline-flex h-7 items-center rounded-lg border border-ink-600 px-2.5 text-xs hover:bg-ink-800">Export diagnostics</a>
        </>
      } />
      {h && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Card title="NEXUS">
            <p className="text-sm">v{h.version} · up {Math.round(h.uptime_s / 60)} min</p>
            <p className="text-xs text-slate-400">{h.paused ? "Paused" : "Running"}</p>
            <p className="mt-1 text-xs text-slate-500">DB schema v{h.database.schema_version}</p>
            {h.database.recovered_from && <p className="mt-1 text-xs text-amber-300">Recovered from a damaged database; the old file was kept at {h.database.recovered_from}</p>}
          </Card>
          <Card title="Ollama">
            <p className={cn("text-sm", h.ollama.available ? "text-emerald-300" : "text-rose-300")}>{h.ollama.available ? `Connected (v${h.ollama.version})` : "Not reachable"}</p>
            <p className="text-xs text-slate-400">Active model: {h.active_model}</p>
            <p className="text-xs text-slate-500">Loaded: {h.ollama.loaded?.map((m) => m.name).join(", ") || "none"}</p>
          </Card>
          <Card title="Computer">
            <p className="text-sm">CPU {h.system.cpu_percent.toFixed(0)}% · {h.system.cpu_count} threads</p>
            <p className="text-xs text-slate-400">RAM {formatBytes(memUsed)} / {formatBytes(h.system.memory_total)}</p>
            <p className="truncate text-xs text-slate-500" title={h.system.platform}>{h.system.platform}</p>
          </Card>
          <Card title="Activity">
            <p className="text-sm">{h.generations.length ? `${h.generations.length} generation(s) running` : "Idle"}</p>
            {h.generations.map((g) => <p key={g.id} className="text-xs text-slate-400">{g.purpose} · {g.state} · {g.elapsed_s.toFixed(0)}s</p>)}
            <p className="mt-1 text-xs text-slate-500">Voice: {h.voice.enabled ? h.voice.state.replace(/_/g, " ") : "disabled"}</p>
          </Card>
        </div>
      )}
      <div className="flex gap-1 border-b border-ink-700" role="tablist">
        {(["audit", "actions", "logs"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)} className={cn("border-b-2 px-3 py-2 text-sm", tab === t ? "border-accent text-fg" : "border-transparent text-slate-400")}>
            {t === "audit" ? "Audit log" : t === "actions" ? "Assistant actions" : "Application log"}
          </button>
        ))}
      </div>
      {tab === "audit" && (
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500"><tr><th className="py-1">When</th><th>Who</th><th>Action</th><th>Target</th><th>Outcome</th></tr></thead>
          <tbody>
            {audit.data?.map((a) => (
              <tr key={a.id} className="border-t border-ink-700 text-slate-300">
                <td className="py-1.5">{timeAgo(a.ts)}</td><td><Badge>{a.actor}</Badge></td><td className="font-mono">{a.action}</td>
                <td className="max-w-xs truncate font-mono text-slate-500">{a.target}</td><td><StatusBadge status={a.outcome} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {tab === "actions" && (
        <table className="w-full text-left text-xs">
          <thead className="text-slate-500"><tr><th className="py-1">When</th><th>Source</th><th>Action</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>
            {actions.data?.map((a) => (
              <tr key={a.id} className="border-t border-ink-700 text-slate-300">
                <td className="py-1.5">{timeAgo(a.created_at)}</td><td>{a.source}</td><td>{a.summary}</td><td><StatusBadge status={a.status} /></td><td className="max-w-md truncate text-slate-400">{a.result}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {tab === "logs" && (
        <div>
          <p className="mb-2 font-mono text-[11px] text-slate-500">{logs.data?.path}</p>
          <pre className="max-h-[60vh] overflow-auto rounded-lg border border-ink-700 bg-ink-950 p-3 font-mono text-[11px] leading-5 text-slate-400">{logs.data?.lines.join("\n")}</pre>
        </div>
      )}
    </div>
  );
}
