import { Download, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, ErrorBanner, Input, PageHeader, Spinner } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api, formatBytes, postStream, timeAgo } from "../../services/api";
import type { ModelInfo } from "../../types";

interface ModelsResp { available: boolean; error?: string; models: ModelInfo[]; loaded?: { name: string; size_bytes: number; size_vram_bytes: number }[]; active: string; active_installed: boolean }
interface Perf { ts: string; model: string; tokens_per_s: number | null; output_tokens: number | null; load_s: number | null; total_s: number | null }

export function ModelsPage() {
  const { refreshHealth, health } = useStore();
  const models = useAsync(() => api.get<ModelsResp>("/api/models"), []);
  const perf = useAsync(() => api.get<Perf[]>("/api/models/performance?limit=10"), []);
  const [pullName, setPullName] = useState("qwen2.5-coder:7b");
  const [progress, setProgress] = useState<{ status: string; total?: number; completed?: number } | null>(null);
  const [pullErr, setPullErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const setActive = async (name: string) => {
    await api.post("/api/models/active", { name });
    await models.reload();
    await refreshHealth();
  };

  const pull = async () => {
    setPullErr(null);
    setProgress({ status: "starting" });
    abortRef.current = new AbortController();
    try {
      await postStream("/api/models/pull", { name: pullName }, (ev) => {
        if (ev.event === "progress") setProgress(ev.data as { status: string });
        if (ev.event === "error") setPullErr(String(ev.data.message));
        if (ev.event === "done") setProgress({ status: "success" });
      }, abortRef.current.signal);
      await models.reload();
    } catch (e) {
      if ((e as Error).name !== "AbortError") setPullErr(e instanceof Error ? e.message : String(e));
      else setProgress({ status: "cancelled — Ollama keeps partial downloads and resumes next time" });
    }
  };

  const d = models.data;
  const pct = progress?.total ? Math.round(((progress.completed ?? 0) / progress.total) * 100) : null;

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <PageHeader title="Models" subtitle="Local models served by Ollama. Nothing is downloaded unless you ask." actions={<Button size="sm" onClick={() => { void models.reload(); void refreshHealth(); }}><RefreshCw className="h-3.5 w-3.5" /> Refresh</Button>} />

      <Card title="Connection">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className={health?.ollama.available ? "text-emerald-300" : "text-rose-300"}>{health?.ollama.available ? "● Connected" : "● Not reachable"}</span>
          <span className="font-mono text-xs text-slate-400">{health?.ollama.url}</span>
          {health?.ollama.version && <Badge>v{health.ollama.version}</Badge>}
        </div>
        {!health?.ollama.available && (
          <div className="mt-3 space-y-1 text-xs text-slate-400">
            <p className="text-rose-200">{health?.ollama.error}</p>
            <p>1. Install Ollama for Windows from ollama.com (one-time download).</p>
            <p>2. Make sure it's running: it starts with Windows by default (look for the llama icon in the tray), or run <code className="text-slate-200">ollama serve</code>.</p>
            <p>3. Download the model: <code className="text-slate-200">ollama pull qwen2.5-coder:7b</code> (≈4.7 GB), or use the download box below.</p>
            <p>Timers, notes, reminders, projects and change review keep working without Ollama.</p>
          </div>
        )}
      </Card>

      <Card title="Installed models">
        {models.loading && !d && <Spinner />}
        {models.error && <ErrorBanner message={models.error} />}
        {d && !d.available && <Empty title="Ollama is not reachable">{d.error}</Empty>}
        {d?.available && d.models.length === 0 && <Empty title="No models installed">Download one below.</Empty>}
        {d?.available && !d.active_installed && d.models.length > 0 && (
          <div className="mb-3"><ErrorBanner message={`The active model "${d.active}" is not installed. Pick an installed model or download it.`} /></div>
        )}
        <div className="divide-y divide-ink-700">
          {d?.models.map((m) => {
            const loaded = d.loaded?.find((l) => l.name === m.name);
            return (
              <div key={m.name} className="flex flex-wrap items-center gap-3 py-2.5">
                <span className="font-mono text-sm text-fg">{m.name}</span>
                {m.parameter_size && <Badge>{m.parameter_size}</Badge>}
                {m.quantization && <Badge>{m.quantization}</Badge>}
                <span className="text-xs text-slate-500">{formatBytes(m.size_bytes)}</span>
                {loaded && <Badge tone="blue">in memory{loaded.size_vram_bytes ? ` · ${formatBytes(loaded.size_vram_bytes)} on GPU` : " · CPU"}</Badge>}
                <div className="flex-1" />
                {loaded && <Button size="sm" variant="ghost" onClick={async () => { await api.post("/api/models/unload", { name: m.name }); void models.reload(); }}>Unload from RAM</Button>}
                {d.active === m.name ? <Badge tone="violet">active</Badge> : <Button size="sm" onClick={() => setActive(m.name)}>Use this model</Button>}
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Download a model">
        <div className="flex gap-2">
          <Input value={pullName} onChange={(e) => setPullName(e.target.value)} className="font-mono" aria-label="Model name" />
          <Button variant="primary" onClick={pull} disabled={!pullName.trim() || !health?.ollama.available || (progress !== null && !["success"].includes(progress.status) && !pullErr && !progress.status.startsWith("cancelled"))}>
            <Download className="h-4 w-4" /> Download
          </Button>
          {progress && progress.status !== "success" && !pullErr && <Button variant="ghost" onClick={() => abortRef.current?.abort()}>Cancel</Button>}
        </div>
        <p className="mt-2 text-[11px] text-slate-500">This is the only place NEXUS asks Ollama to use the internet. Suggested for your laptop: qwen2.5-coder:7b (default), qwen2.5-coder:3b (faster), llama3.2:3b (general chat).</p>
        {progress && (
          <div className="mt-3">
            <p className="text-xs text-slate-300">{progress.status}{pct !== null ? ` — ${pct}% (${formatBytes(progress.completed)} / ${formatBytes(progress.total)})` : ""}</p>
            {pct !== null && <div className="mt-1 h-1.5 overflow-hidden rounded bg-ink-700"><div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} /></div>}
          </div>
        )}
        {pullErr && <div className="mt-3"><ErrorBanner message={pullErr} /></div>}
      </Card>

      <Card title="Recent performance (measured)">
        {perf.data?.length ? (
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500"><tr><th className="py-1">When</th><th>Model</th><th>Tokens/s</th><th>Output tokens</th><th>Load</th><th>Total</th></tr></thead>
            <tbody className="text-slate-300">
              {perf.data.map((p, i) => (
                <tr key={i} className="border-t border-ink-700">
                  <td className="py-1">{timeAgo(p.ts)}</td><td className="font-mono">{p.model}</td><td>{p.tokens_per_s?.toFixed(1) ?? "—"}</td>
                  <td>{p.output_tokens ?? "—"}</td><td>{p.load_s ? `${p.load_s.toFixed(1)}s` : "—"}</td><td>{p.total_s ? `${p.total_s.toFixed(1)}s` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="text-xs text-slate-500">No measurements yet — they're recorded from real model responses.</p>}
      </Card>
    </div>
  );
}
