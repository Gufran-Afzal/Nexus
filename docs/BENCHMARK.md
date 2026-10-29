# Benchmark: 0.6.0 versus 0.7.0

`cd backend && python -m benchmarks.run --system legacy|new [--out file.json]` — 14 scenarios; 11 run through the real HTTP chat
API on both versions, 3 (S06, S10, S12) drive the new runtime directly and have no 0.6.0 equivalent.
Saved results: `backend/benchmarks/baseline_0.6.0.json` (0.6.0, measured before any change) and `after_0.7.0.json`.

**Read this first.** The desktop is a *fake* (windows that appear, dialogs that block, apps that fail to start) and the model is
*scripted*: it answers a fixed plan when asked to plan. So this measures **orchestration** — routing, tool use, verification,
recovery, honesty — and **not** how well Qwen 2.5 7B writes plans. 0.6.0 has no planner, so scenarios that need one (S03, S14) fail
there by construction; the comparison shows what the architecture adds, not what a model adds. I also wrote the scenarios while
building the runtime, so treat 11/11 as "the machinery works as designed", not as a general success rate.

| Metric (11 shared scenarios) | 0.6.0 | 0.7.0 |
|---|---|---|
| Succeeded | 4 / 11 (36 %) | 11 / 11 |
| **False success claims** (said "done" when it was not) | **2** (S08, S09) | **0** |
| Tool-selection accuracy | n/a (no tool trace) | 100 % |
| Tool-argument accuracy | n/a | 100 % |
| Steps whose result was checked against the environment | 0 / 0 | 16 / 21 (the other 5 are read-only previews or have nothing to observe) |
| Recovery from injected failures (S07, S08) | 0 / 2 | 2 / 2 |
| Duplicate actions (S09) | 1 | 0 |
| Approvals requested | 0 | 1 (S02; S13 keeps its 0.6.0 confirm card) |
| Model calls per task | 0.91 | 0.27 |
| Median / p95 latency (fake desktop, no model time) | 17 ms / 32 ms | 28 ms / 56 ms |
| Peak RSS of the benchmark process | 92 MB | 104 MB |

Runtime-only: S06 broken tool result — contained, other step still ran, honest report; S10 restart mid-run — resumed, Spotify
**not** launched twice; S12 cancel mid-run — later steps did not run. All three pass.

0.6.0's failures: S02/S03/S04/S14 had no way to do multi-step or follow-up work; S07 gave up on the first launch error; S08 said
"Opened Spotify" while an update dialog covered it; S09 created the same reminder twice. (The duplicate guard now also exists in the
shared reminder code, so a re-run of `--system legacy` on the new tree shows 0 duplicates for S09; the saved baseline is the true 0.6.0 number.)

Resource cost of the runtime itself: ≈ +12 MB RSS, ≈ +11 ms per command on the fake desktop, one planner call only when a request
needs planning. Real latency is dominated by the model (planner) and by the 6 s window wait on a slow app.

**What to run on your laptop** to get numbers that mean something: put a handful of your own phrasings in `scenarios.py`, run
`python -m benchmarks.run --system new` with Ollama up and the scripted model replaced by the provider (set `plan=None` and drop
`Bench._reply`), and compare planner validity rate and seconds per planned task.
