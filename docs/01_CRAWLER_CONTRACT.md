# LAYER 1 — Crawler Build Spec (standalone)

> **Read this alone.** This document is self-contained so a fresh session can build the crawler with no
> other context. It defines the crawler as a **job-driven engine**: it receives a *crawl job*
> (URL + keywords + budget), harvests raw web assets, **filters** them down, and returns **structured
> records** to our Ingest API. The real seed data it targets lives in `docs/seed/*.json` (already
> populated — not placeholders).

---

## 0. Context (what this crawler feeds)

We are building a defence competitive-intelligence platform for **KSSL** (Kalyani Strategic Systems Ltd),
an Indian defence manufacturer. KSSL is the fixed **anchor**: everything is read "vs KSSL". The platform
has 3 layers; **this crawler is Layer 1 (acquisition).** It feeds Layer 2 (data engineering), which scores
and ranks everything, which feeds Layer 3 (the client UI). The crawler's entire world is:

```
   our seed (who/what to watch)          your output (clean structured data)
   docs/seed/*.json   ───▶  [ CRAWLER ]  ───▶  POST /ingest/v1/...  ───▶  Layer 2
```

**The crawler acquires and normalizes. It never scores, ranks, or judges relevance-to-strategy.**
"Is this a threat?" is Layer 2's call. "Is this page about a watched entity and does it parse?" is yours.

---

## 1. The crawler is a job engine (the correct mental model)

A web crawler can bring back anything. We constrain it with **jobs**. A *crawl job* is the unit of work.
**We generate jobs from the seed** (our logic, see §7) and hand them to you; **you execute them** and
return filtered records.

```
            ┌──────────── one CRAWL JOB ─────────────┐
  seed ──▶  │ seed_urls[], keywords[], max_pages,     │ ──▶ [ HARVEST ] ──▶ raw assets
 (logic)    │ max_depth, capture[], render_js, ...    │        (html/js/img/pdf/shot)
            └─────────────────────────────────────────┘              │
                                                                      ▼
                                              [ FILTER + EXTRACT ] ──▶ document + typed records
                                              (relevance gate, clean,        │
                                               extract, resolve, dedup)      ▼
                                                                    POST /ingest/v1/...
```

Three stages, all yours to build:
1. **Harvest** — fetch within the job's budget; capture the raw asset types the job asks for.
2. **Filter** — drop anything that doesn't match keywords or resolve to a watched entity (mechanical, not strategic).
3. **Extract** — turn the surviving raw pages into clean structured records and POST them.

---

## 2. JOB INPUT — what a crawl job contains (we give you this)

```jsonc
{
  "job_id":        "job_2026-06-29_LT_news_01",
  "job_type":      "news",            // news | tender | profile | spec | patent_aux
  "seed_urls":     ["https://idrw.org/?s=L%26T+defence", "https://janes.com/search?q=L%26T"],
  "keywords":      ["L&T", "Larsen & Toubro", "K9 Vajra", "artillery"],   // must-match (any)
  "target_entity": "LT",              // watchlist competitor id, or null for tenders
  "max_pages":     40,                // hard crawl budget (pages fetched)
  "max_depth":     2,                 // link-follow depth from each seed_url
  "same_domain_only": true,           // don't wander off the seed domain
  "render_js":     false,             // true → use a headless browser (JS-heavy sites)
  "freshness_days":120,               // ignore content published older than this
  "capture":       ["html","text","images","screenshot"],  // raw asset types to grab (see §4)
  "expected_record_types": ["competitive_signal","company_event"]  // what we hope to extract
}
```

**You own everything inside the job; we own which jobs to send.** If a field is absent, use the
`source_registry.json → global_capture_defaults`.

### Recommended job parameters (our suggestion — tune from results)

| job_type | seed example | max_pages | max_depth | render_js | capture |
|---|---|---|---|---|---|
| `news` (signals/events) | news-site search URL for an entity | 30–50 | 2 | only if JS-gated | `html, text, images, screenshot` |
| `tender` | procurement portal listing/search | 50–80 | 2 | often true (portals) | `html, text, pdf, screenshot` |
| `profile` (partnerships/geo) | company IR/press page | 20–40 | 2 | sometimes | `html, text` |
| `spec` (product specs) | OEM product page / datasheet | 5–15 | 1 | sometimes | `html, text, images, pdf, screenshot` |
| `patent_aux` (corroborate) | a patent landing page | 1–3 | 0 | no | `html, text` |

> Depth ≥3 rarely helps and explodes cost — keep it at 1–2. `max_pages` is a hard stop, not a target.

---

## 3. JOB OUTPUT — what FILTERED data looks like (you give us this)

This is the heart of the contract. Raw harvest is huge and messy; **filtered output is small and clean.**
The filter is **mechanical**: keep a page only if it (a) matches ≥1 keyword AND (b) resolves to a watched
entity (or is a tender matching a keyword). Everything kept becomes **one `document`** (the source) plus
**one or more typed records** that reference it.

### 3.1 Raw → Filtered (what you keep vs throw away)

| Raw harvest (you fetch) | Filtered output (you keep) | Thrown away |
|---|---|---|
| Full HTML with nav/ads/scripts | Cleaned `main_text` (article body only) | nav, ads, cookie bars, comments, related-links |
| Every `<img>` on the page | 0–3 **meaningful** images (product, event, chart, map) | logos, avatars, stock, decorative, ad creatives |
| All linked files | Tender RFP **PDFs** (+extracted text); primary docs | unrelated downloads |
| Inline JS / API responses | Only data extracted from them (e.g. a spec JSON) | the JS itself |
| A screenshot of the page | 1 **full-page screenshot** as audit evidence | — |
| Every outbound link | — (links are for crawling, not output) | all of them |

### 3.2 The `document` object (the source — one per kept URL)

```jsonc
{
  "url":            "https://janes.com/defence/india/lt-k9-followon",  // canonical, dedup key, REQUIRED
  "content_hash":   "sha256:9af1...",                                  // hash of main_text, REQUIRED
  "fetched_at":     "2026-06-29T14:30:00Z",                            // REQUIRED
  "source_id":      "JANES",                                          // from source_registry, REQUIRED
  "source_tier":    1,
  "title":          "L&T secures ₹4,500 cr K9 Vajra follow-on order",  // REQUIRED
  "author":         "Staff Reporter",
  "published_at":   "2026-06-28T00:00:00Z",
  "date_precision": "exact",                  // exact | approx | unknown
  "language":       "en",
  "access":         "open",                   // open | paywalled | partial
  "main_text":      "The Ministry of Defence has...",   // cleaned body, REQUIRED — Layer 2 runs NLP on this
  "main_text_en":   null,                     // English translation if language != en
  "summary":        "L&T won ₹4,500 cr for 100 K9 Vajra guns.",  // optional extractive 1–2 lines
  "images": [
    { "url":"https://.../k9.jpg", "storage_path":"s3://mallory-raw/img/abc.jpg",
      "caption":"K9 Vajra-T on trials", "role":"product", "width":1200, "height":800 }
  ],
  "attachments": [
    { "url":"https://.../rfp.pdf", "storage_path":"s3://mallory-raw/doc/xyz.pdf",
      "type":"pdf", "extracted_text":"REQUEST FOR PROPOSAL ..." }
  ],
  "screenshot":   { "storage_path":"s3://mallory-raw/shot/abc.png", "captured_at":"2026-06-29T14:30:05Z" },
  "tables": [ { "title":"Order breakdown", "rows":[{"item":"K9 Vajra","qty":"100","value":"₹4,500 cr"}] } ],
  "entities_detected": [
    { "surface":"L&T",      "resolved_id":"LT",      "type":"competitor", "confidence":0.98 },
    { "surface":"K9 Vajra", "resolved_id":"K9VAJRA", "type":"product",    "confidence":0.95 },
    { "surface":"India",    "resolved_id":"India",   "type":"country",    "confidence":0.99 }
  ]
}
```

Then attach **one or more typed records** (§5), each carrying `document_id`.

---

## 4. Capture types — when to grab html / js / images / screenshots / media / pdf

This answers "what should the crawler bring?" per asset type. Grab only what the `capture[]` list asks for.

| Capture type | Grab when | Keep in output? | Notes |
|---|---|---|---|
| `html` | always (it's the page) | NO (raw) | used only to extract `main_text`/tables; never POST raw HTML |
| `text` | always | YES → `main_text` | the cleaned body; **the single most important field** |
| `images` | product/spec/event/innovation jobs | YES → `images[]` (0–3) | download to storage; tag `role`; skip decorative |
| `pdf` | tender jobs (RFP), primary docs | YES → `attachments[]` + extracted text | **always** fetch+extract tender PDFs |
| `screenshot` | news + tender + spec jobs | YES → `screenshot` | **audit evidence** — sources change/disappear; one full-page shot proves what we saw |
| `media` (video/audio) | only if explicitly requested | metadata only (url, title) | do NOT download large media; just record the link |
| `js` | only when data is in a JS payload | NO (extract data, drop js) | use for sites that render content client-side |

**Screenshots are evidence, not decoration.** Because defence sources frequently edit or pull stories, a
timestamped screenshot is our audit trail behind every signal the CEO sees. One per kept document.

---

## 5. The six typed records (what L2 expects)

Each is POSTed to `/ingest/v1/{type}` with `document_id`. Fields marked **(you)** are yours to fill;
everything else Layer 2 computes — **do not send L2 fields.** (KSSL itself is sent as a `company_event`
with `competitor_id:"KSSL"`, never as a competitor.)

### 5.1 `competitive_signal` → a competitor did/said something
```jsonc
{ "document_id":"doc_8a91",
  "stream":"competitive",            // (you) competitive | market | technology
  "competitor_id":"LT",              // (you) resolved id, or null
  "detected_products":["K9VAJRA"],   // (you)
  "detected_country":"India",        // (you)
  "tech_domain":null,                // (you) for market/technology streams
  "event_summary":"L&T won ₹4,500 cr follow-on for 100 K9 Vajra guns",  // (you) one line
  "deal_value_raw":"₹4,500 cr", "deal_value_num":45000000000, "deal_currency":"INR",  // (you)
  "published_at":"2026-06-28T00:00:00Z" }                                              // (you)
  // L2 adds: dir(threat/watch/fav), lens, tags, rank, sowhat, multi-lens reads, actions
```

### 5.2 `tender` → a procurement opportunity
```jsonc
{ "document_id":"doc_3b22",
  "source_ref":"MoD/2026/ART/0441", "title":"155mm 52-cal Mounted Gun System",  // (you)
  "issuer":"Ministry of Defence", "country":"India", "category_hint":"artillery", // (you)
  "value_raw":"~₹6,500 cr", "value_num":65000000000, "value_currency":"INR", "qty_raw":"100 units", // (you)
  "deadline_date":"2026-07-08",      // (you) ISO — the single most important tender field
  "requirement_text":"Full RFP text from the PDF...",                            // (you)
  "requirement_fields":[ {"label":"System","value":"155mm / 52-cal mounted gun"},
                         {"label":"Range","value":"≥ 45 km"} ] }                  // (you) if structured
  // L2 adds: value_usd, KSSL product fit matches, fit %, lean (go/maybe/pass)
```

### 5.3 `partnership` → an alliance / JV / MoU / licence
```jsonc
{ "document_id":"doc_5c77",
  "competitor_id":"NIBE", "partner_name":"Sig Sauer", "partner_id":"SIGSA",       // (you)
  "partner_country":"USA", "partner_kind":"Foreign OEM", "rel_type":"license",     // (you) jv|mou|license|supply|customer|investment
  "ptype_raw":"Technology licensing agreement", "deal_value_raw":null,            // (you)
  "date_announced":"2026-06-20",
  "description":"NIBE licensed Sig Sauer rifle production", "detected_lines":["small_arms"] } // (you)
  // L2 adds: kssl_relevance (CORE/ADJACENT/context), competitive meaning
```

### 5.4 `geo_footprint` → a competitor product present in a country
```jsonc
{ "document_id":"doc_6d88",
  "competitor_id":"KNDS", "country":"Nigeria", "product_name":"CAESAR 6x6", "product_id":"CAESAR6x6", // (you)
  "product_category":"artillery", "contract_value_raw":"$120M", "qty_raw":"18 units",  // (you)
  "since_year":"2026", "stage":"Contracted",        // (you) Offered|Trials|Contracted|Delivered
  "note":"Nigeria orders 18 CAESAR 6x6", "confidence":"high" }   // (you) high(signed)|medium(reported)|low(rumoured)
```

### 5.5 `innovation` → a technology development
```jsonc
{ "document_id":"doc_7e99",
  "tech_domain":"artillery", "title":"Rheinmetall demos ramjet 155mm at 70km",   // (you)
  "competitor_id":"RHEIN", "driver":"Rheinmetall / KNDS", "maturity_hint":"test", // (you) concept|dev|test|ioc|foc
  "horizon_hint":"2027-2028", "description":"Full article text..." }              // (you)
  // L2 adds: gap_vs_kssl, impact, whats_new, comp_note, recommended action
```

### 5.6 `company_event` → M&A / financial / leadership / contract / launch
```jsonc
{ "document_id":"doc_9f00",
  "competitor_id":"ADANI", "event_type":"acquisition",     // (you) acquisition|financial|leadership|contract_win|product_launch
  "headline":"Adani acquires General Aeronautics", "deal_value_raw":"₹200 cr",  // (you)
  "date_of_event":"2026-06-25", "description":"Full text...", "detected_lines":["uav"] }  // (you)
```

---

## 6. Entity resolution (mechanical, required)

For every record, match surface forms against `docs/seed/watchlist_entities.json` (aliases) and
`watchlist_products.json`. Populate `entities_detected[]` with `resolved_id` + `confidence`.
- Match found → set `competitor_id` / `product_id`.
- A defence company **not** in the seed, recurring across docs → report it as `resolved_id:null,
  type:"unknown_company"`. This is how we discover new competitors to add. (Don't drop it; flag it.)
- This is string/alias/embedding matching — **not** a judgment about importance.

---

## 7. How jobs are generated from the seed (our side — for your awareness)

You don't build this, but knowing it clarifies the contract. Our **job generator** reads
`docs/seed/*.json` and emits jobs:
- For each **competitor** × each **source** → a `news` job (seed_url = that source's search URL for the entity's aliases).
- For each **tender source** × **keyword set** → a `tender` job.
- For each **competitor IR page** → a `profile` job (partnerships, geo).
- For each **product** needing specs → a `spec` job.

So the seed flows: `watchlist_entities/products/tenders/tech_domains` → job generator → crawl jobs → you.
We hand you fully-formed jobs; you never read the seed strategy, only execute the job's URL+keywords+budget.

---

## 7A. Re-crawl, change detection & dedup — YOUR job vs L2's job

Continuous crawling means the same URLs and the same stories recur every day. Split the work cleanly so
you don't reprocess unchanged data and don't try to do L2's clustering:

**You DO (against your OWN crawl history — your Postgres `crawl_pages` remembers the last run):**
- **Conditional re-fetch.** Send `If-None-Match` / `If-Modified-Since`. A `304 Not Modified` → skip, don't even re-download. (Saves crawl cost on daily sweeps.)
- **Self-dedup by content_hash.** If a URL's `content_hash` equals what you stored last run → the page didn't change → **do not re-emit it.** This is exactly what stops "daily crawl, no change" from flooding L2.
- **Emit only when** a URL is new, OR its `content_hash` changed since your last crawl.

**You do NOT (that's L2 — it has the full corpus + embeddings):**
- **Don't merge different URLs that cover the same event** (5 outlets reporting one K9 order). Emit each with its own `content_hash` + `canonical_url`; L2 clusters them into one signal and elects a primary (service S-08).
- **Don't decide which duplicate is "the important one."**

> **The rule in one line:** the crawler dedups against *itself* (same URL, unchanged → idempotent re-crawl);
> L2 dedups across *sources* (different URLs, same event → semantic merge). Two problems, two layers.

**Pinpoint precision at volume:** you never search the open web blindly. Every job is pre-scoped to
**one source + one entity + its keywords**, and the relevance gate (pipeline stage 3) drops anything that
doesn't match. Precision comes from *scoping the job*, not from sifting a giant pile afterward. Large
volume is handled by many small scoped jobs + the gate, not by one big crawl.

---

## 8. TESTING PHASE — scope for the first build (NOT production)

> **Production cadence (P0/P1 every-6h schedules) does NOT apply during testing.** In testing we are only
> proving the engine works end-to-end on a fixed, tiny job set, run **once, manually.** No scheduling,
> no priorities, no full-universe sweep.

### 8.1 Testing job set (run once)
A fixed ~12-job batch, hand-made, to exercise every record type:

| # | job_type | target | proves |
|---|---|---|---|
| 1–3 | `news` | L&T, Adani, KNDS (one source each) | `competitive_signal` + `company_event` extraction |
| 4–5 | `tender` | MoD India + SAM.gov (one query each) | `tender` + PDF extraction + screenshot |
| 6–7 | `profile` | NIBE, Solar IR/press pages | `partnership` + `geo_footprint` |
| 8 | `spec` | one KNDS product page (CAESAR) | `spec` table + product image capture |
| 9–10 | `news` (market) | "Armenia artillery tender", "India defence budget" | `market` stream signals |
| 11–12 | `news` (tech) | "ramjet 155mm", "loitering munition" | `innovation` records |

### 8.2 Testing exit criteria (all must pass)
1. Every job runs within its `max_pages`/`max_depth` budget without crashing.
2. Each produces ≥1 valid `document` with non-empty `main_text` + canonical `url` + `content_hash`.
3. At least one of **every** record type (5.1–5.6) is emitted and **accepted** by a stub Ingest endpoint.
4. PDF extraction works on at least one real tender (the RFP text appears in `attachments[].extracted_text`).
5. Screenshot capture works (one full-page PNG per document, stored, path returned).
6. Entity resolution links ≥80% of obvious mentions to the right seed id.
7. Non-English handling: one non-English source returns both `main_text` and `main_text_en`.

Hit all 7 → the crawler is "pipeline-proven." **Only then** do we add scheduling/cadence/full-universe
sweeps (a later, production phase — out of scope for the first build).

---

## 9. Acceptance rules (the Ingest API enforces these)

A record is accepted only if:
1. It carries a valid `document` (non-empty `main_text`, canonical `url`, `content_hash`).
2. It resolves to ≥1 seed entity/product/tech-domain **or** is a tender matching a keyword.
3. Required **(you)** fields for its type are present (nulls only where allowed).
4. Dates parse as ISO (or are null with a `date_precision` flag).
Rejected → `422 { failing_rule }`. Track accept-rate per source; L2 uses it to tune source tiers.

## 10. Hard "do nots"
- ❌ Don't POST raw HTML / scripts / ad markup. Send extracted `main_text` + structured fields.
- ❌ Don't judge threat/score/relevance-to-strategy or write "so what" analysis. That's Layer 2.
- ❌ Don't merge different URLs about the same event — emit each with its `content_hash`; **L2 clusters** them (§7A). But DO skip re-emitting a URL unchanged since *your* last crawl (self-dedup, §7A).
- ❌ Don't fabricate. Unknown = `null`. Never guess a value, date, or quantity.
- ❌ Don't wander off the seed/registry (except following a link out from a registry page, within `max_depth`).
- ❌ Don't download large media. Record video/audio links as metadata only.
- ❌ Don't apply production cadence in the testing phase — run the fixed §8 set once.

---

## 11. Seed files (already populated — your real targets)

| File | Contents |
|---|---|
| `docs/seed/watchlist_entities.json` | 32 tracked competitors (KSSL anchor + globals + Indian rivals) with aliases, HQ, priority; partner nodes |
| `docs/seed/watchlist_products.json` | 27 KSSL products + tracked competitors' products, by category |
| `docs/seed/watchlist_tech_domains.json` | 8 tech domains + keyword sets |
| `docs/seed/watchlist_tenders.json` | tender keywords + 25 target countries + portal sources |
| `docs/seed/source_registry.json` | approved sources with trust tiers + global capture defaults |
