"""Async twin of interaction.run_interactions for the async_api render pool.

Same canonical order (scroll → click → hover → search → paginate), every call awaited.
Two gov-portal fixes over the sync version:
  - search accumulates per-keyword result text (sync overwrote each keyword);
  - pagination detects DOM change, not just URL change, so ASP.NET __doPostBack /
    AJAX "next" (URL stays constant) is followed instead of stopping at page 1.

This is legitimate front-door interaction (fill a public search box, click next) — no
anti-bot evasion. It's what lets the production pool search a gov tender portal.
"""
from __future__ import annotations

import logging

from .models import InteractionConfig

logger = logging.getLogger(__name__)


def has_any(cfg: InteractionConfig | None) -> bool:
    return bool(cfg and (
        (cfg.scroll and cfg.scroll.enabled)
        or (cfg.click and cfg.click.enabled)
        or (cfg.hover and cfg.hover.enabled)
        or (cfg.search and cfg.search.enabled)
        or (cfg.paginate and cfg.paginate.enabled)
    ))


async def run_interactions(page, config: InteractionConfig) -> tuple[str, str]:
    """Run interaction steps on an async Playwright page. Returns (html, text): html for
    link/BFS extraction (final page state), text = merged innerText across search/pagination."""
    if config.scroll and config.scroll.enabled:
        await _run_scroll(page, config.scroll)
    if config.click and config.click.enabled:
        await _run_clicks(page, config.click)
    if config.hover and config.hover.enabled:
        await _run_hovers(page, config.hover)

    search_texts: list[str] = []
    if config.search and config.search.enabled:
        search_texts = await _run_search(page, config.search)

    if config.paginate and config.paginate.enabled:
        page_texts = await _run_pagination(page, config.paginate)
        html = await page.content()
        return html, "\n\n--- PAGE_BREAK ---\n\n".join(search_texts + page_texts)

    html = await page.content()
    had = any([
        config.scroll and config.scroll.enabled,
        config.click and config.click.enabled,
        config.hover and config.hover.enabled,
        config.search and config.search.enabled,
    ])
    text = await page.inner_text("body") if had else html
    if search_texts:
        text = "\n\n--- PAGE_BREAK ---\n\n".join(search_texts + [text])
    return html, text


async def _run_scroll(page, cfg) -> None:
    if cfg.mode == "infinite":
        last = await page.evaluate("document.body.scrollHeight")
        for _ in range(cfg.steps):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(cfg.pause_ms)
            new = await page.evaluate("document.body.scrollHeight")
            if new == last:
                break
            last = new
    elif cfg.mode == "bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(cfg.pause_ms)
    else:  # viewport
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        for _ in range(cfg.steps):
            await page.evaluate(f"window.scrollBy(0, {vp['height']})")
            await page.wait_for_timeout(cfg.pause_ms)
    if cfg.wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.network_idle_timeout_ms)
        except Exception:
            pass


async def _run_clicks(page, cfg) -> None:
    for sel in cfg.selectors:
        try:
            await page.click(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("click failed '%s': %s", sel, e)
    if cfg.wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass


async def _run_hovers(page, cfg) -> None:
    for sel in cfg.selectors:
        try:
            await page.hover(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("hover failed '%s': %s", sel, e)
    for sel in getattr(cfg, "click_selectors", None) or []:
        try:
            await page.click(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("hover-click failed '%s': %s", sel, e)


async def _run_search(page, cfg) -> list[str]:
    """Fill a public search box, submit (button or Enter), collect each result page's text."""
    results: list[str] = []
    for kw in cfg.keywords_to_search:
        try:
            await page.fill(cfg.input_selector, kw)
            await page.wait_for_timeout(300)
            if cfg.submit_selector:
                await page.click(cfg.submit_selector, timeout=5000)
            else:
                await page.press(cfg.input_selector, "Enter")   # no button → press Enter
            await page.wait_for_timeout(cfg.pause_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            results.append(await page.inner_text("body"))
        except Exception as e:
            logger.warning("search failed '%s': %s", kw, e)
    return results


async def _run_pagination(page, cfg) -> list[str]:
    body0 = await page.inner_text("body")
    texts: list[str] = [body0]
    prev_url, prev_len = page.url, len(body0)
    for _ in range(1, cfg.max_pages):
        try:
            await page.click(cfg.next_selector, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
            if cfg.wait_network_idle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            new_url = page.url
            body = await page.inner_text("body")
            # Stop only when BOTH url and content are unchanged — catches __doPostBack/AJAX
            # pagination where the URL never changes but the results table does.
            if new_url == prev_url and len(body) == prev_len:
                logger.info("pagination stopped: url+content unchanged")
                break
            prev_url, prev_len = new_url, len(body)
            texts.append(body)
        except Exception as e:
            logger.info("pagination ended: %s", e)
            break
    return texts
