"""HTTP API routers — the two hard interfaces.

- ``ingest``  → Interface A (L1 → L2): the crawler POSTs records here.
- ``serving`` → Interface B (L2 → L3): the client reads here.
- ``ops``     → internal pipeline/health controls (not client-facing).
"""
