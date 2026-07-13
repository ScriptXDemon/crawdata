"""
DEPRECATED: Old synchronous crawler logic (kept for reference/fallback only).

These modules implement sequential single-threaded crawling:
- pipeline.py: Sync job orchestration (harvest → extract → ingest)
- harvest.py: Sequential URL fetcher (one page at a time)
- interaction.py: Sync form fill/pagination (see async_engine.interaction_async for production)

PRODUCTION uses async_engine.py instead (8 browsers × 12 tabs = 96 concurrent pages).

To re-enable: Set CRAWLER_ASYNC_ENGINE=0 in environment and restore imports in crawler_api/app.py.
Timeline to delete: After 3+ months of stable async production.
"""
