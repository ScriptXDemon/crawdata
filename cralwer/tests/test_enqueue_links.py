"""_enqueue_links: child-link expansion shared by the live C1 path AND the C3 CamoFox
fallback. Without it a site served entirely by CamoFox dead-ends at the seed page and
never reaches its max_pages budget (only the frontier drives the crawl)."""
import asyncio

from crawler import keywords as kwmod
from crawler.async_engine import AsyncEngine, JobCtx
from crawler.fetcher import FetchResult
from crawler.models import Job
from crawler.seed import load_seed

SEED = load_seed()


def _job(**kw):
    d = dict(job_id="e", job_type="news", seed_urls=["https://ex.com/"],
             max_pages=500, max_depth=4, same_domain_only=True, capture=["html", "text"])
    d.update(kw)
    return Job(**d)


def _eng():
    eng = AsyncEngine.__new__(AsyncEngine)     # skip __init__ (no playwright)
    eng.frontier = asyncio.Queue()
    return eng


def _ctx(job):
    return JobCtx(job, SEED, kwmod.from_list([]), forward=False, l2_url=None)


def _fr(html):
    return FetchResult(url="https://ex.com/", final_url="https://ex.com/", status=200,
                       kind="html", text_html=html, fetched_at="2026-01-01T00:00:00Z")


HTML = """<html><body>
  <a href="https://ex.com/a">A</a>
  <a href="https://ex.com/b">B</a>
  <a href="/c">C relative</a>
  <a href="https://other.com/x">offsite</a>
</body></html>"""


def test_camofox_page_enqueues_children():
    """A C3-served page's links land in the frontier (the fix) — this is what lets a
    fully-WAF-blocked site keep crawling past the seed into its max_pages budget."""
    eng = _eng()
    ctx = _ctx(_job())
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any(u.endswith("/a") for u in urls), urls
    assert any(u.endswith("/b") for u in urls), urls
    assert any(u.endswith("/c") for u in urls), urls          # relative resolved
    assert len(urls) >= 3, urls


def test_respects_max_depth():
    """At the depth limit, no children are enqueued (bounds the crawl)."""
    eng = _eng()
    ctx = _ctx(_job(max_depth=2))
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=2)   # depth == max_depth
    assert eng.frontier.empty()


def test_respects_budget():
    """No enqueue once the page budget is spent (max_pages bound holds for C3 too)."""
    eng = _eng()
    job = _job(max_pages=1)
    ctx = _ctx(job)
    ctx.budget_used = 1                       # budget exhausted
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=0)
    assert eng.frontier.empty()


if __name__ == "__main__":
    test_camofox_page_enqueues_children()
    test_respects_max_depth()
    test_respects_budget()
    print("OK — enqueue_links self-check passed")
