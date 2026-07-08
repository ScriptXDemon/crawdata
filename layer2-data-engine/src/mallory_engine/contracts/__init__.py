"""Pydantic contracts — the typed boundaries between layers.

- ``ingest``  → the L1→L2 contract (document + 6 typed records). Validated at the Ingest API.
- ``serving`` → the L2→L3 DTOs returned by the read-only Serving API.
"""
