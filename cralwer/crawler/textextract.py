"""Clean text + content hash + language + tables (the heart of Stage 3 output).

``main_text`` is the single most important field — Layer 2 runs NLP on it. We
extract the article body with trafilatura (drops nav/ads/comments), fall back to
crude visible text, and hash the *normalized main text* (not raw HTML) so
rotating ads/nav/timestamps never trigger a false "changed" on re-crawl (§7A).
"""
from __future__ import annotations

import hashlib
import re

from . import parse

_WS = re.compile(r"\s+")


def _safe(text: str) -> str:
    """Scrub NULs / surrogate junk that breaks downstream JSON / DB writes."""
    if not text:
        return ""
    return text.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


def main_text(html: str) -> str:
    """Boilerplate-stripped article body; falls back to full visible text."""
    try:
        import trafilatura
        out = trafilatura.extract(html, include_comments=False,
                                  include_tables=True, no_fallback=False)
        if out and out.strip():
            return _safe(out.strip())
    except Exception:
        pass
    return _safe(parse.visible_text(html))


def normalize_for_hash(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def content_hash(text: str) -> str | None:
    """``sha256:...`` of the normalized main text, or None if empty."""
    norm = normalize_for_hash(text)
    if not norm:
        return None
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def detect_language(text: str, html_lang: str | None = None) -> str:
    """Best-effort ISO-639-1 language of the main text. Falls back to the
    <html lang> hint, then 'en'."""
    sample = (text or "").strip()
    if len(sample) >= 30:
        try:
            from langdetect import detect
            return detect(sample[:4000])
        except Exception:
            pass
    if html_lang:
        return html_lang.split("-")[0].lower()
    return "en"


def summary(text: str, max_chars: int = 240) -> str | None:
    """Extractive 1–2 line summary = the first sentence(s) up to a cap. We never
    paraphrase or judge — purely mechanical (the contract's optional field)."""
    t = _WS.sub(" ", (text or "").strip())
    if not t:
        return None
    out = t[:max_chars]
    dot = out.rfind(". ")
    if dot > 60:
        out = out[: dot + 1]
    return out.strip()


def tables_from_html(html: str) -> list[dict]:
    return parse.extract_tables(html)
