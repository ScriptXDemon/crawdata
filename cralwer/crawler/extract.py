"""Stage 3 — EXTRACT. Turn a kept page into one ``document`` bundle (§3).

``build_document`` assembles the source object (clean main_text, content_hash,
images, PDF attachments, screenshot, tables, entity resolution, time signals).
``document_tags`` then attaches flat informational tags (stream, detected
competitor/products/countries/tech domains) derived from what the resolver
already found — it does NOT construct separately-typed records; deep record
classification (tender/partnership/geo_footprint/innovation/company_event/
competitive_signal) is Layer 2's job, operating on the raw text + tags.
"""
from __future__ import annotations

import re
import uuid

from . import images as images_mod
from . import pdfextract, resolver, screenshot, sources, textextract, translate
from .extractutil import parse_date
from .fetcher import Fetcher
from .harvest import HarvestedPage
from .models import Document, Job, Table
from .resolver import _Matcher
from .seed import Seed


def _strip_nav_footer(text: str, max_menu_words: int = 4) -> str:
    """Drop leading/trailing runs of short, menu-shaped lines (e.g. 'Home |
    About | Products | Contact') from plain rendered body text. Only trims
    the outer envelope — never the middle — so dynamically-revealed content
    (the reason this path bypasses trafilatura in the first place) is safe."""
    lines = text.splitlines()

    def _is_menu_line(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and len(stripped.split()) <= max_menu_words

    start = 0
    while start < len(lines) and _is_menu_line(lines[start]):
        start += 1
    end = len(lines)
    while end > start and _is_menu_line(lines[end - 1]):
        end -= 1
    return "\n".join(lines[start:end]).strip()


# --- document assembly ---------------------------------------------------
def build_document(job: Job, page: HarvestedPage, seed: Seed, matcher: _Matcher,
                   fetcher: Fetcher, enrich: bool = True) -> Document | None:
    """Assemble the document. With ``enrich=False`` it stops after text +
    entities + metadata (cheap) so the gate can decide before we spend effort on
    images/PDF/screenshot; call ``enrich_assets`` afterwards on kept docs."""
    res = page.fetch
    html = res.text_html or ""
    is_pdf_page = res.kind == "pdf"

    if is_pdf_page:
        body_text = pdfextract.extract_text(res.body_bytes or b"") or ""
        title = textextract.normalize_for_hash(body_text)[:80] or res.url
        meta = {"author": None, "published_raw": res.published_hint, "lang": None}
        tables: list[dict] = []
    else:
        # When interactions ran, use inner_text directly — trafilatura's
        # boilerplate filter would otherwise drop dynamically-revealed content.
        if res.inner_text:
            body_text = _strip_nav_footer(textextract._safe(res.inner_text.strip()))
        else:
            body_text = textextract.main_text(html)
        from . import parse
        title = parse.title_of(html) or res.title or res.url
        meta = parse.extract_meta(html)
        tables = textextract.tables_from_html(html) if "html" in job.capture else []

    if not body_text.strip():
        return None  # no usable main_text -> nothing to emit (acceptance rule 1)

    chash = textextract.content_hash(body_text)
    lang = textextract.detect_language(body_text, meta.get("lang"))
    main_text_en = (translate.to_english(body_text, lang, chash, res.url)
                    if lang != "en" else None)

    published_iso, precision = parse_date(meta.get("published_raw") or res.published_hint)

    src = sources.resolve_source(res.url, seed, job)
    detected = resolver.resolve(body_text, title, seed, matcher)
    title = _clean_title(title, src)

    doc = Document(
        url=res.url,
        content_hash=chash or "sha256:empty",
        fetched_at=res.fetched_at,
        source_id=src.source_id,
        source_tier=src.source_tier,
        source_type=src.source_type,
        source_region=src.source_region,
        source_known=src.source_known,
        source_resolved_by=src.source_resolved_by,
        title=title[:500],
        author=meta.get("author"),
        published_at=published_iso,
        date_precision=precision,
        language=lang,
        access="open",
        main_text=body_text,
        main_text_en=main_text_en,
        html=html,
        summary=textextract.summary(body_text),
        tables=[Table(**t) for t in tables],
        entities_detected=detected,
        document_id="doc_" + uuid.uuid4().hex[:12],
    )
    _apply_detection_tags(job, doc, seed)

    if enrich:
        enrich_assets(job, doc, page, fetcher)
    return doc


def enrich_assets(job: Job, doc: Document, page: HarvestedPage, fetcher: Fetcher) -> None:
    """Capture the expensive assets (images / PDF attachments / screenshot) for a
    KEPT document. Called after the gate so dropped pages cost nothing here."""
    res = page.fetch
    is_pdf_page = res.kind == "pdf"
    html = res.text_html or ""
    from . import storage
    from .models import Attachment, Screenshot

    # Images (kept only for capture types that ask for them).
    if "images" in job.capture and page.image_candidates:
        doc.images = images_mod.select_and_store(page.image_candidates, fetcher)

    # Media (video/audio) — metadata only, never downloaded (§4).
    if "media" in job.capture and page.media_candidates:
        from .models import Media
        doc.media = [Media(**m) for m in page.media_candidates[:10]]

    # PDF attachments (tender RFPs / primary docs) — downloaded concurrently.
    if "pdf" in job.capture and page.pdf_links:
        doc.attachments.extend(pdfextract.fetch_attachments(page.pdf_links[:3], fetcher))
    if is_pdf_page:  # the page itself is the PDF
        sp = storage.put(res.body_bytes, kind="doc", ext="pdf")
        doc.attachments.append(Attachment(url=res.url, storage_path=sp, type="pdf",
                                          extracted_text=doc.main_text))

    # Screenshot — one full-page audit capture per kept document. Prefer the PNG
    # already grabbed during the render pass (no second browser); only fall back
    # to screenshot.capture() (a fresh launch + re-goto) when the page was NOT
    # rendered (httpx path) so no inline PNG exists.
    if "screenshot" in job.capture and not is_pdf_page:
        png = res.screenshot_png
        if png is None:
            png = screenshot.capture(res.url, html, doc.title, doc.main_text, job.render_js)
        sp = storage.put(png, kind="shot", ext="png")
        doc.screenshot = Screenshot(storage_path=sp, captured_at=res.fetched_at)


# --- detection tags (informational, not record classification) -----------
def _main_competitor(job: Job, detected, seed: Seed) -> str | None:
    if job.target_entity and job.target_entity in seed.entities:
        if seed.entities[job.target_entity].kind in ("competitor", "anchor"):
            return job.target_entity
    comps = [c for c in resolver.competitors(detected)
             if seed.entities.get(c) and seed.entities[c].kind != "anchor"]
    return comps[0] if comps else (resolver.competitors(detected) or [None])[0]


def _stream(job: Job, comp: str | None, detected) -> str:
    if job.job_type == "tender":
        return "tender"
    if comp and job.target_entity:
        return "competitive"
    if resolver.tech_domains(detected):
        return "technology"
    return "market"


def _apply_detection_tags(job: Job, doc: Document, seed: Seed) -> None:
    """Flat, informational summary of doc.entities_detected — reused by L2 as
    a shortcut. Not a classification of the page into a typed record."""
    detected = doc.entities_detected
    comp = _main_competitor(job, detected, seed)
    doc.stream = _stream(job, comp, detected)
    doc.detected_competitor = comp
    doc.detected_products = resolver.products(detected)
    doc.detected_countries = resolver.countries(detected)
    doc.detected_tech_domains = resolver.tech_domains(detected)
    doc.detected_unknown_companies = [d.surface for d in detected if d.type == "unknown_company"]


def _clean_title(title: str, source) -> str:
    """Strip a trailing site/publisher suffix ('… — IDRW', '… | ET') without
    touching titles whose dash is content ('CAESAR 6x6 — 155mm howitzer').
    ``source`` is a SourceInfo — the domain-derived source_id often matches the
    publisher name in the suffix (RAKSHAANIRVEDA ~ 'Raksha Anirveda')."""
    if not title:
        return title
    sid_norm = re.sub(r"[^a-z0-9]", "", (getattr(source, "source_id", "") or "").lower())
    for sep in (" — ", " | ", " – ", " - "):
        if sep in title:
            left, right = title.rsplit(sep, 1)
            r = right.strip()
            r_norm = re.sub(r"[^a-z0-9]", "", r.lower())
            matches_source = bool(r_norm) and sid_norm and (r_norm in sid_norm or sid_norm in r_norm)
            short_abbrev = len(r) <= 5 and r.replace(".", "").isupper()
            if left.strip() and (matches_source or short_abbrev):
                return left.strip()
    return title


