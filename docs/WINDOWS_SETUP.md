# Windows setup, operation and troubleshooting

Target: Windows 10/11 64-bit, i7-8650U, 32 GB RAM, MX130 (2 GB). Everything runs on the CPU;
no CUDA is assumed. Nothing here needs administrator rights.

## 1. Prerequisites

| Software | Version | Get it | Check |
| --- | --- | --- | --- |
| Python | 3.11+ (3.12 recommended) | python.org → tick **Add python.exe to PATH** | `py -3 --version` |
| Node.js | 18+ LTS | nodejs.org | `node --version` |
| Git | any (optional) | git-scm.com | `git --version` |
| Ollama | current | ollama.com/download/windows | `ollama --version` |

## 2. Install NEXUS

```powershell
cd C:\path\to\nexus
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Shortcut
```

Options: `-Voice` (speech recognition packages), `-WakeWord` (openWakeWord), `-Dev` (test tools),
`-Shortcut` (Start Menu entry). The script only installs into `.venv` and `frontend\node_modules`.

## 3. Ollama and the model

1. Install Ollama. It runs in the background and starts with Windows by default.
2. Download the default model once (≈4.7 GB):
   ```powershell
   ollama pull qwen2.5-coder:7b
   ```
   Or use **Models → Download** in NEXUS. Lighter alternatives: `qwen2.5-coder:3b`, `llama3.2:3b`.
3. Check: `ollama list` shows the model; NEXUS's header shows a green “Ollama” dot.

Performance tips for this laptop: keep **Settings → Context window** at 8192 (larger is slower and
uses more RAM); the MX130's 2 GB VRAM is too small to help much, so CPU speed (~3–8 words/s) is normal.
The first answer after a while includes model load time (10–30 s).

## 4. Start, stop, start with Windows

| Action | How |
| --- | --- |
| Start | Start Menu → NEXUS, or `scripts\start.ps1` (tray icon appears, browser opens) |
| Start with logs in a console | `scripts\start.ps1 -Console` |
| Open the UI | Tray icon → Open NEXUS, or http://127.0.0.1:8765 |
| Start at sign-in | Settings → **Start NEXUS when I sign in** (writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\NEXUS`; starts quietly in the tray) |
| Disable start at sign-in | Same toggle, or Task Manager → Startup apps → NEXUS → Disable |
| Pause | Tray → Pause assistant (stops listening and generation; no new chats/tasks) |
| Exit completely | Tray → **Exit NEXUS completely**, or `scripts\stop.ps1` (asks NEXUS to shut down cleanly, force-stops only if it doesn't respond) |

Only one copy runs at a time (a named mutex). Launching again just opens the UI. If the backend crashes
it is restarted automatically (up to 5 times in 10 minutes), and the tray shows a notification if it
can't start. After sleep/resume NEXUS re-checks Ollama and fires any reminders that came due while
the laptop slept (marked late). Sign-out/shutdown: SQLite in WAL mode survives abrupt termination;
commands or tasks that were running are marked as interrupted on next start.

## 5. Voice (optional)

See [VOICE.md](VOICE.md). Short version: `scripts\setup.ps1 -Voice`, then Voice → enable →
download `base.en` → use the microphone button in chat, or switch to wake-word mode.

## 6. Data, backup and recovery

| What | Where |
| --- | --- |
| Database | `%LOCALAPPDATA%\NEXUS\nexus.db` |
| Logs (rotating, secrets redacted) | `%LOCALAPPDATA%\NEXUS\logs\nexus.log` |
| Edit backups and DB backups | `%LOCALAPPDATA%\NEXUS\backups\` |
| Speech models | `%LOCALAPPDATA%\NEXUS\models\` |

- **Backup:** Settings → Your data → Back up database (consistent online copy), or exit NEXUS and copy `nexus.db`.
- **Restore:** exit NEXUS, replace `nexus.db` with a backup (delete any `nexus.db-wal`/`-shm` next to it), start NEXUS.
- **Automatic recovery:** if the database fails its integrity check at start-up it is renamed to
  `nexus.corrupt-<time>.db` (never deleted) and a fresh one is created; System health shows this.
- **Move data elsewhere:** set `NEXUS_DATA_DIR` in `.env` in the NEXUS folder (see `.env.example`).
- **Uninstall:** disable start at sign-in, exit NEXUS, delete the NEXUS folder, `%LOCALAPPDATA%\NEXUS`,
  and the Start Menu shortcut.

## 7. Offline verification

1. Finish all downloads (model, optional speech model).
2. Turn off Wi-Fi / unplug the network.
3. Start NEXUS, send a chat message, use voice if enabled, set a timer, open a project.
4. While it answers, run `powershell -ExecutionPolicy Bypass -File scripts\verify-offline.ps1`.
   It lists all TCP connections of NEXUS and Ollama and fails if any is not loopback.
5. Optional: Windows Resource Monitor → Network → filter `pythonw.exe` / `ollama.exe`.

## 8. Troubleshooting

| Symptom | Fix |
| --- | --- |
| “Ollama offline” | Start Ollama from the Start Menu or run `ollama serve`. Check http://127.0.0.1:11434 shows “Ollama is running”. |
| “Model not installed” | `ollama pull qwen2.5-coder:7b`, or pick an installed model on the Models page. |
| Very slow / laptop hot | Normal for CPU inference. Use `qwen2.5-coder:3b`, lower the context window, close other heavy apps, plug in power. |
| Port 8765 in use | Set `NEXUS_PORT=8766` in a `.env` file in the NEXUS folder (copy `.env.example`). |
| Nothing happens on start | Run `scripts\start.ps1 -Console` to see errors; check `%LOCALAPPDATA%\NEXUS\logs\nexus.log`. |
| No tray icon | `pystray`/`Pillow` missing: rerun `scripts\setup.ps1`. NEXUS still runs; use `scripts\stop.ps1` to exit. |
| “Missing X-Nexus-Client header” | You called the API directly; add `-H "X-Nexus-Client: cli"`. |
| Edits can't be applied | Enable **Allow edits** in the project's Settings tab. Conflicts mean the file changed after the proposal: propose again. |
| Commands can't be approved | Enable **Allow commands** for the project. `powershell`/`cmd` are blocked by design; run the program directly (e.g. `pytest -q`). |
| PowerShell blocks scripts | Use `powershell -ExecutionPolicy Bypass -File ...` as shown (affects only that run). |
| Microphone not found | Windows Settings → Privacy & security → Microphone → allow desktop apps; choose the device in Voice settings. |
| Database errors | See section 6; System health shows if recovery happened. |
| Export a diagnostics bundle | System health → Export diagnostics (JSON with redacted log tail). |

## 9. Command reference

```powershell
scripts\setup.ps1 [-Voice] [-WakeWord] [-Dev] [-Shortcut]   # install / update
scripts\start.ps1 [-Console] [-Background]                  # start
scripts\stop.ps1                                            # stop completely
scripts\dev.ps1                                             # development servers
scripts\check.ps1                                           # all tests and checks
scripts\verify-offline.ps1                                  # list network connections
.venv\Scripts\python.exe -m pytest -q        (in backend\)  # backend tests only
npm test                                     (in frontend\) # frontend tests only
```
