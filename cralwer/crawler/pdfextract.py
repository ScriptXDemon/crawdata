"""PDF handling — fetch a PDF and extract its plain text (§4).

PDF-capture jobs fetch + extract PDFs; the extracted text lands in
``attachments[].extracted_text`` as raw material for Layer 2. Never
fabricate: if extraction yields nothing, the attachment is recorded with the
storage path but ``extracted_text=None``. Table/spec structuring (beyond
plain text) is Layer 2's job, not raw harvest.
"""
from __future__ import annotations

import io

from . import storage
from .fetcher import Fetcher
from .models import Attachment


def extract_text(pdf_bytes: bytes, max_chars: int = 200_000) -> str | None:
    # max_chars bounds how much raw text is retained at all (extraction-time
    # safety cap).
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(p.strip() for p in parts if p.strip())
        text = text.replace("\x00", "").strip()
        return text[:max_chars] or None
    except Exception:
        return None


def extract_tables(pdf_bytes: bytes, max_tables: int = 20) -> list[dict]:
    """Structured tables from a (spec-heavy tender) PDF via pdfplumber → list of
    {title?, rows:[{col:val}]} dicts ready for the Document.tables `Table` model.
    pypdf's flat text collapses columns; this preserves the spec grid. Empty list if
    pdfplumber isn't installed or the PDF has no extractable tables."""
    try:
        import pdfplumber
    except Exception:
        return []
    out: list[dict] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for tbl in (page.extract_tables() or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    header = [(c or "").strip() or f"col{i}" for i, c in enumerate(tbl[0])]
                    rows = []
                    for r in tbl[1:]:
                        rows.append({header[i]: (r[i] or "").strip()
                                     for i in range(min(len(header), len(r)))})
                    if rows:
                        out.append({"rows": rows})
                    if len(out) >= max_tables:
                        return out
    except Exception:
        return out
    return out


def _looks_like_pdf(data: bytes) -> bool:
    """True only if the bytes are actually a PDF. A .pdf link that returned an HTML page
    (redirect / login wall / 404) is classified 'pdf' by URL extension and reaches here with
    HTML bytes — storing that as a .pdf gives a broken attachment the viewer can't open."""
    return b"%PDF" in data[:1024]


def fetch_attachments_and_tables(pdf_urls: list[str], fetcher: Fetcher) -> tuple[list[Attachment], list[dict]]:
    """Download several PDFs concurrently, store each, extract plain text AND structured
    tables (pdfplumber). Returns (attachments, tables); the caller merges tables onto
    Document.tables. Bytes that aren't actually a PDF (a .pdf URL that served HTML) are
    dropped — only real PDFs become attachments."""
    results = fetcher.fetch_assets(pdf_urls)
    atts: list[Attachment] = []
    tables: list[dict] = []
    for res in results:
        if res.error or not res.body_bytes or not _looks_like_pdf(res.body_bytes):
            continue
        storage_path = storage.put(res.body_bytes, kind="doc", ext="pdf")
        atts.append(Attachment(url=res.url, storage_path=storage_path, type="pdf",
                               extracted_text=extract_text(res.body_bytes)))
        tables.extend(extract_tables(res.body_bytes))
    return atts, tables
