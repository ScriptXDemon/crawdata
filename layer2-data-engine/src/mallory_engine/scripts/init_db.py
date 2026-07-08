"""Create all tables (dev convenience; use Alembic for production migrations)."""

from __future__ import annotations

from .. import models  # noqa: F401  (registers all tables on Base.metadata)
from ..db import Base, engine


def main() -> None:
    Base.metadata.create_all(engine)
    print(f"Created {len(Base.metadata.tables)} tables.")


if __name__ == "__main__":
    main()
