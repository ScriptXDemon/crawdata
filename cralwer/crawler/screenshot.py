"""Full-page screenshot — audit evidence, one per kept document (§4).

Screenshots are evidence, not decoration: defence sources frequently edit or
pull stories, so a timestamped capture is the audit trail behind every signal.

Two backends, tried in order:
  1. Playwright full-page PNG of the live URL (production path), if installed.
  2. A Pillow-rendered "evidence card" PNG (title + timestamp + text excerpt)
     when Playwright isn't available — so capture still works offline/in tests
     and a real stored PNG path is always returned. The card explicitly states
     it is a text-rendered fallback so it is never mistaken for a pixel-perfect
     page capture.
"""
from __future__ import annotations

from datetime import datetime, timezone


def capture(url: str, html: str | None, title: str, main_text: str,
            render_js: bool = False) -> bytes:
    png = _playwright_capture(url) if _playwright_available() else None
    if png:
        return png
    return _fallback_card(url, title, main_text)


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def _playwright_capture(url: str) -> bytes | None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            png = page.screenshot(full_page=True)
            browser.close()
            return png
    except Exception:
        return None


def _fallback_card(url: str, title: str, main_text: str) -> bytes:
    """Render a text 'evidence card' PNG (audit fallback when no browser)."""
    import io
    import textwrap

    from PIL import Image, ImageDraw

    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(img)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    d.rectangle([0, 0, W, 60], fill=(25, 35, 60))
    d.text((20, 20), "MalloryBot audit capture (text-rendered fallback)", fill=(255, 255, 255))
    d.text((20, 80), f"captured_at: {stamp}", fill=(80, 80, 80))
    d.text((20, 105), f"url: {url[:120]}", fill=(40, 60, 120))

    y = 145
    for line in textwrap.wrap(title or "(untitled)", width=95)[:3]:
        d.text((20, y), line, fill=(0, 0, 0))
        y += 22
    y += 10
    for line in textwrap.wrap(main_text or "", width=110)[:22]:
        d.text((20, y), line, fill=(50, 50, 50))
        y += 18

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
