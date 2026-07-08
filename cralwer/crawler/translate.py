"""Translation hook for ``main_text_en`` (non-English handling, exit criterion 7).

Production plugs a real machine-translation provider here (the ``_provider``
hook). For the offline test build we additionally consult a fixtures translation
map (``tests/fixtures/translations.json``, keyed by content_hash) so a non-
English source can return both ``main_text`` and a real ``main_text_en`` without
a network MT call. If neither yields a translation, we return None — we never
fabricate a translation.
"""
from __future__ import annotations

import functools
import json
from typing import Callable, Optional

from . import config

# Optional production hook: set translate.set_provider(fn) where
# fn(text, src_lang) -> english str. Left unset by default.
_provider: Optional[Callable[[str, str], Optional[str]]] = None


def set_provider(fn: Callable[[str, str], Optional[str]]) -> None:
    global _provider
    _provider = fn


@functools.lru_cache(maxsize=1)
def _fixture_translations() -> dict:
    path = config.FIXTURES_DIR / "translations.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    # Canonicalize URL-shaped keys so lookups match the fetcher's canonical url;
    # content_hash keys (sha256:...) are left as-is.
    from .canonicalize import canonicalize_url
    out = {}
    for k, v in raw.items():
        out[canonicalize_url(k) if k.startswith("http") else k] = v
    return out


def to_english(text: str, src_lang: str, content_hash: str | None = None,
               url: str | None = None) -> str | None:
    """Translate main_text to English, or None if no translation is available.

    Fixture lookups are keyed by canonical URL (robust to text edits) and fall
    back to content_hash. Production sets a real ``_provider`` instead."""
    if not text or src_lang == "en":
        return None
    if _provider is not None:
        try:
            out = _provider(text, src_lang)
            if out and out.strip():
                return out.strip()
        except Exception:
            pass
    fx = _fixture_translations()
    if url:
        from .canonicalize import canonicalize_url
        hit = fx.get(canonicalize_url(url))
        if hit:
            return hit
    if content_hash and fx.get(content_hash):
        return fx[content_hash]
    return None
