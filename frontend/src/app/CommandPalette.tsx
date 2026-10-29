import { Calculator, CornerDownLeft, Search, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Kbd, cn } from "../components/ui";
import { api } from "../services/api";

export interface PaletteItem {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

/**
 * Spotlight-style launcher: jump anywhere, run a command, get an instant answer ("15% of 240", "5 km in miles",
 * "what time is it") or hand the question to the assistant. Instant answers come from plain code, not the model.
 */
export function CommandPalette({ items, onClose, onAsk }: { items: PaletteItem[]; onClose: () => void; onAsk?: (q: string) => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [peek, setPeek] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return items.filter((i) => terms.every((t) => i.label.toLowerCase().includes(t))).slice(0, 30);
  }, [items, q]);

  useEffect(() => inputRef.current?.focus(), []);
  useEffect(() => setSel(0), [q]);

  useEffect(() => {
    const text = q.trim();
    setPeek(null);
    if (text.length < 2) return;
    let alive = true;
    const id = setTimeout(async () => {
      try {
        const r = await api.get<{ answer: string | null }>(`/api/assistant/peek?q=${encodeURIComponent(text)}`);
        if (alive) setPeek(r.answer);
      } catch {
        /* the launcher still works without instant answers */
      }
    }, 120);
    return () => { alive = false; clearTimeout(id); };
  }, [q]);

  const ask = q.trim().length > 1 && onAsk ? q.trim() : null;
  const rows: PaletteItem[] = [
    ...filtered,
    ...(ask ? [{ id: "ask", label: `Ask NEXUS: “${ask}”`, run: () => onAsk?.(ask) }] : []),
  ];

  const choose = (i: PaletteItem | undefined) => {
    if (!i) return;
    onClose();
    i.run();
  };

  return (
    <div className="fixed inset-0 z-40 flex animate-fade items-start justify-center bg-black/35 pt-[12vh] backdrop-blur-[2px]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label="Command palette"
        className="glass w-full max-w-xl animate-pop overflow-hidden rounded-3xl shadow-float"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-5" style={{ boxShadow: "inset 0 -0.5px 0 rgb(var(--hairline) / var(--hairline-alpha))" }}>
          <Search className="h-5 w-5 text-slate-500" aria-hidden />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
              else if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, rows.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
              else if (e.key === "Enter") choose(rows[sel]);
            }}
            placeholder="Search, calculate, or ask NEXUS…"
            aria-label="Command"
            className="h-14 flex-1 bg-transparent text-[17px] text-fg placeholder:text-slate-500 focus:outline-none"
          />
          <Kbd>Esc</Kbd>
        </div>
        {peek && (
          <div className="flex items-center gap-3 px-5 py-3" style={{ boxShadow: "inset 0 -0.5px 0 rgb(var(--hairline) / var(--hairline-alpha))" }}>
            <Calculator className="h-5 w-5 text-accent" aria-hidden />
            <p className="font-display text-xl font-semibold tabular text-fg" aria-live="polite">{peek}</p>
          </div>
        )}
        <ul className="max-h-80 overflow-y-auto p-2" role="listbox">
          {rows.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No matching commands</li>}
          {rows.map((i, idx) => (
            <li
              key={i.id}
              role="option"
              aria-selected={idx === sel}
              onMouseEnter={() => setSel(idx)}
              onClick={() => choose(i)}
              className={cn("flex cursor-pointer items-center justify-between gap-3 rounded-xl px-3 py-2 text-sm", idx === sel ? "bg-accent text-white" : "text-slate-200")}
            >
              <span className="flex min-w-0 items-center gap-2 truncate">
                {i.id === "ask" && <Sparkles className="h-4 w-4 shrink-0" aria-hidden />}
                <span className="truncate">{i.label}</span>
              </span>
              {idx === sel ? <CornerDownLeft className="h-3.5 w-3.5 shrink-0 opacity-80" aria-hidden /> : i.hint && <span className="truncate text-xs text-slate-500">{i.hint}</span>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
