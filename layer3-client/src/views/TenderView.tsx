import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { TenderCard } from "../api/types";
import { Rail } from "../components/Rail";
import { TenderRow } from "../components/TenderRow";

const FILTERS: [string, string][] = [
  ["all", "All"],
  ["go", "Go"],
  ["maybe", "Maybe"],
  ["pass", "Pass"],
  ["closing", "Closing"],
];

export function TenderView() {
  const [filter, setFilter] = useState("all");
  const [tenders, setTenders] = useState<TenderCard[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .tenders(filter)
      .then((t) => {
        if (!live) return;
        setTenders(t);
        setLoading(false);
      })
      .catch(() => live && setLoading(false));
    return () => { live = false; };
  }, [filter]);

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Tender pipeline</h1>
          <span className="cnt">{tenders.length} live · auto-scored vs KSSL</span>
        </div>
        <div className="filters">
          {FILTERS.map(([f, label]) => (
            <button key={f} className={"fbtn" + (filter === f ? " on" : "")} onClick={() => setFilter(f)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="shell full">
        <Rail />
        <div className="tender-list">
          {loading ? (
            <div className="loading">Loading tenders…</div>
          ) : tenders.length === 0 ? (
            <div className="empty">No tenders for this filter.</div>
          ) : (
            tenders.map((t) => <TenderRow key={t.id} t={t} />)
          )}
        </div>
      </div>
    </>
  );
}
