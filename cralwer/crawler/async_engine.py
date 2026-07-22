"""Async multi-tab browser-pool crawl engine — saturates the machine.

A single-tab sequential crawl uses ~1 core, ~250MB. This engine runs ONE shared pool of W
Chromium browsers, each with T persistent tabs (``playwright.async_api``), fed by a shared
host-aware frontier, so W*T pages render concurrently across many hosts. Per-host politeness
(HostLimiter) means 140 tabs still never hammer a single site.

Governing rule: **one event loop, one thread.** Every Playwright object lives on that loop;
only plain data (FetchResult/Document) ever crosses to ``asyncio.to_thread``. Shared mutable
state (the frontier, per-job seen/counters/budget, the shared CrawlHistory) is touched ONLY
inline in coroutines on the loop thread — cooperative scheduling makes any no-``await`` section
atomic, so almost no locks are needed.

The per-page work is gate → self-dedup → same-run hash dedup → enrich → send.

This is the sole production engine — ``crawler_api.app`` routes both /v1/crawl and
/v1/crawl/batch here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import camofox_client, errors, extract, gate, interaction_async, paid_proxy, parse, stealth
from .canonicalize import canonicalize_url, same_site
from .dedup import CrawlHistory, classify
from .fetcher import (
    _BROWSER_UA_FALLBACK, FetchResult, Fetcher, _is_tls_or_dns_error, _now_iso, _with_www,
)
from .harvest import HarvestedPage, _allowed_offsite, _in_path_scope, _link_is_relevant
from .ingest_client import CollectingIngestClient, HttpIngestClient, is_retryable_failure
from .keywords import get_corpus
from .models import Job
from .robots import RobotsCache
from .seed import Seed, load_seed
from . import config

try:
    import psutil                          # CPU sampler for the adaptive concurrency controller
except Exception:                          # optional — adaptive gate degrades to a no-op without it
    psutil = None

log = logging.getLogger("async_engine")

# Cross-thread STOP signal. The batch runs in its own daemon thread (run_batch_async);
# the API's stop endpoint runs on a different request thread and sets this Event. The
# engine's drain/worker loops poll it and tear the crawl down gracefully (like the
# wall-clock timeout). A threading.Event is safe to set from any thread and read on the
# loop thread without a lock. ponytail: one global flag — there's one shared pool /
# one batch at a time (the /v1/crawl/batch handler is single-flight), so no per-batch id needed.
_STOP = threading.Event()


# Live adaptive-concurrency state, published for /v1/metrics (the engine runs in a batch thread, so
# the request handler can't reach the engine instance — this module global bridges it).
_ADAPTIVE_STATE: dict = {"enabled": False, "cpu": 0.0, "ram": 0.0, "target": 0,
                         "render_limit": 0, "active": 0}


def adaptive_state() -> dict:
    return dict(_ADAPTIVE_STATE)


# Live crawl-progress counter for the dashboard speed metric. Mutated only on the engine loop thread
# (plain-int += is atomic under the GIL); read by the /v1/metrics request thread. Counts pages the
# LIVE engine works through (C1 + impersonate + C3 + incremental-304). C2 archive runs separately
# and isn't counted here. ponytail: single-process counter — a multi-process shard (CRAWLER_WORKERS>1)
# only sees the parent's pages; expose per-worker + sum if that ever matters (default WORKERS=1).
# Live doc counters too, not just pages: the batch `results` (and therefore /v1/metrics "totals")
# are only written when the WHOLE batch finishes, so on a 147-job crawl the dashboard would sit at
# "0 fetched / 0 kept" for hours. These tick as each page lands so the panel is honest mid-run.
# deflected_render / deflected_host: items requeued instead of parking a tab (render pool saturated
# or the host's permit queue was full). Both were invisible before, which made a starved pool look
# identical to a slow site — the throughput was flat either way and nothing said which.
_STAT_KEYS = ("fetched", "kept", "sent", "accepted", "dropped_by_gate", "skipped_unchanged",
              "deflected_render", "deflected_host",
              "paid_recovered")
_PROGRESS: dict = {"pages": 0, "t0": None, "running": False,
                   "stats": {k: 0 for k in _STAT_KEYS}}


def progress_state() -> dict:
    """pages/elapsed + LIVE doc counters for the running crawl (see _STAT_KEYS), for /v1/metrics."""
    import time
    p = _PROGRESS
    elapsed = (time.monotonic() - p["t0"]) if p["t0"] else 0.0
    pages = int(p["pages"])
    return {"pages": pages, "elapsed_s": round(elapsed, 1),
            "pages_per_sec": round(pages / elapsed, 3) if elapsed > 0.5 else 0.0,
            "running": bool(p["running"]),
            "totals": dict(p.get("stats") or {})}


def _tick_stat(name: str, n: int = 1) -> None:
    """Bump a live doc counter. Loop-thread only; plain int += is atomic under the GIL."""
    s = _PROGRESS.get("stats")
    if s is not None and name in s:
        s[name] += n


def _progress_reset() -> None:
    """New run/recrawl: zero the counter and start the clock (dashboard resets its live rate)."""
    import time
    _PROGRESS["pages"] = 0
    _PROGRESS["stats"] = {k: 0 for k in _STAT_KEYS}
    _PROGRESS["t0"] = time.monotonic()
    _PROGRESS["running"] = True


def _progress_done() -> None:
    _PROGRESS["running"] = False


def _tick_page(n: int = 1) -> None:
    _PROGRESS["pages"] += n


def _stop_file():
    return config.DATA_DIR / ".crawl_stop"


def request_stop() -> None:
    """Signal the running crawl to halt (C1 live + C3 CamoFox both stop — the engine drives all
    tiers). Also writes a stop file so CHILD worker processes (multi-process sharding) see it via
    the shared filesystem, not just this process's in-memory event."""
    _STOP.set()
    try:
        _stop_file().write_text("stop")
    except Exception:
        pass


def clear_stop() -> None:
    _STOP.clear()
    try:
        _stop_file().unlink(missing_ok=True)
    except Exception:
        pass


def stop_requested() -> bool:
    if _STOP.is_set():
        return True
    try:
        return _stop_file().exists()      # a sibling worker process / the parent requested stop
    except Exception:
        return False


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


def _blank_between_items() -> bool:
    """Park each tab on about:blank between work items (default ON). See _worker for why.
    CRAWLER_BLANK_IDLE_TABS=0 leaves the tab on the rendered page after _process returns."""
    return os.environ.get("CRAWLER_BLANK_IDLE_TABS", "1") != "0"


def _frontier_dfs() -> bool:
    """DFS (LIFO) frontier instead of the default BFS (FIFO). On sites with a wide nav
    mega-menu the seed page fans out to dozens of category links; BFS drains every
    depth-1 category before reaching a single depth-2/3 article, so a capped crawl
    never gets to real content. DFS dives article-first. Set CRAWLER_FRONTIER=dfs.
    Order only — full-budget coverage is identical; DFS just surfaces articles sooner."""
    return os.environ.get("CRAWLER_FRONTIER", "bfs").strip().lower() == "dfs"


def _render_mode(job) -> str:
    """How to FETCH each page:
      always — Playwright renders every page (the classic render-everything engine).
      auto   — fast httpx first; render ONLY pages that look JS-dependent, and escalate a
               whole host to always-render once httpx keeps coming up empty (a site that
               "won't open" without JS). Default — lets a static site (most WordPress news)
               be crawled at httpx speed while a real SPA still gets a browser.
      never  — httpx only, never render (fastest, misses JS-only content).
    An explicit per-job render_js=true forces 'always'; otherwise CRAWLER_RENDER_MODE wins.
    NOTE: httpx-served pages get no screenshot (that needs a browser) — use render_js=true
    if you need a screenshot of every page."""
    if getattr(job, "render_js", False):
        return "always"
    m = os.environ.get("CRAWLER_RENDER_MODE", "auto").strip().lower()
    return m if m in ("auto", "always", "never") else "auto"


def _depth_ok(depth: int, max_depth: int) -> bool:
    """Whether a page at `depth` may enqueue children. max_depth < 0 = UNLIMITED (crawl as deep as
    the site goes); else follow while depth < max_depth (0 = seed pages only)."""
    return max_depth < 0 or depth < max_depth


def _incremental_skip_eligible(incremental: bool, depth: int, fresh_depth: int) -> bool:
    """Whether a page may take the 304 unchanged-skip on an incremental recrawl. Requires the job
    flag AND the page to sit BELOW the always-fresh discovery surface (depth > fresh_depth). Seeds
    and the top listing layer (depth <= fresh_depth) always re-parse so new children behind an
    unchanged-LOOKING hub (byte-stable JS/AJAX list, template Last-Modified) are still discovered —
    the 304-skip returns before _enqueue_links, so a skipped hub contributes no links. Deep leaf
    pages (the many) still get the fast skip, where the win is."""
    return incremental and depth > fresh_depth


# SPA shells that ship no server-rendered content: an httpx GET sees an empty app root with
# no links, so BFS/DFS dead-ends. These markers + a thin body ⇒ "needs a real browser".
_SPA_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", "__next_data__",
                "window.__nuxt__", "data-reactroot", "ng-version", "data-server-rendered")


def _needs_render(fr: "FetchResult") -> bool:
    """True if an httpx result should be re-fetched through Playwright — a failure/block, or an
    HTML page too thin / link-less to be the real content (a JS-rendered shell). A healthy static
    page (real text + links) returns False and is served straight from httpx (fast)."""
    if fr is None:
        return True
    if fr.error or not fr.status or fr.status >= 400:
        return True                          # failed / WAF-blocked → a browser may clear it
    if not fr.is_html():
        return False                         # pdf/image/binary: httpx already has the bytes
    html = fr.text_html or ""
    if not html.strip():
        return True
    text = parse.visible_text(html)
    try:
        links = parse.extract_links(html, fr.final_url or fr.url)
    except Exception:
        links = []
    low = html.lower()
    spa = any(m in low for m in _SPA_MARKERS)
    thin = len(text) < _env_int("CRAWLER_THIN_TEXT_CHARS", 400)
    if spa and thin:
        return True                          # app shell with no server-rendered content
    if thin and len(links) < 3:
        return True                          # nothing to extract, nowhere to go → probably JS
    return False


def _capture_assets(job, html: str | None, base: str) -> tuple[list, list, list]:
    """Extract PDF attachment links per the job's capture list. L1 captures TEXT + HTML + PDF only —
    images/media were dropped. Returns (pdf_links, [], []) to keep the shared HarvestedPage shape for
    the C1/C3 paths. ponytail: image/media extraction removed, not the tuple shape."""
    pdf_links: list = []
    if html and "pdf" in job.capture:
        try:
            pdf_links = parse.extract_pdf_links(html, base)
        except Exception as exc:
            log.debug("pdf extract failed base=%s: %s", base, exc)
    return pdf_links, [], []


def _env_float(k: str, d: float) -> float:
    return float(os.environ.get(k, str(d)))


# Hosts that should bypass the C1 live fetch and go straight to the CamoFox C3 stealth engine.
# Operators can list high-WAF domains explicitly; careful hosts (.gov/.mil) are also high-risk.
_WAF_HOST_LIST = None

def _waf_host_list() -> set[str]:
    global _WAF_HOST_LIST
    if _WAF_HOST_LIST is None:
        raw = os.environ.get("CRAWLER_C3_HOSTS", "").strip()
        _WAF_HOST_LIST = {h.lower().lstrip("www.").strip() for h in raw.split(",") if h.strip()}
    return _WAF_HOST_LIST


def _is_block_body(fr: "FetchResult | None") -> bool:
    """True if an OK-looking HTML response is actually a WAF wall. Guarded by CRAWLER_BLOCK_BODY_CHECK
    so it can be switched off if a target ever trips it. Only inspects HTML — a PDF/binary body is
    never a block page, and errors.is_ip_block_page needs distinctive phrasing to fire."""
    if fr is None or os.environ.get("CRAWLER_BLOCK_BODY_CHECK", "1") == "0":
        return False
    try:
        if not fr.is_html():
            return False
    except Exception:
        return False
    return errors.is_ip_block_page(fr.text_html or "")


def _is_waf_host(host: str) -> bool:
    """Proactive C3 trigger: high-WAF domains listed by operator, or careful gov/mil hosts."""
    if not host:
        return False
    h = host.lower().lstrip("www.").strip()
    if h in _waf_host_list():
        return True
    return errors.is_careful_host(host)


def _ignore_https_errors() -> bool:
    """Opt-in: let Chromium proceed past cert errors (self-signed / chain issues). OFF by
    default — a broken cert is a real trust failure. Turn on (CRAWLER_IGNORE_HTTPS_ERRORS=1)
    for cert-quirky sites that would otherwise hard-fail and get the whole host circuit-broken."""
    return os.environ.get("CRAWLER_IGNORE_HTTPS_ERRORS", "0") == "1"


async def _swallow(coro) -> None:
    """Await a coroutine, ignoring any error (used to fire-and-forget popup.close/download.cancel)."""
    try:
        await coro
    except Exception:
        pass


# Runs in the page context: mark up to `limit` NON-<a href> clickables (buttons, onclick handlers,
# ARIA link/button roles, data-href/data-url, and cursor:pointer elements) with a data-__cd index
# so Playwright can target them, and return [{idx, sig}] where sig is a stable text/tag signature
# for dedup. Plain <a href=http…> is excluded — extract_links already enqueues those.
_CANDIDATE_JS = """
(limit) => {
  const sel = 'button,[onclick],[role="link"],[role="button"],[data-href],[data-url]';
  const set = new Set(document.querySelectorAll(sel));
  const all = document.querySelectorAll('body *');
  for (let i = 0; i < all.length && set.size < limit * 4; i++) {
    const e = all[i];
    try { if (getComputedStyle(e).cursor === 'pointer') set.add(e); } catch (_) {}
  }
  const out = [];
  let i = 0;
  for (const e of set) {
    if (i >= limit) break;
    if (e.tagName === 'A' && e.getAttribute('href') && /^https?:/i.test(e.href)) continue;
    e.setAttribute('data-__cd', String(i));
    const txt = (e.innerText || e.textContent || '').trim().slice(0, 60);
    const dh = e.getAttribute('data-href') || e.getAttribute('data-url') || '';
    out.push({ idx: i, sig: e.tagName + '|' + txt + '|' + (e.getAttribute('onclick') ? '1' : '') + '|' + dh });
    i++;
  }
  return out;
}
"""


def _job_respects_robots(job, caps: dict) -> bool:
    """Whether this job honors robots.txt. Precedence: explicit job.respect_robots >
    env CRAWLER_RESPECT_ROBOTS > seed capture default. False = bypass (fetch every page
    directly, no robots gate and no robots→C3 detour). The in-code fallback stays at the
    seed value so clearing the env fails safe (back to polite)."""
    if job.respect_robots is not None:
        return job.respect_robots
    env = os.environ.get("CRAWLER_RESPECT_ROBOTS")
    if env is not None:
        return env == "1"
    return bool(caps.get("respect_robots_txt", True))


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
    parent_url: str | None = None   # page this link was found on (None = seed root)
    deadhost_waits: int = 0  # times requeued to wait out a host cooldown (bounded; not a failure)


class JobCtx:
    """Per-job state carried on every WorkItem. Mutated ONLY on the loop thread."""

    def __init__(self, job: Job, seed: Seed, kp, forward: bool) -> None:
        self.job = job
        self.seed = seed
        self.kp = kp                    # global keyword-corpus trie (keep-gate)
        self.crawl_run_id: str | None = None   # set by run() — batch lineage stamped on every doc

        # Forward every kept page to the Ingest API (Postgres + MinIO) when forward=True.
        forwarders: list[HttpIngestClient] = []
        self.forwarded_targets: list[str] = []
        if forward:
            forwarders.append(HttpIngestClient())
            self.forwarded_targets.append(config.INGEST_BASE_URL)
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

        # counters (the API summary shape)
        self.fetched = self.kept = self.sent = self.accepted = self.rejected = self.errors = 0
        self.not_modified = self.dropped_by_gate = self.skipped_unchanged = self.skipped_duplicate = 0
        # Split the old lumped "errors" into three honest buckets: real fetch failures (errors),
        # breaker/policy skips of queued URLs never fetched (skipped), and blocked-but-recovered
        # via C2/C3/API (recovered). errors_by_reason still holds every typed reason.
        self.skipped = self.recovered = 0
        self.gate_reasons: dict[str, int] = {}
        self.errors_by_reason: dict[str, int] = {}   # typed why-did-it-fail breakdown
        self.trap_skipped = 0                         # URLs dropped by trap heuristics
        self.scope_skipped = 0                        # URLs dropped for leaving job.path_scope
        # Phase-3 trap state: per (host,path) count of query-only variants seen (calendar/facet cap).
        self.query_variants: dict[str, int] = {}

    def reserve(self) -> bool:
        """Budget gate at dequeue — bounds total render attempts to max_pages (+in-flight).
        max_pages <= 0 means UNLIMITED: crawl the WHOLE site (bounded only by the frontier
        draining + the engine wall-clock), for 'fetch every page first, filter later' runs."""
        if self.job.max_pages > 0 and self.budget_used >= self.job.max_pages:
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
        return self.job.max_pages <= 0 or self.budget_used < self.job.max_pages

    def bump_reason(self, reason: str) -> None:
        self.gate_reasons[reason] = self.gate_reasons.get(reason, 0) + 1

    def bump_error(self, reason: str) -> None:
        """A REAL fetch failure (DNS/refused/SSL/http-4xx-5xx/render/timeout)."""
        self.errors += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def bump_skip(self, reason: str) -> None:
        """A queued URL SKIPPED without a fetch (breaker host-down / known-gone / off-peak /
        robots). Not a failure — kept out of `errors` so the total isn't misleading."""
        self.skipped += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def bump_recovered(self, reason: str) -> None:
        """A blocked page RECOVERED via a fallback tier (C3 CamoFox / C2 archive / official API)."""
        self.recovered += 1
        self.errors_by_reason[reason] = self.errors_by_reason.get(reason, 0) + 1

    def summary(self) -> dict:
        return {
            "fetched": self.fetched, "not_modified_304": self.not_modified,
            "dropped_by_gate": self.dropped_by_gate, "skipped_unchanged": self.skipped_unchanged,
            "skipped_duplicate": self.skipped_duplicate, "kept": self.kept,
            "sent": self.sent, "accepted": self.accepted, "rejected": self.rejected,
            "errors": self.errors, "skipped": self.skipped, "recovered": self.recovered,
            "errors_by_reason": self.errors_by_reason,
            "trap_skipped": self.trap_skipped, "scope_skipped": self.scope_skipped,
            "gate_reasons": self.gate_reasons,
            "forwarded_to": self.forwarded_targets, "crawl_run_id": self.crawl_run_id,
        }


# ── CPU-feedback adaptive concurrency (fill the cores; the GIL only caps orchestration) ──────

def _aimd_limit(cpu: float, limit: int, target: int, step_up: int, lo: int, hi: int,
                ram: float = 0.0, ram_target: float = 101.0, ram_hard: float = 101.0) -> int:
    """Additive-increase / multiplicative-decrease toward a CPU target — the same self-damping rule
    TCP uses. Below (target-5) there's headroom → admit more renders; above target → back off.
    RAM-aware: a hard RAM brake (ram >= ram_hard) sheds load regardless of CPU, and growth is gated on
    RAM also having headroom (ram < ram_target) so we never fill the cores into an OOM. RAM defaults
    (101) make it a pure CPU controller when RAM isn't supplied. Clamped to [lo, hi]."""
    if ram >= ram_hard:
        limit = int(limit * 0.85)            # hard RAM brake — shed load before OOM, ignore CPU
    elif cpu < target - 5 and ram < ram_target:
        limit += step_up                     # headroom on BOTH cpu and ram → admit more renders
    elif cpu > target:
        limit = int(limit * 0.85)
    return max(lo, min(hi, limit))


class RenderBusy(Exception):
    """No render permit available for this item right now — requeue it, don't park a tab on it.

    Raised instead of waiting when the global render pool is saturated or this host already holds
    its share. Measured why: the adaptive gate is acquired BEFORE the per-host slot, so a tab
    waiting for a render permit is invisible to per-host accounting. One host held all 64 permits
    while every other host's render-needing items sat on the gate until the item watchdog killed
    them — 99 timeouts on one host in 3 minutes with zero HTTP requests issued."""


class AdaptiveGate:
    """A GLOBAL admission permit whose ceiling a CPU sampler grows/shrinks at runtime, so total
    concurrent renders track a CPU target instead of blindly running all 96 tabs (which either
    thrashes at 100% or, when render-heavy, is the only way to use the cores). asyncio.Semaphore
    can't be resized, so this is a counter + Condition. When ``limit == max_limit`` it never blocks
    (a no-op), so wrapping every render is safe even with the controller disabled."""

    def __init__(self, initial: int, min_limit: int, max_limit: int) -> None:
        self.min_limit = max(1, min_limit)
        self.max_limit = max(self.min_limit, max_limit)
        self.limit = max(self.min_limit, min(self.max_limit, initial))
        self.active = 0
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def slot(self, timeout: float | None = None):
        """Acquire a render permit. With *timeout*, raise RenderBusy rather than waiting forever —
        the caller requeues the item instead of holding a tab hostage on a saturated pool."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        async with self._cond:
            while self.active >= self.limit:
                if deadline is None:
                    await self._cond.wait()
                    continue
                rem = deadline - loop.time()
                if rem <= 0:
                    raise RenderBusy(f"no render permit in {timeout}s ({self.active}/{self.limit})")
                try:
                    await asyncio.wait_for(self._cond.wait(), rem)
                except asyncio.TimeoutError:
                    raise RenderBusy(
                        f"no render permit in {timeout}s ({self.active}/{self.limit})") from None
            self.active += 1
        try:
            yield
        finally:
            async with self._cond:
                self.active -= 1
                self._cond.notify()

    async def set_limit(self, n: int) -> int:
        n = max(self.min_limit, min(self.max_limit, n))
        async with self._cond:
            grew = n > self.limit
            self.limit = n
            if grew:
                self._cond.notify(n)         # wake blocked acquirers into the new headroom
        return n


# ── per-host politeness (shared across the whole pool) ───────────────────────

def _parse_host_overrides(raw: str) -> dict[str, int]:
    """CRAWLER_HOST_CONCURRENCY_OVERRIDE='sipri.org=3,mod.gov.my=1' → {host: concurrency}.
    For weak/flaky NO-BOT servers that drop connections: cap concurrency AND keep them on the light
    httpx path (no forced render, no proactive C3) — render just gives a flaky box more sockets to
    drop. Pure so it unit-tests without a browser."""
    out: dict[str, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        h, _, v = part.partition("=")
        h = h.strip().lower().lstrip(".")
        try:
            n = int(v.strip())
        except ValueError:
            continue
        if h and n > 0:
            out[h] = n
    return out


def _match_override(host: str, overrides: dict[str, int]) -> int | None:
    """Concurrency cap for host if it (or a parent domain) is in the override map, else None.
    Segment-suffix match: 'sipri.org' matches www.sipri.org but not evilsipri.org."""
    host = (host or "").lower().rstrip(".")
    for key, n in overrides.items():
        if host == key or host.endswith("." + key):
            return n
    return None


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
        self._cap: dict[str, int] = {}            # live concurrency cap per host (grown by retarget)
        self.peak_inflight: dict[str, int] = {}   # for verification (≤ the host's CURRENT _cap)
        # Hosts forced into careful-mode by a job with careful=True (not just .gov/.mil suffix).
        self.force_careful: set[str] = set()
        # Per-host concurrency caps for weak/flaky no-bot servers (see _parse_host_overrides).
        self.overrides = _parse_host_overrides(os.environ.get("CRAWLER_HOST_CONCURRENCY_OVERRIDE", ""))
        # Engine wires this to _mark_progress so a tab asleep on a host cooldown heartbeats the
        # idle-timer — a cooling host is legit progress, not a wedge. None until run() sets it.
        self._progress = None
        self._waiting = 0            # tabs currently parked in a slot cooldown/delay wait (diagnostic)
        self._queued: dict[str, int] = {}   # per-host tabs blocked on the permit (see slot/queue_depth)

    def timeout_ms(self, host: str) -> float:
        """Per-host patient timeout in ms (bigger for careful hosts; ratchets on TIMEOUT)."""
        return self._timeout.get(host, self.base_timeout_s) * 1000

    def bump_timeout(self, host: str) -> None:
        """A slow gov origin timed out — extend its patience (bounded) before the breaker counts it."""
        cap = _env_float("CRAWLER_MAX_TIMEOUT_S", 120.0)
        cur = self._timeout.get(host, self.base_timeout_s)
        self._timeout[host] = min(cur * 1.5, cap)

    def override_for(self, host: str) -> int | None:
        """The per-host concurrency cap for host (weak/flaky no-bot server), or None."""
        return _match_override(host, self.overrides)

    async def _ensure(self, host: str) -> None:
        if host not in self._sem:
            # Created synchronously before any await → no race creating two semaphores.
            # Precedence: an explicit per-host OVERRIDE (weak/flaky no-bot server) → its cap + a
            # patient timeout, staying on the light httpx path. Else CAREFUL (.gov/.mil / careful=True)
            # → concurrency 1, slow delay, patient timeout ("quiet quiet"). Else the pool default.
            ov = _match_override(host, self.overrides)
            careful = errors.is_careful_host(host) or host in self.force_careful
            if ov is not None:
                conc = ov
                self._delay[host] = max(self.min_delay, _env_float("CRAWLER_OVERRIDE_DELAY_S", 1.0))
                factor = _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)
            elif careful:
                conc = 1
                self._delay[host] = max(self.min_delay, _env_float("CRAWLER_CAREFUL_DELAY_S", 5.0))
                factor = _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)
            else:
                conc = self.max_conc
                self._delay[host] = self.min_delay
                factor = 1.0
            self._sem[host] = asyncio.Semaphore(conc)
            self._cap[host] = conc
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

    def make_careful(self, host: str) -> None:
        """Mid-crawl promotion to careful (concurrency 1 + careful delay + patient timeout), matching the
        .gov/.mil path in _ensure. Idempotent. Safe mid-flight: slot() binds the acquired semaphore
        OBJECT at entry, so replacing self._sem[host] with a fresh Semaphore(1) lets in-flight holders
        drain the old object while new entrants throttle to 1 — no permit is corrupted (peak_inflight may
        briefly exceed 1; it decays as holders release). Only reached after a failure, so slot()→_ensure
        already built this host's state; we just overwrite _sem/_delay/_timeout."""
        if errors.is_careful_host(host) or host in self.force_careful:
            return
        self.force_careful.add(host)
        self._sem[host] = asyncio.Semaphore(1)
        self._cap[host] = 1
        self._delay[host] = max(self._delay.get(host, self.min_delay),
                                _env_float("CRAWLER_CAREFUL_DELAY_S", 5.0))
        self._timeout[host] = self.base_timeout_s * _env_float("CRAWLER_CAREFUL_TIMEOUT_S", 2.0)

    def retarget(self, host: str, target: int) -> None:
        """Grow this host's concurrency cap to *target* by releasing extra permits.

        GROWTH ONLY. Shrinking a live Semaphore would strand permits held by in-flight fetches;
        demotion is make_careful's job (it swaps in a fresh Semaphore(1)). Careful/override hosts
        keep their politeness cap — the fill loop must never widen a weak or .gov/.mil box."""
        sem = self._sem.get(host)
        if sem is None:
            return
        if (errors.is_careful_host(host) or host in self.force_careful
                or self.override_for(host) is not None):
            return
        cur = self._cap.get(host, self.max_conc)
        if target <= cur:
            return
        for _ in range(target - cur):
            sem.release()
        self._cap[host] = target

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
        # Bind the semaphore OBJECT (make_careful may swap self._sem[host] mid-flight; releasing a
        # different object than we acquired would corrupt the permit count). Count queue depth here
        # so _worker can refuse to park another tab on an already-deep host — this wait is unbounded
        # and sits inside the per-item watchdog, which is how 22 tabs burned 240s each on one host.
        sem = self._sem[host]
        self._queued[host] = self._queued.get(host, 0) + 1
        try:
            await sem.acquire()
        finally:
            self._queued[host] -= 1          # also on cancellation, or the depth leaks upward
        try:
            self._inflight[host] += 1
            self.peak_inflight[host] = max(self.peak_inflight[host], self._inflight[host])
            loop = asyncio.get_running_loop()
            wait = self._next_ok[host] - loop.time()
            if wait > 0:
                # Heartbeat the engine's idle-timer while asleep: a long 429/503 cooldown (up to
                # CRAWLER_COOLDOWN_CAP_S) parks every tab here with no fetch completing, which would
                # otherwise read as "idle" and kill the batch. Cooling is progress, not a wedge.
                hb = _env_float("CRAWLER_SLOT_HEARTBEAT_S", 30.0)
                end = loop.time() + wait
                self._waiting += 1
                try:
                    while (rem := end - loop.time()) > 0:
                        await asyncio.sleep(min(rem, hb))
                        if self._progress:
                            self._progress()
                finally:
                    self._waiting -= 1
            self._next_ok[host] = loop.time() + self._delay[host]
            yield
        finally:
            self._inflight[host] -= 1
            sem.release()

    def queue_depth(self, host: str) -> int:
        """Tabs currently blocked waiting for this host's permit (not counting the holder)."""
        return self._queued.get(host, 0)


# ── async render (1:1 port of Fetcher._render_fetch core path) ───────────────

# Giant-binary extensions aborted before bytes flow — the render path can't cap response
# size, so we refuse the classic huge-download classes at the network layer instead.
_BLOCK_EXT = "**/*.{zip,exe,dmg,iso,msi,mp4,mkv,avi,mov,wmv,flv,gz,tgz,7z,rar,bin,pkg,deb,rpm}"


# URLs that serve a file rather than a page. Matched on the path only, so a query string like
# ?download=1 on a real HTML page does not misfire.
_DOWNLOAD_RE = re.compile(
    r"(\.(pdf|docx?|xlsx?|pptx?|csv|zip|rar|7z|gz|tgz|odt|ods|rtf)$"
    r"|/download/?$|/downloads?/[^/]+\.[a-z0-9]{2,5}$)", re.I)


def _looks_like_download(url: str) -> bool:
    try:
        return bool(_DOWNLOAD_RE.search(urlsplit(url).path))
    except Exception:
        return False


async def _abort_route(route) -> None:
    try:
        await route.abort()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


# Same giant-binary classes as _BLOCK_EXT, but as a URL regex so a single catch-all route handler
# ("**/*") can check them itself (Playwright's most-recently-added route runs first, and continue_
# is terminal — so one unified handler is cleaner than chaining two overlapping globs).
_BLOCK_EXT_RE = re.compile(
    r"\.(zip|exe|dmg|iso|msi|mp4|mkv|avi|mov|wmv|flv|gz|tgz|7z|rar|bin|pkg|deb|rpm)(?:[?#]|$)", re.I)
# Resource types a text/link/PDF crawl doesn't need — aborting them stops each render from waiting on
# megabytes of pixels/fonts it discards (the single biggest render-latency win). Kept: document,
# script, xhr/fetch, stylesheet (links + JS-injected content need those). enrich_assets downloads
# images/PDFs over httpx from the PARSED HTML, so blocking browser pixel loads does NOT reduce capture.
_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}


# Dispatch mouseover on nav candidates so hover-only dropdown submenus (whose <a href> aren't
# in the DOM until a mouseover fires) become visible for extract_links. JS events, not a real
# cursor — bounded to the first 24 nav-ish elements.
_AUTO_HOVER_JS = """
() => {
  const sel = 'nav a,header a,[aria-haspopup],[class*="menu"] a,[class*="nav"] li,.dropdown,[role="menu"],[role="menuitem"]';
  const els = Array.from(document.querySelectorAll(sel)).slice(0, 24);
  const fire = (t, e) => { try { e.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window})); } catch(_){} };
  for (const e of els) { fire('pointerover', e); fire('mouseover', e); fire('mouseenter', e); }
  return els.length;
}
"""

# Find a visible "load more / show more" control and tag it for clicking.
_LOADMORE_JS = """
() => {
  const rx = /load more|show more|view more|see more|load all/i;
  const cands = Array.from(document.querySelectorAll('button,a,[role="button"],[onclick]'));
  const hit = cands.find(e => rx.test((e.textContent||'').trim()) && e.offsetParent !== null);
  if (hit) { hit.setAttribute('data-__lm','1'); return true; }
  return false;
}
"""


async def _auto_interact(tab) -> None:
    """Reveal links that only appear after scroll (lazy-load), hover (dropdown menus), or a
    'load more' click, so parse.extract_links sees them. Default-on (CRAWLER_AUTO_INTERACT!=0),
    bounded, every step swallowed (never fatal). Runs on RENDERED pages, just before content()."""
    try:                            # 1) bounded infinite scroll → trigger lazy-load
        steps = _env_int("CRAWLER_AUTOSCROLL_STEPS", 3)
        pause = _env_int("CRAWLER_AUTOSCROLL_PAUSE_MS", 600)
        last = await tab.evaluate("document.body.scrollHeight")
        for _ in range(steps):
            await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await tab.wait_for_timeout(pause)
            new = await tab.evaluate("document.body.scrollHeight")
            if new == last:
                break
            last = new
    except Exception:
        pass
    try:                            # 2) hover nav → reveal dropdown submenus
        await tab.evaluate(_AUTO_HOVER_JS)
        await tab.wait_for_timeout(250)
    except Exception:
        pass
    try:                            # 3) click "load more"/"show more" a few times
        for _ in range(_env_int("CRAWLER_LOADMORE_CLICKS", 5)):
            if not await tab.evaluate(_LOADMORE_JS):
                break
            try:
                await tab.locator('[data-__lm="1"]').first.click(timeout=3000, no_wait_after=True)
            except Exception:
                break
            await tab.wait_for_timeout(600)
            await tab.evaluate(
                "document.querySelectorAll('[data-__lm]').forEach(e=>e.removeAttribute('data-__lm'))")
    except Exception:
        pass


async def _wait_dom_settle(tab, cap_ms: int | None = None) -> bool:
    """Block until the DOM stops growing. Returns True if it was STILL growing at the cap.

    A JS app finishes when its DOM stops changing, which no fixed sleep can know: the same 2s that
    is wasted on a static page is nowhere near enough for a framework that hydrates and then fetches.
    So sample the document size and stop once it repeats — fast pages exit on the second sample
    (~300ms, quicker than the fixed budget it replaces), slow ones get the time they actually need.

    The cap exists because some pages never settle at all (carousels, tickers, live counters, ad
    rotators). Hitting it is reported rather than swallowed: the caller marks the page js_incomplete
    so a half-built capture is visible instead of being stored as if it were finished.
    """
    cap_ms = _env_int("CRAWLER_DOM_SETTLE_MAX_MS", 12000) if cap_ms is None else cap_ms
    step_ms = _env_int("CRAWLER_DOM_SETTLE_STEP_MS", 300)
    stable_needed = _env_int("CRAWLER_DOM_SETTLE_STABLE", 2)
    if cap_ms <= 0:
        return False
    last, stable, waited = -1, 0, 0
    while waited < cap_ms:
        try:
            size = await tab.evaluate("document.documentElement.outerHTML.length")
        except Exception:      # navigated away / tab gone — let the caller's handler classify it
            return False
        if size == last:
            stable += 1
            if stable >= stable_needed:
                return False   # settled
        else:
            stable, last = 0, size
        await tab.wait_for_timeout(step_ms)
        waited += step_ms
    return True                # still moving when the cap hit


async def _render_page(tab, url: str, timeout_ms: int, capture: list[str],
                       interaction=None, discover: bool = True) -> FetchResult:
    """Render one URL on a persistent tab; snapshot to a plain FetchResult on the loop.
    Render-everything mode: always renders, no httpx/conditional (dedup is by content_hash).
    Before snapshotting, `_auto_interact` scrolls/hovers/load-mores to surface JS-revealed links —
    but ONLY when `discover` is True (this page's children will be enqueued). A leaf page at max
    depth has no children to reveal, so we skip the ~4-5s of scroll/hover there.
    If `interaction` is set (form-search/pagination/scroll), run it after load — that's what
    lets the pool search a gov tender portal."""
    networkidle_ms = _env_int("CRAWLER_NETWORKIDLE_MS", 1500)
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
        # Wait for the DOM to stop GROWING, not for a fixed number of milliseconds. Measured on
        # this corpus at the old fixed budget (networkidle 1500ms + 500ms settle): renault-trucks
        # was snapshotted at 25% of its final DOM (44K of 174K), zf.com at 70%. Both were stored as
        # complete pages, because a networkidle timeout is swallowed above and nothing downstream
        # can tell a finished page from one caught mid-hydration.
        # Cheap for static sites — they are stable on the first two samples and exit sooner than
        # the old fixed sleep — and only slow where slowness is the whole point.
        grew = await _wait_dom_settle(tab)
        if discover and os.environ.get("CRAWLER_AUTO_INTERACT", "1") != "0":
            await _auto_interact(tab)      # scroll/hover/load-more to reveal hidden links
            # Short cap on the SECOND wait: the page already settled once, so this is only catching
            # lazy-loads that the scroll triggered, which arrive fast. At the full 12s cap
            # rohde-schwarz took 38.5s/page (12 settle + interact + 12 settle) — unusable on a
            # 2,455-page host.
            await _wait_dom_settle(tab, cap_ms=_env_int("CRAWLER_DOM_SETTLE_POST_MS", 3000))
        html = await tab.content()
    except Exception as exc:
        return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                           error=f"content:{exc}",
                           reason=errors.classify_failure(None, f"content:{exc}"))

    inner_text = None
    extra_links: list | None = None
    extra_pdf_links: list | None = None
    if interaction_async.has_any(interaction):
        try:                       # form-search / pagination / scroll — never fatal
            html, inner_text, extra_links, extra_pdf_links = \
                await interaction_async.run_interactions(tab, interaction)
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

    # Capture HTTP validators from the render response too (Playwright exposes them) so an
    # incremental recrawl can send If-None-Match/If-Modified-Since and skip unchanged pages —
    # otherwise a rendered site (most of them) would store no validators and never get a 304.
    etag = last_modified = None
    if resp:
        try:
            h = await resp.all_headers()
            etag = h.get("etag")
            last_modified = h.get("last-modified")
        except Exception:
            pass

    # Harvest the render's cookies so a protected asset download (Akamai) can replay the
    # page session that just passed the WAF. Best-effort; plain data crosses to to_thread.
    cookies = None
    try:
        cookies = await tab.context.cookies()
    except Exception:
        cookies = None

    return FetchResult(url=url, final_url=final_url, status=status, content_type="text/html",
                       kind="html", text_html=html, inner_text=inner_text, screenshot_png=shot,
                       tier=1, fetched_at=_now_iso(), reason=reason, retry_after_s=retry_after,
                       cookies=cookies, etag=etag, last_modified=last_modified,
                       js_incomplete=grew,
                       extra_links=extra_links or None, extra_pdf_links=extra_pdf_links or None)


# ── the engine ───────────────────────────────────────────────────────────────

class AsyncEngine:
    def __init__(self, W: int, T: int, host: HostLimiter, seed: Seed, kp,
                 robots: RobotsCache | None = None) -> None:
        self.W = W
        self.T = T
        self.host = host
        self.seed = seed
        self.kp = kp                    # global keyword-corpus trie (keep-gate)
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
        self.host_weak: dict[str, int] = {}   # conn_refused/timeout tally → auto-careful promotion
        self._render_hosts: dict[str, int] = {}  # per-host render permits held/queued (see _render_permit)
        self.dead_hosts: set[str] = set()
        # Cooling hosts revive for a fresh live attempt at self.host_cooldown[host] (loop time);
        # while cooling, their queued URLs are recovered via C2/C3 rather than dropped.
        self.host_cooldown: dict[str, float] = {}
        # Asset downloads (images/PDFs in enrich_assets) get a DEDICATED thread pool, isolated from
        # asyncio.to_thread's shared default pool. A slow/hanging asset host (e.g. l3harris) would
        # otherwise fill the shared pool and starve page fetches/builds → the WHOLE crawl stalls with
        # every worker stuck in fetch_assets. Isolated here, backed-up asset downloads never block
        # page crawling (pages keep fetching; only asset capture lags).
        from concurrent.futures import ThreadPoolExecutor
        self._asset_pool = ThreadPoolExecutor(
            max_workers=_env_int("CRAWLER_ASSET_POOL", 24), thread_name_prefix="asset")
        # Auto render-mode: hosts escalated to always-render (httpx kept coming up empty), and
        # the per-host [rendered, total] tally over the first few pages that drives escalation.
        self.host_render_always: set[str] = set()
        self.host_render_stat: dict[str, list] = {}
        # Requeued-transient count — guards _drain so a delayed retry isn't lost.
        # Mutated only on the loop thread.
        self._pending_retries = 0
        self._inflight_items = 0          # items dequeued but not yet task_done
        # CPU-feedback adaptive concurrency: a global gate on concurrent RENDERS whose ceiling the
        # _drain sampler ramps toward CRAWLER_CPU_TARGET. Ceiling can't exceed the tab count (W*T).
        # Disabled (or psutil-missing) → limit pinned at max, so the gate never blocks.
        self.adaptive_on = psutil is not None and os.environ.get("CRAWLER_ADAPTIVE_CONCURRENCY", "1") != "0"
        _tabs = self.W * self.T
        _amin = _env_int("CRAWLER_ADAPTIVE_MIN", 4)
        self.adaptive = AdaptiveGate(
            initial=(8 if self.adaptive_on else _tabs), min_limit=_amin,
            # Cap render admission below total tabs: renders that are I/O-blocked (a WAF-blocked page
            # loading its 403 wall, or a slow C3 escalation) show low CPU, so the CPU-fed controller
            # would otherwise ramp admission to every tab and saturate the pool.
            max_limit=min(_tabs, _env_int("CRAWLER_ADAPTIVE_MAX", 64)))
        # Bound concurrent C3/CamoFox renders. A WAF-blocked section (mass 403 → C3 escalation) must not
        # flood the single CamoFox service faster than it drains, or renders pile up, throughput
        # collapses, and the batch idle-terminates mid-frontier.
        self._camofox_sem = asyncio.Semaphore(_env_int("CRAWLER_CAMOFOX_CONCURRENCY", 6))
        # Deferred C3 lane (CRAWLER_DEFER_C3=1): blocked C1 pages are pushed here instead of parking a
        # tab-worker on the ~90s stealth render; a tab-less _c3_worker pool drains it at the CamoFox
        # cap. Created on the loop in run(). Counters mutate only on the loop thread (like _inflight_items).
        self.c3_queue: asyncio.Queue | None = None
        self._inflight_c3 = 0            # c3 items dequeued, not yet done — blocks _drain exit
        self._netpath_tagged = 0         # COUNT only (never a URL list): needs-network-path backlog size
        self._run_started_iso = ""       # set in run(); scopes the sqlite netpath reconstruction
        # Block image/media/font resource loads during renders (env-guarded). Flipped OFF in run() if
        # any job wants a screenshot (needs the pixels). Read per-request by _route_request.
        self._block_assets = os.environ.get("CRAWLER_BLOCK_ASSETS", "1") != "0"
        # Finalizer pool: the slow I/O tail (asset enrich + ingest POST) runs here so a worker's tab
        # returns to the frontier the instant a page is parsed, instead of blocking on PDF downloads +
        # the ingest round-trip. Created on the loop in run() (queue + N drainers).
        self._finalize_q: asyncio.Queue | None = None

    async def _route_request(self, route) -> None:
        """Single wire-level gate on every request: abort giant-binary downloads always; when
        asset-blocking is on AND the request host isn't careful, also abort image/media/font so a
        text+PDF render stops waiting on pixels it discards. Everything else continues. Never fatal."""
        try:
            req = route.request
            u = req.url
            if _BLOCK_EXT_RE.search(u):
                await route.abort()
                return
            # Third-party ad/analytics beacons — never a crawl target; abort for faster renders and
            # a smaller fingerprint surface. (Skipped on screenshot jobs, which use the narrow route.)
            if stealth.block_trackers_enabled() and stealth.is_tracker(urlsplit(u).hostname or ""):
                await route.abort()
                return
            if self._block_assets and req.resource_type in _BLOCK_RESOURCE_TYPES:
                host = (urlsplit(u).hostname or "").lower()
                if not (errors.is_careful_host(host) or host in self.host.force_careful):
                    await route.abort()
                    return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
        ua = self.seed.capture_defaults["user_agent"]
        proxy = _proxy_config()
        if proxy:
            log.info("proxy enabled via CRAWLER_PROXY_URL: %s", proxy["server"])
        for _ in range(self.W):
            # Harden the primary tier: drop the automation fingerprint (webdriver flag + infobar)
            # so C1 stops drawing blocks that force premature C3 escalation. DoH args (secure mode)
            # only present when CRAWLER_DOH_TEMPLATE is set. Both no-op when CRAWLER_STEALTH=0.
            b = await self._pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", *stealth.stealth_launch_args(), *stealth.doh_launch_args()],
                ignore_default_args=stealth.stealth_ignore_default_args())
            ctx = await b.new_context(user_agent=ua, viewport={"width": 1920, "height": 1080},
                                      locale="en-US", timezone_id="America/New_York",
                                      extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                                      ignore_https_errors=_ignore_https_errors(),
                                      proxy=proxy)
            if stealth.stealth_enabled():
                await ctx.add_init_script(stealth.STEALTH_INIT_JS)
            # Session/cookies: the browser already domain-scopes its cookie jar (host A's cookie
            # is never SENT to host B), and a gov search flow (fill→submit→paginate) runs on ONE
            # tab in this ONE context in a single _render_page call — so its session persists
            # exactly where a portal needs it. ponytail: full per-host BrowserContext isolation
            # (for a session-required MULTI-page BFS spread across the W browsers) is a
            # pool rearchitecture for a rare case — add host→browser affinity if it ever bites.
            try:
                if self._block_assets:
                    # Catch-all so the handler can abort image/media/font per-request (giant binaries
                    # always abort inside it). If _block_assets is later flipped off (a screenshot
                    # job), the handler degrades to giant-binary-only + continue.
                    await ctx.route("**/*", self._route_request)
                else:
                    await ctx.route(_BLOCK_EXT, _abort_route)   # cheap narrow route: giant downloads only
            except Exception:
                pass
            self.browsers.append((b, ctx))
            for _ in range(self.T):
                self.tabs.append(await ctx.new_page())
        log.info("engine started: %d browsers x %d tabs = %d workers",
                 self.W, self.T, len(self.tabs))

    async def run(self, jobs: list[Job], forward: bool) -> list[JobCtx]:
        self.history = CrawlHistory()                       # bound to this (engine) thread
        # Enlarge the default thread pool. httpx fetch + build_document + ingest.send all run via
        # asyncio.to_thread — the default ~28-thread pool (min(32, cpu+4)) caps a whole-site httpx
        # crawl regardless of tab count. A big IO pool lets httpx concurrency scale to the host/tab
        # limits (the lever for keyword=false whole-site sweeps). Renders stay governed by the gate.
        from concurrent.futures import ThreadPoolExecutor
        self._io_pool = ThreadPoolExecutor(
            max_workers=_env_int("CRAWLER_IO_THREADS", 256), thread_name_prefix="io")
        asyncio.get_running_loop().set_default_executor(self._io_pool)
        try:                                                # seed per-host render learning from prior runs
            self.host_render_always |= self.history.get_render_hosts()
        except Exception:
            pass
        try:                                                # seed weak-under-load careful hosts (prior runs)
            self.host.force_careful |= self.history.get_careful_hosts()
        except Exception:
            pass
        # Asset-blocking needs the browser to load images for a screenshot — if ANY job in the batch
        # captures screenshots, disable blocking for the whole (shared-context) pool.
        if any("screenshot" in getattr(j, "capture", []) for j in jobs):
            self._block_assets = False
        # BFS (FIFO) by default; CRAWLER_FRONTIER=dfs → LIFO so articles surface before the
        # seed's category mega-menu is fully drained. Same .get/.put_nowait/.task_done API.
        self.frontier = asyncio.LifoQueue() if _frontier_dfs() else asyncio.Queue()
        if _frontier_dfs():
            log.info("frontier: DFS (LIFO)")
        ctxs = [JobCtx(j, self.seed, self.kp, forward) for j in jobs]
        # One run id per engine run = per Run Batch (WORKERS>1 shards get one each). Stamped on every
        # doc for L2 batch lineage.
        self.run_id = "run_" + uuid.uuid4().hex[:12]
        for c in ctxs:
            c.crawl_run_id = self.run_id
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
        self.host._progress = self._mark_progress   # a host cooling down counts as progress, not a wedge (see HostLimiter.slot)
        # Finalizer pool: drains the slow enrich+send tail off the tab-holding critical path so a
        # worker's tab returns to the frontier the instant a page is parsed.
        self._finalize_q = asyncio.Queue(maxsize=_env_int("CRAWLER_FINALIZE_QUEUE", 200))
        finalizers = [asyncio.create_task(self._finalizer_loop())
                      for _ in range(_env_int("CRAWLER_FINALIZERS", 32))]
        # Tab-less C3 drainer pool (CRAWLER_DEFER_C3=1): drains c3_queue off the tab-workers so a
        # WAF-blocked section never parks a tab on a stealth render. _camofox_sem is the real cap.
        self._run_started_iso = _now_iso()
        self.c3_queue = asyncio.Queue(maxsize=_env_int("CRAWLER_C3_QUEUE", 2000))
        c3_workers = [asyncio.create_task(self._c3_worker())
                      for _ in range(_env_int("CRAWLER_C3_WORKERS",
                                              _env_int("CRAWLER_CAMOFOX_CONCURRENCY", 6)))]
        workers = [asyncio.create_task(self._worker(t)) for t in self.tabs]
        wall = _env_int("CRAWLER_ENGINE_WALL_CLOCK_S", 0)  # 0 = no chop: run to budget/frontier-empty/idle/STOP
        idle = _env_int("CRAWLER_ENGINE_IDLE_S", 300)      # >= cooldown cap so a cooling host can't false-trip idle
        try:
            await asyncio.wait_for(self._drain(idle), timeout=(wall or None))  # None => no wall-clock backstop
        except asyncio.TimeoutError:
            log.warning("engine wall-clock backstop (%ss) hit — terminating batch", wall)
        for w in workers:
            w.cancel()
        for c in c3_workers:
            c.cancel()
        await asyncio.gather(*workers, *c3_workers, return_exceptions=True)
        # Final pass: retry the needs-network-path backlog through a US/residential egress (no-op unless
        # CRAWLER_NETPATH_FINAL=1 + a proxy is wired). Runs after _drain (idle timer disarmed) and before
        # the finalizer join so its recovered docs flush through the same finalizer pool.
        try:
            await self._netpath_final_pass(ctxs)
        except Exception:
            log.exception("netpath final pass failed")
        # Workers stopped queueing docs; drain the finalizer backlog (enrich + POST) so no kept doc is
        # lost, THEN cancel the finalizers. Bounded so a wedged ingest can't hang shutdown forever.
        try:
            await asyncio.wait_for(self._finalize_q.join(),
                                   timeout=_env_int("CRAWLER_FINALIZE_DRAIN_S", 120))
        except asyncio.TimeoutError:
            log.warning("finalizer drain timeout — some kept docs may be unsent")
        for f in finalizers:
            f.cancel()
        await asyncio.gather(*finalizers, return_exceptions=True)
        self.history.close()
        return ctxs

    def _refill_host_caps(self) -> None:
        """Big-site fill — keep the pool busy once the small sites finish.

        The per-host cap is sized ONCE at batch start from the SEED host count
        (_adaptive_host_concurrency: 256 tabs // 89 hosts → 6) and then frozen into each host's
        Semaphore. So when 85 small sites complete, the 2-3 remaining big hosts (a 40k-page news
        site) stay pinned at 6 and ~250 of 256 workers sit idle — the batch tail crawls at 2% of
        the pool. Here, whenever work remains but the pool is underused, we grow the ACTIVE hosts'
        caps toward an even split of the pool.

        Self-limiting: growth stops as soon as _inflight_items climbs back over the busy mark, so a
        wide batch never widens at all. Growth-only, and careful/override hosts are skipped — a weak
        or .gov box is never sped up by this."""
        tabs = len(self.tabs) or 1
        if self.frontier is None or self.frontier.qsize() == 0:
            return                                  # no queued work → widening buys nothing
        if self._inflight_items >= tabs * _env_float("CRAWLER_HOST_FILL_BUSY", 0.75):
            return                                  # pool already busy → leave politeness alone
        active = [h for h, n in self.host._inflight.items() if n > 0]
        if not active:
            return
        target = min(_env_int("CRAWLER_HOST_CONCURRENCY_MAX", 64),
                     max(1, tabs // len(active)))
        for h in active:
            self.host.retarget(h, target)

    async def _drain(self, idle_s: int) -> None:
        """Return when the frontier is fully drained (nothing queued, nothing in flight, no
        retry pending), or after idle_s with no completed page. Counters are all mutated on
        the loop thread, so a delayed requeue can never slip past this check."""
        loop = asyncio.get_running_loop()
        if self.adaptive_on and psutil is not None:
            psutil.cpu_percent(interval=None)       # prime — the first read is meaningless
        target = _env_int("CRAWLER_CPU_TARGET", 90)
        step = _env_int("CRAWLER_ADAPTIVE_STEP", 2)
        ram_target = _env_int("CRAWLER_RAM_TARGET", 85)     # grow only while RAM has headroom too
        ram_hard = _env_int("CRAWLER_RAM_HARD", 92)         # hard brake — shed load before OOM
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            if stop_requested():                    # operator hit STOP → halt now (file-based:
                log.warning("stop requested — terminating batch")   # reaches child worker procs)
                return
            # CPU+RAM feedback: every ~1s ramp the render-admission ceiling toward the CPU target
            # (additive-increase when BOTH cpu and ram have headroom, back off when cpu is over or ram
            # hits the hard brake). Only renders acquire the gate, so an httpx-only crawl is unaffected.
            tick += 1
            if self.adaptive_on and psutil is not None and tick % 2 == 0:
                cpu = psutil.cpu_percent(interval=None)
                try:
                    ram = psutil.virtual_memory().percent
                except Exception:
                    ram = 0.0
                new = _aimd_limit(cpu, self.adaptive.limit, target, step,
                                  self.adaptive.min_limit, self.adaptive.max_limit,
                                  ram=ram, ram_target=ram_target, ram_hard=ram_hard)
                if new != self.adaptive.limit:
                    await self.adaptive.set_limit(new)
                _ADAPTIVE_STATE.update(enabled=True, cpu=round(cpu, 1), ram=round(ram, 1),
                                       target=target, render_limit=self.adaptive.limit,
                                       active=self.adaptive.active)
                if tick % 20 == 0:                  # ~10s heartbeat (if the logger is wired to stdout)
                    log.info("adaptive: cpu=%.0f%% ram=%.0f%% render_limit=%d active=%d",
                             cpu, ram, self.adaptive.limit, self.adaptive.active)
            # Big-site fill (~5s cadence): widen the surviving big hosts once the small sites
            # finish, so the batch tail doesn't run at 6 of 256 workers. See _refill_host_caps.
            if tick % 10 == 0 and os.environ.get("CRAWLER_HOST_FILL", "1") != "0":
                self._refill_host_caps()
            c3q = self.c3_queue.qsize() if self.c3_queue is not None else 0
            if (self.frontier.qsize() == 0 and self._inflight_items == 0
                    and self._pending_retries == 0
                    and c3q == 0 and self._inflight_c3 == 0):   # deferred C3 lane must also be empty
                return
            if loop.time() - self._last_progress > idle_s:
                # Report what was stuck so a premature idle-trip is diagnosable: frontier>0 + inflight>0
                # = hung workers; frontier=0 + pending>0 = stuck retries; frontier>0 + inflight=0 = workers
                # not pulling; c3_q/inflight_c3 = deferred stealth backlog. Also count host-slot cooldown waits.
                log.warning("engine idle for %ss — terminating batch (frontier=%d inflight=%d "
                            "pending_retries=%d c3_q=%d inflight_c3=%d slot_waiting=%d)", idle_s,
                            self.frontier.qsize(), self._inflight_items, self._pending_retries,
                            c3q, self._inflight_c3, getattr(self.host, "_waiting", 0))
                return

    def _note_weak_host(self, host: str) -> None:
        """Tally a weak-under-load signal (conn_refused / timeout) and promote the host to CAREFUL
        (conc=1 + delay) once it crosses the threshold. Called from BOTH the fetch-failure policy and
        the per-item watchdog — a host slow enough to blow the watchdog never reaches the policy
        path, so counting only there let it keep full concurrency while parking a tab-worker per URL
        for the whole ITEM_TIMEOUT. That is how two hosts drained the pool and throughput collapsed."""
        if not host or errors.is_careful_host(host) or host in self.host.force_careful:
            return
        self.host_weak[host] = self.host_weak.get(host, 0) + 1
        if self.host_weak[host] >= _env_int("CRAWLER_HOST_WEAK_FAILS", 3):
            self.host.make_careful(host)
            if self.history is not None:
                self.history.set_careful_host(host, True)   # persist → next run starts careful
            self.host_weak[host] = 0                         # one-shot: don't re-persist every failure
            log.info("auto-careful host=%s (weak under load: conn_refused/timeout) — throttled to conc=1",
                     host)

    async def _worker(self, tab) -> None:
        while True:
            item = await self.frontier.get()
            self._inflight_items += 1
            try:
                if stop_requested():
                    continue                # STOP: drop the item unprocessed so the frontier drains fast
                # Don't park another tab on a host whose permit queue is already deep. The wait in
                # HostLimiter.slot is unbounded and runs INSIDE the watchdog below, so on a conc=1
                # host every extra worker just idles a tab until the watchdog kills it — measured at
                # 97% of all item timeouts. Requeueing costs a few seconds; parking costs 240.
                # ponytail: queued-only, deliberately — deflecting on in-flight would throttle a
                # healthy conc=24 host. Damper, not a hard bound: a burst of workers can all read
                # depth before any reaches slot() and increments. Bounds the sustained queue (the
                # 22-tab case), not a single tick. Reserve at check time if that ever matters.
                qhost = (urlsplit(getattr(item, "url", "")).hostname or "").lower()
                if qhost and self.host.queue_depth(qhost) >= _env_int("CRAWLER_HOST_QUEUE_MAX", 2):
                    _tick_stat("deflected_host")
                    self._schedule_busy(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
                    continue
                # Per-item watchdog: no single page may wedge a tab-worker forever (a hung finalizer put
                # on a slow/stuck ingest, a render that never reaches networkidle, a wedged to_thread).
                # Without it one stuck item pins _inflight_items > 0 and blocks the clean frontier-empty
                # exit until the 900s idle timer. Generous (240s) so only a genuine hang trips it; the
                # cancelled item releases its slots via their context managers and the tab is recycled.
                tab = await asyncio.wait_for(self._process(item, tab),
                                             timeout=_env_int("CRAWLER_ITEM_TIMEOUT_S", 240))
            except RenderBusy:
                # Render pool saturated (globally, or this host's share). Give the page budget back
                # and requeue — parking the tab here is exactly what starved the pool before.
                try:
                    item.ctx.unreserve()     # reserve() ran in _process before the render path
                except Exception:
                    pass
                _tick_stat("deflected_render")
                self._schedule_busy(item, _env_float("CRAWLER_HOST_BUSY_REQUEUE_S", 3.0))
            except asyncio.TimeoutError:
                log.warning("item timeout — abandoning %s, recycling tab", getattr(item, "url", "?"))
                # Deliberately does NOT feed the weak-host tally. Measured: 97% of item timeouts are
                # on hosts ALREADY at concurrency 1 — they time out queueing for the permit, not
                # fetching. Promoting on this signal just makes more conc=1 hosts, i.e. more queues.
                # The queue cap in _worker is what actually stops the pool drain.
                try:
                    tab = await self._recycle(tab)
                except Exception:
                    pass                     # next item's slot()/recycle will replace a dead tab
            except Exception:
                log.exception("worker page failed: %s", getattr(item, "url", "?"))
            finally:
                # Park the tab on about:blank between items. A tab left sitting on the last
                # rendered page keeps compositing that page's animations forever (marketing
                # sites run rAF/CSS loops), and Playwright launches with
                # --disable-background-timer-throttling, so nothing throttles it. Measured per
                # idle tab: loaded page 5.8% of a core, about:blank 0.1%. At W*T tabs that idle
                # burn is ~9 cores, which the adaptive controller reads as "no CPU headroom" and
                # answers by throttling renders to the floor — the idle tabs starve the real
                # ones. Safe here: _process has already snapshotted to FetchResult, the
                # finalizer works off that snapshot, and cookies live in the BrowserContext,
                # not the page. Env-guarded only as an escape hatch for a capture mode that
                # needs the tab left intact after _process returns.
                if _blank_between_items():
                    try:
                        await tab.goto("about:blank")
                    except Exception:
                        pass                     # tab is dead/closing — _recycle handles it next item
                self._inflight_items -= 1
                self.frontier.task_done()

    def _schedule_retry(self, item: WorkItem, delay: float) -> None:
        """Requeue a transient failure after *delay* seconds (one more attempt)."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries + 1,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits))

    def _schedule_busy(self, item: WorkItem, delay: float) -> None:
        """Requeue a URL we declined to start because its host's permit queue was full.

        Preserves retries AND deadhost_waits: a busy host is neither a failure nor a dead host, and
        bumping either would let a merely-popular host exhaust an item's budget and drop the URL.
        Keeps _pending_retries > 0 so _drain won't call the frontier empty while these are in the air."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits))

    def _schedule_wait(self, item: WorkItem, delay: float) -> None:
        """Requeue a URL held off a COOLING host after *delay*s. Preserves item.retries (a cooldown
        wait is not a failure retry) and bumps deadhost_waits so a permanently-dead host caps out.
        Keeps _pending_retries > 0 so _drain won't exit the single-host frontier during the wait."""
        self._pending_retries += 1
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._do_requeue,
                        WorkItem(item.url, item.depth, item.ctx, item.retries,
                                 parent_url=item.parent_url, deadhost_waits=item.deadhost_waits + 1))

    def _do_requeue(self, w: WorkItem) -> None:
        self._pending_retries = max(0, self._pending_retries - 1)
        if self.frontier is not None:
            try:
                self.frontier.put_nowait(w)
            except Exception:
                pass

    async def _emit_items(self, ctx: "JobCtx", items: list[dict],
                          parent_url: str | None = None, depth: int = 1) -> int:
        """Turn feed/API items ({link,title,summary,published}) into synthetic HTML
        pages and run each through the exact build_document → gate → dedup → send
        path as a real fetch. Shared by the feed and API fallbacks. Returns docs SENT.
        parent_url/depth stamp the same provenance C1 does (feed items sit under the seed)."""
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
            hp = HarvestedPage(url=url, depth=depth, fetch=fr, pdf_links=[],
                               image_candidates=[], media_candidates=[], parent_url=parent_url,
                               served_by="feed_api")
            doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                          ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
            if doc is None:
                ctx.bump_reason("no_main_text")
                continue
            g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
            ctx.bump_reason(g.reason)
            if not g.keep:
                ctx.dropped_by_gate += 1
                _tick_stat('dropped_by_gate')
                continue
            if doc.content_hash in ctx.seen_hashes:
                ctx.skipped_duplicate += 1
                continue
            ctx.seen_hashes.add(doc.content_hash)
            ctx.kept += 1
            _tick_stat('kept')
            outcome = await asyncio.to_thread(ctx.ingest.send, doc)
            ctx.sent += 1
            _tick_stat('sent')
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
        return await self._emit_items(ctx, items, parent_url=item.url, depth=item.depth + 1)

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
        return await self._emit_items(ctx, items, parent_url=item.url, depth=item.depth + 1)

    def _c3_proxy_retry_budget(self) -> int:
        """Fresh-IP retries to attempt when a C3 render comes back IP-blocked. 0 (default) = off —
        set CRAWLER_C3_PROXY_RETRY=1 once CamoFox egresses through a rotating proxy (PolyProxy /
        residential), else fresh sessions just reuse the same blocked IP and retrying is wasted."""
        if os.environ.get("CRAWLER_C3_PROXY_RETRY", "0") != "1":
            return 0
        return max(1, _env_int("CRAWLER_C3_PROXY_RETRIES", 2))

    async def _c3_fresh_ip_retry(self, item: "WorkItem", timeout_s: float,
                                 solve_captchas: bool) -> dict | None:
        """A C3 render was an IP-block. Re-render up to the budget, each through a FRESH CamoFox
        session (unique userId ⇒ fresh upstream IP under a rotating gateway). Return the first snap
        whose body is NOT a block page, else None. No-op (None) when the budget is 0."""
        for _ in range(self._c3_proxy_retry_budget()):
            fresh = f"c3px-{uuid.uuid4().hex[:8]}"
            if solve_captchas:
                snap, _ci = await asyncio.to_thread(
                    camofox_client.render_with_solver, item.url, timeout_s, fresh)
            else:
                snap = await asyncio.to_thread(
                    camofox_client.render, item.url, timeout_s, False, fresh)
            if snap is None:
                continue
            if not errors.is_ip_block_page((snap.get("html") or "") or (snap.get("text") or "")):
                log.info("c3_proxy_retry cleared block job=%s url=%s user=%s",
                         item.ctx.job.job_id, item.url, fresh)
                return snap
        return None

    async def _try_camofox_fallback(self, item: WorkItem, host: str, solve_captchas: bool = False) -> bool:
        """Blocked host → C3: stealth-render THIS url through CamoFox and run it through
        the SAME build_document → gate → dedup → enrich → send path as a live fetch — so
        a C3 doc captures everything C1 does (html, text, images, screenshot, PDF, media/
        video links), per the job's capture list. Returns True if a doc was SENT. No-op
        (False) unless CAMOFOX_ENABLED=1. Budget is already reserved at dequeue."""
        if not camofox_client.enabled():
            return False
        ctx = item.ctx
        # Stealth render of a heavy page is inherently slower than a normal fetch — give C3
        # its own generous timeout (not the seed's ~30s httpx budget), tunable via env.
        timeout_s = _env_float("CRAWLER_CAMOFOX_TIMEOUT_S", 90.0)

        # Gate C3 renders through a small semaphore so a WAF-blocked section can't flood CamoFox.
        async with self._camofox_sem:
            if solve_captchas:
                snap, captcha_info = await asyncio.to_thread(camofox_client.render_with_solver, item.url, timeout_s)
            else:
                snap = await asyncio.to_thread(camofox_client.render, item.url, timeout_s)
                captcha_info = camofox_client.CaptchaInfo()

            if not snap:
                # Canonicalization strips www; many gov/mil apexes only serve on www — retry there
                # (mirrors the main render path's www fallback). Retry with a fresh user/session.
                www = _with_www(item.url)
                if www != item.url:
                    if solve_captchas:
                        snap, captcha_info = await asyncio.to_thread(camofox_client.render_with_solver, www, timeout_s)
                    else:
                        snap = await asyncio.to_thread(camofox_client.render, www, timeout_s)

            # LAST RESORT - the paid lane. Free C1 and free C3 have both been refused for this URL,
            # which is exactly the condition paid egress exists for, so free_tiers_tried is True by
            # construction here. Every spend still clears paid_proxy.allow(): master switch, host
            # allowlist, and a hard daily cap that survives restarts. A gate refusal is silent and
            # costs nothing; only a request that actually leaves is metered.
            if not snap and paid_proxy.allow(item.url, free_tiers_tried=True):
                paid_base = paid_proxy.base_url()
                log.info("c3_paid escalation url=%s (free C1+C3 refused)", item.url)
                try:
                    if solve_captchas:
                        snap, captcha_info = await asyncio.to_thread(
                            camofox_client.render_with_solver, item.url, timeout_s, None, paid_base)
                    else:
                        snap = await asyncio.to_thread(
                            camofox_client.render, item.url, timeout_s, False, None, paid_base)
                except Exception as exc:                 # noqa: BLE001 - the paid lane must never crash a crawl
                    log.warning("c3_paid failed url=%s: %s", item.url, exc)
                    snap = None
                paid_proxy.note_use(item.url)            # meter only a request that actually went out
                if snap:
                    _tick_stat("paid_recovered")
        if not snap:
            # C3 gave us nothing. If a captcha was detected on the way, say SO — otherwise this
            # returns silently and the caller books it as a generic render failure, which is how
            # thousands of captcha walls ended up indistinguishable from crashed browsers in the
            # backlog. "The browser died" and "a captcha stopped us" need different fixes.
            if captcha_info.captcha_type:
                reason = (errors.CAPTCHA_FAILED if captcha_info.solver
                          else errors.NEEDS_CAPTCHA_SOLVER)
                ctx.bump_reason(reason)
                try:
                    self.history.record_failure(
                        item.url, status=None, category=reason, failed_at=_now_iso(),
                        detail=f"{captcha_info.captcha_type}/{captcha_info.solvability}"
                               f": {captcha_info.error or 'not solved'}",
                        crawl_run_id=ctx.crawl_run_id)
                except Exception:
                    log.exception("could not record C3 captcha failure for %s", item.url)
                log.info("c3 captcha wall url=%s type=%s solvability=%s err=%s",
                         item.url, captcha_info.captcha_type, captcha_info.solvability,
                         captcha_info.error)
            return False

        final_url = snap.get("final_url") or item.url
        real_html = (snap.get("html") or "").strip()

        # The render SUCCEEDED but the server may still have said no. C3 used to hardcode status=200
        # here, so a 404 body was stored as a real document AND no failure was ever recorded — which
        # is why is_gone() never fired and the same dead URL came back every single run. 19.7% of
        # everything C3 stored was error pages and interstitials, against 0.2% for C1.
        # A None status means the browser could not tell us; that is treated as "carry on", not as
        # an error, so an unavailable timing entry can never silently discard a good page.
        c3_status = snap.get("status")
        if isinstance(c3_status, int) and c3_status >= 400:
            reason = errors.http_reason(c3_status)
            ctx.bump_reason(reason)
            # Feeds dedup.is_gone(): 410 is permanent immediately, 404 needs two strikes. This is
            # the write that makes "it 404s every run" turn into "stop asking".
            try:
                self.history.record_failure(item.url, status=c3_status, category=reason,
                                            failed_at=_now_iso(), crawl_run_id=ctx.crawl_run_id)
            except Exception:
                log.exception("could not record C3 failure for %s", item.url)
            log.info("c3 refused url=%s status=%s — not stored", item.url, c3_status)
            return False

        # C3 stealth beat the browser fingerprint check but the CDN may still IP-block us (Akamai
        # "Access Denied"). That arrives as an HTTP-200 render, so the BODY is the only signal. If
        # it's a block, retry through a fresh proxy IP; if still blocked (or no proxy wired), fail
        # honestly so the ladder continues — never send a block page as served_by_camofox.
        via_proxy = False
        if errors.is_ip_block_page(real_html or snap.get("text") or ""):
            snap = await self._c3_fresh_ip_retry(item, timeout_s, solve_captchas)
            if snap is None:
                ctx.bump_reason(errors.NEEDS_NETWORK_PATH)
                return False
            via_proxy = True
            final_url = snap.get("final_url") or item.url
            real_html = (snap.get("html") or "").strip()

        # Anything <400 (or unknown) reaches here; carry the REAL status so the Document records
        # what the server actually said instead of a fabricated 200.
        ok_status = c3_status if isinstance(c3_status, int) else 200
        if real_html:
            # Real rendered HTML → build_document extracts main_text/title/meta/tables and
            # enrich_assets pulls images/PDFs/media exactly like a live page.
            fr = FetchResult(url=item.url, final_url=final_url, status=ok_status,
                             content_type="text/html", kind="html", text_html=real_html,
                             screenshot_png=snap.get("screenshot"), tier=1,
                             fetched_at=_now_iso(), from_fixture=True)
        else:
            # No HTML (e.g. no CAMOFOX_API_KEY) → fall back to the aria snapshot as body.
            wrap = f"<html><head><title>{item.url}</title></head><body></body></html>"
            fr = FetchResult(url=item.url, final_url=final_url, status=ok_status,
                             content_type="text/html", kind="html", text_html=wrap,
                             inner_text=snap["text"], screenshot_png=snap.get("screenshot"),
                             tier=1, fetched_at=_now_iso(), from_fixture=True)

        # Attach C3 solver / proxy provenance to the fetch result so it flows into the Document.
        if captcha_info.captcha_type:
            fr.captcha_type = captcha_info.captcha_type
            fr.captcha_solved = captcha_info.solved
            fr.captcha_solver = captcha_info.solver
            fr.captcha_solver_cost_usd = captcha_info.cost_usd
        if via_proxy:
            # A fresh-IP retry cleared the block. Stamp provenance (flows to Document.proxy_country).
            fr.proxy_country = os.environ.get("CAMOFOX_PROXY_COUNTRY") or "rotated"

        # Capture candidates from the rendered HTML, per the job's capture list (mirrors _process).
        base = final_url
        html = fr.text_html if fr.is_html() else None
        pdf_links, image_candidates, media_candidates = _capture_assets(ctx.job, html, base)
        hp = HarvestedPage(url=item.url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates,
                           parent_url=item.parent_url, served_by="c3_camofox", served_from=final_url)

        # Enqueue child links so a C3-served site keeps crawling into its max_pages budget —
        # without this, a fully WAF-blocked site (served entirely by CamoFox) stops at the seed.
        # Children carry item.url as their parent (same as C1), so the dashboard tree is consistent.
        self._enqueue_links(ctx, fr, item.url, item.depth)

        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return False
        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
        ctx.bump_reason(g.reason)
        if not g.keep:
            ctx.dropped_by_gate += 1
            return False
        if doc.content_hash in ctx.seen_hashes:
            ctx.skipped_duplicate += 1
            return False
        ctx.seen_hashes.add(doc.content_hash)
        ctx.kept += 1
        # Store the expensive assets (images/PDF/screenshot; media recorded as links) — same
        # enrich step C1 uses. The screenshot PNG captured above is reused (no re-render).
        await asyncio.to_thread(extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        ctx.accepted += 1 if outcome.accepted else 0
        ctx.rejected += 0 if outcome.accepted else 1
        log.info("camofox_fallback job=%s url=%s chars=%d imgs=%d pdfs=%d media=%d captcha=%s solver=%s",
                 ctx.job.job_id, item.url, len(doc.main_text),
                 len(image_candidates), len(pdf_links), len(media_candidates),
                 fr.captcha_type or "none", fr.captcha_solver or "-")
        return True

    async def _recover_deadhost(self, item: WorkItem, host: str) -> bool:
        """A cooling host's queued URL — recover it LIVE through C3 (CamoFox) instead of dropping.
        No-op unless CAMOFOX_ENABLED=1 (camofox_client.enabled() gates _try_camofox_fallback).
        Reserves budget itself (the dead-host check runs before _process's reserve()).

        ponytail: CRAWLER_DEADHOST_RECOVER is GONE. C2 left the ladder, so the var's only remaining
        distinction was "C3 on/off" — which CAMOFOX_ENABLED already says, and a second switch that
        can disagree with the first is worse than no switch. Its default 'c2c3' would have kept
        working BY ACCIDENT ("c3" in "c2c3") while lying about what it does, and a stale 'c2' would
        have silently meant "no recovery at all" ("c3" in "c2" is False).

        Archival recovery is an explicit, dated, EXCLUSIVE mode now (crawler/c2.py). The archive's
        undated "newest capture" was never the same page as the one that just failed, so stamping it
        as this crawl's result was quietly wrong.
        """
        if not item.ctx.reserve():
            return False        # BUGFIX: reserve() returns False WITHOUT incrementing budget_used,
                                # so the old unreserve() here handed back budget never taken —
                                # inflating the page budget of every job that hit a cooling host.
        try:
            if await self._try_camofox_fallback(item, host, solve_captchas=True):
                item.ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                return True
        except Exception:
            pass
        item.ctx.unreserve()                          # nothing recovered → give the budget back
        return False

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

        # A crashed Chromium render says nothing about whether FIREFOX can load the page — different
        # engine, different fingerprint, different media/codec handling. _render_fallback has already
        # exhausted the cheap tiers (download refetch, httpx), so C3 is the last real option before
        # the page is written off. Without this, render_crash never escalated at all: the 4xx ladder
        # below is keyed on a status, and a crash has none.
        if reason == errors.RENDER_CRASH and camofox_client.enabled():
            try:
                if await self._try_camofox_fallback(item, host):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    log.info("render_crash → c3 recovered url=%s", item.url)
                    return None, tab          # C3 sent the document itself; nothing left to store
            except Exception:
                pass

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
                async with self.adaptive.slot():
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
                # Cheap JA3 GET before the expensive C3 spin-up — a TLS-fingerprint block often
                # clears here (always-render/careful hosts never ran the httpx-path impersonate).
                imp = await self._impersonate_try(item, item.url, host)
                if imp is not None:
                    ctx.bump_recovered(errors.SERVED_BY_IMPERSONATE)
                    log.info("impersonate_fallback job=%s host=%s url=%s", ctx.job.job_id, host, item.url)
                    return imp, tab
                # DEFER the expensive C3 → API → feed tail onto the tab-less c3_queue (drained by
                # _c3_worker). Frees THIS tab immediately so a WAF-blocked section can't park the pool
                # on 90s stealth renders. Terminal GONE (404/410) never defers — it falls through to the
                # inline gone branch below. No-op (inline path unchanged) unless CRAWLER_DEFER_C3=1.
                if self._defer_c3_enabled(status):
                    self._enqueue_c3(item, host, status)
                    return None, tab
                # C3 first: CamoFox stealth engine renders the LIVE page with engine-level
                # fingerprint spoofing — the freshest content if the WAF can be bypassed.
                # No-op unless CAMOFOX_ENABLED=1, so the ladder falls through when C3 is off.
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)   # bypassed the block, not a failure
                    return None, tab
                # WAF-free content ladder — try the cheapest/fullest path that lands:
                # 1st: official JSON API (DVIDS) — sanctioned, freshest, full bodies.
                api_sent = await self._try_api_fallback(item, host)
                if api_sent:
                    ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)  # API was the workaround → recovered
                    log.info("api_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, api_sent)
                    return None, tab
                # 2nd: RSS/Atom feed — broader fallback (summaries of the source's items).
                # (C2/archive is NOT a rung here any more: it is a separate, explicitly dated mode —
                #  see crawler/c2.py. An undated "newest capture" was never the page that failed.)
                sent = await self._try_feed_fallback(item, host)
                if sent:
                    ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)  # feed was the workaround → recovered
                    log.info("feed_fallback job=%s host=%s docs=%d", ctx.job.job_id, host, sent)
                    return None, tab                 # blocked HTML abandoned; feed docs already sent
                reason = errors.NEEDS_NETWORK_PATH
                fr.reason = reason
            else:
                reason = fr.reason or errors.classify_failure(fr.status, fr.error) or errors.OTHER
            policy = errors.policy_for_status(status) if status and status >= 400 else None

        # Persist every failure so 'gone' and 'retry next run' survive across runs.
        if self.history is not None:
            # fr.error carries the raw text ("render:Timeout 30000ms exceeded", "nav:net::ERR_…").
            # It is the only thing that makes a render_crash bucket diagnosable after the run.
            self.history.record_failure(item.url, status=status, category=reason,
                                        failed_at=fr.fetched_at or _now_iso(),
                                        crawl_run_id=ctx.crawl_run_id, detail=fr.error)

        # 404/410 → permanently gone (wires dedup.classify's gone branch via record_failure/is_gone).
        if policy == errors.GONE:
            ctx.bump_error(reason)
            return fr, tab

        # 429/503 → cool the WHOLE host down (Retry-After honored, capped).
        if policy == errors.COOLDOWN:
            self.host.cooldown(host, fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 30.0))

        # Hard network failures (DNS/refused/SSL) feed the per-host circuit breaker. When SSL
        # bypass is on, a cert error is NOT a reason to kill the host (Chromium proceeds past it).
        if reason in errors.HARD_FAIL and not (reason == errors.SSL and _ignore_https_errors()):
            self.host_fails[host] = self.host_fails.get(host, 0) + 1
            # Back off the host on EVERY hard failure, not only when the breaker trips. Without
            # this there is no backoff at all between fail 1 and fail `thr` — the pool keeps
            # slamming a host that is already refusing, trips the breaker, and after each
            # cooldown the revived URLs re-trip it in `thr` requests. That oscillation is how one
            # refusing host (sipri.org) turned into 616 conn_refused / 769 skipped. Progressive:
            # 2s, 4s, 6s... so a host that refuses once gets room to recover before we try again.
            # ponytail: reuses the 429/503 _next_ok gate cooldown() already drives — no new
            # machinery, and cooldown() self-caps at CRAWLER_COOLDOWN_CAP_S.
            self.host.cooldown(host, _env_float("CRAWLER_HARD_FAIL_BACKOFF_S", 2.0)
                               * self.host_fails[host])
            # Careful gov/mil hosts get 2x headroom (their failures are often transient under load).
            thr = _env_int("CRAWLER_HOST_HARD_FAILS", 6)
            if errors.is_careful_host(host) or host in self.host.force_careful:
                thr *= 2
            if self.host_fails[host] >= thr:
                # COOL the host down (not permanent death): its queued URLs are recovered via
                # C2/C3 while cooling, then it revives for a fresh live attempt after the window.
                self.dead_hosts.add(host)
                self.host_cooldown[host] = (asyncio.get_running_loop().time()
                                            + _env_float("CRAWLER_HOST_COOLDOWN_S", 120.0))
                self.host_fails[host] = 0
                log.warning("host_cooldown host=%s (recovering queued URLs via C2/C3)", host)

        # Weakness under load (conn_refused/reset/timeout) → auto-slow to CAREFUL (conc 1 + delay)
        # BEFORE the dead-host breaker trips (mirrors host_render_always: learn in-run, persist, seed
        # next run). Careful's throttle is what STOPS the refuse/reset cascade (the SIPRI case), so it
        # must fire below CRAWLER_HOST_HARD_FAILS. Reaches here for both CONN_REFUSED (a HARD_FAIL) and
        # TIMEOUT (which returns at the transient requeue just below) → count before that return.
        if reason in errors.WEAK_UNDER_LOAD:
            self._note_weak_host(host)

        # Transient (5xx/429/timeout/render_crash) → requeue and try again.
        transient = policy in (errors.RETRY_LATER, errors.COOLDOWN) or reason in errors.TRANSIENT
        if transient and item.retries < _env_int("CRAWLER_INRUN_RETRIES", 3):
            ctx.unreserve()                          # give the page budget back for the retry
            base = fr.retry_after_s or _env_float("CRAWLER_COOLDOWN_BASE_S", 8.0)
            # EXPONENTIAL, and jittered. Every URL that fails in the same instant (a host hiccup, a
            # browser pool wobble) was previously rescheduled to the identical second, so they all
            # came back together and re-created the pile-up that broke them. The spread is what
            # stops attempt 2 from being attempt 1 with more contention.
            delay = min(base * (2 ** item.retries), 120.0) * random.uniform(0.5, 1.5)
            self._schedule_retry(item, delay)
            log.info("retry job=%s url=%s reason=%s attempt=%d delay=%.0fs",
                     ctx.job.job_id, item.url, reason, item.retries + 1, delay)
            return None, tab                         # not counted — the retry counts if it fails again

        ctx.bump_error(reason)
        log.info("fetch_fail job=%s url=%s status=%s reason=%s",
                 ctx.job.job_id, item.url, status, reason)
        return fr, tab

    # ── deferred C3 lane (CRAWLER_DEFER_C3=1) ────────────────────────────────
    def _defer_c3_enabled(self, status=None) -> bool:
        """Defer the C3/API/feed tail to the tab-less c3_queue? Only with CRAWLER_DEFER_C3=1 and C3
        available, and NEVER for a terminal GONE (404/410 — those take the inline gone branch)."""
        return (os.environ.get("CRAWLER_DEFER_C3", "0") == "1"
                and camofox_client.enabled()
                and (not status or errors.policy_for_status(status) != errors.GONE))

    def _enqueue_c3(self, item: WorkItem, host: str, status) -> None:
        """Push a blocked page onto the deferred C3 lane. Non-blocking (put_nowait) so a tab is never
        parked; if the lane is saturated, tag needs_network_path now instead of blocking."""
        try:
            self.c3_queue.put_nowait((item, host, status))
        except asyncio.QueueFull:
            self._tag_netpath(item, status)

    async def _c3_worker(self) -> None:
        """Drain the deferred C3 lane off the tab-workers (mirrors _finalizer_loop + the _worker STOP
        discipline). _camofox_sem inside _try_camofox_fallback is the real concurrency cap."""
        q = self.c3_queue
        while True:
            item, host, status = await q.get()
            self._inflight_c3 += 1
            try:
                if stop_requested():
                    continue                     # STOP: drop unprocessed so the lane drains fast
                await asyncio.wait_for(self._run_c3_ladder(item, host, status),
                                       timeout=_env_int("CRAWLER_C3_ITEM_TIMEOUT_S", 300))
            except asyncio.TimeoutError:
                log.warning("c3 item timeout — %s", getattr(item, "url", "?"))
                try:
                    self._tag_netpath(item, status)   # a hung stealth render → record needs_network_path
                except Exception:
                    pass
            except Exception:
                log.exception("c3 worker failed: %s", getattr(item, "url", "?"))
            finally:
                self._mark_progress()            # a slow stealth render is progress, not a wedge
                self._inflight_c3 -= 1
                q.task_done()

    async def _run_c3_ladder(self, item: WorkItem, host: str, status=None) -> None:
        """The expensive tail lifted from _on_failure (C3 → API → feed → tag needs_network_path), run
        off the tab. Behaviour is identical to the inline ladder, just not parking a worker.
        _try_camofox_fallback enqueues any C3-discovered child links back onto the frontier."""
        ctx = item.ctx
        if await self._try_camofox_fallback(item, host, solve_captchas=True):
            ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
            return
        if await self._try_api_fallback(item, host):
            ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)
            log.info("api_fallback job=%s host=%s", ctx.job.job_id, host)
            return
        if await self._try_feed_fallback(item, host):
            ctx.bump_recovered(errors.NEEDS_NETWORK_PATH)
            log.info("feed_fallback job=%s host=%s", ctx.job.job_id, host)
            return
        self._tag_netpath(item, status)

    def _tag_netpath(self, item: WorkItem, status) -> None:
        """A block even C3 couldn't clear → persist needs_network_path (queryable for the final pass /
        next run) + count it. Never a URL list in memory — just an int and the sqlite row.

        A GONE status is NOT a block. 404/410 means the page does not exist, and no proxy, VPN or
        residential egress will conjure it — measured: 2,024 of 4,111 needs_network_path rows (49%)
        were plain 404s, so the queue of "sites we cannot reach" was double its real size and the
        top of it was pages that were never there.
        """
        if status and errors.policy_for_status(status) == errors.GONE:
            reason = errors.http_reason(status)
            if self.history is not None:
                self.history.record_failure(item.url, status=status, category=reason,
                                            failed_at=_now_iso(),
                                            crawl_run_id=item.ctx.crawl_run_id)
            item.ctx.bump_error(reason)
            return
        if self.history is not None:
            self.history.record_failure(item.url, status=status,
                                        category=errors.NEEDS_NETWORK_PATH, failed_at=_now_iso(),
                                        crawl_run_id=item.ctx.crawl_run_id)
        item.ctx.bump_error(errors.NEEDS_NETWORK_PATH)
        self._netpath_tagged += 1
        log.info("needs_network_path job=%s url=%s status=%s", item.ctx.job.job_id, item.url, status)

    async def _netpath_final_pass(self, ctxs) -> None:
        """After the frontier + c3_queue drain, retry the needs-network-path backlog through a
        US/residential egress — CRAWLER_C3_PROXY_RETRY rotates a fresh IP inside _try_camofox_fallback.
        URLs stream lazily from sqlite (a 100k backlog never materializes). No egress wired → log the
        honest count and leave the rows in crawl_pages for a future run."""
        if os.environ.get("CRAWLER_NETPATH_FINAL", "0") != "1" or self._netpath_tagged == 0:
            return
        if self._c3_proxy_retry_budget() == 0:
            log.warning("netpath: %d URLs need a residential/US egress; left in crawl_pages for a "
                        "future run (set CRAWLER_C3_PROXY_RETRY=1 + a proxy)", self._netpath_tagged)
            return
        by_host: dict[str, "JobCtx"] = {}
        for c in ctxs:
            for d in getattr(c, "seed_domains", []) or []:
                by_host.setdefault((urlsplit(d).hostname or "").lower(), c)
        limit = _env_int("CRAWLER_NETPATH_MAX", 5000)
        log.info("netpath final pass: retrying up to %d URLs via residential/US egress", limit)

        async def _runner():
            for url in self.history.iter_needs_network_path(self._run_started_iso, limit):
                if stop_requested():
                    break
                h = (urlsplit(url).hostname or "").lower()
                ctx = by_host.get(h)
                if ctx is None or not ctx.reserve():
                    continue
                await self._run_c3_ladder(WorkItem(url, 0, ctx), h)
                self._mark_progress()
        try:
            await asyncio.wait_for(_runner(), timeout=_env_int("CRAWLER_NETPATH_DRAIN_S", 600))
        except asyncio.TimeoutError:
            log.warning("netpath final pass timed out (%ss) — remaining URLs left for next run",
                        _env_int("CRAWLER_NETPATH_DRAIN_S", 600))

    def _enqueue_candidate(self, ctx: "JobCtx", cl: str, parent_url: str, depth: int) -> bool:
        """Apply the seen/domain/trap/query-variant guards to ONE already-canonicalized URL and
        enqueue it at depth+1. Returns True if enqueued. Shared by link-enqueue and click-discovery.
        Loop-thread-only; the caller wraps this so one bad candidate can't abort the rest."""
        if cl in ctx.seen:
            return False
        if ctx.job.same_domain_only and not _allowed_offsite(cl, parent_url, ctx.seed_domains):
            return False
        if not _in_path_scope(cl, ctx.job.path_scope):
            ctx.scope_skipped += 1
            return False
        # Trap guards: URL-shape (loop/length) + calendar/facet query explosion.
        if errors.looks_like_trap(cl):
            ctx.trap_skipped += 1
            return False
        qp = urlsplit(cl)
        if qp.query:
            key = f"{qp.hostname}{qp.path}"
            n = ctx.query_variants.get(key, 0) + 1
            if n > _env_int("CRAWLER_MAX_QUERY_VARIANTS", 20):
                ctx.trap_skipped += 1
                return False
            ctx.query_variants[key] = n
        ctx.seen.add(cl)
        self.frontier.put_nowait(WorkItem(cl, depth + 1, ctx, parent_url=parent_url))
        return True

    def _enqueue_links(self, ctx: "JobCtx", fr: FetchResult, url: str, depth: int) -> None:
        """Extract child links from a fetched page and enqueue them within the depth/domain
        budget (loop thread; parse is cheap CPU). Shared by the live C1 path AND the C3 CamoFox
        fallback — WITHOUT this a site served entirely by C3 would dead-end at the seed page and
        never reach its max_pages budget, since only the frontier drives the crawl.

        Per-link resilient: canonicalize_url/urlsplit can raise on ONE malformed href (bad port,
        bad IPv6); we skip that link, NOT the whole page (which would drain the frontier → shallow
        crawl). canonicalize_url is now defensive too, so this is belt-and-suspenders."""
        base = fr.final_url or url
        if not (fr.is_html() and fr.text_html and _depth_ok(depth, ctx.job.max_depth) and ctx.has_budget()):
            return
        try:
            # job.link_relevance_keywords: follow only links whose ANCHOR TEXT mentions a keyword.
            # This existed in harvest.py but was never ported here, so it silently did nothing on
            # the async (production) engine — which is how a /about/defence seed on a newspaper
            # walked into its entertainment archive. Empty list = follow everything (default).
            if ctx.job.link_relevance_keywords:
                links = [u for u, t in parse.extract_links_with_text(fr.text_html, base)
                         if _link_is_relevant(t, ctx.job.link_relevance_keywords)]
            else:
                links = parse.extract_links(fr.text_html, base)
        except Exception as exc:
            log.debug("extract_links failed base=%s: %s", base, exc)
            return
        for link in links:
            try:
                self._enqueue_candidate(ctx, canonicalize_url(link), url, depth)
            except Exception as exc:
                log.debug("enqueue skip bad link %r on %s: %s", link, url, exc)
                continue

    async def _click_discover(self, ctx: "JobCtx", tab, origin_url: str, depth: int) -> int:
        """Click every NON-<a href> clickable (button/onclick/role/data-href/cursor:pointer) to
        surface JS/SPA navigation that extract_links can't see, enqueue same-domain discoveries at
        depth+1, and restore the tab to origin_url. Returns count enqueued. All awaits are on `tab`
        → stays on the loop thread. Bounded by CRAWLER_MAX_CLICKS_PER_PAGE so a busy page can't spin.
        Hazards handled: popups/downloads (opener-scoped auto-close/cancel), off-domain nav (skip +
        back), modal/no-nav (skip), any per-click error (logged, never fatal)."""
        import asyncio as _asyncio
        cap = _env_int("CRAWLER_MAX_CLICKS_PER_PAGE", 40)
        nav_wait = _env_int("CRAWLER_CLICK_NAV_WAIT_MS", 1500)
        click_to = _env_int("CRAWLER_CLICK_TIMEOUT_MS", 4000)
        enqueued = 0
        clicked: set[str] = set()

        def _close_popup(p):
            _asyncio.ensure_future(_swallow(p.close()))

        def _cancel_dl(d):
            _asyncio.ensure_future(_swallow(d.cancel()))

        tab.on("popup", _close_popup)
        tab.on("download", _cancel_dl)
        try:
            for _ in range(cap):
                if not ctx.has_budget():
                    break
                # Reset to a stable base if a prior click navigated us away (stale DOM otherwise).
                if tab.url != origin_url:
                    try:
                        await tab.goto(origin_url, timeout=click_to * 3, wait_until="domcontentloaded")
                    except Exception:
                        break
                try:
                    cands = await tab.evaluate(_CANDIDATE_JS, cap * 3)
                except Exception:
                    break
                pick = next((c for c in cands if c.get("sig") not in clicked), None)
                if pick is None:
                    break
                clicked.add(pick.get("sig"))
                try:
                    await tab.locator(f'[data-__cd="{pick["idx"]}"]').first.click(
                        timeout=click_to, no_wait_after=True)
                    await tab.wait_for_timeout(nav_wait)
                except Exception as exc:
                    log.debug("click_discover click failed on %s: %s", origin_url, exc)
                    continue
                self._mark_progress()               # long discovery must not look idle
                new = tab.url
                if new and new != origin_url:
                    if same_site(new, origin_url):
                        cl = canonicalize_url(new)
                        try:
                            if self._enqueue_candidate(ctx, cl, origin_url, depth):
                                enqueued += 1
                        except Exception:
                            pass
                    # Return to origin to keep clicking (go_back, else cold goto).
                    try:
                        await tab.go_back(timeout=click_to * 2, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    if tab.url != origin_url:
                        try:
                            await tab.goto(origin_url, timeout=click_to * 3, wait_until="domcontentloaded")
                        except Exception:
                            break
        finally:
            try:
                tab.remove_listener("popup", _close_popup)
                tab.remove_listener("download", _cancel_dl)
            except Exception:
                pass
        return enqueued

    @asynccontextmanager
    async def _render_permit(self, host: str):
        """Per-host share of the GLOBAL render pool, then a bounded wait for a permit.

        The adaptive gate is host-agnostic and is acquired before the per-host slot, so without a
        per-host cap one site's render-heavy frontier takes every permit and starves every other
        host. Both failure modes raise RenderBusy so the caller requeues instead of parking a tab."""
        cap = _env_int("CRAWLER_HOST_RENDER_MAX", 8)
        if self._render_hosts.get(host, 0) >= cap:
            raise RenderBusy(f"host {host} already holds {cap} render permits")
        self._render_hosts[host] = self._render_hosts.get(host, 0) + 1
        try:
            async with self.adaptive.slot(timeout=_env_float("CRAWLER_RENDER_WAIT_S", 8.0)):
                yield
        finally:
            n = self._render_hosts.get(host, 1) - 1
            if n > 0:
                self._render_hosts[host] = n
            else:
                self._render_hosts.pop(host, None)   # don't grow a dict entry per host forever

    def _note_render(self, host: str, rendered: bool) -> None:
        """Track how often a host's pages need the browser. Once the first few pages are
        mostly renders (httpx keeps coming up empty), escalate the host to always-render so
        we stop paying the wasted httpx round-trip — this is the "site won't open without JS
        → switch to render_js always" behaviour, decided automatically and per-host."""
        if host in self.host_render_always:
            return
        st = self.host_render_stat.setdefault(host, [0, 0])       # [rendered, total]
        st[0] += 1 if rendered else 0
        st[1] += 1
        probe = _env_int("CRAWLER_AUTO_PROBE", 4)
        if st[1] >= probe and st[0] >= max(1, probe - 1):         # ≥3 of the first 4 needed JS
            self.host_render_always.add(host)
            hist = getattr(self, "history", None)
            if hist is not None:                                  # persist so next run skips the probe
                hist.set_render_host(host, True)
            log.info("render mode: host %s → always-render (%d/%d early pages needed JS)",
                     host, st[0], st[1])

    async def _impersonate_try(self, item: "WorkItem", url: str, host: str) -> "FetchResult | None":
        """Cheap curl_cffi real-Chrome-JA3 GET for a TLS-fingerprint-blocked page — clears many WAF
        403/401s without spinning a browser tab or C3. Runs under the host politeness slot. Returns
        good HTML (<400, not a 200 block page) or None. No-op when CRAWLER_IMPERSONATE_FETCH=0."""
        try:
            async with self.host.slot(url):
                fr = await asyncio.to_thread(item.ctx.fetcher._impersonate_fetch, url)
        except Exception:
            return None
        if (fr is not None and fr.is_html() and fr.text_html and fr.status and fr.status < 400
                and not errors.is_ip_block_page(fr.text_html)):
            log.info("impersonate_fetch cleared block job=%s url=%s status=%s",
                     item.ctx.job.job_id, url, fr.status)
            return fr
        return None

    async def _fetch_page(self, item: "WorkItem", tab, host: str):
        """Fetch item.url per the render mode; returns (FetchResult, tab).
        auto  → httpx first, render only JS-dependent/blocked pages, escalate the host if it
                keeps needing the browser. never → httpx only. always → Playwright only."""
        ctx = item.ctx
        url = item.url
        mode = _render_mode(ctx.job)
        # A page we must screenshot needs a browser regardless, so in auto we render it INLINE
        # (one pass grabs html + screenshot) rather than httpx-fetching and then relaunching a
        # browser per page just for the shot. So the httpx fast path only applies when no
        # screenshot is wanted — drop "screenshot" from capture to crawl a whole site at httpx speed.
        wants_shot = "screenshot" in ctx.job.capture
        # Careful gov/mil hosts: skip the httpx probe entirely. A bot-UA httpx hit is what draws
        # Akamai connection-resets (a HARD_FAIL that trips the breaker) — render with a real
        # browser UA at concurrency-1 instead. So auto/never both become render for these hosts.
        # A per-host override host is a gentle-httpx host (weak/flaky no-bot server) — take the light
        # httpx path even if it's .gov/.mil, since render just gives a flaky box more sockets to drop.
        careful = (errors.is_careful_host(host) or host in self.host.force_careful) \
            and self.host.override_for(host) is None
        try_httpx = not careful and ((mode == "never") or (
            mode == "auto" and not wants_shot and host not in self.host_render_always))
        # A URL that plainly serves a FILE must never reach the browser. Chromium answers a download
        # with ERR_ABORTED, which looked like a crashed render — renault-trucks.com/en/media/1119/
        # download was counted as render_crash when nothing was wrong: the PDF was right there.
        # httpx fetches the bytes, which is what the capture wanted in the first place.
        if _looks_like_download(url):
            try_httpx = True

        http_fr = None
        if try_httpx:
            try:
                async with self.host.slot(url):
                    http_fr = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
            except Exception as e:
                http_fr = FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                                      error=f"http:{e}", reason=errors.OTHER)
            if mode == "never":
                return http_fr, tab
            if not _needs_render(http_fr):
                self._note_render(host, False)
                return http_fr, tab
            # A TLS-fingerprint block (401/403) usually clears with a real-Chrome JA3 GET — try that
            # cheap tier before spending a browser tab. Success ⇒ no render, no escalation signal.
            if http_fr.status in (401, 403):
                imp = await self._impersonate_try(item, url, host)
                if imp is not None:
                    self._note_render(host, False)
                    return imp, tab
            # else: JS-dependent / blocked → fall through to the browser (counts toward escalation)

        # Render path: always mode, an escalated host, a screenshot page, or an auto page that
        # httpx couldn't serve.
        try:
            # Render permit (per-host share + bounded wait on the global gate), then host slot.
            async with self._render_permit(host):
                async with self.host.slot(url):
                    fr = await _render_page(tab, url, self.host.timeout_ms(host),
                                            ctx.job.capture, ctx.job.interaction,
                                            discover=_depth_ok(item.depth, ctx.job.max_depth))
        except RenderBusy:
            raise                       # deflect to _worker's requeue; NOT a render crash
        except Exception as e:
            tab = await self._recycle(tab)
            fr = await self._render_fallback(item, url, host, http_fr, e)
        if try_httpx and mode == "auto":     # httpx was tried and wasn't enough → escalation signal
            self._note_render(host, True)
        return fr, tab

    async def _render_fallback(self, item: "WorkItem", url: str, host: str,
                               http_fr: "FetchResult | None", exc: Exception) -> "FetchResult":
        """A render died — get the page by other means before calling it a failure.

        Previously this only REUSED an httpx result that happened to have been fetched already. When
        the render path was chosen up front (render-always host, screenshot job, careful host) there
        was no such result, so a crashed tab meant the page was simply lost and logged as
        `render_crash` — 2,616 of them, with no attempt to fetch the page any other way.

        The ladder here is ordered by cost, and every rung is a genuinely different mechanism, which
        is the point: a Chromium crash tells you nothing about whether httpx or Firefox can load it.
            1. the URL is a FILE (ERR_ABORTED) → fetch the bytes, which is what it needed all along
            2. an httpx result already in hand → use it
            3. httpx not tried yet → try it now
            4. C3 CamoFox — a different browser engine (Firefox), so a Chromium-specific crash,
               fingerprint block or codec fault can still succeed
        Only when all four are exhausted is it recorded as a failure, and by then `render_crash`
        means "nothing could fetch this", not "the first thing we tried broke".
        """
        err = f"render:{exc}"
        ctx = item.ctx

        # 1 · Not a page at all. Chromium aborts navigation when the response is a download, so the
        #     URL is fine — the browser was the wrong tool. Refetching over httpx gets the PDF.
        if errors.is_download_abort(err):
            try:
                async with self.host.slot(url):
                    got = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
                if got is not None and (got.body_bytes or got.text_html) and \
                        got.status and got.status < 400:
                    log.info("render→httpx (download) url=%s kind=%s", url, got.kind)
                    _tick_stat("recovered")
                    return got
            except Exception:
                pass

        # 2 · An httpx body we already paid for.
        if (http_fr is not None and http_fr.is_html() and http_fr.text_html
                and http_fr.status and http_fr.status < 400):
            return http_fr

        # 3 · httpx was never tried (render-always / screenshot / careful host). Try it now: a
        #     server-rendered page does not need the browser that just died.
        if http_fr is None:
            try:
                async with self.host.slot(url):
                    got = await asyncio.to_thread(ctx.fetcher._http_fetch, url, None)
                if got is not None and got.is_html() and got.text_html and \
                        got.status and got.status < 400 and not _needs_render(got):
                    log.info("render→httpx recovered url=%s", url)
                    _tick_stat("recovered")
                    return got
                http_fr = got
            except Exception:
                pass

        # 4 · A different browser engine (CamoFox/Firefox) is worth trying after a Chromium crash,
        #     but it is NOT done here: _try_camofox_fallback sends the document itself, so returning
        #     anything from this function would store it a second time. Falling through with a
        #     render_crash reason hands the URL to _on_failure, which owns the C3 escalation and
        #     already knows how to end the item when C3 succeeds.
        #
        # Keep the ORIGINAL render error as the detail — it is the most specific thing we know, and
        # it is what makes the render_crash bucket diagnosable after the run.
        reason = errors.classify_failure(None, err) or errors.RENDER_CRASH
        return FetchResult(url=url, final_url=url, status=None, fetched_at=_now_iso(),
                           error=err, reason=reason)

    async def _conditional_unchanged(self, item: "WorkItem", host: str) -> bool:
        """Incremental recrawl: send a conditional GET (If-None-Match/If-Modified-Since from
        crawl_history). A 304 means the page is unchanged since last run → the caller skips the
        expensive render + re-ingest. Bumps last_seen (COALESCE preserves the stored hash).
        Careful gov/mil hosts are exempt — a bot-UA probe draws the same WAF resets that trip
        the breaker, so those recrawl by rendering normally."""
        if self.history is None:
            return False
        if errors.is_careful_host(host) or host in self.host.force_careful:
            return False
        cond = self.history.conditional_headers(item.url)
        if not cond:                          # never seen → nothing to be conditional about
            return False
        try:
            async with self.host.slot(item.url):
                fr = await asyncio.to_thread(item.ctx.fetcher._http_fetch, item.url, cond)
        except Exception:
            return False
        if fr is not None and fr.not_modified:
            self.history.upsert(item.url, content_hash=None,
                                etag=fr.etag or cond.get("If-None-Match"),
                                last_modified=fr.last_modified, status=304, fetched_at=_now_iso(),
                                crawl_run_id=item.ctx.crawl_run_id)
            return True
        return False

    async def _process(self, item: WorkItem, tab):
        ctx = item.ctx
        url = item.url
        host = (urlsplit(url).hostname or "").lower()

        # Circuit breaker + known-gone skips — BEFORE reserve() so a cooling host or a
        # permanently-gone URL never burns this job's page budget.
        if host in self.dead_hosts:
            loop = asyncio.get_running_loop()
            if loop.time() >= self.host_cooldown.get(host, 0.0):
                self.dead_hosts.discard(host)        # cooldown elapsed → revive, try live again
                self.host_fails[host] = 0
            elif await self._recover_deadhost(item, host):
                return tab                            # recovered via C2/C3 (counted as recovered)
            elif item.deadhost_waits < _env_int("CRAWLER_DEADHOST_MAX_WAITS", 4):
                # Host cooling, no C2/C3 recovery. Do NOT drop the URL — on a single-host crawl that
                # drains the frontier to empty and ends the job before the 120s revival fires. Requeue
                # it to retry when the cooldown lifts; jitter so ~1500 held URLs don't revive in one
                # thundering herd and instantly re-trip the breaker. _mark_progress keeps the engine
                # from idle-terminating during the wait (CRAWLER_ENGINE_IDLE_S == the cooldown); safe
                # because deadhost_waits is capped.
                wait = max(1.0, self.host_cooldown.get(host, 0.0) - loop.time()) + random.uniform(0.0, 5.0)
                self._schedule_wait(item, wait)
                self._mark_progress()
                return tab
            else:
                ctx.bump_skip(errors.HOST_DOWN)       # waited out the cap → host really is dead; SKIP
                return tab
        if self.history is not None and self.history.is_gone(url):
            ctx.bump_skip(errors.GONE_SKIP)
            return tab
        if errors.careful_off_peak_now(host):        # gov host, outside allowed hours
            ctx.bump_skip(errors.OFF_PEAK)
            return tab

        # Incremental recrawl (opt-in): a conditional GET says 304 Not-Modified → the page is
        # unchanged since last run, so skip it (no render, no re-ingest). Runs BEFORE reserve()
        # so unchanged pages don't burn the page budget — the budget goes to new/changed pages.
        # The depth gate keeps the shallow discovery surface fresh (see _incremental_skip_eligible);
        # CRAWLER_INCREMENTAL_FRESH_DEPTH sets it (default 1 = seeds + top listing layer, 0 = seeds only).
        fresh_depth = _env_int("CRAWLER_INCREMENTAL_FRESH_DEPTH", 1)
        if (_incremental_skip_eligible(ctx.job.incremental, item.depth, fresh_depth)
                and await self._conditional_unchanged(item, host)):
            ctx.not_modified += 1
            self._mark_progress()
            _tick_page()                              # incremental recrawl: 304-checked page counts
            return tab

        if ctx.done or not ctx.reserve():
            return tab

        # Proactive C3: for known high-WAF hosts, skip the noisy C1 live fetch and go
        # straight to CamoFox with captcha solving. Only if C3 fails do we fall through
        # to the normal live fetch / C2 / API ladder. Skipped for override hosts — a flaky no-bot
        # box (mod.gov.my) is served by light httpx, not a heavy stealth render from the same IP.
        if _is_waf_host(host) and camofox_client.enabled() and self.host.override_for(host) is None:
            try:
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    self._audit(url, host, 200, errors.SERVED_BY_CAMOFOX, "allow")
                    _tick_page()                          # proactive-C3 page counts toward speed too
                    return tab
            except Exception:
                log.info("proactive_c3_failed job=%s url=%s", ctx.job.job_id, url, exc_info=True)

        robots_decision = "off"
        # Consult robots only when the shared cache exists AND this job respects robots — a bypass
        # job in a mixed batch skips the consult and the robots→C3 detour, fetching directly.
        if self.robots and _job_respects_robots(ctx.job, self.seed.capture_defaults):
            try:                                  # allow | deny | no_robots (richer than a bool)
                robots_decision = await asyncio.to_thread(self.robots.decision, url)
            except Exception:
                robots_decision = "allow"
            if robots_decision == "deny":
                # robots.txt forbids the live fetch — don't just drop it: route the URL through C3
                # (CamoFox), which the operator opted into for exactly these hosts. If that comes up
                # empty we record the robots block and skip. (C2/archive is no longer a rung here —
                # it is a separate, explicitly dated mode; see crawler/c2.py.)
                if await self._try_camofox_fallback(item, host, solve_captchas=True):
                    ctx.bump_recovered(errors.SERVED_BY_CAMOFOX)
                    self._audit(url, host, None, errors.SERVED_BY_CAMOFOX, robots_decision)
                    return tab
                ctx.bump_skip(errors.ROBOTS)
                self._audit(url, host, None, errors.ROBOTS, robots_decision)
                return tab

        fr, tab = await self._fetch_page(item, tab, host)
        self._mark_progress()
        _tick_page()                                  # live speed metric: one page worked through
        self._audit(url, host, fr.status,
                    fr.reason or errors.classify_failure(fr.status, fr.error), robots_decision)

        # A WAF wall served with HTTP 200 is still a block, not content. CloudFront/Cloudflare do
        # exactly this, so the status check alone stored 709 "403 ERROR / Request blocked" bodies as
        # real documents — and worse, recorded those URLs as CRAWLED, so incremental recrawl skipped
        # them forever. Rewrite it to a 403 so it takes the normal failure ladder (C3 escalation +
        # record_failure), which is what should have happened at fetch time.
        if _is_block_body(fr):
            log.info("block page served as 200 — treating as 403: %s", url)
            fr.status, fr.reason = 403, errors.NEEDS_NETWORK_PATH
        if fr.error or not fr.status or fr.status >= 400:
            fr, tab = await self._on_failure(item, fr, tab, host)
            if fr is None or fr.error or not fr.status or fr.status >= 400:
                return tab
            if _is_block_body(fr):        # the ladder came back with another wall — don't store it
                return tab
        ctx.fetched += 1
        _tick_stat('fetched')
        self.host_fails[host] = 0        # a success clears the host's breaker count

        # Enqueue child links within depth/domain budget (loop thread; parse is cheap CPU).
        self._enqueue_links(ctx, fr, url, item.depth)
        # Links harvested from pagination pages 2+ (opt-in interaction.paginate) — enqueue them
        # too so a paginated tender listing's later-page links reach the frontier.
        if fr.extra_links and _depth_ok(item.depth, ctx.job.max_depth) and ctx.has_budget():
            for link in fr.extra_links:
                try:
                    self._enqueue_candidate(ctx, canonicalize_url(link), url, item.depth)
                except Exception:
                    continue

        # Click-discovery (opt-in, render_js only): after href links are enqueued, click the
        # NON-href clickables to surface JS/SPA navigation. Runs inside a re-acquired host slot
        # for politeness (the render slot already closed). A wedged tab is recycled.
        if (ctx.job.click_discovery and ctx.job.render_js and fr.is_html()
                and os.environ.get("CRAWLER_CLICK_DISCOVERY", "1") != "0"
                and _depth_ok(item.depth, ctx.job.max_depth) and ctx.has_budget()
                and host not in self.dead_hosts):
            try:
                async with self.host.slot(url):
                    await self._click_discover(ctx, tab, fr.final_url or url, item.depth)
            except Exception as exc:
                log.debug("click_discovery failed %s: %s", url, exc)
                tab = await self._recycle(tab)

        # Capture candidates → HarvestedPage (mirrors harvest.py:161-166).
        base = fr.final_url or url
        html = fr.text_html if fr.is_html() else None
        pdf_links, image_candidates, media_candidates = _capture_assets(ctx.job, html, base)
        if fr.extra_pdf_links:                      # PDFs harvested from pagination pages 2..N-1
            seen = set(pdf_links)
            pdf_links = pdf_links + [u for u in fr.extra_pdf_links if u not in seen]
        hp = HarvestedPage(url=url, depth=item.depth, fetch=fr, pdf_links=pdf_links,
                           image_candidates=image_candidates, media_candidates=media_candidates,
                           parent_url=item.parent_url)

        # build_document (CPU + possible translate) off-loop; plain data only.
        doc = await asyncio.to_thread(extract.build_document, ctx.job, hp, ctx.seed,
                                      ctx.fetcher, False, crawl_run_id=ctx.crawl_run_id)
        if doc is None:
            ctx.bump_reason("no_main_text")
            return tab

        g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
        ctx.bump_reason(g.reason)
        # Per-page keyword gate: capture ONLY pages that themselves hit >=1 corpus keyword
        # (freshness drop included in g.keep). The child links were ALREADY enqueued above,
        # so the whole site is still crawled for discovery — we just skip storing/forwarding
        # any page with no keyword match. (An empty corpus fails open: g.keep is True for all.)
        if not g.keep:
            ctx.dropped_by_gate += 1
            _tick_stat('dropped_by_gate')
            return tab
        await self._finalize(ctx, doc, hp, fr, url)
        return tab

    async def _finalize(self, ctx: "JobCtx", doc, hp, fr, url) -> None:
        """Critical path only (tab held, fast): dedup verdict + history upsert + seen-hash. The slow
        I/O tail (asset enrich + ingest POST) is handed to the finalizer pool so the worker's tab
        returns to the frontier immediately instead of blocking on PDF downloads + the ingest POST.
        Dedup/history stay HERE on the loop thread so two near-simultaneous pages can't double-emit."""
        stored = self.history.get(url)
        verdict = classify(stored, status=fr.status, content_hash=doc.content_hash)
        self.history.upsert(url, content_hash=doc.content_hash, etag=fr.etag,
                            last_modified=fr.last_modified, status=fr.status,
                            fetched_at=fr.fetched_at, js_heavy=ctx.job.render_js,
                            crawl_run_id=ctx.crawl_run_id)
        if verdict == "unchanged":
            ctx.skipped_unchanged += 1
            _tick_stat('skipped_unchanged')
            return
        if doc.content_hash in ctx.seen_hashes:
            ctx.skipped_duplicate += 1
            return
        ctx.seen_hashes.add(doc.content_hash)
        ctx.kept += 1
        _tick_stat('kept')
        # Hand the enrich+send tail to the finalizer pool (frees the tab). A full queue awaits here —
        # natural backpressure, bounded RAM. No pool (tests) → run it inline for identical behavior.
        # `url` rides along: it is the CANONICAL url, the key history is stored under, and the tail
        # needs it to undo this upsert if the document never reaches the store (see _run_finalize).
        if getattr(self, "_finalize_q", None) is not None:
            await self._finalize_q.put((ctx, doc, hp, fr, url))
        else:
            await self._run_finalize(ctx, doc, hp, fr, url)

    async def _run_finalize(self, ctx: "JobCtx", doc, hp, fr, url: str | None = None) -> None:
        """The slow tail: download assets (DEDICATED asset pool, isolated from the shared to_thread
        pool so a slow asset host can't starve page fetches), then POST to ingest. Runs on a finalizer
        coroutine (or inline when there's no pool). Counters mutate on the loop thread after each await,
        matching the engine's one-loop-one-thread model. (getattr fallback keeps __init__-skipping
        tests working.)"""
        pool = getattr(self, "_asset_pool", None)
        if pool is not None:
            await asyncio.get_running_loop().run_in_executor(
                pool, extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        else:
            await asyncio.to_thread(extract.enrich_assets, ctx.job, doc, hp, ctx.fetcher)
        # Propagate C3 captcha/solver provenance from the FetchResult to the Document.
        fr = hp.fetch
        if fr.captcha_type:
            doc.captcha_type = fr.captcha_type
            doc.captcha_solved = fr.captcha_solved
            doc.captcha_solver = fr.captcha_solver
            doc.captcha_solver_cost_usd = fr.captcha_solver_cost_usd
        if fr.proxy_country:
            doc.proxy_country = fr.proxy_country
        outcome = await asyncio.to_thread(ctx.ingest.send, doc)
        ctx.sent += 1
        _tick_stat('sent')
        if outcome.accepted:
            ctx.accepted += 1
            _tick_stat('accepted')
        else:
            ctx.rejected += 1
            # The page was fetched fine but never reached the store. _finalize has ALREADY written
            # its content_hash to history, so without this the next run reads "unchanged" and skips
            # it forever — silent, permanent, self-concealing loss. Undo that write so the page is
            # re-fetched and re-sent next run.
            #
            # Only for STORE-side failures. A validation rejection means the document is genuinely
            # unacceptable; clearing history for that would re-crawl a permanently-invalid page on
            # every run, which is a different bug wearing the same fix.
            if url and is_retryable_failure(outcome.failing_rule):
                try:
                    await asyncio.to_thread(self.history.mark_unsent, url)
                    log.warning("ingest failed for %s (%s) — cleared history so it re-sends next run",
                                url, outcome.failing_rule)
                except Exception:
                    # If we cannot undo the upsert, the document IS permanently lost. Say so loudly:
                    # this is the one place where a swallowed error costs data.
                    log.exception("LOST: %s failed ingest and history could not be cleared — this "
                                  "document will never be re-sent", url)

    async def _finalizer_loop(self) -> None:
        """Drain the finalize queue: enrich + send each kept doc. One task per CRAWLER_FINALIZERS."""
        q = self._finalize_q
        while True:
            item = await q.get()
            try:
                fctx, doc, hp, fr, furl = item
                await self._run_finalize(fctx, doc, hp, fr, furl)
            except Exception:
                log.exception("finalizer failed")
            finally:
                q.task_done()

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
                                  ignore_https_errors=_ignore_https_errors(),
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
        try:
            self._asset_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            io_pool = getattr(self, "_io_pool", None)
            if io_pool is not None:
                io_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
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

def _adaptive_host_concurrency(jobs: list[Job], total_tabs: int) -> int:
    """Per-host concurrent-tab cap, scaled to the batch so a SMALL batch still saturates
    the pool. The pool is always ``total_tabs`` tabs, but each host is capped so 96 tabs
    never hammer one site — with a fixed low cap (3), a 1-job single-domain crawl leaves
    ~93 tabs idle. So: spread the pool evenly across the batch's DISTINCT seed hosts, i.e.
    cap ≈ total_tabs / distinct_hosts, clamped to [floor, ceiling].

    Result: 1 host → cap ~= the whole pool (deep single-domain crawl fans out wide);
    32 hosts → cap back down to the polite floor. .gov/.mil careful hosts still override
    to concurrency 1 inside HostLimiter regardless of this cap — politeness there is
    non-negotiable. Overridable end-to-end with CRAWLER_HOST_CONCURRENCY (explicit set wins).
    ponytail: distinct SEED hosts, not live frontier hosts — a same_domain_only crawl stays
    on its seed host, and cross-host batches list their hosts up front, so this is exact
    for the common case and a safe over-estimate otherwise.
    """
    explicit = os.environ.get("CRAWLER_HOST_CONCURRENCY")
    if explicit:                        # operator pinned it → respect the pin
        return max(1, int(explicit))
    hosts = {
        (urlsplit(u).hostname or "").lower()
        for j in jobs for u in j.seed_urls if (urlsplit(u).hostname or "")
    }
    n = max(1, len(hosts))
    floor = _env_int("CRAWLER_HOST_CONCURRENCY_FLOOR", 3)
    ceil = _env_int("CRAWLER_HOST_CONCURRENCY_CEIL", 24)
    return max(floor, min(ceil, total_tabs // n))


def run_batch_async(jobs: list[Job], *, forward: bool,
                    seed: Seed | None = None, kp=None) -> list[dict]:
    """Run the whole batch through ONE shared browser pool. Returns per-job dicts matching
    crawler_api.app._run: {job_id, summary{...}, documents}. Runs in a dedicated thread that
    owns its event loop, so it never touches FastAPI's anyio threadpool.

    This is the LIVE pool (C1 + C3). Archive jobs never reach it — a Job carrying archive_date
    routes to crawler.c2 instead, so C2 costs no browser and cannot run alongside C1/C3."""
    seed = seed or load_seed()
    kp = kp if kp is not None else get_corpus()   # global keep-gate corpus (built once)
    clear_stop()                                   # fresh batch — clear any stale stop (event + file)
    box: dict = {}

    def _thread() -> None:
        async def _main() -> None:
            caps = seed.capture_defaults
            # Build the robots cache only if SOME job still respects robots; if every job bypasses
            # (env/flag), skip it entirely — robots_decision stays "off" and no host is ever blocked.
            robots = (RobotsCache(user_agent=caps["user_agent"])
                      if any(_job_respects_robots(j, caps) for j in jobs) else None)
            W = _env_int("CRAWLER_BROWSERS", 8)
            T = _env_int("CRAWLER_TABS_PER_BROWSER", 12)
            host_conc = _adaptive_host_concurrency(jobs, W * T)
            host = HostLimiter(host_conc,
                               _env_float("CRAWLER_HOST_DELAY", 1.0), robots,
                               base_timeout_s=float(caps.get("timeout_seconds", 30)))
            eng = AsyncEngine(W, T, host, seed, kp, robots)
            await eng.start()
            _progress_reset()                              # zero the live page counter for this run
            try:
                ctxs = await eng.run(jobs, forward)
            finally:
                _progress_done()
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


# ── multi-process sharding (a single Python process is GIL-capped to ~1 core) ─────────────────

def _shard_jobs(jobs: list[Job], n: int) -> list[list[Job]]:
    """Split jobs into n shards, keeping every seed HOST in ONE shard (two workers never crawl the
    same URLs or fight over dedup). Greedy-balance: assign the biggest host-groups to the lightest
    shard so per-worker load is even."""
    from collections import defaultdict
    by_host: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        host = ((urlsplit(j.seed_urls[0]).hostname or "") if j.seed_urls else "") or j.job_id
        by_host[host.lower()].append(j)
    shards: list[list[Job]] = [[] for _ in range(n)]
    for _host, hjobs in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        i = min(range(n), key=lambda k: len(shards[k]))
        shards[i].extend(hjobs)
    return [s for s in shards if s]


def _mp_worker(payload: dict) -> list[dict]:
    """Runs in a CHILD PROCESS (spawn): its OWN GIL, browser pool, event loop and crawl_history DB.
    Returns per-job summaries with documents dropped — they're forwarded to ingest already, and
    shipping full HTML back across the process boundary would be huge."""
    import os
    from . import config as _config
    _config.DB_PATH = _config.DATA_DIR / f"crawl_history_w{payload['worker_id']}.sqlite"
    if payload.get("browsers_per_worker"):
        os.environ["CRAWLER_BROWSERS"] = str(payload["browsers_per_worker"])
    try:
        jobs = [Job(**d) for d in payload["job_dicts"]]
        results = run_batch_async(jobs, forward=payload["forward"],
                                  seed=load_seed(), kp=get_corpus())
    except Exception as exc:
        log.error("mp worker %s failed: %s", payload.get("worker_id"), exc)
        return []
    for r in results:                       # drop documents (already forwarded) → small IPC payload
        r["documents"] = []
    return results


def run_batch_multiprocess(jobs: list[Job], *, forward: bool,
                           seed: Seed | None = None, kp=None) -> list[dict]:
    """Shard the batch across N worker PROCESSES (CRAWLER_WORKERS) so it uses ~N cores instead of the
    ~1 core a single Python process is GIL-capped to. Falls back to the single-process runner for
    1 worker / 1 job. Each worker owns its browser pool + crawl_history DB; all forward to the same
    ingest/MinIO. NOTE: worker results carry summaries only (documents are forwarded, not returned)."""
    workers = _env_int("CRAWLER_WORKERS", 1)
    if workers <= 1 or len(jobs) <= 1:
        return run_batch_async(jobs, forward=forward, seed=seed, kp=kp)
    shards = _shard_jobs(jobs, min(workers, len(jobs)))
    total_browsers = _env_int("CRAWLER_BROWSERS", 8)
    bpw = max(1, total_browsers // len(shards))     # keep total Chromium ≈ CRAWLER_BROWSERS
    payloads = [{"job_dicts": [j.model_dump() for j in shard], "forward": forward,
                 "worker_id": i, "browsers_per_worker": bpw}
                for i, shard in enumerate(shards)]
    log.info("multi-process batch: %d jobs → %d worker processes (%d browsers each)",
             len(jobs), len(shards), bpw)
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(shards)) as pool:
        shard_results = pool.map(_mp_worker, payloads)
    out: list[dict] = []
    for r in shard_results:
        out.extend(r or [])
    return out
