import { Calculator, Flame, MicOff, Mic, SendHorizontal, Sparkles, Square, Timer, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useStore } from "../../app/store";
import { Kbd, cn } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api, postStream } from "../../services/api";
import type { Conversation, Reminder, VoiceStatus } from "../../types";
import { useRecorder } from "../voice/useRecorder";
import { makeChunker } from "./chunker";
import { Orb, type OrbState } from "./Orb";
import { ClockWidget, ModelWidget, NotesWidget, SystemWidget, TimersWidget, TodoWidget } from "./widgets";

const CONV_KEY = "nexus.assistantConversation";
const SPEAK_KEY = "nexus.speakReplies";

interface Turn {
  id: number;
  role: "user" | "assistant";
  text: string;
  note?: string; // e.g. "Timer set"
  error?: boolean;
}

const SUGGESTIONS = [
  { text: "Set a timer for 10 minutes", icon: Timer },
  { text: "Start a focus session", icon: Flame },
  { text: "What's on my agenda today?", icon: Sparkles },
  { text: "What is 18% of 240?", icon: Calculator },
  { text: "How's my computer doing?", icon: Sparkles },
];

const ACTION_LABEL: Record<string, string> = {
  timer: "Timer set", reminder: "Reminder set", note: "Note saved", todo: "To-do added", agenda: "Agenda",
  open_app: "App opened", open_url: "Link opened", open_folder: "Folder opened",
  time: "Instant", date: "Instant", math: "Calculated", convert: "Converted", list_reminders: "Timers", time_left: "Timers",
  cancel_reminders: "Cancelled", list_todos: "To-do", done_todo: "Done", list_notes: "Notes", search_notes: "Notes",
  focus: "Focus started", system: "System", coin: "Instant", dice: "Instant", greeting: "Instant", thanks: "Instant",
  capabilities: "Instant", remember: "Remembered", recall: "Memory", forget: "Forgotten",
};

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: fine */
  }
}

async function ensureConversation(): Promise<number> {
  const saved = Number(read(CONV_KEY));
  if (saved) {
    try {
      const c = await api.get<Conversation>(`/api/conversations/${saved}`);
      return c.id;
    } catch {
      /* deleted: make a new one */
    }
  }
  const c = await api.post<Conversation>("/api/conversations", { mode: "assistant", title: "Assistant" });
  write(CONV_KEY, String(c.id));
  return c.id;
}

function greeting(d: Date): string {
  const h = d.getHours();
  return h < 5 ? "Still up?" : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.";
}

export function AssistantPage() {
  const { health, voiceState, navigate, toast, refreshConversations, refreshHealth } = useStore();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [gotToken, setGotToken] = useState(false);
  const [speak, setSpeak] = useState(read(SPEAK_KEY) !== "off");
  const [speakingNow, setSpeakingNow] = useState(false);
  const convId = useRef<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  const seq = useRef(1);
  const input = useRef<HTMLInputElement>(null);
  const idleLevel = useRef(0);

  const voice = useAsync(() => api.get<VoiceStatus>("/api/voice/status"), []);
  const reminders = useAsync(() => api.get<Reminder[]>("/api/reminders"), []);
  const agenda = useAsync(() => api.get<{ text: string }>("/api/agenda"), []);
  useEvents((e) => {
    if (e.type === "reminder" || e.type === "voice") {
      void reminders.reload();
      void agenda.reload();
    }
  });

  // Restore the last few turns of the persistent assistant conversation.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const id = await ensureConversation();
        convId.current = id;
        const c = await api.get<Conversation>(`/api/conversations/${id}`);
        if (!alive) return;
        setTurns(
          (c.messages ?? []).slice(-6).filter((m) => m.role !== "system").map((m) => ({
            id: seq.current++, role: m.role as "user" | "assistant", text: m.content || (m.meta.error ?? ""), error: m.status === "error",
          })),
        );
      } catch (e) {
        toast({ title: "Assistant", body: e instanceof Error ? e.message : String(e), tone: "error" });
      }
    })();
    return () => { alive = false; };
  }, [toast]);

  // Hands-free turns (wake word) arrive as events: show what was heard and what NEXUS answered.
  useEvents((ev) => {
    if (ev.type !== "voice" || typeof ev.text !== "string" || !ev.text) return;
    const text = ev.text;
    if (ev.state === "transcribed") {
      setTurns((t) => [...t.slice(-5), { id: seq.current++, role: "user", text }]);
    } else if (ev.state === "answered") {
      setTurns((t) => [...t.slice(-5), { id: seq.current++, role: "assistant", text }]);
      void reminders.reload();
      void agenda.reload();
    }
  });

  // Spoken replies are queued sentence by sentence on the server, so the first sentence is heard while the model
  // is still writing the rest.
  const sayPiece = useCallback((text: string) => {
    setSpeakingNow(true);
    api.post("/api/voice/speak", { text, wait: false }).catch(() => {
      /* speech output is optional; the reply is already on screen */
    });
  }, []);

  useEffect(() => {
    if (!speakingNow || streaming || voiceState === "speaking") return;
    const id = setTimeout(() => setSpeakingNow(false), 2500); // the server confirms "speaking" quickly; otherwise give up
    return () => clearTimeout(id);
  }, [speakingNow, streaming, voiceState]);

  const send = useCallback(async (raw: string) => {
    const content = raw.trim();
    if (!content || streaming) return;
    setDraft("");
    const uid = seq.current++;
    const aid = seq.current++;
    setTurns((t) => [...t.slice(-5), { id: uid, role: "user", text: content }, { id: aid, role: "assistant", text: "" }]);
    setStreaming(true);
    setGotToken(false);
    const ctl = new AbortController();
    abort.current = ctl;
    let reply = "";
    const talk = speak && voice.data?.tts.available ? makeChunker() : null;
    try {
      if (convId.current === null) convId.current = await ensureConversation();
      await postStream(`/api/conversations/${convId.current}/messages`, { content, auto_context: false, agent: false }, (ev) => {
        const d = ev.data as Record<string, unknown>;
        if (ev.event === "meta" && d.action) {
          const kind = String((d.action as { kind?: string }).kind ?? "");
          setTurns((t) => t.map((x) => (x.id === aid ? { ...x, note: ACTION_LABEL[kind] ?? "Done" } : x)));
        } else if (ev.event === "token") {
          reply += String(d.text ?? "");
          talk?.feed(String(d.text ?? "")).forEach(sayPiece);
          setGotToken(true);
          setTurns((t) => t.map((x) => (x.id === aid ? { ...x, text: reply } : x)));
        } else if (ev.event === "error") {
          setTurns((t) => t.map((x) => (x.id === aid ? { ...x, text: String(d.message ?? "Something went wrong"), error: true } : x)));
        }
      }, ctl.signal);
      void reminders.reload();
      void agenda.reload();
      void refreshConversations();
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setTurns((t) => t.map((x) => (x.id === aid ? { ...x, text: e instanceof Error ? e.message : String(e), error: true } : x)));
      }
    } finally {
      setStreaming(false);
      abort.current = null;
    }
    talk?.flush().forEach(sayPiece);
  }, [streaming, speak, voice.data, sayPiece, reminders, agenda, refreshConversations]);

  const rec = useRecorder(
    useCallback((t: string) => void send(t), [send]),
    useCallback((m: string) => toast({ title: "Voice", body: m, tone: "error" }), [toast]),
  );

  const interrupt = useCallback(() => {
    abort.current?.abort();
    void api.post("/api/generations/stop-all").catch(() => {});
    void api.post("/api/voice/interrupt").catch(() => {});
    setSpeakingNow(false);
  }, []);

  const enableVoice = useCallback(async () => {
    try {
      await api.patch("/api/preferences", { voice: { enabled: true, listening_mode: "wake_word" } });
      await Promise.all([refreshHealth(), voice.reload()]);
      return true;
    } catch (e) {
      toast({ title: "Voice", body: e instanceof Error ? e.message : String(e), tone: "error" });
      return false;
    }
  }, [refreshHealth, voice, toast]);

  const voiceOff = health ? !health.voice.enabled : false;
  const toggleMic = useCallback(async () => {
    if (rec.recording) return rec.stop();
    if (rec.busy || streaming) return;
    // Pressing the mic is the explicit opt-in: turn voice on, then record.
    if (voiceOff && !(await enableVoice())) return;
    await rec.start();
  }, [rec, streaming, voiceOff, enableVoice]);

  // Space talks, Escape interrupts (only when not typing).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = document.activeElement instanceof HTMLInputElement || document.activeElement instanceof HTMLTextAreaElement;
      if (e.key === "Escape") interrupt();
      else if (e.code === "Space" && !typing && !e.repeat && !(e.target instanceof HTMLButtonElement)) {
        e.preventDefault();
        void toggleMic();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [interrupt, toggleMic]);

  // A question typed into the Ctrl+K launcher arrives here.
  const sendRef = useRef(send);
  sendRef.current = send;
  useEffect(() => {
    const take = () => {
      let q: string | null = null;
      try {
        q = sessionStorage.getItem("nexus.pendingAsk");
        sessionStorage.removeItem("nexus.pendingAsk");
      } catch {
        /* no storage: nothing to carry over */
      }
      if (q) void sendRef.current(q);
    };
    const id = setTimeout(take, 400); // after the conversation has been restored
    window.addEventListener("nexus:ask", take);
    return () => { clearTimeout(id); window.removeEventListener("nexus:ask", take); };
  }, []);

  const warm = useCallback(async () => {
    try {
      await api.post("/api/models/warm");
    } catch {
      /* the model widget keeps showing its real state */
    }
  }, []);

  const ollamaOff = health ? !health.ollama.available : false;
  const orbState: OrbState = ollamaOff ? "offline"
    : rec.recording || voiceState === "recording" ? "listening"
    : rec.busy || voiceState === "transcribing" || (streaming && !gotToken) || voiceState === "thinking" ? "thinking"
    : streaming ? "responding"
    : speakingNow || voiceState === "speaking" ? "speaking"
    : "idle";
  const label: Record<OrbState, string> = {
    idle: "Ready", listening: "Listening", thinking: "Thinking", responding: "Answering", speaking: "Speaking", offline: "Ollama is offline",
  };

  const now = useClock();
  const last = turns[turns.length - 1];
  const sttReady = voice.data?.stt.available;
  const greetingText = useMemo(() => greeting(now), [now]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void send(draft);
  };

  return (
    <div className="mx-auto grid min-h-full max-w-[1180px] gap-8 px-5 py-8 lg:grid-cols-[minmax(0,1fr)_340px] lg:px-8">
      <section className="flex min-w-0 flex-col items-center">
        <h1 className="font-display text-[34px] font-bold tracking-[-0.03em] text-fg">{greetingText}</h1>
        <div className="relative mt-1">
          <Orb state={orbState} levelRef={rec.recording ? rec.levelRef : idleLevel} size={280} />
          <button
            onClick={() => void toggleMic()}
            aria-label={rec.recording ? "Stop recording" : "Talk to NEXUS"}
            aria-pressed={rec.recording}
            disabled={rec.busy || streaming}
            className={cn(
              "glass absolute left-1/2 top-1/2 flex h-[68px] w-[68px] -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full shadow-float transition-transform duration-200 hover:scale-105 active:scale-95 disabled:opacity-40",
              rec.recording ? "text-ios-red" : "text-accent",
            )}
          >
            {rec.recording ? <Square className="h-6 w-6 fill-current" /> : sttReady === false ? <MicOff className="h-7 w-7" /> : <Mic className="h-7 w-7" />}
          </button>
        </div>
        <p className="-mt-3 text-[15px] font-medium text-slate-300" aria-live="polite">{label[orbState]}</p>
        <p className="mt-1 min-h-5 text-center text-xs text-slate-500">
          {orbState === "idle" && voiceOff && (
            <>Voice is off. <button className="text-accent-soft hover:underline" onClick={() => void enableVoice()}>Turn on voice</button> or type below.</>
          )}
          {orbState === "idle" && !voiceOff && voiceState === "listening_for_wake" && (
            <>Listening for “{voice.data?.wake.phrase ?? "Hey Nexus"}” — or press <Kbd>Space</Kbd></>
          )}
          {orbState === "idle" && !voiceOff && voiceState !== "listening_for_wake" && (sttReady === false
            ? <>Voice isn't set up yet. <button className="text-accent-soft hover:underline" onClick={() => navigate("voice")}>Set up voice</button> or type below.</>
            : <>Press <Kbd>Space</Kbd> to talk · <Kbd>Esc</Kbd> to interrupt</>)}
          {orbState === "listening" && "Speak now. Press the button or Space when you're done."}
          {orbState === "offline" && <>Start Ollama, then check <button className="text-accent-soft hover:underline" onClick={() => navigate("models")}>Models</button>.</>}
        </p>

        <div className="mt-6 flex w-full max-w-[640px] flex-col gap-2.5" aria-label="Conversation">
          {turns.length === 0 && (
            <div className="flex flex-wrap justify-center gap-2" aria-label="Suggestions">
              {SUGGESTIONS.map(({ text, icon: Icon }) => (
                <button key={text} onClick={() => void send(text)} disabled={streaming}
                  className="glass flex items-center gap-1.5 rounded-full px-3.5 py-2 text-[13px] text-slate-200 transition-transform hover:scale-[1.03] active:scale-95 disabled:opacity-40">
                  <Icon className="h-3.5 w-3.5 text-accent" aria-hidden /> {text}
                </button>
              ))}
            </div>
          )}
          {turns.slice(-4).map((t, i, arr) => (
            <div key={t.id} className={cn("flex flex-col animate-pop", t.role === "user" ? "items-end" : "items-start", i < arr.length - 2 && "opacity-45")}>
              {t.note && <span className="mb-1 ml-1 rounded-full bg-ios-green/15 px-2 py-0.5 text-[11px] font-medium text-emerald-300">{t.note}</span>}
              <p className={cn(
                "max-w-[88%] whitespace-pre-wrap rounded-[22px] px-4 py-2.5 text-[15px] leading-relaxed",
                t.role === "user" ? "rounded-br-md bg-accent text-white" : cn("rounded-bl-md bg-ink-800", t.error ? "text-rose-300" : "text-slate-100"),
              )}>
                {t.text || (t === last && streaming ? "…" : "")}
              </p>
            </div>
          ))}
        </div>

        <form onSubmit={submit} className="glass sticky bottom-5 z-10 mt-6 flex w-full max-w-[640px] items-center gap-1.5 rounded-full py-1.5 pl-5 pr-1.5 shadow-float focus-within:shadow-[0_0_0_3px_rgb(var(--accent)/0.35),0_10px_40px_-10px_rgb(0_0_0/0.45)]">
          <input
            ref={input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Ask NEXUS, or give a command…"
            aria-label="Message NEXUS"
            className="min-w-0 flex-1 bg-transparent text-[15px] text-fg placeholder:text-slate-500 focus:outline-none"
          />
          <button
            type="button"
            onClick={() => { const next = !speak; setSpeak(next); write(SPEAK_KEY, next ? "on" : "off"); }}
            aria-pressed={speak}
            title={speak ? "Spoken replies on" : "Spoken replies off"}
            disabled={voice.data ? !voice.data.tts.available : false}
            className="rounded-full p-2 text-slate-400 hover:bg-ink-700/60 hover:text-fg disabled:opacity-30"
          >
            {speak && voice.data?.tts.available ? <Volume2 className="h-[18px] w-[18px]" /> : <VolumeX className="h-[18px] w-[18px]" />}
            <span className="sr-only">Toggle spoken replies</span>
          </button>
          {streaming || speakingNow ? (
            <button type="button" onClick={interrupt} aria-label="Stop" className="flex h-9 w-9 items-center justify-center rounded-full bg-ios-red text-white hover:brightness-110">
              <Square className="h-3.5 w-3.5 fill-current" />
            </button>
          ) : (
            <button type="submit" disabled={!draft.trim()} aria-label="Send" className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-white transition-opacity hover:brightness-110 disabled:opacity-35">
              <SendHorizontal className="h-4 w-4" />
            </button>
          )}
        </form>
      </section>

      <aside className="grid content-start gap-4 sm:grid-cols-2 lg:grid-cols-1" aria-label="Today">
        <ClockWidget />
        <TimersWidget reminders={Array.isArray(reminders.data) ? reminders.data : []} reload={() => void reminders.reload()} onStart={(p) => void send(p)} />
        <TodoWidget />
        <ModelWidget onWarm={warm} />
        <SystemWidget />
        <NotesWidget />
      </aside>
    </div>
  );
}

function useClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60000);
    return () => clearInterval(id);
  }, []);
  return now;
}
