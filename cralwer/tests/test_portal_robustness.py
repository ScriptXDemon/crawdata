"""Phase-2 portal-robustness tests: broadened attachment discovery, PDF table extraction
(graceful), and async form-search + DOM-change pagination. Offline (fake page)."""
import asyncio

from crawler import interaction_async, parse, pdfextract
from crawler.models import InteractionConfig, PaginateConfig, SearchConfig


# ── broadened tender-attachment discovery ────────────────────────────────────
def test_extract_pdf_links_broadened():
    html = """
      <a href="/docs/tender.pdf">pdf</a>
      <a href="/specs.docx">docx</a>
      <a href="/Download.aspx?id=123">gov attach</a>
      <a href="/getfile?doc=9">getfile</a>
      <a href="/page.html">not attach</a>
      <a href="/article">a page</a>
    """
    links = [l.lower() for l in parse.extract_pdf_links(html, "https://x.gov/")]
    assert "https://x.gov/docs/tender.pdf" in links
    assert "https://x.gov/specs.docx" in links
    assert any("download.aspx" in l for l in links)
    assert any("getfile" in l for l in links)
    assert not any(l.endswith("/page.html") for l in links)     # ordinary pages not captured
    assert not any(l.endswith("/article") for l in links)


# ── PDF table extraction degrades gracefully ─────────────────────────────────
def test_extract_tables_graceful():
    assert pdfextract.extract_tables(b"not a pdf at all") == []
    assert pdfextract.extract_tables(b"") == []


# ── has_any gate ─────────────────────────────────────────────────────────────
def test_has_any():
    assert not interaction_async.has_any(None)
    assert not interaction_async.has_any(InteractionConfig())
    cfg = InteractionConfig(search=SearchConfig(
        enabled=True, input_selector="#q", submit_selector="#go", keywords_to_search=["missile"]))
    assert interaction_async.has_any(cfg)


# ── fake async page for interaction logic ────────────────────────────────────
class FakePage:
    def __init__(self, bodies, urls=None):
        self._bodies = bodies
        self._urls = urls or [f"u{i}" for i in range(len(bodies))]
        self._i = 0
        self.url = self._urls[0]

    async def inner_text(self, sel):
        return self._bodies[self._i]

    async def content(self):
        return f"<html>{self._bodies[self._i]}</html>"

    async def click(self, sel, timeout=0):
        self._i = min(self._i + 1, len(self._bodies) - 1)
        self.url = self._urls[self._i]

    async def fill(self, sel, kw):
        pass

    async def press(self, sel, key):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_load_state(self, s, timeout=0):
        pass


def test_pagination_follows_ajax_then_stops():
    async def go():
        # URL never changes (ASP.NET __doPostBack), content changes then repeats → follow all, stop.
        p = FakePage(bodies=["page one", "page two longer", "page three longest"],
                     urls=["u", "u", "u"])
        cfg = PaginateConfig(enabled=True, next_selector=".next", max_pages=10,
                             wait_network_idle=False)
        texts = await interaction_async._run_pagination(p, cfg)
        assert len(texts) == 3          # followed all 3 despite URL never changing
    asyncio.run(go())


def test_search_accumulates_per_keyword():
    async def go():
        p = FakePage(bodies=["result A", "result B"])
        cfg = SearchConfig(enabled=True, input_selector="#q", submit_selector="#go",
                           keywords_to_search=["missile", "radar"], pause_ms=0)
        res = await interaction_async._run_search(p, cfg)
        assert len(res) == 2            # one result text per keyword (sync version overwrote)
    asyncio.run(go())


def test_search_press_enter_when_no_submit():
    async def go():
        p = FakePage(bodies=["r1"])
        cfg = SearchConfig(enabled=True, input_selector="#q", submit_selector="",
                           keywords_to_search=["missile"], pause_ms=0)
        res = await interaction_async._run_search(p, cfg)   # no submit_selector → press Enter path
        assert len(res) == 1
    asyncio.run(go())
