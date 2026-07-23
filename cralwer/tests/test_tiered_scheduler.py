"""Tiered C3 scheduler, auto-careful, and the batch-report taxonomy.

Guards the load-bearing pieces of the deferred-C3 / needs-network-path / auto-careful work:
- errors.bucket_reason splits resolvable vs terminal correctly (drives the completion report),
- dedup stamps + scopes crawl_run_id and streams the needs_network_path backlog,
- host_careful persists (learn→persist→seed like host_render_always),
- HostLimiter.make_careful throttles a host to concurrency 1 mid-run,
- _defer_c3_enabled never defers a terminal GONE (404/410).
All new behavior is flag-gated; these test the mechanisms directly.
"""
import asyncio

from crawler import config, errors
from crawler.async_engine import AsyncEngine, HostLimiter
from crawler.dedup import CrawlHistory


# ── report taxonomy ──────────────────────────────────────────────────────────
def test_bucket_reason():
    assert errors.bucket_reason(None) == "kept"
    assert errors.bucket_reason("") == "kept"
    # resolvable
    for r in ("needs_network_path", "timeout", "conn_refused", "skipped_host_down",
              "http_503", "http_429", "http_500"):
        assert errors.bucket_reason(r) == "retryable", r
    # terminal
    for r in ("skipped_gone", "blocked_by_robots", "ssl", "no_main_text",
              "http_404", "http_410", "http_400", "http_401"):
        assert errors.bucket_reason(r) == "terminal", r


# ── dedup: crawl_run_id scoping + netpath backlog ────────────────────────────
def test_crawl_run_id_and_netpath_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    h = CrawlHistory()
    try:
        h.record_failure("https://x.com/a", status=403, category="needs_network_path",
                         failed_at="2026-07-18T00:00:00Z", crawl_run_id="run_A")
        h.record_failure("https://x.com/b", status=404, category="skipped_gone",
                         failed_at="2026-07-18T00:00:00Z", crawl_run_id="run_A")
        h.upsert("https://x.com/c", content_hash="sha256:z", etag=None, last_modified=None,
                 status=200, fetched_at="2026-07-18T00:00:00Z", crawl_run_id="run_A")
        # backlog: only the needs_network_path URL, scoped by run start
        got = list(h.iter_needs_network_path("2026-07-17T00:00:00Z", 100))
        assert got == ["https://x.com/a"]
        # a later success clears it from the backlog (error_category reset to NULL)
        h.upsert("https://x.com/a", content_hash="sha256:y", etag=None, last_modified=None,
                 status=200, fetched_at="2026-07-18T01:00:00Z", crawl_run_id="run_A")
        assert list(h.iter_needs_network_path("2026-07-17T00:00:00Z", 100)) == []
        # crawl_run_id persisted on the row
        row = h._conn.execute("SELECT crawl_run_id FROM crawl_pages WHERE canonical_url='https://x.com/c'").fetchone()
        assert row["crawl_run_id"] == "run_A"
    finally:
        h.close()


def test_host_careful_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    h = CrawlHistory()
    try:
        assert h.get_careful_hosts() == set()
        h.set_careful_host("sipri.org", True)
        assert "sipri.org" in h.get_careful_hosts()
        h.set_careful_host("sipri.org", False)
        assert "sipri.org" not in h.get_careful_hosts()
    finally:
        h.close()


# ── HostLimiter.make_careful ─────────────────────────────────────────────────
def test_make_careful_throttles_to_one(monkeypatch):
    monkeypatch.setenv("CRAWLER_CAREFUL_DELAY_S", "5.0")

    async def go():
        h = HostLimiter(max_conc=24, min_delay=0.3)
        await h._ensure("weak.com")
        assert h._sem["weak.com"]._value == 24          # normal host: pool concurrency
        h.make_careful("weak.com")
        assert "weak.com" in h.force_careful
        assert h._sem["weak.com"]._value == 1           # throttled to 1
        assert h._delay["weak.com"] >= 5.0
        # idempotent + no-op on an already-careful host
        h.make_careful("weak.com")
        assert h._sem["weak.com"]._value == 1

    asyncio.run(go())


# ── _defer_c3_enabled never defers a terminal GONE ───────────────────────────
def test_defer_c3_excludes_gone(monkeypatch):
    monkeypatch.setenv("CRAWLER_DEFER_C3", "1")
    monkeypatch.setenv("CAMOFOX_ENABLED", "1")
    eng = AsyncEngine.__new__(AsyncEngine)              # bypass __init__
    assert eng._defer_c3_enabled(403) is True           # resolvable block → defer
    assert eng._defer_c3_enabled(404) is False          # GONE → never defer
    assert eng._defer_c3_enabled(410) is False
    monkeypatch.setenv("CRAWLER_DEFER_C3", "0")
    assert eng._defer_c3_enabled(403) is False          # flag off → inline (today's behavior)


# ── big-site fill: grow a surviving host's cap so the pool tail isn't 6/256 ───
def test_retarget_grows_only_and_spares_weak_hosts(monkeypatch):
    monkeypatch.setenv("CRAWLER_CAREFUL_DELAY_S", "5.0")

    async def go():
        h = HostLimiter(max_conc=6, min_delay=0.1)          # 256 tabs // 89 hosts = 6
        await h._ensure("big.com")
        assert h._sem["big.com"]._value == 6

        h.retarget("big.com", 64)                            # small sites done → widen
        assert h._sem["big.com"]._value == 64
        assert h._cap["big.com"] == 64

        h.retarget("big.com", 24)                            # never shrinks
        assert h._sem["big.com"]._value == 64

        # a host demoted to careful must NOT be widened by the fill loop
        await h._ensure("weak.com")
        h.make_careful("weak.com")
        h.retarget("weak.com", 64)
        assert h._sem["weak.com"]._value == 1
        assert h._cap["weak.com"] == 1

    asyncio.run(go())


# ── path_scope: keep a crawl inside the seeded SECTION of a hub-shaped host ───
def test_in_path_scope():
    from crawler.harvest import _in_path_scope as ok

    # no scope = unrestricted (today's behaviour, must not regress)
    assert ok("https://x.com/anything", [])
    assert ok("https://x.com/anything", None)

    scope = ["/defence", "/about/defence"]
    assert ok("https://indianexpress.com/defence", scope)
    assert ok("https://indianexpress.com/defence/", scope)
    assert ok("https://indianexpress.com/about/defence/drdo-test", scope)
    # the real-world miss: entertainment must NOT survive a /about/defence seed
    assert not ok("https://indianexpress.com/article/entertainment/telugu/mahesh-babu", scope)
    assert not ok("https://indianexpress.com/about/marvel-comics", scope)
    # segment-boundary: a prefix must not match a longer sibling segment
    assert not ok("https://indianexpress.com/defence-contractors", scope)
    # leading/trailing slashes in the scope entry are normalised
    assert ok("https://x.com/defence/a", ["defence/"])
    # "/" means the whole site
    assert ok("https://x.com/whatever", ["/"])


# ── link_relevance_keywords must actually prune on the ASYNC engine ──────────
def test_async_engine_honors_link_relevance(monkeypatch):
    """It was implemented only in harvest.py; the async engine ignored it, so a defence seed on a
    newspaper followed its entertainment links. Guards the port."""
    from types import SimpleNamespace
    from crawler.async_engine import AsyncEngine

    html = ('<a href="/article/entertainment/taylor-swift">Taylor Swift concert</a>'
            '<a href="/article/defence/drdo-test">DRDO missile test</a>')
    eng = AsyncEngine.__new__(AsyncEngine)
    got = []
    eng._enqueue_candidate = lambda ctx, cl, parent, depth: got.append(cl) or True

    job = SimpleNamespace(max_depth=5, link_relevance_keywords=["defence", "missile", "drdo"])
    ctx = SimpleNamespace(job=job, has_budget=lambda: True)
    fr = SimpleNamespace(text_html=html, final_url="https://ie.com/x",
                         is_html=lambda: True)

    eng._enqueue_links(ctx, fr, "https://ie.com/x", 0)
    assert any("drdo-test" in u for u in got), got
    assert not any("taylor-swift" in u for u in got), got

    # empty keyword list = follow everything (default behaviour must not regress)
    got.clear()
    job.link_relevance_keywords = []
    eng._enqueue_links(ctx, fr, "https://ie.com/x", 0)
    assert len(got) == 2, got


# ── WAF wall served as HTTP 200 must not be stored as a document ─────────────
def test_block_page_detection():
    """709 CloudFront walls were ingested as real docs because the markers were Akamai-only and
    CloudFront returns the wall with status 200."""
    from crawler import errors
    from crawler.async_engine import _is_block_body
    from types import SimpleNamespace

    CLOUDFRONT = ("<html><head><title>ERROR: The request could not be satisfied</title></head>"
                  "<body><h1>403 ERROR</h1><p>The request could not be satisfied.</p>"
                  "<p>Request blocked.</p><p>Generated by cloudfront (CloudFront)</p></body></html>")
    CLOUDFLARE = "<html><body>Just a moment... Enable JavaScript and cookies to continue</body></html>"
    AKAMAI = "<html><head><title>Access Denied</title></head><body>Reference #18.x</body></html>"

    assert errors.is_ip_block_page(CLOUDFRONT)
    assert errors.is_ip_block_page(CLOUDFLARE)
    assert errors.is_ip_block_page(AKAMAI)          # existing behaviour preserved

    # a real article that merely MENTIONS these terms must not be flagged
    article = ("<html><body>DRDO said access denied to the test range was lifted. The 403 error "
               "in the portal was fixed after the missile trial concluded.</body></html>")
    assert not errors.is_ip_block_page(article)

    # _is_block_body: HTML only, and env-switchable
    html = SimpleNamespace(is_html=lambda: True, text_html=CLOUDFRONT)
    pdf = SimpleNamespace(is_html=lambda: False, text_html=CLOUDFRONT)
    assert _is_block_body(html)
    assert not _is_block_body(pdf)
    assert not _is_block_body(None)


# ── ingest must refuse to STORE a WAF wall (defence in depth) ────────────────
def test_ingest_rejects_block_page():
    """703 CloudFront walls reached Postgres and were shipped to L2, which analysed the error page
    as if it were Lockheed content. The storage boundary must reject them independently."""
    from ingest_api.validation import validate_page

    base = {"url": "https://lockheedmartin.com/en-us/capabilities/autonomous-unmanned-systems.html",
            "content_hash": "sha256:abc"}

    wall = dict(base, main_text="403 ERROR\nThe request could not be satisfied.\nRequest blocked.\n"
                                "We can't connect to the server for this app or website at this time.")
    ok, rule = validate_page(wall)
    assert ok is False and rule == "rule1_block_page"

    real = dict(base, main_text="Lockheed Martin's autonomous unmanned systems portfolio spans "
                                "air, land and sea platforms for defence customers worldwide.")
    ok, rule = validate_page(real)
    assert ok is True and rule is None


# ── special crawler must resume, not refetch (and not duplicate) ─────────────
def test_special_crawler_incremental_resume(tmp_path, monkeypatch):
    """Without this the in-memory `seen` set makes every restart refetch the whole site, and since
    _document mints a fresh uuid per fetch, each refetch writes a DUPLICATE row (measured: 503
    sipri.org rows for 431 distinct urls)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    h = CrawlHistory()
    try:
        done = "https://www.sipri.org/research"
        h.upsert(done, content_hash="sha256:abc", etag=None, last_modified=None,
                 status=200, fetched_at="2026-07-21T00:00:00Z")

        # exactly the predicate crawl.py._already_done uses
        def already_done(u):
            row = h.get(u)
            return bool(row and getattr(row, "content_hash", None))

        assert already_done(done), "collected page must be skipped on resume"

        # a page only RECORDED AS FAILED has no content_hash => must be retried, not skipped
        h.record_failure("https://www.sipri.org/events", status=None, category="timeout",
                         failed_at="2026-07-21T00:00:00Z")
        assert not already_done("https://www.sipri.org/events")

        # a never-seen page is obviously still crawled
        assert not already_done("https://www.sipri.org/brand-new")
    finally:
        h.close()


# ── deterministic document_id: collapse re-crawls, keep real revisions ───────
def test_version_id_dedupes_but_keeps_revisions():
    """Was uuid4() per fetch, so ON CONFLICT (document_id) could never fire and every re-crawl wrote
    a duplicate row (2,547 dupes over 1,946 urls). Keyed on content too, so genuine revisions still
    get their own row rather than overwriting history."""
    from crawler.models import version_id

    url = "https://www.mbda-systems.com/newsroom"
    # unchanged re-crawl => identical id => upsert collides => no duplicate row
    assert version_id(url, "sha256:aaa") == version_id(url, "sha256:aaa")
    # content changed => new id => new row => history preserved
    assert version_id(url, "sha256:aaa") != version_id(url, "sha256:bbb")
    # different page, same content (boilerplate) must not collide
    assert version_id(url, "sha256:aaa") != version_id(url + "/x", "sha256:aaa")
    # stable shape, and a missing hash must not explode
    vid = version_id(url, None)
    assert vid.startswith("doc_") and len(vid) == 20
    # deterministic across processes (no salt/randomness)
    assert version_id(url, "sha256:aaa") == "doc_" + __import__("hashlib").sha256(
        f"{url}\x00sha256:aaa".encode()).hexdigest()[:16]


# ── report must PROVE retries happened, not just label them "retryable" ──────
def test_report_surfaces_attempts_and_detail(tmp_path, monkeypatch):
    """The report showed only the reason, so a page that went through the whole
    C1 -> impersonate -> C3 ladder looked identical to one never retried. fail_count was already
    tracked in crawl_pages; it just was not selected."""
    from crawler import errors as e
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    h = CrawlHistory()
    try:
        url = "https://www.scrapingcourse.com/antibot-challenge"
        for _ in range(2):                       # two escalating attempts, as the engine records them
            h.record_failure(url, status=403, category=e.NEEDS_NETWORK_PATH,
                             failed_at="2026-07-22T04:51:33Z", crawl_run_id="run_X")
        row = h._conn.execute(
            "SELECT fail_count, error_category, last_status FROM crawl_pages "
            "WHERE canonical_url=?", (url,)).fetchone()
        assert row["fail_count"] == 2, "attempts must be counted"
        assert e.bucket_reason(row["error_category"]) == "retryable"

        # the explanation must say what was tried and what would change it
        detail = e.reason_detail(row["error_category"], row["last_status"])
        assert "C3" in detail and "residential" in detail
        # unknown reasons degrade to the raw reason rather than blowing up
        assert e.reason_detail("something_new") == "something_new"
        assert e.reason_detail(None) == "kept"
    finally:
        h.close()


# ── forward flow must not retain doc HTML in memory (the 2GB swap-thrash leak) ─
def test_collecting_client_drops_html_when_forwarding():
    """A 47-site keyword=false crawl grew collected[] to ~2GB of html over 11h — every page's full
    document was retained in RAM even though it was already POSTed to ingest and stored in Postgres,
    and the background batch keeps only counts. That exhausted RAM+swap and throughput fell
    4471->94 pages/hr. Retain the body ONLY for the pull flow (no forwarders)."""
    import os as _os
    from crawler.ingest_client import CollectingIngestClient, HttpIngestClient
    from crawler.models import Document

    def doc():
        return Document(url="https://x.com/a", content_hash="sha256:a",
                        fetched_at="2026-07-23T00:00:00Z", title="t", main_text="b",
                        html="<html>" + "x" * 5000 + "</html>", document_id="doc_a", source_id="x")

    pull = CollectingIngestClient()
    pull.send(doc())
    assert pull.collected[0]["document"] is not None      # inline return still works

    fwd = CollectingIngestClient(forwarders=[HttpIngestClient("http://localhost:9")])
    fwd.send(doc())
    assert fwd.collected[0]["document"] is None            # html not retained
    assert fwd.collected[0]["document_id"] == "doc_a"      # counts/bookkeeping kept
    assert "accepted" in fwd.collected[0]

    _os.environ["CRAWLER_RETAIN_INLINE_DOCS"] = "1"
    try:
        forced = CollectingIngestClient(forwarders=[HttpIngestClient("http://localhost:9")])
        forced.send(doc())
        assert forced.collected[0]["document"] is not None  # escape hatch
    finally:
        del _os.environ["CRAWLER_RETAIN_INLINE_DOCS"]


# ── tab pool: httpx concurrency decoupled from Chromium tabs (CRAWLER_TAB_POOL) ─
def test_tab_pool_borrow_return_invariants():
    """The pool lets N tab-less workers share M tabs so a 97%-httpx crawl needn't launch a tab per
    worker (measured ~10GB of idle tabs). The load-bearing invariant: a borrowed tab ALWAYS returns
    live, so the pool never shrinks and renders can't eventually deadlock."""
    from crawler.async_engine import AsyncEngine

    class FakeTab:
        def __init__(self, dead=False): self.dead = dead; self.blanked = False
        async def goto(self, url):
            if self.dead: raise RuntimeError("tab crashed")
            self.blanked = True

    async def go():
        eng = AsyncEngine.__new__(AsyncEngine)            # bypass __init__
        eng._tab_pool = asyncio.Queue()
        pool = [FakeTab(), FakeTab(), FakeTab()]
        for t in pool: eng._tab_pool.put_nowait(t)
        start = eng._tab_pool.qsize()
        # legacy mode (hold=None): every helper is a pass-through, pool untouched
        assert await eng._ensure_tab("worker-tab", None) == "worker-tab"
        assert eng._tab_pool.qsize() == start
        # pool mode: borrow caches into hold; a second _ensure_tab reuses it (one tab per item)
        hold = {"tab": None}
        b1 = await eng._ensure_tab(None, hold)
        assert b1 is hold["tab"] and eng._tab_pool.qsize() == start - 1
        assert await eng._ensure_tab(None, hold) is b1   # no second borrow
        # return a LIVE tab → parked, pool restored
        await eng._return_tab(b1)
        assert b1.blanked and eng._tab_pool.qsize() == start
        # a tab that DIED mid-use is replaced on return, not re-pooled: borrow one (pool -1),
        # kill it, return it → a fresh tab goes back (pool +1) so size is constant and no poison.
        eng._recycle = lambda t: _fresh()                # stub: hand back a fresh live tab
        b2 = await eng._ensure_tab(None, {"tab": None})
        assert eng._tab_pool.qsize() == start - 1
        b2.dead = True
        await eng._return_tab(b2)
        assert eng._tab_pool.qsize() == start            # size held despite the death
        assert b2 not in list(eng._tab_pool._queue)       # the dead one was NOT re-pooled

    async def _fresh():
        return _FreshTab()

    asyncio.run(go())


class _FreshTab:
    async def goto(self, url): pass


# ── captcha wall detection + reason (operator: resolve or flag as captcha limitation) ──────────
def test_captcha_wall_detection_and_reasons():
    """A rendered anti-bot interstitial must be flagged as a captcha limitation (a solver / better
    fingerprint can clear it), distinct from a generic IP block, and kept out of false positives."""
    from crawler import errors

    assert errors.is_captcha_wall("<title>Just a moment...</title>")
    assert errors.is_captcha_wall("please complete the security check")
    assert errors.is_captcha_wall("verifying you are human. cdn-cgi/challenge-platform")
    # A bare Turnstile WIDGET is NOT a wall: the .cf-turnstile div, its script, and the
    # cf-turnstile-response input all remain in the DOM AFTER the challenge is passed, so keying on
    # them flagged the "You bypassed the Cloudflare challenge!" success page as an uncleared wall.
    # An ACTIVE wall is identified by the interstitial text / orchestrate path, which is gone post-pass.
    assert not errors.is_captcha_wall('<div class="cf-turnstile" data-sitekey="x">')
    assert not errors.is_captcha_wall(
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
        '<body>You bypassed the Cloudflare challenge! :D</body>')
    # ...but that same widget on an ACTIVE interstitial still fires, via the text.
    assert errors.is_captcha_wall('<title>Just a moment...</title>'
                                  '<div class="cf-turnstile"></div>')
    # must NOT fire on real content that merely discusses these topics
    assert not errors.is_captcha_wall("Our radar can verify human operators at the border.")
    assert not errors.is_captcha_wall("<title>Defence News — Missiles</title>")
    # the reasons the flag uses are recognised by the report taxonomy
    for r in ("captcha_failed", "needs_captcha_solver"):
        assert errors.bucket_reason(r) == "retryable"
        assert "captcha" in errors.reason_detail(r).lower()
