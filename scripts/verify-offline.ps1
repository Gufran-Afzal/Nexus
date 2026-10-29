<#
.SYNOPSIS  Show every network connection held by NEXUS and Ollama. In normal use all of them should be
           to 127.0.0.1 / ::1 (loopback). Run while you chat with NEXUS, ideally with Wi-Fi turned off.
#>
$nexus = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*run_nexus.pyw*" -or $_.CommandLine -like "*app.main*" }
$ollama = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
$pids = @($nexus.ProcessId) + @($ollama.Id) | Where-Object { $_ }
if (-not $pids) { Write-Host "Neither NEXUS nor Ollama is running."; exit 1 }
$conns = Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object { $pids -contains $_.OwningProcess -and $_.State -ne "Listen" }
$external = $conns | Where-Object { $_.RemoteAddress -notin @("127.0.0.1", "::1", "0.0.0.0", "::") }
$conns | Select-Object OwningProcess, LocalAddress, LocalPort, RemoteAddress, RemotePort, State | Format-Table -AutoSize
if ($external) {
  Write-Host "Non-loopback connections found (see above). Expected only while you explicitly download a model." -ForegroundColor Yellow
  exit 2
}
Write-Host "OK: all NEXUS/Ollama connections are local (loopback)." -ForegroundColor Green
