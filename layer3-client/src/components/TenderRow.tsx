import type { TenderCard } from "../api/types";

function deadlineLabel(t: TenderCard): string {
  if (t.dl_days == null) return "no deadline";
  if (t.dl_days < 0) return "closed";
  return `${t.dl_days} days to close`;
}

export function TenderRow({ t }: { t: TenderCard }) {
  const meta = [t.issuer, t.country, t.value_display, t.qty].filter(Boolean).join("  ·  ");
  return (
    <div className="tender">
      <div className="tender-head">
        <div>
          <div className="ttl">{t.title}</div>
          {meta && <div className="meta">{meta}</div>}
          <div className={"deadline" + (t.status === "closing" ? " closing" : "")}>
            {deadlineLabel(t)}
            {t.status === "closing" ? " · closing" : ""}
          </div>
        </div>
        {t.lean && <span className={`lean ${t.lean}`}>{t.lean}</span>}
      </div>

      {t.lean_text && <div className="lean-text" dangerouslySetInnerHTML={{ __html: t.lean_text }} />}

      {t.matches.map((m, i) => (
        <div className="match" key={i}>
          <div className="mrow">
            <span className="pname">{m.kssl_product_name}</span>
            <span className={`track ${m.fit_level}`}>
              <i style={{ width: `${m.fit_pct}%` }} />
            </span>
            <span className="pct">{m.fit_pct}%</span>
          </div>
          {m.match_lines && m.match_lines.length > 0 && (
            <ul className="mlines">
              {m.match_lines.map((l, j) => (
                <li key={j} className={l[0]}>
                  {l[1]}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}
