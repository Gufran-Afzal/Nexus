# NEXUS — Your Local AI Engineering Partner

NEXUS is a local-first personal assistant and coding agent for Windows. It runs a small FastAPI
server on `127.0.0.1`, a web UI in your browser, and a system-tray icon. Language models run in
[Ollama](https://ollama.com) on your own machine; speech recognition and speech output are local too.
No cloud AI, no telemetry, no hidden network fallbacks.

> **Honest expectations.** A 7B model on a laptop CPU (i7-8650U) is much weaker and slower than the best
> hosted models — expect roughly 3–8 words per second and occasional mistakes. NEXUS compensates with
> transparent context retrieval (it shows exactly which files it used), proposals you review as diffs,
> commands you approve, and test results it reports verbatim instead of claiming success.

## What it does

| Area | What you get |
| --- | --- |
| Chat | Streaming answers, stop button, Markdown + syntax highlighting, copyable code, search/rename/export/delete conversations |
| Coding mode | Senior-engineer instructions, project context with a visible token budget, `NEXUS.md` project guidance |
| Coding agent | The model searches/reads your project with tools, then proposes edits and test commands for your approval — every step shown live |
| Retrieval | Cached BM25 keyword index with identifier splitting; optional local semantic search (embeddings via Ollama), fused with keywords |
| Projects | Explicitly added folders, file tree, preview, name/content search, Git status + diff, per-project permissions |
| Change review | Proposals (manual or AI-generated) shown as unified diffs; accept/reject per file; stale-file detection; backups; revert |
| Commands | Tests/linters/type-checks run **only after you approve the exact command**; no shell; timeouts, output limits, cancel |
| Tasks | Bounded workflows (investigate bug, plan feature, implement change) that pause at approval checkpoints |
| Assistant | Timers, reminders, notes, to-dos, agenda — handled by real code, not the model; launch approved apps/sites/folders |
| Voice | Push-to-talk and “Hey Nexus” wake word, local Whisper transcription, Windows voices or Piper for speech |
| Desktop | Tray icon (open, listen, mute, pause, settings, logs, exit), opt-in start at sign-in, single instance |
| Trust | Read-only by default, secrets excluded, audit log, CSRF/DNS-rebinding protection, everything stored locally |

## Quick start (Windows 10/11)

Prerequisites: **Python 3.11+** (3.12 recommended, tick “Add to PATH”), **Node.js 18+ LTS**, optionally
**Git** and **Ollama**. No administrator rights are needed.

```powershell
# 1. In the folder where you unpacked NEXUS:
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Shortcut          # add -Voice for speech
# 2. Install Ollama from https://ollama.com, then download the model (≈4.7 GB, one time):
ollama pull qwen2.5-coder:7b
# 3. Start NEXUS (tray icon + browser UI at http://127.0.0.1:8765):
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then follow the in-app setup checklist. To start with Windows: **Settings → Start NEXUS when I sign in**.
To stop everything: tray icon → **Exit NEXUS completely** (or `scripts\stop.ps1`).

Full guide: [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) · Voice: [docs/VOICE.md](docs/VOICE.md) ·
Design: [ARCHITECTURE.md](ARCHITECTURE.md) · Security model: [SECURITY.md](SECURITY.md) ·
What's verified and what isn't: [docs/STATUS.md](docs/STATUS.md)

## What needs the internet

| When | What | Why |
| --- | --- | --- |
| Setup | Python packages (pip), npm packages | Installing NEXUS itself |
| Once | `ollama pull qwen2.5-coder:7b` or Models → Download | The language model |
| Once, optional | Voice → Download speech model | Whisper model from Hugging Face |
| Once, optional | openWakeWord / Piper voices | Only if you choose those engines |
| Once, optional | `ollama pull nomic-embed-text` (≈270 MB) | Semantic search in projects |

After that NEXUS runs fully offline. `scripts\verify-offline.ps1` lists every network connection held
by NEXUS and Ollama so you can confirm it.

## Development

```powershell
scripts\setup.ps1 -Dev      # adds pytest, ruff, mypy
scripts\dev.ps1             # backend with reload (:8765) + Vite dev server (:5173)
scripts\check.ps1           # ruff, mypy, pytest, tsc, vitest, production build
```

See [CONTRIBUTING.md](CONTRIBUTING.md).


## Assistant screen and shortcuts

NEXUS opens on the **Assistant** screen: press **Space** to talk, **Esc** to interrupt, or type. Shortcuts:
Ctrl+Alt+1 Assistant, 2 Chat, 3 Projects, 4 Changes, 5 Commands, 6 Tasks, 7 Notes, 8 Models, 9 Voice, 0 Settings;
Ctrl+K command palette. If NEXUS ever crashes, a watchdog restarts it automatically; native crash traces go to
`%LOCALAPPDATA%\NEXUS\logs\crash.log`.

Troubleshooting on Windows (crashes, missing tray icon, voice errors, which Python is used): `docs/TROUBLESHOOTING.md`
Speed on a CPU (fast model, what to expect): `docs/PERFORMANCE.md`
Apps panel, voice control of apps and Windows, reminders/alarms: `docs/APPS_AND_REMINDERS.md`
and `scripts\diagnose.ps1`.
