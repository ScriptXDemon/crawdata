"""Run the processing pipeline over pending staging rows (stg_* → srv_*)."""

from __future__ import annotations

from ..db import SessionLocal
from ..pipeline.runner import process_pending


def main() -> None:
    with SessionLocal() as db:
        result = process_pending(db)
    print(f"Processed {result.signals_processed} signal(s), {result.tenders_processed} tender(s).")


if __name__ == "__main__":
    main()
