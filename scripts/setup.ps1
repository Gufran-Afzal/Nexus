<#
.SYNOPSIS
  One-time NEXUS setup for Windows. Runs as your normal user; never requests administrator rights.

.DESCRIPTION
  - Checks prerequisites (Python 3.11+, Node.js 18+, Git optional, Ollama optional)
  - Creates a virtual environment in .venv and installs the backend packages
  - Builds the web UI into frontend\dist
  - Optionally installs voice packages and a Start Menu shortcut

  Network use: this script downloads Python and npm packages. It does NOT download any AI model.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Voice -Shortcut
#>
param(
  [switch]$Voice,       # install faster-whisper + sounddevice (speech recognition)
  [switch]$WakeWord,    # install openWakeWord (optional second wake-word engine)
  [switch]$Shortcut,    # add "NEXUS" to your Start Menu
  [switch]$Dev,         # install test/lint tools
  [string]$Python = ""  # force a Python version for the new .venv, e.g. -Python 3.12
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Check($what) { if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE). See the output above." } }

Step "Checking prerequisites"
# Pick a Python that the native voice packages (onnxruntime, CTranslate2, PyAV) publish wheels for: 3.12 first,
# then 3.11, then 3.13. The py launcher's plain "-3" would pick the NEWEST installed Python (e.g. a 3.15 preview), so
# it is never used. Nothing outside the project is changed.
function Find-Python {
  $wanted = if ($Python) { @($Python) } else { @("3.12", "3.11", "3.13") }
  $pyl = Get-Command py -ErrorAction SilentlyContinue
  foreach ($v in $wanted) {
    if ($pyl) {
      & py "-$v" -c "import sys" 2>$null
      if ($LASTEXITCODE -eq 0) { return @{ Exe = "py"; Args = @("-$v"); Version = $v } }
    }
  }
  $p = Get-Command python -ErrorAction SilentlyContinue
  if ($p) {
    $pv = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ($wanted -contains $pv) { return @{ Exe = "python"; Args = @(); Version = $pv } }
  }
  return $null
}
$found = Find-Python
if (-not $found) {
  Fail "No suitable Python found. NEXUS needs Python 3.12 (or 3.11 / 3.13). Install 3.12 from python.org; a newer Python alone is not enough because voice packages have no wheels for it yet. Installed: $((& py -0p 2>&1) -join ' ')"
}
$PyExe = $found.Exe; $PyArgs = $found.Args; $ver = $found.Version
Write-Host "Using Python $ver ($PyExe $($PyArgs -join ' '))"

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { Fail "Node.js not found. Install the LTS version from nodejs.org." }
$nodeVer = (node --version).TrimStart("v")
if ([version]$nodeVer -lt [version]"18.0") { Fail "Node.js $nodeVer found; NEXUS needs 18 or newer." }
Write-Host "Node.js $nodeVer OK"

if (Get-Command git -ErrorAction SilentlyContinue) { Write-Host "Git OK" } else { Write-Host "Git not found (optional: needed only for Git status in projects)" -ForegroundColor Yellow }
if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Host "Ollama OK" } else { Write-Host "Ollama not found (install from ollama.com; NEXUS works without it, minus model answers)" -ForegroundColor Yellow }

Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $PyExe @PyArgs -m venv .venv; Check "Creating the virtual environment" }
$VPy = Join-Path $Root ".venv\Scripts\python.exe"
$venvVer = & $VPy -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "Virtual environment: $VPy (Python $venvVer)"
if ([version]$venvVer -gt [version]"3.13" -or [version]$venvVer -lt [version]"3.11") {
  Fail "The existing .venv uses Python $venvVer, which NEXUS does not support. Your data is untouched. Rename the folder (Rename-Item .venv .venv-old) and run this script again; the new .venv will use Python $ver."
}
& $VPy -m pip install --upgrade pip --quiet; Check "Upgrading pip"

Step "Installing backend packages"
& $VPy -m pip install -r backend\requirements.txt -r backend\requirements-desktop.txt --quiet; Check "Installing backend packages"
if ($Voice) { Step "Installing voice packages"; & $VPy -m pip install -r backend\requirements-voice.txt --quiet; Check "Installing voice packages" }
if ($WakeWord) { Step "Installing openWakeWord"; & $VPy -m pip install -r backend\requirements-wakeword.txt --quiet; Check "Installing openWakeWord" }
if ($Voice -or $WakeWord) {
  Step "Checking that the native voice packages load (each in its own process)"
  & $VPy backend\native_check.py onnxruntime ctranslate2 "voice stack" openwakeword
  if ($LASTEXITCODE -ne 0) {
    Write-Host "A voice package crashed or failed to load. NEXUS itself is fine and typing works; voice will report the problem instead of crashing." -ForegroundColor Yellow
    Write-Host "Run scripts\diagnose.ps1 for details. A common fix is installing the latest Microsoft Visual C++ Redistributable (x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Yellow
  }
}
if ($Dev) { Step "Installing development tools"; & $VPy -m pip install -r backend\requirements-dev.txt --quiet; Check "Installing dev tools" }

Step "Building the user interface"
Push-Location frontend
if (Test-Path package-lock.json) { npm ci --no-audit --no-fund } else { npm install --no-audit --no-fund }
Check "npm install"
npm run build
Check "Building the UI"
Pop-Location

if ($Shortcut) {
  Step "Adding Start Menu shortcut"
  $startMenu = [Environment]::GetFolderPath("Programs")
  $lnk = Join-Path $startMenu "NEXUS.lnk"
  $shell = New-Object -ComObject WScript.Shell
  $s = $shell.CreateShortcut($lnk)
  $s.TargetPath = Join-Path $Root ".venv\Scripts\pythonw.exe"
  $s.Arguments = "`"$(Join-Path $Root 'backend\run_nexus.pyw')`""
  $s.WorkingDirectory = Join-Path $Root "backend"
  $s.Description = "NEXUS - Your Local AI Engineering Partner"
  $s.Save()
  Write-Host "Created $lnk"
}

Step "Done"
Write-Host "Start NEXUS:            scripts\start.ps1   (or the Start Menu shortcut)"
Write-Host "Get the model (4.7 GB): ollama pull qwen2.5-coder:7b"
Write-Host "Start with Windows:     NEXUS > Settings > 'Start NEXUS when I sign in'"
