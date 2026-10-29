import { HelpCircle, Send, X } from "lucide-react";
import { useStore } from "../app/store";
import { Button } from "./ui";

/** Something is waiting for your yes/no: a message or email that is ready to send, or a command NEXUS wasn't sure it heard. */
export function PendingCard() {
  const { pending, answerPending } = useStore();
  if (!pending) return null;
  const isSend = pending.kind === "send";
  const Icon = isSend ? Send : HelpCircle;
  return (
    <div className="fixed bottom-5 right-5 z-[55] w-[min(24rem,calc(100vw-2.5rem))] animate-pop" role="alertdialog" aria-label="Waiting for your answer" data-testid="pending">
      <div className="glass rounded-3xl p-4 shadow-float">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/20 text-accent-soft"><Icon className="h-5 w-5" /></span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-slate-100">{isSend ? `Ready to send: ${pending.title}` : pending.question || `Do this? ${pending.title}`}</p>
            {pending.preview && <p className="mt-1 line-clamp-3 break-words text-xs text-slate-400">“{pending.preview}”</p>}
            <p className="mt-1 text-[11px] text-slate-500">{isSend ? "Check the window that opened, then " : ""}say “{isSend ? "send it" : "yes"}” or “cancel”.</p>
          </div>
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => void answerPending(false)}><X className="h-3.5 w-3.5" /> Cancel</Button>
          <Button size="sm" variant="primary" onClick={() => void answerPending(true)}>{isSend ? "Send it" : "Yes, do it"}</Button>
        </div>
      </div>
    </div>
  );
}
