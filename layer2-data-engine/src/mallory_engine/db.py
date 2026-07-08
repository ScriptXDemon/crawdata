"""Database engine, session factory, and declarative base.

Postgres is the production target; SQLite is a first-class dev/demo fallback
(``DATABASE_URL=sqlite:///./mallory.db``) — no docker needed. The two variants below make
the same models work on both: JSONB renders as JSON on SQLite, and connections are shared
across FastAPI's threads.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


_settings = get_settings()

_is_sqlite = _settings.database_url.startswith("sqlite")
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=not _is_sqlite,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    future=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        # WAL = concurrent readers alongside one writer (the scheduler) without blocking;
        # busy_timeout = wait for a held write lock instead of failing with "database is locked"
        # when the crawler POSTs pages while the pipeline is mid-run. Postgres needs neither.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a scoped session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
