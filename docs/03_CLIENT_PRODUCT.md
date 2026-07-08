# LAYER 3 — Client Product

> **Audience: Frontend team.** This layer renders and nothing else. It reads `srv_*` through the
> read-only Serving API and draws the 8 views of the prototype. **No scores are computed here, no LLMs
> are called here, no staging/reference tables are visible here.** If you need a number, it is already a
> column. If it isn't, that's a gap in L2 — file it, don't compute it in the browser.

---

## 1. The one rule

```
L3 may ONLY:  GET  srv_* via /api/v1/...        (read, filter, paginate, render)
L3 may ALSO:  POST to exactly 2 endpoints       (Mallory chat, CEO report) — thin proxies into L2
L3 may NEVER: touch stg_* / ref_* / ext_* ; compute edge/fit/rank ; call an LLM directly
```

Why: the CEO must never see raw or unverified data, the browser must stay light, and "vs KSSL"
judgments must be consistent across every user — which only holds if they're computed once, in L2.

---

## 2. View → serving-table map

Each prototype view maps to one or two read endpoints. The client does `fetch → render`.

| View (prototype) | Endpoint | Reads | Client does |
|---|---|---|---|
| Competitive / Market / Tech Overview | `GET /api/v1/signals?pillar=…&filter=…` | `srv_signals` | list cards by `rank` |
| — metric strip | `GET /api/v1/overview/:pillar/metrics` | `srv_overview_metrics` | render chips |
| — signal detail panel | `GET /api/v1/signals/:id/detail` | `srv_signal_details` | render right panel |
| Positioning (Matchup dossier) | `GET /api/v1/matchups?category=…` , `:id` | `srv_matchups`, `srv_matchup_specs` | spec table + edge bar |
| Partnerships (SVG graph) | `GET /api/v1/partnerships?competitor=…` | `srv_partnerships` | draw nodes/edges |
| Geo Footprint (map) | `GET /api/v1/geo?competitor=…&country=…` | `srv_geo_entries` | color countries |
| Tender Pipeline | `GET /api/v1/tenders?filter=…` , `:id` | `srv_tenders`, `srv_tender_matches` | list + fit bars |
| Innovation Pipeline | `GET /api/v1/innovation?domain=…` | `srv_innovation` | timeline by maturity |
| Patents · by Competitor | `GET /api/v1/patents?competitor=…` | `srv_patents` | patent cards |
| Patents · by Technology | `GET /api/v1/patents/tech/:domain` | `srv_patents`, `srv_patent_analytics`, `srv_patent_whitespace` | crowding + whitespace |
| Competitor profile | `GET /api/v1/competitors/:id/synthesis` | `srv_competitor_synthesis`, `srv_competitor_vulnerabilities` | thesis + vulns |
| Field patterns | `GET /api/v1/field-patterns` | `srv_field_patterns` | pattern list |

---

## 3. The Serving API (Interface B)

Read-only REST. All responses are already scored, ranked, paginated, and "vs KSSL". The client passes
filters; the server never recomputes — filters map to `WHERE`/`ORDER BY` on pre-built columns.

```http
GET /api/v1/signals?pillar=competitive&filter=threat&company=LT&page=1&size=20
→ 200 { items:[ srv_signals rows… ], page, total }
   # server: SELECT * FROM srv_signals WHERE pillar=? [AND dir=?] [AND company=?] ORDER BY rank LIMIT…

GET /api/v1/signals/:id/detail            → srv_signal_details row
GET /api/v1/overview/:pillar/metrics      → srv_overview_metrics row
GET /api/v1/tenders?filter=go&category=artillery&sort=deadline  → srv_tenders (+ embedded matches)
GET /api/v1/tenders/:id                    → srv_tenders + srv_tender_matches + match_lines
GET /api/v1/matchups?category=artillery&dir=threat  → srv_matchups
GET /api/v1/matchups/:id                    → srv_matchups + srv_matchup_specs
GET /api/v1/partnerships?competitor=NIBE    → center + srv_partnerships nodes
GET /api/v1/geo?competitor=LT&country=India → srv_geo_entries
GET /api/v1/patents?competitor=ADANI        → srv_patents
GET /api/v1/patents/tech/:domain            → srv_patents + analytics + whitespace
GET /api/v1/innovation?domain=artillery     → srv_innovation
GET /api/v1/competitors/:id/synthesis       → srv_competitor_synthesis + vulnerabilities
GET /api/v1/field-patterns                  → srv_field_patterns
```

### The only two writes (thin proxies — client still computes nothing)

```http
POST /api/v1/mallory/chat
  body { message, panel_context:"signal|tender|matchup|overview|ceo", entity_id }
  → proxies to L2 S-26; streams answer. Client renders the stream.

POST /api/v1/reports/ceo
  body { date_range, focus_areas[] }
  → proxies to L2 S-25; returns the executive brief. Client renders HTML.
```

Optionally, the client may persist **its own UI state** (saved filters, last-viewed) in a small client-side
store. That is the *only* data L3 owns — and it never flows back into L2's intelligence tables.

---

## 4. Rendering rules that keep the client honest

- **Color = `dir`/`lean`/`gap` columns.** threat=red, watch=amber, fav=green come straight from the column.
  Don't infer color from text.
- **Order = `rank` column.** Never re-sort by your own heuristic; L2 already ranked it.
- **Provenance badge = `provenance` column.** `estimate` rows get the dashed/"synthetic" styling the
  prototype used for `syn`; `sourced` rows show the source link. This is how the CEO knows what's verified.
- **Empty states are real.** If `srv_*` returns nothing (e.g. patents `awaiting_api`), show the prototype's
  "live data will populate once connected" state — don't fabricate sample rows in the client.
- **Liveness is honest.** The prototype's animated dots were cosmetic. Here, drive a "last updated" stamp
  from `srv_*` `updated_at`/`generated_at` so "live" reflects real freshness (fed by L2 S-29).

---

## 5. What moved from prototype to real

| Prototype (hardcoded JS) | Real L3 source |
|---|---|
| `competitiveCards`, `marketCards`, `techCards` arrays | `GET /signals?pillar=…` |
| `details`/`marketDetails`/`techDetails` objects | `GET /signals/:id/detail` |
| `matchups` object | `GET /matchups` |
| `tenders` array | `GET /tenders` |
| `geoData` nested object | `GET /geo` |
| `competitors` + `TRACEIDS` partnership graph | `GET /partnerships` |
| `COMPSYN` / `FIELDSYN` | `GET /competitors/:id/synthesis`, `GET /field-patterns` |
| `PATENTS` (awaiting_api) | `GET /patents…` |
| `overviewConfig` metrics | `GET /overview/:pillar/metrics` |
| Mallory pattern-matched strings | `POST /mallory/chat` (real LLM via L2) |
| Report generator (string templates) | `POST /reports/ceo` (real LLM via L2) |

The UI/UX stays the prototype's; only the data source changes from in-file constants to the Serving API.
