# AI Data Operations Employee

A production-grade SaaS backend that acts as an autonomous "AI Data Operations Employee."

> **Status:** Module 1 — Foundation

## Repository

https://github.com/Smian83/ai-data-operations-employee

## Tech Stack

| Layer            | Technology                     |
|-------------------|---------------------------------|
| API framework     | FastAPI + Uvicorn               |
| Validation/config | Pydantic v2 / pydantic-settings |
| ORM               | SQLAlchemy 2.0                  |
| Migrations        | Alembic                         |
| Database          | PostgreSQL 16 (Docker)          |
| DB driver         | psycopg 3 (binary)              |
| Logging           | Structured logging (stdlib + python-json-logger) |
| Testing           | pytest, httpx, pytest-cov       |
| Containerization  | Docker, Docker Compose          |

## Project Structure

```
ai-data-operations-employee/
├── backend/                     # FastAPI application
│   ├── app/
│   │   ├── main.py               # App factory / entrypoint
│   │   ├── core/
│   │   │   ├── config.py         # Pydantic settings (env-driven)
│   │   │   └── logging.py        # Structured logging configuration
│   │   ├── api/
│   │   │   └── health.py         # GET /health
│   │   ├── db/
│   │   │   ├── base.py           # Declarative base
│   │   │   └── session.py        # Engine / session factory
│   │   ├── models/               # SQLAlchemy ORM models (empty in Module 1)
│   │   └── schemas/              # Pydantic request/response schemas
│   ├── requirements.txt          # Production dependencies (pinned)
│   └── requirements-dev.txt      # + testing dependencies
├── frontend/                     # Reserved for a future module
├── database/                     # Migrations
│   ├── alembic.ini
│   └── alembic/
│       ├── env.py
│       └── versions/
├── docker/
│   ├── Dockerfile                # Multi-stage production image
│   └── .dockerignore
├── docker-compose.yml            # app + postgres services
├── docs/                         # Project documentation
├── tests/
│   ├── conftest.py
│   └── test_health.py
├── scripts/
│   └── wait_for_postgres.py      # Startup dependency check
├── pytest.ini
├── .env.example                  # Template for local environment variables
└── .gitignore
```

## Prerequisites

- **Python 3.13** (required — see "Why Python 3.13" below)
- Docker Desktop (or Docker Engine + Docker Compose plugin)
- Git

### Why Python 3.13

This project officially targets **Python 3.13**, pinned via `.python-version` and
the `docker/Dockerfile` base image (`python:3.13-slim`).

- **Not 3.14:** Python 3.14 is very new. Two of our pinned production dependencies
  (`psycopg[binary]`, and transitively `pydantic`/`pydantic-core`) either lacked
  `cp314` wheels outright or only gained them in versions released within the last
  ~2 months, forcing pip to compile from source (which requires a full C/Rust
  toolchain — e.g. Visual Studio Build Tools on Windows). We don't want production
  installs depending on a toolchain being present. 3.13 has ~1.5 years of ecosystem
  wheel coverage and every currently pinned dependency has verified prebuilt wheels
  for it.
- **Not 3.12:** Not required. It offers no advantage over 3.13 for this project's
  dependencies, and 3.13 is already what's recommended, so there's no reason to
  install an additional interpreter.

If you have multiple Python versions installed on Windows, create the virtual
environment explicitly with the `py` launcher:

```powershell
py -3.13 -m venv backend\.venv
```

## Local Development Setup

### 1. Clone and enter the project

```bash
git clone https://github.com/Smian83/ai-data-operations-employee.git
cd ai-data-operations-employee
```

### 2. Create your environment file

```bash
cp .env.example .env
# edit .env and set real values, especially SECRET_KEY and POSTGRES_PASSWORD
```

### 3. Run with Docker (recommended)

```bash
docker compose up --build
```

- API: http://localhost:8000
- Health check: http://localhost:8000/health
- Interactive docs: http://localhost:8000/docs

Stop and remove containers:

```bash
docker compose down
```

Stop and also wipe the Postgres volume (destructive):

```bash
docker compose down -v
```

### 4. Run natively (without Docker)

Requires Python 3.13 (see "Why Python 3.13" above).

```bash
cd backend
python3.13 -m venv .venv            # Windows: py -3.13 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Requires a reachable Postgres instance matching DATABASE_URL in .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Database Migrations (Alembic)

Run from the repository root:

```bash
cd database
alembic revision --autogenerate -m "describe change"
alembic upgrade head
alembic downgrade -1
```

### Verifying a migration against real PostgreSQL (required — not optional)

SQLite is used only for fast sandbox/unit-test iteration. It does not
enforce the same constraint and transaction behavior as PostgreSQL, so
every migration must also be verified against a real running Postgres
container before being considered done. On Windows PowerShell:

```powershell
# 1. Start Postgres (and the app) via Docker
docker compose up -d db

# 2. Activate your venv
backend\.venv\Scripts\activate

# 3. Point alembic at the real Postgres container (mapped to localhost:5432)
#    Use the same POSTGRES_PASSWORD you set in your .env file.
$env:DATABASE_URL = "postgresql+psycopg://aidataops:<your POSTGRES_PASSWORD>@localhost:5432/aidataops"
cd database
alembic upgrade head
cd ..

# 4. Confirm the tables exist with the expected constraints
docker compose exec db psql -U aidataops -d aidataops -c "\d organizations"
docker compose exec db psql -U aidataops -d aidataops -c "\d users"
```

You should see the `uq_organizations_slug` unique constraint on
`organizations`, and both the `uq_users_org_email` unique constraint and the
`fk_users_organization_id_organizations` foreign key on `users`.

To roll back: `alembic downgrade -1` (from the `database/` directory, with
the same `DATABASE_URL` set).

## Running Tests

From the repository root, against the fast SQLite fallback:

```bash
cd backend
pip install -r requirements-dev.txt
cd ..
PYTHONPATH=backend pytest -v --cov=backend/app --cov-report=term-missing
```

### Running the same suite against real PostgreSQL (required for Module 2)

The exact same tests can run against your real Postgres container instead
of SQLite — set `DATABASE_URL` before invoking pytest and it takes priority
over the SQLite default. Use a **separate database** so tests never touch
your dev data:

```powershell
docker compose up -d db
docker compose exec db psql -U aidataops -c "CREATE DATABASE aidataops_test;"

backend\.venv\Scripts\activate
$env:DATABASE_URL = "postgresql+psycopg://aidataops:<your POSTGRES_PASSWORD>@localhost:5432/aidataops_test"
$env:PYTHONPATH = "backend"
pytest -v tests/
```

Every test cleans up its own rows between tests (see `tests/conftest.py`),
so this is safe to run repeatedly.

## Authentication (Module 2)

Multi-tenant JWT authentication. Every user belongs to exactly one
**organization** (tenant). Because email is only unique *within* an
organization (not globally), login requires three fields, not two.

### `POST /auth/register`

Creates a new organization and its first (admin) user in one transaction,
returns a token. Fails with `409` if the organization slug is already taken.

```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "organization_name": "Acme Corp",
    "email": "owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Owner Person"
  }'
```

`organization_slug` is optional — if omitted it's deterministically derived
from `organization_name` (e.g. "Acme Corp" -> "acme-corp"). A colliding slug
is always rejected with `409`, never silently modified.

Password rules: minimum 8 characters, and must not exceed bcrypt's 72-byte
(UTF-8 encoded) limit — passwords are never truncated, an oversized password
is rejected with `422`.

### `POST /auth/login`

```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "organization_slug": "acme-corp",
    "email": "owner@example.com",
    "password": "correct-horse-battery"
  }'
```

Returns `401` for a wrong organization slug, unknown email, or wrong
password (one generic error for all three, to avoid leaking which one was
wrong). Returns `403` if the account exists but is inactive.

### `GET /auth/me`

```bash
curl http://localhost:8000/auth/me -H "Authorization: Bearer <token>"
```

Returns the current user. `401` with no/invalid token, `403` if inactive.

### Tenant isolation rules

- Email is unique **per organization**, not globally — the same email can
  register in two different organizations.
- Emails and organization slugs are normalized (lowercased, trimmed) before
  storage and comparison.
- A JWT encodes both the user id (`sub`) and `org_id`. On every request,
  `org_id` is re-checked against the user's *current* `organization_id` in
  the database, not just trusted from the token.

## Health Endpoint

`GET /health` returns:

```json
{
  "status": "healthy"
}
```

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable        | Description                                    |
|-----------------|-------------------------------------------------|
| `APP_ENV`       | `development`, `staging`, or `production`       |
| `DATABASE_URL`  | Full SQLAlchemy connection string for Postgres  |
| `LOG_LEVEL`     | Minimum log level to emit                       |
| `LOG_FORMAT`    | `json` (production) or `console` (development)  |
| `SECRET_KEY`    | Application secret. Used to sign JWTs (Module 2) — generate a real 32+ byte random value before production, never commit it |
| `JWT_ALGORITHM` | JWT signing algorithm (default `HS256`)          |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Access token lifetime in minutes (default `60`) |

## License

Proprietary — all rights reserved.
