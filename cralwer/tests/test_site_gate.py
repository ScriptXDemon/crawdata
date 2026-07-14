"""Site-level keyword gating (§A): probe depth 0..N, buffer until one page hits a
corpus keyword, then UNLOCK the whole site (flush buffer + keep everything after).
Zero hits through the probe window -> the site's buffered pages are discarded.

The gate DECISION lives in AsyncEngine._process; here we drive an AsyncEngine's
_finalize/_flush_buffer against in-memory fakes and replicate the branch to assert
the buffer/unlock/discard state machine without spinning a browser pool.
"""
import asyncio

from crawler import keywords as kwmod
from crawler.async_engine import AsyncEngine, JobCtx
from crawler.dedup import CrawlHistory
from crawler.fetcher import FetchResult
from crawler.harvest import HarvestedPage
from crawler.models import Document, Job
from crawler.seed import load_seed

SEED = load_seed()
CORPUS = kwmod.from_list(["DRDO", "artillery"])   # tiny corpus for the probe


def _job():
    return Job(job_id="sg", job_type="news", seed_urls=["https://ex.com/"],
               keywords=["x"], max_pages=100, max_depth=4,
               capture=["html", "text"])


def _doc(url, text):
    # Minimal Document; content_hash must differ per page so dedup doesn't collapse them.
    import hashlib
    h = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return Document(url=url, source_id="ex", title="t", main_text=text,
                    content_hash=h, html="<html></html>",
                    fetched_at="2026-01-01T00:00:00Z")


def _hp(url, depth):
    fr = FetchResult(url=url, final_url=url, status=200, kind="html",
                     text_html="<html></html>", fetched_at="2026-01-01T00:00:00Z")
    return HarvestedPage(url=url, depth=depth, fetch=fr, pdf_links=[],
                         image_candidates=[], media_candidates=[])


class _Ingest:
    def __init__(self):
        self.collected = []

    def send(self, doc):
        self.collected.append(doc)
        class _O:  # noqa
            accepted = True
        return _O()


def _engine(kp):
    from crawler.async_engine import HostLimiter
    eng = AsyncEngine.__new__(AsyncEngine)      # skip __init__ (no playwright)
    eng.history = CrawlHistory(":memory:")
    eng.kp = kp
    return eng


def _ctx(kp):
    ctx = JobCtx(_job(), SEED, kp, forward=False, l2_url=None)
    ctx.ingest = _Ingest()
    return ctx


async def _process_gate(eng, ctx, url, depth, text):
    """Replicate _process's site-gate branch (the part after build_document)."""
    from crawler import gate
    doc = _doc(url, text)
    hp = _hp(url, depth)
    fr = hp.fetch
    g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
    if g.reason == "stale_beyond_freshness_days":
        ctx.dropped_by_gate += 1
        return
    if ctx.unlocked:
        await eng._finalize(ctx, doc, hp, fr, url)
        return
    if g.keep:
        ctx.unlocked = True
        await eng._flush_buffer(ctx)
        await eng._finalize(ctx, doc, hp, fr, url)
        return
    if depth <= ctx.probe_depth and len(ctx.buffer) < ctx.probe_max_buffer:
        ctx.buffer.append((doc, hp, fr, url))
    else:
        ctx.dropped_by_gate += 1


def test_inner_page_hit_unlocks_whole_site():
    """Seed + depth-1 page have NO keyword; depth-2 page hits 'DRDO' -> all three kept."""
    async def run():
        eng = _engine(CORPUS)
        ctx = _ctx(CORPUS)
        await _process_gate(eng, ctx, "https://ex.com/", 0, "welcome home page")
        await _process_gate(eng, ctx, "https://ex.com/about", 1, "about us company")
        assert not ctx.unlocked and len(ctx.buffer) == 2   # buffered, nothing sent yet
        assert len(ctx.ingest.collected) == 0
        await _process_gate(eng, ctx, "https://ex.com/news", 2, "DRDO tests new system")
        assert ctx.unlocked
        # buffer flushed (2) + the hitting page (1) = 3 sent, none dropped
        assert len(ctx.ingest.collected) == 3, [d.url for d in ctx.ingest.collected]
        assert ctx.dropped_by_gate == 0
    asyncio.run(run())


def test_post_unlock_pages_kept_without_keyword():
    """Once unlocked, a page with zero keywords is still captured."""
    async def run():
        eng = _engine(CORPUS)
        ctx = _ctx(CORPUS)
        await _process_gate(eng, ctx, "https://ex.com/", 0, "artillery report")  # unlock at seed
        assert ctx.unlocked and len(ctx.ingest.collected) == 1
        await _process_gate(eng, ctx, "https://ex.com/x", 3, "totally unrelated cooking")
        assert len(ctx.ingest.collected) == 2   # kept despite no keyword
    asyncio.run(run())


def test_site_never_unlocks_is_discarded():
    """Zero corpus hits through the probe window -> buffered pages dropped at end-of-run."""
    async def run():
        eng = _engine(CORPUS)
        ctx = _ctx(CORPUS)
        await _process_gate(eng, ctx, "https://ex.com/", 0, "nothing relevant here")
        await _process_gate(eng, ctx, "https://ex.com/a", 1, "still nothing")
        # A deep page past probe_depth with no hit is dropped immediately, not buffered.
        await _process_gate(eng, ctx, "https://ex.com/deep", 3, "cooking recipes")
        assert not ctx.unlocked
        assert len(ctx.buffer) == 2 and ctx.dropped_by_gate == 1
        assert len(ctx.ingest.collected) == 0        # nothing ever sent
        # end-of-run discard (mirrors AsyncEngine.run's tail)
        if not ctx.unlocked and ctx.buffer:
            ctx.dropped_by_gate += len(ctx.buffer)
            ctx.buffer = []
        assert ctx.dropped_by_gate == 3 and len(ctx.ingest.collected) == 0
    asyncio.run(run())


def test_empty_corpus_unlocks_immediately():
    """No corpus -> every site is on-topic (fail-open): unlocked from the start."""
    ctx = _ctx(kwmod.from_list([]))
    assert ctx.unlocked is True


if __name__ == "__main__":
    test_inner_page_hit_unlocks_whole_site()
    test_post_unlock_pages_kept_without_keyword()
    test_site_never_unlocks_is_discarded()
    test_empty_corpus_unlocks_immediately()
    print("OK — site-gate self-check passed")
