"""Translation for ``main_text_en`` (non-English handling, exit criterion 7).

The offline build consults a fixtures translation map
(``tests/fixtures/translations.json``, keyed by canonical URL / content_hash) so a
non-English source returns a real ``main_text_en`` without a network MT call.
If no translation is found, we return None — we never fabricate one. (Real
machine translation is Layer 2's job; the crawler only carries source text.)
"""
from __future__ import annotations

import functools
import json

from . import config


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
    back to content_hash."""
    if not text or src_lang == "en":
        return None
    fx = _fixture_translations()
    if url:
        from .canonicalize import canonicalize_url
        hit = fx.get(canonicalize_url(url))
        if hit:
            return hit
    if content_hash and fx.get(content_hash):
        return fx[content_hash]
    return None
