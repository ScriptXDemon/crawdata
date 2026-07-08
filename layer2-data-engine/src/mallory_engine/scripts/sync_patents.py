"""Run the free patent connector against the live DB. `python -m mallory_engine.scripts.sync_patents`."""
import sys

from sqlalchemy.orm import Session

from ..db import engine
from ..services import graph_builder, patent_sync

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _selfcheck() -> None:
    # pure-function mapping sanity (runs offline, no network)
    assert patent_sync._jurisdiction("US20250300420A1") == "US"
    assert patent_sync._jurisdiction("EP4123456A1") == "EP"
    assert patent_sync._status("US11123456B2") == "granted"
    assert patent_sync._status("US20250300420A1") == "pending"
    print("selfcheck ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _selfcheck()
        raise SystemExit(0)
    with Session(engine) as db:
        counts = patent_sync.sync_patents(db)
        print("\npatent sync:", counts)
        # patents feed graph edges (competitor -[filed]-> patent); rebuild so they show
        graph_builder.rebuild_graph(db)
        db.commit()
    print("done.")
