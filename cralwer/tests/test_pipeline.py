"""Integration tests: the full HARVEST->FILTER->EXTRACT->INGEST pipeline on the
shipped fixtures, plus §7A re-crawl idempotency.

Entity resolution was removed from L1 (it's Layer 2's job now), so these tests
pass an EMPTY keyword corpus — which makes the gate keep every page (fail-open)
— to exercise document assembly / dedup / PDF / translation / images / title
independently of corpus tuning. Gate keep/drop behavior is covered in
test_units.py."""
from crawler import keywords as kwmod
from crawler.dedup import CrawlHistory
from crawler.ingest_client import InProcessIngestClient
from crawler.pipeline import run_job
from crawler.seed import load_seed
from crawler.testing_batch import build as build_batch
from ingest_api.app import reset

SEED = load_seed()
KEEP_ALL = kwmod.from_list([])          # empty corpus -> gate keeps every page (fail-open)


def _fresh():
    reset()
    return CrawlHistory(":memory:"), InProcessIngestClient()


def _job(job_id):
    return next(j for j in build_batch() if j.job_id.startswith(job_id) or job_id in j.job_id)


def test_lt_news_sends_one_page_bundle():
    h, c = _fresh()
    r = run_job(_job("LT_news"), c, SEED, h, KEEP_ALL)
    assert r.kept == 1 and r.sent == 1 and r.rejected == 0
    doc = r.documents[0]
    assert "nav" not in doc.main_text.lower()        # boilerplate stripped
    assert doc.content_hash.startswith("sha256:")
    assert doc.html                                   # raw source HTML retained


def test_tender_extracts_pdf_attachment_text():
    h, c = _fresh()
    r = run_job(_job("MOD_tender"), c, SEED, h, KEEP_ALL)
    doc = r.documents[0]
    # the RFP PDF text made it into an attachment (raw text, no field extraction)
    assert any(a.extracted_text and "REQUEST FOR PROPOSAL" in a.extracted_text.upper()
               for a in doc.attachments)


def test_non_english_translation():
    h, c = _fresh()
    r = run_job(_job("KNDS_news"), c, SEED, h, KEEP_ALL)
    doc = r.documents[0]
    assert doc.language == "fr"
    assert doc.main_text and doc.main_text_en
    assert "KNDS" in doc.main_text_en


def test_same_run_duplicate_content_emitted_once():
    # Two distinct URLs (e.g. an SPA's normal page + its AMP mirror) that
    # render byte-identical main_text must not both be emitted as separate
    # documents within the same run.
    h, c = _fresh()
    from crawler.models import Job
    job = Job(job_id="dup_test", job_type="news",
              seed_urls=["https://idrw.org/lt-k9-vajra-followon/",
                         "https://idrw.org/lt-k9-vajra-followon/amp/"],
              keywords=["K9", "vajra"], max_depth=0)
    r = run_job(job, c, SEED, h, KEEP_ALL)
    assert r.fetched == 2                 # both pages were fetched
    assert r.kept == 1                    # only the first is kept/sent
    assert r.skipped_duplicate == 1
    assert len(r.documents) == 1


def test_recrawl_is_idempotent():
    h, c = _fresh()
    job = _job("LT_news")
    r1 = run_job(job, c, SEED, h, KEEP_ALL)
    r2 = run_job(job, c, SEED, h, KEEP_ALL)
    assert r1.sent > 0
    assert r2.sent == 0                            # unchanged -> nothing new
    assert r2.not_modified >= 1 or r2.skipped_unchanged >= 1


def test_spec_keeps_all_nonjunk_images():
    h, c = _fresh()
    r = run_job(_job("CAESAR_spec"), c, SEED, h, KEEP_ALL)
    doc = r.documents[0]
    names = [i.url.rsplit("/", 1)[-1] for i in doc.images]
    assert "caesar.jpg" in names
    assert "logo.png" not in names and "ad-banner.png" not in names  # junk still dropped
    assert doc.tables and len(doc.tables[0].rows) == 5
    # media captured as metadata only (video link, never downloaded)
    assert any(m.type == "video" and "youtube" in m.url for m in doc.media)


def test_title_strips_site_suffix():
    h, c = _fresh()
    r = run_job(_job("LT_news"), c, SEED, h, KEEP_ALL)
    assert r.documents[0].title == "L&T secures ₹4,500 cr K9 Vajra follow-on order"
