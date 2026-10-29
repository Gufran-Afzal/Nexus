import { AlertTriangle, Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from "react";

export function cn(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

type Variant = "primary" | "ghost" | "outline" | "danger" | "subtle";

/** Capsule buttons, like iOS: a filled blue primary action, quiet tinted secondaries. */
export function Button({
  variant = "outline",
  size = "md",
  loading,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean }) {
  const styles: Record<Variant, string> = {
    primary: "bg-accent text-white hover:brightness-110 active:brightness-95 shadow-sm shadow-accent/30",
    ghost: "text-accent-soft hover:bg-accent/10",
    outline: "bg-ink-800/80 text-slate-100 hover:bg-ink-700/80",
    danger: "bg-ios-red text-white hover:brightness-110",
    subtle: "bg-accent/15 text-accent-soft hover:bg-accent/20",
  };
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={cn(
        "inline-flex select-none items-center justify-center gap-1.5 rounded-full font-medium transition-[filter,background-color,transform] duration-150 active:scale-[0.98]",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 disabled:cursor-not-allowed disabled:opacity-45",
        size === "sm" ? "h-7 px-3 text-xs" : "h-9 px-4 text-sm",
        styles[variant],
        className,
      )}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("card-surface rounded-3xl", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 px-5 pb-0 pt-4">
          <h2 className="text-[15px] font-semibold tracking-tight text-slate-100">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

const TONES: Record<string, string> = {
  green: "bg-ios-green/15 text-emerald-300",
  red: "bg-ios-red/15 text-rose-300",
  amber: "bg-ios-orange/15 text-amber-300",
  blue: "bg-accent/15 text-accent-soft",
  violet: "bg-ios-purple/15 text-accent-soft",
  gray: "bg-slate-500/15 text-slate-300",
};

export function Badge({ tone = "gray", children, className }: { tone?: keyof typeof TONES | string; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium", TONES[tone] ?? TONES.gray, className)}>
      {children}
    </span>
  );
}

/** Consistent colouring for lifecycle states shared across actions, commands, tasks and change sets. */
export function statusTone(s: string): string {
  if (["succeeded", "applied", "complete", "completed", "accepted", "ok"].includes(s)) return "green";
  if (["failed", "error", "timed_out", "conflict", "rejected", "blocked"].includes(s)) return "red";
  if (["pending_approval", "waiting_approval", "awaiting_permission", "proposed", "partially_applied", "partially_completed", "waiting_for_approval", "waiting_for_input", "retrying"].includes(s)) return "amber";
  if (["running", "streaming", "generating"].includes(s)) return "blue";
  return "gray";
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={statusTone(status)}>{status.replace(/_/g, " ")}</Badge>;
}

const FIELD =
  "w-full rounded-xl bg-ink-850 text-sm text-slate-100 placeholder:text-slate-500 shadow-[inset_0_0_0_0.5px_rgb(var(--hairline)/var(--hairline-alpha))] " +
  "transition-shadow focus:outline-none focus:shadow-[0_0_0_3px_rgb(var(--accent)/0.35),inset_0_0_0_1px_rgb(var(--accent))]";

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(FIELD, "h-9 px-3", props.className)} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(FIELD, "px-3 py-2", props.className)} />;
}

/** iOS-style switch. */
export function Toggle({ checked, onChange, label, description, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; description?: string; disabled?: boolean }) {
  return (
    <label className={cn("flex items-start justify-between gap-4 py-2", disabled && "opacity-50")}>
      <span>
        <span className="block text-sm text-slate-100">{label}</span>
        {description && <span className="block text-xs text-slate-400">{description}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn("relative mt-0.5 h-[26px] w-[44px] shrink-0 rounded-full transition-colors duration-200", checked ? "bg-ios-green" : "bg-ink-600")}
      >
        <span
          className={cn(
            "absolute top-[2px] h-[22px] w-[22px] rounded-full bg-white shadow-[0_2px_4px_rgb(0_0_0/0.3)] transition-all duration-200",
            checked ? "left-[20px]" : "left-[2px]",
          )}
        />
      </button>
    </label>
  );
}

/** Segmented control (iOS). */
export function Segmented<T extends string>({ value, onChange, options, ariaLabel }: { value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode }[]; ariaLabel?: string }) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className="inline-flex rounded-full bg-ink-800 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-full px-3 py-1 text-xs font-medium transition-colors",
            value === o.value ? "bg-ink-600 text-fg shadow-sm" : "text-slate-400 hover:text-slate-100",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Circular progress ring (Activity-ring style). `value` is 0..1. */
export function Ring({ value, size = 56, stroke = 7, color = "rgb(var(--accent))", children }: { value: number; size?: number; stroke?: number; color?: string; children?: ReactNode }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(1, value));
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeWidth={stroke} className="text-ink-700/70" />
        <circle
          cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - v)} style={{ transition: "stroke-dashoffset 0.6s cubic-bezier(0.2,0.8,0.2,1)" }}
        />
      </svg>
      {children && <div className="absolute inset-0 flex items-center justify-center text-center">{children}</div>}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded-md bg-ink-800 px-1.5 py-0.5 font-sans text-[10px] font-medium text-slate-400 hairline">{children}</kbd>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-400" role="status">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> {label ?? "Loading…"}
    </div>
  );
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-2xl bg-ios-red/10 px-4 py-3 text-sm text-rose-200">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <span className="flex-1 whitespace-pre-wrap">{message}</span>
      {onRetry && (
        <Button size="sm" variant="ghost" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      {icon && <div className="text-slate-500">{icon}</div>}
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {children && <div className="max-w-md text-xs text-slate-500">{children}</div>}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="font-display text-[28px] font-bold leading-tight tracking-[-0.025em] text-fg">{title}</h1>
        {subtitle && <p className="mt-1 text-[15px] text-slate-400">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-slate-300">{label}</span>
      {children}
      {hint && <span className="block text-[11px] leading-snug text-slate-500">{hint}</span>}
    </label>
  );
}

export function Select({ value, onChange, options, ariaLabel }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; ariaLabel?: string }) {
  return (
    <select
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(FIELD, "h-9 px-2.5")}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
