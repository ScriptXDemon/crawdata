import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { GeoEntry } from "../api/types";
import { Rail } from "../components/Rail";

export function GeoView() {
  const [entries, setEntries] = useState<GeoEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    api.geo().then((e) => {
      if (!live) return;
      setEntries(e);
      setLoading(false);
    }).catch(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  const byCountry: Record<string, GeoEntry[]> = {};
  for (const e of entries) {
    const k = e.country ?? "—";
    (byCountry[k] = byCountry[k] || []).push(e);
  }
  const countries = Object.keys(byCountry).sort();

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Geo footprint</h1>
          <span className="cnt">{entries.length} placements · {countries.length} markets · competitor activity by country</span>
        </div>
      </div>
      <div className="shell full">
        <Rail />
        <div className="feed">
          {loading ? (
            <div className="loading">Loading geo footprint…</div>
          ) : countries.length === 0 ? (
            <div className="empty">No geo placements yet.</div>
          ) : (
            countries.map((c) => (
              <div key={c}>
                <div className="feed-grp-h">{c}</div>
                {byCountry[c].map((e) => (
                  <div className="alert" key={e.id} style={{ cursor: "default" }}>
                    <div className="gut">
                      <span className="sig" style={{ background: e.provenance === "sourced" ? "var(--fav)" : "var(--synthetic)" }} />
                    </div>
                    <div className="body">
                      <div className="ttl">{e.competitor_name} · {e.product_name}</div>
                      <div className="meta">
                        {[e.category, e.contract_value, e.qty, e.since_year].filter(Boolean).join(" · ")}
                        {e.note ? ` — ${e.note}` : ""}
                      </div>
                    </div>
                    <div className="aside">
                      {e.stage && <span className="dirtag watch">{e.stage}</span>}
                      <span className={"srcbadge" + (e.provenance === "sourced" ? "" : " est")}>
                        {e.provenance === "sourced" ? "src" : "est"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
