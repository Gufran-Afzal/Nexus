import { useStore } from "../app/store";

/** What you've said so far, shown while you are still speaking (so a misheard word is visible before NEXUS acts on it). */
export function LiveCaption() {
  const { caption, voiceState } = useStore();
  if (!caption || voiceState !== "recording") return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-24 z-[50] flex justify-center px-4" aria-live="polite" data-testid="caption">
      <p className="glass max-w-2xl rounded-2xl px-4 py-2 text-center text-sm text-slate-100 shadow-float">{caption}<span className="ml-0.5 animate-pulse text-accent-soft">▍</span></p>
    </div>
  );
}
