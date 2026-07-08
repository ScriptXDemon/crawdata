# Mallory — Layer 3 (Client)

The presentation layer. Renders the 8-view competitive-intelligence product for KSSL leadership.
It **reads the Layer 2 Serving API and computes nothing** — every score, rank, color, fit % and
verdict is already a field in the response. No business logic, no LLM calls, no database access.

Deploys independently of the backend (static build → any CDN / nginx).

## Quickstart

```bash
npm install
npm run dev          # http://localhost:5173  (Vite proxies /api and /ops to Layer 2 on :8000)
```

Layer 2 must be running (see `../layer2-data-engine`). With both up, run the backend's
`mock_feeder` once to populate data, then refresh the client.

```bash
npm run build        # type-check + production build → dist/
npm run preview      # serve the production build locally
```

## Configuration

| Env | Meaning |
|---|---|
| `VITE_API_BASE_URL` | Layer 2 host. Empty in dev (Vite proxy handles it); set to the deployed engine URL in prod. |

## What's implemented

| View | Serving endpoints | Status |
|---|---|---|
| Competitive / Market / Technology overview | `/signals`, `/signals/{id}/detail`, `/overview/{pillar}/metrics` | ✅ feed + metrics + dossier |
| Positioning | `/matchups` | ✅ edge bars + spec-comparison dossier + verdict |
| Tender pipeline | `/tenders` | ✅ list + KSSL fit matches + go/maybe/pass |
| Partnerships | `/partnerships`, `/competitors/{id}/synthesis` | ✅ relevance-tagged + synthesis dossier |
| Geo footprint | `/geo` | ✅ by-country matrix (map layer is a future enhancement) |
| Innovation | `/innovation` | ✅ pipeline by domain + gap-vs-KSSL |
| Patents | `/patents` | ✅ by technology (sample until patent API connected) |
| Mallory chat | `POST /mallory/chat` | ✅ scoped, grounded dock |
| CEO brief | `POST /reports/ceo` | ✅ cross-pillar synthesis modal |

All read-only and compute-nothing. Mallory and the CEO brief are thin proxies into Layer 2.

## Structure

```
src/
  api/
    types.ts        types mirroring the Layer 2 serving DTOs
    client.ts       thin read-only fetch client (base URL from env)
  components/       Sidebar · MetricStrip · SignalCardItem · SignalDetailPanel · TenderCardItem
  views/            OverviewView · TenderView
  App.tsx           shell + navigation
  index.css         dark "control-room" theme (threat/watch/fav tokens match L2 metric colors)
```

## The one rule

This app only ever calls `GET /api/v1/...`. If the UI needs a value that isn't in the response,
that's a gap to fix in Layer 2 — never computed here.
