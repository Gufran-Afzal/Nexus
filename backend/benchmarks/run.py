"""python -m benchmarks.run [--system legacy|new] [--out file.json]: run the benchmark and print a table."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict

import psutil

from benchmarks.harness import Outcome, run_chat_scenario
from benchmarks.scenarios import CHAT, Scenario


def run_all(system: str, only: set[str] | None = None) -> list[tuple[Scenario, Outcome]]:
    scenarios = list(CHAT)
    if system == "new":
        from benchmarks.runtime_scenarios import RUNTIME, run_runtime_scenario

        results = [(s, run_chat_scenario(s, legacy=False)) for s in scenarios if not only or s.id in only]
        results += [(s, run_runtime_scenario(s)) for s in RUNTIME if not only or s.id in only]
        return results
    return [(s, run_chat_scenario(s, legacy=True)) for s in scenarios if not only or s.id in only]


def summarise(results: list[tuple[Scenario, Outcome]]) -> dict[str, object]:
    comparable = [(s, o) for s, o in results if not s.runtime_only]
    n = len(comparable)
    secs = sorted(o.seconds for _s, o in comparable)
    ok = [o for _s, o in comparable if o.success]
    tool_scored = [(s, o) for s, o in comparable if s.expect_tools and o.tools_used]
    sel = [sorted(o.tools_used) == sorted(s.expect_tools) for s, o in tool_scored]
    arg = []
    for s, o in tool_scored:
        for t, want in s.expect_args.items():
            got = [a for tool, a in zip(o.tools_used, o.tool_args, strict=False) if tool == t]
            arg.append(any(all(str(w).lower() in str(a.get(k, "")).lower() for k, w in want.items()) for a in got))
    ver = [v for _s, o in results for v in o.steps_verified]
    rec = [o.recovered for s, o in comparable if s.failure_injected]
    return {
        "scenarios": n, "success": len(ok), "success_rate": round(len(ok) / n, 3) if n else 0,
        "honest": sum(1 for _s, o in comparable if o.honest), "false_success_claims": sum(1 for _s, o in comparable if not o.honest),
        "tool_selection_accuracy": round(sum(sel) / len(sel), 3) if sel else None,
        "tool_arg_accuracy": round(sum(arg) / len(arg), 3) if arg else None,
        "steps_with_a_verification_result": sum(1 for v in ver if v is not None), "steps_total": len(ver),
        "recovery_success": f"{sum(1 for r in rec if r)}/{len(rec)}" if rec else None,
        "duplicate_actions": sum(o.duplicates for _s, o in comparable),
        "approvals_requested": sum(o.approvals for _s, o in comparable),
        "median_s": round(statistics.median(secs), 3) if secs else 0, "p95_s": round(secs[min(len(secs) - 1, int(len(secs) * 0.95))], 3) if secs else 0,
        "llm_calls_total": sum(o.llm_calls for _s, o in comparable), "tokens_est_total": sum(o.tokens for _s, o in comparable),
        "llm_calls_per_task": round(sum(o.llm_calls for _s, o in comparable) / n, 2) if n else 0,
        "peak_rss_mb": round(psutil.Process().memory_info().rss / 1e6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["legacy", "new"], default="new")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    results = run_all(a.system, set(a.only) if a.only else None)
    print(f"{'id':4} {'ok':3} {'honest':6} {'llm':3} {'sec':>6}  title")
    for s, o in results:
        print(f"{s.id:4} {'yes' if o.success else 'NO':3} {'yes' if o.honest else 'NO':6} {o.llm_calls:3} {o.seconds:6.2f}  {s.title}"
              + ("  [runtime only]" if s.runtime_only else ""))
    summ = summarise(results)
    print(json.dumps(summ, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"system": a.system, "summary": summ,
                       "results": [{"id": s.id, "title": s.title, "runtime_only": s.runtime_only, **asdict(o)} for s, o in results]}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
