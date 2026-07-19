"""Shared pytest fixtures."""
import os
import sys
from pathlib import Path

# Make backend/app importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# Force a local SQLite database for the test suite so tests never depend on
# a running Postgres instance. Must be set before app.core.config is imported
# anywhere (including transitively via app.main).
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_FORMAT", "console")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


# --- Module 2: database fixtures --------------------------------------------
# These respect a pre-set DATABASE_URL (see setdefault() above), so the exact
# same test suite can be pointed at a real Postgres instance by exporting
# DATABASE_URL before invoking pytest — see README "Authentication" section
# for the exact commands.


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Create all ORM-mapped tables once per test session. Uses
    Base.metadata.create_all() rather than running Alembic migrations,
    so this fixture works identically against SQLite (sandbox) and
    Postgres (real verification) without needing a migration runner
    available at test time."""
    from app.db.base import Base
    from app.db.session import engine
    import app.models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Delete all rows from every table before each test so tests are
    order-independent and repeatable, regardless of backend."""
    from app.db.base import Base
    from app.db.session import SessionLocal

    yield
    db = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db_session():
    """A raw DB session for tests that need to set up data directly."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
