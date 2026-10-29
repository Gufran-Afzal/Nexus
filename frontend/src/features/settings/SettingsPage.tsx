import { useState } from "react";
import { useStore } from "../../app/store";
import { Button, Card, ErrorBanner, Field, Input, PageHeader, Spinner, Textarea, Toggle } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api, formatBytes } from "../../services/api";
import type { Preferences } from "../../types";

interface Storage {
  data_dir: string;
  database: { path: string; bytes: number };
  logs: { path: string; bytes: number };
  backups: { path: string; bytes: number };
  models: { path: string; bytes: number };
  counts: Record<string, number>;
  what_is_stored: string[];
}

export function SettingsPage() {
  const { refreshHealth, refreshConversations, toast, health } = useStore();
  const prefs = useAsync(() => api.get<Preferences>("/api/preferences"), []);
  const storage = useAsync(() => api.get<Storage>("/api/system/storage"), []);
  const [err, setErr] = useState<string | null>(null);

  const save = async (body: Partial<Preferences>, msg = "Saved") => {
    setErr(null);
    try {
      prefs.setData(await api.patch<Preferences>("/api/preferences", body));
      toast({ title: msg, tone: "success" });
      void refreshHealth();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  if (!prefs.data) return <div className="p-8">{prefs.error ? <ErrorBanner message={prefs.error} /> : <Spinner />}</div>;
  const p = prefs.data;

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <PageHeader title="Settings" />
      {err && <ErrorBanner message={err} />}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Start with Windows">
          {health?.startup.supported ? (
            <>
              <Toggle
                checked={health.startup.enabled}
                onChange={async (v) => {
                  try {
                    await api.post("/api/system/startup", { enabled: v });
                    await refreshHealth();
                  } catch (e) {
                    setErr(e instanceof Error ? e.message : String(e));
                  }
                }}
                label="Start NEXUS when I sign in"
                description="Adds a per-user entry (no admin rights). Starts quietly in the system tray; models load only when needed."
              />
              <p className="mt-2 text-[11px] text-slate-500">You can also disable it in Task Manager → Startup apps. Use the tray icon's “Exit NEXUS completely” to stop it.</p>
            </>
          ) : (
            <p className="text-xs text-slate-400">{health?.startup.detail ?? "Unavailable on this system."}</p>
          )}
        </Card>

        <Card title="Assistant">
          <Field label="Name"><Input defaultValue={p.assistant_name} onBlur={(e) => e.target.value !== p.assistant_name && save({ assistant_name: e.target.value })} /></Field>
          <div className="mt-3">
            <Field label="Keep conversations for (days)" hint="0 keeps them until you delete them.">
              <Input type="number" min={0} defaultValue={p.retention_days} onBlur={(e) => Number(e.target.value) !== p.retention_days && save({ retention_days: Number(e.target.value) })} />
            </Field>
          </div>
        </Card>

        <Card title="Model generation">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Temperature" hint="Lower = more focused">
              <Input type="number" step="0.1" min={0} max={2} defaultValue={p.generation.temperature} onBlur={(e) => save({ generation: { ...p.generation, temperature: Number(e.target.value) } })} />
            </Field>
            <Field label="Top-p"><Input type="number" step="0.05" min={0.05} max={1} defaultValue={p.generation.top_p} onBlur={(e) => save({ generation: { ...p.generation, top_p: Number(e.target.value) } })} /></Field>
            <Field label="Context window (tokens)" hint="8192 is a good CPU default; larger uses more RAM and is slower.">
              <Input type="number" step={1024} min={1024} defaultValue={p.generation.num_ctx} onBlur={(e) => save({ generation: { ...p.generation, num_ctx: Number(e.target.value) } })} />
            </Field>
            <Field label="Max answer length (tokens)"><Input type="number" min={64} defaultValue={p.generation.max_output_tokens} onBlur={(e) => save({ generation: { ...p.generation, max_output_tokens: Number(e.target.value) } })} /></Field>
            <Field label="Keep model loaded for" hint="e.g. 5m, 30m, 0 to unload immediately">
              <Input defaultValue={p.generation.keep_alive} onBlur={(e) => save({ generation: { ...p.generation, keep_alive: e.target.value } })} />
            </Field>
          </div>
        </Card>

        <Card title="Model roles">
          <p className="mb-2 text-xs text-slate-400">Optional: use a different installed model per job. Leave empty to use the active model ({p.active_model}). Only one chat model is used per request.</p>
          <div className="space-y-3">
            <Field label="Fast model (Assistant and voice)" hint="A small model answers several times faster on a CPU. Try qwen2.5:3b (install it from Models first). Empty = the active model."><Input defaultValue={p.model_roles.fast} placeholder={p.active_model} onBlur={(e) => e.target.value !== p.model_roles.fast && save({ model_roles: { ...p.model_roles, fast: e.target.value.trim() } })} /></Field>
            <Field label="Coding conversations" hint="e.g. qwen2.5-coder:7b"><Input defaultValue={p.model_roles.coding} placeholder={p.active_model} onBlur={(e) => e.target.value !== p.model_roles.coding && save({ model_roles: { ...p.model_roles, coding: e.target.value.trim() } })} /></Field>
            <Field label="Tasks (plans, reviews, edit proposals)" hint="A faster model (e.g. qwen2.5-coder:3b) makes tasks quicker"><Input defaultValue={p.model_roles.tasks} placeholder={p.active_model} onBlur={(e) => e.target.value !== p.model_roles.tasks && save({ model_roles: { ...p.model_roles, tasks: e.target.value.trim() } })} /></Field>
            <Field label="Embeddings (semantic search)"><Input defaultValue={p.model_roles.embedding} onBlur={(e) => e.target.value !== p.model_roles.embedding && save({ model_roles: { ...p.model_roles, embedding: e.target.value.trim() || "nomic-embed-text" } })} /></Field>
          </div>
        </Card>

        <Card title="Approved actions">
          <Field label="Applications" hint="NEXUS can only open apps you integrate. Windows apps are found automatically; no program names to type.">
            <Button size="sm" variant="subtle" onClick={() => { window.location.hash = "/apps"; }}>Manage apps…</Button>
          </Field>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field label="Web search address" hint="Used by “google …” / “search for … in Chrome”. Must be https and contain {q}.">
              <Input defaultValue={p.search_url} onBlur={(e) => e.target.value !== p.search_url && save({ search_url: e.target.value.trim() })} />
            </Field>
            <Toggle checked={p.reminders_speak} onChange={(v) => save({ reminders_speak: v })} label="Speak reminders aloud"
              description="Works even with the microphone and wake word off." />
          </div>
          <div className="mt-3">
            <Field label="Websites (domains)">
              <Textarea rows={3} className="font-mono text-xs" defaultValue={p.allowed_url_domains.join("\n")} onBlur={(e) => save({ allowed_url_domains: e.target.value.split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean) })} />
            </Field>
          </div>
          <div className="mt-3">
            <Field label="Folders that can be opened (full paths)">
              <Textarea rows={3} className="font-mono text-xs" defaultValue={p.approved_folders.join("\n")} onBlur={(e) => save({ approved_folders: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
            </Field>
          </div>
        </Card>
      </div>

      <Card title="Instructions">
        <Field label="Assistant personality">
          <Textarea rows={5} defaultValue={p.persona_prompt} onBlur={(e) => e.target.value !== p.persona_prompt && save({ persona_prompt: e.target.value })} />
        </Field>
        <div className="mt-3">
          <Field label="Senior-engineer instructions (coding mode)" hint="NEXUS's security rules are always added after these and can't be overridden.">
            <Textarea rows={5} defaultValue={p.engineer_prompt} onBlur={(e) => e.target.value !== p.engineer_prompt && save({ engineer_prompt: e.target.value })} />
          </Field>
        </div>
      </Card>

      <Card title="Your data">
        {storage.data && (
          <div className="space-y-3 text-sm">
            <p className="text-slate-300">Everything is stored locally in <span className="font-mono text-xs text-fg">{storage.data.data_dir}</span></p>
            <div className="grid gap-2 text-xs sm:grid-cols-2">
              <p>Database: {formatBytes(storage.data.database.bytes)}</p>
              <p>Logs: {formatBytes(storage.data.logs.bytes)}</p>
              <p>Backups: {formatBytes(storage.data.backups.bytes)}</p>
              <p>Speech models: {formatBytes(storage.data.models.bytes)}</p>
            </div>
            <p className="text-xs text-slate-400">{Object.entries(storage.data.counts).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(" · ")}</p>
            <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-400">{storage.data.what_is_stored.map((s) => <li key={s}>{s}</li>)}</ul>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={async () => { const r = await api.post<{ path: string }>("/api/system/backup"); toast({ title: "Backup created", body: r.path, tone: "success" }); void storage.reload(); }}>Back up database</Button>
              <a href="/api/system/diagnostics" className="inline-flex h-7 items-center rounded-lg border border-ink-600 px-2.5 text-xs hover:bg-ink-800">Export diagnostics</a>
              <Button size="sm" variant="danger" onClick={async () => {
                if (!window.confirm("Delete ALL conversation history? Notes, reminders and projects are kept.")) return;
                const r = await api.del<{ deleted: number }>("/api/conversations?confirm=true");
                toast({ title: `Deleted ${r.deleted} conversations`, tone: "success" });
                void refreshConversations();
                void storage.reload();
              }}>Clear conversation history</Button>
              <Button size="sm" variant="ghost" onClick={() => save({ onboarding_complete: false }, "Setup guide will show on the chat page")}>Show setup guide again</Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
