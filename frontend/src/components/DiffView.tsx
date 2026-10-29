import { cn } from "./ui";

/** Renders a unified diff with added/removed line indicators and line numbers. */
export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="text-xs text-slate-500">No textual changes.</p>;
  let oldNo = 0;
  let newNo = 0;
  const rows = diff.split("\n").map((line, i) => {
    if (line.startsWith("@@")) {
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      if (m) {
        oldNo = Number(m[1]);
        newNo = Number(m[2]);
      }
      return { i, kind: "hunk", text: line, o: "", n: "" };
    }
    if (line.startsWith("+++") || line.startsWith("---")) return { i, kind: "file", text: line, o: "", n: "" };
    if (line.startsWith("+")) return { i, kind: "add", text: line, o: "", n: String(newNo++) };
    if (line.startsWith("-")) return { i, kind: "del", text: line, o: String(oldNo++), n: "" };
    if (line.startsWith("\\")) return { i, kind: "meta", text: line, o: "", n: "" };
    const r = { i, kind: "ctx", text: line, o: String(oldNo), n: String(newNo) };
    oldNo++;
    newNo++;
    return r;
  });
  return (
    <div className="overflow-x-auto rounded-lg border border-ink-700 bg-ink-950 font-mono text-[12.5px] leading-5" data-testid="diff">
      <table className="w-full border-collapse">
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.i}
              className={cn(
                r.kind === "add" && "bg-emerald-500/10",
                r.kind === "del" && "bg-rose-500/10",
                r.kind === "hunk" && "bg-accent/10 text-accent-soft",
                r.kind === "file" && "text-slate-500",
              )}
            >
              <td className="w-10 select-none px-2 text-right text-slate-600">{r.o}</td>
              <td className="w-10 select-none px-2 text-right text-slate-600">{r.n}</td>
              <td
                className={cn(
                  "whitespace-pre px-2",
                  r.kind === "add" && "text-emerald-300",
                  r.kind === "del" && "text-rose-300",
                )}
                aria-label={r.kind === "add" ? "added line" : r.kind === "del" ? "removed line" : undefined}
              >
                {r.text || " "}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
