"""Mallory Layer 2 — Data Engineering service.

Three inputs (crawler ``stg_*`` via the Ingest API, external APIs ``ext_*``, admin seed
``ref_*``) and exactly one output consumer: the Layer 3 client, which reads ``srv_*`` through
the read-only Serving API. All "vs KSSL" compute lives here.
"""

__version__ = "0.1.0"
