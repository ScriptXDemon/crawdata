"""SQLAlchemy models across the four namespaces.

- ``reference``  → ref_*  (static seed, admin-owned)
- ``staging``    → stg_*  (crawler raw, written by the Ingest API)
- ``serving``    → srv_*  (read-only tables the Layer 3 client consumes)

Importing this package registers every table on ``Base.metadata``.
"""

from . import graph, llm_ops, reference, serving, staging  # noqa: F401

__all__ = ["reference", "staging", "serving", "llm_ops", "graph"]
