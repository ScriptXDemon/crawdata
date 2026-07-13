"""URL canonicalization + hashing.

Equivalent URLs must collapse to one identity so the frontier and self-dedup
(§7A) don't re-crawl the same content under cosmetically different URLs. The
canonical URL is the dedup key on every ``document``.

(Adapted from the reference crawler's canonicalize module.)
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Query params that never change page content — stripped during canonicalization.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid",
    "mc_eid", "_ga", "_gl", "ref", "ref_src", "ref_url", "igshid", "yclid",
    "spm", "scm",
}
DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str, base: str | None = None) -> str:
    """Canonical form: resolve against base, lowercase scheme/host, drop default
    port, strip tracking params, sort remaining params, drop fragment, normalize
    trailing slash, collapse a leading ``www.``."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if host.startswith("www."):
        host = host[4:]

    port = parts.port
    netloc = host
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme, ""):
        netloc = f"{host}:{port}"

    pairs = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    pairs.sort()
    query = urlencode(pairs)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def registered_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    return registered_domain(a) == registered_domain(b)
