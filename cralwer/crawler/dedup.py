"""Self-dedup + conditional re-fetch against our OWN crawl history (§7A).

The crawler dedups against *itself* — same URL, unchanged content => idempotent
re-crawl (do not re-emit). It does NOT merge different URLs about the same event
(that is L2's cross-source clustering).

Backed by a SQLite ``crawl_pages`` table keyed by canonical URL. For each page
we remember the last ``content_hash`` + HTTP validators (ETag / Last-Modified)
so the next run can (a) send conditional GETs and (b) skip unchanged content.

The classifier follows the reference crawler's golden rule: **a false
"changed" only wastes a re-crawl; a false "unchanged" loses data** — so every
uncertainty resolves to ``changed``.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config

Verdict = str  # "new" | "unchanged" | "changed" | "gone"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_pages (
    canonical_url TEXT PRIMARY KEY,
    content_hash  TEXT,
    etag          TEXT,
    last_modified TEXT,
    js_heavy      INTEGER DEFAULT 0,
    last_status   INTEGER,
    last_seen     TEXT,
    times_seen    INTEGER DEFAULT 0
);
"""


@dataclass
class StoredPage:
    canonical_url: str
    content_hash: str | None
    etag: str | None
    last_modified: str | None
    js_heavy: bool
    last_status: int | None


class CrawlHistory:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        with closing(self._conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, canonical_url: str) -> StoredPage | None:
        row = self._conn.execute(
            "SELECT * FROM crawl_pages WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        if not row:
            return None
        return StoredPage(
            canonical_url=row["canonical_url"], content_hash=row["content_hash"],
            etag=row["etag"], last_modified=row["last_modified"],
            js_heavy=bool(row["js_heavy"]), last_status=row["last_status"],
        )

    def conditional_headers(self, canonical_url: str) -> dict:
        """If we've seen this URL, return validators for a conditional GET."""
        sp = self.get(canonical_url)
        headers: dict[str, str] = {}
        if sp and sp.etag:
            headers["If-None-Match"] = sp.etag
        if sp and sp.last_modified:
            headers["If-Modified-Since"] = sp.last_modified
        return headers

    def upsert(self, canonical_url: str, *, content_hash: str | None,
               etag: str | None, last_modified: str | None,
               status: int | None, fetched_at: str, js_heavy: bool = False) -> None:
        self._conn.execute(
            """
            INSERT INTO crawl_pages
                (canonical_url, content_hash, etag, last_modified, js_heavy,
                 last_status, last_seen, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(canonical_url) DO UPDATE SET
                content_hash  = COALESCE(excluded.content_hash, crawl_pages.content_hash),
                etag          = COALESCE(excluded.etag,          crawl_pages.etag),
                last_modified = COALESCE(excluded.last_modified, crawl_pages.last_modified),
                js_heavy      = excluded.js_heavy,
                last_status   = excluded.last_status,
                last_seen     = excluded.last_seen,
                times_seen    = crawl_pages.times_seen + 1
            """,
            (canonical_url, content_hash, etag, last_modified,
             int(js_heavy), status, fetched_at),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def is_strong_etag(etag: str | None) -> bool:
    """Weak validators (``W/"..."``) can't justify trusting a 304."""
    return bool(etag) and not etag.strip().startswith(("W/", "w/"))


def classify(stored: StoredPage | None, *, status: int | None,
             content_hash: str | None, error: bool = False) -> Verdict:
    """Verdict from a fetch result. Bias every ambiguity toward ``changed``."""
    if stored is None or not stored.content_hash:
        return "new"
    if error:
        return "changed"                      # couldn't verify -> re-crawl
    if status in (404, 410):
        return "gone"
    if status == 304:
        return "unchanged" if is_strong_etag(stored.etag) else "changed"
    if status is not None and status >= 400:
        return "changed"
    if not content_hash:
        return "changed"                      # no usable hash -> uncertain
    return "unchanged" if content_hash == stored.content_hash else "changed"
