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

- Python 3.10+
- Docker Desktop (or Docker Engine + Docker Compose plugin)
- Git

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

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
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

## Running Tests

From the repository root:

```bash
cd backend
pip install -r requirements-dev.txt
cd ..
PYTHONPATH=backend pytest -v --cov=backend/app --cov-report=term-missing
```

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
| `SECRET_KEY`    | Application secret, used by future auth modules |

## License

Proprietary — all rights reserved.
