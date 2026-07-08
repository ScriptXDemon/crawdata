# Mallory Intelligence Platform — System Overview

> KSSL (Kalyani Strategic Systems Ltd) is the fixed anchor. Every signal, score, match and
> verdict in this system is computed **vs KSSL**. KSSL is never "a competitor" — it is the
> lens through which all other data is read.

---

## 1. The three-layer model

The platform is **three separate products** owned by three teams. Data flows in **one direction only**.
The output of each layer is the *only* input the next layer is allowed to consume.

```
                 ┌────────────────────────────────────────────────┐
   admin seed ──▶│                                                │
   (ref_*)       │  LAYER 2 — DATA ENGINEERING  (Backend team)    │
                 │  all heavy compute lives here                  │
   external ────▶│                                                │
   APIs (ext_*)  │                                                │
                 └────────────────────────────────────────────────┘
                        ▲                          │
        raw records     │                          │  pre-computed
        (Ingest API)    │                          │  serving tables
                        │                          ▼  (read-only API)
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │ LAYER 1 — CRAWLER        │        │ LAYER 3 — CLIENT PRODUCT  │
   │ (Crawler team)           │        │ (Frontend team)           │
   │ acquisition only         │        │ presentation only         │
   └──────────────────────────┘        └──────────────────────────┘
        ▲                                          │
        │  watchlist_*.json                        │  writeback:
        └──────────── (from L2 admin) ─────────────┘  Mallory Q, report req
```

| Layer | Name | Owner | Allowed to do | NOT allowed to do |
|---|---|---|---|---|
| **L1** | Crawler | Crawler team | Execute crawl jobs (URL+keywords+budget), harvest, **filter**, resolve entities, POST records | Score, rank, judge, decide relevance, write to serving tables |
| **L2** | Data Engineering | Backend team | Classify, dedup, enrich, score, rank, synthesize, store everything | Render UI, expose raw/staging tables to the client |
| **L3** | Client Product | Frontend team | Read serving tables, filter, paginate, render, capture interactions | Compute scores, call LLMs directly, touch staging/reference tables |

**Core rule — no heavy compute on the client.** Every score, rank, verdict, fit %, edge value,
metric strip and synthesis paragraph is **pre-computed in L2 and materialized into `srv_*` tables.**
L3 only ever does `SELECT … WHERE … ORDER BY … LIMIT`. If the client needs a number, that number
already exists in a column.

---

## 2. The four table namespaces

Every table belongs to exactly one namespace. The namespace tells you who writes it and who reads it.

| Prefix | Namespace | Written by | Read by | Lifecycle |
|---|---|---|---|---|
| `ref_*` | Reference / seed | L2 Admin API (humans) | L2 services | Rarely changes (static) |
| `stg_*` | Staging / raw | L1 crawler + L2 sync workers | L2 services | Append-only, high churn |
| `ext_*` | External cache | L2 sync workers (APIs) | L2 services | Refreshed on schedule |
| `srv_*` | Serving | L2 services only | **L3 client only** | Continuously updated, denormalized for read |

The client (**L3**) is **only ever allowed to touch `srv_*`.** It never sees `stg_*`, `ref_*` or `ext_*`.
This is the firewall that keeps compute off the client and keeps raw/unverified data away from the CEO.

---

## 3. The two hard interfaces (the contracts)

Everything else is internal to a layer. These two boundaries are the only places the layers touch,
and each is a frozen contract:

### Interface A — L1 → L2 : the Ingest API
- Transport: HTTPS `POST /ingest/v1/{record_type}`
- Payload: a `document` object + one or more typed records (see `01_CRAWLER_CONTRACT.md`)
- L1 produces; L2 consumes. L1 never writes to a database directly.
- Defined in: **`01_CRAWLER_CONTRACT.md` §4**

### Interface B — L2 → L3 : the Serving API
- Transport: HTTPS `GET /api/v1/...` (read-only)
- Payload: rows from `srv_*` tables, already scored/ranked/paginated
- L2 produces; L3 consumes. L3 never computes.
- Writeback exceptions (only two): `POST /api/v1/mallory/chat` and `POST /api/v1/reports/ceo`,
  which are thin proxies into L2 compute — the client still does nothing itself.
- Defined in: **`03_CLIENT_PRODUCT.md` §3**

---

## 4. The eight data domains (end-to-end view)

Each domain flows L1 → L2 → L3. This table is the index; each row is detailed in the layer docs.

| # | Domain | L1 brings (stg_) | L2 produces (srv_) | L3 view |
|---|---|---|---|---|
| 1 | **Signals** (competitive / market / tech) | `stg_signals` | `srv_signals`, `srv_signal_details` | Overview feeds |
| 2 | **Tenders** | `stg_tenders` | `srv_tenders`, `srv_tender_matches` | Tender Pipeline |
| 3 | **Partnerships** | `stg_partnerships` | `srv_partnerships` | Partnership graph |
| 4 | **Geo footprint** | `stg_geo` | `srv_geo_entries` | Geo map |
| 5 | **Innovation** | `stg_innovation` | `srv_innovation` | Innovation pipeline |
| 6 | **Company events** | `stg_company_events` | feeds signals + synthesis | (folded into above) |
| 7 | **Patents** | *(not crawler)* `ext_patents` | `srv_patents`, `srv_patent_analytics`, `srv_patent_whitespace` | Patents by competitor / tech |
| 8 | **Matchups** | *(not crawler)* `ref_matchups` + specs | `srv_matchups`, `srv_matchup_specs` | Positioning dossier |

Plus two **cross-domain synthesis outputs** computed in L2 from the above:
- `srv_competitor_synthesis` + `srv_competitor_vulnerabilities` → competitor profile pages
- `srv_field_patterns` → field-wide patterns view
- `srv_overview_metrics` → the metric strips on every overview

---

## 5. Design principles (read before building anything)

1. **One direction.** Data never flows backward. L3 cannot write to L2 storage; L2 cannot ask L1 to refetch synchronously. Backward needs go through a queue, not a call.
2. **Pre-compute everything the client shows.** If a UI element shows a number, color, rank or label, it is a column in `srv_*`, not a calculation in the browser.
3. **Crawler brings content, not conclusions.** L1 resolves *who/what/where* (entity linking) but never decides *threat/watch/fav* or *go/maybe/pass*. Judgment is L2's job.
4. **Everything is "vs KSSL."** Relevance tags (CORE/ADJACENT/context), edge scores, fit %, and synthesis are always relative to KSSL's product lines.
5. **Provenance is mandatory.** Every `srv_*` row traces back to a `stg_documents.source_url` or an `ext_*` API record or a human `ref_*` edit. Nothing is unsourced. The `syn` (synthetic/estimate) vs `src` (sourced) flag from the prototype becomes a first-class `provenance` column.
6. **Idempotent ingestion.** The same article crawled twice must not create two signals. Dedup by `content_hash` and by semantic event-matching.
7. **Never fabricate.** If a value is unknown, it is `NULL`. Estimates are flagged `provenance='estimate'` and visually distinguished in L3.

---

## 6. The L2 processing monitor (internal ops view)

L2 is a *product in itself*, not just a database. It has an **internal processing monitor** — a control-room
view of records flowing through the pipeline (inputs → resolve → classify → enrich → rank → serving), with
live counts per `proc_status`, per-serving-table freshness, and service health across the 30 services. This
is for the **backend/ops team**, never the CEO. (A mockup of it was shown in chat.) It is how we watch the
"vs KSSL" machine run — distinct from L3, which is the polished client the CEO sees.

## 7. Status & document map

> **Purchasing is ON HOLD** — build all three layers on test data first, then buy APIs (see `04`).
> **First build target:** the L1 crawler, developed in a separate session from `01` + `seed/`.

| Doc | Layer | Audience |
|---|---|---|
| `00_SYSTEM_OVERVIEW.md` | all | everyone (this file) |
| `01_CRAWLER_CONTRACT.md` | L1 | **Crawler team** — standalone build spec (job → harvest → filter → records) |
| `seed/*.json` | L1 | **Crawler team** — real watchlist seed (entities, products, tech, tenders, sources) |
| `02_DATA_ENGINEERING.md` | L2 | **Backend team** — all tables + the 30-service catalog |
| `03_CLIENT_PRODUCT.md` | L3 | **Frontend team** — read APIs + views |
| `04_EXTERNAL_APIS.md` | L2 | **You** — APIs to purchase/test (on hold until layers built) |
