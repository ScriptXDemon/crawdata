"""Low-level HTML helpers: links, images, title, meta, tables.

Main-content (article body) extraction lives in ``textextract.py`` (trafilatura);
this module is the BeautifulSoup layer the fetcher/harvester use for crawling
(links) and the extractor uses for images/metadata/tables.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

_WS = re.compile(r"\s+")
# Asset extensions we never enqueue as crawlable pages. Documents (pdf/doc/...)
# are captured as ATTACHMENTS (see extract_pdf_links), not crawled as pages, so
# a tender RFP isn't emitted twice (once as attachment, once as its own page).
_NON_PAGE_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".mp4", ".mp3", ".avi", ".mov", ".zip", ".gz", ".woff", ".woff2", ".ttf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def title_of(html: str) -> str | None:
    try:
        t = _soup(html).title
        return t.get_text(strip=True)[:2000] if t and t.get_text(strip=True) else None
    except Exception:
        return None


def visible_text(html: str) -> str:
    """Crude full-page visible text (fallback when trafilatura finds nothing).
    Drops nav/header/footer chrome so entity resolution doesn't pick up menu
    junk ("Our Organisations" etc.) as a company mention."""
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "template",
                      "nav", "header", "footer"]):
        tag.decompose()
    return _WS.sub(" ", soup.get_text(" ", strip=True)).strip()


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute hrefs from <a>, dropping fragments, mailto/js, and asset files."""
    out: list[str] = []
    seen: set[str] = set()
    for a in _soup(html).find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base_url, href).split("#", 1)[0]
        if not absu.startswith(("http://", "https://")):
            continue
        path = urlsplit(absu).path.lower()
        if path.endswith(_NON_PAGE_EXT):
            continue
        if absu not in seen:
            seen.add(absu)
            out.append(absu)
    return out


def extract_links_with_text(html: str, base_url: str) -> list[tuple[str, str]]:
    """Same filtering as extract_links(), paired with the anchor's visible
    text (empty string if none). For opt-in link-text relevance (harvest.py)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in _soup(html).find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(base_url, href).split("#", 1)[0]
        if not absu.startswith(("http://", "https://")):
            continue
        path = urlsplit(absu).path.lower()
        if path.endswith(_NON_PAGE_EXT):
            continue
        if absu not in seen:
            seen.add(absu)
            out.append((absu, a.get_text(" ", strip=True)[:300]))
    return out


def extract_pdf_links(html: str, base_url: str) -> list[str]:
    """Absolute links that look like PDFs (tender RFP attachments)."""
    out: list[str] = []
    for a in _soup(html).find_all("a", href=True):
        absu = urljoin(base_url, a["href"].strip()).split("#", 1)[0]
        if absu.lower().split("?", 1)[0].endswith(".pdf"):
            out.append(absu)
    return list(dict.fromkeys(out))


def extract_media_links(html: str, base_url: str) -> list[dict]:
    """Video/audio links as METADATA ONLY (contract §4 — never downloaded).

    Captures <video>/<audio>/<source> sources, common embed iframes (YouTube /
    Vimeo), and <a> links to media files. Returns {url, type, title}."""
    soup = _soup(html)
    out: list[dict] = []
    seen: set[str] = set()

    def _add(url: str, mtype: str, title: str | None):
        absu = urljoin(base_url, (url or "").strip()).split("#", 1)[0]
        if absu.startswith(("http://", "https://")) and absu not in seen:
            seen.add(absu)
            out.append({"url": absu, "type": mtype, "title": title or None})

    for tag in soup.find_all(["video", "audio"]):
        mtype = tag.name
        src = tag.get("src")
        if src:
            _add(src, mtype, tag.get("title"))
        for s in tag.find_all("source"):
            if s.get("src"):
                _add(s["src"], mtype, tag.get("title"))
    for ifr in soup.find_all("iframe", src=True):
        s = ifr["src"]
        if any(h in s for h in ("youtube.com/embed", "youtu.be", "player.vimeo.com")):
            _add(s, "video", ifr.get("title"))
    for a in soup.find_all("a", href=True):
        low = a["href"].lower().split("?", 1)[0]
        if low.endswith((".mp4", ".mov", ".avi", ".webm")):
            _add(a["href"], "video", a.get_text(strip=True))
        elif low.endswith((".mp3", ".wav", ".m4a", ".ogg")):
            _add(a["href"], "audio", a.get_text(strip=True))
    return out


def extract_images(html: str, base_url: str) -> list[dict]:
    """Every <img> as {url, alt, width, height} (filtering happens later)."""
    out: list[dict] = []
    for img in _soup(html).find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        absu = urljoin(base_url, src).split("#", 1)[0]

        def _int(v):
            try:
                return int(str(v).replace("px", ""))
            except (TypeError, ValueError):
                return None
        out.append({
            "url": absu,
            "alt": (img.get("alt") or "").strip() or None,
            "width": _int(img.get("width")),
            "height": _int(img.get("height")),
        })
    return out


def extract_tables(html: str) -> list[dict]:
    """Each <table> -> {title, rows:[{col: val}]}. Best-effort, capped."""
    soup = _soup(html)
    out: list[dict] = []
    for tbl in soup.find_all("table"):
        header_cells = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
        rows: list[dict] = []
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            if header_cells and len(header_cells) == len(cells):
                rows.append(dict(zip(header_cells, cells)))
            else:
                rows.append({f"col{i}": c for i, c in enumerate(cells)})
        if rows:
            cap = tbl.find("caption")
            out.append({
                "title": cap.get_text(" ", strip=True) if cap else None,
                "rows": rows[:200],
            })
    return out[:20]


def extract_meta(html: str) -> dict:
    """Pull author + published date hints from meta tags / JSON-LD / <time>."""
    soup = _soup(html)
    meta: dict = {"author": None, "published_raw": None, "lang": None}

    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        meta["lang"] = html_tag["lang"].split("-")[0].lower()

    def _content(*selectors):
        for attr, val in selectors:
            tag = soup.find("meta", attrs={attr: val})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    meta["published_raw"] = _content(
        ("property", "article:published_time"),
        ("name", "publishdate"), ("name", "pubdate"), ("name", "date"),
        ("itemprop", "datePublished"), ("name", "dc.date"),
    )
    if not meta["published_raw"]:
        t = soup.find("time")
        if t and (t.get("datetime") or t.get_text(strip=True)):
            meta["published_raw"] = t.get("datetime") or t.get_text(strip=True)

    meta["author"] = _content(
        ("name", "author"), ("property", "article:author"), ("name", "byl"),
    )
    return meta
