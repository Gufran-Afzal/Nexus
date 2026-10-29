<#
.SYNOPSIS
  Read-only health and crash diagnosis for NEXUS on Windows. Never changes settings, never deletes anything,
  never needs administrator rights, never uses the network. Safe to run while NEXUS is running.

.DESCRIPTION
  1. Which Python the project's .venv really uses, and which other Pythons exist.
  2. Microsoft Visual C++ runtime versions (system and bundled inside packages).
  3. Versions of the native voice packages.
  4. Each native package tested in its OWN process (backend\native_check.py), so a crash is pinned on one package.
  5. The newest crash and error lines from NEXUS's logs, and recent Windows "Application Error" events.
  The full report is also saved to %LOCALAPPDATA%\NEXUS\logs\diagnose-<time>.txt so you can send it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
  powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1 -Only onnxruntime
#>
param([string]$Only = "")
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$VPy = Join-Path $Root ".venv\Scripts\python.exe"
$DataDir = if ($env:NEXUS_DATA_DIR) { $env:NEXUS_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "NEXUS" }
$LogDir = Join-Path $DataDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Report = Join-Path $LogDir ("diagnose-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
Start-Transcript -Path $Report | Out-Null

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function FileVer($f) { try { $v = (Get-Item $f).VersionInfo.FileVersion; if ($v) { $v } else { "?" } } catch { "?" } }

Section "1. Python used by this project"
if (-not (Test-Path $VPy)) {
  Write-Host "No .venv found at $VPy. Run scripts\setup.ps1 first." -ForegroundColor Red
  Stop-Transcript | Out-Null; exit 1
}
& $VPy -c "import sys, struct, platform; print('venv python :', sys.executable); print('version     :', sys.version.split()[0], str(struct.calcsize('P')*8)+'-bit'); print('base python :', sys.base_prefix); print('platform    :', platform.platform())"
Write-Host "Other Pythons on this computer (the py launcher):"
try { & py -0p 2>&1 | ForEach-Object { "  $_" } } catch { Write-Host "  (py launcher not installed)" }
$onPath = Get-Command python -ErrorAction SilentlyContinue
if ($onPath) { Write-Host "'python' on PATH: $($onPath.Source)  (NEXUS does not use this one; it always runs .venv\Scripts\python.exe)" }
$vver = (& $VPy -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ([version]$vver -gt [version]"3.13") { Write-Host "WARNING: .venv uses Python $vver. Several native voice packages publish no wheels for it yet. Use 3.12." -ForegroundColor Yellow }

Section "2. Microsoft Visual C++ runtime"
$reg = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"
if (Test-Path $reg) {
  $r = Get-ItemProperty $reg
  Write-Host "Installed redistributable (x64): $($r.Version)  (build $($r.Bld))"
} else { Write-Host "No Visual C++ 2015-2022 x64 redistributable entry found in the registry." -ForegroundColor Yellow }
foreach ($n in $(if ($env:WINDIR) { "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll", "concrt140.dll" })) {
  $f = Join-Path $env:WINDIR "System32\$n"
  if (Test-Path $f) { Write-Host ("  System32\{0,-20} {1}" -f $n, (FileVer $f)) }
}
Write-Host "Runtime DLLs bundled inside installed packages (these can shadow the system ones):"
Get-ChildItem -Path (Join-Path $Root ".venv") -Recurse -Include "msvcp140*.dll", "vcomp140*.dll", "libiomp5md.dll", "libomp*.dll" -ErrorAction SilentlyContinue |
  ForEach-Object { Write-Host ("  {0}  [{1}]" -f $_.FullName.Substring($Root.Length + 1), (FileVer $_.FullName)) }
Write-Host "Reference: the C++ standard library in Visual Studio 2022 17.10+ needs msvcp140.dll 14.40 or newer."
Write-Host "           Packages built with a newer toolset can crash (0xC0000005) when only an older msvcp140.dll is present."

Section "3. Native package tests, each in its own process"
$checkArgs = @((Join-Path $Root "backend\native_check.py"))
if ($Only) { $checkArgs += $Only }
& $VPy @checkArgs

Section "4. NEXUS logs"
foreach ($n in "crash.log", "speech-worker-crash.log") {
  $f = Join-Path $LogDir $n
  if (Test-Path $f) { Write-Host "--- $n (last 25 lines) ---"; Get-Content $f -Tail 25 } else { Write-Host "--- ${n}: none (no native crash recorded yet) ---" }
}
$f = Join-Path $LogDir "nexus.log"
if (Test-Path $f) {
  Write-Host "--- nexus.log: errors, restarts and voice lines (last 25) ---"
  Select-String -Path $f -Pattern "ERROR|WARNING|exited unexpectedly|speech worker|voice" | Select-Object -Last 25 | ForEach-Object { $_.Line }
}

Section "5. Windows Application Error events for python (last 5)"
try {
  Get-WinEvent -FilterHashtable @{ LogName = "Application"; ProviderName = "Application Error"; Id = 1000 } -MaxEvents 40 -ErrorAction Stop |
    Where-Object { $_.Message -match "python" } | Select-Object -First 5 |
    ForEach-Object { "$($_.TimeCreated)`n$($_.Message)`n" }
} catch { Write-Host "(none found, or the event log could not be read)" }

Write-Host "`nReport saved to: $Report" -ForegroundColor Green
Stop-Transcript | Out-Null
