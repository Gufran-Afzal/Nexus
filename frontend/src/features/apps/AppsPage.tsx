import { AppWindow, Check, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, ErrorBanner, Input, PageHeader, Spinner, Toggle } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { AppsResponse, IntegratedApp } from "../../types";

const KIND_LABEL: Record<string, string> = { exe: "Program", uwp: "Store app", shortcut: "Shortcut", uri: "System" };

const CHEATSHEET: [string, string][] = [
  ["Apps", "Open Spotify · Close Chrome · Switch to Teams"],
  ["Browser", "Search for cats in Chrome · Google python dataclasses"],
  ["Music", "Play lo-fi beats on Spotify · Pause the music · Next song"],
  ["Sound", "Volume up · Volume down by 20 · Set the volume to 30 · Mute the sound"],
  ["Windows", "Lock my computer · Take a screenshot · Show the desktop · Open Bluetooth settings"],
  ["Messages", "Message Mom on WhatsApp I'll be late · then say “send it” (add people on the People page)"],
  ["Email", "Write an email to Sam asking for the project update (opens a Gmail draft; you confirm before it sends)"],
  ["Calendar & maps", "Schedule a meeting with Sam on Friday at 2pm · Directions to the airport"],
  ["Reminders", "Remind me every weekday at 9 to check email · Set an alarm for 7am · Snooze for 10 minutes"],
];

export function AppsPage() {
  const { toast } = useStore();
  const data = useAsync(() => api.get<AppsResponse>("/api/apps"), []);
  const [scanning, setScanning] = useState(false);
  const [q, setQ] = useState("");
  const [onlyNew, setOnlyNew] = useState(true);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const fail = (title: string, e: unknown) => toast({ title, body: e instanceof Error ? e.message : String(e), tone: "error" });

  const scan = async () => {
    setScanning(true);
    try {
      data.setData(await api.post<AppsResponse>("/api/apps/scan"));
    } catch (e) {
      fail("Scan failed", e);
    } finally {
      setScanning(false);
    }
  };
  const add = async (body: Record<string, unknown>, key: string) => {
    setBusy(key);
    try {
      const a = await api.post<IntegratedApp>("/api/apps/integrate", body);
      toast({ title: `${a.name} is integrated`, body: a.examples[0] ? `Try: “${a.examples[0]}”` : undefined, tone: "success" });
      await data.reload();
    } catch (e) {
      fail("Couldn't add the app", e);
    } finally {
      setBusy(null);
    }
  };
  const act = async (a: IntegratedApp, what: "open" | "close") => {
    setBusy(`${what}-${a.id}`);
    try {
      const r = await api.post<{ message: string }>(`/api/apps/integrated/${a.id}/${what}`);
      toast({ title: a.name, body: r.message, tone: "success" });
    } catch (e) {
      fail(a.name, e);
    } finally {
      setBusy(null);
    }
  };
  const patch = async (a: IntegratedApp, body: Record<string, unknown>) => {
    try {
      await api.patch(`/api/apps/integrated/${a.id}`, body);
      await data.reload();
    } catch (e) {
      fail("Couldn't save", e);
    }
  };
  const remove = async (a: IntegratedApp) => {
    try {
      await api.del(`/api/apps/integrated/${a.id}`);
      await data.reload();
    } catch (e) {
      fail("Couldn't remove", e);
    }
  };

  const shown = useMemo(() => {
    const term = q.trim().toLowerCase();
    return (data.data?.discovered ?? []).filter((a) => (!onlyNew || !a.integrated) && (!term || a.name.toLowerCase().includes(term)));
  }, [data.data, q, onlyNew]);

  if (!data.data) return <div className="p-8">{data.error ? <ErrorBanner message={data.error} onRetry={data.reload} /> : <Spinner />}</div>;
  const d = data.data;
  const addable = shown.filter((a) => !a.integrated);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <PageHeader
        title="Apps"
        subtitle="Tick the Windows apps NEXUS may use. Then just say “open Spotify”, “close Chrome” or “search for cats in Chrome”: no program names to type."
        actions={<Button variant="primary" loading={scanning} onClick={scan}><RefreshCw className="h-4 w-4" /> Scan this PC</Button>}
      />
      {d.error && <ErrorBanner message={d.error} />}

      <Card title={`Integrated apps (${d.integrated.length})`}>
        {d.integrated.length === 0 ? (
          <Empty icon={<AppWindow className="h-6 w-6" />} title="No apps yet">Add apps from the list below.</Empty>
        ) : (
          <ul className="divide-y divide-ink-700">
            {d.integrated.map((a) => (
              <li key={a.id} className="space-y-2 py-3" data-testid="integrated-app">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-fg">{a.name}</span>
                  <Badge>{KIND_LABEL[a.kind] ?? a.kind}</Badge>
                  {!a.enabled && <Badge tone="amber">off</Badge>}
                  <div className="flex-1" />
                  <Toggle checked={a.enabled} onChange={(v) => patch(a, { enabled: v })} label={`Allow ${a.name}`} />
                  <Button size="sm" loading={busy === `open-${a.id}`} disabled={!a.enabled} onClick={() => act(a, "open")}>Open</Button>
                  {a.can_close && <Button size="sm" variant="ghost" loading={busy === `close-${a.id}`} onClick={() => act(a, "close")}>Close</Button>}
                  <Button size="sm" variant="ghost" aria-label={`Remove ${a.name}`} onClick={() => remove(a)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-slate-500">Also called</span>
                  <Input
                    className="h-8 max-w-xs text-xs"
                    aria-label={`Other names for ${a.name}`}
                    placeholder="e.g. music, browser (comma-separated)"
                    defaultValue={a.aliases.join(", ")}
                    onBlur={(e) => {
                      const next = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
                      if (next.join("|") !== a.aliases.join("|")) void patch(a, { aliases: next });
                    }}
                  />
                </div>
                <p className="text-xs text-slate-500">Say: {a.examples.map((e) => `“${e}”`).join(" · ")}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title={`Apps found on this PC (${d.discovered.length})`}
        actions={<span className="text-xs text-slate-500">{d.scanned_at ? `Scanned ${new Date(d.scanned_at * 1000).toLocaleString()}` : "Not scanned yet"}</span>}
      >
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-48 flex-1">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" aria-hidden />
            <Input className="pl-9" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search apps…" aria-label="Search apps" />
          </div>
          <Toggle checked={onlyNew} onChange={setOnlyNew} label="Hide integrated" />
          <Button size="sm" variant="subtle" disabled={addable.length === 0}
            onClick={async () => {
              setBusy("many");
              try {
                const r = await api.post<{ added: number }>("/api/apps/integrate-many", { ids: addable.map((a) => a.id) });
                toast({ title: `${r.added} apps integrated`, tone: "success" });
                await data.reload();
              } catch (e) {
                fail("Couldn't add apps", e);
              } finally {
                setBusy(null);
              }
            }}
            loading={busy === "many"}>
            Add all {addable.length > 0 ? `(${addable.length})` : ""}
          </Button>
        </div>
        <ul className="mt-3 max-h-96 divide-y divide-ink-700 overflow-y-auto" data-testid="discovered-list">
          {shown.length === 0 && <li className="py-4 text-center text-xs text-slate-500">{d.discovered.length ? "No matching apps." : "Click “Scan this PC”."}</li>}
          {shown.map((a) => (
            <li key={a.id} className="flex items-center gap-2 py-2 text-sm">
              <span className="min-w-0 flex-1 truncate text-slate-100">{a.name}</span>
              <Badge>{KIND_LABEL[a.kind] ?? a.kind}</Badge>
              {a.integrated ? (
                <span className="flex w-24 items-center justify-end gap-1 text-xs text-emerald-400"><Check className="h-3.5 w-3.5" /> Integrated</span>
              ) : (
                <Button size="sm" variant="subtle" className="w-24" loading={busy === a.id} onClick={() => add({ id: a.id }, a.id)} aria-label={`Add ${a.name}`}>
                  <Plus className="h-3.5 w-3.5" /> Add
                </Button>
              )}
            </li>
          ))}
        </ul>
        <div className="mt-4 border-t border-ink-700 pt-3">
          <p className="mb-2 text-xs text-slate-400">Not in the list? Type its name (NEXUS finds the program itself) or give the .exe path.</p>
          <div className="flex flex-wrap gap-2">
            <Input className="w-48" value={name} onChange={(e) => setName(e.target.value)} placeholder="App name" aria-label="App name" />
            <Input className="min-w-56 flex-1" value={path} onChange={(e) => setPath(e.target.value)} placeholder="Optional: C:\Program Files\…\app.exe" aria-label="Program path" />
            <Button variant="primary" disabled={!name.trim() && !path.trim()} loading={busy === "manual"}
              onClick={async () => { await add({ name: name.trim() || undefined, path: path.trim() || undefined }, "manual"); setName(""); setPath(""); }}>
              <Plus className="h-4 w-4" /> Add
            </Button>
          </div>
        </div>
      </Card>

      <Card title="What you can say">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[7rem_1fr]">
          {CHEATSHEET.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">{k}</dt>
              <dd className="text-slate-300">{v}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3 text-[11px] text-slate-500">
          These work by typing in Chat/Assistant or by voice. App commands only work for apps ticked above. NEXUS never sends email, never
          force-closes a program (it asks it to close, so it can offer to save) and has no shutdown or restart command.
        </p>
      </Card>
    </div>
  );
}
