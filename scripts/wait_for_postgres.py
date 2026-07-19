#!/usr/bin/env python3
"""
Block until the configured Postgres database accepts connections, or exit
non-zero after a timeout. Intended for use in entrypoint scripts / CI, as a
belt-and-suspenders complement to docker-compose's `depends_on.condition`.

Usage:
    PYTHONPATH=backend python scripts/wait_for_postgres.py --timeout 30
"""
import argparse
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings


def wait_for_postgres(timeout_seconds: int = 30, interval_seconds: float = 1.0) -> bool:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except OperationalError as exc:
            last_error = exc
            time.sleep(interval_seconds)

    print(f"Database not reachable after {timeout_seconds}s: {last_error}", file=sys.stderr)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    if wait_for_postgres(timeout_seconds=args.timeout):
        print("Database is reachable.")
        sys.exit(0)
    sys.exit(1)
