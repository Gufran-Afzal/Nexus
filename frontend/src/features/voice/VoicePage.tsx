import { CheckCircle2, Download, Mic, MicOff, Volume2, XCircle } from "lucide-react";
import { useState } from "react";
import { useStore } from "../../app/store";
import { Badge, Button, Card, ErrorBanner, Field, Input, PageHeader, Select, Spinner, Toggle } from "../../components/ui";
import { useAsync, useEvents } from "../../hooks/useApi";
import { api } from "../../services/api";
import type { MicTestResult, Preferences, VoicePrefs, VoiceStatus } from "../../types";

const MODEL_HINT: Record<string, string> = {
  "tiny.en": "fastest, mishears most",
  "base.en": "balanced",
  "small.en": "accurate, ~480 MB",
  "distil-small.en": "small.en accuracy, faster",
  "distil-medium.en": "very accurate, ~750 MB",
  "distil-large-v3": "best accuracy, ~1.5 GB, slower on this CPU",
  tiny: "any language, fastest",
  base: "any language, balanced",
  small: "any language, accurate",
};

function Check({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div className="flex items-start gap-2 py-1">
      {ok ? <CheckCircle2 className="mt-0.5 h-4 w-4 text-emerald-400" /> : <XCircle className="mt-0.5 h-4 w-4 text-rose-400" />}
      <div>
        <p className="text-sm text-slate-100">{label}</p>
        <p className="text-xs text-slate-500">{ok ? "Ready" : detail}</p>
      </div>
    </div>
  );
}

export function VoicePage() {
  const { refreshHealth, toast } = useStore();
  const status = useAsync(() => api.get<VoiceStatus>("/api/voice/status"), []);
  const prefs = useAsync(() => api.get<Preferences>("/api/preferences"), []);
  const devices = useAsync(() => api.get<{ available: boolean; devices: { index: number; name: string; default: boolean }[]; error?: string }>("/api/voice/devices"), []);
  const voices = useAsync(() => api.get<string[]>("/api/voice/tts/voices"), []);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<MicTestResult | null>(null);

  useEvents((e) => e.type === "voice" && void status.reload());

  const save = async (v: Partial<VoicePrefs>) => {
    setErr(null);
    try {
      const p = await api.patch<Preferences>("/api/preferences", { voice: v });
      prefs.setData(p);
      await status.reload();
      await refreshHealth();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const call = async (path: string, body?: unknown) => {
    try {
      await api.post(path, body);
      await status.reload();
      await refreshHealth();
    } catch (e) {
      toast({ title: "Voice", body: e instanceof Error ? e.message : String(e), tone: "error" });
    }
  };
  const download = async (model: string) => {
    setDownloading(model);
    try {
      await api.post("/api/voice/stt/download", { model });
      toast({ title: `Speech model ${model} downloaded`, tone: "success" });
      await status.reload();
    } catch (e) {
      toast({ title: "Download failed", body: e instanceof Error ? e.message : String(e), tone: "error" });
    } finally {
      setDownloading(null);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.post<MicTestResult>("/api/voice/test"));
    } catch (e) {
      toast({ title: "Microphone test", body: e instanceof Error ? e.message : String(e), tone: "error" });
    } finally {
      setTesting(false);
      void status.reload();
    }
  };

  if (!status.data || !prefs.data) return <div className="p-8">{status.error ? <ErrorBanner message={status.error} /> : <Spinner />}</div>;
  const s = status.data;
  const v = prefs.data.voice;

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <PageHeader title="Voice" subtitle="Fully local speech: recognition with Whisper, speech with Windows voices or Piper. Audio is never saved." />
      {err && <ErrorBanner message={err} />}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Status" actions={<Badge tone={s.state === "recording" ? "red" : s.wake_listening ? "violet" : "gray"}>{s.state.replace(/_/g, " ")}</Badge>}>
          <Toggle checked={v.enabled} onChange={(x) => save({ enabled: x })} label="Enable voice" description="Off by default. Typing always works regardless." />
          <div className="mt-2 border-t border-ink-700 pt-2">
            <Check ok={s.microphone.available} label="Microphone" detail={s.microphone.detail} />
            <Check ok={s.stt.available} label={`Speech recognition (${s.stt.model})`} detail={s.stt.detail} />
            <Check ok={s.tts.available} label={`Speech output (${s.tts.engine})`} detail={s.tts.detail} />
            <Check ok={v.wake_engine === "openwakeword" ? s.components.openwakeword : s.stt.available} label={`Wake word (${v.wake_engine === "openwakeword" ? "openWakeWord" : "Whisper keyword"})`} detail={v.wake_engine === "openwakeword" ? "pip install openwakeword, then set a model" : "Needs speech recognition"} />
          </div>
          {s.last_error && <p className="mt-2 text-xs text-rose-300">Last error: {s.last_error}</p>}
          {s.last_transcript && <p className="mt-1 text-xs text-slate-400">Last heard: “{s.last_transcript}”</p>}
          <div className="mt-3 flex flex-wrap gap-2">
            {s.wake_listening ? (
              <Button size="sm" onClick={() => call("/api/voice/listen/stop")}>Stop listening</Button>
            ) : (
              <Button size="sm" variant="primary" disabled={!v.enabled} onClick={() => call("/api/voice/listen/start")}>Listen for “{v.wake_phrase}”</Button>
            )}
            <Button size="sm" disabled={!v.enabled} onClick={() => call(s.muted ? "/api/voice/unmute" : "/api/voice/mute")}>
              {s.muted ? <Mic className="h-3.5 w-3.5" /> : <MicOff className="h-3.5 w-3.5" />} {s.muted ? "Unmute" : "Mute"}
            </Button>
            <Button size="sm" disabled={!s.tts.available} onClick={() => call("/api/voice/speak", { text: `Hello, I'm ${prefs.data!.assistant_name}. Speech output is working.` })}>
              <Volume2 className="h-3.5 w-3.5" /> Test voice
            </Button>
            <Button size="sm" variant="ghost" onClick={() => call("/api/voice/interrupt")}>Interrupt</Button>
          </div>
        </Card>

        <Card title="Test your microphone">
          <p className="text-xs text-slate-400">Click, then say something like “Hey Nexus, set a timer for ten minutes”. NEXUS shows what it heard, how loud you were and how long recognition took. Nothing is saved.</p>
          <Button className="mt-3" size="sm" variant="primary" loading={testing} disabled={!v.enabled} onClick={runTest}>
            <Mic className="h-3.5 w-3.5" /> {testing ? "Listening… speak now" : "Start test"}
          </Button>
          {test && (
            <div className="mt-3 space-y-2 text-sm">
              <p className="text-slate-100">Heard: {test.heard ? <span className="font-medium">“{test.heard}”</span> : <span className="text-slate-500">nothing</span>}</p>
              <div className="h-2 w-full overflow-hidden rounded-full bg-ink-700" aria-label="Voice level">
                <div className={`h-full ${test.peak > 0.97 ? "bg-rose-400" : test.peak < 0.05 ? "bg-amber-400" : "bg-emerald-400"}`} style={{ width: `${Math.min(100, Math.round(test.peak * 100))}%` }} />
              </div>
              <p className="text-xs text-slate-500">Peak {Math.round(test.peak * 100)}% · {test.level_db} dB · spoke {test.seconds}s{test.recognise_s !== null ? ` · recognised in ${test.recognise_s}s` : ""}</p>
              <ul className="list-disc space-y-1 pl-5 text-xs text-slate-300">{test.advice.map((a) => <li key={a}>{a}</li>)}</ul>
            </div>
          )}
        </Card>

        <Card title="Recent wake-ups">
          {(s.wake_attempts ?? []).length === 0 ? (
            <p className="text-xs text-slate-500">Nothing yet. Each time NEXUS hears speech while listening, what it understood appears here and whether it counted as “{v.wake_phrase.split(",")[0]}”. If it never wakes, the words shown tell you why.</p>
          ) : (
            <ul className="space-y-1">
              {(s.wake_attempts ?? []).map((a) => (
                <li key={a.ts} className="flex items-center gap-2 text-sm">
                  {a.accepted ? <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-400" /> : <XCircle className="h-4 w-4 shrink-0 text-slate-500" />}
                  <span className={a.accepted ? "text-slate-100" : "text-slate-400"}>{a.heard ? `“${a.heard}”` : "(unclear)"}</span>
                  <span className="ml-auto text-[11px] text-slate-500">{new Date(a.ts * 1000).toLocaleTimeString()}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Speech recognition models (faster-whisper)">
          {!s.components.faster_whisper && <p className="mb-2 text-xs text-amber-300">faster-whisper isn't installed. Run scripts\setup.ps1 -Voice (see docs/VOICE.md).</p>}
          <div className="space-y-1.5">
            {s.stt_models.map((m) => (
              <div key={m.name} className="flex items-center gap-2 text-sm">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-mono text-slate-200">{m.name}</p>
                  <p className="truncate text-[11px] text-slate-500">{MODEL_HINT[m.name] ?? ""}</p>
                </div>
                {m.downloaded ? <Badge tone="green" className="shrink-0 whitespace-nowrap">downloaded</Badge> : <Badge className="shrink-0 whitespace-nowrap">not downloaded</Badge>}
                {v.stt_model === m.name ? <Badge tone="violet" className="shrink-0 whitespace-nowrap">in use</Badge> : <Button size="sm" variant="ghost" className="shrink-0" onClick={() => save({ stt_model: m.name })}>Use</Button>}
                {!m.downloaded && (
                  <Button size="sm" className="shrink-0" disabled={!s.components.faster_whisper} loading={downloading === m.name} onClick={() => download(m.name)}>
                    <Download className="h-3.5 w-3.5" /> Download
                  </Button>
                )}
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-slate-500">Downloads come from Hugging Face once; afterwards recognition works offline. tiny.en is fastest but mishears more; base.en is a balance; small.en (~480 MB) is the most accurate and is the one to try if NEXUS gets words wrong.</p>
        </Card>

        <Card title="Accuracy & conversation">
          <Field label="My words (comma separated)" hint="Names, products or jargon that get misheard. NEXUS also learns your app names and the people you add, automatically.">
            <Input defaultValue={v.custom_words.join(", ")} placeholder="Kubernetes, Priya, Nexus" aria-label="My words"
              onBlur={(e) => save({ custom_words: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
          </Field>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field label={`Keep listening after an answer: ${v.follow_up_s === 0 ? "off" : `${v.follow_up_s}s`}`} hint="Reply without saying “Hey Nexus” again. Say “thanks” to end it.">
              <input type="range" min={0} max={20} step={1} defaultValue={v.follow_up_s} aria-label="Follow-up seconds"
                onMouseUp={(e) => save({ follow_up_s: Number((e.target as HTMLInputElement).value) })} onKeyUp={(e) => save({ follow_up_s: Number((e.target as HTMLInputElement).value) })} className="w-full accent-[#7c83ff]" />
            </Field>
            <Field label="Live captions" hint="Shows your words as you speak (uses tiny.en if it is downloaded).">
              <Select value={v.live_captions ? "on" : "off"} onChange={(x) => save({ live_captions: x === "on" })} options={[{ value: "on", label: "On" }, { value: "off", label: "Off" }]} />
            </Field>
          </div>
          <div className="mt-3">
            <Toggle checked={v.confirm_when_unsure} onChange={(x) => save({ confirm_when_unsure: x })} label="Ask before acting when I'm not sure I heard right" description={`Messages, emails and app commands heard with low confidence (below ${Math.round(v.unsure_threshold * 100)}%) are read back and need a “yes”.`} />
          </div>
          {typeof s.last_confidence === "number" && <p className="mt-2 text-xs text-slate-400">Last request heard with {Math.round(s.last_confidence * 100)}% confidence.</p>}
          <p className="mt-2 text-[11px] text-slate-500">Tip: speak at a normal pace about a hand's width from the mic, or use a headset. If words are still wrong, switch to a bigger model above (distil-medium.en is a good step up).</p>
        </Card>

        <Card title="Listening">
          <Field label="Mode">
            <Select value={v.listening_mode} onChange={(x) => save({ listening_mode: x as VoicePrefs["listening_mode"] })} options={[
              { value: "push_to_talk", label: "Push-to-talk only (mic button in chat)" },
              { value: "wake_word", label: "Wake word (start listening automatically)" },
            ]} />
          </Field>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field label="Wake engine">
              <Select value={v.wake_engine} onChange={(x) => save({ wake_engine: x as VoicePrefs["wake_engine"] })} options={[
                { value: "whisper_keyword", label: "Whisper keyword (any phrase)" },
                { value: "openwakeword", label: "openWakeWord (lighter, needs model)" },
              ]} />
            </Field>
            <Field label="Wake phrase (separate alternatives with commas)">
              <Input defaultValue={v.wake_phrase} onBlur={(e) => e.target.value !== v.wake_phrase && save({ wake_phrase: e.target.value })} />
            </Field>
          </div>
          {v.wake_engine === "openwakeword" && (
            <div className="mt-3 grid gap-3 sm:grid-cols-[2fr_1fr]">
              <Field label="openWakeWord model" hint="Path to a custom .onnx/.tflite (e.g. a trained “hey nexus”), or a built-in name like hey_jarvis.">
                <Input defaultValue={v.openwakeword_model} onBlur={(e) => save({ openwakeword_model: e.target.value })} className="font-mono" />
              </Field>
              <Field label="Threshold">
                <Input type="number" step="0.05" min="0.1" max="0.95" defaultValue={v.wake_threshold} onBlur={(e) => save({ wake_threshold: Number(e.target.value) })} />
              </Field>
            </div>
          )}
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field label="Wait after I stop talking (seconds)" hint="How long a pause counts as the end of your request. Longer is more forgiving for long sentences.">
              <Input type="number" step="0.1" min="0.6" max="5" defaultValue={v.pause_tolerance_s} onBlur={(e) => save({ pause_tolerance_s: Number(e.target.value) })} />
            </Field>
            <Field label="Unfinished sentence" hint="If it sounds unfinished (ends in “and”, “because”, a comma…), keep listening instead of sending.">
              <Select value={v.smart_endpoint ? "on" : "off"} onChange={(x) => save({ smart_endpoint: x === "on" })} options={[
                { value: "on", label: "Keep listening (recommended)" },
                { value: "off", label: "Send after the pause" },
              ]} />
            </Field>
          </div>
          <div className="mt-3">
            <Field label="Microphone">
              <Select
                value={v.input_device === null ? "" : String(v.input_device)}
                onChange={(x) => save({ input_device: x === "" ? null : Number(x) })}
                options={[{ value: "", label: "System default" }, ...(devices.data?.devices ?? []).map((d) => ({ value: String(d.index), label: d.name }))]}
              />
            </Field>
            {devices.data && !devices.data.available && <p className="mt-1 text-[11px] text-rose-300">{devices.data.error}</p>}
          </div>
          <div className="mt-2">
            <Toggle checked={v.ack_sound} onChange={(x) => save({ ack_sound: x })} label="Play a short sound when the wake word is heard" />
          </div>
          <p className="mt-2 text-[11px] text-slate-500">A wake word can start a conversation, set timers and take notes. It can never approve edits, commands or anything destructive.</p>
        </Card>

        <Card title="Speech output">
          <Field label="Engine">
            <Select value={v.tts_engine} onChange={(x) => save({ tts_engine: x as VoicePrefs["tts_engine"] })} options={[
              { value: "sapi", label: "Windows voices (built in)" },
              { value: "piper", label: "Piper (installed separately)" },
              { value: "none", label: "No speech output" },
            ]} />
          </Field>
          {v.tts_engine === "sapi" && (
            <div className="mt-3">
              <Field label="Voice">
                <Select value={v.tts_voice} onChange={(x) => save({ tts_voice: x })} options={[{ value: "", label: "Default voice" }, ...(voices.data ?? []).map((n) => ({ value: n, label: n }))]} />
              </Field>
            </div>
          )}
          {v.tts_engine === "piper" && (
            <div className="mt-3 space-y-3">
              <Field label="piper.exe path"><Input defaultValue={v.piper_exe} onBlur={(e) => save({ piper_exe: e.target.value })} className="font-mono" /></Field>
              <Field label="Voice model (.onnx)"><Input defaultValue={v.piper_voice_model} onBlur={(e) => save({ piper_voice_model: e.target.value })} className="font-mono" /></Field>
            </div>
          )}
          <div className="mt-3">
            <Field label={`Speaking rate: ${v.tts_rate} words/min`}>
              <input type="range" min={100} max={300} step={10} defaultValue={v.tts_rate} onMouseUp={(e) => save({ tts_rate: Number((e.target as HTMLInputElement).value) })} onKeyUp={(e) => save({ tts_rate: Number((e.target as HTMLInputElement).value) })} className="w-full accent-[#7c83ff]" aria-label="Speaking rate" />
            </Field>
          </div>
          <Toggle checked={v.speak_responses} onChange={(x) => save({ speak_responses: x })} label="Speak answers to voice requests" />
        </Card>
      </div>
    </div>
  );
}
