import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type { MatchupCard } from "../api/types";
import { Rail } from "../components/Rail";
import { useScope } from "../scope";

function edgeColor(e: number): string {
  return e >= 60 ? "var(--fav)" : e < 40 ? "var(--threat)" : "var(--watch)";
}

function uniq(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v))].sort();
}

function MatchupDossier({ m }: { m: MatchupCard | null }) {
  if (!m) return <div className="ctx"><div className="ctx-empty">Select a matchup for the spec-by-spec read.</div></div>;
  return (
    <div className="ctx">
      <div className={`ctx-h dir-${m.dir ?? "watch"}`}>
        <span className="eyebrow">Positioning · {m.category}</span>
        <div className="ct">{m.kssl_name} vs {m.comp_name}</div>
      </div>
      <div className="ctx-sec">
        <span className="eyebrow">Spec comparison</span>
        <div className="spec-head"><span>{m.comp_name}</span><span></span><span>{m.kssl_name}</span></div>
        {m.specs.map((s, i) => (
          <div className="spec-row" key={i}>
            <span className={"spec-v" + (s.leader === "comp" ? " lead" : "")}>{s.comp_value ?? "—"}</span>
            <span className="spec-k">{s.spec_label}</span>
            <span className={"spec-v r" + (s.leader === "kssl" ? " lead" : "")}>{s.kssl_value ?? "—"}</span>
          </div>
        ))}
      </div>
      {m.adv_kssl && m.adv_kssl.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">KSSL advantages</span>
          {m.adv_kssl.map((a, i) => <div className="adv kssl" key={i}>{a}</div>)}
        </div>
      )}
      {m.adv_comp && m.adv_comp.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">{m.comp_name} advantages</span>
          {m.adv_comp.map((a, i) => <div className="adv comp" key={i}>{a}</div>)}
        </div>
      )}
      {m.verdict && (
        <div className="ctx-sec">
          <span className="eyebrow">Verdict</span>
          <div className="cd-prose">{m.verdict}</div>
        </div>
      )}
    </div>
  );
}

export function PositioningView() {
  const { setScope } = useScope();
  const [all, setAll] = useState<MatchupCard[]>([]);
  const [sel, setSel] = useState<MatchupCard | null>(null);
  const [loading, setLoading] = useState(true);

  // Filters — matching the demo: search + company + category + KSSL product + country.
  const [q, setQ] = useState("");
  const [company, setCompany] = useState("");
  const [category, setCategory] = useState("");
  const [kssl, setKssl] = useState("");
  const [country, setCountry] = useState("");

  useEffect(() => {
    let live = true;
    setLoading(true);
    api.matchups().then((d) => {
      if (!live) return;
      setAll(d);
      setLoading(false);
    }).catch(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  const companies = useMemo(() => uniq(all.map((m) => m.comp_by)), [all]);
  const categories = useMemo(() => uniq(all.map((m) => m.category)), [all]);
  const ksslProducts = useMemo(() => uniq(all.map((m) => m.kssl_name)), [all]);
  const countries = useMemo(() => uniq(all.map((m) => m.country)), [all]);

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((m) =>
      (!company || m.comp_by === company) &&
      (!category || m.category === category) &&
      (!kssl || m.kssl_name === kssl) &&
      (!country || m.country === country) &&
      (!needle ||
        m.comp_name.toLowerCase().includes(needle) ||
        m.kssl_name.toLowerCase().includes(needle)),
    );
  }, [all, q, company, category, kssl, country]);

  useEffect(() => {
    setSel((prev) => (prev && list.includes(prev) ? prev : list[0] ?? null));
  }, [list]);

  useEffect(() => {
    if (sel) setScope({ panel: "matchup", entityId: String(sel.id), label: `Positioning · ${sel.kssl_name}` });
  }, [sel, setScope]);

  return (
    <>
      <div className="subhead">
        <div className="left">
          <h1>Positioning</h1>
          <span className="cnt">{list.length} of {all.length} head-to-heads · KSSL vs real competitors</span>
        </div>
      </div>
      <div className="shell">
        <Rail />
        <div className="feed">
          <div className="mu-filterbar">
            <div className="mu-search">
              <span className="si">⌕</span>
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search product or competitor…" />
            </div>
            <div className="mu-filters">
              <select className="seq-select" value={company} onChange={(e) => setCompany(e.target.value)}>
                <option value="">All companies</option>
                {companies.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className="seq-select" value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">All categories</option>
                {categories.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className="seq-select" value={kssl} onChange={(e) => setKssl(e.target.value)}>
                <option value="">All KSSL products</option>
                {ksslProducts.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className="seq-select" value={country} onChange={(e) => setCountry(e.target.value)}>
                <option value="">All countries</option>
                {countries.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          </div>

          {loading ? (
            <div className="loading">Loading matchups…</div>
          ) : list.length === 0 ? (
            <div className="empty">No matchups for these filters.</div>
          ) : (
            list.map((m) => (
              <div key={m.id} className={"mu-row" + (sel?.id === m.id ? " sel" : "")} onClick={() => setSel(m)}>
                <div className="mu-ttl">{m.kssl_name} <span className="vs">vs</span> {m.comp_name}</div>
                <div className="meta">{m.category} · {m.comp_by} · {m.country}</div>
                <div className="edge">
                  <span className="edge-lab">KSSL edge</span>
                  <span className="edge-track"><i style={{ width: `${m.edge_score}%`, background: edgeColor(m.edge_score) }} /></span>
                  <span className="edge-val">{m.edge_score}</span>
                </div>
              </div>
            ))
          )}
        </div>
        <MatchupDossier m={sel} />
      </div>
    </>
  );
}
