<#
.SYNOPSIS  Start NEXUS (tray icon + local server on http://127.0.0.1:8765). Use -Console to see logs in this window.
#>
param([switch]$Console, [switch]$Background)
$Root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $Root "backend\run_nexus.pyw"
if ($Console) {
  Write-Host "Console mode: logs appear here and there is NO tray icon. Run scripts\start.ps1 without -Console for the tray." -ForegroundColor Yellow
  & (Join-Path $Root ".venv\Scripts\python.exe") $script --no-tray
} else {
  $argList = @("`"$script`"")
  if ($Background) { $argList += "--background" }
  Start-Process -FilePath (Join-Path $Root ".venv\Scripts\pythonw.exe") -ArgumentList $argList -WorkingDirectory (Join-Path $Root "backend")
  Write-Host "NEXUS is starting. Look for the tray icon; the UI opens at http://127.0.0.1:8765"
}
