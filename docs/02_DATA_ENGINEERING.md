# LAYER 2 — Data Engineering Product

> **Audience: Backend team.** This is the engine room. All compute lives here. It has three inputs
> (crawler `stg_*`, external APIs `ext_*`, admin seed `ref_*`) and exactly one output consumer (the
> client, which reads `srv_*`). This doc lists **every table with its schema** and **every service with
> an explicit `INPUT table → LOGIC → OUTPUT table` contract.** Nothing here is rendered; everything
> here is computed.

---

## How to read the service catalog

Every service is written as a data-lineage contract:

```
S-NN  Service Name
  TRIGGER:  what starts it (event on a table | schedule | API call)
  INPUT:    table(s).column(s) it reads   ← always names a real table
  LOGIC:    the transformation, step by step
  OUTPUT:   table(s).column(s) it writes  → always names a real table
  MODEL:    LLM model if used, else —
  IDEMPOTENT: what happens if it runs twice on the same input
```

The chain is the product: `stg_signals` → **S-07** → `stg_signals(+class)` → **S-09** →
`srv_signal_details` → **S-10** → `srv_signals`. Follow the arrows and you can trace any pixel in
the client back to a crawled URL.

---

# PART A — THE TABLES

## A.1 Reference tables (`ref_*`) — the static seed (admin-owned)

These are written by humans through the Admin API (S-30), not by crawlers. They are the watchlist's
source of truth and the "vs KSSL" baseline. **These are exported nightly to the crawler as `watchlist_*.json`.**

```sql
-- The competitor universe (KSSL is row 0, is_anchor = true)
ref_competitors (
  id            TEXT PRIMARY KEY,        -- 'LT','ADANI','KSSL'
  name          TEXT NOT NULL,
  aliases       JSONB,                   -- ["Larsen & Toubro", ...]  exported to crawler
  hq_country    TEXT,
  threat_level  TEXT,                    -- threat | watch | fav
  sector_tags   TEXT,
  is_anchor     BOOLEAN DEFAULT false,   -- true only for KSSL
  priority      TEXT,                    -- P1|P2  (crawl cadence)
  created_at    TIMESTAMPTZ, updated_at TIMESTAMPTZ
);

ref_kssl_products (
  id            TEXT PRIMARY KEY,        -- 'ATAGS'
  name          TEXT NOT NULL,
  category_id   TEXT REFERENCES ref_categories(id),
  stage         TEXT,                    -- developed | offered | in-service
  indigenous_share_pct NUMERIC,
  description   TEXT
);

ref_competitor_products (
  id            TEXT PRIMARY KEY,        -- 'CAESAR6x6'
  competitor_id TEXT REFERENCES ref_competitors(id),
  name          TEXT NOT NULL,
  category_id   TEXT REFERENCES ref_categories(id),
  aliases       JSONB,
  programme     TEXT, stage TEXT
);

-- One spec row per (product, attribute). Works for both KSSL and competitor products.
ref_product_specs (
  id            BIGSERIAL PRIMARY KEY,
  product_id    TEXT NOT NULL,           -- FK to either kssl or competitor product
  product_side  TEXT NOT NULL,           -- 'kssl' | 'competitor'
  spec_label    TEXT NOT NULL,           -- 'Max range'
  value_text    TEXT,                    -- '40+'   (display)
  value_num     NUMERIC,                 -- 40.0    (for comparison)
  unit          TEXT,                    -- 'km'
  is_highlight  BOOLEAN,
  polarity      TEXT                     -- 'higher_better' | 'lower_better'  (for scoring)
);

-- The matchup DEFINITION (which competitor product is benchmarked vs which KSSL product)
ref_matchups (
  id            BIGSERIAL PRIMARY KEY,
  kssl_product_id       TEXT REFERENCES ref_kssl_products(id),
  competitor_product_id TEXT REFERENCES ref_competitor_products(id),
  category_id   TEXT, is_global BOOLEAN
);

ref_categories      ( id TEXT PK, name TEXT, slug TEXT );           -- Artillery, Small Arms...
ref_countries       ( id TEXT PK, name TEXT, iso2 TEXT, iso3 TEXT, region TEXT );
ref_tech_domains    ( id TEXT PK, name TEXT, slug TEXT, keywords JSONB );
ref_lenses          ( id TEXT PK, name TEXT );                       -- MARKET/DEMAND, BENCHMARK...
ref_pillars         ( id TEXT PK, name TEXT );                       -- competitive|market|technology
ref_ipc_domain_map  ( ipc_prefix TEXT PK, tech_domain_id TEXT );     -- 'F41' -> 'small_arms'  (patents)
ref_assignee_map    ( assignee_pattern TEXT PK, competitor_id TEXT ); -- patent assignee -> competitor
```

## A.2 Staging tables (`stg_*`) — what the crawler / API workers write

Append-only, high churn, **never read by the client.** Mirror the crawler contract record types plus a
processing-state machine. Each row walks: `received → resolved → classified → enriched → published`.

```sql
stg_documents (
  id            TEXT PRIMARY KEY,        -- 'doc_8a91'
  url           TEXT UNIQUE NOT NULL,    -- canonical; dedup key #1
  content_hash  TEXT NOT NULL,           -- dedup key #2
  source_id     TEXT, source_tier INT,
  title TEXT, author TEXT,
  published_at  TIMESTAMPTZ, date_precision TEXT,
  language TEXT, access TEXT,
  main_text     TEXT, main_text_en TEXT, summary TEXT,
  images        JSONB, attachments JSONB, tables JSONB,
  entities_detected JSONB,
  fetched_at    TIMESTAMPTZ, received_at TIMESTAMPTZ,
  dedup_status  TEXT                      -- new | duplicate_of:<id>
);

stg_signals (
  id            BIGSERIAL PRIMARY KEY,
  document_id   TEXT REFERENCES stg_documents(id),
  stream        TEXT,                    -- competitive|market|technology   (crawler)
  competitor_id TEXT,                    -- (crawler, resolved)
  detected_products JSONB, detected_country TEXT, tech_domain TEXT,
  event_summary TEXT,                    -- (crawler)
  deal_value_raw TEXT, deal_value_num NUMERIC, deal_currency TEXT,
  published_at  TIMESTAMPTZ,
  -- L2-computed fields (filled by services, null on arrival):
  resolved_competitor_id TEXT,           -- S-05
  dir           TEXT,                    -- S-07  threat|watch|fav
  lens          TEXT,                    -- S-07
  tags          JSONB,                   -- S-07
  dedup_group   TEXT,                    -- S-08
  proc_status   TEXT DEFAULT 'received'  -- received|resolved|classified|enriched|published
);

stg_tenders (
  id            BIGSERIAL PRIMARY KEY,
  document_id   TEXT REFERENCES stg_documents(id),
  source_ref    TEXT, title TEXT, issuer TEXT, country TEXT,
  category_hint TEXT,
  value_raw TEXT, value_num NUMERIC, value_currency TEXT, qty_raw TEXT,
  deadline_date DATE,
  requirement_text TEXT, requirement_fields JSONB,
  -- L2-computed:
  value_usd     NUMERIC,                 -- S-12
  category_id   TEXT,                    -- S-12 validated
  proc_status   TEXT DEFAULT 'received'
);

stg_partnerships    ( id BIGSERIAL PK, document_id TEXT, competitor_id TEXT, partner_name TEXT,
                      partner_id TEXT, partner_country TEXT, partner_kind TEXT, rel_type TEXT,
                      ptype_raw TEXT, deal_value_raw TEXT, date_announced DATE, description TEXT,
                      detected_lines JSONB, kssl_relevance TEXT /*S-20*/, proc_status TEXT );

stg_geo             ( id BIGSERIAL PK, document_id TEXT, competitor_id TEXT, country TEXT,
                      product_name TEXT, product_id TEXT, product_category TEXT,
                      contract_value_raw TEXT, qty_raw TEXT, since_year TEXT, stage TEXT,
                      note TEXT, confidence TEXT, proc_status TEXT );

stg_innovation      ( id BIGSERIAL PK, document_id TEXT, tech_domain TEXT, title TEXT,
                      competitor_id TEXT, driver TEXT, maturity_hint TEXT, horizon_hint TEXT,
                      description TEXT, proc_status TEXT );

stg_company_events  ( id BIGSERIAL PK, document_id TEXT, competitor_id TEXT, event_type TEXT,
                      headline TEXT, deal_value_raw TEXT, date_of_event DATE, description TEXT,
                      detected_lines JSONB, proc_status TEXT );
```

## A.3 External cache tables (`ext_*`) — API worker output

```sql
ext_patents (
  id            TEXT PRIMARY KEY,        -- jurisdiction+number
  jurisdiction  TEXT,                    -- IN|US|EP|WO
  patent_number TEXT, title TEXT, status TEXT,  -- granted|pending|filed
  filed_date DATE, granted_date DATE,
  assignee_raw  TEXT, inventors JSONB, ipc_codes JSONB, abstract TEXT,
  api_source    TEXT,                    -- uspto|epo|lens
  synced_at     TIMESTAMPTZ,
  -- L2-computed:
  competitor_id   TEXT,                  -- S-15 (via ref_assignee_map)
  tech_domain_id  TEXT,                  -- S-15 (via ref_ipc_domain_map)
  kssl_relevance  TEXT
);

ext_fx_rates ( base TEXT, quote TEXT, rate NUMERIC, as_of DATE, PRIMARY KEY(base,quote,as_of) );
```

## A.4 Serving tables (`srv_*`) — the ONLY tables the client reads

Denormalized, pre-computed, read-optimized. Every column the UI needs is here as a literal value.

```sql
-- Feed cards (all three streams). One row = one card in an overview feed.
srv_signals (
  id            BIGINT PRIMARY KEY,
  pillar        TEXT,                    -- competitive|market|technology
  dir           TEXT,                    -- threat|watch|fav     (pre-computed color)
  rank          INT,                     -- pre-sorted; client just ORDER BY rank
  rank_group    TEXT,                    -- 'Priority — Competitive Threats'
  title         TEXT, meta TEXT, company TEXT, lens TEXT,
  sowhat        TEXT,                    -- LLM-generated implication
  tags          JSONB,
  ago_display   TEXT,                    -- '9 days', 'Jun 2026'  (pre-formatted)
  source_url    TEXT, provenance TEXT,   -- sourced|estimate
  published_at  TIMESTAMPTZ
);

-- Right-panel detail for a signal (1:1 with srv_signals)
srv_signal_details (
  signal_id     BIGINT PRIMARY KEY,
  rank_display  TEXT, dir TEXT, title TEXT,
  facts         JSONB,    -- [["Company","L&T"],["Category","Artillery"],...]
  what_text     TEXT, why_text TEXT,
  lens_reads    JSONB,    -- [["MARKET/DEMAND","..."],["BENCHMARK","..."]]
  actions       JSONB,    -- [["Counter","..."],["Benchmark","..."]]
  suggest       JSONB,    -- Mallory prompt chips
  source_url    TEXT
);

srv_tenders (
  id            BIGINT PRIMARY KEY,
  title TEXT, issuer TEXT, country TEXT, category TEXT,
  value_display TEXT, value_usd NUMERIC, qty TEXT,
  deadline_date DATE, dl_days INT,       -- recomputed daily by S-14
  req_note TEXT, requirements JSONB,
  lean          TEXT,                    -- go|maybe|pass  (LLM verdict)
  lean_text     TEXT,
  status        TEXT,                    -- open|closing|closed
  source_url TEXT, provenance TEXT
);

srv_tender_matches (
  id            BIGSERIAL PRIMARY KEY,
  tender_id     BIGINT REFERENCES srv_tenders(id),
  kssl_product_id TEXT, kssl_product_name TEXT,
  fit_level     TEXT,                    -- high|medium|low
  fit_pct       INT,                     -- 88
  match_lines   JSONB                    -- [["up","52km exceeds 45km bar"],["down","indigenous 42%"]]
);

srv_matchups (
  id            BIGINT PRIMARY KEY,
  category TEXT, is_global BOOLEAN, dir TEXT, country TEXT,
  comp_name TEXT, comp_by TEXT, kssl_name TEXT, kssl_by TEXT,
  edge_score    INT,                     -- 0-100, pre-computed by S-22
  adv_comp JSONB, adv_kssl JSONB, details JSONB, verdict TEXT
);
srv_matchup_specs (
  matchup_id BIGINT, spec_label TEXT, comp_value TEXT, comp_num NUMERIC,
  kssl_value TEXT, kssl_num NUMERIC, leader TEXT /*comp|kssl|tie*/, is_highlight BOOLEAN
);

srv_geo_entries (
  id BIGINT PRIMARY KEY, competitor_id TEXT, competitor_name TEXT,
  country TEXT, product_name TEXT, category TEXT,
  contract_value TEXT, since_year TEXT, qty TEXT, stage TEXT, note TEXT,
  provenance TEXT,                       -- sourced|estimate  (the src/syn flag)
  source_url TEXT
);

srv_partnerships (
  id BIGINT PRIMARY KEY, competitor_id TEXT, competitor_name TEXT,
  partner_id TEXT, partner_name TEXT, partner_kind TEXT, rel_type TEXT,
  sig_score INT, country TEXT, deal_value TEXT, date_announced DATE,
  insight TEXT, meaning TEXT,
  kssl_relevance TEXT,                   -- CORE|ADJACENT|context
  provenance TEXT
);

srv_patents (
  id TEXT PRIMARY KEY, competitor_id TEXT, tech_domain_id TEXT,
  jurisdiction TEXT, patent_number TEXT, title TEXT, status TEXT,
  filed_date DATE, granted_date DATE, assignee TEXT, ipc_codes JSONB,
  abstract TEXT, kssl_relevance TEXT
);
srv_patent_analytics (
  tech_domain_id TEXT PRIMARY KEY, total_filings INT, active_assignees INT,
  crowding TEXT,                         -- sparse|emerging|crowded
  kssl_position TEXT,                    -- leader|parity|contested|behind
  leaders JSONB, summary TEXT, computed_at TIMESTAMPTZ
);
srv_patent_whitespace (
  id BIGSERIAL PRIMARY KEY, tech_domain_id TEXT, area TEXT, filings INT, note TEXT
);

srv_innovation (
  id BIGINT PRIMARY KEY, tech_domain_id TEXT, title TEXT,
  maturity TEXT, gap_vs_kssl TEXT,       -- ahead|parity|behind
  driver TEXT, horizon TEXT,
  body TEXT, impact TEXT, whats_new TEXT, comp_note TEXT, action TEXT,
  sources TEXT, updated_at TIMESTAMPTZ
);

srv_competitor_synthesis (
  competitor_id TEXT PRIMARY KEY, thesis TEXT, strat_pattern TEXT,
  strat_sowhat TEXT, predictions JSONB, moves JSONB, updated_at TIMESTAMPTZ
);
srv_competitor_vulnerabilities (
  id BIGSERIAL PRIMARY KEY, competitor_id TEXT, title TEXT,
  from_sources JSONB, intel TEXT, ord INT
);

srv_field_patterns ( id BIGSERIAL PK, title TEXT, examples TEXT, summary TEXT, exceptions TEXT, bottom_line TEXT );

-- Pre-computed metric strips for every overview header (so client renders zero math)
srv_overview_metrics (
  pillar TEXT PRIMARY KEY, generated_at TIMESTAMPTZ,
  metrics JSONB   -- [{label:"Competitive threats", value:6, color:"threat", filter:"threat"}, ...]
);
```

---

# PART B — THE SERVICE CATALOG (30 services)

## B.1 Ingestion services (boundary in)

```
S-01  Crawler Ingest API                                         [Interface A]
  TRIGGER:  HTTP POST /ingest/v1/{type} from L1
  INPUT:    JSON payload (document + typed record) from crawler
  LOGIC:    1. validate against the type schema (01_CRAWLER_CONTRACT §6 rules)
            2. upsert stg_documents by url; if content_hash seen → set dedup_status
            3. insert typed record into stg_{type} with proc_status='received'
            4. return 200{ingest_id} or 422{failing_rule}
  OUTPUT:   stg_documents, stg_{signals|tenders|partnerships|geo|innovation|company_events}
  MODEL:    —
  IDEMPOTENT: same url+content_hash → no new document; returns existing id

S-02  Patent API Sync Worker
  TRIGGER:  schedule (weekly)
  INPUT:    ref_competitors.name+aliases, ref_tech_domains.keywords  (what to query)
  LOGIC:    1. for each competitor → query USPTO/EPO/Lens by assignee
            2. for each tech domain → query by IPC + keyword
            3. dedup vs ext_patents by (jurisdiction, patent_number)
            4. insert new; update status on existing (pending→granted)
  OUTPUT:   ext_patents
  MODEL:    —
  IDEMPOTENT: keyed by (jurisdiction,patent_number); re-runs only update status

S-03  Tender Portal Sync Worker
  TRIGGER:  schedule (6–12h, mirrors crawler P0)
  INPUT:    watchlist_tenders source list + keywords (from ref_*)
  LOGIC:    pull SAM.gov API / scrape GePNIC/OCCAR → emit the SAME shape as the
            crawler 'tender' record → hand to S-01 path (writes stg_tenders)
  OUTPUT:   stg_tenders
  MODEL:    —
  IDEMPOTENT: dedup by source_ref + country

S-04  Exchange Rate Sync Worker
  TRIGGER:  schedule (daily)
  INPUT:    external FX API
  LOGIC:    fetch INR/USD/EUR/GBP cross-rates → upsert
  OUTPUT:   ext_fx_rates
  MODEL:    —
  IDEMPOTENT: keyed by (base,quote,as_of)
```

## B.2 Resolution & classification (staging → staging)

```
S-05  Entity Resolution Service
  TRIGGER:  row enters stg_signals|partnerships|geo|innovation|company_events (proc_status='received')
  INPUT:    stg_*.competitor_id (raw), stg_documents.entities_detected, ref_competitors.aliases
  LOGIC:    confirm/repair the crawler's entity link: fuzzy-match surface forms to ref_competitors;
            if crawler said null but text names a known alias → set resolved_competitor_id;
            if a NEW company recurs (≥N docs) → write a 'candidate competitor' to an admin review queue
  OUTPUT:   stg_*.resolved_competitor_id ; proc_status='resolved'
  MODEL:    embedding match (no LLM) + fallback Haiku for ambiguous
  IDEMPOTENT: deterministic on same input

S-06  Document Dedup Service
  TRIGGER:  new stg_documents row
  INPUT:    stg_documents.content_hash, .url, .main_text
  LOGIC:    exact match on hash/url → mark duplicate; else embedding similarity vs docs in 30-day
            window > 0.92 → mark duplicate_of:<id>, carry over the richer text
  OUTPUT:   stg_documents.dedup_status
  MODEL:    embeddings
  IDEMPOTENT: yes

S-07  Signal Classification Engine
  TRIGGER:  stg_signals.proc_status='resolved'
  INPUT:    stg_signals.event_summary, stg_documents.main_text, resolved_competitor_id,
            ref_competitors.threat_level, ref_kssl_products (to know KSSL lines)
  LOGIC:    decide pillar (competitive|market|technology), dir (threat|watch|fav),
            lens (which of ref_lenses), tags[] (threat/watch/fav/opening/atstake/deadline) — all "vs KSSL"
  OUTPUT:   stg_signals.dir, .lens, .tags ; proc_status='classified'
  MODEL:    Haiku (bulk) → Sonnet (low-confidence)
  IDEMPOTENT: yes; re-run overwrites classification

S-08  Signal Dedup / Event-Merge Engine
  TRIGGER:  stg_signals.proc_status='classified'
  INPUT:    stg_signals (same resolved_competitor_id, 30-day window), event_summary embeddings
  LOGIC:    cluster signals describing the SAME real-world event (e.g. one K9 order reported by 4 sources);
            elect a primary; attach the others' source_urls
  OUTPUT:   stg_signals.dedup_group
  MODEL:    embeddings
  IDEMPOTENT: stable cluster ids
```

## B.3 Enrichment & publish (staging → serving)

```
S-09  Signal Enrichment Engine
  TRIGGER:  stg_signals.proc_status='classified' AND elected primary of its dedup_group
  INPUT:    stg_signals (+class), stg_documents.main_text, ref_kssl_products,
            ref_competitor synthesis context
  LOGIC:    LLM, structured output → generate sowhat, sec[] (multi-lens reads), facts[],
            what/why, actions[], suggest[] — all framed "what this means for KSSL"
  OUTPUT:   srv_signal_details (full row) ; stg_signals.proc_status='enriched'
  MODEL:    Sonnet
  IDEMPOTENT: re-run replaces the srv_signal_details row for that signal_id

S-10  Signal Ranking Engine
  TRIGGER:  after S-09 batch, or schedule (hourly)
  INPUT:    stg_signals(proc_status='enriched'): dir, deal_value_num, published_at,
            ref_competitors.threat_level
  LOGIC:    score = f(threat weight, recency decay, deal size, KSSL-line overlap);
            sort within pillar; assign rank + rank_group; PUBLISH the card to serving
  OUTPUT:   srv_signals (insert/replace) ; stg_signals.proc_status='published'
  MODEL:    — (deterministic scoring)
  IDEMPOTENT: full recompute of rank each run

S-11  Overview Metrics Builder
  TRIGGER:  after S-10
  INPUT:    srv_signals grouped by pillar, dir
  LOGIC:    count threats/watch/fav, count closing tenders, etc. → build the metric-strip JSON
  OUTPUT:   srv_overview_metrics
  MODEL:    —
  IDEMPOTENT: yes (full recompute)
```

## B.4 Tender pipeline

```
S-12  Tender Normalizer
  TRIGGER:  stg_tenders.proc_status='received'
  INPUT:    stg_tenders.value_num/currency, ext_fx_rates, category_hint, ref_categories
  LOGIC:    convert value → value_usd; validate category_hint → category_id; parse requirement_fields
  OUTPUT:   stg_tenders.value_usd, .category_id ; proc_status='normalized'
  MODEL:    —
  IDEMPOTENT: yes

S-13  Tender Scoring Engine     ← the example you gave
  TRIGGER:  stg_tenders.proc_status='normalized'
  INPUT:    stg_tenders.requirement_text/fields  +  ref_kssl_products + ref_product_specs(side='kssl')
  LOGIC:    1. parse requirements into slots (calibre, range, weight, qty, indigenous %...)
            2. for each KSSL product: compare each slot to its spec (value_num, polarity)
            3. per-product fit_pct = weighted slot match; fit_level by threshold
            4. build match_lines[] (up = KSSL exceeds bar, down = gap)
            5. LLM: write lean (go|maybe|pass) + lean_text from the fit picture
  OUTPUT:   srv_tenders (row) + srv_tender_matches (1..n) + match_lines
  MODEL:    Sonnet (requirement parsing + verdict); scoring math deterministic
  IDEMPOTENT: re-run replaces matches for that tender_id

S-14  Tender Deadline Tracker
  TRIGGER:  schedule (daily 00:00)
  INPUT:    srv_tenders.deadline_date
  LOGIC:    dl_days = deadline - today; status open→closing(≤7)→closed(≤0);
            closing tenders → emit to S-27
  OUTPUT:   srv_tenders.dl_days, .status
  MODEL:    —
  IDEMPOTENT: yes
```

## B.5 Patents

```
S-15  Patent Assignee/IPC Mapper
  TRIGGER:  new ext_patents rows
  INPUT:    ext_patents.assignee_raw, .ipc_codes ; ref_assignee_map ; ref_ipc_domain_map
  LOGIC:    map assignee → competitor_id; map IPC prefix → tech_domain_id; set kssl_relevance
  OUTPUT:   ext_patents.competitor_id, .tech_domain_id, .kssl_relevance ; srv_patents (publish)
  MODEL:    fuzzy match + Haiku fallback
  IDEMPOTENT: yes

S-16  Patent Analysis Engine
  TRIGGER:  after S-15 batch (weekly)
  INPUT:    srv_patents grouped by tech_domain_id, competitor_id, filed_date
  LOGIC:    filings/yr trend; crowding (sparse/emerging/crowded); kssl_position; leaders[]; summary
  OUTPUT:   srv_patent_analytics
  MODEL:    Sonnet for summary; counts deterministic
  IDEMPOTENT: yes (recompute)

S-17  Patent Whitespace Detector
  TRIGGER:  after S-16
  INPUT:    srv_patents IPC sub-class counts within KSSL-relevant domains
  LOGIC:    sub-areas with <3 total filings, or 0 KSSL + >0 competitor → whitespace/gap
  OUTPUT:   srv_patent_whitespace
  MODEL:    —
  IDEMPOTENT: yes
```

## B.6 Geo / Partnerships / Innovation

```
S-18  Geo Footprint Updater
  TRIGGER:  stg_geo.proc_status='resolved'
  INPUT:    stg_geo + ref_competitors + ref_countries
  LOGIC:    upsert by (competitor, country, product_category): new→insert, existing→update stage/value;
            if a crawled doc confirms a prior 'estimate' → flip provenance 'estimate'→'sourced'
  OUTPUT:   srv_geo_entries
  MODEL:    —
  IDEMPOTENT: keyed upsert

S-19  Partnership Graph Updater
  TRIGGER:  stg_partnerships.proc_status='resolved'
  INPUT:    stg_partnerships + existing srv_partnerships (fuzzy partner match)
  LOGIC:    new partner → insert node; existing → update/append event; recount competitor's
            core-relevant partnership total
  OUTPUT:   srv_partnerships
  MODEL:    —
  IDEMPOTENT: dedup by (competitor, partner, rel_type, date)

S-20  Partnership Relevance Tagger
  TRIGGER:  after S-19 (or inline)
  INPUT:    srv_partnerships.partner_kind/rel_type/description + ref_kssl_products lines
  LOGIC:    classify CORE (touches a KSSL line) | ADJACENT | context; write the "<b>Threat/Opening/
            Dependency</b>" meaning text
  OUTPUT:   srv_partnerships.kssl_relevance, .meaning
  MODEL:    Haiku/Sonnet
  IDEMPOTENT: yes

S-21  Innovation Pipeline Updater
  TRIGGER:  stg_innovation.proc_status='resolved'
  INPUT:    stg_innovation + ref_tech_domains + known KSSL capability per domain
  LOGIC:    upsert item per domain; LLM generates body/impact/whats_new/comp_note/action and
            gap_vs_kssl (ahead|parity|behind)
  OUTPUT:   srv_innovation
  MODEL:    Sonnet
  IDEMPOTENT: upsert by (domain, title)
```

## B.7 Matchups & synthesis (cross-domain)

```
S-22  Matchup Edge Score Recalculator
  TRIGGER:  change in ref_product_specs OR ref_matchups (admin edit via S-30)
  INPUT:    ref_matchups + ref_product_specs (both sides)
  LOGIC:    per matchup, compare each spec (value_num, polarity) → count leads each side →
            edge_score 0-100 → dir; build srv_matchup_specs with leader flag; LLM writes verdict + adv lists
  OUTPUT:   srv_matchups + srv_matchup_specs
  MODEL:    Sonnet (verdict text); scoring deterministic
  IDEMPOTENT: full recompute per matchup

S-23  Competitor Synthesis Updater
  TRIGGER:  ≥3 new signals/partnerships for a competitor in 30 days, OR weekly
  INPUT:    srv_signals + srv_partnerships + srv_geo_entries + srv_innovation for that competitor
  LOGIC:    LLM re-derives thesis, vulnerabilities, predictions, recommended KSSL moves; marks
            a prediction 'hit' if a predicted event occurred
  OUTPUT:   srv_competitor_synthesis + srv_competitor_vulnerabilities
  MODEL:    Sonnet
  IDEMPOTENT: replaces synthesis row per competitor

S-24  Field Pattern Analyzer
  TRIGGER:  schedule (monthly) or major synthesis change
  INPUT:    all srv_competitor_synthesis + srv_partnerships (full field)
  LOGIC:    LLM derives cross-competitor patterns (borrowed-IP gap, archetypes, chokepoints,
            consolidation, Pinaka pile-up) + bottom line
  OUTPUT:   srv_field_patterns
  MODEL:    Opus (highest reasoning)
  IDEMPOTENT: replaces pattern set
```

## B.8 Client-facing compute (L2 side of the writeback)

```
S-25  CEO Report Generator
  TRIGGER:  POST /api/v1/reports/ceo from L3
  INPUT:    top srv_signals (3 pillars) + srv_tenders(lean=go) + srv_innovation(high maturity)
            + srv_competitor_synthesis
  LOGIC:    cross-pillar synthesis → executive brief (HTML/structured)
  OUTPUT:   returned to client (optionally cached in a reports table)
  MODEL:    Opus
  IDEMPOTENT: stateless

S-26  Mallory AI Service
  TRIGGER:  POST /api/v1/mallory/chat from L3
  INPUT:    { message, panel_context, entity ids } → fetches the relevant srv_* rows ONLY
            (panel scope = which tables Mallory may read, mirroring the prototype)
  LOGIC:    RAG over the scoped serving rows → system prompt "You are Mallory, KSSL's analyst…"
  OUTPUT:   streamed answer to client
  MODEL:    Sonnet (panel scope) / Opus (CEO scope)
  IDEMPOTENT: stateless
```

## B.9 Operational services

```
S-27  Notification / Alert Engine
  INPUT:    tender status→closing (S-14), new dir='threat' signal (S-10), new CORE partnership (S-20)
  OUTPUT:   alerts (SSE to client, email digest)

S-28  Source Reliability Tracker
  INPUT:    S-01 accept/reject rate per source, S-06 duplication rate, downstream corrections
  OUTPUT:   updates ref source trust tiers → re-exported to crawler in source_registry.json
  (this is the feedback loop that improves crawler targeting over time)

S-29  Data Freshness Monitor
  INPUT:    max(crawled_at) per domain, age of srv_geo/innovation rows
  OUTPUT:   internal staleness dashboard + alerts (e.g. geo entry >90d, innovation >60d)

S-30  Admin / Seed Management API
  TRIGGER:  human (KSSL admin)
  INPUT:    CRUD on ref_* (competitors, products, specs, matchups, categories, ipc maps)
  OUTPUT:   ref_* tables; nightly export of watchlist_*.json to crawler; triggers S-22 on spec change
  MODEL:    —
```

---

## PART C — The end-to-end trace (one example, fully)

How a single crawled article becomes a ranked card the CEO sees — every hop names a table and a service:

```
1. Crawler fetches janes.com article  ──POST S-01──▶  stg_documents(doc_8a91) + stg_signals(id=501, proc=received)
2. S-05 Entity Resolution   reads stg_signals.competitor_id="LT" ✓  ──▶  stg_signals.resolved_competitor_id=LT (proc=resolved)
3. S-07 Classification      reads event_summary + KSSL lines  ──▶  dir=threat, lens=BENCHMARK, tags=[threat] (proc=classified)
4. S-08 Dedup               4 sources, same event  ──▶  dedup_group=g77, signal 501 elected primary
5. S-09 Enrichment (Sonnet) reads main_text + KSSL context  ──▶  srv_signal_details(501): sowhat, facts, lens_reads, actions (proc=enriched)
6. S-10 Ranking             scores vs other competitive signals  ──▶  srv_signals(501): rank=1, rank_group="Priority — Threats" (proc=published)
7. S-11 Metrics             recounts  ──▶  srv_overview_metrics(competitive): threats=7
8. L3 Competitive Feed API  GET /api/v1/signals?pillar=competitive  ──▶  SELECT * FROM srv_signals ORDER BY rank  (NO compute)
9. CEO clicks the card → GET /api/v1/signals/501/detail  ──▶  SELECT * FROM srv_signal_details WHERE signal_id=501
10. CEO asks Mallory "what does this mean for KSSL?" → S-26 reads srv_signal_details(501) only → streamed answer
```

Every arrow is a contract. Every box is a table in one of the four namespaces. No step in L3 computes anything.
