"""Unit tests for the mechanical building blocks."""
from crawler.canonicalize import canonicalize_url, same_site
from crawler.dedup import StoredPage, classify
from crawler.fetcher import FetchResult, Fetcher
from crawler.gate import GateResult, evaluate
from crawler.harvest import _link_is_relevant
from crawler.models import Job
from crawler.parse import extract_links_with_text, visible_text
from crawler import keywords as kwmod


# --- canonicalize --------------------------------------------------------
def test_canonicalize_collapses_equivalents():
    a = canonicalize_url("https://www.Janes.com/a/?utm_source=x#frag")
    b = canonicalize_url("https://janes.com/a/")
    assert a == b == "https://janes.com/a"


def test_same_site():
    assert same_site("https://idrw.org/x", "https://www.idrw.org/y")
    assert not same_site("https://idrw.org/x", "https://janes.com/y")


def test_visible_text_strips_nav_footer():
    html = ("<html><body>"
            "<nav>Home | About | Acme Defence Systems | Contact</nav>"
            "<article>The main story is about K9 Vajra guns.</article>"
            "<footer>Acme Defence Systems | Privacy</footer>"
            "</body></html>")
    text = visible_text(html)
    assert "K9 Vajra" in text
    assert "Acme Defence Systems" not in text


# --- gate ----------------------------------------------------------------
def _job(**kw):
    base = dict(job_id="j", job_type="news", seed_urls=["https://x"], keywords=["artillery"])
    base.update(kw)
    return Job(**base)


def test_gate_drops_no_corpus_keyword():
    # The gate matches the GLOBAL corpus (kp), not job.keywords.
    kp = kwmod.from_list(["howitzer", "artillery"])
    g = evaluate(_job(), "Cooking recipes", "no defence here", None, kp)
    assert not g.keep and g.reason == "no_keyword_match"


def test_binary_download_never_render_falls_through(monkeypatch):
    # Regression: a render_js job fetching a PDF must NOT fall through to the
    # Playwright renderer (which throws "Download is starting" and yields None).
    # A successful non-HTML download short-circuits before the render fallback.
    f = Fetcher(user_agent="x", delay_s=0, render_js=True)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    pdf = FetchResult(url="https://x/f.pdf", final_url="https://x/f.pdf", status=200,
                      kind="pdf", text_html=None, body_bytes=b"%PDF-1.4 ...",
                      fetched_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(f, "_http_fetch", lambda *a, **k: pdf)
    # if the guard regresses, _render_fetch runs and returns this sentinel error
    monkeypatch.setattr(f, "_render_fetch",
                        lambda *a, **k: FetchResult(url="https://x/f.pdf",
                            final_url="https://x/f.pdf", status=None,
                            fetched_at="", error="RENDER_SHOULD_NOT_RUN"))
    res = f.fetch("https://x/f.pdf")
    assert res.kind == "pdf" and res.body_bytes and res.error is None


def test_fetch_asset_never_renders(monkeypatch):
    # fetch_asset() is httpx-only — even on render_js it must not call the
    # renderer (assets are files, not pages).
    f = Fetcher(user_agent="x", delay_s=0, render_js=True)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    img = FetchResult(url="https://x/a.jpg", final_url="https://x/a.jpg", status=200,
                      kind="image", body_bytes=b"\xff\xd8jpeg", fetched_at="t")
    monkeypatch.setattr(f, "_http_fetch", lambda *a, **k: img)

    def _boom(*a, **k):
        raise AssertionError("_render_fetch must not run for an asset")
    monkeypatch.setattr(f, "_render_fetch", _boom)
    res = f.fetch_asset("https://x/a.jpg")
    assert res.kind == "image" and res.body_bytes


def test_asset_delay_is_lighter_than_page_delay():
    # Same-page asset sub-fetches throttle on a smaller delay than page loads.
    f = Fetcher(user_agent="x", delay_s=2.0)
    assert f.asset_delay_s < f.delay_s
    # env override respected
    import os
    os.environ["CRAWLER_ASSET_DELAY"] = "0.1"
    try:
        f2 = Fetcher(user_agent="x", delay_s=2.0)
        assert f2.asset_delay_s == 0.1
    finally:
        del os.environ["CRAWLER_ASSET_DELAY"]


def test_screenshot_wanted_flag_defaults_off():
    assert Fetcher(user_agent="x").screenshot_wanted is False
    assert Fetcher(user_agent="x", screenshot_wanted=True).screenshot_wanted is True


def test_fetch_assets_parallel_preserves_order_and_bytes(monkeypatch):
    # Concurrent asset download must return results in the SAME order as the
    # input URLs and the SAME bytes as a per-URL fetch (just faster).
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    bodies = {f"https://x/{i}.jpg": bytes([i]) * 4 for i in range(10)}

    def _fake_http(canon, *a, **k):
        return FetchResult(url=canon, final_url=canon, status=200, kind="image",
                           body_bytes=bodies[canon], fetched_at="t")
    monkeypatch.setattr(f, "_http_fetch", _fake_http)
    monkeypatch.setenv("CRAWLER_ASSET_WORKERS", "4")
    urls = list(bodies)
    out = f.fetch_assets(urls)
    assert [r.url for r in out] == urls              # order preserved
    assert [r.body_bytes for r in out] == [bodies[u] for u in urls]  # bytes intact


def test_fetch_assets_respects_robots(monkeypatch):
    # Even in the parallel path, a robots-blocked URL is not downloaded.
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False

    class _Robots:
        def allowed(self, url):
            return "blocked" not in url
    f._robots = _Robots()
    monkeypatch.setattr(f, "_http_fetch",
                        lambda canon, *a, **k: FetchResult(url=canon, final_url=canon,
                            status=200, kind="image", body_bytes=b"ok", fetched_at="t"))
    monkeypatch.setenv("CRAWLER_ASSET_WORKERS", "4")
    out = f.fetch_assets(["https://x/ok.jpg", "https://x/blocked.jpg"])
    assert out[0].body_bytes == b"ok"
    assert out[1].body_bytes is None and out[1].error == "blocked_by_robots"


def test_fetch_assets_empty_and_single():
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    assert f.fetch_assets([]) == []


def test_gate_keeps_on_corpus_hit():
    kp = kwmod.from_list(["artillery"])
    g = evaluate(_job(), "Generic artillery musings", "artillery in general", None, kp)
    assert g.keep and g.reason == "keyword_match"
    assert "artillery" in g.matched_keywords


def test_gate_empty_corpus_fails_open():
    # A misconfigured/empty corpus must keep every page, not silently drop all.
    kp = kwmod.from_list([])
    g = evaluate(_job(), "anything", "no keywords loaded", None, kp)
    assert g.keep and g.reason == "no_corpus_keep_all"


def test_gate_drops_stale_beyond_freshness():
    kp = kwmod.from_list(["artillery"])
    g = evaluate(_job(freshness_days=7), "artillery", "artillery news",
                 "2000-01-01T00:00:00Z", kp)
    assert not g.keep and g.reason == "stale_beyond_freshness_days"


# --- change detection ----------------------------------------------------
def test_classify_verdicts():
    assert classify(None, status=200, content_hash="sha256:a") == "new"
    stored = StoredPage("u", "sha256:a", '"strong"', None, False, 200)
    assert classify(stored, status=200, content_hash="sha256:a") == "unchanged"
    assert classify(stored, status=200, content_hash="sha256:b") == "changed"
    assert classify(stored, status=404, content_hash=None) == "gone"
    # a 304 is trusted only with a strong etag
    assert classify(stored, status=304, content_hash=None) == "unchanged"
    weak = StoredPage("u", "sha256:a", 'W/"weak"', None, False, 200)
    assert classify(weak, status=304, content_hash=None) == "changed"


# --- link-text relevance (opt-in) -----------------------------------------
def test_extract_links_with_text():
    html = ('<a href="/howitzer-news">Howitzer Programme Update</a>'
            '<a href="/careers">Careers</a>')
    pairs = extract_links_with_text(html, "https://x.example/")
    urls = {u: t for u, t in pairs}
    assert urls["https://x.example/howitzer-news"] == "Howitzer Programme Update"
    assert urls["https://x.example/careers"] == "Careers"
    assert _link_is_relevant(urls["https://x.example/howitzer-news"], ["howitzer"])
    assert not _link_is_relevant(urls["https://x.example/careers"], ["howitzer"])
