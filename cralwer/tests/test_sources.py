"""Tests for source_id + tier resolution (L2-confirmed design)."""
from crawler.models import Job
from crawler.seed import load_seed
from crawler.sources import mint_source_id, resolve_source

SEED = load_seed()


def _job(**kw):
    base = dict(job_id="j", job_type="news", seed_urls=["https://x"], keywords=["a"])
    base.update(kw)
    return Job(**base)


# --- stable id from eTLD+1 (subdomains collapse) -------------------------
def test_source_id_from_registrable_domain():
    assert mint_source_id("https://raksha-anirveda.com/x") == "RAKSHAANIRVEDA"
    a = mint_source_id("https://www.raksha-anirveda.com/x")
    b = mint_source_id("https://m.raksha-anirveda.com/y?z=1")
    assert a == b == "RAKSHAANIRVEDA"          # www./m. collapse to one id


def test_source_id_public_suffix_aware():
    assert mint_source_id("https://foo.gov.in/x") == "FOO"
    assert mint_source_id("https://news.foo.co.uk/x") == "FOO"


# --- precedence: job > registry > heuristic > fallback -------------------
def test_job_stamped_used_verbatim():
    job = _job(source_id="RAKSHAANIRVEDA", source_tier=3, source_type="aggregator",
               source_region="India")
    si = resolve_source("https://raksha-anirveda.com/x", SEED, job)
    assert (si.source_id, si.source_tier, si.source_type, si.source_region) == \
        ("RAKSHAANIRVEDA", 3, "aggregator", "India")
    assert si.source_known and si.source_resolved_by == "job"


def test_job_stamped_tier_defaults_from_type():
    job = _job(source_id="X", source_type="trade_press")   # tier omitted
    si = resolve_source("https://x.example/x", SEED, job)
    assert si.source_tier == 2 and si.source_resolved_by == "job"


def test_registry_wins_when_not_stamped():
    si = resolve_source("https://idrw.org/some-article", SEED)
    assert si.source_id == "IDRW" and si.source_tier == 3
    assert si.source_resolved_by == "registry" and si.source_known
    janes = resolve_source("https://www.janes.com/x", SEED)
    assert janes.source_id == "JANES" and janes.source_tier == 1


# --- heuristic classification --------------------------------------------
def test_gov_domain_is_tier1():
    si = resolve_source("https://someministry.gov.in/tenders", SEED)
    assert si.source_tier == 1 and si.source_type == "gov_primary"
    assert si.source_resolved_by == "heuristic" and si.source_known


def test_manufacturer_and_think_tank():
    m = resolve_source("https://bharatforge.com/defence", SEED)
    assert m.source_type == "manufacturer_ir" and m.source_tier == 1
    t = resolve_source("https://idsa.in/analysis", SEED)
    assert t.source_type == "think_tank" and t.source_tier == 2


# --- the headline fix: unknown domain FAILS SAFE to tier 3, not tier 1 ---
def test_unknown_domain_fails_safe_to_low_trust():
    si = resolve_source("https://raksha-anirveda.com/k9-vajra", SEED)
    assert si.source_id == "RAKSHAANIRVEDA"
    assert si.source_tier == 3                 # NOT tier 1
    assert si.source_type == "aggregator"
    assert si.source_known is False            # provenance flag
    assert si.source_resolved_by == "fallback"


def test_no_unknown_ever_gets_tier1():
    for url in ["https://randomblog.xyz/post", "https://someguy.wordpress.com/p",
                "https://obscure-forum.net/thread"]:
        assert resolve_source(url, SEED).source_tier == 3
