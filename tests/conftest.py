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
