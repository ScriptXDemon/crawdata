import type { SignalDetail } from "../api/types";

// The right-hand "intel document" — light paper panel inside the dark shell.
export function Dossier({ detail, loading }: { detail: SignalDetail | null; loading: boolean }) {
  if (loading) return <div className="ctx"><div className="ctx-empty">Loading…</div></div>;
  if (!detail)
    return (
      <div className="ctx">
        <div className="ctx-empty">
          Select a signal to open its full read — facts, multi-lens analysis, and recommended
          actions, all framed vs KSSL.
        </div>
      </div>
    );

  return (
    <div className="ctx">
      <div className={`ctx-h dir-${detail.dir ?? "watch"}`}>
        {detail.rank_display && <span className="eyebrow">{detail.rank_display}</span>}
        <div className="ct">{detail.title}</div>
      </div>

      {detail.facts && detail.facts.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Facts</span>
          {detail.facts.map((f, i) => (
            <div className="cd-frow" key={i}>
              <span className="cd-fk">{f[0]}</span>
              <span className="cd-fv">{f[1]}</span>
            </div>
          ))}
        </div>
      )}

      {detail.what_text && (
        <div className="ctx-sec">
          <span className="eyebrow">What happened</span>
          <div className="cd-prose">{detail.what_text}</div>
        </div>
      )}

      {detail.why_text && (
        <div className="ctx-sec">
          <span className="eyebrow">Why it matters · vs KSSL</span>
          <div className="cd-prose">{detail.why_text}</div>
        </div>
      )}

      {detail.lens_reads && detail.lens_reads.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Multi-lens read</span>
          {detail.lens_reads.map((l, i) => (
            <div className="lens-block" key={i}>
              <span className="lens-lead">{l[0]}</span>
              <div className="lens-body">{l[1]}</div>
            </div>
          ))}
        </div>
      )}

      {detail.actions && detail.actions.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Recommended actions</span>
          {detail.actions.map((a, i) => (
            <button className="cta" key={i}>
              <span className="ch">{a[0]}</span>
              {a[1]}
            </button>
          ))}
        </div>
      )}

      {detail.suggest && detail.suggest.length > 0 && (
        <div className="ctx-sec">
          <span className="eyebrow">Ask Mallory</span>
          <div className="suggest">
            {detail.suggest.map((s, i) => (
              <span className="schip" key={i}>
                {s}
              </span>
            ))}
          </div>
        </div>
      )}

      {detail.source_url && (
        <div className="ctx-sec">
          <a className="src-link" href={detail.source_url} target="_blank" rel="noreferrer">
            Open source ↗
          </a>
        </div>
      )}
    </div>
  );
}
