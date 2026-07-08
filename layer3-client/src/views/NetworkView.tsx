// Network view — the alliance knowledge graph (Mallory Intara) + hidden-pattern insights.
// d3-force computes positions only; React renders the SVG. Everything shown is precomputed
// server-side (communities, centrality, insights) — this view computes nothing but layout.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

import { api } from "../api/client";
import type { AllianceGraph, EgoGraph, GraphInsight } from "../api/types";
import { Rail } from "../components/Rail";

interface SimNode extends SimulationNodeDatum {
  id: string;
  kind: "competitor" | "org";
  label: string;
  community?: number | null;
  betweenness?: number;
  dir?: string | null;
  is_anchor?: boolean;
}

type SimLink = SimulationLinkDatum<SimNode> & { rel: string; provenance: string };

const W = 860;
const H = 560;
// community index → hue (kept muted to match the control-room theme)
const COMMUNITY_HUES = [210, 30, 140, 80, 270, 0];

function nodeColor(n: SimNode): string {
  if (n.is_anchor) return "var(--fav)";
  if (n.kind === "org") return "var(--d-txt-4, #65656a)";
  if (n.dir === "threat") return "var(--threat)";
  if (n.dir === "fav") return "var(--fav)";
  return "var(--watch)";
}

function communityStroke(n: SimNode): string {
  if (n.community == null) return "transparent";
  const hue = COMMUNITY_HUES[n.community % COMMUNITY_HUES.length];
  return `hsl(${hue} 45% 55%)`;
}

export function NetworkView() {
  const [graph, setGraph] = useState<AllianceGraph | null>(null);
  const [insights, setInsights] = useState<GraphInsight[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<SimNode | null>(null);
  const [egoData, setEgoData] = useState<EgoGraph | null>(null);
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(new Map());
  const simNodes = useRef<SimNode[]>([]);
  const simLinks = useRef<SimLink[]>([]);

  useEffect(() => {
    let live = true;
    Promise.all([api.allianceGraph(), api.graphInsights()])
      .then(([g, ins]) => {
        if (!live) return;
        setGraph(g);
        setInsights(ins);
        setLoading(false);
      })
      .catch(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  // Run the force layout once per graph payload; positions land in React state on each tick.
  useEffect(() => {
    if (!graph) return;
    const nodes: SimNode[] = graph.nodes.map((n) => ({ ...n }));
    const ids = new Set(nodes.map((n) => n.id));
    const links: SimLink[] = graph.edges
      .filter((e) => ids.has(e.src) && ids.has(e.dst))
      .map((e) => ({ source: e.src, target: e.dst, rel: e.rel, provenance: e.provenance }));
    simNodes.current = nodes;
    simLinks.current = links;

    const sim = forceSimulation(nodes)
      .force("link", forceLink<SimNode, SimLink>(links).id((d) => d.id).distance(90))
      .force("charge", forceManyBody().strength(-260))
      .force("center", forceCenter(W / 2, H / 2))
      .force("collide", forceCollide(26))
      .on("tick", () => {
        setPositions(new Map(nodes.map((n) => [n.id, { x: n.x ?? 0, y: n.y ?? 0 }])));
      });
    return () => { sim.stop(); };
  }, [graph]);

  const onSelect = (n: SimNode) => {
    setSelected(n);
    setEgoData(null);
    api.ego(n.id, 1).then(setEgoData).catch(() => undefined);
  };

  const neighborIds = useMemo(() => {
    if (!selected || !egoData) return new Set<string>();
    return new Set(egoData.nodes.map((n) => n.id));
  }, [selected, egoData]);

  const stats = graph?.stats ?? {};

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Network</h1>
          <span className="cnt">
            {stats.nodes ?? 0} entities · {stats.edges ?? 0} alliances mapped ·{" "}
            {(stats.blocs?.length ?? 0)} blocs · {insights.length} pattern insights
          </span>
        </div>
      </div>
      <div className="shell full">
        <Rail />
        <div className="feed" style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
          {loading ? (
            <div className="loading">Loading network…</div>
          ) : !graph || graph.nodes.length === 0 ? (
            <div className="empty">Graph not built yet — run the pipeline or POST /ops/rebuild-graph.</div>
          ) : (
            <>
              <div style={{ flex: "1 1 auto", minWidth: 0 }}>
                <svg
                  viewBox={`0 0 ${W} ${H}`}
                  style={{ width: "100%", height: "auto", background: "var(--d-bg-1, #1c1c1f)",
                           border: "1px solid var(--d-line, #323237)", borderRadius: 4 }}
                >
                  {simLinks.current.map((l, i) => {
                    const s = typeof l.source === "object" ? (l.source as SimNode).id : String(l.source);
                    const t = typeof l.target === "object" ? (l.target as SimNode).id : String(l.target);
                    const ps = positions.get(s);
                    const pt = positions.get(t);
                    if (!ps || !pt) return null;
                    const dim = selected && !(neighborIds.has(s) && neighborIds.has(t));
                    return (
                      <line
                        key={i} x1={ps.x} y1={ps.y} x2={pt.x} y2={pt.y}
                        stroke={l.provenance === "sourced" ? "#6b6a63" : "var(--synthetic, #c0610f)"}
                        strokeWidth={1}
                        strokeDasharray={l.provenance === "sourced" ? undefined : "4 3"}
                        opacity={dim ? 0.15 : 0.6}
                      />
                    );
                  })}
                  {simNodes.current.map((n) => {
                    const p = positions.get(n.id);
                    if (!p) return null;
                    const r = n.kind === "competitor" ? (n.is_anchor ? 13 : 10) : 6;
                    const dim = selected && !neighborIds.has(n.id) && selected.id !== n.id;
                    return (
                      <g key={n.id} transform={`translate(${p.x},${p.y})`}
                         style={{ cursor: "pointer" }} opacity={dim ? 0.25 : 1}
                         onClick={() => onSelect(n)}>
                        <circle r={r} fill={nodeColor(n)}
                                stroke={selected?.id === n.id ? "#fff" : communityStroke(n)}
                                strokeWidth={selected?.id === n.id ? 2 : 1.5} />
                        <text y={-r - 4} textAnchor="middle"
                              style={{ fontSize: 10, fill: "var(--d-txt-2, #bdbdbb)",
                                       fontFamily: "var(--mono, monospace)" }}>
                          {n.label}
                        </text>
                      </g>
                    );
                  })}
                </svg>

                <div style={{ marginTop: 14 }}>
                  <div className="feed-grp-h">Hidden patterns — link analysis</div>
                  {insights.length === 0 ? (
                    <div className="empty">No pattern insights yet.</div>
                  ) : (
                    insights.map((ins) => (
                      <div className="alert" key={ins.id} style={{ cursor: "default" }}>
                        <div className="gut">
                          <span className="sig" style={{ background: `var(--${ins.dir})` }} />
                        </div>
                        <div className="body">
                          <div className="ttl">{ins.title}</div>
                          <div className="meta">{ins.sowhat}</div>
                        </div>
                        <div className="aside">
                          <span className="dirtag watch">{ins.kind.replace(/_/g, " ")}</span>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>

              <div style={{ flex: "0 0 240px", borderLeft: "1px solid var(--d-line, #323237)",
                            paddingLeft: 14, minHeight: 200 }}>
                {!selected ? (
                  <div className="meta" style={{ paddingTop: 6 }}>
                    Click a node to inspect its connections. Ring color = alliance bloc;
                    dashed edge = estimated; solid = sourced.
                  </div>
                ) : (
                  <>
                    <div className="ttl" style={{ marginBottom: 4 }}>{selected.label}</div>
                    <div className="meta" style={{ marginBottom: 10 }}>
                      {selected.kind}
                      {selected.community != null ? ` · bloc ${selected.community}` : ""}
                      {selected.betweenness ? ` · broker ${selected.betweenness}` : ""}
                    </div>
                    {!egoData ? (
                      <div className="loading">Loading connections…</div>
                    ) : (
                      egoData.edges.map((e, i) => {
                        const otherId = e.src === selected.id ? e.dst : e.src;
                        const other = egoData.nodes.find((n) => n.id === otherId);
                        return (
                          <div key={i} className="meta" style={{ padding: "3px 0" }}>
                            <span style={{ color: "var(--d-txt-3, #908f8c)" }}>
                              {e.rel}{e.subtype ? `:${e.subtype}` : ""} →{" "}
                            </span>
                            {other?.label ?? otherId}
                          </div>
                        );
                      })
                    )}
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
