"""Image selection — keep every non-junk image on the page (§3.1).

Drop only obvious junk: logos, avatars, icons, sprites, tracking pixels,
tiny/decorative images. This is noise removal, not content curation — L1
sends all remaining images for Layer 2 to judge relevance/importance.
Role tagging is a coarse keyword guess, kept as a cheap extra hint. Kept
images are downloaded to the object store.
"""
from __future__ import annotations

import re

from . import storage
from .fetcher import Fetcher
from .models import Image

_DROP_NAME = re.compile(
    r"(logo|icon|sprite|avatar|favicon|placeholder|banner|ad[\-_]|advert|"
    r"pixel|spacer|blank|1x1|button|share|social|footer|header[\-_]?logo)",
    re.IGNORECASE,
)
_ROLE_HINTS = [
    ("chart", re.compile(r"(chart|graph|plot|figure|infographic)", re.I)),
    ("map", re.compile(r"(map|geo|region|deployment|location)", re.I)),
    ("event", re.compile(r"(signing|ceremony|event|trial|launch|delivery|handover|parade)", re.I)),
    ("product", re.compile(r"(gun|howitzer|vehicle|missile|drone|uav|rifle|system|"
                           r"vajra|caesar|atags|tank|artillery|naval|radar)", re.I)),
]


def _role(name: str, alt: str | None) -> str:
    hay = f"{name} {alt or ''}"
    for role, rx in _ROLE_HINTS:
        if rx.search(hay):
            return role
    return "other"


def _is_meaningful(cand: dict) -> bool:
    url = cand.get("url", "")
    name = url.rsplit("/", 1)[-1]
    if _DROP_NAME.search(name):
        return False
    w, h = cand.get("width"), cand.get("height")
    # Drop obviously tiny/decorative images when dimensions are known.
    if w is not None and h is not None and (w < 200 or h < 150):
        return False
    if url.lower().endswith(".svg"):     # almost always logos/icons
        return False
    return True


def select_and_store(candidates: list[dict], fetcher: Fetcher,
                     limit: int = 30, download: bool = True,
                     referer: str | None = None, cookies: list | None = None) -> list[Image]:
    """Filter out junk (logos/icons/tracking pixels), tag role, store up to
    ``limit`` of the rest — a generous safety cap, not a curation size.

    Selection (filter + role tag) is pure; the downloads for the selected URLs
    are fetched concurrently via fetcher.fetch_assets() — same bytes, same cap,
    just parallel instead of one-at-a-time behind the throttle."""
    # 1) Pure selection: pick up to `limit` meaningful candidates, no network.
    selected: list[dict] = []
    for cand in candidates:
        if len(selected) >= limit:
            break
        if not _is_meaningful(cand):
            continue
        selected.append(cand)

    def _mk(cand: dict, storage_path) -> Image:
        name = cand["url"].rsplit("/", 1)[-1]
        return Image(
            url=cand["url"], storage_path=storage_path, caption=cand.get("alt"),
            role=_role(name, cand.get("alt")),
            width=cand.get("width"), height=cand.get("height"),
        )

    if not download:
        return [_mk(c, None) for c in selected]

    # 2) Concurrent download of exactly the selected URLs (order preserved).
    results = fetcher.fetch_assets([c["url"] for c in selected], referer=referer, cookies=cookies)
    kept: list[Image] = []
    for cand, res in zip(selected, results):
        body = getattr(res, "body_bytes", None)
        if not body:
            continue   # couldn't retrieve -> don't claim an image we don't have
        name = cand["url"].rsplit("/", 1)[-1]
        ext = (name.rsplit(".", 1)[-1] or "jpg").lower()[:4]
        kept.append(_mk(cand, storage.put(body, kind="img", ext=ext)))
    return kept
