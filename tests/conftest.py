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
    order-independent and repeatable, regardless of backend.

    Module 6 added a self-referential FK (task_runs.source_task_run_id ->
    task_runs.id, ondelete=RESTRICT). A single bulk DELETE against a table
    with a RESTRICT self-reference can fail depending on which row SQLite
    happens to delete first within that statement (the referencing row's
    delete succeeding is not guaranteed to precede the referenced row's),
    so self-referencing columns are nulled out first, independent of
    delete order, before the normal dependency-ordered delete pass runs."""
    from app.db.base import Base
    from app.db.session import SessionLocal

    yield
    db = SessionLocal()
    try:
        for table in Base.metadata.sorted_tables:
            # A column can appear in a self-referential FK constraint
            # (referred_table is this table) while ALSO appearing in a
            # different, non-self FK constraint on the same table -- e.g.
            # task_runs.organization_id is part of BOTH the self-
            # referential (organization_id, source_task_run_id) ->
            # task_runs constraint AND the ordinary organization_id ->
            # organizations.id constraint. Only columns exclusively used
            # for self-reference are safe to null out here; nulling a
            # shared column like organization_id would violate its own
            # NOT NULL constraint.
            used_elsewhere: set[str] = set()
            self_ref_only: set[str] = set()
            for fkc in table.foreign_key_constraints:
                names = {c.name for c in fkc.columns}
                if fkc.referred_table is table:
                    self_ref_only |= names
                else:
                    used_elsewhere |= names
            nullable_self_ref_columns = [
                table.c[name]
                for name in (self_ref_only - used_elsewhere)
                if table.c[name].nullable
            ]
            if nullable_self_ref_columns:
                db.execute(
                    table.update().values(
                        {col.name: None for col in nullable_self_ref_columns}
                    )
                )
        db.commit()
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
