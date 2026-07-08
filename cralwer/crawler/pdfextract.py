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


def _store_and_extract(res) -> Attachment | None:
    if res.error or not res.body_bytes:
        return None
    storage_path = storage.put(res.body_bytes, kind="doc", ext="pdf")
    return Attachment(
        url=res.url, storage_path=storage_path, type="pdf",
        extracted_text=extract_text(res.body_bytes),
    )


def fetch_attachment(pdf_url: str, fetcher: Fetcher) -> Attachment | None:
    """Download a PDF, store it, and extract its plain text. None on hard failure."""
    return _store_and_extract(fetcher.fetch_asset(pdf_url))


def fetch_attachments(pdf_urls: list[str], fetcher: Fetcher) -> list[Attachment]:
    """Download several PDFs concurrently (fetcher.fetch_assets) then store +
    extract each. Same result as calling fetch_attachment per URL, just faster;
    failed downloads are dropped from the list."""
    results = fetcher.fetch_assets(pdf_urls)
    out: list[Attachment] = []
    for res in results:
        att = _store_and_extract(res)
        if att:
            out.append(att)
    return out
