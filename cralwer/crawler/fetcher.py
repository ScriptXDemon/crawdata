"""Harvest fetch — httpx fast-path, fixtures fallback, optional JS render (§4).

Responsibilities (Stage 1 HARVEST):
  * fetch a URL within the job's budget, honoring conditional GET (304) so an
    unchanged page on a daily sweep is skipped before download (§7A);
  * capture validators (ETag / Last-Modified) for next run;
  * detect content type (html / pdf / image / other);
  * optionally render JS via Playwright when ``render_js`` is set AND Playwright
    is installed — otherwise degrade gracefully to the httpx body.

Anti-bot ladders, proxy pools and captcha solving (present in the production
reference) are intentionally out of scope for the test build.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import config, fixtures, interaction
from .canonicalize import canonicalize_url
from .models import InteractionConfig


@dataclass
class FetchResult:
    url: str                                  # canonical
    final_url: str
    status: int | None
    content_type: str | None = None
    kind: str = "other"                       # html | pdf | image | other
    text_html: str | None = None             # decoded body when textual
    inner_text: str | None = None            # visible innerText after interactions ran
    body_bytes: bytes | None = None          # raw bytes (pdf/image)
    screenshot_png: bytes | None = None       # full-page PNG grabbed during the render (no 2nd browser)
    title: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False                # 304 — unchanged since last crawl
    published_hint: str | None = None         # from fixture meta, if any
    tier: int = 0                             # 0=httpx/fixture, 1=rendered
    fetched_at: str = ""
    error: str | None = None
    from_fixture: bool = False

    def is_html(self) -> bool:
        return self.kind == "html"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _force_playwright() -> bool:
    return os.environ.get("CRAWLER_FORCE_PLAYWRIGHT", "0") == "1"


# Realistic browser UA used only to retry a 403 once — some basic WAF rules
# key off the UA string alone. This does not bypass Cloudflare JS challenges,
# IP-based blocks, or CAPTCHAs (out of scope; see module docstring).
_BROWSER_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _with_www(url: str) -> str:
    """The `www.` variant of a canonical (www-stripped) URL — some sites' certs
    or DNS only validate one of the two hostnames."""
    p = urlsplit(url)
    if p.hostname and p.hostname.startswith("www."):
        return url
    from urllib.parse import urlunsplit
    netloc = "www." + p.netloc
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def _is_tls_or_dns_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("certificate", "ssl", "getaddrinfo", "name or service not known",
                                  "nodename nor servname"))


def _classify_kind(content_type: str | None, url: str) -> str:
    ct = (content_type or "").lower()
    low = url.lower().split("?", 1)[0]
    if "pdf" in ct or low.endswith(".pdf"):
        return "pdf"
    if ct.startswith("image/") or low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    if "html" in ct or "xml" in ct or low.endswith((".html", ".htm", "/")):
        return "html"
    if not ct and not low.rsplit("/", 1)[-1].count("."):
        return "html"           # extensionless path, no content-type -> assume page
    return "other"


class Fetcher:
    """One fetcher per crawl job. Carries the job's UA / timeout / delay."""

    def __init__(self, user_agent: str, timeout_s: int = 30, delay_s: float = 2.0,
                 max_retries: int = 2, render_js: bool = False,
                 respect_robots: bool = True,
                 prefer_fixtures: bool | None = None, allow_network: bool | None = None,
                 interaction_cfg: InteractionConfig | None = None,
                 screenshot_wanted: bool = False):
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        # Allow env var override for benchmarks: CRAWLER_CRAWL_DELAY=0 disables politeness
        self.delay_s = float(os.environ.get("CRAWLER_CRAWL_DELAY", str(delay_s)))
        # Same-page asset sub-fetches (images/PDFs on an already-fetched page)
        # get a lighter delay than page fetches — they're not new page loads on
        # the host's routes, just static files, so full politeness is overkill.
        self.asset_delay_s = float(os.environ.get("CRAWLER_ASSET_DELAY", "0.5"))
        # Grab the screenshot during the render pass instead of relaunching a
        # second browser per kept page (see _render_fetch / enrich_assets).
        self.screenshot_wanted = screenshot_wanted
        self.max_retries = max_retries
        self.render_js = render_js
        self.respect_robots = respect_robots
        self.prefer_fixtures = config.prefer_fixtures() if prefer_fixtures is None else prefer_fixtures
        self.allow_network = config.allow_network() if allow_network is None else allow_network
        self._last_fetch_ts = 0.0
        self._robots = None
        self._interaction_cfg = interaction_cfg
        self._shared_ctx = None   # (playwright_cm, browser, context, page) once opened
        if respect_robots:
            from .robots import RobotsCache
            self._robots = RobotsCache(user_agent, timeout_s=min(timeout_s, 10))

    # -- SPA click-through shared page ------------------------------------
    def open_shared_page(self) -> bool:
        """Open one Playwright browser/context/page kept alive across calls, for
        SPA click-through mode (§4). Returns False (no-op) if Playwright isn't
        installed or a shared page is already open. Caller is responsible for
        calling close_shared_page() when done."""
        if self._shared_ctx is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False
        try:
            pw_cm = sync_playwright().start()
            headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
            slow_mo = int(os.environ.get("CRAWLER_SLOW_MO", "0"))
            browser = pw_cm.chromium.launch(headless=headless, slow_mo=slow_mo, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=self.user_agent,
                                      viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            self._shared_ctx = (pw_cm, browser, ctx, page)
            return True
        except Exception:
            self._shared_ctx = None
            return False

    def close_shared_page(self) -> None:
        """Idempotent teardown of the shared browser opened by open_shared_page()."""
        if self._shared_ctx is None:
            return
        pw_cm, browser, ctx, page = self._shared_ctx
        for closer in (ctx.close, browser.close, pw_cm.stop):
            try:
                closer()
            except Exception:
                pass
        self._shared_ctx = None

    def _click_or_goto(self, canon: str, interaction_cfg: InteractionConfig | None = None) -> FetchResult | None:
        """Try to reach *canon* by clicking a matching <a href> on the shared live
        page instead of a cold navigation. Returns None (caller falls back to the
        ordinary render/httpx ladder) whenever the click path isn't viable or its
        result can't be verified — never fabricates a status."""
        if self._shared_ctx is None:
            return None
        _, _, _, page = self._shared_ctx
        rel_path = _extract_path(canon)
        try:
            link = (page.query_selector(f'a[href="{rel_path}"]')
                    or page.query_selector(f'a[href$="{rel_path}"]')
                    or page.query_selector(f'a[href="{canon}"]'))
            if not link:
                return None
            try:
                link.click(timeout=10000)
            except Exception:
                return None   # a failed click falls through to a real cold fetch,
                              # never a fabricated navigation (no pushState fallback here)
            page.wait_for_timeout(int(os.environ.get("CRAWLER_SLOW_MO", "0")) or 300)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            url_matches = urlsplit(page.url).path.rstrip("/") == rel_path.rstrip("/")
            if not url_matches:
                return None   # click didn't actually navigate to the target path

            if interaction_cfg and self._has_any_interaction(interaction_cfg):
                html, inner_text = interaction.run_interactions(page, interaction_cfg)
            else:
                html, inner_text = page.content(), None

            return FetchResult(url=canon, final_url=page.url, status=200,
                               content_type="text/html", kind="html", text_html=html,
                               inner_text=inner_text, tier=1, fetched_at=_now_iso())
        except Exception:
            return None

    # -- public ----------------------------------------------------------
    def fetch(self, url: str, conditional: dict | None = None, click_mode: bool = False) -> FetchResult:
        canon = canonicalize_url(url)

        if self.prefer_fixtures and fixtures.has(canon):
            return self._from_fixture(canon, conditional)

        if not self.allow_network:
            # Offline and no fixture -> a clean error (never a fabricated page).
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="offline_no_fixture")

        # robots.txt politeness — only for live (non-fixture) fetches.
        if self._robots is not None and not self._robots.allowed(canon):
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="blocked_by_robots")

        self._throttle()

        # When forced, skip httpx entirely — go straight to Playwright.
        # SPA sites return a 200 shell with no links to httpx, killing BFS.
        if _force_playwright():
            rendered = self._render_fetch(canon, self._interaction_cfg)
            if rendered is not None:
                return rendered

        res = self._http_fetch(canon, conditional)

        # A WAF/Cloudflare block page often comes back as 403 *with* an HTML
        # body — retry once with a realistic browser UA before giving up.
        # (Covers UA-based blocks only; see _BROWSER_UA_FALLBACK docstring.)
        if res.status == 403:
            retried = self._http_fetch(canon, conditional, user_agent=_BROWSER_UA_FALLBACK)
            if retried.status != 403:
                res = retried

        # If interaction is configured, always use Playwright for richer page content
        # (even if httpx returned valid HTML — interaction steps need a real browser).
        if self._interaction_cfg and self._has_any_interaction(self._interaction_cfg):
            rendered = self._render_fetch(canon, self._interaction_cfg)
            if rendered is not None:
                return rendered

        # A successfully-downloaded non-HTML file (PDF, image, other binary) is
        # already the content we want — never fall through to the JS renderer,
        # which would try to page.goto() it and fail on "Download is starting".
        got_binary_file = (res.status and res.status < 400 and res.body_bytes
                           and not res.is_html())
        if (res.error or not res.text_html or res.status == 403) and self.render_js \
                and not got_binary_file:
            if click_mode and self._shared_ctx is not None:
                clicked = self._click_or_goto(canon, self._interaction_cfg)
                if clicked is not None:
                    return clicked
                # fall through to the ordinary cold-render ladder below
            fallback_ua = _BROWSER_UA_FALLBACK if res.status == 403 else None
            rendered = self._render_fetch(canon, user_agent_override=fallback_ua)
            if rendered is not None:
                return rendered
        return res

    def fetch_asset(self, url: str) -> FetchResult:
        """Download a same-page asset (image / PDF) via httpx only. Uses the
        lighter asset delay and NEVER renders — assets are files, not pages, so
        the JS renderer would only try to page.goto() them and fail on
        "Download is starting". Same robots + fixture handling as fetch()."""
        self._throttle(self.asset_delay_s)
        return self._fetch_asset_core(url)

    def _fetch_asset_core(self, url: str) -> FetchResult:
        """The un-throttled body of fetch_asset — fixture/robots handling + a
        plain httpx download, no page-load throttle. Used directly by
        fetch_assets(), where the bounded worker pool is the rate limit."""
        canon = canonicalize_url(url)
        if self.prefer_fixtures and fixtures.has(canon):
            return self._from_fixture(canon, None)
        if not self.allow_network:
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="offline_no_fixture")
        if self._robots is not None and not self._robots.allowed(canon):
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="blocked_by_robots")
        return self._http_fetch(canon, None)

    def fetch_assets(self, urls: list[str]) -> list[FetchResult]:
        """Download several same-page assets (images/PDFs) concurrently, results
        in the SAME order as ``urls``. A bounded pool (CRAWLER_ASSET_WORKERS,
        default 6) IS the rate limit — at most N concurrent requests to the host
        — so no per-asset throttle is applied. Each worker uses its own httpx
        client (no shared state); robots + fixtures still honored per URL. Same
        bytes as sequential fetch_asset, just faster."""
        if not urls:
            return []
        workers = int(os.environ.get("CRAWLER_ASSET_WORKERS", "6"))
        if workers <= 1 or len(urls) == 1:
            return [self._fetch_asset_core(u) for u in urls]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
            return list(pool.map(self._fetch_asset_core, urls))

    # -- fixtures --------------------------------------------------------
    def _from_fixture(self, canon: str, conditional: dict | None) -> FetchResult:
        data, meta = fixtures.get(canon)
        etag = meta.get("etag")
        # Honor conditional GET against the fixture's validator (proves §7A 304).
        if conditional and etag and conditional.get("If-None-Match") == etag:
            return FetchResult(url=canon, final_url=canon, status=304,
                               not_modified=True, etag=etag,
                               last_modified=meta.get("last_modified"),
                               fetched_at=_now_iso(), from_fixture=True)
        ct = meta.get("content_type", "text/html")
        kind = _classify_kind(ct, canon)
        text_html = data.decode("utf-8", errors="ignore") if kind in ("html", "other") else None
        return FetchResult(
            url=canon, final_url=canon, status=200, content_type=ct, kind=kind,
            text_html=text_html, body_bytes=data if kind in ("pdf", "image") else None,
            etag=etag, last_modified=meta.get("last_modified"),
            published_hint=meta.get("published"), fetched_at=_now_iso(),
            from_fixture=True,
        )

    # -- network ---------------------------------------------------------
    def _http_fetch(self, canon: str, conditional: dict | None,
                     user_agent: str | None = None, _fetch_url: str | None = None) -> FetchResult:
        import httpx
        fetch_url = _fetch_url or canon
        headers = {"User-Agent": user_agent or self.user_agent, "Accept": "*/*"}
        if conditional:
            headers.update({k: v for k, v in conditional.items() if v})
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_s, follow_redirects=True,
                                  headers=headers) as client:
                    resp = client.get(fetch_url)
                ct = resp.headers.get("content-type")
                etag = resp.headers.get("etag")
                lm = resp.headers.get("last-modified")
                if resp.status_code == 304:
                    return FetchResult(url=canon, final_url=str(resp.url), status=304,
                                       not_modified=True, etag=etag, last_modified=lm,
                                       content_type=ct, fetched_at=_now_iso())
                kind = _classify_kind(ct, canon)
                text_html = resp.text if kind in ("html", "other") else None
                body = resp.content if kind in ("pdf", "image") else None
                return FetchResult(
                    url=canon, final_url=str(resp.url), status=resp.status_code,
                    content_type=ct, kind=kind, text_html=text_html, body_bytes=body,
                    etag=etag, last_modified=lm, fetched_at=_now_iso(),
                )
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                # Some sites' certs/DNS only validate the www. host even though
                # our dedup key strips it — retry once against www. before giving up.
                if _fetch_url is None and _is_tls_or_dns_error(exc):
                    www_url = _with_www(canon)
                    if www_url != canon:
                        return self._http_fetch(canon, conditional, user_agent, _fetch_url=www_url)
                time.sleep(0.3 * (attempt + 1))
        return FetchResult(url=canon, final_url=canon, status=None,
                           fetched_at=_now_iso(), error=last_err or "fetch_failed")

    def _render_fetch(self, canon: str, interaction_cfg: InteractionConfig | None = None,
                       user_agent_override: str | None = None) -> FetchResult | None:
        """Render with Playwright if available; else None (graceful degrade).

        Handles pure SPAs where every server-side route returns 404 — we bootstrap
        through the root page then client-side navigate to the target path.

        Playwright's sync API allows only one driver connection per thread, so
        when a shared click-through page is open (self._shared_ctx), this reuses
        its browser (a new tab) instead of starting a second sync_playwright()
        — starting a second one would raise, breaking the cold-fetch fallback
        that click-mode depends on whenever a link can't be found/clicked."""
        owns_browser = self._shared_ctx is None
        if owns_browser:
            try:
                from playwright.sync_api import sync_playwright
            except Exception:
                return None
        pw_cm = None
        page = None
        try:
            if owns_browser:
                pw_cm = sync_playwright().start()
                headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
                slow_mo = int(os.environ.get("CRAWLER_SLOW_MO", "0"))
                browser = pw_cm.chromium.launch(headless=headless, slow_mo=slow_mo, args=["--no-sandbox"])
                ctx = browser.new_context(user_agent=user_agent_override or self.user_agent,
                                          viewport={"width": 1920, "height": 1080})
                page = ctx.new_page()
            else:
                # Navigate the shared page itself (not a new tab) so its
                # current location keeps advancing — that's what lets
                # _click_or_goto find links on wherever we just landed.
                _, browser, ctx, page = self._shared_ctx

            html = None
            inner_text = None
            status = None

            # Attempt 1: direct navigation. Some sites' certs/DNS only validate
            # the www. host even though our dedup key strips it — retry once
            # against www. before giving up.
            nav_url = canon
            try:
                resp = page.goto(nav_url, timeout=self.timeout_s * 1000,
                                 wait_until="domcontentloaded")
            except Exception as exc:
                www_url = _with_www(canon)
                if not _is_tls_or_dns_error(exc) or www_url == canon:
                    raise
                # Let the failed navigation's internal error page settle before
                # retrying, or Chromium reports the retry as "interrupted by
                # another navigation."
                page.wait_for_timeout(500)
                nav_url = www_url
                resp = page.goto(nav_url, timeout=self.timeout_s * 1000,
                                 wait_until="domcontentloaded")
            try:
                # domcontentloaded already fired; this waits for XHR/JS to settle
                # but EXITS EARLY the instant the network idles. The cap only
                # bounds sites that never idle (analytics beacons / websockets) —
                # they'd otherwise burn the full 15s every page. 4s default.
                page.wait_for_load_state(
                    "networkidle",
                    timeout=int(os.environ.get("CRAWLER_NETWORKIDLE_MS", "4000")))
            except Exception:
                pass
            # Fixed post-load settle. networkidle already waited for the network;
            # this is just for late JS paint. 500ms is plenty for most sites.
            page.wait_for_timeout(int(os.environ.get("CRAWLER_RENDER_SETTLE_MS", "500")))
            html = page.content()
            status = resp.status if resp else None

            # SPA bootstrap: if we got a tiny 404 body, the JS router never ran.
            # Navigate to root, let the SPA boot, then client-side navigate to target.
            if status and status >= 400 and len(html) < 5000 and canon.rstrip("/") != _root_url(canon).rstrip("/"):
                pre_bootstrap_len = len(html)
                root = _root_url(canon)
                rel_path = _extract_path(canon)
                page.goto(root, timeout=self.timeout_s * 1000,
                          wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=int(os.environ.get("CRAWLER_NETWORKIDLE_MS", "4000")))
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                # Click the nav link matching this path to trigger SPA router
                link = page.query_selector(f'a[href="{rel_path}"]')
                if not link:
                    link = page.query_selector(f'a[href$="{rel_path}"]')
                navigated = False
                if link:
                    try:
                        link.click(timeout=10000)
                        navigated = True
                    except Exception:
                        try:
                            page.evaluate(
                                """(p) => { window.location.hash = ''; window.history.pushState({}, '', p); }""",
                                rel_path)
                            navigated = True
                        except Exception:
                            navigated = False
                    page.wait_for_timeout(3000)
                candidate_html = page.content()
                # Only trust the bootstrap result if the DOM demonstrably
                # changed (bigger body) and/or the URL now matches the
                # target path — otherwise we're still looking at the same
                # 404 shell / homepage and must not mask that as success.
                url_matches = urlsplit(page.url).path.rstrip("/") == rel_path.rstrip("/")
                content_changed = len(candidate_html) > pre_bootstrap_len * 1.5
                if navigated and (url_matches or content_changed):
                    html = candidate_html
                    status = 200  # verified: rendered content replaced the server 404
                # else: keep the original >=400 status — a broken/blank
                # SPA route must surface as an error, not a fabricated 200.

            if interaction_cfg and self._has_any_interaction(interaction_cfg):
                html, inner_text = interaction.run_interactions(page, interaction_cfg)

            # Grab the full-page screenshot NOW, on the already-rendered page,
            # instead of relaunching a second browser + re-navigating per kept
            # page (the old screenshot.capture path). enrich_assets uses this.
            shot_png = None
            if self.screenshot_wanted:
                try:
                    shot_png = page.screenshot(full_page=True)
                except Exception:
                    shot_png = None

            final_url = page.url
            if owns_browser:
                browser.close()   # tears down our own browser+context+driver
            # else: leave the shared page open — close_shared_page() (called once
            # by harvest() at the end of the job) owns its teardown.
            return FetchResult(url=canon, final_url=final_url, status=status,
                               content_type="text/html", kind="html", text_html=html,
                               inner_text=inner_text, screenshot_png=shot_png,
                               tier=1, fetched_at=_now_iso())
        except Exception as exc:  # noqa: BLE001
            if owns_browser and pw_cm is not None:
                try:
                    pw_cm.stop()
                except Exception:
                    pass
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error=f"render_failed: {exc}")

    @staticmethod
    def _has_any_interaction(cfg: InteractionConfig) -> bool:
        return bool(
            (cfg.scroll and cfg.scroll.enabled)
            or (cfg.paginate and cfg.paginate.enabled)
            or (cfg.click and cfg.click.enabled)
            or (cfg.hover and cfg.hover.enabled)
            or (cfg.search and cfg.search.enabled)
        )

    def _throttle(self, delay_s: float | None = None) -> None:
        """Respect crawl_delay_seconds between live fetches. Pass a smaller
        delay for asset sub-fetches (see fetch_asset)."""
        d = self.delay_s if delay_s is None else delay_s
        if d <= 0:
            return
        elapsed = time.monotonic() - self._last_fetch_ts
        if elapsed < d:
            time.sleep(d - elapsed)
        self._last_fetch_ts = time.monotonic()


def _root_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/", "", ""))


def _extract_path(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return p.path or "/"
