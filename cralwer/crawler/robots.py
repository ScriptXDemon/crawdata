"""robots.txt politeness (honors source_registry → respect_robots_txt).

A small per-host cache of parsed robots rules. Live fetches consult it before
hitting a URL; a disallowed URL is skipped (never fetched). Fixtures bypass this
entirely — they are our own offline test content, not live sites.

Fail-open on robots fetch errors: if robots.txt can't be retrieved we allow the
fetch (matching common crawler behavior) rather than silently dropping coverage.
"""
from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser


class RobotsCache:
    def __init__(self, user_agent: str, timeout_s: int = 10):
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self._cache: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return None
        if host in self._cache:
            return self._cache[host]
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp: RobotFileParser | None = RobotFileParser()
        rp.set_url(robots_url)
        try:
            import httpx
            resp = httpx.get(robots_url, timeout=self.timeout_s,
                            headers={"User-Agent": self.user_agent},
                            follow_redirects=True)
            if resp.status_code >= 400 or not resp.text:
                rp = None                      # no robots / error -> fail open
            else:
                rp.parse(resp.text.splitlines())
        except Exception:
            rp = None                          # unreachable -> fail open
        self._cache[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._parser_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float | None:
        rp = self._parser_for(url)
        if rp is None:
            return None
        try:
            d = rp.crawl_delay(self.user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None
