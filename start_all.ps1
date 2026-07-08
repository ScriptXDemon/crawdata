# Start every Mallory service locally, each in its own window.
#   L2 API + dashboards : http://127.0.0.1:8000   (/dashboard, /docs)
#   L3 client (React)   : http://127.0.0.1:5173
#   Crawler ingest dash : http://127.0.0.1:9090
#   Crawler API dash    : http://127.0.0.1:8099
#
# Run:  powershell -ExecutionPolicy Bypass -File start_all.ps1
# Needs: ollama serve running (for live LLM prose). Without it, prose falls back to the stub.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$l2   = Join-Path $root "layer2-data-engine"
$l3   = Join-Path $root "layer3-client"
$crw  = Join-Path $root "cralwer"
$l2py = Join-Path $l2 ".venv-win\Scripts\python.exe"
$crpy = Join-Path $crw ".venv\Scripts\python.exe"

function Start-Svc($title, $wd, $cmd) {
  Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle='$title'; Set-Location '$wd'; $cmd"
  ) | Out-Null
  Write-Host "  started: $title"
}

Write-Host "Starting Mallory services..." -ForegroundColor Cyan

# L2 API (SQLite demo DB + scheduler + local Ollama)
Start-Svc "Mallory L2 :8000" $l2 (
  "`$env:DATABASE_URL='sqlite:///./mallory_demo.db'; " +
  "`$env:LLM_PROVIDER='ollama'; `$env:SCHEDULER_ENABLED='1'; `$env:SCHEDULER_INTERVAL_S='60'; " +
  "& '$l2py' -m uvicorn mallory_engine.main:app --host 127.0.0.1 --port 8000"
)

# L3 React client (proxies /api to :8000)
Start-Svc "Mallory L3 client :5173" $l3 "npm run dev"

# Crawler ingest dashboard (browsable audit of ingested pages)
Start-Svc "Crawler ingest :9090" $crw "& '$crpy' run.py serve"

# Crawler API dashboard (job runner)
Start-Svc "Crawler API :8099" $crw "& '$crpy' run.py crawler-api"

Start-Sleep -Seconds 4
Write-Host ""
Write-Host "All services launching. Open:" -ForegroundColor Green
Write-Host "  L2 dashboard   http://127.0.0.1:8000/dashboard"
Write-Host "  L2 API docs    http://127.0.0.1:8000/docs"
Write-Host "  L3 client app  http://127.0.0.1:5173"
Write-Host "  Crawler audit  http://127.0.0.1:9090"
Write-Host "  Crawler API    http://127.0.0.1:8099"
Write-Host ""
Write-Host "Feed live data:  cd cralwer; `$env:INGEST_BASE_URL='http://127.0.0.1:8000'; " -NoNewline
Write-Host "`$env:CRAWLER_ALLOW_NETWORK='1'; .\.venv\Scripts\python.exe run.py push jobs\live_push_small.json"
