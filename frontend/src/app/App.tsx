import {
  Activity, AppWindow, AudioLines, Workflow, Bell, Boxes, Cpu, FolderGit2, GitPullRequest, ListChecks, MessageSquare, Mic, MicOff, Pause, Play, Plus,
  Moon, Search, Settings, Sun, SunMoon, Terminal, Users, X,
} from "lucide-react";
import { Suspense, lazy, useEffect, useMemo, useState, type ReactNode } from "react";
import { Badge, Button, Kbd, Spinner, cn } from "../components/ui";
import { AlarmModal } from "../components/AlarmModal";
import { LiveCaption } from "../components/LiveCaption";
import { PendingCard } from "../components/PendingCard";
import { useTheme } from "../components/theme";
import { ChatPage } from "../features/chat/ChatPage";
import { Onboarding } from "../features/onboarding/Onboarding";

// Secondary pages load on demand so the chat opens fast.
const ChangesPage = lazy(() => import("../features/changes/ChangesPage").then((m) => ({ default: m.ChangesPage })));
const CommandsPage = lazy(() => import("../features/commands/CommandsPage").then((m) => ({ default: m.CommandsPage })));
const HealthPage = lazy(() => import("../features/health/HealthPage").then((m) => ({ default: m.HealthPage })));
const ModelsPage = lazy(() => import("../features/models/ModelsPage").then((m) => ({ default: m.ModelsPage })));
const PersonalPage = lazy(() => import("../features/personal/PersonalPage").then((m) => ({ default: m.PersonalPage })));
const SettingsPage = lazy(() => import("../features/settings/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const TasksPage = lazy(() => import("../features/tasks/TasksPage").then((m) => ({ default: m.TasksPage })));
const AssistantPage = lazy(() => import("../features/assistant/AssistantPage").then((m) => ({ default: m.AssistantPage })));
const PeoplePage = lazy(() => import("../features/people/PeoplePage").then((m) => ({ default: m.PeoplePage })));
const AutomationPage = lazy(() => import("../features/automation/AutomationPage").then((m) => ({ default: m.AutomationPage })));
const AppsPage = lazy(() => import("../features/apps/AppsPage").then((m) => ({ default: m.AppsPage })));
const VoicePage = lazy(() => import("../features/voice/VoicePage").then((m) => ({ default: m.VoicePage })));
const WorkspacesPage = lazy(() => import("../features/workspaces/WorkspacesPage").then((m) => ({ default: m.WorkspacesPage })));
import { api, timeAgo } from "../services/api";
import type { Conversation } from "../types";
import { CommandPalette, type PaletteItem } from "./CommandPalette";
import { useStore } from "./store";

const NAV = [
  { id: "assistant", label: "Assistant", icon: AudioLines, key: "1" },
  { id: "chat", label: "Chat", icon: MessageSquare, key: "2" },
  { id: "workspaces", label: "Projects", icon: FolderGit2, key: "3" },
  { id: "changes", label: "Changes", icon: GitPullRequest, key: "4" },
  { id: "commands", label: "Commands", icon: Terminal, key: "5" },
  { id: "tasks", label: "Tasks", icon: ListChecks, key: "6" },
  { id: "personal", label: "Notes & reminders", icon: Bell, key: "7" },
  { id: "models", label: "Models", icon: Boxes, key: "8" },
  { id: "voice", label: "Voice", icon: Mic, key: "9" },
  { id: "automation", label: "Automation", icon: Workflow, key: "" },
  { id: "apps", label: "Apps", icon: AppWindow, key: "" },
  { id: "people", label: "People & skills", icon: Users, key: "" },
  { id: "health", label: "System health", icon: Activity, key: "" },
  { id: "settings", label: "Settings", icon: Settings, key: "0" },
];

export function App() {
  const { route, navigate, health, toasts, dismissToast, backendConnected } = useStore();
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      } else if (mod && e.altKey && /^[0-9]$/.test(e.key)) {
        const item = NAV.find((n) => n.key === e.key);
        if (item) {
          e.preventDefault();
          navigate(item.id);
        }
      } else if (mod && e.shiftKey && e.key.toLowerCase() === "o") {
        e.preventDefault();
        navigate("chat/new");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  const page = route[0];
  let content: ReactNode;
  switch (page) {
    case "assistant": content = <AssistantPage />; break;
    case "workspaces": content = <WorkspacesPage id={route[1] ? Number(route[1]) : undefined} />; break;
    case "changes": content = <ChangesPage id={route[1] ? Number(route[1]) : undefined} />; break;
    case "commands": content = <CommandsPage />; break;
    case "tasks": content = <TasksPage id={route[1] ? Number(route[1]) : undefined} />; break;
    case "personal": content = <PersonalPage />; break;
    case "models": content = <ModelsPage />; break;
    case "voice": content = <VoicePage />; break;
    case "automation": content = <AutomationPage />; break;
    case "apps": content = <AppsPage />; break;
    case "people": content = <PeoplePage />; break;
    case "health": content = <HealthPage />; break;
    case "settings": content = <SettingsPage />; break;
    case "setup": content = <Onboarding />; break;
    default: content = <ChatPage key={route[1] ?? "none"} conversationId={route[1] && route[1] !== "new" ? Number(route[1]) : undefined}/>;
  }

  const showOnboarding = health && !health.onboarding_complete && (page === "chat" || page === "assistant") && !route[1];

  return (
    <div className="ambient flex h-screen overflow-hidden text-slate-200">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onPalette={() => setPaletteOpen(true)} />
        {!backendConnected && (
          <div role="alert" className="bg-ios-red/15 px-4 py-1.5 text-center text-xs text-rose-200">
            Lost connection to the NEXUS backend. It may be restarting — reconnecting automatically.
          </div>
        )}
        {health?.paused && (
          <div className="bg-ios-orange/15 px-4 py-1.5 text-center text-xs text-amber-200">
            NEXUS is paused: no new chats, tasks or voice until you resume.
          </div>
        )}
        <main className="min-h-0 flex-1 overflow-y-auto pb-16 md:pb-0">
          <Suspense fallback={<div className="p-8"><Spinner /></div>}>{showOnboarding ? <Onboarding /> : content}</Suspense>
        </main>
        <MobileTabs />
      </div>
      <AlarmModal />
      <PendingCard />
      <LiveCaption />
      {paletteOpen && <Palette onClose={() => setPaletteOpen(false)} />}
      <div className="pointer-events-none fixed right-4 top-14 z-50 flex w-80 flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="glass pointer-events-auto animate-pop rounded-2xl p-3.5 shadow-float">
            <div className="flex items-start gap-2.5">
              <span
                className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", t.tone === "error" ? "bg-ios-red" : t.tone === "success" ? "bg-ios-green" : "bg-accent")}
                aria-hidden
              />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-fg">{t.title}</p>
                {t.body && <p className="mt-0.5 whitespace-pre-wrap text-xs text-slate-300">{t.body}</p>}
              </div>
              <button aria-label="Dismiss" onClick={() => dismissToast(t.id)} className="rounded-full p-0.5 text-slate-500 hover:bg-ink-700 hover:text-fg">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const TABS = ["assistant", "chat", "workspaces", "personal"] as const;

function MobileTabs() {
  const { route, navigate } = useStore();
  return (
    <nav className="glass fixed inset-x-0 bottom-0 z-30 flex justify-around px-2 pb-[env(safe-area-inset-bottom)] pt-1.5 md:hidden" aria-label="Quick navigation">
      {TABS.map((id) => {
        const n = NAV.find((x) => x.id === id)!;
        const on = route[0] === id;
        return (
          <button key={id} onClick={() => navigate(id)} aria-current={on ? "page" : undefined}
            className={cn("flex flex-col items-center gap-0.5 rounded-xl px-3 py-1 text-[10px]", on ? "text-accent-soft" : "text-slate-400")}>
            <n.icon className="h-5 w-5" aria-hidden /> {n.label.split(" ")[0]}
          </button>
        );
      })}
    </nav>
  );
}

function Sidebar() {
  const { route, navigate, conversations } = useStore();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Conversation[] | null>(null);

  useEffect(() => {
    if (!q.trim()) {
      setResults(null);
      return;
    }
    const id = setTimeout(async () => {
      try {
        setResults(await api.get<Conversation[]>(`/api/conversations?q=${encodeURIComponent(q)}`));
      } catch {
        setResults([]);
      }
    }, 250);
    return () => clearTimeout(id);
  }, [q]);

  const list = results ?? conversations;
  const activeId = route[0] === "chat" ? Number(route[1]) : NaN;

  return (
    <aside className="glass hidden w-[264px] shrink-0 flex-col md:flex" style={{ boxShadow: "inset -0.5px 0 0 rgb(var(--hairline) / var(--hairline-alpha))" }}>
      <div className="flex items-center gap-2.5 px-5 pb-3 pt-5">
        <img src="/nexus.svg" alt="" className="h-8 w-8 rounded-[10px]" />
        <div>
          <p className="font-display text-[15px] font-bold tracking-tight text-fg">NEXUS</p>
          <p className="text-[11px] text-slate-500">Local AI partner</p>
        </div>
      </div>
      <div className="px-3">
        <Button variant="primary" className="w-full" onClick={() => navigate("chat/new")} title="New chat (Ctrl+Shift+O)">
          <Plus className="h-4 w-4" /> New chat
        </Button>
      </div>
      <nav className="mt-3 space-y-0.5 px-3" aria-label="Main">
        {NAV.map((n) => {
          const on = route[0] === n.id;
          return (
            <button
              key={n.id}
              onClick={() => navigate(n.id)}
              title={n.key ? `${n.label} (Ctrl+Alt+${n.key})` : n.label}
              aria-current={on ? "page" : undefined}
              className={cn(
                "flex w-full items-center gap-3 rounded-xl px-3 py-[7px] text-[14px] transition-colors",
                on ? "bg-accent/15 font-medium text-fg" : "text-slate-300 hover:bg-ink-700/50",
              )}
            >
              <n.icon className={cn("h-[18px] w-[18px]", on ? "text-accent" : "text-slate-400")} aria-hidden /> {n.label}
            </button>
          );
        })}
      </nav>
      <div className="mt-4 px-4">
        <div className="relative">
          <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-500" aria-hidden />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search conversations"
            aria-label="Search conversations"
            className="h-8 w-full rounded-lg bg-ink-800/70 pl-8 pr-2 text-xs text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2" aria-label="Conversations">
        {list.length === 0 && <p className="px-2 py-3 text-xs text-slate-500">{q ? "No matches." : "No conversations yet."}</p>}
        {list.map((c) => (
          <button
            key={c.id}
            onClick={() => navigate(`chat/${c.id}`)}
            className={cn("group mb-0.5 w-full rounded-xl px-3 py-1.5 text-left", activeId === c.id ? "bg-ink-700/70" : "hover:bg-ink-700/40")}
          >
            <span className={cn("block truncate text-[13px]", activeId === c.id ? "text-fg" : "text-slate-200")}>{c.title}</span>
            <span className="text-[10px] text-slate-500">{c.mode === "coding" ? "Coding" : "Assistant"} · {timeAgo(c.updated_at)}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}

function TopBar({ onPalette }: { onPalette: () => void }) {
  const { health, refreshHealth, navigate, voiceState, toast } = useStore();
  const [theme, setTheme] = useTheme();
  const ollamaOk = health?.ollama.available;
  const listening = ["listening_for_wake", "recording", "transcribing"].includes(voiceState);
  const recording = voiceState === "recording";

  const togglePause = async () => {
    await api.post(health?.paused ? "/api/system/resume" : "/api/system/pause");
    await refreshHealth();
  };
  const toggleListen = async () => {
    try {
      await api.post(listening ? "/api/voice/listen/stop" : "/api/voice/listen/start");
    } catch (e) {
      toast({ title: "Voice", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };

  return (
    <header className="glass z-20 flex h-12 shrink-0 items-center gap-1.5 px-4" style={{ boxShadow: "inset 0 -0.5px 0 rgb(var(--hairline) / var(--hairline-alpha))" }}>
      <button onClick={() => navigate("models")} className="flex items-center gap-2 rounded-full px-2.5 py-1 text-xs hover:bg-ink-700/50" title="Ollama status">
        <span className={cn("h-2 w-2 rounded-full", health ? (ollamaOk ? "bg-ios-green" : "bg-ios-red") : "bg-slate-500")} aria-hidden />
        <span className="text-slate-300">{health ? (ollamaOk ? "Local model ready" : "Ollama offline") : "Checking…"}</span>
      </button>
      <button onClick={() => navigate("models")} className="hidden items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-slate-300 hover:bg-ink-700/50 sm:flex" title="Active model">
        <Cpu className="h-3.5 w-3.5 text-slate-500" aria-hidden /> {health?.active_model ?? "—"}
      </button>
      {health && health.generations.length > 0 && (
        <Badge tone="blue">{health.generations[0].state === "loading" ? "Loading model…" : "Thinking…"}</Badge>
      )}
      <div className="flex-1" />
      <button
        onClick={onPalette}
        className="hidden items-center gap-2 rounded-full bg-ink-800/70 px-3 py-1 text-xs text-slate-400 hover:text-fg md:flex"
      >
        <Search className="h-3.5 w-3.5" /> Search or ask <Kbd>Ctrl K</Kbd>
      </button>
      {health?.voice.enabled ? (
        <button
          onClick={toggleListen}
          aria-pressed={listening}
          className={cn(
            "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs",
            recording ? "bg-ios-red/20 text-rose-200" : listening ? "bg-accent/20 text-accent-soft" : "text-slate-400 hover:bg-ink-700/50",
          )}
          title={listening ? "Stop listening for the wake word" : "Start listening for the wake word"}
        >
          {voiceState === "muted" ? <MicOff className="h-3.5 w-3.5" /> : <Mic className={cn("h-3.5 w-3.5", recording && "animate-pulse")} />}
          {recording ? "Recording" : listening ? "Listening" : voiceState === "muted" ? "Muted" : "Voice idle"}
        </button>
      ) : (
        <button onClick={() => navigate("voice")} className="flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs text-slate-500 hover:bg-ink-700/50" title="Voice is disabled">
          <MicOff className="h-3.5 w-3.5" /> Voice off
        </button>
      )}
      <button
        onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")}
        className="rounded-full p-1.5 text-slate-400 hover:bg-ink-700/50 hover:text-fg"
        title={`Appearance: ${theme} (click to change)`}
        aria-label={`Appearance: ${theme}`}
      >
        {theme === "dark" ? <Moon className="h-4 w-4" /> : theme === "light" ? <Sun className="h-4 w-4" /> : <SunMoon className="h-4 w-4" />}
      </button>
      <button
        onClick={togglePause}
        className="rounded-full p-1.5 text-slate-400 hover:bg-ink-700/50 hover:text-fg"
        title={health?.paused ? "Resume assistant" : "Pause assistant"}
        aria-label={health?.paused ? "Resume assistant" : "Pause assistant"}
      >
        {health?.paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
      </button>
    </header>
  );
}

function Palette({ onClose }: { onClose: () => void }) {
  const { navigate, conversations, workspaces } = useStore();
  const items = useMemo<PaletteItem[]>(() => {
    const go = (p: string) => () => navigate(p);
    return [
      { id: "new", label: "New chat", hint: "Ctrl+Shift+O", run: go("chat/new") },
      ...NAV.map((n) => ({ id: `nav-${n.id}`, label: `Go to ${n.label}`, hint: n.key ? `Ctrl+Alt+${n.key}` : undefined, run: go(n.id) })),
      { id: "pause", label: "Pause assistant", run: () => void api.post("/api/system/pause") },
      { id: "resume", label: "Resume assistant", run: () => void api.post("/api/system/resume") },
      { id: "stop", label: "Stop all generation", run: () => void api.post("/api/generations/stop-all") },
      { id: "setup", label: "Open setup guide", run: go("setup") },
      ...workspaces.map((w) => ({ id: `ws-${w.id}`, label: `Project: ${w.name}`, hint: w.path, run: go(`workspaces/${w.id}`) })),
      ...conversations.slice(0, 50).map((c) => ({ id: `c-${c.id}`, label: `Chat: ${c.title}`, run: go(`chat/${c.id}`) })),
    ];
  }, [navigate, conversations, workspaces]);
  const ask = (q: string) => {
    try {
      sessionStorage.setItem("nexus.pendingAsk", q);
    } catch {
      /* the question is simply not carried over */
    }
    window.dispatchEvent(new CustomEvent("nexus:ask", { detail: q }));
    navigate("assistant");
  };
  return <CommandPalette items={items} onClose={onClose} onAsk={ask} />;
}
