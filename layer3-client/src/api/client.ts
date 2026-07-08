// Thin read-only client for the Layer 2 Serving API. No business logic here — every value the UI
// shows is already computed server-side. Base URL is empty in dev (Vite proxies /api) and set via
// VITE_API_BASE_URL in production.

import type {
  AllianceGraph,
  Competitor,
  CompetitorSynthesis,
  EgoGraph,
  FieldPattern,
  GeoEntry,
  GraphInsight,
  InnovationCard,
  MalloryResponse,
  MatchupCard,
  OverviewMetrics,
  Page,
  PartnershipCard,
  PatentCard,
  Pillar,
  ReportResponse,
  SignalCard,
  SignalDetail,
  TenderCard,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
  return res.json() as Promise<T>;
}

export const api = {
  signals: (pillar: Pillar, filter = "all", company?: string) => {
    const q = new URLSearchParams({ pillar, filter, size: "50" });
    if (company) q.set("company", company);
    return get<Page<SignalCard>>(`/api/v1/signals?${q.toString()}`);
  },

  signalDetail: (id: number) => get<SignalDetail>(`/api/v1/signals/${id}/detail`),

  // The metric strip may not exist for an empty pillar — callers treat a throw as "no data".
  metrics: (pillar: Pillar) => get<OverviewMetrics>(`/api/v1/overview/${pillar}/metrics`),

  tenders: (filter = "all", category?: string) => {
    const q = new URLSearchParams({ filter });
    if (category) q.set("category", category);
    return get<TenderCard[]>(`/api/v1/tenders?${q.toString()}`);
  },

  competitors: () => get<Competitor[]>(`/api/v1/competitors`),

  navCounts: () => get<Record<string, number>>(`/api/v1/nav/counts`),

  matchups: (category?: string) =>
    get<MatchupCard[]>(`/api/v1/matchups${category ? `?category=${category}` : ""}`),

  geo: (competitor?: string, country?: string) => {
    const q = new URLSearchParams();
    if (competitor) q.set("competitor", competitor);
    if (country) q.set("country", country);
    const qs = q.toString();
    return get<GeoEntry[]>(`/api/v1/geo${qs ? `?${qs}` : ""}`);
  },

  partnerships: (competitor?: string) =>
    get<PartnershipCard[]>(`/api/v1/partnerships${competitor ? `?competitor=${competitor}` : ""}`),

  innovation: (domain?: string) =>
    get<InnovationCard[]>(`/api/v1/innovation${domain ? `?domain=${domain}` : ""}`),

  patents: (competitor?: string, domain?: string) => {
    const q = new URLSearchParams();
    if (competitor) q.set("competitor", competitor);
    if (domain) q.set("domain", domain);
    const qs = q.toString();
    return get<PatentCard[]>(`/api/v1/patents${qs ? `?${qs}` : ""}`);
  },

  synthesis: (competitorId: string) =>
    get<CompetitorSynthesis>(`/api/v1/competitors/${competitorId}/synthesis`),

  fieldPatterns: () => get<FieldPattern[]>(`/api/v1/field-patterns`),

  allianceGraph: () => get<AllianceGraph>(`/api/v1/graph/alliances`),

  graphInsights: (kind?: string) =>
    get<GraphInsight[]>(`/api/v1/graph/insights${kind ? `?kind=${kind}` : ""}`),

  ego: (node: string, depth = 1) =>
    get<EgoGraph>(`/api/v1/graph/ego?node=${encodeURIComponent(node)}&depth=${depth}`),

  mallory: (message: string, panel_context: string, entity_id?: string | null) =>
    post<MalloryResponse>(`/api/v1/mallory/chat`, { message, panel_context, entity_id }),

  ceoReport: (focus?: string) => post<ReportResponse>(`/api/v1/reports/ceo`, { focus }),
};
