"""Finds the applications installed on this PC so the Apps panel can list them (you never type an .exe name).

Sources, all read-only: Start Menu shortcuts (classic programs), Microsoft Store apps (Get-StartApps), and the
registry "App Paths" list. They are read in ONE PowerShell child process with a timeout, so a slow or broken scan can
never hang or crash the NEXUS server. Non-Windows systems only get the built-in essentials.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

log = logging.getLogger(__name__)

SCAN_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$out = New-Object System.Collections.ArrayList
$sh = New-Object -ComObject WScript.Shell
$dirs = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
foreach ($d in $dirs) {
  Get-ChildItem -Path $d -Filter *.lnk -Recurse -File | ForEach-Object {
    $l = $sh.CreateShortcut($_.FullName)
    [void]$out.Add([pscustomobject]@{k='lnk'; name=$_.BaseName; target=[string]$l.TargetPath; args=[string]$l.Arguments; path=$_.FullName})
  }
}
Get-StartApps | ForEach-Object { [void]$out.Add([pscustomobject]@{k='start'; name=$_.Name; target=[string]$_.AppID; args=''; path=''}) }
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\*' |
  ForEach-Object { [void]$out.Add([pscustomobject]@{k='reg'; name=[IO.Path]::GetFileNameWithoutExtension($_.PSChildName); target=[string]$_.'(default)'; args=''; path=''}) }
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""

# Things in the Start Menu that are not apps you would ask for by name.
_SKIP = re.compile(r"\b(uninstall|uninstaller|readme|read me|release notes|documentation|user guide|manual|help|"
                   r"license|website|web site|support|updater|update|repair|setup|install)\b", re.I)

# Always offered, even if the scan fails. (name, kind, target, process)
ESSENTIALS: list[tuple[str, str, str, str]] = [
    ("Notepad", "exe", "notepad.exe", "notepad.exe"),
    ("Calculator", "exe", "calc.exe", "CalculatorApp.exe"),
    ("Paint", "exe", "mspaint.exe", "mspaint.exe"),
    ("File Explorer", "exe", "explorer.exe", ""),
    ("Task Manager", "exe", "taskmgr.exe", "Taskmgr.exe"),
    ("Command Prompt", "exe", "cmd.exe", "cmd.exe"),
    ("PowerShell", "exe", "powershell.exe", "powershell.exe"),
    ("Snipping Tool", "uri", "ms-screenclip:", ""),
    ("Windows Settings", "uri", "ms-settings:", ""),
    ("WordPad", "exe", "write.exe", "wordpad.exe"),
]


@dataclass
class DiscoveredApp:
    id: str
    name: str
    kind: str  # exe | uwp | shortcut | uri
    target: str
    args: list[str] = field(default_factory=list)
    process: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def app_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _split_args(args: str) -> list[str]:
    import shlex

    try:
        return shlex.split(args, posix=False) if args.strip() else []
    except ValueError:
        return []


def parse_scan(rows: list[dict[str, Any]]) -> list[DiscoveredApp]:
    """Turn the PowerShell rows into a clean, de-duplicated app list (pure function: unit-tested with sample data)."""
    apps: dict[str, DiscoveredApp] = {}
    exe_seen: set[str] = set()

    def put(app: DiscoveredApp, replace: bool = False) -> None:
        key = app.id
        if not key:
            return
        if key not in apps or replace:
            apps[key] = app
        if app.kind == "exe":
            exe_seen.add(PureWindowsPath(app.target).name.lower())

    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name or _SKIP.search(name):
            continue
        k, target = r.get("k"), str(r.get("target") or "").strip()
        if k == "lnk":
            low = target.lower()
            if low.endswith(".exe"):
                put(DiscoveredApp(app_id(name), name, "exe", target, _split_args(str(r.get("args") or "")),
                                  PureWindowsPath(target).name, "start-menu"), replace=True)
            elif r.get("path") and target and "://" not in low and not low.endswith((".url", ".htm", ".html")):
                put(DiscoveredApp(app_id(name), name, "shortcut", str(r["path"]), [], "", "start-menu"))
        elif k == "start":
            if "!" in target:  # Microsoft Store (UWP) app: AppUserModelID like Package_xyz!App
                put(DiscoveredApp(app_id(name), name, "uwp", target, [], "", "store"))
        elif k == "reg":
            if target.lower().endswith(".exe") and PureWindowsPath(target).name.lower() not in exe_seen:
                put(DiscoveredApp(app_id(name), name.title() if name.islower() else name, "exe", target.strip('"'), [],
                                  PureWindowsPath(target).name, "registry"))
    for name, kind, target, process in ESSENTIALS:
        put(DiscoveredApp(app_id(name), name, kind, target, [], process, "essentials"))
    return sorted(apps.values(), key=lambda a: a.name.lower())


def _run_powershell(script: str, timeout: float = 60.0) -> str:
    exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not exe:
        raise RuntimeError("PowerShell was not found")
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=timeout,
                       check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[:300] or f"exit {r.returncode}")
    return r.stdout.decode("utf-8-sig", "replace")


class AppCatalog:
    """Caches the discovered apps (memory + a JSON file) so the panel opens instantly; scanning is explicit."""

    def __init__(self, cache_file: Path, runner: Callable[[str], str] = _run_powershell,
                 platform: str | None = None) -> None:
        self.cache_file, self.runner, self.platform = cache_file, runner, platform or sys.platform
        self.apps: list[DiscoveredApp] = []
        self.scanned_at: float | None = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            self.apps = [DiscoveredApp(**a) for a in data["apps"]]
            self.scanned_at = data.get("scanned_at")
        except Exception:  # noqa: S110 - a missing or corrupt cache just means "scan again"
            self.apps = parse_scan([])

    def scan(self) -> list[DiscoveredApp]:
        self.error = None
        rows: list[dict[str, Any]] = []
        if self.platform == "win32":
            try:
                out = self.runner(SCAN_SCRIPT).strip()
                parsed = json.loads(out) if out else []
                rows = [parsed] if isinstance(parsed, dict) else list(parsed)
            except Exception as e:
                self.error = f"Scanning for apps failed: {e}"
                log.warning(self.error)
        else:
            self.error = "Scanning for installed apps only works on Windows; showing the built-in Windows tools."
        self.apps = parse_scan(rows)
        self.scanned_at = time.time()
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps({"apps": [a.as_dict() for a in self.apps], "scanned_at": self.scanned_at}),
                                       encoding="utf-8")
        except OSError as e:
            log.warning("could not save the app list: %s", e)
        return self.apps

    def by_id(self, app_id_: str) -> DiscoveredApp | None:
        return next((a for a in self.apps if a.id == app_id_), None)

    def guess(self, name: str) -> DiscoveredApp | None:
        """Find a discovered app for a name typed by hand ("chrome" -> Google Chrome); falls back to PATH."""
        from app.services.appcontrol import norm

        q = norm(name)
        if not q:
            return None
        for a in self.apps:
            if norm(a.name) == q:
                return a
        words = set(q.split())
        hits = [a for a in self.apps if words <= set(norm(a.name).split())]
        if hits:
            return min(hits, key=lambda a: len(a.name))
        exe = shutil.which(name) or shutil.which(name + ".exe")
        if exe:
            return DiscoveredApp(app_id(name), name.title(), "exe", exe, [], PureWindowsPath(exe).name, "manual")
        return None
