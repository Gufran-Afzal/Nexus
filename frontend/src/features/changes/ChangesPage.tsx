import { Check, GitPullRequest, RotateCcw, X } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { DiffView } from "../../components/DiffView";
import { Badge, Button, Card, Empty, ErrorBanner, PageHeader, Spinner, StatusBadge, cn } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api, timeAgo } from "../../services/api";
import type { ChangeSet } from "../../types";

export function ChangesPage({ id }: { id?: number }) {
  const { navigate, workspaces } = useStore();
  const list = useAsync(() => api.get<ChangeSet[]>("/api/changesets"), []);
  useEvents((e) => e.type === "changeset" && void list.reload());
  if (id) return <ChangeSetDetail id={id} onChanged={list.reload} />;
  return (
    <div className="mx-auto max-w-4xl p-6">
      <PageHeader title="Change review" subtitle="Every edit is a proposal until you accept and apply it. Nothing is written silently." />
      {list.loading && <Spinner />}
      {list.error && <ErrorBanner message={list.error} onRetry={list.reload} />}
      {list.data?.length === 0 && (
        <Empty icon={<GitPullRequest className="h-8 w-8" />} title="No change proposals">
          Open a file in a project and choose “Propose”, or run an “Implement a change” task.
        </Empty>
      )}
      <div className="space-y-2">
        {list.data?.map((cs) => (
          <button key={cs.id} onClick={() => navigate(`changes/${cs.id}`)} className="flex w-full items-center gap-3 rounded-xl border border-ink-700 bg-ink-900 px-4 py-3 text-left hover:border-accent/50">
            <span className="text-xs text-slate-500">#{cs.id}</span>
            <span className="min-w-0 flex-1 truncate text-sm text-fg">{cs.title}</span>
            <span className="text-xs text-slate-500">{workspaces.find((w) => w.id === cs.workspace_id)?.name}</span>
            <Badge tone="gray">{cs.file_count} file{cs.file_count === 1 ? "" : "s"}</Badge>
            <Badge tone="violet">{cs.source}</Badge>
            <StatusBadge status={cs.status} />
            <span className="w-20 text-right text-[11px] text-slate-500">{timeAgo(cs.created_at)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function ChangeSetDetail({ id, onChanged }: { id: number; onChanged: () => void }) {
  const { navigate, workspaces, toast } = useStore();
  const cs = useAsync(() => api.get<ChangeSet>(`/api/changesets/${id}`), [id]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ws = workspaces.find((w) => w.id === cs.data?.workspace_id);

  const act = async (fn: () => Promise<unknown>, successMsg?: string) => {
    setBusy(true);
    setErr(null);
    try {
      const r = (await fn()) as { changeset_detail?: ChangeSet } | ChangeSet;
      if ("changeset_detail" in r && r.changeset_detail) cs.setData(r.changeset_detail);
      else if ("files" in r) cs.setData(r as ChangeSet);
      if (successMsg) toast({ title: successMsg, tone: "success" });
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (cs.loading && !cs.data) return <div className="p-8"><Spinner /></div>;
  if (cs.error) return <div className="p-6"><ErrorBanner message={cs.error} /></div>;
  const c = cs.data!;
  const editable = c.status === "proposed" || c.status === "partially_applied";
  const accepted = c.files?.filter((f) => f.status === "accepted").length ?? 0;

  return (
    <div className="mx-auto max-w-5xl p-6">
      <button onClick={() => navigate("changes")} className="mb-2 text-xs text-slate-400 hover:text-fg">← All change sets</button>
      <PageHeader
        title={`#${c.id} ${c.title}`}
        subtitle={`${ws?.name ?? "project"} · proposed by ${c.source} · ${timeAgo(c.created_at)}`}
        actions={
          <>
            <StatusBadge status={c.status} />
            {editable && (
              <>
                <Button size="sm" onClick={() => act(() => api.post(`/api/changesets/${id}/decide-all`, { decision: "accepted" }))}>Accept all</Button>
                <Button size="sm" onClick={() => act(() => api.post(`/api/changesets/${id}/decide-all`, { decision: "rejected" }))}>Reject all</Button>
                <Button size="sm" variant="primary" loading={busy} disabled={!accepted} onClick={() => act(() => api.post(`/api/changesets/${id}/apply`), "Changes applied")}>
                  Apply {accepted} accepted
                </Button>
              </>
            )}
            {(c.status === "applied" || c.status === "partially_applied") && (
              <Button size="sm" onClick={() => window.confirm("Restore the original files for applied changes?") && act(() => api.post(`/api/changesets/${id}/revert`), "Changes reverted")}>
                <RotateCcw className="h-3.5 w-3.5" /> Revert
              </Button>
            )}
          </>
        }
      />
      {!ws?.allow_edits && editable && (
        <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          This project is read-only. Enable “Allow edits” in the project's settings to apply changes.{" "}
          {ws && <button className="underline" onClick={() => navigate(`workspaces/${ws.id}`)}>Open project</button>}
        </div>
      )}
      {err && <div className="mb-3"><ErrorBanner message={err} /></div>}
      {c.description && (
        <Card title="Context" className="mb-4">
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs text-slate-400">{c.description}</pre>
        </Card>
      )}
      <div className="space-y-4">
        {c.files?.map((f) => (
          <section key={f.id} className="rounded-xl border border-ink-700 bg-ink-900">
            <header className="flex flex-wrap items-center gap-2 border-b border-ink-700 px-4 py-2">
              <span className="font-mono text-sm text-fg">{f.path}</span>
              {f.is_new && <Badge tone="green">new file</Badge>}
              {f.is_delete && <Badge tone="red">delete</Badge>}
              <span className="text-xs text-emerald-300">+{f.added}</span>
              <span className="text-xs text-rose-300">−{f.removed}</span>
              <div className="flex-1" />
              <StatusBadge status={f.status} />
              {editable && ["proposed", "accepted", "rejected", "conflict"].includes(f.status) && (
                <div className="flex gap-1">
                  <button
                    aria-label={`Accept ${f.path}`}
                    onClick={() => act(() => api.post(`/api/changesets/${id}/files/${f.id}/decision`, { decision: "accepted" }))}
                    className={cn("rounded-md border p-1", f.status === "accepted" ? "border-emerald-500 bg-emerald-500/20 text-emerald-200" : "border-ink-600 text-slate-400 hover:text-fg")}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                  <button
                    aria-label={`Reject ${f.path}`}
                    onClick={() => act(() => api.post(`/api/changesets/${id}/files/${f.id}/decision`, { decision: "rejected" }))}
                    className={cn("rounded-md border p-1", f.status === "rejected" ? "border-rose-500 bg-rose-500/20 text-rose-200" : "border-ink-600 text-slate-400 hover:text-fg")}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </header>
            {f.error && <div className="px-4 pt-3"><ErrorBanner message={f.error} /></div>}
            <div className="p-3"><DiffView diff={f.diff} /></div>
          </section>
        ))}
      </div>
    </div>
  );
}
