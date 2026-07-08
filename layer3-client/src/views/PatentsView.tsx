import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { PatentCard } from "../api/types";
import { Rail } from "../components/Rail";

export function PatentsView({ mode }: { mode: "competitor" | "tech" }) {
  const [patents, setPatents] = useState<PatentCard[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    api.patents().then((p) => {
      if (!live) return;
      setPatents(p);
      setLoading(false);
    }).catch(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  const groupKey = (p: PatentCard) =>
    (mode === "competitor" ? p.competitor_id : p.tech_domain_id) ?? "—";
  const groups: Record<string, PatentCard[]> = {};
  for (const p of patents) (groups[groupKey(p)] = groups[groupKey(p)] || []).push(p);
  const keys = Object.keys(groups).sort();
  const heading = mode === "competitor" ? "by competitor" : "by technology";

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Patents · {heading}</h1>
          <span className="cnt">{patents.length} filings · sample until the patent API is connected</span>
        </div>
      </div>
      <div className="banner">
        <i className="ti ti-info-circle" aria-hidden="true" /> Showing sample records. Live data will
        populate from the patent API (USPTO / EPO / Lens) + crawler once connected.
      </div>
      <div className="shell full">
        <Rail />
        <div className="feed">
          {loading ? (
            <div className="loading">Loading patents…</div>
          ) : keys.length === 0 ? (
            <div className="empty">No patents yet.</div>
          ) : (
            keys.map((k) => (
              <div key={k}>
                <div className="feed-grp-h">{k}</div>
                {groups[k].map((p) => (
                  <div className="alert" key={p.id} style={{ cursor: "default" }}>
                    <div className="gut"><span className="sig" style={{ background: "var(--synthetic)" }} /></div>
                    <div className="body">
                      <div className="ttl">{p.title}</div>
                      <div className="meta">{[p.assignee, p.jurisdiction, p.filed_date].filter(Boolean).join(" · ")}</div>
                      {p.abstract && <div className="sowhat">{p.abstract}</div>}
                    </div>
                    <div className="aside">
                      {p.status && <span className="dirtag watch">{p.status}</span>}
                      {p.kssl_relevance && <span className="srcbadge est">{p.kssl_relevance}</span>}
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
