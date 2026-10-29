<#
.SYNOPSIS  Run every automated check: ruff, mypy, pytest, TypeScript, Vitest, production build.
           Requires: scripts\setup.ps1 -Dev
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Root ".venv\Scripts\python.exe"
$failed = @()
function Run($name, [scriptblock]$cmd) {
  Write-Host "`n==> $name" -ForegroundColor Cyan
  & $cmd
  if ($LASTEXITCODE -ne 0) { $script:failed += $name }
}
Push-Location (Join-Path $Root "backend")
Run "ruff" { & $py -m ruff check . }
Run "mypy" { & $py -m mypy app }
Run "pytest" { & $py -m pytest -q }
Pop-Location
Push-Location (Join-Path $Root "frontend")
Run "eslint" { npx eslint src }
Run "typescript" { npx tsc -p tsconfig.json --noEmit }
Run "vitest" { npx vitest run }
Run "build" { npm run build }
Pop-Location
if ($failed.Count) { Write-Host "`nFAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "`nAll checks passed." -ForegroundColor Green
