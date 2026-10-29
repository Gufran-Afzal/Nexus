"""Controlled command execution.

Commands are argv lists executed WITHOUT a shell. Every command needs an explicit user approval in
the UI; this module classifies risk so the approval prompt is honest, and outright blocks commands
that cannot be made safe (privilege elevation, inline interpreter/shell code).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.redact import redact, scrub_env


@dataclass
class Classification:
    risk: str  # verification | unfamiliar | destructive | package_install | network | blocked
    reasons: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.risk == "blocked"


ELEVATION = {"sudo", "su", "runas", "doas", "gsudo", "pkexec"}
SHELLS = {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "fish"}
INTERPRETER_INLINE = {
    "python": {"-c"}, "python3": {"-c"}, "py": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
    "ruby": {"-e"}, "perl": {"-e", "-E"}, "deno": {"eval"},
}
DESTRUCTIVE = {"rm", "rmdir", "del", "erase", "rd", "format", "mkfs", "shred", "diskpart", "dd",
               "remove-item", "move", "mv", "reg", "bcdedit", "shutdown", "taskkill", "kill", "killall"}
NETWORK = {"curl", "wget", "invoke-webrequest", "iwr", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet",
           "rsync"}

VERIFICATION_PREFIXES: list[tuple[str, ...]] = [
    ("pytest",), ("python", "-m", "pytest"), ("py", "-m", "pytest"), ("python3", "-m", "pytest"),
    ("ruff", "check"), ("ruff", "format", "--check"), ("python", "-m", "ruff", "check"),
    ("mypy",), ("python", "-m", "mypy"), ("pyright",), ("flake8",), ("black", "--check"),
    ("npm", "test"), ("npm", "run", "test"), ("npm", "run", "lint"), ("npm", "run", "typecheck"),
    ("npm", "run", "build"), ("npx", "vitest", "run"), ("npx", "tsc", "--noEmit"), ("npx", "eslint"),
    ("pnpm", "test"), ("yarn", "test"), ("cargo", "test"), ("cargo", "check"), ("cargo", "clippy"),
    ("go", "test"), ("go", "vet"), ("dotnet", "test"), ("dotnet", "build"),
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "show"),
]


def _base(arg: str) -> str:
    name = Path(arg.replace("\\", "/")).name.lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def parse_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        argv = [str(a) for a in command]
    else:
        argv = shlex.split(command, posix=sys.platform != "win32")
        if sys.platform == "win32":
            argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a for a in argv]
    if not argv or not argv[0].strip():
        raise ValueError("Empty command")
    if any("\x00" in a for a in argv):
        raise ValueError("Invalid characters in command")
    return argv


def classify(argv: list[str]) -> Classification:
    base = _base(argv[0])
    lowered = [a.lower() for a in argv]
    norm = (base, *lowered[1:])
    reasons: list[str] = []

    if base in ELEVATION or "runas" in lowered or "-verb" in lowered:
        return Classification("blocked", ["Privilege elevation is never performed by NEXUS"])
    if base in SHELLS:
        return Classification("blocked", ["Shells are not run directly; request the specific program instead"])
    inline = INTERPRETER_INLINE.get(base)
    if inline and any(a in inline for a in lowered[1:]):
        return Classification("blocked", ["Inline interpreter code (-c/-e) is not allowed"])
    if any(ch in a for a in argv for ch in ("|", "&&", "||", ";", ">", "<", "`")) and base not in ("git",):
        reasons.append("Contains shell operators; they will be passed literally because no shell is used")

    if base in DESTRUCTIVE:
        return Classification("destructive", ["Deletes, moves or stops things", *reasons])
    if base == "git" and len(lowered) > 1:
        sub = lowered[1]
        if sub in ("reset", "clean", "checkout", "restore", "rebase", "stash", "branch") and any(
            f in lowered for f in ("--hard", "-f", "--force", "-d", "-D", "-fd", "-fdx", ".")
        ):
            return Classification("destructive", ["Can discard local work", *reasons])
        if sub in ("push", "pull", "fetch", "clone", "remote", "submodule"):
            return Classification("network", ["Contacts a remote repository", *reasons])
    if base in NETWORK:
        return Classification("network", ["Makes network connections", *reasons])
    if (base in ("pip", "pip3", "uv", "poetry", "conda") and "install" in lowered) or (
        base in ("npm", "pnpm", "yarn", "bun") and len(lowered) > 1 and lowered[1] in ("install", "i", "add", "ci")
    ) or (len(lowered) > 2 and base in ("python", "py", "python3") and lowered[1:3] == ["-m", "pip"]):
        return Classification("package_install", ["Installs packages (downloads code from the internet)", *reasons])
    if base == "npx" and len(lowered) > 1 and lowered[1] not in ("vitest", "tsc", "eslint", "prettier", "jest"):
        return Classification("package_install", ["npx may download and run a package", *reasons])
    if any(norm[: len(p)] == p for p in VERIFICATION_PREFIXES):
        return Classification("verification", ["Recognised test/lint/type-check/build command", *reasons])
    return Classification("unfamiliar", ["Not a recognised verification command", *reasons])


@dataclass
class RunResult:
    status: str  # succeeded | failed | timed_out | cancelled | error
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
        parent.kill()
    except Exception:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False,
                           creationflags=NO_WINDOW)
        else:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# cmd.exe re-parses arguments of .bat/.cmd files; these characters could inject extra commands.
_BATCH_UNSAFE = set('&|<>^%"\n\r!')


def project_bin_dirs(root: Path) -> list[Path]:
    """Virtual-environment / node bin folders inside the project, so `pytest`, `python`, `eslint`
    resolve to the project's own tools (as they would in an activated terminal)."""
    candidates = [root / ".venv" / "Scripts", root / "venv" / "Scripts", root / ".venv" / "bin", root / "venv" / "bin",
                  root / "node_modules" / ".bin"]
    return [c for c in candidates if c.is_dir()]


def build_env(extra_path: list[Path] | None = None) -> dict[str, str]:
    env = scrub_env(dict(os.environ))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("VIRTUAL_ENV", None)
    if extra_path:
        env["PATH"] = os.pathsep.join([*(str(p) for p in extra_path), env.get("PATH", "")])
    return env


def resolve_program(name: str, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))


async def run_command(
    argv: list[str],
    cwd: Path,
    timeout_s: int,
    output_limit: int,
    cancel: asyncio.Event | None = None,
    extra_path: list[Path] | None = None,
) -> RunResult:
    """Run argv without a shell. Uses subprocess.Popen + reader threads so it works on every asyncio
    loop type (Windows' Selector loop, used by `uvicorn --reload`, cannot spawn asyncio subprocesses)."""
    env = build_env(extra_path)
    exe = resolve_program(argv[0], env)
    if exe is None:
        return RunResult("error", None, "", f"Program not found on PATH: {argv[0]}", False)
    if exe.lower().endswith((".bat", ".cmd")) and any(ch in _BATCH_UNSAFE for a in argv[1:] for ch in a):
        return RunResult("error", None, "",
                         "Refused: arguments to a .bat/.cmd program may not contain & | < > ^ % \" ! or newlines", False)
    flags = (NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    try:
        proc = subprocess.Popen(  # noqa: ASYNC220 - Popen returns immediately; loop-agnostic by design
            [exe, *argv[1:]], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env, creationflags=flags,
        )
    except OSError as e:
        return RunResult("error", None, "", f"Failed to start {argv[0]}: {e}", False)
    out, err = bytearray(), bytearray()
    state = {"truncated": False}

    def pump(stream: Any, buf: bytearray) -> None:
        for chunk in iter(lambda: stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536), b""):
            room = output_limit - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                state["truncated"] = True
        stream.close()

    readers = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for t in readers:
        t.start()
    status = "succeeded"
    deadline = time.monotonic() + timeout_s
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                break
            if time.monotonic() >= deadline:
                status = "timed_out"
                break
            await asyncio.sleep(0.05)
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
            await asyncio.to_thread(proc.wait, 10)
    def join_readers() -> None:
        for t in readers:
            t.join(5)

    await asyncio.to_thread(join_readers)
    code = proc.returncode
    if status == "succeeded" and code != 0:
        status = "failed"
    stdout = redact(out.decode("utf-8", errors="replace"))
    stderr = redact(err.decode("utf-8", errors="replace"))
    if state["truncated"]:
        stderr += f"\n[output truncated at {output_limit} bytes per stream]"
    if status == "timed_out":
        stderr += f"\n[killed after {timeout_s}s timeout]"
    return RunResult(status, code, stdout, stderr, state["truncated"])
