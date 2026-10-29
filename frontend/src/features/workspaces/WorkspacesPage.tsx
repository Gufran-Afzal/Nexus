import { ChevronDown, ChevronRight, File, Folder, FolderPlus, GitBranch, Search, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useStore } from "../../app/store";
import { DiffView } from "../../components/DiffView";
import { Markdown } from "../../components/Markdown";
import { Badge, Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, Spinner, Textarea, Toggle, cn } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api, formatBytes } from "../../services/api";
import type { ChangeSet, TreeNode, Workspace } from "../../types";

export function WorkspacesPage({ id }: { id?: number }) {
  const { workspaces, refreshWorkspaces, navigate } = useStore();
  const [path, setPath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const add = async () => {
    setAdding(true);
    setErr(null);
    try {
      const w = await api.post<Workspace>("/api/workspaces", { path });
      setPath("");
      await refreshWorkspaces();
      navigate(`workspaces/${w.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setAdding(false);
    }
  };

  if (id) {
    const ws = workspaces.find((w) => w.id === id);
    if (!ws) return <div className="p-8"><Spinner /></div>;
    return <WorkspaceDetail ws={ws} />;
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <PageHeader title="Projects" subtitle="Folders you explicitly add. NEXUS never scans your disk on its own." />
      <Card title="Add a project folder">
        <div className="flex gap-2">
          <Input value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\\Users\\you\\code\\my-project" aria-label="Project folder path" onKeyDown={(e) => e.key === "Enter" && path && void add()} />
          <Button variant="primary" onClick={add} loading={adding} disabled={!path.trim()}>
            <FolderPlus className="h-4 w-4" /> Add
          </Button>
        </div>
        <p className="mt-2 text-xs text-slate-500">Tip: in File Explorer, Shift+right-click the folder → “Copy as path”, then paste here. New projects are read-only until you allow edits or commands.</p>
        {err && <div className="mt-3"><ErrorBanner message={err} /></div>}
      </Card>
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        {workspaces.map((w) => (
          <button key={w.id} onClick={() => navigate(`workspaces/${w.id}`)} className="rounded-xl border border-ink-700 bg-ink-900 p-4 text-left hover:border-accent/50">
            <div className="flex items-center gap-2">
              <Folder className="h-4 w-4 text-accent-soft" />
              <span className="font-medium text-fg">{w.name}</span>
              {!w.exists && <Badge tone="red">missing</Badge>}
            </div>
            <p className="mt-1 truncate font-mono text-[11px] text-slate-500">{w.path}</p>
            <div className="mt-2 flex gap-1.5">
              <Badge tone={w.allow_edits ? "amber" : "gray"}>{w.allow_edits ? "edits allowed" : "read-only"}</Badge>
              <Badge tone={w.allow_commands ? "amber" : "gray"}>{w.allow_commands ? "commands allowed" : "no commands"}</Badge>
              {w.has_instructions && <Badge tone="violet">NEXUS.md</Badge>}
            </div>
          </button>
        ))}
      </div>
      {workspaces.length === 0 && <Empty icon={<Folder className="h-8 w-8" />} title="No projects yet">Add a folder above to let NEXUS read it when you ask.</Empty>}
    </div>
  );
}

function WorkspaceDetail({ ws }: { ws: Workspace }) {
  const { refreshWorkspaces, navigate, toast } = useStore();
  const tree = useAsync(() => api.get<{ tree: TreeNode; truncated: boolean; file_count: number }>(`/api/workspaces/${ws.id}/tree`), [ws.id]);
  const git = useAsync(() => api.get<{ available: boolean; reason?: string; branch?: string; changes?: { status: string; path: string }[]; diff?: string }>(`/api/workspaces/${ws.id}/git`), [ws.id]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<"files" | "search" | "git" | "settings">("files");

  useEffect(() => {
    void api.get(`/api/workspaces/${ws.id}`); // marks as recently opened
  }, [ws.id]);

  const patch = async (body: Partial<Workspace>) => {
    try {
      await api.patch(`/api/workspaces/${ws.id}`, body);
      await refreshWorkspaces();
    } catch (e) {
      toast({ title: "Could not update project", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-5 py-3">
        <button onClick={() => navigate("workspaces")} className="text-xs text-slate-400 hover:text-fg">Projects</button>
        <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
        <h1 className="font-semibold text-fg">{ws.name}</h1>
        <span className="truncate font-mono text-[11px] text-slate-500">{ws.path}</span>
        <div className="flex-1" />
        <Button size="sm" variant="primary" onClick={async () => {
          const c = await api.post<{ id: number }>("/api/conversations", { mode: "coding", workspace_id: ws.id });
          navigate(`chat/${c.id}`);
        }}>Ask about this project</Button>
      </div>
      <div className="flex gap-1 border-b border-ink-700 px-5" role="tablist">
        {(["files", "search", "git", "settings"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={cn("border-b-2 px-3 py-2 text-sm capitalize", tab === t ? "border-accent text-fg" : "border-transparent text-slate-400 hover:text-fg")}>
            {t === "git" ? "Git" : t}
          </button>
        ))}
      </div>
      {!ws.exists && <div className="p-4"><ErrorBanner message={`This folder no longer exists: ${ws.path}`} /></div>}
      {tab === "files" && (
        <div className="flex min-h-0 flex-1">
          <div className="w-72 shrink-0 overflow-y-auto border-r border-ink-700 p-2 text-sm">
            {tree.loading && <Spinner />}
            {tree.error && <ErrorBanner message={tree.error} onRetry={tree.reload} />}
            {tree.data && (
              <>
                <p className="px-2 pb-1 text-[11px] text-slate-500">{tree.data.file_count} files{tree.data.truncated ? " (list truncated)" : ""} · secrets & build output hidden</p>
                {tree.data.tree.children?.map((n) => <TreeItem key={n.path} node={n} depth={0} selected={selected} onSelect={setSelected} />)}
              </>
            )}
          </div>
          <div className="min-w-0 flex-1 overflow-y-auto p-4">
            {selected ? <FilePreview wsId={ws.id} path={selected} allowEdits={ws.allow_edits} /> : <Empty icon={<File className="h-8 w-8" />} title="Select a file to preview" />}
          </div>
        </div>
      )}
      {tab === "search" && <SearchTab wsId={ws.id} onOpen={(p) => { setSelected(p); setTab("files"); }} />}
      {tab === "git" && (
        <div className="overflow-y-auto p-5">
          {git.loading && <Spinner />}
          {git.data && !git.data.available && <Empty icon={<GitBranch className="h-8 w-8" />} title="Git status unavailable">{git.data.reason}</Empty>}
          {git.data?.available && (
            <div className="space-y-4">
              <p className="text-sm text-slate-300">Branch: <span className="font-mono text-fg">{git.data.branch}</span></p>
              <ul className="space-y-0.5 text-xs">
                {git.data.changes?.map((c) => <li key={c.path} className="font-mono"><span className="inline-block w-6 text-amber-300">{c.status}</span>{c.path}</li>)}
                {git.data.changes?.length === 0 && <li className="text-slate-500">Working tree clean.</li>}
              </ul>
              {git.data.diff && <DiffView diff={git.data.diff} />}
            </div>
          )}
        </div>
      )}
      {tab === "settings" && <SettingsTab ws={ws} patch={patch} />}
    </div>
  );
}

function TreeItem({ node, depth, selected, onSelect }: { node: TreeNode; depth: number; selected: string | null; onSelect: (p: string) => void }) {
  const [open, setOpen] = useState(depth < 1);
  if (node.type === "dir") {
    return (
      <div>
        <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-slate-300 hover:bg-ink-800" style={{ paddingLeft: depth * 12 + 4 }} aria-expanded={open}>
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <Folder className="h-3.5 w-3.5 text-accent-soft/80" /> <span className="truncate">{node.name}</span>
        </button>
        {open && node.children?.map((c) => <TreeItem key={c.path} node={c} depth={depth + 1} selected={selected} onSelect={onSelect} />)}
      </div>
    );
  }
  return (
    <button onClick={() => onSelect(node.path)} className={cn("flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left", selected === node.path ? "bg-accent/15 text-fg" : "text-slate-400 hover:bg-ink-800")} style={{ paddingLeft: depth * 12 + 20 }}>
      <File className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">{node.name}</span>
    </button>
  );
}

const EXT_LANG: Record<string, string> = { py: "python", ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", json: "json", md: "markdown", rs: "rust", go: "go", java: "java", cs: "csharp", cpp: "cpp", c: "c", html: "html", css: "css", yml: "yaml", yaml: "yaml", toml: "toml", sh: "bash", ps1: "powershell", sql: "sql" };

function FilePreview({ wsId, path, allowEdits }: { wsId: number; path: string; allowEdits: boolean }) {
  const { navigate, toast } = useStore();
  const file = useAsync(() => api.get<{ path: string; content: string; size: number }>(`/api/workspaces/${wsId}/file?path=${encodeURIComponent(path)}`), [wsId, path]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [instruction, setInstruction] = useState("");
  const [generating, setGenerating] = useState(false);
  const lang = EXT_LANG[path.split(".").pop() ?? ""] ?? "";

  const proposeManual = async () => {
    try {
      const cs = await api.post<ChangeSet>("/api/changesets", { workspace_id: wsId, title: `Edit ${path}`, files: [{ path, new_content: draft }] });
      navigate(`changes/${cs.id}`);
    } catch (e) {
      toast({ title: "Could not create proposal", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const proposeAI = async () => {
    setGenerating(true);
    try {
      const r = await api.post<{ changeset: ChangeSet | null; notes: string[] }>("/api/changesets/generate", { workspace_id: wsId, instruction, files: [path] });
      if (r.changeset) navigate(`changes/${r.changeset.id}`);
      else toast({ title: "No change proposed", body: r.notes.join("\n") || "The model did not suggest an edit.", tone: "info" });
    } catch (e) {
      toast({ title: "Model could not propose an edit", body: e instanceof Error ? e.message : String(e), tone: "error" });
    } finally {
      setGenerating(false);
    }
  };

  if (file.loading) return <Spinner />;
  if (file.error) return <ErrorBanner message={file.error} />;
  if (!file.data) return null;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm text-fg">{file.data.path}</span>
        <span className="text-xs text-slate-500">{formatBytes(file.data.size)}</span>
        <div className="flex-1" />
        {!editing && <Button size="sm" onClick={() => { setDraft(file.data!.content); setEditing(true); }}>Edit (as proposal)</Button>}
      </div>
      <div className="flex gap-2">
        <Input value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="Describe a change for the AI to propose for this file…" aria-label="Change instruction" />
        <Button variant="primary" onClick={proposeAI} loading={generating} disabled={instruction.trim().length < 3}>
          <Sparkles className="h-4 w-4" /> Propose
        </Button>
      </div>
      {!allowEdits && <p className="text-[11px] text-slate-500">Proposals can be reviewed now; applying them requires enabling “Allow edits” in this project's Settings tab.</p>}
      {editing ? (
        <div className="space-y-2">
          <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={24} className="font-mono text-[12.5px]" aria-label="File content" spellCheck={false} />
          <div className="flex gap-2">
            <Button variant="primary" onClick={proposeManual} disabled={draft === file.data.content}>Create change proposal</Button>
            <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <Markdown text={"```" + lang + "\n" + file.data.content + "\n```"} />
      )}
    </div>
  );
}

function SearchTab({ wsId, onOpen }: { wsId: number; onOpen: (p: string) => void }) {
  const [q, setQ] = useState("");
  const [content, setContent] = useState(true);
  const [res, setRes] = useState<{ results: { path: string; line?: number; text?: string }[]; scanned: number; truncated: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const run = async () => {
    setLoading(true);
    try {
      setRes(await api.get(`/api/workspaces/${wsId}/search?q=${encodeURIComponent(q)}&content=${content}`));
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="overflow-y-auto p-5">
      <div className="flex gap-2">
        <Input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q && void run()} placeholder="Search…" aria-label="Search query" />
        <Button onClick={run} loading={loading} disabled={!q}><Search className="h-4 w-4" /> Search</Button>
      </div>
      <div className="mt-2 w-60"><Toggle checked={content} onChange={setContent} label="Search file contents" /></div>
      {res && (
        <div className="mt-3">
          <p className="mb-2 text-xs text-slate-500">{res.results.length} results · {res.scanned} files scanned{res.truncated ? " · truncated" : ""}</p>
          <ul className="space-y-1">
            {res.results.map((r, i) => (
              <li key={i}>
                <button onClick={() => onOpen(r.path)} className="w-full rounded-md px-2 py-1 text-left hover:bg-ink-800">
                  <span className="font-mono text-xs text-accent-soft">{r.path}{r.line ? `:${r.line}` : ""}</span>
                  {r.text && <span className="block truncate font-mono text-xs text-slate-400">{r.text}</span>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SettingsTab({ ws, patch }: { ws: Workspace; patch: (b: Partial<Workspace>) => Promise<void> }) {
  const { refreshWorkspaces, navigate, toast } = useStore();
  const [excludes, setExcludes] = useState(ws.exclude_globs.join("\n"));
  const [summary, setSummary] = useState(ws.summary ?? "");
  const [drafting, setDrafting] = useState(false);
  const instructions = useAsync(() => api.get<{ content: string | null }>(`/api/workspaces/${ws.id}/instructions`), [ws.id]);
  const dirty = useMemo(() => summary !== (ws.summary ?? ""), [summary, ws.summary]);

  const draft = async () => {
    setDrafting(true);
    try {
      const r = await api.post<{ draft: string }>(`/api/workspaces/${ws.id}/summarize`);
      setSummary(r.draft);
      toast({ title: "Summary drafted", body: "Review and edit it, then save to approve it.", tone: "info" });
    } catch (e) {
      toast({ title: "Could not draft summary", body: e instanceof Error ? e.message : String(e), tone: "error" });
    } finally {
      setDrafting(false);
    }
  };

  return (
    <div className="grid gap-4 overflow-y-auto p-5 lg:grid-cols-2">
      <Card title="Permissions">
        <Toggle checked={ws.allow_edits} onChange={(v) => void patch({ allow_edits: v })} label="Allow edits" description="Lets you apply change proposals you've reviewed. Each change set still needs your approval." />
        <Toggle checked={ws.allow_commands} onChange={(v) => void patch({ allow_commands: v })} label="Allow commands" description="Lets you approve commands (tests, linters) to run in this folder. Each command still needs approval." />
        <p className="mt-2 text-[11px] text-slate-500">NEXUS never runs commands with administrator rights and never runs a shell. This is approval-gated execution, not a sandbox.</p>
      </Card>
      <Card title="Project instructions (NEXUS.md)">
        {instructions.data?.content ? <pre className="max-h-48 overflow-auto whitespace-pre-wrap text-xs text-slate-300">{instructions.data.content}</pre> : <p className="text-xs text-slate-500">No NEXUS.md found. Create one in the project root to describe conventions (it can't change NEXUS's security rules).</p>}
      </Card>
      <Card title="Approved project summary" actions={<Button size="sm" onClick={draft} loading={drafting}><Sparkles className="h-3.5 w-3.5" /> Draft with AI</Button>}>
        <Textarea value={summary} onChange={(e) => setSummary(e.target.value)} rows={7} placeholder="A short description NEXUS includes in coding conversations." aria-label="Project summary" />
        <Button className="mt-2" size="sm" variant="primary" disabled={!dirty} onClick={() => void patch({ summary })}>Save summary</Button>
      </Card>
      <Card title="Extra exclusions">
        <Field label="Glob patterns, one per line" hint="Secrets, .git, node_modules, build output are always excluded.">
          <Textarea value={excludes} onChange={(e) => setExcludes(e.target.value)} rows={5} placeholder={"docs/generated/*\n*.csv"} />
        </Field>
        <Button className="mt-2" size="sm" onClick={() => void patch({ exclude_globs: excludes.split("\n").map((s) => s.trim()).filter(Boolean) })}>Save exclusions</Button>
      </Card>
      <SemanticCard wsId={ws.id} />
      <Card title="Remove project">
        <p className="mb-2 text-xs text-slate-400">Removes the project from NEXUS (and its project conversations' link). Files on disk are not touched.</p>
        <Button variant="danger" size="sm" onClick={async () => {
          if (!window.confirm(`Remove ${ws.name} from NEXUS?`)) return;
          await api.del(`/api/workspaces/${ws.id}`);
          await refreshWorkspaces();
          navigate("workspaces");
        }}><Trash2 className="h-3.5 w-3.5" /> Remove</Button>
      </Card>
    </div>
  );
}

interface SemanticStatus {
  chunks: number; files: number; model: string | null; enabled: boolean; embedding_model: string;
  job: { state: string; files_total: number; files_done: number; chunks: number; error: string | null } | null;
}

function SemanticCard({ wsId }: { wsId: number }) {
  const { toast } = useStore();
  const st = useAsync(() => api.get<SemanticStatus>(`/api/workspaces/${wsId}/semantic-index`), [wsId]);
  useEvents((e) => e.type === "index" && e.workspace_id === wsId && void st.reload());
  const running = st.data?.job?.state === "running";
  const call = async (method: "post" | "del", path: string) => {
    try {
      if (method === "post") await api.post(path);
      else await api.del(path);
      await st.reload();
    } catch (e) {
      toast({ title: "Semantic search", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const d = st.data;
  const pct = d?.job && d.job.files_total ? Math.round((d.job.files_done / d.job.files_total) * 100) : null;
  return (
    <Card title="Semantic search (optional)">
      <p className="text-xs text-slate-400">
        Finds code by meaning (“where do we format prices?”), using a small local embedding model
        ({d?.embedding_model ?? "nomic-embed-text"}). Combined with keyword search. Needs a one-time
        <code className="mx-1 text-slate-200">ollama pull {d?.embedding_model ?? "nomic-embed-text"}</code>.
      </p>
      {d && (
        <p className="mt-2 text-xs text-slate-300">
          {d.enabled ? "Enabled" : "Off"} · {d.chunks} chunks from {d.files} files{d.model ? ` · ${d.model}` : ""}
        </p>
      )}
      {running && pct !== null && (
        <div className="mt-2">
          <p className="text-[11px] text-slate-400">Indexing {d!.job!.files_done}/{d!.job!.files_total} files…</p>
          <div className="mt-1 h-1.5 overflow-hidden rounded bg-ink-700"><div className="h-full bg-accent" style={{ width: `${pct}%` }} /></div>
        </div>
      )}
      {d?.job?.error && <p className="mt-2 text-xs text-rose-300">{d.job.error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" variant="primary" loading={running} onClick={() => call("post", `/api/workspaces/${wsId}/semantic-index`)}>
          <Sparkles className="h-3.5 w-3.5" /> {d?.chunks ? "Update index" : "Build index"}
        </Button>
        {running && <Button size="sm" onClick={() => call("post", `/api/workspaces/${wsId}/semantic-index/cancel`)}>Cancel</Button>}
        {Boolean(d?.chunks || d?.enabled) && !running && <Button size="sm" variant="ghost" onClick={() => call("del", `/api/workspaces/${wsId}/semantic-index`)}>Turn off & delete index</Button>}
      </div>
    </Card>
  );
}
