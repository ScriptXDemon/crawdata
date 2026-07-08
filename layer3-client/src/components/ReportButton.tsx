import { useState } from "react";

import { api } from "../api/client";
import type { ReportResponse } from "../api/types";

function strip(html?: string | null): string {
  return (html ?? "").replace(/<[^>]+>/g, "");
}

export function ReportButton() {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [loading, setLoading] = useState(false);

  async function generate() {
    setOpen(true);
    setLoading(true);
    try {
      setReport(await api.ceoReport());
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <button className="brief-btn" onClick={generate}>
        <i className="ti ti-file-text" aria-hidden="true" /> CEO brief
      </button>
      {open && (
        <div className="modal-overlay" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-h">
              <span className="eyebrow">Cross-pillar synthesis</span>
              <button className="modal-x" onClick={() => setOpen(false)} aria-label="Close">
                ✕
              </button>
            </div>
            {loading || !report ? (
              <div className="loading">Composing brief…</div>
            ) : (
              <div className="report">
                <h2>{report.title}</h2>
                {report.sections.map((s, i) => (
                  <div className="report-sec" key={i}>
                    <span className="eyebrow">{s.heading}</span>
                    {typeof s.body === "string" ? (
                      <p>{s.body}</p>
                    ) : (
                      <ul className="report-list">
                        {s.body.map((it, j) => (
                          <li key={j}>
                            <b>{it.title}</b>
                            {it.note ? ` — ${strip(it.note)}` : ""}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
