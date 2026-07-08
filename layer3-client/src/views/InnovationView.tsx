import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { InnovationCard } from "../api/types";
import { Rail } from "../components/Rail";

const DOMAINS: [string, string][] = [
  ["all", "All"],
  ["artillery", "Artillery"],
  ["armoured", "Armoured"],
  ["ammunition", "Ammunition"],
  ["missiles_ad", "Missiles & AD"],
  ["uav", "UAV"],
  ["naval", "Naval"],
  ["small_arms", "Small arms"],
];

function gapColor(g?: string | null): string {
  return g === "ahead" ? "var(--fav)" : g === "behind" ? "var(--threat)" : "var(--watch)";
}

export function InnovationView() {
  const [domain, setDomain] = useState("all");
  const [items, setItems] = useState<InnovationCard[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api.innovation(domain === "all" ? undefined : domain).then((d) => {
      if (!live) return;
      setItems(d);
      setLoading(false);
    }).catch(() => live && setLoading(false));
    return () => { live = false; };
  }, [domain]);

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Innovation pipeline</h1>
          <span className="cnt">{items.length} developments tracked across KSSL technology domains</span>
        </div>
        <div className="filters">
          {DOMAINS.map(([f, l]) => (
            <button key={f} className={"fbtn" + (domain === f ? " on" : "")} onClick={() => setDomain(f)}>{l}</button>
          ))}
        </div>
      </div>
      <div className="shell full">
        <Rail />
        <div className="feed">
          {loading ? (
            <div className="loading">Loading innovation pipeline…</div>
          ) : items.length === 0 ? (
            <div className="empty">No developments for this domain.</div>
          ) : (
            items.map((i) => (
              <div className="innov" key={i.id}>
                <div className="innov-head">
                  <div>
                    <div className="ttl">{i.title}</div>
                    <div className="meta">{[i.tech_domain_id, i.driver, i.horizon].filter(Boolean).join(" · ")}</div>
                  </div>
                  <div className="innov-tags">
                    {i.maturity && <span className="dirtag watch">{i.maturity}</span>}
                    {i.gap_vs_kssl && <span className="gaptag" style={{ color: gapColor(i.gap_vs_kssl), borderColor: gapColor(i.gap_vs_kssl) }}>{i.gap_vs_kssl}</span>}
                  </div>
                </div>
                {i.body && <div className="innov-body">{i.body}</div>}
                {i.impact && <div className="innov-line"><span className="il-k">Impact</span> {i.impact}</div>}
                {i.action && <div className="innov-line"><span className="il-k">Action</span> {i.action}</div>}
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
