import { Mail, MessageCircle, Pencil, Plus, Trash2, Users } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, Empty, ErrorBanner, Field, Input, PageHeader, Select, Spinner, Toggle } from "../../components/ui";
import { useAsync } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { Contact, Preferences, SkillsResponse } from "../../types";

interface Draft { id: number | null; name: string; aliases: string; phone: string; email: string }
const EMPTY: Draft = { id: null, name: "", aliases: "", phone: "", email: "" };

function PeopleCard() {
  const { toast } = useStore();
  const people = useAsync(() => api.get<Contact[]>("/api/contacts"), []);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setErr(null);
    const body = { name: draft.name, aliases: draft.aliases.split(",").map((x) => x.trim()).filter(Boolean), phone: draft.phone, email: draft.email };
    try {
      if (draft.id === null) await api.post("/api/contacts", body);
      else await api.put(`/api/contacts/${draft.id}`, body);
      setDraft(EMPTY);
      await people.reload();
      toast({ title: `${body.name} saved`, body: body.phone ? `Try: “Message ${body.name} on WhatsApp I'll be late”` : `Try: “Write an email to ${body.name} about the report”`, tone: "success" });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async (c: Contact) => {
    try {
      await api.del(`/api/contacts/${c.id}`);
      await people.reload();
    } catch (e) {
      toast({ title: "Couldn't remove", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };

  return (
    <Card title="People you can message">
      <p className="text-xs text-slate-400">Only people listed here can be messaged or emailed by voice, so “Message Mom…” never goes to the wrong person. Numbers stay on this computer. Use the country code (+44…).</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label="Name"><Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Mom" aria-label="Name" /></Field>
        <Field label="Other names (comma separated)"><Input value={draft.aliases} onChange={(e) => setDraft({ ...draft, aliases: e.target.value })} placeholder="mother, mum" aria-label="Other names" /></Field>
        <Field label="Phone (for WhatsApp)"><Input value={draft.phone} onChange={(e) => setDraft({ ...draft, phone: e.target.value })} placeholder="+1 555 123 0001" aria-label="Phone" /></Field>
        <Field label="Email (for Gmail)"><Input value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} placeholder="mom@example.com" aria-label="Email" /></Field>
      </div>
      {err && <p className="mt-2 text-xs text-rose-300" role="alert">{err}</p>}
      <div className="mt-3 flex gap-2">
        <Button size="sm" variant="primary" loading={busy} disabled={!draft.name.trim() || (!draft.phone.trim() && !draft.email.trim())} onClick={save}>
          <Plus className="h-3.5 w-3.5" /> {draft.id === null ? "Add person" : "Save changes"}
        </Button>
        {draft.id !== null && <Button size="sm" variant="ghost" onClick={() => { setDraft(EMPTY); setErr(null); }}>Cancel</Button>}
      </div>
      <div className="mt-4 border-t border-ink-700 pt-3">
        {people.error && <ErrorBanner message={people.error} onRetry={people.reload} />}
        {!people.data ? <Spinner /> : people.data.length === 0 ? (
          <Empty icon={<Users className="h-6 w-6" />} title="Nobody yet">Add someone above, then say “Message Mom on WhatsApp I'll be late”.</Empty>
        ) : (
          <ul className="divide-y divide-ink-700">
            {people.data.map((c) => (
              <li key={c.id} className="flex items-center gap-3 py-2" data-testid="person">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-100">{c.name}</p>
                  <p className="truncate text-xs text-slate-500">{[c.aliases.length ? `also “${c.aliases.join("”, “")}”` : "", c.phone ? `+${c.phone}` : "", c.email].filter(Boolean).join(" · ")}</p>
                </div>
                {c.phone && <Badge tone="green"><MessageCircle className="mr-1 inline h-3 w-3" />WhatsApp</Badge>}
                {c.email && <Badge tone="violet"><Mail className="mr-1 inline h-3 w-3" />Email</Badge>}
                <Button size="sm" variant="ghost" aria-label={`Edit ${c.name}`} onClick={() => setDraft({ id: c.id, name: c.name, aliases: c.aliases.join(", "), phone: c.phone ? `+${c.phone}` : "", email: c.email })}><Pencil className="h-3.5 w-3.5" /></Button>
                <Button size="sm" variant="ghost" aria-label={`Remove ${c.name}`} onClick={() => remove(c)}><Trash2 className="h-3.5 w-3.5" /></Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function SkillsCard() {
  const { toast } = useStore();
  const data = useAsync(() => api.get<SkillsResponse>("/api/skills"), []);
  const prefs = useAsync(() => api.get<Preferences>("/api/preferences"), []);
  const [custom, setCustom] = useState({ name: "", phrase: "", url: "" });
  const [err, setErr] = useState<string | null>(null);

  const toggle = async (id: string, enabled: boolean) => {
    try {
      data.setData(await api.put<SkillsResponse>(`/api/skills/${id}`, { enabled }));
    } catch (e) {
      toast({ title: "Couldn't save", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const pref = async (body: Partial<Preferences>) => {
    try {
      prefs.setData(await api.patch<Preferences>("/api/preferences", body));
      await data.reload();
    } catch (e) {
      toast({ title: "Couldn't save", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const addCustom = async () => {
    setErr(null);
    try {
      data.setData(await api.post<SkillsResponse>("/api/skills/custom", custom));
      setCustom({ name: "", phrase: "", url: "" });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const delCustom = async (name: string) => {
    try {
      data.setData(await api.del<SkillsResponse>(`/api/skills/custom/${encodeURIComponent(name)}`));
    } catch (e) {
      toast({ title: "Couldn't remove", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };

  if (!data.data || !prefs.data) return <Card title="What NEXUS can do inside apps">{data.error ? <ErrorBanner message={data.error} /> : <Spinner />}</Card>;
  const p = prefs.data;
  return (
    <Card title="What NEXUS can do inside apps">
      <p className="text-xs text-slate-400">NEXUS opens your own signed-in WhatsApp, Gmail, Calendar… with the draft already filled in. Nothing is sent until you say “send it” (or press the button), and only if the right window is in front. It can't read replies or incoming mail.</p>
      <ul className="mt-3 space-y-2">
        {data.data.skills.map((s) => (
          <li key={s.id} className="rounded-xl border border-ink-700 p-3" data-testid="skill">
            <Toggle checked={s.enabled} onChange={(x) => toggle(s.id, x)} label={s.title} description={s.examples.map((e) => `“${e}”`).join("  ·  ")} />
          </li>
        ))}
      </ul>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <Field label="Email with"><Select value={p.email_app} onChange={(x) => pref({ email_app: x as Preferences["email_app"] })} options={[
          { value: "gmail", label: "Gmail in my browser" }, { value: "mailto", label: "My default mail app" }]} /></Field>
        <Field label="Seconds to wait for the chat window before pressing Send">
          <Input type="number" min="0.5" max="15" step="0.5" defaultValue={p.send_key_delay_s} onBlur={(e) => pref({ send_key_delay_s: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-3">
        <Toggle checked={p.smart_routing} onChange={(x) => pref({ smart_routing: x })} label="Understand free-form requests" description="If no fixed phrase matches (“ping my mum that dinner is at seven”), let the local model pick a skill. Adds a few seconds on this PC; it can never run anything beyond the skills above." />
      </div>

      <h3 className="mt-5 text-sm font-semibold text-slate-100">Your own “open this site” commands</h3>
      <p className="text-xs text-slate-500">Say a phrase, and NEXUS opens a website search. Example: phrase “search amazon for {"{q}"}”, address https://www.amazon.com/s?k={"{q}"}</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        <Input placeholder="Name" aria-label="Skill name" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} />
        <Input placeholder="search amazon for {q}" aria-label="Skill phrase" value={custom.phrase} onChange={(e) => setCustom({ ...custom, phrase: e.target.value })} />
        <Input placeholder="https://…?q={q}" aria-label="Skill address" value={custom.url} onChange={(e) => setCustom({ ...custom, url: e.target.value })} className="font-mono" />
      </div>
      {err && <p className="mt-2 text-xs text-rose-300" role="alert">{err}</p>}
      <Button className="mt-2" size="sm" disabled={!custom.name || !custom.phrase || !custom.url} onClick={addCustom}><Plus className="h-3.5 w-3.5" /> Add command</Button>
      <ul className="mt-2 space-y-1">
        {data.data.custom.map((c) => (
          <li key={c.name} className="flex items-center gap-2 text-sm">
            <span className="font-medium text-slate-100">{c.name}</span><span className="truncate text-xs text-slate-500">“{c.phrase}” → {c.url}</span>
            <div className="flex-1" /><Button size="sm" variant="ghost" aria-label={`Remove ${c.name}`} onClick={() => delCustom(c.name)}><Trash2 className="h-3.5 w-3.5" /></Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function PeoplePage() {
  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <PageHeader title="People & skills" subtitle="Who NEXUS may message or email for you, and what it can do inside WhatsApp, Gmail, Calendar and the web." />
      <div className="grid gap-5 lg:grid-cols-2">
        <PeopleCard />
        <SkillsCard />
      </div>
    </div>
  );
}
