"""Crawl-error taxonomy + per-status policy — single source of truth for
'what went wrong' (reason strings) and 'what to do about it' (policy verbs).

Reason strings are the keys of the per-job ``errors_by_reason`` summary and are
persisted in CrawlHistory. HTTP failures use ``"http_<status>"`` verbatim so the
per-status breakdown is free; non-HTTP categories are the constants below.

Nothing here imports the rest of the crawler package (no circular deps) — it is
pure classification + policy tables, consumed by fetcher/async_engine/harvest.
"""
from __future__ import annotations

import os

# ── reason vocabulary (errors_by_reason keys; also stored in sqlite) ──────────
DNS = "dns"
SSL = "ssl"                         # cert/handshake — NEVER bypassed (see classify note)
CONN_REFUSED = "conn_refused"
TIMEOUT = "timeout"
RENDER_CRASH = "render_crash"
PARSE_ERROR = "parse_error"
TOO_LARGE = "too_large"
ROBOTS = "blocked_by_robots"
HOST_DOWN = "skipped_host_down"     # circuit breaker tripped this run
GONE_SKIP = "skipped_gone"          # known-404/410 from a prior run
OFF_PEAK = "skipped_off_peak"       # careful host, outside its allowed crawl window
TRAP = "trap"                       # frontier heuristic drop (not a fetch error)
# A 4xx that a full human-like Playwright session STILL couldn't clear — needs a
# different network path (residential proxy / real headless session / source API),
# not another retry. Routed here so these are queryable instead of blending into http_403.
NEEDS_NETWORK_PATH = "needs_network_path"
# Live host was WAF-blocked but we served the content from the Wayback Machine's
# archived copy instead — a SUCCESS via a WAF-free path, tagged so the dashboard
# shows "routed around the block" distinctly from a raw fetch.
SERVED_FROM_ARCHIVE = "served_from_archive"
OTHER = "other"


def http_reason(status: int) -> str:
    return f"http_{status}"


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


# ── policy verbs ──────────────────────────────────────────────────────────────
DROP = "drop"              # count, move on
GONE = "gone"              # persist as permanently gone (skip on future runs)
DISGUISE = "disguise"      # one retry with a browser UA (403 only — UA spoof is the ceiling)
RETRY_LATER = "retry_later"  # transient: one in-run requeue + next-run eligible
COOLDOWN = "cooldown"      # RETRY_LATER + put the whole host on cooldown (429/503)

_STATUS_POLICY = {
    400: DROP, 401: DROP, 403: DISGUISE, 404: GONE, 410: GONE,
    429: COOLDOWN, 500: RETRY_LATER, 502: RETRY_LATER, 503: COOLDOWN, 504: RETRY_LATER,
}


def policy_for_status(status: int) -> str:
    if status in _STATUS_POLICY:
        return _STATUS_POLICY[status]
    return RETRY_LATER if status >= 500 else DROP


# categories that feed the per-host circuit breaker (a dead HOST, not a bad page)
HARD_FAIL = {DNS, CONN_REFUSED, SSL}
# categories eligible for an in-run requeue (transient — the page itself may recover)
TRANSIENT = {TIMEOUT, RENDER_CRASH}


# ── exception / Chromium-net:: message → reason ──────────────────────────────
# Playwright exposes only error *messages* (net::ERR_*); httpx exposes exception
# text. Both are substring-matched here, first pattern wins. SSL is classified
# but the response is NEVER verify=False / ignore_https_errors — a broken cert is
# a real trust failure; we skip the page, we do not bypass it.
# ponytail: substring taxonomy; add a needle when a new failure string appears in logs.
_PATTERNS = [
    (DNS, ("getaddrinfo", "name or service not known", "nodename nor servname",
           "err_name_not_resolved", "temporary failure in name resolution")),
    (SSL, ("certificate", "ssl", "err_cert", "err_ssl", "handshake", "tlsv1", "sslv3")),
    (CONN_REFUSED, ("connection refused", "err_connection_refused", "err_connection_closed",
                    "connection reset", "err_connection_reset", "err_empty_response")),
    (TIMEOUT, ("timed out", "timeout", "err_timed_out", "err_connection_timed_out",
               "deadline exceeded")),
]


def classify_failure(status: int | None, error_text: str | None) -> str | None:
    """Reason for a FAILED fetch; None when the fetch succeeded (no status>=400, no error)."""
    if status is not None and status >= 400:
        return http_reason(status)
    if not error_text:
        return None
    low = error_text.lower()
    if low == ROBOTS:
        return ROBOTS
    if low == "offline_no_fixture":
        return OTHER
    for reason, needles in _PATTERNS:
        if any(n in low for n in needles):
            return reason
    if low.startswith(("render", "nav:", "content:")) or "render_failed" in low:
        return RENDER_CRASH
    return OTHER


# ── Retry-After parsing (429 / 503) ──────────────────────────────────────────
def parse_retry_after(value: str | None, cap_s: float = 300.0) -> float | None:
    """Retry-After header → seconds (delta-seconds int form OR HTTP-date). None if
    absent/unparseable. Clamped to cap_s so a hostile '31536000' can't freeze a host."""
    if not value:
        return None
    value = value.strip()
    try:
        return min(float(int(value)), cap_s)
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(secs, 0.0), cap_s)
    except Exception:
        return None


# ── careful hosts (.gov / .mil — "quiet quiet") ──────────────────────────────
def _careful_tlds() -> set[str]:
    raw = os.environ.get("CRAWLER_CAREFUL_HOSTS", ".gov,.mil")
    return {s.strip().lstrip(".").lower() for s in raw.split(",") if s.strip()}


def is_careful_host(host: str | None) -> bool:
    """Segment-exact suffix match: epa.gov ✓, ministry.gov.in ✓, defence.mod.gov.uk ✓,
    but govtrack.us ✗ and email.com ✗ (substring, not a domain segment)."""
    if not host:
        return False
    segments = host.lower().rstrip(".").split(".")
    tlds = _careful_tlds()
    return any(seg in tlds for seg in segments)


# ── off-peak crawl window for careful hosts ──────────────────────────────────
def _parse_hours(spec: str) -> list[tuple[int, int]] | None:
    """'0-6,20-23' → [(0,6),(20,23)]. 'off'/'' → None (always allowed)."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "off":
        return None
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            ranges.append((int(a) % 24, int(b) % 24))
        except ValueError:
            continue
    return ranges or None


def in_allowed_window(hour: int, spec: str) -> bool:
    """Is `hour` (0-23) inside the allowed spec? Ranges may wrap midnight (20-6)."""
    ranges = _parse_hours(spec)
    if ranges is None:
        return True
    for a, b in ranges:
        if a <= b:
            if a <= hour <= b:
                return True
        elif hour >= a or hour <= b:      # wraps midnight
            return True
    return False


def careful_off_peak_now(host: str | None) -> bool:
    """True when `host` is careful AND the current LOCAL hour is OUTSIDE
    CRAWLER_CAREFUL_HOURS — the URL should be skipped now and retried next run
    (polite: don't hammer a gov server during its business/peak hours)."""
    if not is_careful_host(host):
        return False
    spec = os.environ.get("CRAWLER_CAREFUL_HOURS", "off")
    from datetime import datetime
    return not in_allowed_window(datetime.now().hour, spec)


# ── URL trap heuristics (stateless, cheap) ───────────────────────────────────
def looks_like_trap(canon_url: str) -> str | None:
    """Cheap stateless URL-shape trap checks → TRAP or None. The STATEFUL
    query-explosion cap (calendar/facet traps) lives at the enqueue site — it
    needs per-job (host,path)->count state that doesn't belong in a pure function."""
    from urllib.parse import urlsplit

    if len(canon_url) > _env_int("CRAWLER_MAX_URL_LEN", 2000):
        return TRAP
    segs = [s for s in urlsplit(canon_url).path.split("/") if s]
    if len(segs) > 12:
        return TRAP
    counts: dict[str, int] = {}
    for s in segs:                       # /a/b/a/b/a/b … loop trap
        counts[s] = counts.get(s, 0) + 1
        if counts[s] >= 3:
            return TRAP
    return None
