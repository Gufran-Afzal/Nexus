<#
.SYNOPSIS  Stop NEXUS completely (same as tray > "Exit NEXUS completely").
           Asks NEXUS to shut down gracefully first (stops commands it started, saves state);
           only if it doesn't exit within 15 seconds are NEXUS's own processes force-stopped.
#>
param([int]$Port = 8765)
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*run_nexus.pyw*" }
if (-not $procs) { Write-Host "NEXUS is not running."; exit 0 }
try {
  Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/system/shutdown" -Headers @{ "X-Nexus-Client" = "stop-script" } -TimeoutSec 5 | Out-Null
  Write-Host "Asked NEXUS to shut down..."
} catch {
  Write-Host "NEXUS did not answer the shutdown request ($($_.Exception.Message))."
}
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
  $alive = $procs | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
  if (-not $alive) { Write-Host "NEXUS stopped."; exit 0 }
  Start-Sleep -Milliseconds 500
}
foreach ($p in $procs) {
  if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) {
    Write-Host "Force-stopping NEXUS (PID $($p.ProcessId))"
    Stop-Process -Id $p.ProcessId -Force
  }
}
