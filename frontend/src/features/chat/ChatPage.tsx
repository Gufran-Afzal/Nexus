import { Bot, Code2, Download, FileText, Loader2, Mic, Pencil, Send, Square, Trash2, Wrench, X, Zap } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useStore } from "../../app/store";
import { Markdown } from "../../components/Markdown";
import { Badge, Button, cn, ErrorBanner, Select, Spinner, StatusBadge, Toggle } from "../../components/ui";
import { api, postStream } from "../../services/api";
import type { ContextFile, Conversation, Message, Mode, ToolStep } from "../../types";
import { useRecorder } from "../voice/useRecorder";

interface Props {
  conversationId?: number;
}

export function ChatPage({ conversationId }: Props) {
  const { navigate, refreshConversations, workspaces, health, toast } = useStore();
  const [conv, setConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(Boolean(conversationId));
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [genId, setGenId] = useState<string | null>(null);
  const [draftMode, setDraftMode] = useState<Mode>("assistant");
  const [draftWs, setDraftWs] = useState<number | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [autoContext, setAutoContext] = useState(true);
  const [agentMode, setAgentMode] = useState(true);
  const [renaming, setRenaming] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (!conversationId) return;
    setLoading(true);
    try {
      const c = await api.get<Conversation>(`/api/conversations/${conversationId}`);
      setConv(c);
      setMessages(c.messages ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const mode: Mode = conv?.mode ?? draftMode;
  const wsId = conv ? conv.workspace_id : draftWs;
  const workspace = workspaces.find((w) => w.id === wsId);

  const send = async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || streaming) return;
    setError(null);
    let id = conversationId;
    if (!id) {
      try {
        const c = await api.post<Conversation>("/api/conversations", { mode: draftMode, workspace_id: draftWs });
        id = c.id;
        setConv(c);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    setInput("");
    const now = new Date().toISOString();
    const userMsg: Message = { id: -1, role: "user", content, model: null, status: "complete", context_files: [], meta: {}, created_at: now };
    const botMsg: Message = { id: -2, role: "assistant", content: "", model: null, status: "streaming", context_files: [], meta: {}, created_at: now };
    setMessages((m) => [...m, userMsg, botMsg]);
    setStreaming(true);
    const patchBot = (fn: (m: Message) => Message) => setMessages((ms) => ms.map((m) => (m.id === botMsg.id ? fn(m) : m)));
    try {
      await postStream(`/api/conversations/${id}/messages`, { content, files, auto_context: autoContext, agent: agentMode }, (ev) => {
        const d = ev.data as Record<string, unknown>;
        if (ev.event === "meta") {
          setGenId((d.generation_id as string) ?? null);
          patchBot((m) => ({
            ...m,
            model: (d.model as string) ?? null,
            context_files: (d.context_files as ContextFile[]) ?? [],
            meta: { ...m.meta, omitted: d.omitted as Message["meta"]["omitted"], action: d.action as Message["meta"]["action"], history_dropped: d.history_dropped as number },
          }));
        } else if (ev.event === "tool") {
          const step = d as unknown as ToolStep & { phase: string };
          patchBot((m) => {
            const trace = [...(m.meta.tool_trace ?? [])];
            if (step.phase === "running") trace.push({ name: step.name, arguments: step.arguments, status: "running" });
            else {
              const i = trace.map((t) => t.status).lastIndexOf("running");
              const done: ToolStep = { name: step.name, arguments: step.arguments, status: step.status, summary: step.summary, ref: step.ref };
              if (i >= 0) trace[i] = done;
              else trace.push(done);
            }
            return { ...m, meta: { ...m.meta, tool_trace: trace } };
          });
        } else if (ev.event === "notice") {
          toast({ title: "Note", body: String(d.text ?? ""), tone: "info" });
        } else if (ev.event === "token") {
          patchBot((m) => ({ ...m, content: m.content + String(d.text ?? "") }));
        } else if (ev.event === "error") {
          patchBot((m) => ({ ...m, status: "error", meta: { ...m.meta, error: String(d.message) } }));
        } else if (ev.event === "done") {
          patchBot((m) => ({ ...m, status: m.status === "error" ? "error" : (d.status as Message["status"]), meta: { ...m.meta, stats: d.stats as Message["meta"]["stats"] } }));
        }
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      patchBot((m) => ({ ...m, status: "error", meta: { ...m.meta, error: msg } }));
    } finally {
      setStreaming(false);
      setGenId(null);
      void refreshConversations();
      if (!conversationId && id) navigate(`chat/${id}`);
      else void load();
    }
  };

  const stop = async () => {
    if (genId) await api.post(`/api/generations/${genId}/stop`);
  };

  const patchConv = async (body: Record<string, unknown>) => {
    if (!conv) return;
    const c = await api.patch<Conversation>(`/api/conversations/${conv.id}`, body);
    setConv({ ...conv, ...c });
    void refreshConversations();
  };

  const remove = async () => {
    if (!conv || !window.confirm(`Delete "${conv.title}"? This cannot be undone.`)) return;
    await api.del(`/api/conversations/${conv.id}`);
    await refreshConversations();
    navigate("chat/new");
  };

  if (loading) return <div className="p-8"><Spinner /></div>;

  const wsOptions = [{ value: "", label: "No project" }, ...workspaces.map((w) => ({ value: String(w.id), label: w.name }))];

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="glass z-10 flex flex-wrap items-center gap-2 px-5 py-2.5">
          {renaming && conv ? (
            <input
              autoFocus
              defaultValue={conv.title}
              aria-label="Conversation title"
              className="h-8 rounded-lg bg-ink-850 px-2 text-sm text-slate-100"
              onBlur={(e) => { setRenaming(false); void patchConv({ title: e.target.value }); }}
              onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
            />
          ) : (
            <h1 className="truncate text-sm font-semibold text-fg">{conv?.title ?? "New conversation"}</h1>
          )}
          {conv && (
            <button aria-label="Rename" onClick={() => setRenaming(true)} className="text-slate-500 hover:text-fg">
              <Pencil className="h-3.5 w-3.5" />
            </button>
          )}
          <div className="flex-1" />
          <div className="flex rounded-full bg-ink-800 p-0.5 text-xs" role="radiogroup" aria-label="Mode">
            {(["assistant", "coding"] as Mode[]).map((m) => (
              <button
                key={m}
                role="radio"
                aria-checked={mode === m}
                onClick={() => (conv ? void patchConv({ mode: m }) : setDraftMode(m))}
                className={cn("flex items-center gap-1 rounded-full px-3 py-1", mode === m ? "bg-ink-600 text-fg shadow-sm" : "text-slate-400 hover:text-fg")}
              >
                {m === "coding" ? <Code2 className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />} {m === "coding" ? "Coding" : "Assistant"}
              </button>
            ))}
          </div>
          <div className="w-44">
            <Select
              ariaLabel="Project"
              value={wsId ? String(wsId) : ""}
              onChange={(v) => {
                setFiles([]);
                if (conv) void patchConv(v ? { workspace_id: Number(v) } : { clear_workspace: true });
                else setDraftWs(v ? Number(v) : null);
              }}
              options={wsOptions}
            />
          </div>
          {conv && (
            <>
              <a href={`/api/conversations/${conv.id}/export?format=md`} className="rounded-md p-1.5 text-slate-400 hover:bg-ink-800 hover:text-fg" title="Export as Markdown" aria-label="Export">
                <Download className="h-4 w-4" />
              </a>
              <button onClick={remove} className="rounded-md p-1.5 text-slate-400 hover:bg-ink-800 hover:text-rose-300" title="Delete conversation" aria-label="Delete conversation">
                <Trash2 className="h-4 w-4" />
              </button>
            </>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          {error && <div className="mb-4"><ErrorBanner message={error} /></div>}
          {messages.length === 0 && (
            <EmptyChat mode={mode} hasWorkspace={Boolean(workspace)} ollamaOk={Boolean(health?.ollama.available)} onPick={(t) => void send(t)} />
          )}
          <div className="mx-auto max-w-3xl space-y-5">
            {messages.map((m, i) => <MessageView key={`${m.id}-${i}`} m={m} />)}
          </div>
          <div ref={bottomRef} />
        </div>

        <Composer
          value={input}
          onChange={setInput}
          onSend={() => void send()}
          onStop={stop}
          streaming={streaming}
          disabled={Boolean(health?.paused)}
          voiceEnabled={Boolean(health?.voice.enabled)}
          onTranscript={(t) => setInput((v) => (v ? `${v} ${t}` : t))}
          onVoiceError={(msg) => toast({ title: "Voice input", body: msg, tone: "error" })}
          mode={mode}
        />
      </div>
      {workspace && mode === "coding" && (
        <ContextPanel wsId={workspace.id} wsName={workspace.name} files={files} setFiles={setFiles} auto={autoContext} setAuto={setAutoContext} query={input} agent={agentMode} setAgent={setAgentMode} />
      )}
    </div>
  );
}

function EmptyChat({ mode, hasWorkspace, ollamaOk, onPick }: { mode: Mode; hasWorkspace: boolean; ollamaOk: boolean; onPick: (t: string) => void }) {
  const ideas =
    mode === "coding"
      ? ["Explain how this project is structured", "Find likely bugs in the selected files", "Write unit tests for the selected file"]
      : ["Set a timer for 25 minutes", "What's my agenda?", "Explain the difference between a process and a thread"];
  return (
    <div className="mx-auto mt-10 max-w-xl text-center">
      <img src="/nexus.svg" alt="" className="mx-auto mb-3 h-10 w-10 opacity-90" />
      <h2 className="text-lg font-semibold text-fg">{mode === "coding" ? "Coding mode" : "How can I help?"}</h2>
      <p className="mt-1 text-sm text-slate-400">
        {mode === "coding"
          ? hasWorkspace ? "I'll use relevant files from the selected project and show you exactly which ones." : "Select a project above so I can read relevant files (read-only)."
          : "Everything runs locally on this computer. Timers, reminders and notes are handled by NEXUS itself."}
      </p>
      {!ollamaOk && <p className="mt-3 text-xs text-amber-300">Ollama is offline: model answers won't work, but timers, notes and reminders will.</p>}
      <div className="mt-5 flex flex-wrap justify-center gap-2">
        {ideas.map((t) => (
          <button key={t} onClick={() => onPick(t)} className="glass rounded-full px-3.5 py-2 text-[13px] text-slate-200 transition-transform hover:scale-[1.03] active:scale-95">
            {t}
          </button>
        ))}
      </div>
    </div>
  );
}

function MessageView({ m }: { m: Message }) {
  const isUser = m.role === "user";
  const [showCtx, setShowCtx] = useState(false);
  return (
    <article className={cn("flex gap-3", isUser && "justify-end")} aria-label={isUser ? "Your message" : "NEXUS reply"}>
      {!isUser && (
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent/15">
          {m.meta.action ? <Zap className="h-4 w-4 text-accent-soft" /> : <Bot className="h-4 w-4 text-accent-soft" />}
        </div>
      )}
      <div className={cn("min-w-0", isUser ? "flex max-w-[85%] flex-col items-end" : "flex-1")}>
        <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
          <span className="font-medium text-slate-300">{isUser ? "You" : "NEXUS"}</span>
          {m.meta.source === "voice" && <Badge tone="violet">voice</Badge>}
          {!isUser && m.model && <span>{m.model}</span>}
          {!isUser && m.meta.action && (
            <Badge tone={m.meta.action.status === "succeeded" ? "green" : "amber"}>
              action: {m.meta.action.kind.replace("_", " ")} · {m.meta.action.status}
            </Badge>
          )}
          {!isUser && m.status !== "complete" && m.status !== "streaming" && <StatusBadge status={m.status} />}
          {!isUser && m.meta.stats?.tokens_per_s && <span>{m.meta.stats.tokens_per_s.toFixed(1)} tok/s</span>}
        </div>
        {!isUser && m.meta.tool_trace && m.meta.tool_trace.length > 0 && <ToolTrace steps={m.meta.tool_trace} />}
        {isUser ? (
          <p className="whitespace-pre-wrap rounded-[22px] rounded-br-md bg-accent px-4 py-2.5 text-[15px] text-white">{m.content}</p>
        ) : m.status === "streaming" && !m.content ? (
          <p className="flex items-center gap-2 text-sm text-slate-400"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Thinking… (the first reply can take a while on CPU while the model loads)</p>
        ) : (
          <Markdown text={m.content || (m.status === "stopped" ? "_Generation stopped before any text was produced._" : "")} />
        )}
        {m.meta.error && <div className="mt-2"><ErrorBanner message={m.meta.error} /></div>}
        {!isUser && (m.context_files.length > 0 || (m.meta.omitted?.length ?? 0) > 0) && (
          <div className="mt-2">
            <button onClick={() => setShowCtx((s) => !s)} className="flex items-center gap-1 text-[11px] text-slate-400 hover:text-fg" aria-expanded={showCtx}>
              <FileText className="h-3 w-3" /> Answer used {m.context_files.length} file{m.context_files.length === 1 ? "" : "s"}
              {(m.meta.omitted?.length ?? 0) > 0 && `, ${m.meta.omitted!.length} not included`}
            </button>
            {showCtx && (
              <ul className="mt-1.5 space-y-0.5 rounded-lg border border-ink-700 bg-ink-900 p-2 text-[11px]">
                {m.context_files.map((f) => (
                  <li key={f.path} className="flex justify-between gap-3 text-slate-300">
                    <span className="truncate font-mono">{f.path}</span>
                    <span className="shrink-0 text-slate-500">{f.reason}{f.truncated ? " · truncated" : ""} · ~{f.tokens} tok</span>
                  </li>
                ))}
                {m.meta.omitted?.map((o) => (
                  <li key={o.path} className="flex justify-between gap-3 text-amber-300/80">
                    <span className="truncate font-mono">{o.path}</span>
                    <span className="shrink-0">{o.reason}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {!isUser && m.meta.semantic_note && <p className="mt-1 text-[11px] text-amber-300/80">{m.meta.semantic_note}</p>}
        {!isUser && (m.meta.history_dropped ?? 0) > 0 && (
          <p className="mt-1 text-[11px] text-slate-500">{m.meta.history_dropped} older message(s) didn't fit the model's context window and were left out.</p>
        )}
      </div>
    </article>
  );
}

const TOOL_LABEL: Record<string, string> = {
  search_code: "Search", read_file: "Read", list_files: "List files", git_status: "Git status", git_diff: "Git diff",
  propose_edit: "Propose edit", propose_command: "Request command",
};

function ToolTrace({ steps }: { steps: ToolStep[] }) {
  const { navigate } = useStore();
  return (
    <ol className="mb-2 space-y-1 rounded-lg border border-ink-700 bg-ink-900/70 p-2 text-[12px]" aria-label="Agent steps">
      {steps.map((s, i) => (
        <li key={i} className="flex flex-wrap items-center gap-2">
          {s.status === "running" ? <Loader2 className="h-3 w-3 animate-spin text-sky-400" /> : <Wrench className="h-3 w-3 text-slate-500" />}
          <span className="font-medium text-slate-300">{TOOL_LABEL[s.name] ?? s.name}</span>
          <span className="min-w-0 flex-1 truncate text-slate-400">{s.summary ?? Object.values(s.arguments ?? {}).join(" ").slice(0, 120)}</span>
          {s.status && s.status !== "running" && <StatusBadge status={s.status === "awaiting_permission" ? "awaiting_permission" : s.status} />}
          {s.ref?.changeset_id && (
            <button className="text-accent-soft underline" onClick={() => navigate(`changes/${s.ref!.changeset_id}`)}>Review change #{s.ref.changeset_id}</button>
          )}
          {s.ref?.command_id && s.status === "awaiting_permission" && (
            <button className="text-accent-soft underline" onClick={() => navigate("commands")}>Approve command #{s.ref.command_id}</button>
          )}
        </li>
      ))}
    </ol>
  );
}

function Composer(props: {
  value: string; onChange: (v: string) => void; onSend: () => void; onStop: () => void; streaming: boolean; disabled: boolean;
  voiceEnabled: boolean; onTranscript: (t: string) => void; onVoiceError: (m: string) => void; mode: Mode;
}) {
  const rec = useRecorder(props.onTranscript, props.onVoiceError);
  return (
    <div className="px-5 pb-4 pt-2">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <textarea
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              props.onSend();
            }
          }}
          rows={Math.min(8, Math.max(1, props.value.split("\n").length))}
          disabled={props.disabled}
          placeholder={props.disabled ? "NEXUS is paused" : props.mode === "coding" ? "Ask about your code… (Enter to send, Shift+Enter for a new line)" : "Ask anything, or e.g. “remind me to stretch in 30 minutes”"}
          aria-label="Message"
          className="glass max-h-60 min-h-[44px] flex-1 resize-none rounded-3xl px-4 py-3 text-[15px] text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-accent/40"
        />
        {props.voiceEnabled && (
          <button
            type="button"
            onClick={rec.recording ? rec.stop : rec.start}
            disabled={rec.busy || props.disabled}
            aria-pressed={rec.recording}
            aria-label={rec.recording ? "Stop recording" : "Push to talk"}
            title={rec.recording ? "Stop recording and transcribe" : "Push to talk (transcribed locally)"}
            className={cn(
              "glass flex h-11 w-11 items-center justify-center rounded-full transition",
              rec.recording ? "animate-pulse text-ios-red" : "text-slate-300 hover:text-accent",
            )}
          >
            {rec.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic className="h-4 w-4" />}
          </button>
        )}
        {props.streaming ? (
          <Button variant="subtle" className="h-11" onClick={props.onStop} aria-label="Stop generating">
            <Square className="h-3.5 w-3.5" /> Stop
          </Button>
        ) : (
          <Button variant="primary" className="h-11 w-11 !px-0" onClick={props.onSend} disabled={!props.value.trim() || props.disabled} aria-label="Send">
            <Send className="h-4 w-4" />
          </Button>
        )}
      </div>
      {rec.recording && <p className="mx-auto mt-1.5 max-w-3xl text-[11px] text-rose-300">● Recording from your microphone — audio is transcribed locally and not saved.</p>}
    </div>
  );
}

function ContextPanel({ wsId, wsName, files, setFiles, auto, setAuto, query, agent, setAgent }: {
  wsId: number; wsName: string; files: string[]; setFiles: (f: string[]) => void; auto: boolean; setAuto: (v: boolean) => void; query: string;
  agent: boolean; setAgent: (v: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<string[]>([]);
  const [preview, setPreview] = useState<{ included: ContextFile[]; omitted: { path: string; reason: string }[]; used_tokens: number; budget_tokens: number } | null>(null);

  useEffect(() => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    const id = setTimeout(async () => {
      try {
        const r = await api.get<{ results: { path: string }[] }>(`/api/workspaces/${wsId}/search?q=${encodeURIComponent(q)}`);
        setResults(r.results.map((x) => x.path).slice(0, 12));
      } catch {
        setResults([]);
      }
    }, 200);
    return () => clearTimeout(id);
  }, [q, wsId]);

  const runPreview = async () => {
    setPreview(await api.post(`/api/workspaces/${wsId}/context-preview`, { query, files, auto }));
  };

  return (
    <aside className="glass hidden w-72 shrink-0 flex-col p-4 lg:flex" aria-label="Context">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Context · {wsName}</p>
      <p className="mt-1 text-[11px] text-slate-500">Files are read only when you send a message. Secrets like .env are never included.</p>
      <div className="mt-3">
        <Toggle checked={agent} onChange={setAgent} label="Agent tools" description="Let the model search and read files itself, and propose edits/commands for your approval" />
        <Toggle checked={auto} onChange={setAuto} label="Auto-find relevant files" description="Keyword (and semantic, if enabled) search" />
      </div>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Add a file…"
        aria-label="Search files to add"
        className="mt-3 h-8 w-full rounded-md border border-ink-700 bg-ink-850 px-2 text-xs focus:border-accent/60 focus:outline-none"
      />
      {results.length > 0 && (
        <ul className="mt-1 max-h-40 overflow-y-auto rounded-md border border-ink-700 bg-ink-850 text-xs">
          {results.map((p) => (
            <li key={p}>
              <button
                className="w-full truncate px-2 py-1 text-left font-mono text-slate-300 hover:bg-ink-700"
                onClick={() => { if (!files.includes(p)) setFiles([...files, p]); setQ(""); }}
              >
                {p}
              </button>
            </li>
          ))}
        </ul>
      )}
      <ul className="mt-3 space-y-1">
        {files.map((f) => (
          <li key={f} className="flex items-center justify-between gap-2 rounded-md bg-ink-800 px-2 py-1 text-xs">
            <span className="truncate font-mono text-slate-200">{f}</span>
            <button aria-label={`Remove ${f}`} onClick={() => setFiles(files.filter((x) => x !== f))} className="text-slate-500 hover:text-fg">
              <X className="h-3 w-3" />
            </button>
          </li>
        ))}
        {files.length === 0 && <li className="text-[11px] text-slate-500">No files pinned.</li>}
      </ul>
      <Button size="sm" variant="outline" className="mt-3" onClick={runPreview}>Preview what will be sent</Button>
      {preview && (
        <div className="mt-2 text-[11px]">
          <p className="text-slate-400">~{preview.used_tokens} of {preview.budget_tokens} context tokens</p>
          <ul className="mt-1 space-y-0.5">
            {preview.included.map((f) => <li key={f.path} className="truncate font-mono text-slate-300">✓ {f.path}</li>)}
            {preview.omitted.map((o) => <li key={o.path} className="truncate text-amber-300/80" title={o.reason}>✗ {o.path}</li>)}
          </ul>
        </div>
      )}
    </aside>
  );
}
