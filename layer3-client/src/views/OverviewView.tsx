import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { MetricItem, Pillar, SignalCard, SignalDetail } from "../api/types";
import { AlertRow } from "../components/AlertRow";
import { Dossier } from "../components/Dossier";
import { MetricStrip } from "../components/MetricStrip";
import { Rail } from "../components/Rail";
import { useScope } from "../scope";

const TITLES: Record<Pillar, string> = {
  competitive: "Competitive intelligence",
  market: "Market intelligence",
  technology: "Technology intelligence",
};

const FILTERS: [string, string, string][] = [
  ["all", "All", ""],
  ["threat", "Threat", "var(--threat)"],
  ["watch", "Watch", "var(--watch)"],
  ["fav", "Favourable", "var(--fav)"],
];

export function OverviewView({ pillar }: { pillar: Pillar }) {
  const { setScope } = useScope();
  const [metrics, setMetrics] = useState<MetricItem[]>([]);
  const [signals, setSignals] = useState<SignalCard[]>([]);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<SignalDetail | null>(null);
  const [loadingFeed, setLoadingFeed] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);

  useEffect(() => {
    let live = true;
    api.metrics(pillar).then((m) => live && setMetrics(m.metrics)).catch(() => live && setMetrics([]));
    return () => { live = false; };
  }, [pillar]);

  useEffect(() => {
    let live = true;
    setLoadingFeed(true);
    api.signals(pillar, filter).then((p) => {
      if (!live) return;
      setSignals(p.items);
      setSelected(p.items[0]?.id ?? null);
      setLoadingFeed(false);
    }).catch(() => live && setLoadingFeed(false));
    return () => { live = false; };
  }, [pillar, filter]);

  useEffect(() => {
    if (selected == null) { setDetail(null); return; }
    let live = true;
    setLoadingDetail(true);
    api.signalDetail(selected).then((d) => {
      if (!live) return;
      setDetail(d);
      setLoadingDetail(false);
    }).catch(() => live && setLoadingDetail(false));
    return () => { live = false; };
  }, [selected]);

  useEffect(() => {
    setScope(
      selected != null
        ? { panel: "signal", entityId: String(selected), label: `${pillar} signal` }
        : { panel: "overview", entityId: null, label: "Overview" },
    );
  }, [selected, pillar, setScope]);

  const groupLabel = signals[0]?.rank_group ?? "Signals";

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>{TITLES[pillar]}</h1>
          <span className="cnt">{signals.length} signals · sorted by relevance</span>
        </div>
        <div className="filters">
          {FILTERS.map(([f, label, c]) => (
            <button key={f} className={"fbtn" + (filter === f ? " on" : "")} onClick={() => setFilter(f)}>
              {c && <span className="sw" style={{ background: c }} />}
              {label}
            </button>
          ))}
        </div>
      </div>

      <MetricStrip metrics={metrics} active={filter} onPick={setFilter} />

      <div className="shell">
        <Rail />
        <div className="feed">
          {loadingFeed ? (
            <div className="loading">Loading signals…</div>
          ) : signals.length === 0 ? (
            <div className="empty">No signals for this view. Run the pipeline (mock_feeder) to populate.</div>
          ) : (
            <>
              <div className="feed-grp-h">{groupLabel}</div>
              {signals.map((s) => (
                <AlertRow key={s.id} signal={s} selected={s.id === selected} onClick={() => setSelected(s.id)} />
              ))}
            </>
          )}
        </div>
        <Dossier detail={detail} loading={loadingDetail} />
      </div>
    </>
  );
}
