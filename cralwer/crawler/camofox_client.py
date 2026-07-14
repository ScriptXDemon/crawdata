"""C3 — CamoFox stealth-render client (§ Tri-Crawler).

jo-inc/camofox-browser is a standalone anti-detection browser SERVER (a Firefox
fork with C++-engine-level fingerprint spoofing) exposing a REST API on :9377 —
NOT a Playwright target. We call it on demand as the WAF-block fallback (C3):
open a tab, read the accessibility snapshot (lightweight, LLM-ready text), close
the tab. The snapshot — not bloated HTML — IS the content, per the plan.

Disabled unless CAMOFOX_ENABLED=1, so the fallback ladder cleanly skips C3 when
no CamoFox server is deployed. Server URL via CAMOFOX_URL (default the compose
service http://camofox:9377). Every call is best-effort — returns None on any
error so the caller falls through to the archival ladder.

ponytail: thin httpx REST wrapper (~1 open + 1 snapshot + 1 close); no CLI
subprocess, no Playwright, no camofox SDK.
"""
from __future__ import annotations

import itertools
import logging
import os
import threading

log = logging.getLogger("camofox")

# camofox-browser creates a fresh browser CONTEXT per userId. A unique userId PER
# URL is the strongest anti-bot posture, BUT the server's session caps REJECT (503)
# rather than evict, so thousands of per-URL contexts pile up and OOM the 128MB→2GB
# Node heap. Instead ROTATE a small bounded pool of userIds (round-robin): still
# isolated, fresh-ish contexts, but ≤N live sessions instead of hundreds.
# ponytail: 4 rotating contexts is enough isolation for a reactive fallback; per-URL
# uniqueness was over-isolation that caused the OOM crash.
_USER_PREFIX = "crawler-"
_SESSION = "s"


def _pool_size() -> int:
    try:
        return max(1, int(os.environ.get("CRAWLER_CAMOFOX_POOL", "4")))
    except ValueError:
        return 4


# Round-robin userId picker, thread-safe (render/fetch_bytes run under asyncio.to_thread
# on the pool). Rebuilt if the env pool size changes between calls (cheap).
_pool_lock = threading.Lock()
_pool_cycle = itertools.cycle(f"{_USER_PREFIX}{i}" for i in range(_pool_size()))
_pool_n = _pool_size()


def _next_user() -> str:
    global _pool_cycle, _pool_n
    with _pool_lock:
        n = _pool_size()
        if n != _pool_n:                        # pool size changed → rebuild the cycle
            _pool_cycle = itertools.cycle(f"{_USER_PREFIX}{i}" for i in range(n))
            _pool_n = n
        return next(_pool_cycle)


def enabled() -> bool:
    return os.environ.get("CAMOFOX_ENABLED", "0") == "1"


def _base() -> str:
    return os.environ.get("CAMOFOX_URL", "http://camofox:9377").rstrip("/")


def base_url() -> str:
    return _base()


def _headers() -> dict:
    """Bearer header for the auth-gated /evaluate endpoint (raw-HTML grab).
    Empty when no key is set — /evaluate then just fails and we fall back to the
    aria snapshot for text."""
    key = os.environ.get("CAMOFOX_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def health() -> bool:
    """True if the CamoFox server answers /health (only meaningful when enabled)."""
    if not enabled():
        return False
    import httpx
    try:
        return httpx.get(f"{_base()}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def _flatten(node, out: list[str]) -> None:
    """Collect human-readable strings from a CamoFox accessibility snapshot — a
    JSON tree of {role, name, children} (shape may drift, so we pull the common
    text fields and recurse into anything nested)."""
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
    elif isinstance(node, dict):
        for k in ("name", "text", "value", "label"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for k, v in node.items():
            if k in ("name", "text", "value", "label"):
                continue
            if isinstance(v, (dict, list)):
                _flatten(v, out)
    elif isinstance(node, list):
        for it in node:
            _flatten(it, out)


def _snapshot_text(snapshot) -> str:
    """A11y snapshot -> plain text (one line per node), collapsing adjacent
    duplicate lines (nav items repeat across the tree)."""
    if isinstance(snapshot, str):
        return snapshot.strip()
    out: list[str] = []
    _flatten(snapshot, out)
    lines: list[str] = []
    for s in out:
        if not lines or lines[-1] != s:
            lines.append(s)
    return "\n".join(lines)


def render(url: str, timeout_s: float = 45.0) -> dict | None:
    """Open URL in CamoFox and capture EVERYTHING the page yields, so a C3 doc is
    as rich as a C1 one. Returns {text, html, screenshot, snapshot, final_url} or
    None. Steps (one fresh tab/context):
      - snapshot   -> aria text (LLM-ready fallback body)
      - evaluate   -> document.documentElement.outerHTML = the REAL rendered HTML
                      (needs CAMOFOX_API_KEY Bearer; enables downstream image/pdf/
                      media/video extraction the same as a live fetch)
      - screenshot -> full-page PNG bytes
    The tab is always torn down. Synchronous (httpx) — call via asyncio.to_thread."""
    if not enabled():
        return None
    import httpx

    base = _base()
    user = _next_user()             # rotating pool userId (bounded live sessions; see _next_user)
    tab_id = None
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
            # POST /tabs requires userId + sessionKey; it navigates and returns {tabId, url}.
            r = c.post(f"{base}/tabs",
                       json={"userId": user, "sessionKey": _SESSION, "url": url})
            if r.status_code >= 400 or not r.content:
                return None
            data = r.json()
            tab_id = data.get("tabId") or data.get("id") or data.get("targetId")
            if not tab_id:
                return None
            # GET snapshot requires ?userId=; the aria snapshot text is in .snapshot.
            s = c.get(f"{base}/tabs/{tab_id}/snapshot", params={"userId": user})
            payload = {}
            if s.status_code < 400 and s.content:
                ctype = s.headers.get("content-type", "")
                payload = s.json() if ctype.startswith("application/json") else {"snapshot": s.text}

            # Raw rendered HTML (best-effort; needs the Bearer key). This is what lets
            # enrich_assets pull images / PDFs / media / video links downstream.
            html = ""
            try:
                e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                           json={"userId": user, "expression": "document.documentElement.outerHTML"},
                           headers=_headers())
                if e.status_code < 400:
                    res = e.json().get("result")
                    if isinstance(res, str):
                        html = res
            except Exception:
                pass

            # Full-page screenshot (PNG bytes; no auth).
            shot = None
            try:
                sc = c.get(f"{base}/tabs/{tab_id}/screenshot",
                           params={"userId": user, "fullPage": "true"})
                if sc.status_code < 400 and sc.content:
                    shot = sc.content
            except Exception:
                pass

        raw = payload.get("snapshot") if isinstance(payload, dict) else payload
        text = _snapshot_text(raw)
        if not text.strip() and not html.strip():
            return None                 # nothing usable came back
        page_url = payload.get("url") if isinstance(payload, dict) else None
        return {"text": text, "html": html, "screenshot": shot,
                "snapshot": raw, "final_url": page_url or url}
    except Exception:
        log.info("camofox render failed url=%s", url, exc_info=True)
        return None
    finally:
        if tab_id:
            try:                        # tear down the tab (frees the page immediately)
                with httpx.Client(timeout=5.0) as c:
                    c.delete(f"{base}/tabs/{tab_id}", params={"userId": user})
            except Exception:
                pass


def fetch_bytes(url: str, timeout_s: float = 45.0) -> bytes | None:
    """Fetch a WAF-blocked asset (image/PDF) through the stealth browser and return its
    RAW BYTES — the last-resort path when even curl_cffi's Chrome fingerprint is 403'd
    (Akamai). Opens the asset URL as a tab, then evaluates an in-page fetch() that reads
    the response as base64 (an arrayBuffer collapses to {} through Playwright's JSON
    serialization; a base64 string survives). Needs CAMOFOX_API_KEY (the evaluate route is
    Bearer-gated). Returns None on any error / when disabled. Synchronous — call via
    asyncio.to_thread. ponytail: base64 over the wire is ~33% bloat but it's the only
    serialization the evaluate endpoint supports; fine for the rare blocked asset."""
    if not enabled():
        return None
    import base64
    import httpx

    base = _base()
    user = _next_user()
    tab_id = None
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
            r = c.post(f"{base}/tabs",
                       json={"userId": user, "sessionKey": _SESSION, "url": url})
            if r.status_code >= 400 or not r.content:
                return None
            tab_id = (r.json() or {}).get("tabId") or (r.json() or {}).get("id")
            if not tab_id:
                return None
            # In-page fetch reads the asset from the SAME browser session that just loaded it
            # (same cookies/fingerprint that passed the WAF), then hands back base64.
            expr = (
                "(async()=>{const r=await fetch(" + _js_str(url) + ",{credentials:'include'});"
                "if(!r.ok)return null;const b=await r.arrayBuffer();"
                "let s='',a=new Uint8Array(b);for(let i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);"
                "return btoa(s);})()"
            )
            e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                       json={"userId": user, "expression": expr}, headers=_headers())
            if e.status_code >= 400:
                return None
            res = (e.json() or {}).get("result")
            if not isinstance(res, str) or not res:
                return None
            return base64.b64decode(res)
    except Exception:
        log.info("camofox fetch_bytes failed url=%s", url, exc_info=True)
        return None
    finally:
        if tab_id:
            try:
                with httpx.Client(timeout=5.0) as c:
                    c.delete(f"{base}/tabs/{tab_id}", params={"userId": user})
            except Exception:
                pass


def _js_str(s: str) -> str:
    """JSON-encode a Python string into a safe JS string literal (handles quotes/backslashes
    in the URL — a query string with an apostrophe would otherwise break the expression)."""
    import json
    return json.dumps(s)


if __name__ == "__main__":   # python -m crawler.camofox_client  (offline self-check)
    # Snapshot flattening is the only non-trivial logic; verify it without a server.
    tree = {"role": "document", "name": "K9 Vajra page",
            "children": [{"role": "heading", "name": "K9 Vajra-T delivered"},
                         {"role": "text", "text": "L&T handed over the howitzer."},
                         {"role": "nav", "children": [{"name": "Home"}, {"name": "Home"}]}]}
    txt = _snapshot_text(tree)
    assert "K9 Vajra-T delivered" in txt and "L&T handed over the howitzer." in txt
    assert txt.count("Home") == 1, f"adjacent dupes not collapsed: {txt!r}"
    assert _snapshot_text("raw yaml-ish text") == "raw yaml-ish text"

    # userId pool rotates round-robin within a bounded set (bounds live sessions → no OOM).
    os.environ["CRAWLER_CAMOFOX_POOL"] = "4"
    users = [_next_user() for _ in range(9)]
    assert set(users) == {"crawler-0", "crawler-1", "crawler-2", "crawler-3"}, users
    assert users[0] != users[1], "should rotate, not repeat"

    # _js_str produces a valid JS/JSON string literal even with quotes in the URL.
    assert _js_str("http://x/a'b\"c") == '"http://x/a\'b\\"c"', _js_str("http://x/a'b\"c")
    print("OK — camofox snapshot flattener + userId pool self-check passed")
