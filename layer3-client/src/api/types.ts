// Types mirror the Layer 2 serving DTOs (contracts/serving.py). The client renders these directly.

export type Pillar = "competitive" | "market" | "technology";
export type Direction = "threat" | "watch" | "fav";
export type Pair = [string, string];

export interface SignalCard {
  id: number;
  pillar: string;
  dir: Direction;
  rank: number;
  rank_group?: string | null;
  title: string;
  meta?: string | null;
  company?: string | null;
  lens?: string | null;
  sowhat?: string | null;
  tags?: string[] | null;
  ago_display?: string | null;
  source_url?: string | null;
  provenance: string;
  confidence?: number | null;
  confidence_band?: "high" | "medium" | "low" | null;
  corroboration?: number;
}

export interface SignalDetail {
  signal_id: number;
  rank_display?: string | null;
  dir?: Direction | null;
  title: string;
  facts?: Pair[] | null;
  what_text?: string | null;
  why_text?: string | null;
  lens_reads?: Pair[] | null;
  actions?: Pair[] | null;
  suggest?: string[] | null;
  source_url?: string | null;
}

export interface TenderMatch {
  kssl_product_id?: string | null;
  kssl_product_name: string;
  fit_level: "high" | "medium" | "low";
  fit_pct: number;
  match_lines?: Pair[] | null;
}

export interface TenderCard {
  id: number;
  title: string;
  issuer?: string | null;
  country?: string | null;
  category?: string | null;
  value_display?: string | null;
  qty?: string | null;
  deadline_date?: string | null;
  dl_days?: number | null;
  req_note?: string | null;
  requirements?: { label: string; value: string }[] | null;
  lean?: "go" | "maybe" | "pass" | null;
  lean_text?: string | null;
  status?: "open" | "closing" | "closed" | null;
  source_url?: string | null;
  provenance: string;
  matches: TenderMatch[];
}

export interface MetricItem {
  label: string;
  value: number;
  color: string;
  filter: string;
}

export interface OverviewMetrics {
  pillar: string;
  generated_at: string;
  metrics: MetricItem[];
}

export interface Page<T> {
  items: T[];
  page: number;
  size: number;
  total: number;
}

export interface Competitor {
  id: string;
  name: string;
  hq?: string | null;
  dir?: string | null;
  is_anchor: boolean;
}

export interface MatchupSpec {
  spec_label: string;
  comp_value?: string | null;
  kssl_value?: string | null;
  leader: "comp" | "kssl" | "tie";
}

export interface MatchupCard {
  id: number;
  category?: string | null;
  dir?: Direction | null;
  country?: string | null;
  comp_name: string;
  comp_by?: string | null;
  kssl_name: string;
  edge_score: number;
  adv_comp?: string[] | null;
  adv_kssl?: string[] | null;
  verdict?: string | null;
  specs: MatchupSpec[];
}

export interface GeoEntry {
  id: number;
  competitor_id?: string | null;
  competitor_name?: string | null;
  country?: string | null;
  product_name?: string | null;
  category?: string | null;
  contract_value?: string | null;
  since_year?: string | null;
  qty?: string | null;
  stage?: string | null;
  note?: string | null;
  provenance: string;
}

export interface PartnershipCard {
  id: number;
  competitor_id?: string | null;
  competitor_name?: string | null;
  partner_name: string;
  partner_kind?: string | null;
  rel_type?: string | null;
  country?: string | null;
  deal_value?: string | null;
  kssl_relevance?: string | null;
  meaning?: string | null;
  provenance: string;
}

export interface InnovationCard {
  id: number;
  tech_domain_id?: string | null;
  title: string;
  maturity?: string | null;
  gap_vs_kssl?: string | null;
  driver?: string | null;
  horizon?: string | null;
  body?: string | null;
  impact?: string | null;
  action?: string | null;
  provenance: string;
}

export interface PatentCard {
  id: string;
  competitor_id?: string | null;
  tech_domain_id?: string | null;
  jurisdiction?: string | null;
  title: string;
  status?: string | null;
  filed_date?: string | null;
  assignee?: string | null;
  abstract?: string | null;
  kssl_relevance?: string | null;
  provenance: string;
}

export interface CompetitorSynthesis {
  competitor_id: string;
  competitor_name?: string | null;
  thesis?: string | null;
  strat_sowhat?: string | null;
  vulnerabilities?: { title: string; intel: string }[] | null;
  predictions?: string[] | null;
  moves?: string[] | null;
  provenance: string;
}

export interface FieldPattern {
  id: number;
  title: string;
  summary?: string | null;
  exceptions?: string | null;
  ord: number;
}

export interface MalloryResponse {
  answer: string;
  scope: string;
  sources: string[];
}

export interface ReportSection {
  heading: string;
  body: string | { title: string; note?: string | null }[];
}

export interface ReportResponse {
  title: string;
  generated_at: string;
  sections: ReportSection[];
}

// ── Knowledge graph (Mallory Intara) ──

export interface AllianceNode {
  id: string;
  kind: "competitor" | "org";
  label: string;
  community?: number | null;
  degree?: number | null;
  betweenness?: number;
  dir?: Direction | null;
  is_anchor?: boolean;
}

export interface AllianceEdge {
  src: string;
  dst: string;
  rel: string;
  provenance: string;
}

export interface AllianceGraph {
  generated_at?: string | null;
  nodes: AllianceNode[];
  edges: AllianceEdge[];
  stats: { nodes?: number; edges?: number; insights?: number; blocs?: string[][] };
}

export interface GraphInsight {
  id: number;
  kind: string;
  dir: Direction;
  rank: number;
  title: string;
  sowhat?: string | null;
  entities?: string[] | null;
  metric: number;
  provenance: string;
}

export interface EgoGraph {
  center: string;
  nodes: { id: string; kind: string; label: string; attrs?: Record<string, unknown> | null }[];
  edges: { src: string; dst: string; rel: string; subtype?: string; provenance: string }[];
}

