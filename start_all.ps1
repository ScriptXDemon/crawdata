# ═══════════════════════════════════════════════════════════════════════════
#  Mallory — start the whole stack (data services + all 4 apps), wired.
# ═══════════════════════════════════════════════════════════════════════════
#   Flow:  8099 crawler-api  →  9090 ingest dashboard  +  8000 L2 intelligence  →  5173 client
#   Data:  Postgres (docker :5433)  ·  MinIO (docker :9000/:9001)  ·  Ollama farm (remote)
#
#   Run:        powershell -ExecutionPolicy Bypass -File start_all.ps1
#   Run+flush:  powershell -ExecutionPolicy Bypass -File start_all.ps1 -Flush
#
#   URLs:
#     http://127.0.0.1:8099   Crawler API — paste jobs here (Manual Batch)
#     http://127.0.0.1:9090   Ingest dashboard — raw harvested pages
#     http://127.0.0.1:8000   L2 intelligence dashboard (/dashboard, /docs)
#     http://127.0.0.1:5173   React client
#
#   Farm: config comes from layer2-data-engine/.env (OLLAMA_BASE_URL + OLLAMA_API_KEY).
#   Postgres is on host :5433 (a native postgresql-x64 service owns :5432).

param([switch]$Flush)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$l2   = Join-Path $root "layer2-data-engine"
$l3   = Join-Path $root "layer3-client"
$crw  = Join-Path $root "cralwer"
$l2py = Join-Path $l2 ".venv-win\Scripts\python.exe"
$crpy = Join-Path $crw ".venv\Scripts\python.exe"
$pgUrl = "postgresql+psycopg://mallory:mallory@localhost:5433/mallory"

function Stop-Port($port) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep 1 }
}

function Start-Hidden($exe, $argList, $wd, $logName) {
  Start-Process $exe -ArgumentList $argList -WorkingDirectory $wd -WindowStyle Hidden `
    -RedirectStandardOutput "$env:TEMP\$logName.log" -RedirectStandardError "$env:TEMP\${logName}_err.log"
}

Write-Host "Starting Mallory stack..." -ForegroundColor Cyan

# ── crawler env (MinIO blobs + live-network crawling) ────────────────────────
$env:MINIO_ENDPOINT   = "localhost:9000"
$env:MINIO_ACCESS_KEY = "mallory"
$env:MINIO_SECRET_KEY = "mallory123"
$env:MINIO_BUCKET     = "mallory-raw"
$env:CRAWLER_ALLOW_NETWORK  = "1"
$env:CRAWLER_PREFER_FIXTURES = "0"

# ── 1. Data services: Postgres + MinIO (docker) ──────────────────────────────
Write-Host "  [1/6] Postgres + MinIO (docker)..." -ForegroundColor DarkGray
Push-Location $l2
docker compose up -d postgres minio | Out-Null
for ($i=0; $i -lt 20; $i++) {
  docker exec mallory-layer2-postgres-1 pg_isready -U mallory 2>$null | Out-Null
  if ($?) { break }; Start-Sleep 2
}
Pop-Location

# ── optional: flush all crawl data (keeps ref_* seed) ────────────────────────
if ($Flush) {
  Write-Host "  [--] Flushing crawl data (L2 + crawler stores + MinIO)..." -ForegroundColor Yellow
  # crawler stores
  Remove-Item "$crw\data\crawl_history.sqlite","$crw\data\output\ingested.ndjson","$crw\data\output\rejected.ndjson" -Force -ErrorAction SilentlyContinue
  Get-ChildItem "$crw\data\storage\img","$crw\data\storage\doc","$crw\data\storage\shot" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  # L2 crawl-derived tables (ref_* seed stays)
  $env:DATABASE_URL = $pgUrl
  & $l2py -c @"
from sqlalchemy.orm import Session; from sqlalchemy import delete; from mallory_engine.db import engine
from mallory_engine.models.staging import StgDocument, StgSignal, StgTender, StgPartnership, StgGeo, StgCompanyEvent, StgAssetAnalysis, StgInnovation
from mallory_engine.models.serving import SrvSignal, SrvSignalDetail, SrvTender, SrvTenderMatch, SrvGeoEntry, SrvPartnership, SrvEvidence
from mallory_engine.models.llm_ops import LlmRun
s=Session(engine)
for M in (StgSignal,StgTender,StgPartnership,StgGeo,StgCompanyEvent,StgAssetAnalysis,StgInnovation,StgDocument,SrvSignal,SrvSignalDetail,SrvTender,SrvTenderMatch,SrvGeoEntry,SrvPartnership,LlmRun): s.query(M).delete()
s.execute(delete(SrvEvidence).where(SrvEvidence.target_kind.in_(['signal','tender','geo','partnership','document_asset'])))
s.commit(); print('    L2 flushed: docs=%d signals=%d' % (s.query(StgDocument).count(), s.query(SrvSignal).count()))
"@
  Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue
  # MinIO bucket
  docker run --rm --network mallory-layer2_default --entrypoint sh minio/mc -c "mc alias set local http://minio:9000 mallory mallory123 >/dev/null 2>&1 && mc rm --recursive --force local/mallory-raw >/dev/null 2>&1" | Out-Null
}

# ── 2. L2 intelligence (8000) — .env drives Postgres+MinIO+farm ──────────────
# Bind 0.0.0.0 so the client's vite proxy reaches it via 127.0.0.1 OR ::1.
# Scheduler OFF: process on demand with  curl -X POST http://127.0.0.1:8000/ops/process
Write-Host "  [2/6] L2 intelligence :8000..." -ForegroundColor DarkGray
Stop-Port 8000
$env:SCHEDULER_ENABLED = "0"
Start-Hidden $l2py @("-m","uvicorn","mallory_engine.main:app","--host","0.0.0.0","--port","8000") $l2 "l2"

# ── 3. Ingest dashboard (9090) ───────────────────────────────────────────────
Write-Host "  [3/6] Ingest dashboard :9090..." -ForegroundColor DarkGray
Stop-Port 9090
Start-Hidden $crpy @("run.py","serve") $crw "ingest9090"

# ── 4. Crawler API (8099) — where you paste jobs ─────────────────────────────
Write-Host "  [4/6] Crawler API :8099..." -ForegroundColor DarkGray
Stop-Port 8099
Start-Hidden $crpy @("run.py","crawler-api") $crw "crawlerapi"

# ── 5. React client (5173) — vite proxies /api + /ops to :8000 ───────────────
Write-Host "  [5/6] Client :5173..." -ForegroundColor DarkGray
Stop-Port 5173
Start-Hidden "cmd.exe" @("/c","npm run dev") $l3 "client5173"

# ── 6. Wait + health check ───────────────────────────────────────────────────
Write-Host "  [6/6] Waiting for services..." -ForegroundColor DarkGray
Start-Sleep 12
Write-Host ""
foreach ($svc in @(@{p=8099;n="Crawler API"}, @{p=9090;n="Ingest"}, @{p=8000;n="L2"}, @{p=5173;n="Client"})) {
  $c = Get-NetTCPConnection -LocalPort $svc.p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  $state = if ($c) { "UP  " } else { "DOWN" }
  $color = if ($c) { "Green" } else { "Red" }
  Write-Host ("  {0} :{1}  {2}" -f $state, $svc.p, $svc.n) -ForegroundColor $color
}

Write-Host ""
Write-Host "Open the pipeline:" -ForegroundColor Green
Write-Host "  Crawler (paste jobs) http://127.0.0.1:8099"
Write-Host "  Ingest dashboard     http://127.0.0.1:9090"
Write-Host "  L2 intelligence      http://127.0.0.1:8000/dashboard"
Write-Host "  Client               http://127.0.0.1:5173"
Write-Host ""
Write-Host "In the 8099 dashboard (Manual Batch):" -ForegroundColor Cyan
Write-Host "  1. Freshness filter OFF   2. L2 Ingest URL = http://127.0.0.1:8000"
Write-Host "  3. Dropdown = 'Push to Ingest API too'   4. Paste jobs -> Run Batch"
Write-Host ""
Write-Host "Then process (scheduler is off):" -ForegroundColor Cyan
Write-Host "  curl -X POST http://127.0.0.1:8000/ops/process"
