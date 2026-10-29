import { CheckCircle2, Circle, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { useStore } from "../../app/store";
import { Button, Card } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api } from "../../services/api";

function Step({ state, title, children }: { state: "ok" | "todo" | "error"; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-3 py-3">
      {state === "ok" ? <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" /> : state === "error" ? <XCircle className="mt-0.5 h-5 w-5 shrink-0 text-rose-400" /> : <Circle className="mt-0.5 h-5 w-5 shrink-0 text-slate-500" />}
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-fg">{title}</p>
        <div className="mt-1 text-xs text-slate-400">{children}</div>
      </div>
    </li>
  );
}

export function Onboarding() {
  const { health, refreshHealth, navigate, workspaces } = useStore();
  const models = useAsync(() => api.get<{ available: boolean; active: string; active_installed: boolean }>("/api/models"), [health?.ollama.available]);
  const ollamaOk = Boolean(health?.ollama.available);
  const modelOk = Boolean(models.data?.active_installed);

  const finish = async () => {
    await api.patch("/api/preferences", { onboarding_complete: true });
    await refreshHealth();
    navigate("chat/new");
  };

  return (
    <div className="mx-auto max-w-2xl p-8">
      <div className="mb-6 flex items-center gap-3">
        <img src="/nexus.svg" alt="" className="h-10 w-10" />
        <div>
          <h1 className="text-2xl font-semibold text-fg">Welcome to NEXUS</h1>
          <p className="text-sm text-slate-400">Your local AI engineering partner. Everything runs on this laptop.</p>
        </div>
      </div>
      <Card title="Setup checklist" actions={<Button size="sm" variant="ghost" onClick={() => { void refreshHealth(); void models.reload(); }}>Re-check</Button>}>
        <ol className="divide-y divide-ink-700">
          <Step state="ok" title="NEXUS backend is running">Your data is stored locally. No telemetry, no cloud services.</Step>
          <Step state={ollamaOk ? "ok" : "error"} title="Ollama is installed and running">
            {ollamaOk ? `Connected to Ollama ${health?.ollama.version ?? ""}.` : (
              <>Install Ollama for Windows from <span className="text-slate-200">ollama.com</span> and start it. NEXUS checks every 30 seconds. Until then, notes, timers, reminders and projects still work.</>
            )}
          </Step>
          <Step state={modelOk ? "ok" : ollamaOk ? "error" : "todo"} title={`Model ${models.data?.active ?? "qwen2.5-coder:7b"} is downloaded`}>
            {modelOk ? "Ready to chat." : <>Run <code className="text-slate-200">ollama pull qwen2.5-coder:7b</code> or <button className="underline" onClick={() => navigate("models")}>download it from the Models page</button>. Expect roughly 3–8 words/second on your CPU.</>}
          </Step>
          <Step state={workspaces.length ? "ok" : "todo"} title="Add a project (optional)">
            <button className="underline" onClick={() => navigate("workspaces")}>Add a code folder</button> to get project-aware answers. Projects start read-only.
          </Step>
          <Step state={health?.voice.enabled ? "ok" : "todo"} title="Set up voice (optional)">
            <button className="underline" onClick={() => navigate("voice")}>Voice settings</button>: push-to-talk and the “Hey Nexus” wake word, fully local.
          </Step>
          <Step state={health?.startup.enabled ? "ok" : "todo"} title="Start with Windows (optional)">
            {health?.startup.supported ? <button className="underline" onClick={() => navigate("settings")}>Enable in Settings</button> : health?.startup.detail}
          </Step>
        </ol>
        <div className="mt-4 flex justify-end">
          <Button variant="primary" onClick={finish}>{ollamaOk && modelOk ? "Start using NEXUS" : "Continue without the model for now"}</Button>
        </div>
      </Card>
    </div>
  );
}
