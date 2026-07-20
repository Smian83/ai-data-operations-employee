"""
Database engine and session management.

Provides a single SQLAlchemy engine for the process and a FastAPI dependency
(`get_db`) that yields a request-scoped session and guarantees cleanup.
"""
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

_engine_kwargs = {"pool_pre_ping": True}
# SQLite (used only as a local fallback when DATABASE_URL is unset) requires
# this flag for use with FastAPI's threaded request handling.
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.database_url.startswith("sqlite"):
    # SQLite does not enforce FOREIGN KEY constraints by default (unlike
    # PostgreSQL, where they are always enforced) — it must be turned on
    # per-connection. Without this, Module 3's tenant-aware composite
    # foreign keys would silently pass on SQLite while still being
    # correctly enforced on PostgreSQL, making local/sandbox testing give
    # false confidence. This only makes the SQLite fallback stricter
    # (closer to real Postgres behavior), never looser.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
