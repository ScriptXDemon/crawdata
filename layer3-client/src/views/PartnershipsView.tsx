import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { Competitor, CompetitorSynthesis, PartnershipCard } from "../api/types";
import { Rail } from "../components/Rail";
import { useScope } from "../scope";

function relColor(r?: string | null): string {
  return r === "CORE" ? "var(--threat)" : r === "ADJACENT" ? "var(--watch)" : "var(--d-txt-4)";
}
function relClass(r?: string | null): string {
  return r === "CORE" ? "threat" : r === "ADJACENT" ? "watch" : "ctx";
}

function SynthDossier({ syn, sel }: { syn: CompetitorSynthesis | null; sel: string | null }) {
  if (!sel) return <div className="ctx"><div className="ctx-empty">Select a competitor for its strategic synthesis — thesis, vulnerabilities and recommended KSSL moves.</div></div>;
  if (!syn) return <div className="ctx"><div className="ctx-empty">No synthesis on file for this competitor yet.</div></div>;
  return (
    <div className="ctx">
      <div className="ctx-h dir-threat">
        <span className="eyebrow">Competitor synthesis</span>
        <div className="ct">{syn.competitor_name}</div>
      </div>
      {syn.thesis && <div className="ctx-sec"><span className="eyebrow">Thesis</span><div className="cd-prose">{syn.thesis}</div></div>}
      {syn.strat_sowhat && <div className="ctx-sec"><span className="eyebrow">So what · vs KSSL</span><div className="cd-prose">{syn.strat_sowhat}</div></div>}
      {syn.vulnerabilities && syn.vulnerabilities.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Vulnerabilities</span>
          {syn.vulnerabilities.map((v, i) => (
            <div className="lens-block" key={i}>
              <span className="lens-lead">{v.title}</span>
              <div className="lens-body">{v.intel}</div>
            </div>
          ))}
        </div>
      )}
      {syn.moves && syn.moves.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Recommended KSSL moves</span>
          {syn.moves.map((m, i) => <button className="cta" key={i}>{m}</button>)}
        </div>
      )}
    </div>
  );
}

export function PartnershipsView() {
  const { setScope } = useScope();
  const [comps, setComps] = useState<Competitor[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [parts, setParts] = useState<PartnershipCard[]>([]);
  const [syn, setSyn] = useState<CompetitorSynthesis | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.competitors().then((c) => setComps(c.filter((x) => !x.is_anchor))).catch(() => undefined);
  }, []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api.partnerships(sel ?? undefined).then((p) => {
      if (!live) return;
      setParts(p);
      setLoading(false);
    }).catch(() => live && setLoading(false));
    return () => { live = false; };
  }, [sel]);

  useEffect(() => {
    if (!sel) {
      setSyn(null);
      setScope({ panel: "overview", entityId: null, label: "Partnerships" });
      return;
    }
    setScope({ panel: "competitor", entityId: sel, label: `Partnerships · ${sel}` });
    let live = true;
    api.synthesis(sel).then((s) => live && setSyn(s)).catch(() => live && setSyn(null));
    return () => { live = false; };
  }, [sel, setScope]);

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Partnerships</h1>
          <span className="cnt">{parts.length} alliances · tagged by KSSL-line relevance</span>
        </div>
        <div className="filters">
          <select className="seq-select" value={sel ?? ""} onChange={(e) => setSel(e.target.value || null)}>
            <option value="">All competitors</option>
            {comps.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="shell">
        <Rail />
        <div className="feed">
          {loading ? (
            <div className="loading">Loading partnerships…</div>
          ) : parts.length === 0 ? (
            <div className="empty">No partnerships for this competitor.</div>
          ) : (
            parts.map((p) => (
              <div className="alert" key={p.id} style={{ cursor: "default" }}>
                <div className="gut"><span className="sig" style={{ background: relColor(p.kssl_relevance) }} /></div>
                <div className="body">
                  <div className="ttl">{p.competitor_name} <span className="vs">+</span> {p.partner_name}</div>
                  <div className="meta">{[p.rel_type, p.partner_kind, p.country, p.deal_value].filter(Boolean).join(" · ")}</div>
                  {p.meaning && <div className="sowhat" dangerouslySetInnerHTML={{ __html: p.meaning }} />}
                </div>
                <div className="aside"><span className={`dirtag ${relClass(p.kssl_relevance)}`}>{p.kssl_relevance}</span></div>
              </div>
            ))
          )}
        </div>
        <SynthDossier syn={syn} sel={sel} />
      </div>
    </>
  );
}
