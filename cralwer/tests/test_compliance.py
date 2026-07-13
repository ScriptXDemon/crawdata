"""Phase-1 compliance tests: off-peak window, fail-closed robots, identifying UA,
force-careful rate, patient timeout, audit trail. Offline."""
import asyncio

import httpx
import pytest

from crawler import errors
from crawler.async_engine import HostLimiter
from crawler.dedup import CrawlHistory
from crawler.robots import RobotsCache


# ── off-peak window ──────────────────────────────────────────────────────────
def test_in_allowed_window():
    assert errors.in_allowed_window(3, "0-6,20-23")
    assert errors.in_allowed_window(22, "0-6,20-23")
    assert not errors.in_allowed_window(12, "0-6,20-23")
    assert errors.in_allowed_window(23, "20-6")        # wraps midnight
    assert errors.in_allowed_window(2, "20-6")
    assert not errors.in_allowed_window(10, "20-6")
    assert errors.in_allowed_window(12, "off")         # off → always allowed
    assert errors.in_allowed_window(12, "")


def test_careful_off_peak_now(monkeypatch):
    monkeypatch.setenv("CRAWLER_CAREFUL_HOURS", "0-0")   # only hour 0 allowed → almost always off
    # non-careful host never gated
    assert not errors.careful_off_peak_now("navalnews.com")
    monkeypatch.setenv("CRAWLER_CAREFUL_HOURS", "off")
    assert not errors.careful_off_peak_now("x.gov")      # off → never skipped


# ── fail-closed robots for careful hosts ─────────────────────────────────────
def _mk_resp(status, text=""):
    return httpx.Response(status_code=status, text=text, request=httpx.Request("GET", "http://x/robots.txt"))


def test_robots_fetch_error_denies_careful(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("unreachable")
    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setenv("CRAWLER_ROBOTS_STRICT_CAREFUL", "1")
    rc = RobotsCache("advanceBot/1.0")
    assert rc.allowed("https://ddpmod.gov.in/tenders") is False    # gov + unknown robots → deny
    assert rc.decision("https://ddpmod.gov.in/tenders") == "deny"


def test_robots_fetch_error_allows_non_careful(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
    rc = RobotsCache("advanceBot/1.0")
    assert rc.allowed("https://navalnews.com/x") is True           # non-gov error → fail open


def test_robots_404_allows_careful(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _mk_resp(404))
    rc = RobotsCache("advanceBot/1.0")
    assert rc.allowed("https://x.gov/page") is True                # 4xx = no robots = allowed
    assert rc.decision("https://x.gov/page") == "no_robots"


def test_robots_strict_off_allows_careful(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
    monkeypatch.setenv("CRAWLER_ROBOTS_STRICT_CAREFUL", "0")
    rc = RobotsCache("advanceBot/1.0")
    assert rc.allowed("https://x.gov/page") is True                # strict off → fail open even on gov


# ── identifying UA (no branding) ─────────────────────────────────────────────
def test_ua_is_honest_and_unbranded():
    from crawler.seed import load_seed
    ua = load_seed().capture_defaults["user_agent"]
    assert "advanceBot" in ua
    low = ua.lower()
    assert "kssl" not in low and "mallory" not in low


# ── force-careful rate + patient timeout (HostLimiter) ───────────────────────
def test_force_careful_and_patient_timeout():
    async def go():
        hl = HostLimiter(max_conc=3, min_delay=1.0, robots=None, base_timeout_s=30.0)
        hl.force_careful.add("portal.example.com")
        await hl._ensure("portal.example.com")
        assert hl._sem["portal.example.com"]._value == 1           # concurrency 1
        assert hl._delay["portal.example.com"] >= 5.0              # slow delay floor
        assert hl.timeout_ms("portal.example.com") == 30.0 * 2.0 * 1000  # careful timeout factor

        await hl._ensure("news.example.org")
        assert hl._sem["news.example.org"]._value == 3            # normal concurrency
        assert hl.timeout_ms("news.example.org") == 30000
        hl.bump_timeout("news.example.org")                        # ratchet on timeout
        assert hl.timeout_ms("news.example.org") == 30 * 1.5 * 1000
    asyncio.run(go())


def test_bump_timeout_capped(monkeypatch):
    async def go():
        monkeypatch.setenv("CRAWLER_MAX_TIMEOUT_S", "40")
        hl = HostLimiter(3, 1.0, None, base_timeout_s=30.0)
        await hl._ensure("slow.gov")            # careful → 60s seed... capped to 40 on next bump
        for _ in range(5):
            hl.bump_timeout("slow.gov")
        assert hl.timeout_ms("slow.gov") == 40 * 1000             # never exceeds the cap
    asyncio.run(go())


# ── audit trail ──────────────────────────────────────────────────────────────
def test_record_audit(tmp_path):
    h = CrawlHistory(tmp_path / "h.sqlite")
    h.record_audit(url="https://x.gov/a", host="x.gov", fetched_at="2026-07-09T00:00:00Z",
                   ua="advanceBot/1.0", robots_decision="allow", status=200, reason=None, careful=True)
    h.record_audit(url="https://x.gov/b", host="x.gov", fetched_at="2026-07-09T00:00:05Z",
                   ua="advanceBot/1.0", robots_decision="deny", status=None,
                   reason="blocked_by_robots", careful=True)
    rows = h._conn.execute(
        "SELECT url, host, ua, robots_decision, status, reason, careful FROM crawl_audit ORDER BY id"
    ).fetchall()
    assert len(rows) == 2                        # append-only (both visits kept)
    assert rows[0]["robots_decision"] == "allow" and rows[0]["careful"] == 1
    assert rows[1]["robots_decision"] == "deny" and rows[1]["reason"] == "blocked_by_robots"
    h.close()
