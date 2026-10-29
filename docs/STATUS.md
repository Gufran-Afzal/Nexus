# Status: what is done, what was verified, what remains

Built and verified on 15 Oct 2026 (release 0.3.3) in a Linux build environment (Python 3.13, Node 22). The Windows-only
parts are implemented but **could not be executed there**; they need a first run on your laptop.

## Release 0.7.0: an automation runtime (plan, act, check, recover, resume)

You asked for a reliable, resumable automation agent rather than a smarter-sounding chatbot. See `docs/AUTOMATION.md` (design and
limits), `docs/BENCHMARK.md` (before/after) and `docs/DEMOS.md` (five end-to-end transcripts).

- **New:** tool contract + registry with 29 tools; plans validated before they run; observe-act-verify; approval policy by risk;
  retries, recovery tools, replanning, budgets; durable runs that survive a restart without repeating a launch; cancel, approve,
  decline, resume; saved workflows and verified lessons; the **Automation** page; self-check; duplicate guards for reminders,
  notes and to-dos in every path.
- **Changed behaviour:** "open/close/switch to <app>" now confirms the window really appeared/disappeared and says so if it
  did not; an unknown app or an unapproved folder is a clear refusal; free-form multi-step requests go to the planner when it is
  enabled (Automation page → "Plan multi-step tasks"). Turn it off and 0.6.0 behaviour returns.
- **Verified here (Linux, fake desktop, scripted model):** backend tests on Python 3.11 (413 passed, 3 skipped), 3.12 (416, including the real faster-whisper signature check) and 3.13 (414 passed, 2 skipped), 27 frontend tests,
  ruff, mypy, eslint, tsc, production build, the 14-scenario benchmark, five transcripts.
- **Not verified, needs your laptop:** everything involving real windows on Windows (list, focus, close, Esc on a dialog),
  and the quality and speed of Qwen 2.5 7B as planner. No UI Automation trees and no vision (documented, not hidden).

## Release 0.6.0: wrong words, a conversation, and working inside apps

You said it still misheard words, that the voice should feel like ChatGPT voice, and that apps could only be opened and
closed, not used (a WhatsApp message, a Gmail draft in Chrome). See `docs/SKILLS.md` and the 0.6.0 section of `docs/VOICE.md`.

- **Wrong words:** your app names, people and "My words" are given to Whisper as vocabulary and repaired afterwards
  ("what's app" -> WhatsApp); people's names are hints only and are never auto-corrected. New models: distil-small/medium/large-v3.
  Every request carries a recognition confidence; unsure message/email/app commands are read back and need a "yes".
- **Conversation:** follow-up listening (reply without "Hey Nexus", ends on silence or "thanks"), live captions while you
  speak (if tiny.en is downloaded), confirmation by voice or by the card at the bottom right.
- **Inside apps:** People page (contacts with validated phone/email), skills for WhatsApp / Teams messages, Gmail drafts,
  Calendar events, Maps, YouTube, new Docs/Sheets/Slides, typing into an integrated app, your own "open this site"
  commands, and a model-driven router for free-form requests. The model writes email drafts. Nothing is sent until you
  confirm; the Send key press only happens if the right window is in front.
- Safety checks added along the way: a bare "type ..." with no app named is not a command (it would paste into whatever
  is in front); terminals are never typed into; only an allowlist of key combinations can be pressed.
- Not done: barge-in (talking over NEXUS), reading incoming messages/email, Telegram/SMS.
- Verified (Linux build environment): backend tests on Python 3.11 / 3.12 / 3.13 (3.12 includes a check against the real
  faster-whisper `transcribe` signature), ruff, mypy, frontend tests, eslint, tsc and the production build, plus a live
  server with seeded people and a real "message Alexandre Reed..." turn that produced the "Did you mean Alexandra Reed?" card.
- **Not verified, needs your laptop:** real microphone accuracy (nothing here can measure whether words are now right),
  WhatsApp desktop and Gmail actually receiving the draft and accepting Enter / Ctrl+Enter, window-title matching,
  clipboard paste, SAPI speech, and the router's speed with the 7B model (expect 5-15 s on this CPU).

## Release 0.5.0: smarter reminders, Apps panel, voice control of apps

You said reminders were basic (just a notification), apps needed typed exe names, and the integration panel should list
every Windows app and let you drive them by voice. See `docs/APPS_AND_REMINDERS.md`.

- **Reminders:** rings with a pop-up (Snooze/Done), sound, tray notification and **spoken alert**; re-rings if ignored;
  repeats (daily/weekday/weekly/monthly); alarms; labelled timers; "…and open <app>" actions; natural-language times;
  voice "snooze"/"stop the alarm". Existing reminders are kept (database migration 3).
- **Apps panel:** scans Start Menu, Store apps and registry, one-click Add / Add all, aliases, on/off, Open/Close tests.
  The old "name = exe" settings box is replaced (existing entries are imported).
- **Voice/text:** open, close, switch to apps; search in a browser; Spotify search; open a folder in VS Code; web search;
  email draft; media keys, volume, lock, screenshot, show desktop, Settings pages. Strict patterns: 16 ordinary
  sentences ("search for a bug in my code", "close the loop on that issue") still go to the model.
- Fixed: removing an entry from a dict preference (e.g. allowed apps) never worked because values were merged.
- Verified: 340 backend tests, ruff, mypy, frontend tests/lint/typecheck/build, live-server screenshots of the Apps
  page and the alarm pop-up (Linux). **Not verified on Windows**: the PowerShell scan, Store-app launch, window
  focus/close, media/volume keys, lock, screenshot, spoken alert. These are the first things to try on your laptop.

## Release 0.4.1: wake-up reliability and recognition accuracy

You said NEXUS heard the wrong words, did not wake up, and you had to repeat yourself.

Defects found by reading the code, and fixed:
- **Wake spotting used the weakest model (`tiny.en`) by default.** It now uses your main model unless you choose otherwise.
- **Greedy decoding** (beam 1, one temperature) replaced by beam search with temperature fallback for requests, and a
  separate cheap "wake" decode primed with the wake phrase (`initial_prompt` + `hotwords`).
- **Quiet microphones**: audio is now level-normalised (DC removed, gain up to 25x) before recognition.
- **Words clipped at the start**: 400 ms of audio before the speech gate opens is kept (pre-roll).
- **Fixed loudness gate** replaced by an adaptive one (noise floor x3, minimum 150) that follows the room.
- **Lost audio between "Hey Nexus" and the command**: one continuous microphone stream is now shared by wake listening
  and recording, and discarded after a reply so NEXUS doesn't hear itself.
- **Phrase matching**: several phrases (`hey nexus, ok nexus`), common mishearings ("hay/hi/a nexus", "next us"), and
  space-insensitive fuzzy matching.
- **Hallucinated text** from silence ("Thanks for watching") is dropped. Vocabulary prompt helps spell "Nexus", timers, reminders.
- A bare "Hey Nexus" now waits ~6 s for the command, then gives up instead of waiting 60 s.

New on the Voice page: **Test your microphone** (what was heard, level, timing, advice) and **Recent wake-ups**
(what NEXUS understood each time, and whether it counted).

Verified here: 259 backend tests, ruff, mypy, frontend tests/lint/typecheck/build. Not verified: a real microphone, real Whisper
accuracy, Windows audio. The accuracy gains are expected from the changes above, **not measured**. Run the microphone test
and send me the result if it is still wrong.

## Release 0.4.0: speed, instant answers, Apple-style dashboard

You said answers, voice and recognition felt slow and the dashboard did not feel premium.

**What was slow, found by reading the code (not measured on your laptop):**

| Cause | Change |
| --- | --- |
| The model was unloaded after 5 minutes idle, so many questions paid a multi-second reload | Kept loaded for 60 min, and loaded in the background at start-up (and by the dashboard's "Wake up") |
| The system prompt contained the current minute, so it changed every minute and Ollama had to re-read the whole prompt each time | Clock moved to the end of the newest message; the system prompt is now identical between requests so Ollama can reuse its cache |
| 8192-token context and a long prompt for simple questions | 4096 context (also keeps one value everywhere, since Ollama reloads the model when it changes), a short assistant prompt, 12 messages of memory, answers capped at 400 tokens in assistant/voice |
| Speech started only after the whole answer was written | The reply is spoken sentence by sentence while the model is still writing (never reads code aloud) |
| Windows speech started a new PowerShell for every reply (about a second) | One long-lived speech process, started ahead of time; interrupt kills it and a fresh one starts in the background |
| Speech-recognition model loaded at the first request | Loaded when listening starts; wake-phrase spotting uses `tiny.en` when it is already downloaded; Whisper decoding uses the fastest single-pass settings |
| Waiting 1.3 s after you stop talking | Default 0.9 s (the "unfinished sentence" check still protects long sentences) |

**Instant answers (no model):** time, date, arithmetic ("18% of 240"), unit conversion, timers/reminders/to-do/notes
questions, cancel timers, focus and break timers, computer status (CPU, memory, battery, disk), coin and dice, and
memory ("remember that my cat is Miso", "what do you remember", "forget ..."). Remembered facts are kept as notes titled
"Memory" (visible and deletable under Notes) and are included in the assistant's prompt. Patterns are strict: 15 ordinary
questions are tested to still reach the model.

**Dashboard:** new design system (Apple-style materials, system colours, capsule controls, light and dark with a
toggle), widgets for clock, timers with live rings, to-do, model status (loaded or sleeping, words per second, load time),
computer load, and quick notes, a Spotlight-style Ctrl+K launcher that answers calculations and conversions as you
type and can hand a question to the assistant, and a mobile tab bar. Settings has a new "Fast model" field.

**Verified here:** 236 backend tests (also on Python 3.11 and 3.12), ruff, mypy, frontend tests, ESLint, tsc and build;
screenshots in dark, light and phone width; the persistent speech process, interrupt and restart against a stand-in
process; sentence chunker in Python and TypeScript.
**Not verified:** actual speed on your laptop (no Ollama, Whisper model or Windows speech was available here), the
real Windows speech process, a live model reading the shorter prompt. Expect the largest remaining cost to be the 7B
model itself on a CPU: see `docs/PERFORMANCE.md` for the one change that matters most (a small fast model).

## Release 0.3.3: your crash logs, hands-free listening, long sentences

**Evidence from your logs:** two separate faults.

| Finding | Status |
| --- | --- |
| Tray start raised `ValueError`; the watchdog then restarted in a loop and ended with exit code 1 | **My bug, fixed.** A pystray menu callback may take at most 2 positional parameters; mine took 3. Fixed, with a regression test that builds the real pystray menu. If the tray still fails, NEXUS now runs without it instead of exiting |
| 13 access violations, all at `faster_whisper/transcribe.py:689` (`ctranslate2.models.Whisper(...)`, the native model load), called from the wake-word loop | Located exactly. **Why** CTranslate2 faults there on your CPU/runtime is still unconfirmed. NEXUS now tries a ladder of safer engine settings (fewer threads, forced AVX2, float32, generic CPU path) in the worker, remembers the one that works, and trips the circuit breaker if none do. `scripts\diagnose.ps1` runs the same load per setting and reports which one survives |
| Wake-word listening had to be started by hand | Once voice is enabled, the default is now wake-word mode and listening starts automatically (still opt-in: voice is off until you turn it on). The Assistant screen shows "Listening for Hey Nexus" and shows hands-free requests and replies live |
| A pause in a long sentence ended the request | Fixed. After a pause NEXUS checks whether the sentence sounds finished (trailing "and", "because", a comma...) and keeps listening up to 2 x 1.6 s more. "Hey Nexus" plus part of a command waits for the rest. Speech detection adapts to room noise. Settings: "wait after I stop talking" (default 1.3 s), "unfinished sentence" toggle. 16 new tests, one of which caught a real bug (a loud first frame blinded the noise detector) |

The "does the sentence sound finished" check is a simple heuristic on the transcript (final word and punctuation), not
understanding; it can wait when it need not, or send early. It never waits forever.
**Not verified:** anything on Windows, a real Whisper model load/transcription, real microphone behaviour, the wake-word
live events in a browser (no automated test), and whether an old Visual C++ runtime is the cause of the model-load crash.

## Release 0.3.2: crash report from your Windows laptop

Evidence you supplied: `exited unexpectedly (code 3221225477)` = 0xC0000005 access violation, faulting module
`MSVCP140.dll` 14.32.31326 (a 2022 runtime), Python 3.12.10, faster-whisper 1.2.1, openwakeword 0.6.0,
onnxruntime 1.31.0. This proves the 0.3.1 watchdog works on your machine. It does **not** yet identify the package.

| Finding | Status |
| --- | --- |
| The crash was in the *server* process because 0.3.1 still ran speech recognition / openWakeWord in it | Fixed: they now run in a separate speech worker; a crash there becomes an error message, with a circuit breaker (3 crashes -> paused 10 min). 4 new tests: round trip, crash + recovery, hang kill + breaker, error without dying |
| Which package crashes | **Unknown until you run `scripts\diagnose.ps1`.** It tests each package in its own process and lists Visual C++ DLL versions. An outdated runtime (14.32 is older than 14.40) is a known cause for packages built with a newer toolset, so it is the first thing to try, but it is not assumed |
| Missing tray icon | Explained by code, not a bug: `start.ps1 -Console` passes `--no-tray`. Start without `-Console`. The script and console now say so |
| PowerShell execution policy | Not a NEXUS fault; the venv never needs activating. Documented |
| `setup.ps1` could pick the newest Python (it used `py -3`) | Fixed: prefers 3.12 -> 3.11 -> 3.13, `-Python` override, refuses unsupported `.venv` without deleting it, runs the isolated native check after `-Voice`/`-WakeWord` |
| Existing database, `.venv`, data | Untouched; nothing here deletes or reinstalls anything |

New: `scripts\diagnose.ps1`, `backend\native_check.py`, `docs/TROUBLESHOOTING.md`.
Verified on Linux only: worker protocol and crash handling (real child processes), `native_check.py` against real
onnxruntime/CTranslate2/PyAV (hand-built ONNX model runs), PowerShell scripts parse and `diagnose.ps1` runs end to end
under PowerShell 7. **Not verified:** any of it on Windows, and that the Visual C++ runtime is the cause.

## Release 0.3.1: Voice tab crash and the Assistant screen

**Reported:** clicking the Voice tab made NEXUS stop (terminal process gone, browser "lost connection"). It also
looked like a plain admin app, not an assistant.

**Cause (not reproduced here; strongly suspected):** opening the Voice page made the *server process* import
PortAudio, faster-whisper and openWakeWord and enumerate audio devices. On some Windows setups that kills the whole
process outright (native access violation or duplicate-OpenMP abort), which no `try/except` can catch. On Linux the
same requests were stable, so this is a Windows-only hypothesis. If it still happens, send `crash.log` and `nexus.log`
from `%LOCALAPPDATA%\NEXUS\logs`.

**Fixes:**

| Fix | Evidence |
| --- | --- |
| "Is X installed?" uses `importlib.util.find_spec` and never imports native libraries in the server | test asserts `sounddevice` is not imported |
| Microphone listing runs in a short-lived child process, serialised and cached 20 s; a crash, hang or garbage there becomes "microphone unavailable" | tests: child `os._exit`, hang, garbage output, concurrent callers share one probe |
| Windows voice list cached 5 minutes (PowerShell is slow) | code |
| **Watchdog**: `run_nexus.pyw` runs the real launcher as a child and restarts it after any abnormal exit (max 5 per 10 min, quiet restart, no extra browser tab); clean exit and port conflicts end it | unit tests with a fake process; real run: `kill -9` of the child, server was back in seconds, graceful shutdown left no processes |
| `faulthandler` writes native crash stacks to `logs\crash.log` | code |

**New Assistant screen** (opens by default): a live "core" whose rings follow the real microphone level while you
speak and change colour/motion for thinking, answering and speaking; one-press talk (Space) and interrupt (Esc);
typed commands; spoken replies via Windows voices; a "Coming up" panel with live timers/reminders; one-tap example
commands; a voice pipeline checklist. Pressing the mic is the explicit opt-in that turns voice on.
Verified with real Chromium screenshots against the real backend (stand-in for Ollama) and 2 new Vitest tests
(10 -> 12). Not verified: Windows speech output, real Whisper transcription, your microphone.

Honest limits: this is a local web app plus tray, not a floating desktop overlay, and a 7B model on a CPU answers in
seconds, not instantly. It is a capable local assistant, not the film character.

## Final audit (release 0.3)

Independent checks added this round (beyond NEXUS's own tests):

| Check | Result |
| --- | --- |
| Full test suite on **Python 3.11, 3.12 and 3.13**, installed only from `requirements*.txt` | 150 passed on each |
| Fresh install from the zip into an empty folder (`npm ci`, build, all tests, ESLint) | passed |
| Real launcher from the fresh copy: single-instance lock, port-conflict message, agent chat over Ollama's wire format, graceful shutdown | passed |
| Every `.ps1` script parsed by the real PowerShell 7.5 parser; PSScriptAnalyzer incl. Windows PowerShell **5.1** syntax compatibility | no errors (only expected `Write-Host` style warnings) |
| PowerShell snippets used for Windows speech: parsed; stdin/UTF-8/env handling executed | passed |
| Optional packages installed and their APIs checked against NEXUS's calls (faster-whisper 1.2.1, PyAV 19, Pillow, openWakeWord metadata) | 1 incompatibility found and fixed (below) |
| ESLint (TypeScript + React hooks rules) | clean |
| Bug-pattern lint (dangling tasks, closure-in-loop, raise-from, async blocking calls) | clean |

Defects found and fixed in this round:

| # | Problem | Impact | Fix |
| --- | --- | --- | --- |
| 13 | faster-whisper 1.2.1 calls a PyAV option that PyAV 19 removed | Every browser push-to-talk transcription failed on a fresh install | NEXUS decodes audio with PyAV itself; tested with a real WebM/Opus recording |
| 14 | Tray detection caught only `ImportError` | Startup crash instead of falling back when the tray backend fails | Any tray failure falls back to running without the icon |
| 15 | uvicorn reports a failed port bind with `SystemExit` | Supervisor thread died silently | Handled and logged; restart/notification logic applies |
| 16 | Tool-call arguments (e.g. a proposed file) not counted in the context budget | Long agent sessions could overflow the model's context | Counted; results trimmed to the remaining budget |
| 17 | Qwen sometimes emits `<tool_call>{…}</tool_call>` as text | Tool call lost and raw JSON shown in chat | Parsed as a tool call and hidden from the chat |
| 18 | Prompt-building/tool-setup errors raised inside the stream | Reply stuck as “streaming”, no error shown | Error event + saved error message |
| 19 | Replies streaming during a crash/shutdown stayed “streaming” forever | Confusing history | Marked “stopped” on next start |
| 20 | `stop.ps1` force-killed NEXUS | Commands NEXUS had started (e.g. `pytest`) left running as orphans | Graceful `/api/system/shutdown` first (stops commands, saves state), force-stop only as fallback |

## Self-audit (release 0.2)

Defects found in release 0.1 and fixed, each with a regression test where testable:

| # | Problem in 0.1 | Impact on your laptop | Fix |
| --- | --- | --- | --- |
| 1 | Commands used asyncio subprocesses; `uvicorn --reload` on Windows uses a Selector loop that can't spawn them | Every approved command failed in dev mode | Loop-agnostic runner (`Popen` + reader threads); tested on a Selector loop |
| 2 | Under `pythonw.exe` `sys.stdout/stderr` are `None` | Speech-model download progress bars would crash the app | stdout/stderr redirected to `logs\console.log`; console log handler only when a console exists |
| 3 | `git` subprocesses without `CREATE_NO_WINDOW` | Console windows flashing on screen | All child processes created without windows |
| 4 | Commands used whatever `python`/`pytest` was first on PATH | Tests ran in the wrong environment (not the project's `.venv`) | Project `.venv\Scripts`, `venv`, `node_modules\.bin` prepended to PATH |
| 5 | `.bat`/`.cmd` programs (npm, npx) get re-parsed by cmd.exe | Argument injection via `&`, `|`, `%` | Such arguments refused for batch programs |
| 6 | `os.walk` treats Windows junctions as normal folders (Python < 3.12) | Possible walk outside the project | Links and junctions are never descended into |
| 7 | Browser textareas and models produce LF line endings | Editing a CRLF file showed a whole-file diff and changed line endings | Proposals keep the file's line-ending style |
| 8 | Port 8765 already in use | Silent crash loop | Clear message box with the fix |
| 9 | Tray toggles flipped state even when the action failed | Tray showed “listening” when it wasn't | Tray reflects real results and live events; dynamic labels (Start/Stop, Mute/Unmute, Pause/Resume) |
| 10 | Changing voice settings while listening kept old settings | Wake phrase changes ignored | Listener restarts with new settings |
| 11 | “run the tests” was parsed as “launch an app called tests” | Coding requests intercepted | “run/start X” only launches approved app names |
| 12 | Context retrieval read every project file on every message, on the event loop | Slow, server-blocking on large repos | Incremental cached BM25 index, run off the event loop |

New capabilities from the spec that 0.1 lacked:

- **Tool-calling coding agent** (spec §11): the model searches/reads files itself and proposes edits/commands for approval, live trace in chat, native Ollama tools with text fallback, bounded rounds.
- **Advanced retrieval** (spec §10/§19): BM25 with identifier splitting, optional local semantic search with incremental embedding index, rank fusion, excerpts of large files.
- **Configurable model roles** (spec §19): separate models for coding chat, tasks and embeddings.
- Lazy-loaded UI pages (smaller initial bundle).

## Verification performed

| Check | Result |
| --- | --- |
| Backend tests (`pytest`) | **150 passed** (Python 3.11, 3.12, 3.13) — API, provider (mocked HTTP), path security, secrets, edits, commands, tasks, voice (simulated audio), actions, scheduler, database recovery/restart/backup/v1→v2 migration, agent tools, retrieval |
| `ruff check .` / `mypy app` | clean / no issues (56 files) |
| Frontend tests (`vitest`) + ESLint | **10 passed**, lint clean — SSE parsing, chat streaming + context display + error state + live agent steps, diff view, palette, accessible toggle |
| `tsc --noEmit`, production build | clean; main bundle 550 KB (170 KB gzip), other pages lazy-loaded |
| Live smoke test | Real server + real `OllamaProvider` against a stand-in server speaking Ollama's documented API: health, model list, workspace, coding chat with file context (`.env` excluded, verified absent from DB), CSRF rejection, timer action, CSP header, clean shutdown; 0.2: native tool call over the wire, real `git` command through the new runner, embedding-model check |

Not possible in the build environment: a real Ollama/qwen2.5-coder model, a browser for visual checks
(the Playwright browser download was blocked), PowerShell, the Windows registry, the tray, microphones
and Windows speech.

## Acceptance criteria

| Criterion | State |
| --- | --- |
| Launches on Windows | Implemented (`run_nexus.pyw`, `scripts\start.ps1`) — **needs first run on Windows** |
| Starts after sign-in | Implemented (HKCU Run, opt-in, logic unit-tested with a fake registry) — **verify on Windows** |
| Tray + complete exit | Implemented (pystray, single-instance mutex, supervisor) — **verify on Windows** |
| Detects Ollama and models | Done, tested (mocked) |
| Connects to qwen2.5-coder:7b, streams real responses | Done against Ollama's documented API (mocked); **verify with the real model** |
| Stores history locally | Done, tested (incl. restart persistence) |
| Project workspaces, bounded context | Done, tested |
| Protects secrets by default | Done, tested |
| Proposes and reviews code changes | Done, tested (manual + model proposals, diff, accept/reject, stale detection, backup, revert) |
| Approval for edits and commands | Done, enforced server-side, tested |
| Runs approved tests, reports actual results | Done, tested with real pytest subprocesses |
| Local audit log | Done, tested |
| Task-planning workflow | Done, tested end-to-end (propose → apply → approve tests → review → report), cancel, retry |
| Local voice when models installed | Implemented; tested with simulated audio — **needs a real microphone test on Windows** |
| Optional wake word | Implemented (two engines); tested with simulated audio — **not yet tested on Windows, so not claimed to work** |
| Recovers from missing optional dependencies | Done, tested (no Ollama, no model, no mic, no STT, no wake-word model, corrupt DB) |
| No silent cloud access | Done by design (no cloud code paths; `trust_env=False`); `verify-offline.ps1` provided — **run it on Windows** |
| Security boundaries tested | Done (traversal, symlink escape, sensitive files, CSRF/host/origin, blocked commands, permissions) |
| Setup and troubleshooting docs | Done (`docs/WINDOWS_SETUP.md`, `docs/VOICE.md`); PowerShell scripts are **unexecuted** — report any error message |

## First-run checklist on your laptop

1. `scripts\setup.ps1 -Voice -Shortcut` completes without errors.
2. `ollama pull qwen2.5-coder:7b`, then `scripts\start.ps1`: tray icon appears, browser opens, header shows Ollama green.
3. Ask a question → streamed answer; press Stop mid-answer.
4. Add a project, ask in Coding mode → agent steps appear (Search, Read…) and “Answer used N files” lists real files.
5. Propose an edit, apply it (enable Allow edits), revert it.
6. Request `pytest -q` (or `npm test`), approve it, see the real output.
7. Settings → Start with Windows → sign out/in → NEXUS starts in the tray; tray → Exit closes everything.
8. Voice → download `base.en` → mic button in chat → wake word mode → “Hey Nexus, set a timer for one minute”.
9. Wi-Fi off → repeat 3–4 → `scripts\verify-offline.ps1` reports only loopback connections.

## Not implemented (optional modules, deliberately left out of this release)

- Language-server integration, plugin interface, local developer-documentation search.
- Voice barge-in (interrupting by speaking while NEXUS talks); interrupt is via the UI or tray.
- Playwright end-to-end browser tests (component tests with Vitest + RTL are in place).
- Monaco editor (see ARCHITECTURE.md for why).
