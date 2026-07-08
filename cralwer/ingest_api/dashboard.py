"""A tiny, dependency-free HTML dashboard for the ingested page bundles.

Reads ``data/output/ingested.ndjson`` (the audit trail every run writes) and
renders each raw harvested page — clean text, detection tags, resolved
entities, and viewable artifacts (screenshot / images / PDF) — so the
crawler's output is browsable in a browser, not just on the CLI.
"""
from __future__ import annotations

import html
import json
from urllib.parse import quote

from crawler import config

_STREAM_BADGE = {
    "competitive": "#2563eb", "tender": "#b45309",
    "market": "#475569", "technology": "#15803d",
}


def load_records() -> list[dict]:
    path = config.OUTPUT_DIR / "ingested.ndjson"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _esc(v) -> str:
    return html.escape(str(v))


def _kb(n: int) -> str:
    return f"{n/1024:.0f} KB" if n >= 1024 else f"{n} B"


def _raw_metrics(doc: dict) -> str:
    """One row of raw-data-captured counts per page (what L2 receives)."""
    parts = [
        f'📝 {len(doc.get("main_text") or ""):,} chars',
        f'🌐 {_kb(len(doc.get("html") or ""))} html',
    ]
    imgs = [i for i in doc.get("images", []) if i.get("storage_path")]
    if imgs:
        parts.append(f'🖼️ {len(imgs)} img')
    pdfs = [a for a in doc.get("attachments", []) if a.get("storage_path")]
    if pdfs:
        parts.append(f'📄 {len(pdfs)} pdf')
    if (doc.get("screenshot") or {}).get("storage_path"):
        parts.append("📸 shot")
    if doc.get("main_text_en"):
        parts.append("🔤 +EN")
    return '<div class="raw">' + "".join(f'<span class="m">{_esc(p)}</span>' for p in parts) + "</div>"


def _artifact_img(storage_path: str, label: str) -> str:
    src = "/artifact?path=" + quote(storage_path)
    return (f'<a href="{src}" target="_blank" title="{_esc(label)}">'
            f'<img class="thumb" src="{src}" alt="{_esc(label)}"></a>')


def _page_card(item: dict) -> str:
    doc = item["document"]
    stream = doc.get("stream") or "?"
    color = _STREAM_BADGE.get(stream, "#475569")

    tags = ""
    for label, val in (
        ("competitor", doc.get("detected_competitor")),
        ("products", ", ".join(doc.get("detected_products") or [])),
        ("countries", ", ".join(doc.get("detected_countries") or [])),
        ("tech", ", ".join(doc.get("detected_tech_domains") or [])),
    ):
        if val:
            tags += f'<span class="f"><b>{_esc(label)}</b> {_esc(val)}</span>'

    ents = ""
    for e in doc.get("entities_detected", [])[:12]:
        cls = "ent" if e.get("resolved_id") else "ent unk"
        rid = e.get("resolved_id") or "?"
        ents += f'<span class="{cls}">{_esc(e["surface"])} → {_esc(rid)}</span>'

    arts = ""
    shot = doc.get("screenshot")
    if shot and shot.get("storage_path"):
        arts += _artifact_img(shot["storage_path"], "screenshot")
    for img in doc.get("images", []):
        if img.get("storage_path"):
            arts += _artifact_img(img["storage_path"], img.get("caption") or img.get("role") or "image")
    pdfs = ""
    for att in doc.get("attachments", []):
        if att.get("storage_path"):
            pdfs += (f'<a class="pill" href="/artifact?path={quote(att["storage_path"])}" '
                     f'target="_blank">📄 PDF ({len((att.get("extracted_text") or ""))} chars)</a>')
    for m in doc.get("media", []):
        pdfs += f'<a class="pill" href="{_esc(m["url"])}" target="_blank">🎬 {_esc(m.get("type","media"))}</a>'
    # raw source HTML lives inline on the doc; open it via /raw-html by doc id
    if doc.get("html") and doc.get("document_id"):
        pdfs += (f'<a class="pill html" href="/raw-html?doc_id={quote(doc["document_id"])}" '
                 f'target="_blank">🌐 raw HTML ({_kb(len(doc["html"]))})</a>')

    title = doc.get("title") or "(untitled)"
    summary = doc.get("summary") or (doc.get("main_text") or "")[:240]
    tier = doc.get("source_tier")
    lang = doc.get("language", "en")
    lang_badge = "" if lang == "en" else f'<span class="lang">{_esc(lang)}→en</span>'

    return f"""
    <div class="card">
      <div class="top">
        <span class="badge" style="background:{color}">{_esc(stream)}</span>
        <span class="src">{_esc(doc.get('source_id','?'))} · tier {_esc(tier)}</span>
        {'' if doc.get('source_known', True) else '<span class="unverified">unverified</span>'}
        {lang_badge}
        <span class="date">{_esc(doc.get('published_at') or '')}</span>
      </div>
      <a class="title" href="{_esc(doc.get('url',''))}" target="_blank">{_esc(title)}</a>
      {_raw_metrics(doc)}
      <div class="summary">{_esc(summary)}</div>
      <div class="fields">{tags}</div>
      <div class="ents">{ents}</div>
      <div class="arts">{arts}{pdfs}</div>
    </div>"""


def render() -> str:
    records = load_records()
    by_stream: dict[str, int] = {}
    by_source: dict[str, int] = {}
    raw_bytes = raw_chars = n_img = n_pdf = n_shot = 0
    for r in records:
        doc = r["document"]
        stream = doc.get("stream") or "?"
        by_stream[stream] = by_stream.get(stream, 0) + 1
        sid = doc.get("source_id", "?")
        by_source[sid] = by_source.get(sid, 0) + 1
        raw_bytes += len(doc.get("html") or "")
        raw_chars += len(doc.get("main_text") or "")
        n_img += sum(1 for i in doc.get("images", []) if i.get("storage_path"))
        n_pdf += sum(1 for a in doc.get("attachments", []) if a.get("storage_path"))
        n_shot += 1 if (doc.get("screenshot") or {}).get("storage_path") else 0

    raw_total = (f'📦 raw captured: <b>{raw_bytes/1_048_576:.1f} MB</b> html · '
                 f'<b>{raw_chars:,}</b> text chars · '
                 f'<b>{n_img}</b> img · <b>{n_pdf}</b> pdf · <b>{n_shot}</b> screenshots')

    chips = "".join(f'<span class="stat">{_esc(k)}: <b>{v}</b></span>'
                    for k, v in sorted(by_stream.items()))
    srcs = "".join(f'<span class="stat src">{_esc(k)}: <b>{v}</b></span>'
                   for k, v in sorted(by_source.items()))
    cards = "".join(_page_card(r) for r in records) or (
        '<p class="empty">No page bundles yet. Run <code>python run.py testing</code> '
        'or <code>python run.py run jobs/testing_batch.json</code>, then refresh.</p>')

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mallory Crawler — harvested pages</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin:0;
          background:#0f172a; color:#e2e8f0; }}
  header {{ padding:18px 24px; background:#111827; border-bottom:1px solid #1f2937;
            position:sticky; top:0; }}
  h1 {{ margin:0 0 6px; font-size:18px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }}
  .stat {{ background:#1e293b; padding:3px 9px; border-radius:12px; font-size:12px; }}
  .stat.src {{ background:#172033; color:#93c5fd; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr));
           gap:14px; padding:18px 24px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px; }}
  .top {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:6px; }}
  .badge {{ color:#fff; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }}
  .src {{ font-size:12px; color:#94a3b8; }}
  .lang {{ font-size:11px; background:#3b0764; color:#e9d5ff; padding:1px 7px; border-radius:10px; }}
  .unverified {{ font-size:11px; background:#7c2d12; color:#fed7aa; padding:1px 7px; border-radius:10px; }}
  .date {{ margin-left:auto; font-size:12px; color:#64748b; }}
  .title {{ display:block; font-weight:600; color:#f1f5f9; text-decoration:none; margin:4px 0; }}
  .title:hover {{ color:#60a5fa; }}
  .raw {{ display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 8px; }}
  .m {{ background:#0b1220; color:#94a3b8; padding:1px 7px; border-radius:6px;
        font-size:11px; border:1px solid #1e293b; }}
  .summary {{ font-size:12.5px; color:#cbd5e1; margin-bottom:8px; }}
  .fields {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }}
  .f {{ background:#0f172a; padding:2px 8px; border-radius:6px; font-size:11.5px; }}
  .f b {{ color:#7dd3fc; font-weight:600; }}
  .ents {{ display:flex; flex-wrap:wrap; gap:5px; margin-bottom:8px; }}
  .ent {{ background:#064e3b; color:#a7f3d0; padding:1px 7px; border-radius:10px; font-size:11px; }}
  .ent.unk {{ background:#7c2d12; color:#fed7aa; }}
  .arts {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
  .thumb {{ height:70px; border-radius:6px; border:1px solid #475569; }}
  .pill {{ background:#1d4ed8; color:#fff; padding:3px 9px; border-radius:8px;
           font-size:11.5px; text-decoration:none; }}
  .pill.html {{ background:#334155; }}
  .empty {{ padding:40px; color:#94a3b8; }}
  code {{ background:#0f172a; padding:1px 6px; border-radius:4px; }}
  a {{ color:#60a5fa; }}
</style></head><body>
<header>
  <h1>🛰️ Mallory Crawler — harvested pages <span style="color:#64748b">(Layer 1 → Ingest)</span></h1>
  <div class="stats">{chips or '<span class="stat">0 pages</span>'}</div>
  <div class="stats">{srcs}</div>
  <div style="margin-top:8px;font-size:12.5px;color:#cbd5e1">{raw_total}</div>
  <div style="margin-top:6px;font-size:12px;color:#64748b">
    {_esc(len(records))} accepted pages · <a href="/stats">/stats</a> ·
    <a href="/v1/docs">API docs</a> · data/output/ingested.ndjson
  </div>
</header>
<div class="grid">{cards}</div>
</body></html>"""
