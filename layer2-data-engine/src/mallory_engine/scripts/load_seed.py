"""Load the bundled watchlist seed into the ref_* tables."""

from __future__ import annotations

from ..db import SessionLocal
from ..seed.loader import load_all


def main() -> None:
    with SessionLocal() as db:
        counts = load_all(db)
    print("Seeded ref_* tables:", counts)


if __name__ == "__main__":
    main()
