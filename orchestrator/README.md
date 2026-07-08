# Mallory — Layer 1.5 (Acquisition Orchestrator)

Owns the **seed → job matrix**, the **Source Catalog** (source_id + trust tier, fully automatic),
the **scheduler** (cadence = per-source frequency), and the **coverage ledger** (no-miss guarantee).
Reads the static seed, generates crawl jobs, dispatches them to the Layer 1 crawler API, and forwards
returned records to the Layer 2 ingest API. Separate deployable (built for a high-end crawl VPS).

## Design invariants
- **Only human inputs: `{url, frequency, category}`.** Everything else is automatic — no review queue.
- **Tier ≠ relevance.** `frequency` = how often we crawl (human); `tier` = confidence weight for L2
  (auto); `relevance` = importance, computed by L2 vs KSSL. Tier never gates crawling/ingestion and
  never hides a signal — a new tier-3 blog can rank #1 on merit.
- **Fail to low trust.** Unknown domains → tier 3, `source_known=false`, never tier 1.

## Quickstart
```bash
pip install -e .            # or reuse a venv with fastapi+sqlalchemy+httpx+uvicorn
python -m mallory_orchestrator.scripts.init_db
python -m mallory_orchestrator.scripts.seed_sources     # seed catalog from registry + tender portals
uvicorn mallory_orchestrator.main:app --port 8090 --app-dir src   # admin console → http://localhost:8090
```
Requires the crawler API (:8099) and L2 (:8000) running to dispatch.

## End-to-end offline test (orchestrator → crawler → L2 → L3)
```bash
rm -f ../cralwer/data/crawl_history.sqlite          # let fixtures re-emit
python -m mallory_orchestrator.scripts.run_once --test
```
Verified: 7 fixture jobs → 10 records forwarded → all accepted by L2 → pipeline ran → visible in L3.

## Components
```
src/mallory_orchestrator/
  sources.py     source_id + tier classifier (eTLD+1, taxonomy, fail-to-tier-3) — automatic
  seed.py        loads the watchlist (entities/products/keywords/countries)
  jobgen.py      coverage-complete job matrix (all sources × all competitors × countries), stamps source_*
  orchestrate.py control loop: build jobs → dispatch → forward to L2 → mark coverage
  crawler_client.py  POST /v1/crawl (L1) + POST /ingest/v1/{type} (L2) + trigger L2 pipeline
  models.py      Source catalog · CoverageCell ledger · JobRun log
  api.py/main.py admin API + single-page admin console (static/index.html)
```

## Admin console (the only human control surface)
`http://localhost:8090` — add sources (url · frequency · category), watch the coverage cards,
job matrix, source catalog (with auto tier/known), and recent job runs. "Run offline test batch"
exercises the full chain; "Run due jobs" dispatches the production matrix.

## Taxonomy → default tier
gov_primary / manufacturer_ir / defence_org / tender_portal = **1** · trade_press / think_tank /
business_press = **2** · aggregator / blog_forum_social / unknown = **3**. Curated registry entries
(e.g. Janes = 1) override the category default.

## Still to wire
- Continuous **scheduler daemon** (cron loop calling `run due` on cadence) — today it's on-demand.
- **S-28 dynamic tiering** off the crawler's `/stats` + dedup (auto-demote freely; auto-promote to ≤2).
- Postgres for the control store at internet-scale (SQLAlchemy URL swap).
