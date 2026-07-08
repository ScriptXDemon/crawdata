# Start every Mallory service locally, each in its own window.
#   L2 API + dashboards : http://127.0.0.1:8000   (/dashboard, /docs)
#   L3 client (React)   : http://127.0.0.1:5173
#   Crawler ingest dash : http://127.0.0.1:9090
#   Crawler API dash    : http://127.0.0.1:8099
#
# Run:  powershell -ExecutionPolicy Bypass -File start_all.ps1
# Needs: ollama serve running (for live LLM prose). Without it, prose falls back to the stub.

$ErrorActionPreference = "Stop"

# Ollama concurrency: the extraction stage fans out 7b calls over a thread pool, so Ollama
# must serve parallel requests (default is 1 → serialized). 3 loaded models lets 7b+14b+embed
# stay resident (~17GB on 24GB); the vision model swaps in on /ops/analyze-assets.
[System.Environment]::SetEnvironmentVariable("OLLAMA_NUM_PARALLEL", "4", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "3", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "10m", "User")
Write-Host "Set Ollama concurrency env (restart 'ollama serve' to apply): NUM_PARALLEL=4 MAX_LOADED=3" -ForegroundColor DarkGray

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

# Data services: Postgres (host 5433 -> container 5432) + MinIO (9000 API / 9001 console).
# Postgres uses 5433 because a native postgresql-x64 service owns host 5432.
Write-Host "  bringing up Postgres + MinIO (docker)..." -ForegroundColor DarkGray
Push-Location $l2
docker compose up -d postgres minio | Out-Null
# wait for Postgres health
for ($i=0; $i -lt 20; $i++) {
  docker exec mallory-layer2-postgres-1 pg_isready -U mallory 2>$null | Out-Null
  if ($?) { break }; Start-Sleep 2
}
Pop-Location

# L2 API — config comes from .env (Postgres 5433 + MinIO 9000 + Ollama farm). Scheduler on.
Start-Svc "Mallory L2 :8000" $l2 (
  "`$env:SCHEDULER_ENABLED='1'; `$env:SCHEDULER_INTERVAL_S='120'; " +
  "& '$l2py' -m uvicorn mallory_engine.main:app --host 127.0.0.1 --port 8000"
)

# L3 React client (proxies /api to :8000)
Start-Svc "Mallory L3 client :5173" $l3 "npm run dev"

# Crawler MinIO env so blobs (images/PDFs/screenshots) land in MinIO, not local disk.
$minioEnv = "`$env:MINIO_ENDPOINT='localhost:9000'; `$env:MINIO_ACCESS_KEY='mallory'; `$env:MINIO_SECRET_KEY='mallory123'; `$env:MINIO_BUCKET='mallory-raw'; "

# Crawler ingest dashboard (browsable audit of ingested pages)
Start-Svc "Crawler ingest :9090" $crw ($minioEnv + "& '$crpy' run.py serve")

# Crawler API dashboard (job runner) — writes blobs to MinIO
Start-Svc "Crawler API :8099" $crw ($minioEnv + "& '$crpy' run.py crawler-api")

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
