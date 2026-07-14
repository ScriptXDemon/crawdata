"""Contract data models — the JOB INPUT (§2) and the DOCUMENT (§3.2).

L1 sends one raw page bundle (the ``Document``) per kept page, carrying
detection tags (stream/competitor/countries/tech domains) as flat
informational fields. It does not construct separately-typed records —
deep record classification (tender/partnership/geo_footprint/innovation/
company_event/competitive_signal) is Layer 2's job, operating on the raw
text + tags handed over here.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# --- interaction config (§2.1) -------------------------------------------

class ScrollConfig(BaseModel):
    enabled: bool = False
    mode: str = "viewport"           # "viewport" | "bottom" | "infinite"
    pause_ms: int = 800
    steps: int = 5
    wait_network_idle: bool = True
    network_idle_timeout_ms: int = 5000


class PaginateConfig(BaseModel):
    enabled: bool = False
    next_selector: str = ""          # CSS selector, e.g. "a.next", "[rel=next]"
    max_pages: int = 10
    pause_ms: int = 1000
    wait_network_idle: bool = True


class ClickConfig(BaseModel):
    enabled: bool = False
    selectors: list[str] = Field(default_factory=list)
    pause_ms: int = 500
    wait_network_idle: bool = False


class HoverConfig(BaseModel):
    enabled: bool = False
    selectors: list[str] = Field(default_factory=list)
    pause_ms: int = 300
    click_selectors: list[str] = Field(default_factory=list)


class SearchConfig(BaseModel):
    enabled: bool = False
    input_selector: str = ""
    submit_selector: str = ""
    keywords_to_search: list[str] = Field(default_factory=list)
    pause_ms: int = 1000


class InteractionConfig(BaseModel):
    scroll: ScrollConfig | None = None
    paginate: PaginateConfig | None = None
    click: ClickConfig | None = None
    hover: HoverConfig | None = None
    search: SearchConfig | None = None


# --- enums (kept as Literals so the Ingest API can validate) -------------
JobType = Literal["news", "tender", "profile", "spec", "patent_aux"]
CaptureType = Literal["html", "text", "images", "screenshot", "pdf", "media", "js"]


# --- JOB INPUT (§2) ------------------------------------------------------
class Job(BaseModel):
    job_id: str
    job_type: JobType
    seed_urls: list[str]
    keywords: list[str] = Field(default_factory=list)
    target_entity: Optional[str] = None          # watchlist id, or null for tenders
    max_pages: int = 40
    max_depth: int = 2
    same_domain_only: bool = True
    render_js: bool = False
    # SPA click-through (§4): for same-site links, click the discovered <a href>
    # in-app on a shared live page instead of a cold page.goto(), for sites whose
    # server only serves working content via client-side routing. Requires render_js.
    spa_click_through: bool = False
    freshness_days: Optional[int] = None          # ignore content older than this
    capture: list[CaptureType] = Field(default_factory=lambda: ["html", "text", "screenshot"])
    # Opt-in link-text relevance (§harvest): when non-empty, only same-site
    # links whose anchor text overlaps one of these keywords get enqueued.
    # Empty (default) = crawl every same-site link, same as today.
    link_relevance_keywords: list[str] = Field(default_factory=list)
    # Opt-in seed-relevance pruning: when True, a seed (depth-0) page whose
    # title+visible text contains ZERO job keywords does not get its links
    # enqueued — saves the crawl budget on dead seeds. The page itself still
    # flows to the gate (which will drop it); this only stops BFS expansion.
    skip_irrelevant_seed_links: bool = False

    # Optional source identity stamped by the orchestrator's Source Catalog.
    # When present these are used VERBATIM (the crawler does not re-derive them).
    source_id: Optional[str] = None
    source_tier: Optional[int] = None
    source_type: Optional[str] = None
    source_region: Optional[str] = None

    # Hunt mode presets — fill only knobs left at default (see _apply_hunt_mode):
    #   "exhaustive" = deep dig (750 pages, depth 5) for meaty defense-caves;
    #   "focused"    = probe-then-crawl (small budget, shallow, relevance-pruned).
    hunt_mode: Optional[Literal["exhaustive", "focused"]] = None
    # Careful mode ("quiet quiet"): force per-host concurrency 1 + slow delay + no UA disguise
    # even for non-.gov hosts. None = auto by hostname suffix (CRAWLER_CAREFUL_HOSTS).
    careful: Optional[bool] = None

    # Page interaction steps for JS rendering (§2.1).
    interaction: InteractionConfig | None = None

    @model_validator(mode="after")
    def _apply_hunt_mode(self) -> "Job":
        """Expand hunt_mode into concrete knobs, but NEVER override a value the caller set
        explicitly (model_fields_set) — presets are defaults, not mandates."""
        if self.hunt_mode is None:
            return self
        s = self.model_fields_set
        if self.hunt_mode == "exhaustive":
            if "max_pages" not in s:
                self.max_pages = 750
            if "max_depth" not in s:
                self.max_depth = 5
        else:  # "focused" — probe-then-crawl: small, shallow, relevance-pruned
            if "max_pages" not in s:
                self.max_pages = 60
            if "max_depth" not in s:
                self.max_depth = 2
            if "skip_irrelevant_seed_links" not in s:
                self.skip_irrelevant_seed_links = True
            if "link_relevance_keywords" not in s and self.keywords:
                self.link_relevance_keywords = list(self.keywords)
        return self


# --- DOCUMENT sub-objects (§3.2) -----------------------------------------
class Image(BaseModel):
    url: str
    storage_path: Optional[str] = None
    caption: Optional[str] = None
    role: Optional[str] = None        # product | event | chart | map | other
    width: Optional[int] = None
    height: Optional[int] = None


class Attachment(BaseModel):
    url: str
    storage_path: Optional[str] = None
    type: str = "pdf"
    extracted_text: Optional[str] = None


class Media(BaseModel):
    """A video/audio link recorded as metadata only — never downloaded (§4)."""
    url: str
    type: str = "video"               # video | audio
    title: Optional[str] = None


class Screenshot(BaseModel):
    storage_path: str
    captured_at: Optional[str] = None


class Table(BaseModel):
    title: Optional[str] = None
    rows: list[dict] = Field(default_factory=list)


class Document(BaseModel):
    url: str                                  # canonical, dedup key, REQUIRED
    content_hash: str                         # sha256 of main_text, REQUIRED
    fetched_at: str                           # REQUIRED (ISO)
    source_id: str                            # resolved outlet id, REQUIRED
    source_tier: Optional[int] = None         # 1 primary | 2 trade press | 3 aggregator
    source_type: Optional[str] = None         # category (gov_primary, trade_press, …)
    source_region: Optional[str] = None
    source_known: bool = True                 # False = classifier fell back to low trust
    source_resolved_by: Optional[str] = None  # job | registry | heuristic | fallback
    title: str                                # REQUIRED
    author: Optional[str] = None
    published_at: Optional[str] = None
    date_precision: Literal["exact", "approx", "unknown"] = "unknown"
    language: str = "en"
    access: Literal["open", "paywalled", "partial"] = "open"
    main_text: str                            # cleaned body, REQUIRED
    main_text_en: Optional[str] = None        # English translation if language != en
    html: str = ""                            # raw source HTML of the page
    summary: Optional[str] = None
    images: list[Image] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    media: list[Media] = Field(default_factory=list)   # video/audio links (metadata only)
    screenshot: Optional[Screenshot] = None
    tables: list[Table] = Field(default_factory=list)

    # This bundle's own id (idempotency/logging). There are no sibling
    # records to link to — L1 sends exactly one bundle per kept page.
    # Entity resolution + record classification are Layer 2's job, run on
    # main_text/html; L1 no longer ships detection tags.
    document_id: Optional[str] = None
