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
│   │   │   ├── logging.py        # Structured logging configuration
│   │   │   ├── security.py       # Password hashing, JWT, slug generation
│   │   │   └── validation.py     # Secret-key denylist, name normalization
│   │   ├── api/
│   │   │   ├── health.py         # GET /health
│   │   │   ├── auth.py           # /auth/register, /login, /me
│   │   │   ├── deps.py           # get_current_active_user, pagination, superuser gate
│   │   │   ├── data_sources.py   # /data-sources CRUD + credentials (write-only)
│   │   │   ├── tasks.py          # /tasks CRUD + /tasks/{id}/runs + run events
│   │   │   └── internal.py       # /internal/metrics (Module 4, superuser-only)
│   │   ├── db/
│   │   │   ├── base.py           # Declarative base
│   │   │   └── session.py        # Engine / session factory
│   │   ├── models/                # organization, user, data_source, task,
│   │   │                          # task_run, task_run_event, data_source_credential, enums
│   │   ├── schemas/               # Pydantic request/response schemas
│   │   └── worker/                # Module 4: task execution engine
│   │       ├── engine.py          # claim/heartbeat/complete (lease_token fencing)
│   │       ├── reaper.py          # stuck-run recovery (expired leases)
│   │       ├── credentials.py     # CredentialProvider abstraction
│   │       ├── metrics.py         # Prometheus counters/gauges/histogram
│   │       ├── runner.py          # worker process main loop
│   │       └── handlers/          # ExecutionHandler registry + no-op handler
│   ├── requirements.txt          # Production dependencies (pinned)
│   └── requirements-dev.txt      # + testing dependencies
├── frontend/                     # Reserved for a future module
├── database/                     # Migrations
│   ├── alembic.ini
│   └── alembic/
│       ├── env.py
│       └── versions/              # organizations+users, data_sources+tasks+task_runs,
│       │                          # task execution engine (Module 4)
├── docker/
│   ├── Dockerfile                # Multi-stage production image
│   └── .dockerignore
├── docker-compose.yml            # app + postgres services
├── docs/                         # Project documentation
├── tests/
│   ├── conftest.py
│   ├── test_health.py
│   ├── test_auth.py
│   ├── test_data_sources.py
│   ├── test_tasks.py
│   ├── test_worker_engine.py
│   ├── test_worker_reaper.py
│   ├── test_worker_credentials.py
│   ├── test_worker_handlers.py
│   ├── test_worker_metrics.py
│   └── test_worker_api.py
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

# Required as of Module 4: every native PostgreSQL enum type
# (source_type_enum, task_type_enum, task_run_status_enum) is owned
# exclusively by Alembic migrations (create_type=False on the ORM models —
# see "Task Execution Engine" below for why). Base.metadata.create_all(),
# which tests/conftest.py uses to build tables, therefore requires these
# types to already exist. Run migrations against aidataops_test once
# before the first test run against it:
cd database; alembic upgrade head; cd ..

$env:PYTHONPATH = "backend"
pytest -v tests/
```

Every test cleans up its own rows between tests (see `tests/conftest.py`),
so this is safe to run repeatedly. Re-running `alembic upgrade head` against
`aidataops_test` on a subsequent session is also safe — it's a no-op once
already at head.

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

## Data Sources & Tasks (Module 3)

Core domain model: each organization can register **Data Sources** (systems
the AI agent will operate on) and define **Tasks** against them. This module
only models and CRUDs these — actually executing a task is the Module 4
Task Execution Engine below. All endpoints require a Bearer token (see Authentication above)
and are strictly scoped to the caller's organization.

### Data Sources

```bash
curl -X POST http://localhost:8000/data-sources \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name": "Prod Postgres", "source_type": "postgres", "connection_metadata": {"host": "db.internal"}}'
```

`source_type` is one of `postgres`, `mysql`, `rest_api`, `csv_upload`, `s3`, `other`
— enforced by a native PostgreSQL enum type, not just request validation.

**`connection_metadata` must never contain secrets.** Keys that look like
credentials (`password`, `token`, `api_key`, `secret`, etc. — checked
recursively) are rejected with `422`. Real credential storage is a future
encrypted secrets module; this is metadata only (host, port, bucket name, etc).

`GET/PATCH/DELETE /data-sources/{id}` and `GET /data-sources` (paginated,
`limit`/`offset`, default 50 / max 100) — standard CRUD. `DELETE` is a soft
delete (`is_active=False`); afterwards the resource behaves exactly like it
doesn't exist for every operation (`404`, including inactive results being
excluded from `GET /data-sources` unless `?include_inactive=true`). Names
are unique per organization, case-insensitively, among active resources only
— deleting a data source frees its name for reuse.

### Tasks

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name": "Nightly Sync", "task_type": "sync", "data_source_id": "<data-source-id>"}'
```

`task_type` is one of `sync`, `transform`, `export`, `other`. `data_source_id`
is optional; if given, it must reference an **active** data source in the
**same organization** — enforced both by a PostgreSQL composite foreign key
(`(organization_id, data_source_id) -> data_sources(organization_id, id)`,
not just application code) and, for the "inactive" case, an application-level
check. Either way the failure is `404`, identical to a non-existent reference.

Same CRUD/pagination/soft-delete/case-insensitive-naming rules as Data Sources.

### Task Runs

`POST /tasks/{id}/runs` creates a run record in `pending` status — this is an
enqueue stub; the Module 4 worker process picks it up and executes it. `GET
/tasks/{id}/runs` (paginated) and `GET /tasks/{id}/runs/{run_id}` read run
history. A run's `organization_id`, `task_id`, and `triggered_by` are always
server-derived, never accepted from the client. There is no PATCH/PUT for a
run's `status` anywhere in the API — status only ever changes via the
execution engine (see below).

## Task Execution Engine (Module 4)

A separate worker process (`python -m app.worker`, its own `worker` service
in `docker-compose.yml`) claims `pending` TaskRuns and executes them. It runs
independently of the API process — a worker crash or restart never affects
API availability.

**Claiming.** Workers claim work with `SELECT ... FOR UPDATE SKIP LOCKED`
(PostgreSQL-only — concurrent workers polling simultaneously never see a row
another transaction already locked, so two workers can never claim the same
run) inside a short, atomic transaction that also sets the run to `running`.
On SQLite (sandbox only) this degenerates to a plain `SELECT`; true claim-
concurrency safety can only be verified against real PostgreSQL.

**Lease tokens.** Every claim generates a fresh `lease_token` (a UUID, not a
stable worker identity). Heartbeats and completion calls must present the
*current* `lease_token` and `status='running'`, or they fail with no effect.
This is a fencing mechanism: if a worker's lease expires and the row is
reclaimed (by the reaper or another worker), that row now has a different
`lease_token`, so the original worker's late heartbeat or result can never
corrupt state it no longer owns. Enforced additionally at the database level
by the `ck_task_runs_lease_consistency` CHECK constraint (`running` rows
must always carry both `lease_token` and `lease_expires_at`; every other
status must carry neither).

**Idempotency.** Every TaskRun gets an `idempotency_key` (a UUID) generated
once at creation and never changed, including across retries of the same
row. Handlers must pass this value to any downstream system whose write they
perform, so a duplicate execution (e.g. a retry after a crash) cannot create
a duplicate downstream effect. It is exposed read-only on `TaskRunRead`.

**Retries.** On a retryable failure with attempts remaining, a run is
requeued to `pending` with `started_at`/`finished_at`/`error_message` reset
to `NULL` — Module 3's original CHECK constraints needed no changes at all.
`attempt_count` and `next_retry_at` (exponential backoff, capped) persist
across the requeue. `max_attempts` and `timeout_seconds` are configurable
per-`Task` (`null` = use the worker's global default) via the `Task` API.

**Timeouts and stuck-run recovery.** A worker heartbeats a claimed run every
`WORKER_HEARTBEAT_INTERVAL_SECONDS`, extending `lease_expires_at`. A
separate reaper loop recovers any `running` row whose lease expired without
a heartbeat (worker crash, lost connectivity) — reclaiming it exactly as a
worker-reported failure would (requeue if attempts remain, else terminate).

**Audit trail.** Every transition (claimed, heartbeat-driven requeue,
succeeded, failed, reaped) is appended to `task_run_events` — append-only,
never updated or deleted, with full untruncated error detail even after the
mutable `TaskRun` row has moved past that attempt. Read via `GET
/tasks/{id}/runs/{run_id}/events` (paginated).

**Credentials.** `DataSource.connection_metadata` still holds non-secret
parameters only (Module 3's rule, unchanged). Live credentials are set via
`PUT /data-sources/{id}/credentials` (write-only — no corresponding GET
anywhere) and stored encrypted (Fernet) in a dedicated
`data_source_credentials` table. Workers and handlers never talk to that
table directly — they depend only on the `CredentialProvider` interface
(`get_credentials(data_source) -> dict`, see `app/worker/credentials.py`).
The current implementation (`DatabaseCredentialProvider`) is explicitly an
MVP; migrating to a managed secrets service (Vault, AWS/GCP Secrets Manager)
means writing a new provider and swapping it in `app/worker/runner.py` — no
change to engine or handler code. Set `CREDENTIAL_ENCRYPTION_KEY` in
production (generate with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`);
the app refuses to store/read credentials without it when `APP_ENV=production`.

**Task types.** `sync`/`transform`/`export`/`other` (unchanged from Module
3) all currently map to a single diagnostic `NoOpHandler` — real connector
logic per task type is a scoped follow-up module. Adding one means writing a
new handler and registering it in `app/worker/handlers/__init__.py`; nothing
in the engine changes.

**Metrics.** `GET /internal/metrics` (superuser-only) exposes Prometheus-
format counters: `task_engine_tasks_claimed_total`,
`task_engine_tasks_completed_total`, `task_engine_tasks_failed_total`,
`task_engine_tasks_retried_total`, `task_engine_execution_duration_seconds`
(histogram), `task_engine_queue_depth` (gauge, pending-run count).

**Enum type ownership.** Every native PostgreSQL enum type in this project
(`source_type_enum`, `task_type_enum`, `task_run_status_enum`) is owned
exclusively by Alembic migrations — `CREATE TYPE`/`DROP TYPE` only ever
happens in `database/alembic/versions/`. The corresponding ORM model
`Enum(...)` objects set `create_type=False` explicitly, so
`Base.metadata.create_all()` never attempts to create these types itself.
This was fixed after real-PostgreSQL verification surfaced a
`DuplicateObject` error: the models originally left `create_type` at
SQLAlchemy's default (`True`), so `create_all()` (used by
`tests/conftest.py` against a live database) and Alembic's migrations were
both independently capable of creating the same type — whichever ran
first would "win," and the other would collide, with no relationship to
`alembic_version` tracking. `e8e9044941dd`'s own enum creation additionally
uses a PostgreSQL-native guarded `DO $$ ... EXCEPTION WHEN duplicate_object
...` block rather than relying solely on SQLAlchemy's `checkfirst`, so it
is unconditionally safe to re-run even if `alembic_version` and the
database's actual object state have drifted apart for any other reason.

**Known limitations.** `FOR UPDATE SKIP LOCKED` concurrency safety is
PostgreSQL-only and cannot be verified against the SQLite sandbox — treat
sandbox test results for claim-atomicity as indicative, not conclusive.
Application-layer Fernet encryption for credentials is weaker than a
dedicated secrets manager for key rotation/access auditing; that migration
is a recommended follow-up, not part of this module. There is no scheduler
yet — the unused `schedule` column on `Task` stays unused until a future
cron-driven module; runs are only created via `POST /tasks/{id}/runs`.

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
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key encrypting `DataSourceCredential` rows (Module 4). Required in production |
| `WORKER_ID` | Base identifier for this worker process (default `worker-1`) |
| `WORKER_CLAIM_BATCH_SIZE` | Max TaskRuns claimed per poll (default `5`) |
| `WORKER_POLL_INTERVAL_SECONDS` | Sleep between polls when nothing was claimed (default `5`) |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | Heartbeat frequency while executing (default `30`) |
| `WORKER_DEFAULT_TIMEOUT_SECONDS` | Default per-run execution timeout (default `300`) |
| `WORKER_DEFAULT_MAX_ATTEMPTS` | Default max retry attempts (default `3`) |
| `WORKER_RETRY_BASE_DELAY_SECONDS` / `WORKER_RETRY_MAX_DELAY_SECONDS` | Exponential backoff base/cap (default `30` / `900`) |
| `REAPER_POLL_INTERVAL_SECONDS` | How often the reaper scans for expired leases (default `15`) |

## License

Proprietary — all rights reserved.
