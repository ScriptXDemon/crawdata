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
    times_seen    INTEGER DEFAULT 0,
    error_category TEXT,
    fail_count    INTEGER DEFAULT 0,
    failed_at     TEXT
);
CREATE TABLE IF NOT EXISTS crawl_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    host            TEXT,
    fetched_at      TEXT,
    ua              TEXT,
    robots_decision TEXT,
    status          INTEGER,
    reason          TEXT,
    careful         INTEGER DEFAULT 0
);
"""

# Columns added after the original schema shipped — back-filled onto existing DBs
# (no migration framework; a crawl_history.sqlite may predate these).
_EXTRA_COLS = {"error_category": "TEXT", "fail_count": "INTEGER DEFAULT 0", "failed_at": "TEXT"}


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
            existing = {r[1] for r in cur.execute("PRAGMA table_info(crawl_pages)")}
            for col, decl in _EXTRA_COLS.items():
                if col not in existing:
                    cur.execute(f"ALTER TABLE crawl_pages ADD COLUMN {col} {decl}")
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
                times_seen    = crawl_pages.times_seen + 1,
                error_category = NULL,
                fail_count    = 0
            """,
            (canonical_url, content_hash, etag, last_modified,
             int(js_heavy), status, fetched_at),
        )
        self._conn.commit()

    def record_failure(self, canonical_url: str, *, status: int | None,
                       category: str | None, failed_at: str) -> None:
        """Persist a failed fetch so 'gone' (404/410) and 'retry next run' survive across runs.
        Increments fail_count so is_gone can require two 404s before giving up on a URL."""
        self._conn.execute(
            """
            INSERT INTO crawl_pages
                (canonical_url, last_status, error_category, failed_at, fail_count,
                 last_seen, times_seen)
            VALUES (?, ?, ?, ?, 1, ?, 0)
            ON CONFLICT(canonical_url) DO UPDATE SET
                last_status    = excluded.last_status,
                error_category = excluded.error_category,
                failed_at      = excluded.failed_at,
                fail_count     = COALESCE(crawl_pages.fail_count, 0) + 1,
                last_seen      = excluded.last_seen
            """,
            (canonical_url, status, category, failed_at, failed_at),
        )
        self._conn.commit()

    def record_audit(self, *, url: str, host: str, fetched_at: str, ua: str,
                     robots_decision: str, status: int | None, reason: str | None,
                     careful: bool) -> None:
        """Append-only compliance record: what we crawled, when, as whom, under which robots
        decision. One row per visit (unlike crawl_pages which overwrites). Used for careful
        (gov/mil) hosts so polite crawling is provable."""
        self._conn.execute(
            "INSERT INTO crawl_audit (url, host, fetched_at, ua, robots_decision, status, "
            "reason, careful) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (url, host, fetched_at, ua, robots_decision, status, reason, int(bool(careful))))
        self._conn.commit()

    def is_gone(self, canonical_url: str) -> bool:
        """True if this URL is known permanently gone → skip it on future runs. Golden rule:
        a false 'gone' loses data, so 410 (the server's explicit word) counts immediately but
        404 needs TWO strikes (a transient 404 during a deploy shouldn't kill a URL forever)."""
        row = self._conn.execute(
            "SELECT last_status, fail_count FROM crawl_pages WHERE canonical_url = ?",
            (canonical_url,)).fetchone()
        if not row:
            return False
        st, fc = row["last_status"], (row["fail_count"] or 0)
        if st == 410:
            return True
        return st == 404 and fc >= 2

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
