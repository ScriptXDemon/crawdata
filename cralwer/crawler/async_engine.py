"""Async multi-tab browser-pool crawl engine — saturates the machine.

The sync engine (pipeline.run_job) crawls ONE site through ONE tab, sequentially — ~1 core,
~250MB. This engine runs ONE shared pool of W Chromium browsers, each with T persistent tabs
(``playwright.async_api``), fed by a shared host-aware frontier, so W*T pages render
concurrently across many hosts. Per-host politeness (HostLimiter) means 140 tabs still never
hammer a single site.

Governing rule: **one event loop, one thread.** Every Playwright object lives on that loop;
only plain data (FetchResult/Document) ever crosses to ``asyncio.to_thread``. Shared mutable
state (the frontier, per-job seen/counters/budget, the shared CrawlHistory) is touched ONLY
inline in coroutines on the loop thread — cooperative scheduling makes any no-``await`` section
atomic, so almost no locks are needed.

The per-page work replicates pipeline.run_job step-for-step (gate → self-dedup → same-run hash
dedup → enrich → send), so emitted docs + counters match the sync engine; only the driver changed.

Opt in with ``CRAWLER_ASYNC_ENGINE=1``; ``crawler_api.app`` routes /v1/crawl/batch here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import errors, extract, gate, interaction_async, parse
from .canonicalize import canonicalize_url
from .dedup import CrawlHistory, classify
from .fetcher import (
    _BROWSER_UA_FALLBACK, FetchResult, Fetcher, _is_tls_or_dns_error, _now_iso, _with_www,
)
from .harvest import HarvestedPage, _allowed_offsite
from .ingest_client import CollectingIngestClient, HttpIngestClient
from .models import Job
from .resolver import build_matcher
from .robots import RobotsCache
from .seed import Seed, load_seed
from . import config

log = logging.getLogger("async_engine")


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


def _env_float(k: str, d: float) -> float:
    return float(os.environ.get(k, str(d)))


def _proxy_config() -> dict | None:
    """Playwright proxy= dict from CRAWLER_PROXY_URL, or None (direct connection).

    The actual fix for IP-blocked hosts (war.gov etc.): route Chromium through a
    residential/mobile proxy gateway, which rotates the exit IP per request on the
    provider side. Datacenter proxies DON'T help — they're on the same blocklists as
    a server IP; the provider MUST be residential/mobile (BrightData/Oxylabs/IPRoyal/…).

    Format: CRAWLER_PROXY_URL=http://user:pass@gateway.provider.com:7777
    (scheme http or socks5; auth optional). Returns the dict Playwright wants:
      {server, username?, password?}
    """
    raw = os.environ.get("CRAWLER_PROXY_URL", "").strip()
    if not raw:
        return None
    p = urlsplit(raw)
    if not p.hostname:
        log.warning("CRAWLER_PROXY_URL set but unparseable: %r — ignoring", raw)
        return None
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    cfg: dict = {"server": server}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def _feed_items(feed_url: str, timeout_s: float) -> list[dict]:
    """Fetch an RSS/Atom feed (un-gated) and return its items as dicts:
    {link, title, summary, published}. The fallback for WAF-blocked HTML — feeds
    are rarely bot-challenged, and the feed body ALREADY carries title+summary, so
    when the item pages are themselves blocked we emit the feed content directly.
    Handles RSS (<item>) and Atom (<entry>). [] on any error.
    ponytail: no feedparser dep; stdlib ElementTree covers RSS+Atom.
    """
    import httpx
    from xml.etree import ElementTree as ET

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA_FALLBACK,
                                   "Accept": "application/rss+xml, application/xml, */*"}) as c:
            r = c.get(feed_url)
        if r.status_code >= 400 or not r.content.strip():
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []

    def _txt(el, *names):
        for n in names:
            for ch in el:
                if ch.tag.rsplit("}", 1)[-1].lower() == n:
                    return (ch.get("href") or ch.text or "").strip()
        return ""

    items: list[dict] = []
    seen: set[str] = set()
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        link = _txt(el, "link", "id")
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        items.append({
            "link": link,
            "title": _txt(el, "title"),
            "summary": _txt(el, "description", "summary", "content"),
            "published": _txt(el, "pubdate", "published", "updated"),
        })
    return items


# Archive.org injects a nav toolbar into every replayed page; strip it so we
# extract the ORIGINAL page's text, not the archive chrome.
_WB_TOOLBAR = re.compile(rb"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?"
                         rb"<!--\s*END WAYBACK TOOLBAR INSERT\s*-->", re.DOTALL | re.IGNORECASE)


def _wayback_snapshot(url: str, timeout_s: float) -> dict | None:
    """Fetch the Wayback Machine's newest archived copy of `url` — a WAF-FREE
    read of a live-blocked page. archive.org replays the original HTML, so we get
    real content Akamai/Cloudflare would 403 on the live host.

    Returns {html, snapshot_url, timestamp} or None (no snapshot / any error).
    Two hops: availability API for the snapshot URL, then fetch the replayed page
    with `id_` raw mode (no toolbar rewrite), then belt-and-suspenders toolbar strip.
    ponytail: stdlib httpx + one regex; no archive.org SDK for two GETs.
    """
    import httpx

    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA_FALLBACK}) as c:
            avail = c.get("http://archive.org/wayback/available", params={"url": url})
            snap = (avail.json().get("archived_snapshots", {}) or {}).get("closest")
            if not snap or not snap.get("available") or snap.get("status") != "200":
                return None
            ts = snap.get("timestamp", "")
            snap_url = snap["url"]
            # `<ts>id_/<url>` = raw archived bytes without the injected toolbar/link-rewrites.
            raw_url = snap_url.replace(f"/web/{ts}/", f"/web/{ts}id_/", 1) if ts else snap_url
            r = c.get(raw_url)
        if r.status_code >= 400 or not r.content.strip():
            return None
        html = _WB_TOOLBAR.sub(b"", r.content).decode(r.encoding or "utf-8", errors="ignore")
    except Exception:
        return None
    return {"html": html, "snapshot_url": snap_url, "timestamp": ts}


def _api_items(api_url: str, timeout_s: float) -> list[dict]:
    """Fetch an official JSON API (DVIDS) → items {link, title, summary, published}.

    The SANCTIONED path for WAF-blocked DoD sites: same news/press content that
    renders on war.gov/defense.gov, served WAF-free as JSON. DVIDS needs a free
    key (DVIDS_API_KEY env) appended as ?api_key=. Returns [] on any error / no key.
    Shape: DVIDS returns {"results":[{id,title,short_description,date_published,url}]}.
    ponytail: DVIDS-shaped for now; generalize the field map only if a 2nd JSON API appears.
    """
    import httpx

    key = os.environ.get("DVIDS_API_KEY", "").strip()
    if "dvidshub.net" in api_url and not key:
        return []                       # DVIDS search 400s without a key — don't waste the call
    params = {"api_key": key} if key else {}
    # Ask DVIDS for exactly the fields we map, newest first.
    if "dvidshub.net" in api_url:
        params.update({"fields": "id,title,short_description,date_published,url",
                       "sort": "date", "sortdir": "desc", "max_results": "50"})
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": _BROWSER_UA_FALLBACK,
                                   "Accept": "application/json"}) as c:
            r = c.get(api_url, params=params)
        if r.status_code >= 400:
            return []
        data = r.json()
    except Exception:
        return []

    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []
    items: list[dict] = []
    for it in results:
        if not isinstance(it, dict):
            continue
        link = (it.get("url") or it.get("link") or "").strip()
        if not link.startswith("http"):
            continue
        items.append({
            "link": link,
            "title": (it.get("title") or "").strip(),
            "summary": (it.get("short_description") or it.get("description")
                        or it.get("summary") or "").strip(),
            "published": (it.get("date_published") or it.get("published") or "").strip(),
        })
    return items


# ── work item + per-job context ──────────────────────────────────────────────

@dataclass
class WorkItem:
    url: str
    depth: int
    ctx: "JobCtx"
    retries: int = 0        # in-run retry count (transient failures get one requeue)


class JobCtx:
    """Per-job state carried on every WorkItem. Mutated ONLY on the loop thread."""

    def __init__(self, job: Job, seed: Seed, matcher, forward: bool, l2_url: str | None) -> None:
        self.job = job
        self.seed = seed
        self.matcher = matcher

        # Forwarders exactly like crawler_api.app._run: default ingest + optional L2.
        forwarders: list[HttpIngestClient] = []
        self.forwarded_targets: list[str] = []
        if forward:
            forwarders.append(HttpIngestClient())
            self.forwarded_targets.append(config.INGEST_BASE_URL)
        if l2_url:
            forwarders.append(HttpIngestClient(base_url=l2_url))
            self.forwarded_targets.append(l2_url)
        self.ingest = CollectingIngestClient(forwarders=forwarders)

        # Asset-only fetcher: never renders (the pool renders); screenshots are captured
        # inline in _render_page and set on FetchResult.screenshot_png.
        caps = seed.capture_defaults
        self.fetcher = Fetcher(
            user_agent=caps["user_agent"], timeout_s=caps.get("timeout_seconds", 30),
            render_js=False, respect_robots=False, screenshot_wanted=False,
        )

        self.seed_domains = {canonicalize_url(s) for s in job.seed_urls}
        self.seen: set[str] = set()
        self.seen_hashes: set[str] = set()
        self.budget_used = 0
        self.done = False

        # counters (mirror pipeline.JobResult → the API summary shape)
        self.fetched = self.kept = self.sent = self.accepted = self.rejected = self.errors = 0
        self.not_modified = self.dropped_by_gate = self.skipped_unchanged = self.skipped_duplicate = 0
        self.gate_reasons: dict[str, int] = {}
        self.errors_by_reason: dict[str, int] = {}   # typed why-did-it-fail breakdown
        self.trap_skipped = 0                         # URLs dropped by trap heuristics
        # Phase-3 trap state: per (host,path) count of query-only variants seen (calendar/facet cap).
        self.query_variants: dict[str, int] = {}

    def reserve(self) -> bool:
        """Budget gate at dequeue — bounds total render attempts to max_pages (+in-flight)."""
        if self.budget_used >= self.job.max_pages:
            self.done = True
            return False
        self.budget_used += 1
        return True

    def unreserve(self) -> None:
        """Give a page's budget back when a transient failure is requeued for one more try.
        Loop-thread-only mutation with no await between fail-detect and requeue → race-safe."""
        if self.budget_used > 0:
            self.budget_used -= 1

    def has_budget(self) -> bool:
        return self.budget_used < self.job.max_pages

    def bump_reason(self, reason: str) -> None:
        self.gate_reasons[reason] = self.gate_reasons.get(reason, 0) + 1

    def bump_error(self, reason: str) -> None:
        self.errors += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def summary(self) -> dict:
        return {
            "fetched": self.fetched, "not_modified_304": self.not_modified,
            "dropped_by_gate": self.dropped_by_gate, "skipped_unchanged": self.skipped_unchanged,
            "skipped_duplicate": self.skipped_duplicate, "kept": self.kept,
            "sent": self.sent, "accepted": self.accepted, "rejected": self.rejected,
            "errors": self.errors, "errors_by_reason": self.errors_by_reason,
            "trap_skipped": self.trap_skipped, "gate_reasons": self.gate_reasons,
            "forwarded_to": self.forwarded_targets,
        }


# ── per-host politeness (shared across the whole pool) ───────────────────────

class HostLimiter:
    """≤ max_conc concurrent AND ≥ min_delay apart, per hostname. The politeness guarantee
    that lets 140 tabs run without hammering any single site."""

    def __init__(self, max_conc: int, min_delay: float, robots: RobotsCache | None = None,
                 base_timeout_s: float = 30.0) -> None:
        self.max_conc = max_conc
        self.min_delay = min_delay
        self.robots = robots
        self.base_timeout_s = base_timeout_s
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._delay: dict[str, float] = {}
        self._next_ok: dict[str, float] = {}
        self._inflight: dict[str, int] = {}
        self._timeout: dict[str, float] = {}      # per-host patient timeout (ms), seeded in _ensure
        self.peak_inflight: dict[str, int] = {}   # for verification (must stay ≤ max_conc)
        # Hosts forced into careful-mode by a job with careful=True (not just .gov/.mil suffix).
        self.force_careful: set[str] = set()

    def timeout_ms(self, host: str) -> float:
        """Per-host patient timeout in ms (bigger for careful hosts; ratchets on TIMEOUT)."""
        return self._timeout.get(host, self.base_timeout_s) * 1000

    def bump_timeout(self, host: str) -> None:
        """A slow gov origin timed out — extend its patience (bounded) before the breaker counts it."""
        cap = _env_float("CRAWLER_MAX_TIMEOUT_S", 120.0)
        cur = self._timeout.get(host, self.base_timeout_s)
        self._timeout[host] = min(cur * 1.5, cap)

    async def _ensure(self, host: str) -> None:
        if host not in self._sem:
            # Created synchronously before any await → no race creating two semaphores.
            # Careful hosts (.gov/.mil suffix OR a job that set careful=True): concurrency 1,
            # a slower delay floor, and a more patient timeout — "quiet quiet."
            careful = errors.is_careful_host(host) or host in self.force_careful
            self._sem[host] = asyncio.Semaphore(1 if careful else self.max_conc)
            self._delay[host] = max(self.min_delay, _env_float("CRAWLER_CAREFUL_DELAY_S", 5.0)) \
                if careful else self.min_delay
            factor = _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0) if careful else 1.0
            self._timeout[host] = self.base_timeout_s * factor
            self._next_ok[host] = 0.0
            self._inflight[host] = 0
            self.peak_inflight[host] = 0
            if self.robots:
                try:  # robots crawl_delay is a blocking httpx fetch → off-loop
                    d = await asyncio.to_thread(self.robots.crawl_delay, f"https://{host}/")
                    if d:
                        self._delay[host] = max(self.min_delay, float(d))
                except Exception:
                    pass

    def cooldown(self, host: str, seconds: float) -> None:
        """429/503 backpressure: push this host's next-allowed time out by *seconds* (capped).
        Reuses the same _next_ok gate slot() already enforces — no new queueing machinery.
        ponytail: a long cooldown idles the ≤max_conc tabs holding that host's semaphore;
        fine at current scale — raise a circuit breaker if one host dominates a batch."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        cap = _env_float("CRAWLER_COOLDOWN_CAP_S", 300.0)
        target = loop.time() + min(max(seconds, 0.0), cap)
        self._next_ok[host] = max(self._next_ok.get(host, 0.0), target)

    @asynccontextmanager
    async def slot(self, url: str):
        host = (urlsplit(url).hostname or "").lower()
        await self._ensure(host)
        async with self._sem[host]:
            self._inflight[host] += 1
            self.peak_inflight[host] = max(self.peak_inflight[host], self._inflight[host])
            loop = asyncio.get_running_loop()
            wait = self._next_ok[host] - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok[host] = loop.time() + self._delay[host]
            try:
                yield
            finally:
                self._inflight[host] -= 1


# ── async render (1:1 port of Fetcher._render_fetch core path) ───────────────

# Giant-binary extensions aborted before bytes flow — the render path can't cap response
# size, so we refuse the classic huge-download classes at the network layer instead.
_BLOCK_EXT = "**/*.{zip,exe,dmg,iso,msi,mp4,mkv,avi,mov,wmv,flv,gz,tgz,7z,rar,bin,pkg,deb,rpm}"


async def _abort_route(route) -> None:
    try:
        await route.abort()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


async def _render_page(tab, url: str, timeout_ms: int, capture: list[str],
                       interaction=None) -> FetchResult:
    """Render one URL on a persistent tab; snapshot to a plain FetchResult on the loop.
    Render-everything mode: always renders, no httpx/conditional (dedup is by content_hash).
    If `interaction` is set (form-search/pagination/scroll), run it after load — that's what
    lets the pool search a gov tender portal."""
    networkidle_ms = _env_int("CRAWLER_NETWORKIDLE_MS", 4000)
    settle_ms = _env_int("CRAWLER_RENDER_SETTLE_MS", 500)

    nav_url = url
    try:
        resp = await tab.goto(nav_url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:
        www = _with_www(url)
        if not _is_tls_or_dns_error(exc) or www == url:
            return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                               error=f"nav:{exc}",
                               reason=errors.classify_failure(None, f"nav:{exc}"))
        try:
            await tab.wait_for_timeout(500)
            resp = await tab.goto(www, timeout=timeout_ms, wait_until="domcontentloaded")
            nav_url = www
        except Exception as exc2:
            return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                               error=f"nav:{exc2}",
                               reason=errors.classify_failure(None, f"nav:{exc2}"))

    try:  # networkidle exits early the instant the network idles; cap bounds non-idling sites
        await tab.wait_for_load_state("networkidle", timeout=networkidle_ms)
    except Exception:
        pass
    try:
        await tab.wait_for_timeout(settle_ms)
        html = await tab.content()
    except Exception as exc:
        return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                           error=f"content:{exc}",
                           reason=errors.classify_failure(None, f"content:{exc}"))

    inner_text = None
    if interaction_async.has_any(interaction):
        try:                       # form-search / pagination / scroll — never fatal
            html, inner_text = await interaction_async.run_interactions(tab, interaction)
        except Exception:
            pass

    # Post-hoc DOM cap — Playwright can't cap response size, so a giant page is only
    # measurable after content(). ponytail: memory is already spent here; this bounds
    # a runaway DOM (Chromium itself bounds the tab) rather than the wire response.
    if len(html) > _env_int("CRAWLER_MAX_HTML_BYTES", 20_971_520):
        return FetchResult(url=url, final_url=nav_url, status=(resp.status if resp else None),
                           fetched_at=_now_iso(), error="too_large", reason=errors.TOO_LARGE)

    status = resp.status if resp else None
    reason = errors.http_reason(status) if status and status >= 400 else None
    retry_after = None
    if status in (429, 503) and resp:
        try:
            retry_after = errors.parse_retry_after(await resp.header_value("retry-after"))
        except Exception:
            retry_after = None
    shot = None
    if "screenshot" in capture:
        try:
            shot = await tab.screenshot(full_page=True)
        except Exception:
            shot = None
    try:
        final_url = tab.url
    except Exception:
        final_url = nav_url

    return FetchResult(url=url, final_url=final_url, status=status, content_type="text/html",
                       kind="html", text_html=html, inner_text=inner_text, screenshot_png=shot,
                       tier=1, fetched_at=_now_iso(), reason=reason, retry_after_s=retry_after)


# ── the engine ───────────────────────────────────────────────────────────────

class AsyncEngine:
    def __init__(self, W: int, T: int, host: HostLimiter, seed: Seed, matcher,
                 robots: RobotsCache | None = None) -> None:
        self.W = W
        self.T = T
        self.host = host
        self.seed = seed
        self.matcher = matcher
        self.robots = robots
        self._pw = None
        self.browsers: list[tuple] = []     # (browser, context)
        self.tabs: list = []
        self.history: CrawlHistory | None = None
        self.frontier: asyncio.Queue | None = None
        self._last_progress = 0.0
        # Circuit breaker: consecutive HARD failures (DNS/refused/SSL) per host; a host
        # crossing the threshold goes into dead_hosts and its remaining URLs are skipped.
        self.host_fails: dict[str, int] = {}
        self.dead_hosts: set[str] = set()
        # Requeued-transient count — guards _drain so a delayed retry isn't lost.
        # Mutated only on the loop thread.
        self._pending_retries = 0
        self._inflight_items = 0          # items dequeued but not yet task_done

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
        ua = self.seed.capture_defaults["user_agent"]
        proxy = _proxy_config()
        if proxy:
            log.info("proxy enabled via CRAWLER_PROXY_URL: %s", proxy["server"])
        for _ in range(self.W):
            b = await self._pw.chromium.launch(headless=headless, args=["--no-sandbox"])
            ctx = await b.new_context(user_agent=ua, viewport={"width": 1920, "height": 1080},
                                      locale="en-US", timezone_id="America/New_York",
                                      extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                                      proxy=proxy)
            # Session/cookies: the browser already domain-scopes its cookie jar (host A's cookie
            # is never SENT to host B), and a gov search flow (fill→submit→paginate) runs on ONE
            # tab in this ONE context in a single _render_page call — so its session persists
            # exactly where a portal needs it. ponytail: full per-host BrowserContext isolation
            # (for a session-required MULTI-page BFS spread across the W browsers) is a
            # pool rearchitecture for a rare case — add host→browser affinity if it ever bites.
            try:
                await ctx.route(_BLOCK_EXT, _abort_route)   # refuse giant downloads at the wire
            except Exception:
                pass
            self.browsers.append((b, ctx))
            for _ in range(self.T):
                self.tabs.append(await ctx.new_page())
        log.info("engine started: %d browsers x %d tabs = %d workers",
                 self.W, self.T, len(self.tabs))

    async def run(self, jobs: list[Job], forward: bool, l2_url: str | None) -> list[JobCtx]:
        self.history = CrawlHistory()                       # bound to this (engine) thread
        self.frontier = asyncio.Queue()
        ctxs = [JobCtx(j, self.seed, self.matcher, forward, l2_url) for j in jobs]
        for c in ctxs:
            for s in c.job.seed_urls:
                cu = canonicalize_url(s)
                if c.job.careful:               # manual careful=True → force this host careful
                    h = (urlsplit(cu).hostname or "").lower()
                    if h:
                        self.host.force_careful.add(h)
                if cu not in c.seen:
                    c.seen.add(cu)
                    self.frontier.put_nowait(WorkItem(cu, 0, c))

        loop = asyncio.get_running_loop()
        self._last_progress = loop.time()
        workers = [asyncio.create_task(self._worker(t)) for t in self.tabs]
        wall = _env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 1800)
        idle = _env_int("CRAWLER_ENGINE_IDLE_S", 120)
        try:
            await asyncio.wait_for(self._drain(idle), timeout=wall)
        except asyncio.TimeoutError:
            log.warning("engine wall-clock timeout (%ss) — terminating batch", wall)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.history.close()
        return ctxs

    async def _drain(self, idle_s: int) -> None:
        """Return when the frontier is fully drained (nothing queued, nothing in flight, no
        retry pending), or after idle_s with no completed page. Counters are all mutated on
        the loop thread, so a delayed requeue can never slip past this check."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.5)
            if (self.frontier.qsize() == 0 and self._inflight_items == 0
                    and self._pending_retries == 0):
                return
            if loop.time() - self._last_progress > idle_s:
                log.warning("engine idle for %ss — terminating batch", idle_s)
                return

    async def _worker(self, tab) -> None:
        while True:
            item = await self.frontier.get()
            self._inflight_items += 1
            try:
                tab = await self._process(item, tab)
            except Exception:
                log.exception("worker page failed: %s", getattr(item, "url", "?"))
            finally:
                self._inflight_items -= 1
                self.frontier.task_done()

    def _schedule_retry(self, item: WorkItem, delay: float) -> None:
        """Requeue a transient failure after *delay* seconds (one more attempt)."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries + 1))

    def _do_requeue(self, w: WorkItem) -> None:
        self._pending_retries = max(0, self._pending_retries - 1)
        if self.frontier is not None:
            try:
                self.frontier.put_nowait(w)
            except Exception:
                pass

    async def _emit_items(self, ctx: "JobCtx", items: list[dict]) -> int:
        """Turn feed/API items ({link,title,summary,published}) into synthetic HTML
        pages and run each through the exact build_document → gate → dedup → send
        path as a real fetch. Shared by the feed and API fallbacks. Returns docs SENT."""
        sent = 0
        for it in items:
            url = canonicalize_url(it["link"])
            if url in ctx.seen or not ctx.reserve():
                continue
            ctx.seen.add(url)
            title = it["title"] or url
            summary = it["summary"] or ""
            # Synthetic page: title + summary as minimal HTML — build_document parses
            # this the same as a real page body. Published date carried via fixture hint.
            html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>{summary}</p></body></html>"
            fr = FetchResult(url=url, final_url=url, status=200, content_type="text/html",
                             kind="html", text_html=html, tier=1, fetched_at=_now_iso(),
                             published_hint=it["published"] or None, from_fixture=True)
            hp = HarvestedPage(url=url, depth=1, fetch=fr, pdf_links=[],
                               image_candidates=[], media_candidates=[])
            doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                          ctx.matcher, ctx.fetcher, False)
            if doc is None:
                ctx.bump_reason("no_main_text")
                continue
            g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.entities_detected,
                              doc.published_at)
            ctx.bump_reason(g.reason)
            if not g.keep:
                ctx.dropped_by_gate += 1
                continue
            if doc.content_hash in ctx.seen_hashes:
                ctx.skipped_duplicate += 1
                continue
            ctx.seen_hashes.add(doc.content_hash)
            ctx.kept += 1
            outcome = await asyncio.to_thread(ctx.ingest.send, doc)
            ctx.sent += 1
            ctx.accepted += 1 if outcome.accepted else 0
            ctx.rejected += 0 if outcome.accepted else 1
            sent += 1
        return sent

    async def _try_api_fallback(self, item: WorkItem, host: str) -> int:
        """Blocked host → emit its registry official-API (DVIDS) items as documents.
        The sanctioned, freshest path — tried before Wayback/feed. Returns docs SENT."""
        ctx = item.ctx
        src = ctx.seed.source_for_url(item.url)
        if src is None or not src.api_url:
            return 0
        timeout_s = float(ctx.seed.capture_defaults.get("timeout_seconds", 30))
        items = await asyncio.to_thread(_api_items, src.api_url, timeout_s)
        return await self._emit_items(ctx, items)

    async def _try_feed_fallback(self, item: WorkItem, host: str) -> int:
        """Blocked host → emit its registry RSS/Atom feed items as documents.
        The feed body carries title+summary+date, so we emit it directly rather than
        re-crawl the (same-host, blocked) item pages. Returns docs SENT."""
        ctx = item.ctx
        src = ctx.seed.source_for_url(item.url)
        if src is None or not src.feed_url:
            return 0
        timeout_s = float(ctx.seed.capture_defaults.get("timeout_seconds", 30))
        items = await asyncio.to_thread(_feed_items, src.feed_url, timeout_s)
        return await self._emit_items(ctx, items)

    async def _try_wayback_fallback(self, item: WorkItem, host: str) -> bool:
        """Blocked host → serve the Wayback Machine's archived copy of THIS url.

        Unlike the feed fallback (which emits many synthetic items), this recovers
        the actual blocked page's real content from archive.org and runs it through
        the same build_document → gate → dedup → send path. Returns True if a doc
        was SENT. Budget is already reserved for this item at dequeue, so no reserve()."""
        ctx = item.ctx
        timeout_s = float(ctx.seed.capture_defaults.get("timeout_seconds", 30))
        snap = await asyncio.to_thread(_wayback_snapshot, item.url, timeout_s)
        if not snap:
            return False
        fr = FetchResult(url=item.url, final_url=snap["snapshot_url"], status=200,
                         content_type="text/html", kind="html", text_html=snap["html"],
                         tier=1, fetched_at=_now_iso(), from_fixture=True)
        hp = HarvestedPage(url=item.url, depth=item.depth, fetch=fr, pdf_links=[],
                           image_candidates=[], media_candidates=[])
        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.matcher, ctx.fetcher, False)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return False
        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.entities_detected,
                          doc.published_at)
        ctx.bump_reason(g.reason)
        if not g.keep:
            ctx.dropped_by_gate += 1
            return False
        if doc.content_hash in ctx.seen_hashes:
            ctx.skipped_duplicate += 1
            return False
        ctx.seen_hashes.add(doc.content_hash)
        ctx.kept += 1
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        ctx.accepted += 1 if outcome.accepted else 0
        ctx.rejected += 0 if outcome.accepted else 1
        log.info("wayback_fallback job=%s url=%s snapshot=%s",
                 ctx.job.job_id, item.url, snap["timestamp"])
        return True

    async def _on_failure(self, item: WorkItem, fr: FetchResult, tab, host: str):
        """Dispatch a failed fetch per the errors policy table. Returns (fr, tab):
        a SUCCESSFUL fr (disguise worked) → caller proceeds down the success path; an fr still
        >=400 / None (or None) → caller counts it and moves on."""
        ctx = item.ctx
        reason = fr.reason or errors.classify_failure(fr.status, fr.error) or errors.OTHER
        status = fr.status
        policy = errors.policy_for_status(status) if status and status >= 400 else None

        # A timeout on a (slow gov) origin → extend its patience before the breaker counts it.
        if reason == errors.TIMEOUT:
            self.host.bump_timeout(host)

        # Any 4xx → ONE human-like browser retry: browser UA + real Accept-Language/locale
        # headers, like a person hitting the page. Careful hosts (.gov/.mil) are included —
        # we still make exactly one polite attempt, then back off. A survivor is retagged
        # NEEDS_NETWORK_PATH (below) so it routes to the different-network-path plan
        # (residential proxy / real headless session / source API), not another retry.
        blocked_4xx = bool(status and 400 <= status < 500)
        if blocked_4xx:
            try:
                await tab.set_extra_http_headers({
                    "User-Agent": _BROWSER_UA_FALLBACK,
                    "Accept-Language": "en-US,en;q=0.9",
                })
                async with self.host.slot(item.url):
                    retried = await _render_page(tab, item.url, self.host.timeout_ms(host),
                                                 ctx.job.capture, ctx.job.interaction)
            except Exception as e:
                retried = FetchResult(url=item.url, final_url=item.url, status=None,
                                      fetched_at=_now_iso(), error=f"render:{e}",
                                      reason=errors.RENDER_CRASH)
                tab = await self._recycle(tab)
            finally:
                try:
                    await tab.set_extra_http_headers({})
                except Exception:
                    pass
            if retried.status and 200 <= retried.status < 400 and not retried.error:
                return retried, tab                 # human-like retry worked → success path
            fr = retried
            status = fr.status
            # Still 4xx after a full human-like session. Before giving up, try the
            # source's un-gated RSS/Atom feed (if the registry has one): crawl its
            # item links instead of the blocked HTML. Only when there's no feed (or
            # it yields nothing) do we tag needs_network_path for the proxy/API plan.
            if status and 400 <= status < 500:
                # WAF-free content ladder — try the cheapest/fullest path that lands:
                # 1st: official JSON API (DVIDS) — sanctioned, freshest, full bodies.
                api_sent = await self._try_api_fallback(item, host)
                if api_sent:
                    ctx.bump_error(errors.NEEDS_NETWORK_PATH)  # record the block; API was the workaround
                    log.info("api_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, api_sent)
                    return None, tab
                # 2nd: Wayback Machine — this exact page's real content from archive.org.
                if await self._try_wayback_fallback(item, host):
                    ctx.bump_error(errors.SERVED_FROM_ARCHIVE)  # routed around the block, not a failure
                    return None, tab
                # 3rd: RSS/Atom feed — broader fallback (summaries of the source's items).
                sent = await self._try_feed_fallback(item, host)
                if sent:
                    ctx.bump_error(errors.NEEDS_NETWORK_PATH)  # record the block; feed was the workaround
                    log.info("feed_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, sent)
                    return None, tab                 # blocked HTML abandoned; feed docs already sent
                reason = errors.NEEDS_NETWORK_PATH
                fr.reason = reason
            else:
                reason = fr.reason or errors.classify_failure(fr.status, fr.error) or errors.OTHER
            policy = errors.policy_for_status(status) if status and status >= 400 else None

        # Persist every failure so 'gone' and 'retry next run' survive across runs.
        if self.history is not None:
            self.history.record_failure(item.url, status=status, category=reason,
                                        failed_at=fr.fetched_at or _now_iso())

        # 404/410 → permanently gone (wires dedup.classify's gone branch via record_failure/is_gone).
        if policy == errors.GONE:
            ctx.bump_error(reason)
            return fr, tab

        # 429/503 → cool the WHOLE host down (Retry-After honored, capped).
        if policy == errors.COOLDOWN:
            self.host.cooldown(host, fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 30.0))

        # Hard network failures (DNS/refused/SSL) feed the per-host circuit breaker.
        if reason in errors.HARD_FAIL:
            self.host_fails[host] = self.host_fails.get(host, 0) + 1
            if self.host_fails[host] >= _env_int("CRAWLER_HOST_HARD_FAILS", 3):
                self.dead_hosts.add(host)
                log.warning("host_down host=%s fails=%d", host, self.host_fails[host])

        # Transient (5xx/429/timeout/render_crash) → one in-run requeue.
        transient = policy in (errors.RETRY_LATER, errors.COOLDOWN) or reason in errors.TRANSIENT
        if transient and item.retries < _env_int("CRAWLER_INRUN_RETRIES", 1):
            ctx.unreserve()                          # give the page budget back for the retry
            delay = min(fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 30.0), 60.0)
            self._schedule_retry(item, delay)
            log.info("retry_later job=%s url=%s reason=%s delay=%.0fs",
                     ctx.job.job_id, item.url, reason, delay)
            return None, tab                         # not counted — the retry counts if it fails again

        ctx.bump_error(reason)
        log.info("fetch_fail job=%s url=%s status=%s reason=%s",
                 ctx.job.job_id, item.url, status, reason)
        return fr, tab

    async def _process(self, item: WorkItem, tab):
        ctx = item.ctx
        url = item.url
        host = (urlsplit(url).hostname or "").lower()

        # Circuit breaker + known-gone skips — BEFORE reserve() so a dead host or a
        # permanently-gone URL never burns this job's page budget.
        if host in self.dead_hosts:
            ctx.bump_error(errors.HOST_DOWN)
            return tab
        if self.history is not None and self.history.is_gone(url):
            ctx.bump_error(errors.GONE_SKIP)
            return tab
        if errors.careful_off_peak_now(host):        # gov host, outside allowed hours
            ctx.bump_error(errors.OFF_PEAK)
            return tab

        if ctx.done or not ctx.reserve():
            return tab

        robots_decision = "off"
        if self.robots:
            try:                                  # allow | deny | no_robots (richer than a bool)
                robots_decision = await asyncio.to_thread(self.robots.decision, url)
            except Exception:
                robots_decision = "allow"
            if robots_decision == "deny":
                ctx.bump_error(errors.ROBOTS)
                self._audit(url, host, None, errors.ROBOTS, robots_decision)
                return tab

        try:
            async with self.host.slot(url):
                fr = await _render_page(tab, url, self.host.timeout_ms(host), ctx.job.capture,
                                        ctx.job.interaction)
        except Exception as e:
            fr = FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                             error=f"render:{e}", reason=errors.RENDER_CRASH)
            tab = await self._recycle(tab)
        self._mark_progress()
        self._audit(url, host, fr.status,
                    fr.reason or errors.classify_failure(fr.status, fr.error), robots_decision)

        if fr.error or not fr.status or fr.status >= 400:
            fr, tab = await self._on_failure(item, fr, tab, host)
            if fr is None or fr.error or not fr.status or fr.status >= 400:
                return tab
        ctx.fetched += 1
        self.host_fails[host] = 0        # a success clears the host's breaker count

        # Enqueue child links within depth/domain budget (loop thread; parse is cheap CPU).
        base = fr.final_url or url
        if fr.is_html() and fr.text_html and item.depth < ctx.job.max_depth and ctx.has_budget():
            try:
                max_qv = _env_int("CRAWLER_MAX_QUERY_VARIANTS", 20)
                for link in parse.extract_links(fr.text_html, base):
                    cl = canonicalize_url(link)
                    if cl in ctx.seen:
                        continue
                    if ctx.job.same_domain_only and not _allowed_offsite(cl, url, ctx.seed_domains):
                        continue
                    # Trap guards: URL-shape (loop/length) + calendar/facet query explosion.
                    if errors.looks_like_trap(cl):
                        ctx.trap_skipped += 1
                        continue
                    qp = urlsplit(cl)
                    if qp.query:
                        key = f"{qp.hostname}{qp.path}"
                        n = ctx.query_variants.get(key, 0) + 1
                        if n > max_qv:
                            ctx.trap_skipped += 1
                            continue
                        ctx.query_variants[key] = n
                    ctx.seen.add(cl)
                    self.frontier.put_nowait(WorkItem(cl, item.depth + 1, ctx))
            except Exception:
                ctx.bump_error(errors.PARSE_ERROR)

        # Capture candidates → HarvestedPage (mirrors harvest.py:161-166).
        pdf_links: list[str] = []
        image_candidates: list[dict] = []
        media_candidates: list[dict] = []
        if fr.is_html() and fr.text_html:
            try:
                if "pdf" in ctx.job.capture:
                    pdf_links = parse.extract_pdf_links(fr.text_html, base)
                if "images" in ctx.job.capture:
                    image_candidates = parse.extract_images(fr.text_html, base)
                if "media" in ctx.job.capture:
                    media_candidates = parse.extract_media_links(fr.text_html, base)
            except Exception:
                ctx.bump_error(errors.PARSE_ERROR)
        hp = HarvestedPage(url=url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates)

        # build_document (CPU + possible translate) off-loop; plain data only.
        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.matcher, ctx.fetcher, False)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return tab

        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.entities_detected, doc.published_at)
        ctx.bump_reason(g.reason)
        if not g.keep:
            ctx.dropped_by_gate += 1
            return tab

        # Self-dedup (§7A) — sqlite on the loop thread, no await between get/upsert.
        stored = self.history.get(url)
        verdict = classify(stored, status=fr.status, content_hash=doc.content_hash)
        self.history.upsert(url, content_hash=doc.content_hash, etag=fr.etag,
                            last_modified=fr.last_modified, status=fr.status,
                            fetched_at=fr.fetched_at, js_heavy=ctx.job.render_js)
        if verdict == "unchanged":
            ctx.skipped_unchanged += 1
            return tab
        if doc.content_hash in ctx.seen_hashes:
            ctx.skipped_duplicate += 1
            return tab
        ctx.seen_hashes.add(doc.content_hash)

        ctx.kept += 1
        await asyncio.to_thread(extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        if outcome.accepted:
            ctx.accepted += 1
        else:
            ctx.rejected += 1
        return tab

    async def _recycle(self, tab):
        """A tab crashed — close it and hand back a fresh page, relaunching a browser if needed."""
        try:
            await tab.close()
        except Exception:
            pass
        for _b, ctx in self.browsers:
            try:
                return await ctx.new_page()
            except Exception:
                continue
        # every context dead → relaunch one browser
        headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
        b = await self._pw.chromium.launch(headless=headless, args=["--no-sandbox"])
        ctx = await b.new_context(user_agent=self.seed.capture_defaults["user_agent"],
                                  viewport={"width": 1920, "height": 1080},
                                  proxy=_proxy_config())
        self.browsers.append((b, ctx))
        return await ctx.new_page()

    def _audit(self, url: str, host: str, status: int | None, reason: str | None,
               robots_decision: str) -> None:
        """Append one compliance-audit row — ONLY for careful (gov/mil) hosts, so the table
        stays small and focused on the hosts where provable politeness matters."""
        if self.history is None:
            return
        if not (errors.is_careful_host(host) or host in self.host.force_careful):
            return
        try:
            self.history.record_audit(
                url=url, host=host, fetched_at=_now_iso(),
                ua=self.seed.capture_defaults.get("user_agent", ""),
                robots_decision=robots_decision, status=status, reason=reason, careful=True)
        except Exception:
            pass

    def _mark_progress(self) -> None:
        try:
            self._last_progress = asyncio.get_running_loop().time()
        except Exception:
            pass

    async def shutdown(self) -> None:
        for t in list(self.tabs):
            try:
                await t.close()
            except Exception:
                pass
        for b, ctx in list(self.browsers):
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                await b.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass


# ── sync bridge (the FastAPI endpoint is sync) ───────────────────────────────

def run_batch_async(jobs: list[Job], *, forward: bool, l2_url: str | None,
                    seed: Seed | None = None, matcher=None) -> list[dict]:
    """Run the whole batch through ONE shared browser pool. Returns per-job dicts matching
    crawler_api.app._run: {job_id, summary{...}, documents}. Runs in a dedicated thread that
    owns its event loop, so it never touches FastAPI's anyio threadpool."""
    seed = seed or load_seed()
    matcher = matcher or build_matcher(seed)
    box: dict = {}

    def _thread() -> None:
        async def _main() -> None:
            caps = seed.capture_defaults
            robots = RobotsCache(user_agent=caps["user_agent"]) if caps.get("respect_robots_txt", True) else None
            host = HostLimiter(_env_int("CRAWLER_HOST_CONCURRENCY", 3),
                               _env_float("CRAWLER_HOST_DELAY", 1.0), robots,
                               base_timeout_s=float(caps.get("timeout_seconds", 30)))
            eng = AsyncEngine(_env_int("CRAWLER_BROWSERS", 8),
                              _env_int("CRAWLER_TABS_PER_BROWSER", 12),
                              host, seed, matcher, robots)
            await eng.start()
            try:
                ctxs = await eng.run(jobs, forward, l2_url)
            finally:
                await eng.shutdown()
            box["out"] = [{"job_id": c.job.job_id, "summary": c.summary(),
                           "documents": [d["document"] for d in c.ingest.collected]}
                          for c in ctxs]
            box["host_peak"] = dict(host.peak_inflight)

        asyncio.run(_main())

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    t.join()
    return box.get("out", [])
