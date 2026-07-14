"""Tests for the Crawler HTTP API (job in -> raw page bundles out)."""
from fastapi.testclient import TestClient

from crawler_api.app import app
from crawler.jobgen import _candidate_pool
from crawler.seed import load_seed

client = TestClient(app)
_SEED = load_seed()

LT_JOB = {
    "job_id": "api_test_LT",
    "job_type": "news",
    "seed_urls": ["https://idrw.org/lt-k9-vajra-followon/"],
    "keywords": ["L&T", "K9 Vajra", "artillery"],
    "target_entity": "LT",
    "max_pages": 5, "max_depth": 1,
    "capture": ["html", "text", "screenshot"],
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_crawl_returns_page_bundle():
    r = client.post("/v1/crawl", json=LT_JOB)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "api_test_LT"
    assert len(body["documents"]) == 1
    doc = body["documents"][0]
    assert doc["url"] and doc["content_hash"].startswith("sha256:") and doc["main_text"]
    assert doc["html"]                          # raw source HTML present
    assert body["summary"]["sent"] == 1
    assert body["summary"]["accepted"] == 1
    assert body["summary"]["rejected"] == 0


def test_schema_exposes_contract():
    body = client.get("/v1/schema").json()
    assert "job_input" in body
    assert "document_output" in body
    assert body["ingest_endpoint"].endswith("/page")


def test_batch_endpoint():
    r = client.post("/v1/crawl/batch", json={"jobs": [
        {**LT_JOB, "job_id": "batch_a", "seed_urls":
         ["https://www.shephardmedia.com/news/ramjet-155mm-rheinmetall/"],
         "keywords": ["ramjet", "155mm", "Rheinmetall"], "target_entity": None},
    ]})
    assert r.status_code == 200
    out = r.json()
    assert out["jobs"] == 1
    assert out["results"][0]["documents"]       # produced a page bundle


def test_batch_parallel_preserves_order_and_runs_all():
    # Two jobs, run concurrently — results must come back in input order and
    # both must complete (each job runs on its own thread + CrawlHistory).
    # Uses fixture URLs no other api test touches (dedup history is on-disk and
    # shared, so re-crawled URLs would come back as skipped_unchanged).
    r = client.post("/v1/crawl/batch", json={"parallel": 2, "jobs": [
        {**LT_JOB, "job_id": "par_adani",
         "seed_urls": ["https://economictimes.indiatimes.com/news/defence/adani-acquires-general-aeronautics/"],
         "keywords": ["Adani", "Adani Defence", "acquisition"], "target_entity": "ADANI"},
        {**LT_JOB, "job_id": "par_knds",
         "seed_urls": ["https://www.defensenews.com/global/2026/06/27/knds-caesar-nigeria/"],
         "keywords": ["KNDS", "Nexter", "CAESAR"], "target_entity": "KNDS"},
    ]})
    assert r.status_code == 200
    out = r.json()
    assert out["jobs"] == 2
    # order preserved despite concurrent completion
    assert [x["job_id"] for x in out["results"]] == ["par_adani", "par_knds"]
    # both jobs actually produced a bundle
    assert out["results"][0]["documents"] and out["results"][1]["documents"]


def test_check_keywords_matches_without_crawling():
    r = client.post("/v1/check-keywords", json={
        "url": "https://idrw.org/lt-k9-vajra-followon/",
        "keywords": ["K9 Vajra", "artillery", "submarine"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is True
    assert "K9 Vajra" in body["matched_keywords"]
    assert "submarine" not in body["matched_keywords"]
    # debug-only internal keys must not leak through the API
    assert "_text" not in body and "_title" not in body


def test_check_keywords_no_match():
    r = client.post("/v1/check-keywords", json={
        "url": "https://idrw.org/lt-k9-vajra-followon/",
        "keywords": ["submarine", "frigate"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is False
    assert body["matched_keywords"] == []


def test_check_keywords_batch_probes_many_urls():
    r = client.post("/v1/check-keywords/batch", json={
        "urls": ["https://idrw.org/lt-k9-vajra-followon/",
                 "https://www.shephardmedia.com/news/ramjet-155mm-rheinmetall/"],
        "keywords": ["K9 Vajra", "artillery"],
        "parallel": 2,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # results in input order
    assert body["results"][0]["url"].endswith("lt-k9-vajra-followon/")
    # the LT page matches the LT keywords; 'relevant' lists the URLs that hit
    assert "https://idrw.org/lt-k9-vajra-followon/" in body["relevant"]


def test_suggest_job_builds_job_with_probe_selected_keywords():
    r = client.post("/v1/suggest-job", json={
        "url": "https://idrw.org/lt-k9-vajra-followon/",
        "target_entity": "LT", "job_type": "news",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["relevant"] is True
    assert body["pool_size"] > 0
    kws = set(body["selected_keywords"])
    # selected keywords are a subset of the candidate pool that appear on the page
    assert kws and kws <= set(_candidate_pool(_SEED, "LT", "news"))
    # returned job is ready to POST to /v1/crawl
    job = body["job"]
    assert job["seed_urls"] == ["https://idrw.org/lt-k9-vajra-followon/"]
    assert set(job["keywords"]) == kws
    assert job["target_entity"] == "LT"


def test_suggest_job_irrelevant_seed_returns_empty():
    r = client.post("/v1/suggest-job", json={
        "url": "https://www.shephardmedia.com/news/ramjet-155mm-rheinmetall/",
        "target_entity": "NIBE", "job_type": "news",   # NIBE terms not on this page
    })
    assert r.status_code == 200
    body = r.json()
    # NIBE-specific keywords won't appear on a ramjet article
    assert body["relevant"] is False
    assert body["selected_keywords"] == [] and body["job"]["keywords"] == []
