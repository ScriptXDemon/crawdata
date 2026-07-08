# Mallory — Layer 2 (Data Engineering)

The backend "engine room". All **"vs KSSL"** compute lives here. Three inputs — crawler records
(`stg_*` via the Ingest API), external APIs (`ext_*`), admin seed (`ref_*`) — and exactly one output
consumer: the Layer 3 client, which reads `srv_*` through the read-only Serving API.

Deploys independently of the client. See `../docs/` for the full architecture and the crawler contract.

## Quickstart (Docker)

```bash
cp .env.example .env
docker compose up        # postgres + minio + api (auto-creates tables, loads seed, serves :8000)
```

Then, from a venv (or `docker compose exec api ...`), feed sample data and process it:

```bash
python -m mallory_engine.scripts.mock_feeder   # POST sample crawler records + run the pipeline
open http://localhost:8000/docs                 # interactive API
```

## Quickstart (local Python)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
# point DATABASE_URL at any Postgres, then:
python -m mallory_engine.scripts.init_db        # create tables
python -m mallory_engine.scripts.load_seed      # load ref_* from seed_data/
uvicorn mallory_engine.main:app --reload        # serve :8000
python -m mallory_engine.scripts.mock_feeder    # ingest sample + process
```

No `ANTHROPIC_API_KEY` is required: `LLM_PROVIDER=stub` (default) runs the whole pipeline with
deterministic, rule-based enrichment. Set `LLM_PROVIDER=anthropic` + a key for real LLM output.

## The two interfaces

| | Direction | Endpoints |
|---|---|---|
| **Ingest API** | L1 → L2 | `POST /ingest/v1/page` (document + records), `POST /ingest/v1/document` |
| **Serving API** | L2 → L3 | `GET /api/v1/signals`, `/signals/{id}/detail`, `/overview/{pillar}/metrics`, `/tenders`, `/tenders/{id}`, `/competitors` |
| **Ops** | internal | `POST /ops/process` (run pipeline), `GET /ops/status` (proc-state counts) |

Every ingest body is validated against the Pydantic contract (`contracts/ingest.py`) — a malformed
crawler record is rejected with **HTTP 422** before it reaches staging.

## How a record flows

```
POST /ingest/v1/page → stg_documents + stg_* (proc_status='received')
   → S-05 resolve entity → S-07 classify (dir/lens/tags, vs KSSL)
   → S-09 enrich (sowhat/details) → S-10 rank → srv_signals + srv_signal_details
tenders: → normalize → score vs all KSSL product specs → fit % + go/maybe/pass → srv_tenders(+matches)
   → S-11 metrics → srv_overview_metrics
Client reads srv_* only. No compute on the client.
```

## Project structure

```
src/mallory_engine/
  config.py              settings (env)
  db.py                  engine / session / Base
  models/                ref_* · stg_* · srv_* (SQLAlchemy)
  contracts/             ingest.py (L1→L2 contract) · serving.py (L2→L3 DTOs)
  api/                   ingest.py · serving.py · ops.py
  services/              llm.py (stub + anthropic) · entity_resolution · signal_pipeline
                         · tender_scoring · metrics
  pipeline/runner.py     orchestrates stg_* → srv_*
  seed/loader.py         loads ref_* from seed_data/*.json
  scripts/               init_db · load_seed · run_pipeline · mock_feeder
seed_data/               bundled watchlist seed (the static "vs KSSL" baseline)
sample_data/             sample crawler output (exact contract shape) for the feeder
tests/                   contract + tender-scoring tests (no DB needed)
```

## Connecting the real crawler

The crawler (Layer 1, built separately) POSTs to `/ingest/v1/page` in the shape defined by
`sample_data/sample_records.json` and `../docs/01_CRAWLER_CONTRACT.md`. Until then, `mock_feeder`
stands in. Nothing else changes when the real crawler goes live.

## Tests

```bash
pytest          # contract validation + deterministic tender scoring (no database required)
```

## Production notes

- `scripts/init_db` uses `create_all` for convenience; use **Alembic** for real migrations
  (`alembic.ini` + models are ready for `alembic revision --autogenerate`).
- Swap the `stub` LLM for `anthropic` via env; the pipeline interface is unchanged.
- Patents / FX / tender-portal sync workers (`ext_*`) are stubbed in the service catalog and not yet
  implemented here — they attach at the same staging boundary.
