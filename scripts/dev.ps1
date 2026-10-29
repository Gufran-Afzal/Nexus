<#
.SYNOPSIS  Development mode: backend with auto-reload on :8765 and the Vite dev server on :5173 (two windows).
#>
$Root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Root ".venv\Scripts\python.exe"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\backend'; & '$py' -m uvicorn --factory app.main:create_app --reload --port 8765"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\frontend'; npm run dev"
Write-Host "Open http://127.0.0.1:5173 when both servers are up."
