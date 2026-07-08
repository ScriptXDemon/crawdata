# Mallory — Cross-Machine Deployment Guide

## Architecture

```
L1 Machine (Crawler + Ingest)    IP: 192.168.5.50
┌─────────────────────────────────────────────────────────────────────┐
│  Crawler API       http://0.0.0.0:8099                             │
│  (job in → records out, can forward to L1 + L2 simultaneously)     │
│  • GET  /                          → Job Dashboard (HTML)           │
│  • GET  /health                    → liveness check                 │
│  • GET  /v1/generate-jobs          → generate jobs from seed        │
│  • GET  /v1/docs                   → Swagger API docs               │
│  • GET  /v1/schema                 → JSON Schema contract           │
│  • POST /v1/crawl                  → run one crawl job              │
│  • POST /v1/crawl/batch            → run multiple jobs              │
│    Both accept l2_ingest_url to push records to L2                  │
│                                                                     │
│  Ingest API        http://0.0.0.0:9090                             │
│  (record dashboard + asset proxy)                                   │
│  • GET  /                          → Record Dashboard (HTML)        │
│  • GET  /health                    → liveness + record counts       │
│  • GET  /stats                     → accept/reject stats (JSON)     │
│  • GET  /artifact?path=...         → serves images/PDFs             │
│  • POST /ingest/v1/{type}          → accepts {document, record}     │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ HTTP (8099, 9090)
                        ↓
L2 Machine (Orchestrator + Data Engine)  IP: 192.168.5.153
┌─────────────────────────────────────────────────────────────────────┐
│  Orchestrator         http://0.0.0.0:8001                          │
│  • generates jobs from seed × Source Catalog                       │
│  • POSTs to L1 Crawler API (port 8099)                             │
│  • forwards crawl results to L2 Data Engine (port 8000)            │
│  • triggers L2 pipeline after ingestion                            │
│                                                                     │
│  Layer 2 Data Engine  http://0.0.0.0:8000                          │
│  • POST /ingest/v1/{type}   receives records from crawler/orch.    │
│  • GET  /api/v1/asset-proxy proxies assets from L1 /artifact       │
│  • POST /ops/process        processes staging → serving tables     │
│  • GET  /ops/status         staging/serving row counts             │
│  • GET  /api/v1/signals     served signals                         │
│  • GET  /api/v1/tenders     served tenders                         │
│                                                                     │
│  Layer 3 Client        http://localhost:5173                       │
│  • reads from L2 serving API                                       │
│  • images/PDFs via /api/v1/asset-proxy → L1 /artifact              │
└─────────────────────────────────────────────────────────────────────┘
```

## Setup on L1 Machine (Crawler + Ingest)

### 1. Firewall Rules (Run as Administrator)

```powershell
New-NetFirewallRule -DisplayName "Crawler API" -Direction Inbound -Protocol TCP -LocalPort 8099 -Action Allow
New-NetFirewallRule -DisplayName "Ingest API" -Direction Inbound -Protocol TCP -LocalPort 9090 -Action Allow
```

### 2. Find L1 IP Address

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -ne 'WellKnown' } | Select IPAddress, InterfaceAlias
```

Example output → `192.168.5.50` (Wi-Fi)

### 3. No .env needed on L1

Both APIs read defaults directly from `cralwer/crawler/config.py`:
- Crawler API binds to `0.0.0.0:8099`
- Ingest API binds to `0.0.0.0:9090`
- Ingest base URL defaults to `http://127.0.0.1:9090`

### 4. Start Services

```powershell
cd cralwer

# Terminal 1 — Crawler API (job dashboard + run interface)
python run.py crawler-api

# Terminal 2 — Ingest API (record dashboard + artifact proxy)
python run.py serve
```

### 5. Verify from L1

```powershell
curl.exe -s http://localhost:8099/health
# → {"status":"ok","entities":51,"sources":15}

curl.exe -s http://localhost:9090/health
# → {"status":"ok","accepted":0,"rejected":0}
```

Open in browser:
- `http://192.168.5.50:8099` — Crawler Job Dashboard
- `http://192.168.5.50:9090` — Ingest Record Dashboard

---

## Setup on L2 Machine (Orchestrator + Data Engine)

### 1. Orchestrator `.env`

```
DATABASE_URL=sqlite:///./orchestrator.db
CRAWLER_API=http://192.168.5.50:8099
L2_INGEST_API=http://192.168.5.153:8000
SEED_DIR=./seed_data
CORS_ORIGINS=*
```

### 2. Layer 2 Data Engine `.env`

```
DATABASE_URL=postgresql+psycopg://mallory:mallory@localhost:5432/mallory
LLM_PROVIDER=stub
SEED_DIR=./seed_data
CRAWLER_INGEST_URL=http://192.168.5.50:9090
CORS_ORIGINS=http://localhost:5173,http://localhost:4173
```

### 3. Start Data Engine

```bash
cd layer2-data-engine
docker compose up -d
```

### 4. Start Orchestrator

```bash
cd orchestrator
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
python -m mallory_orchestrator.scripts.init_db
python -m mallory_orchestrator.scripts.seed_sources
uvicorn mallory_orchestrator.main:app --host 0.0.0.0 --port 8001
```

### 5. Verify from L2

```bash
curl http://192.168.5.50:8099/health   # Crawler API on L1
curl http://192.168.5.50:9090/health   # Ingest API on L1
curl http://localhost:8000/health      # L2 Data Engine
```

---

## Running Jobs — Two Paths

### Path A — Crawler Dashboard → L1 + L2 (recommended)

Uses the 8099 dashboard directly. Records flow to BOTH L1 (9090) and L2 (8000).

```
1. Open http://192.168.5.50:8099
2. Fill the L2 Ingest URL field: http://192.168.5.153:8000
3. Click "Generate Jobs from Seed" → ~100 jobs appear as cards
4. Click "Run" on any job (or "Run All Jobs")
   → Crawler fetches, gates, dedupes, extracts records
   → Records posted to L1 Ingest API (:9090) — visible on :9090 dashboard
   → Records ALSO posted to L2 Data Engine (:8000) — visible in L2 staging
```

Verify L2 received data:

```bash
curl http://192.168.5.153:8000/ops/status
# Look for non-zero counts under staging.signals and staging.tenders

# Process staging → serving tables
curl -X POST http://192.168.5.153:8000/ops/process

# Query served data
curl http://192.168.5.153:8000/api/v1/signals
curl http://192.168.5.153:8000/api/v1/tenders
```

### Path A — Curl Equivalent

```powershell
curl.exe -s -X POST http://localhost:8099/v1/crawl/batch `
  -H "Content-Type: application/json" `
  -d "{\"jobs\":[{\"job_id\":\"manual_bharat52\",\"job_type\":\"spec\",\"seed_urls\":[\"https://en.wikipedia.org/wiki/Bharat-52\"],\"keywords\":[\"Bharat-52\",\"155mm\"],\"max_pages\":5,\"max_depth\":1,\"capture\":[\"html\",\"text\",\"images\",\"screenshot\"],\"expected_record_types\":[\"competitive_signal\"]}],\"forward_to_ingest\":true,\"l2_ingest_url\":\"http://192.168.5.153:8000\"}"
```

### Path B — Orchestrator → L2 (automated)

```
1. Orchestrator generates jobs from Source Catalog × seed
2. Orchestrator POSTs each job to L1 Crawler API (:8099)
3. Crawler runs job, returns {documents, records}
4. Orchestrator forwards each record to L2 Data Engine (:8000)
5. Orchestrator triggers L2 pipeline → staging → serving tables
6. L3 client reads from L2, proxies images via /api/v1/asset-proxy → L1 /artifact
```

```bash
curl -X POST http://localhost:8001/api/run
```

---

## Key Files Changed

| File | Change | Purpose |
|------|--------|---------|
| `cralwer/crawler_api/dashboard.py` | **Created** | Inline HTML dashboard for job gen + crawl execution |
| `cralwer/crawler_api/app.py` | Added `GET /`, `GET /v1/generate-jobs`, `l2_ingest_url` param | Dashboard, job gen, dual forwarding |
| `cralwer/crawler/ingest_client.py` | `CollectingIngestClient` accepts multiple forwarders | Send records to L1 + L2 simultaneously |
| `orchestrator/.env` | **Created** with `CRAWLER_API`, `L2_INGEST_API` | Points orchestrator at L1 → L2 |
| `orchestrator/src/mallory_orchestrator/jobgen.py` | `forward_to_ingest: False → True` | Records land on :9090 dashboard by default |
| `layer2-data-engine/.env` | Added `CRAWLER_INGEST_URL` | Asset proxy fetches from L1 |
| `layer2-data-engine/docker-compose.yml` | Added `CRAWLER_INGEST_URL` env var | Passes through to API container |
| `layer2-data-engine/.env.example` | Fixed port comment `8077 → 9090` | Documentation accuracy |

---

## Env Vars Summary

| Machine | Variable | Value | Purpose |
|---------|----------|-------|---------|
| L1 | (none required) | — | Both APIs run on L1 |
| L2 | `CRAWLER_API` | `http://192.168.5.50:8099` | Orchestrator → L1 Crawler API |
| L2 | `L2_INGEST_API` | `http://192.168.5.153:8000` | Orchestrator → L2 Data Engine |
| L2 | `CRAWLER_INGEST_URL` | `http://192.168.5.50:9090` | L2 asset-proxy → L1 Ingest API |

---

## Port Map

| Port | Service | Machine | Exposed to |
|------|---------|---------|------------|
| 8099 | Crawler API | L1 | Any browser, Orchestrator |
| 9090 | Ingest API | L1 | Any browser, L2 asset-proxy |
| 8000 | L2 Data Engine | L2 | Orchestrator, L3 client, Crawler (via l2_ingest_url) |
| 8001 | Orchestrator | L2 | Browser (admin UI) |
| 5173 | L3 Client | L2 | Browser |
| 5432 | Postgres | L2 | L2 internally |
| 9000 | MinIO (blob store) | L2 | L2 internally |

---

## L2 Health Check Commands

```bash
# Is L2 alive?
curl http://192.168.5.153:8000/health

# Did records land in staging?
curl http://192.168.5.153:8000/ops/status

# Process staging → serving
curl -X POST http://192.168.5.153:8000/ops/process

# Read served data
curl http://192.168.5.153:8000/api/v1/signals
curl http://192.168.5.153:8000/api/v1/tenders
```

---

## Clear Crawl History (Fresh Re-Run)

```powershell
Remove-Item cralwer\data\crawl_history.sqlite -ErrorAction SilentlyContinue
```
