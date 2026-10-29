import { AlarmClock, BellRing, Check, Timer } from "lucide-react";
import { useEffect } from "react";
import { useStore } from "../app/store";
import { Button } from "./ui";

/** A short two-tone chime. Browsers may refuse to play before the first click; the server also beeps and speaks. */
function chime() {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    [880, 660].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.22);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + i * 0.22 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.22 + 0.2);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.22);
      o.stop(ctx.currentTime + i * 0.22 + 0.22);
    });
    setTimeout(() => void ctx.close(), 1200);
  } catch {
    /* no audio available */
  }
}

const SNOOZES = [5, 10, 30, 60];

export function AlarmModal() {
  const { alarms, snoozeAlarm, dismissAlarm } = useStore();
  const ringKey = alarms.map((a) => `${a.id}:${a.ring}`).join(",");

  useEffect(() => {
    if (!ringKey) return;
    chime();
    const original = document.title;
    let on = false;
    const t = setInterval(() => {
      on = !on;
      document.title = on ? "🔔 NEXUS reminder" : original;
    }, 900);
    return () => {
      clearInterval(t);
      document.title = original;
    };
  }, [ringKey]);

  if (alarms.length === 0) return null;
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm" role="alertdialog" aria-modal="true" aria-label="Reminder">
      <div className="flex max-h-[85vh] w-full max-w-md flex-col gap-3 overflow-y-auto">
        {alarms.map((a) => {
          const Icon = a.kind === "timer" ? Timer : a.kind === "alarm" ? AlarmClock : BellRing;
          const title = a.kind === "timer" ? "Timer finished" : a.kind === "alarm" ? "Alarm" : "Reminder";
          return (
            <div key={a.id} className="glass animate-pop rounded-3xl p-5 shadow-float" data-testid="alarm">
              <div className="flex items-center gap-3">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent/20 text-accent-soft">
                  <Icon className="h-5 w-5 animate-pulse" aria-hidden />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                    {title}{a.late ? " · missed while away" : ""}{a.ring > 1 ? ` · ring ${a.ring}` : ""}
                  </p>
                  <p className="text-lg font-semibold leading-snug text-fg">{a.text}</p>
                </div>
              </div>
              {a.action && <p className="mt-2 text-xs text-slate-400">NEXUS opened {a.action.name} for you.</p>}
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <Button variant="primary" onClick={() => void dismissAlarm(a.id)}><Check className="h-4 w-4" /> Done</Button>
                <span className="ml-auto text-xs text-slate-500">Snooze</span>
                {SNOOZES.map((m) => (
                  <Button key={m} size="sm" variant="subtle" onClick={() => void snoozeAlarm(a.id, m)}>{m < 60 ? `${m} min` : "1 h"}</Button>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
