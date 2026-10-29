# Contributing to NEXUS

## Set up

```powershell
scripts\setup.ps1 -Dev
scripts\dev.ps1          # backend (reload) on :8765 + Vite on :5173 → open http://127.0.0.1:5173
```

On Linux/macOS (for backend/frontend development only — tray, startup and SAPI speech are Windows features):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
(cd backend && python -m uvicorn --factory app.main:create_app --reload --port 8765)
(cd frontend && npm install && npm run dev)
```

## Checks (run before every change)

```powershell
scripts\check.ps1
```

which runs: `ruff check .`, `mypy app`, `pytest -q` (backend) and `eslint src`, `tsc --noEmit`, `vitest run`,
`npm run build` (frontend). Tests never need Ollama or a microphone: `tests/conftest.py` provides a fake
provider, fake audio, fake STT/TTS and a fake registry. `tests/test_ollama_provider.py` checks the real
provider against mocked HTTP responses in Ollama's documented format.

## Rules of the codebase

1. **The model is untrusted.** Never give it a code path that executes, writes or deletes. New tools must
   produce proposals/pending requests that a user endpoint approves.
2. **Every filesystem access goes through `resolve_in_workspace`** (or `walk_files`/`read_text_file`).
3. **No shell.** Commands are argv lists through `tools/commands.py`.
4. **Never claim success you didn't observe.** Statuses come from real results (exit codes, file hashes).
5. **No network calls** except `OllamaProvider` (localhost) and explicit, audited, user-initiated downloads.
6. **Optional components degrade gracefully** and report *why* they're unavailable.
7. Database changes: append a migration to `MIGRATIONS` in `database/db.py`; never edit an applied one.
8. UI: every control must work or be visibly disabled with an explanation. No fake metrics.

## Adding a provider

Implement the `LLMProvider` protocol in `app/providers/` (health, list_models, loaded_models,
chat_stream, chat, pull, unload) and select it in `create_app`.

## Adding a workflow

Add a template to `TEMPLATES` in `app/agent/tasks.py` using the existing step kinds
(`gather_context`, `analyze`, `plan`, `propose_edit`, `await_changes`, `run_command`, `review`, `report`).
