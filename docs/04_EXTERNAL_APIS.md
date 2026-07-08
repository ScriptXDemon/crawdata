# External APIs — Purchase & Test Guide

> ## 🛑 STATUS: PURCHASING ON HOLD
> **Decision (2026-06-29): buy nothing — free or paid — until all three layers are built.**
> The APIs below have no consumer yet: there is no Layer 2 to write `ext_*` into and no Layer 3 to show
> the result. Validating data quality only means something once the pipeline that *uses* it exists.
> This doc is the **ready-to-execute plan** for when we resume — keys to get, tests to run, buy order —
> not a to-do for today. Revisit after the L1→L2→L3 skeleton is running end-to-end on test data.

---

> **Audience: You (buyer), when we resume.** These feed Layer 2's `ext_*` tables (S-02 patents,
> S-03 tenders, S-04 FX) and back up the crawler. Goal: spend ₹0 first, validate the data is what we
> want, then pay only where the free tier proves the value. Each entry has **what it feeds**, a
> **copy-paste test**, and **the pass/fail criterion** so you know if the data is good enough before paying.

Legend: 🟢 free to start · 🟡 paid but cheap to test · 🔴 enterprise (defer)

---

## PHASE 1 — Validate for ₹0 (start here, this week)

### 🟢 USPTO PatentsView  → feeds `ext_patents` (S-02)
- **What:** all US patents by assignee, IPC, date. Free, no key for basic.
- **Test:**
  ```
  GET https://search.patentsview.org/api/v1/patent/?q={"_and":[{"_text_any":{"assignees.assignee_organization":"Rheinmetall"}},{"_gte":{"patent_date":"2022-01-01"}}]}&f=["patent_id","patent_title","patent_date","ipc_at_issue"]
  ```
- **Pass if:** you get records with `patent_title` + IPC codes for Rheinmetall/BAE/Elbit.
- **Maps to:** `ext_patents.assignee_raw`, `.ipc_codes`, `.granted_date`.

### 🟢 EPO Open Patent Services (OPS)  → `ext_patents` (S-02)
- **What:** EU + PCT/WO patents. Free tier 4 GB/week (needs a free OAuth key).
- **Test:** OAuth, then search `applicant="KNDS" and ipc=F41` via the OPS published-data endpoint.
- **Pass if:** EU/PCT filings for KNDS/Rheinmetall return with IPC F41/F42.
- **Why both:** USPTO covers US; EPO covers Europe + international. Together ≈ global minus India/China.

### 🟢 Lens.org  → `ext_patents` (S-02), **best for Indian competitors**
- **What:** US+EU+IN+CN+AU patents in one place. Free for non-commercial; request a key.
- **Test:** `applicant:"Bharat Forge" AND ipc:F41` and `applicant:"Solar Industries"`.
- **Pass if:** **Indian** filings appear. If yes, you may NOT need a custom Indian Patent Office scraper.
- **Decision gate:** Lens covers India well → skip the IPO scraper (Phase 3). It doesn't → build it.

### 🟢 SAM.gov API  → `stg_tenders` via S-03
- **What:** all US federal solicitations/contracts. Free with a key.
- **Test:** `GET https://api.sam.gov/opportunities/v2/search?q=howitzer&postedFrom=01/01/2026&postedTo=06/29/2026&api_key=KEY`
- **Pass if:** structured opportunities with title, deadline, NAICS, value.
- **Maps to:** `stg_tenders.title/issuer/deadline_date/value_*`.

### 🟢 Open Exchange Rates (or Frankfurter.app, fully free)  → `ext_fx_rates` (S-04)
- **What:** currency conversion for tender value normalization.
- **Test (Frankfurter, no key):** `GET https://api.frankfurter.app/latest?from=USD&to=INR`
- **Pass if:** INR/EUR/GBP cross-rates return. (Frankfurter = ₹0 forever; Open Exchange Rates if you want hourly.)
- **Maps to:** `ext_fx_rates`, used by S-12 Tender Normalizer.

### 🟢 Tavily Search API  → crawler enrichment / discovery (helps L1)
- **What:** real-time web search returning clean, structured results — good seed for the crawler.
- **Free:** 1,000 searches/month.
- **Test:** search `"Adani Defence" partnership 2026` and `"L&T Defence" contract win 2026`.
- **Pass if:** results include defence-trade sources (Janes, IDRW, PIB), not just generic news.

### 🟢 GDELT 2.0  → geo + signal discovery (free, bulk)
- **What:** global news event stream with entity/location extraction. Free.
- **Test:** GDELT DOC API query for `"CAESAR howitzer" sourcecountry:Nigeria`.
- **Pass if:** it surfaces arms-deal/geo events by country (feeds `stg_geo` discovery).

**Phase 1 exit criteria:** patents return for ≥3 competitors incl. one Indian (via Lens); SAM.gov returns
artillery tenders; FX works; Tavily/GDELT surface real defence sources. **Total spend: ₹0.**

---

## PHASE 2 — Pay only what Phase 1 proved you need (~$500/mo)

### 🟡 NewsAPI.ai / NewsAPI.org Business  → primary signal source (`stg_signals`)
- **What:** 150k+ sources, filter by domain (janes.com, defensenews.com, idrw.org) + keyword.
- **Cost:** ~$449/mo Business (commercial use + full archive).
- **Test (free dev key first):** `everything?q="Kalyani Strategic" OR "KSSL"&domains=janes.com,defensenews.com`
- **Pass if:** coverage + recency are good for ALL 19 competitors, not just the big 3.
- **Note:** this is the workhorse behind competitive/market/tech signals — worth paying if Phase-1 free
  search (Tavily/GDELT) shows gaps in trade-press coverage.

### 🟡 Perplexity Sonar API  → crawler enrichment + Mallory "what's new"
- **What:** real-time answers with citations. Cheap, pay-per-use.
- **Cost:** ~$5 / 1k requests.
- **Test:** `"What defence contracts has L&T won in 2026?"` → check citations are defence sources.
- **Pass if:** citations are usable as crawler seed URLs.

### 🟡 Bing/Brave Search API  → fallback web search if NewsAPI misses sources
- **Cost:** Brave ~$3–5/1k; Azure Bing ~$7/1k (1k/mo free tier on Brave).
- **Test:** same queries as Tavily; compare coverage of Indian defence blogs.
- **Decision gate:** only buy if NewsAPI + Tavily leave coverage gaps.

**Phase 2 exit criteria:** all 19 competitors have fresh, multi-source signal coverage. ~$500/mo ceiling.

---

## PHASE 3 — Evaluate after the pipeline runs (defer)

| API | Feeds | Why defer | Cost |
|---|---|---|---|
| 🟡 Crunchbase API | `stg_company_events` (M&A, funding) | Only if acquisition-tracking (Adani/NIBE) needs structured deal data | ~$49/mo |
| 🟡 Indian Patent Office scraper | `ext_patents` (IN) | Build **only if** Lens.org's India coverage failed the Phase-1 gate | build cost |
| 🟡 GePNIC / MoD India scraper | `stg_tenders` (IN) | No official API; **highest-value tender source** but must be scraped (crawler team) | build cost |
| 🟡 OCCAR / NATO NSPA scrapers | `stg_tenders` (EU) | Lower priority than India tenders | build cost |
| 🔴 Janes Intelligence | competitor specs, OOB, contracts | Authoritative but expensive; would make `ref_product_specs` rock-solid | $15k–$50k/yr |
| 🔴 SIPRI Arms Transfers DB | `srv_geo_entries` baseline | Free but annual release — use as a **one-time seed import**, not a live API | free |

---

## Mapping back to Layer 2 (so the buy maps to a service)

| API | Layer-2 service | Writes to |
|---|---|---|
| USPTO / EPO / Lens | **S-02** Patent Sync | `ext_patents` |
| SAM.gov / GePNIC / OCCAR | **S-03** Tender Sync | `stg_tenders` |
| Frankfurter / Open Exchange Rates | **S-04** FX Sync | `ext_fx_rates` |
| NewsAPI / Tavily / Perplexity / GDELT | crawler seed (**L1**) → **S-01** | `stg_documents`, `stg_signals` |
| Crunchbase | crawler/sync → **S-01** | `stg_company_events` |

---

## Recommended buy order (one line)

1. **This week, ₹0:** USPTO + EPO + Lens + SAM.gov + Frankfurter + Tavily + GDELT — validate data quality.
2. **If Phase 1 passes, ~$500/mo:** NewsAPI Business + Perplexity Sonar — turn on full competitor coverage.
3. **Later, as needed:** Crunchbase, the India tender/patent scrapers (crawler team), SIPRI one-time seed.
4. **Skip for now:** Janes (revisit only if spec accuracy becomes the bottleneck).

> Gate every paid step on its free test passing first. The Phase-1 set is enough to prove the whole
> `ext_*` half of Layer 2 works before you spend a rupee.
