# NEXUS architecture

## Overview

```
 ┌─────────────── Windows user session ────────────────────────────────────────────┐
 │  pythonw run_nexus.pyw  (one process, single-instance mutex)                    │
 │   ├─ Tray icon (pystray)  ── Open / Listen / Mute / Pause / Settings / Logs / Exit│
 │   └─ Supervisor thread ── uvicorn ── FastAPI app (127.0.0.1:8765)                │
 │         ├─ REST + SSE API  (/api/...)          ◄── browser UI (React, same origin)│
 │         ├─ Services: conversation, actions, scheduler, tasks, commands, voice    │
 │         ├─ SQLite (WAL)  %LOCALAPPDATA%\NEXUS\nexus.db                           │
 │         └─ OllamaProvider ──HTTP──► Ollama (127.0.0.1:11434)  [separate app]     │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Desktop shell | Browser UI + tray icon, **no Electron/Tauri** | The prompt asked for a wrapper only with a concrete advantage. A wrapper would add ~150 MB and a second runtime; the browser already provides mic access, notifications and a good editor surface. The tray covers “always there”. |
| Backend | FastAPI + Pydantic, async | Streaming (SSE), cancellation and subprocess control are natural in asyncio. |
| Persistence | SQLite via SQLAlchemy 2, WAL mode | Single file, crash-safe, no server. |
| Migrations | Small versioned migration list (`database/db.py`) instead of Alembic | One user, one file; ordered Python migrations are enough and avoid another dependency. Corrupt databases are moved aside (never deleted) and recreated. |
| Streaming | `text/event-stream` over `fetch` POST | Lets the chat request body and the stream share one request; stop is a separate call so the server can cancel the Ollama stream cleanly. |
| LLM access | `LLMProvider` protocol + `OllamaProvider` | Only documented Ollama endpoints (`/api/version`, `/api/tags`, `/api/ps`, `/api/chat`, `/api/pull`, `/api/generate` with `keep_alive: 0` to unload). Another engine (llama.cpp server, LM Studio) only needs a new provider class. |
| Concurrency | `LLMService` semaphore (default 1) | On a 4-core CPU, parallel generations just slow each other down. |
| Model memory | Ollama `keep_alive` (default 5 min), explicit “Unload” button | The 7B model is loaded only when used and released after idle. NEXUS itself stays at ~100 MB. |
| Context | Cached BM25 index (`retrieval/index.py`) + optional semantic index, fused by reciprocal rank | The keyword index re-reads only files whose mtime/size changed, so each message costs a directory walk, not a full read. Semantic search is opt-in per project: it needs a small embedding model (`nomic-embed-text`), requested with a 2-minute keep-alive so it doesn't stay in RAM beside the chat model. Large files matched semantically are included as excerpts around the matching lines. Every answer lists included and omitted files. |
| Coding agent | Bounded tool loop (`agent/loop.py`, `agent/tools.py`) | Up to 6 rounds × 4 calls. Native Ollama tool calls when the model supports them; automatic fallback to a fenced-JSON protocol (hidden from the chat) when it doesn't. Read-only tools run immediately inside the workspace boundary; `propose_edit` / `propose_command` only create pending items. Tool output is cut to fit the remaining context budget. |
| Model roles | `model_roles` preference (coding, tasks, embedding) | Lets you use e.g. a 3B model for tasks and the 7B for coding chat; still one chat model per request. |
| Edits | Model returns whole files; NEXUS computes diffs | Small models produce broken unified diffs far more often than complete files. |
| Commands | argv lists, no shell, risk classification, per-run approval | Removes shell-injection classes entirely; approval is enforced server-side. |
| Personal actions | Deterministic intent parser (regex) | Timers and reminders must be real, not hallucinated. Unrecognised requests go to the model. |
| Voice | Separate modules: capture, VAD, wake word, STT, TTS, orchestration | Each part can be missing; the service reports exactly which. |
| STT | faster-whisper (MIT), int8 on CPU, models downloaded only on request | Best accuracy/speed on CPU; loads from a local folder with `local_files_only`. |
| TTS | Windows System.Speech via a PowerShell child process (default), Piper as optional external program | Zero downloads, fully offline, and interruptible by terminating the child process. Piper is now GPL-3.0 (OHF-Voice/piper1-gpl), so NEXUS invokes a user-installed executable instead of bundling it. |
| Wake word | Two engines: Whisper-keyword (any phrase, default “hey nexus”) and openWakeWord | openWakeWord has no pre-trained “hey nexus” (you can train one); the Whisper-keyword engine works with any phrase by transcribing only short bursts of speech found by an energy gate. |
| Monaco | Not used | Monaco's React wrapper loads from a CDN by default (bad for offline) and adds several MB; the needs here (preview, diff, simple edit) are met by highlight.js, a custom diff table and a textarea. |
| Component library | Small in-house primitives in shadcn style (`components/ui.tsx`) | Avoids the shadcn CLI/Radix dependency tree for ~10 simple components. |

## Backend layout (`backend/app`)

| Package | Responsibility |
| --- | --- |
| `core/` | settings, logging with secret redaction, event bus, shared state |
| `database/` | ORM models, migrations, integrity check, backup |
| `providers/` | `LLMProvider` protocol, `OllamaProvider` |
| `services/` | `llm` (concurrency/cancel), `chat` (prompt + budget), `conversation` (one path for typed and spoken turns), `actions` (timers, reminders, notes, launches), `scheduler`, `preferences`, `audit` |
| `workspaces/` | `security` (path validation, sensitive files), `files` (tree, read, search, git, NEXUS.md) |
| `retrieval/` | `index` (incremental BM25), `context` (bounded builder, rank fusion, excerpts), `semantic` (optional embeddings) |
| `agent/` | `edits` (proposals, diff, apply, revert, CRLF-preserving), `proposer` (model → file rewrites), `tasks` (workflow engine), `tools` + `loop` (tool-calling agent) |
| `tools/` | command classification + runner, command lifecycle |
| `voice/` | audio capture + VAD, STT, TTS, wake word, orchestration |
| `windows/` | start-at-sign-in (HKCU Run), tray launcher + supervisor |
| `api/` | HTTP routes |

## Key flows

**Chat turn** (`services/conversation.py`): save user message → if it is a recognised action
(timer, reminder, note, to-do, agenda, open app/site/folder) execute it and reply with the real result →
otherwise build the prompt (persona + safety rules + optional NEXUS.md + project summary; bounded file
context; most-recent history that fits) → stream from the model → persist the reply with status
(`complete`/`stopped`/`error`), context files, omitted files and measured speed.

**Agent turn** (coding mode with a project, “Agent tools” on): the prompt is built as above, then
`run_agent` calls the model with tool schemas. Each requested tool is validated with Pydantic, executed
(read-only) or turned into a pending change set / command, streamed to the UI as a step, and its result
fed back. The loop ends when the model answers without tools, after 6 rounds, or when the context budget
is nearly spent. The trace is saved with the message.

**Task** (`agent/tasks.py`): sequential steps with dependencies and retry for model errors only.
`propose_edit` creates a change set; `await_changes` waits for you to apply/reject it; `run_command`
creates a pending command and waits for your approval; `report` lists recorded outcomes and labels model
statements as unverified unless a command result backs them.

**Voice** (`voice/service.py`): wake loop thread reads 80 ms frames → detector → acknowledgement beep →
record until silence → transcribe → the same conversation path as typed chat (source = voice) → speak
the answer → back to listening. Browser push-to-talk records with MediaRecorder and uploads to
`/api/voice/transcribe`; the transcript is placed in the message box for you to send.

## Resource use on the target laptop

- Idle: NEXUS ≈ 100 MB RAM, near-zero CPU (scheduler ticks once per second; Ollama health check every 30 s).
- Wake word (Whisper-keyword engine): energy gate on every frame (cheap); Whisper runs only on short bursts of speech.
- qwen2.5-coder:7b Q4: ≈ 5–6 GB RAM while loaded; unloaded after `keep_alive` idle time.
- Whisper base.en int8: ≈ 300 MB RAM after first use (loaded lazily on the first transcription).
