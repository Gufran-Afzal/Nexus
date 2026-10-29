# NEXUS security model

NEXUS treats the language model as **untrusted**. The model can only produce text. Everything that
changes your computer goes through backend code that enforces permissions, and through explicit
approvals that only your UI actions can give.

## Trust boundaries

| Source | Trust | Consequence |
| --- | --- | --- |
| You, via the UI / tray | Trusted | Only your clicks call the approve/apply endpoints |
| Model output | Untrusted | Never executed; edits become proposals, commands become pending requests |
| Project files, NEXUS.md, retrieved content | Untrusted data | Wrapped as reference data in prompts; cannot change rules or permissions |
| Voice / wake word | Same as typed text | Can set timers, take notes, open *pre-approved* apps/sites/folders; can never approve edits or commands |
| Other websites in your browser | Untrusted | Blocked by Host/Origin checks and a required custom header (see below) |

## What is enforced in code (and tested)

**Local-only API** (`app/main.py`)
- Binds to `127.0.0.1` only.
- Rejects requests whose `Host` is not a loopback name (DNS-rebinding protection).
- Rejects requests with a foreign `Origin`.
- State-changing requests must carry `X-Nexus-Client`; browsers can only send it cross-origin after a CORS
  preflight that NEXUS refuses for unknown origins (CSRF protection).
- UI responses carry a strict Content-Security-Policy (no third-party scripts, no framing).

**Filesystem** (`app/workspaces/security.py`)
- Only folders you explicitly add are accessible; drive roots and your whole home folder are refused.
- Every path is canonicalised and must stay inside the project: `..`, absolute paths, UNC paths, drive
  letters, null bytes and **symlinks or Windows junctions that resolve outside** are rejected; the file
  walker never descends into links/junctions.
- Sensitive files are never listed, read, searched, sent to the model or edited: `.env*` (except
  `.env.example`/`.sample`/`.template`), private keys, certificates/keystores, `credentials*`, `secrets*`,
  `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, Terraform state, and the `.git`, `.ssh`, `.aws`, `.gnupg`,
  `.azure`, `.kube`, `.docker` directories.
- Dependency/build folders (`node_modules`, `.venv`, `dist`, `build`, …) are excluded as noise.

**Edits** (`app/agent/edits.py`)
- Projects are **read-only by default** (`allow_edits = false`).
- A change set is a proposal; only files you mark *accepted* are applied, and only by the apply endpoint.
- Each file records the hash of the content it was proposed against. If the file changed on disk since,
  it is marked **conflict** and left untouched (your work is never overwritten).
- Originals are copied to `%LOCALAPPDATA%\NEXUS\backups\` before writing; writes are atomic; revert
  refuses to touch files modified after NEXUS applied them.

**Commands** (`app/tools/commands.py`, `command_service.py`)
- Projects start with `allow_commands = false`.
- Every command is a pending request until you approve that exact argv. Destructive, package-install
  and network commands need a second explicit confirmation.
- Blocked outright: privilege elevation (`sudo`, `runas`, `-Verb RunAs`, `gsudo`…), shells
  (`cmd`, `powershell`, `bash`…), inline interpreter code (`python -c`, `node -e`…).
- No shell is ever used (`create_subprocess_exec` with an argv list), so `|`, `&&`, `>` are literal text.
- Working directory must be inside the project. Timeouts kill the whole process tree. Output is capped.
- Secret-looking environment variables (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`…) are removed
  from the child environment; captured output and logs are passed through a redactor.
- **This is approval-gated execution, not a sandbox.** An approved command runs with your user's rights.

**Agent tools** (`app/agent/tools.py`)
- Available only in coding conversations with a project. Arguments are validated by Pydantic, not trusted.
- Read-only tools (list, search, read, git status/diff) go through the same path checks as above.
- `propose_edit` creates a change set (status *proposed*); `propose_command` creates a command
  (status *pending approval*, or *rejected* if policy blocks it). The model is told, and the UI shows,
  that nothing has happened yet. There is no tool that applies, approves, deletes or runs anything.
- Bounded: 6 rounds, 4 calls per round, outputs cut to the context budget; Stop cancels between tools.

**Command runner on Windows** — runs with `subprocess.Popen` (works under every event loop), adds the
project's own `.venv`/`node_modules/.bin` to `PATH`, never flashes console windows, and refuses arguments
containing `& | < > ^ % " !` when the program is a `.bat`/`.cmd` file (cmd.exe would re-parse them).

**Audit log** — proposals, applies, reverts, command requests/approvals/results, permission changes,
model downloads, memory clearing and assistant actions are recorded (redacted) and visible in
System health → Audit log.

## Privacy

- No telemetry. Ollama is contacted at `localhost:11434` with `trust_env=False` (system proxies ignored).
- Network is used only when you explicitly download a model (Ollama pull, Whisper model) — both are audited.
- Raw audio is processed in memory and never written to disk. Transcribed text is saved like typed chat.
- Full source files are stored only inside change sets you create (needed for review and revert).
- Everything lives in `%LOCALAPPDATA%\NEXUS` (database, logs, backups, speech models).

## Known limitations

- Prompt injection inside project files can still influence the *text* the model writes (e.g. a bad
  suggestion). It cannot cause actions: those require your approval. Read proposals before applying.
- Keyword-based secret redaction is best-effort.
- The local API is reachable by any program running as your user on this machine (same as any local
  dev server). It is not reachable from the network.

Report a security problem by opening a private issue/email to the maintainer of your copy.
