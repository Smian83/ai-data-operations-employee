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
│   │   │                          # task_run, task_run_event, data_source_credential,
│   │   │                          # data_profile (Module 5), cleaning_run,
│   │   │                          # cleaning_change (Module 6), enums
│   │   ├── schemas/               # Pydantic request/response schemas
│   │   ├── profiling/             # Module 5: pure CSV loading + profiling logic
│   │   │   ├── csv_loader.py      # bounded, read-only, path-traversal-safe
│   │   │   ├── csv_profiler.py    # deterministic, pure quality-metric calculation
│   │   │   └── types.py           # CsvLimits / LoadedCsv / ProfileResult
│   │   ├── cleaning/               # Module 6: pure deterministic cleaning rule engine
│   │   │   ├── rules.py            # trim/blank-normalize/type-coerce, pure functions
│   │   │   ├── engine.py           # clean() orchestration; CLEANING_ENGINE_VERSION
│   │   │   └── types.py            # CleaningLimits / Change / CleaningResult
│   │   └── worker/                # Module 4: task execution engine
│   │       ├── engine.py          # claim/heartbeat/complete (lease_token fencing)
│   │       ├── reaper.py          # stuck-run recovery (expired leases)
│   │       ├── credentials.py     # CredentialProvider abstraction
│   │       ├── metrics.py         # Prometheus counters/gauges/histogram
│   │       ├── runner.py          # worker process main loop
│   │       └── handlers/          # ExecutionHandler registry (SYNC -> CSV
│   │                              # profiling as of Module 5; TRANSFORM ->
│   │                              # CleaningHandler as of Module 6; others no-op)
│   ├── requirements.txt          # Production dependencies (pinned)
│   └── requirements-dev.txt      # + testing dependencies
├── frontend/                     # Reserved for a future module
├── database/                     # Migrations
│   ├── alembic.ini
│   └── alembic/
│       ├── env.py
│       └── versions/              # organizations+users, data_sources+tasks+task_runs,
│       │                          # task execution engine (Module 4),
│       │                          # data ingestion & profiling (Module 5),
│       │                          # data cleaning engine (Module 6)
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
│   ├── test_worker_api.py
│   ├── test_csv_loader.py
│   ├── test_csv_profiler.py
│   ├── test_csv_profiling_handler.py
│   ├── test_task_run_profile_api.py
│   ├── test_cleaning_rules.py
│   ├── test_cleaning_engine.py
│   ├── test_cleaning_handler.py
│   └── test_cleaning_api.py
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

## Data Ingestion & Profiling Engine (Module 5)

The first real (non-diagnostic) execution handler: `TaskType.SYNC` now maps
to `CsvProfilingHandler` instead of the Module 4 no-op — a deliberate
behavior change, not a purely additive one. A `SYNC` task now requires an
active `CSV_UPLOAD` data source and produces a real, immutable profiling
result; against any other `source_type` it fails permanently rather than
succeeding as a no-op. `TRANSFORM`, `EXPORT`, and `OTHER` are unaffected.

**Loading.** `app/profiling/csv_loader.py` reads a CSV strictly read-only,
bounded by `CSV_MAX_FILE_SIZE_BYTES` / `CSV_MAX_ROWS` / `CSV_MAX_COLUMNS` /
`CSV_MAX_CELL_LENGTH`. `DataSource.connection_metadata.file_path` is never
resolved against the shared `CSV_INPUT_ROOT` directly -- it is resolved
against that organization's own subdirectory,
`CSV_INPUT_ROOT/{organization_id}/` (`CsvProfilingHandler.execute` builds
this tenant root from `data_source.organization_id` before calling
`resolve_source_path`). This is a hard tenant-isolation boundary, not just
traversal protection: `resolve_source_path`'s existing absolute-path
rejection and `../`-escape check still apply unchanged, but they now bound
each org to its own slice of the filesystem, not the whole shared root --
one organization's `file_path` can never resolve to a file under a
different organization's directory, even if the filename is known or
guessed. CSV files must therefore be laid out per organization:

```
CSV_INPUT_ROOT/
  {organization_id}/
    customer-file.csv
  {another_organization_id}/
    their-file.csv
```

A `DataSource.connection_metadata.file_path` of `"customer-file.csv"`
belonging to `{organization_id}` resolves to
`CSV_INPUT_ROOT/{organization_id}/customer-file.csv` and nowhere else --
placing a file outside its matching organization's directory makes it
unreachable for that org, and placing it under a *different* org's
directory does not make it reachable by the first org, by design. Encoding
is detected (UTF-8, with a UTF-8 BOM check) and hashed (SHA-256 of the raw
bytes) before parsing; malformed rows are recorded as structural issues
rather than silently dropped or crashing the load.

**Profiling.** `app/profiling/csv_profiler.py` is a pure function: given a
loaded CSV and the same limits, it deterministically computes row/column
counts, duplicate rows, per-column missing-value and type-inference
statistics (bounded sample and distinct-value lists via
`CSV_MAX_DISTINCT_VALUES` / `CSV_MAX_SAMPLE_VALUES`), and structural issues
(blank/duplicate headers, ragged rows, inconsistent column types). It never
mutates its input and has no I/O of its own.

**Persistence.** One immutable `DataProfile` row per `TaskRun`, enforced by
`uq_data_profiles_task_run_id` at the database layer — the same
tenant-aware composite-FK pattern as every other Module 3/4 table
(`(organization_id, task_run_id/task_id/data_source_id)`, `RESTRICT` on the
task/data-source FKs, `CASCADE` only from the owning TaskRun/organization).
`CsvProfilingHandler` respects Module 4's idempotency contract without any
change to the `ExecutionContext` interface: it checks for an existing
profile by `task_run_id` first, and if a race loses to a concurrent insert
(`IntegrityError` on the unique constraint), it re-fetches and returns the
winner's profile rather than erroring — a retried run never produces two
profiles. Read via `GET /tasks/{id}/runs/{run_id}/profile` (404 if the run
isn't visible to the caller's org, or if no profile exists yet).

**Known limitations.** CSV only — `SourceType.POSTGRES`/`MYSQL`/`REST_API`/
`S3`/`OTHER` all still fail permanently under `SYNC`, per the explicit
`PermanentExecutionError` in `CsvProfilingHandler.execute`; real connectors
per source type are scoped follow-up work, same rationale as Module 4's
single-handler rollout. Type inference (`app/profiling/csv_profiler.py`) is
heuristic (boolean/integer/decimal/datetime/date/string, "mixed" when no
type reaches an 80% majority) and not configurable per column. Files are
read entirely into memory up to `CSV_MAX_FILE_SIZE_BYTES` (25 MB default) —
true streaming for larger files is not implemented.

## Data Cleaning Engine (Module 6)

The second real execution handler: `TaskType.TRANSFORM` now maps to
`CleaningHandler` instead of the Module 4 no-op (`SYNC`, `EXPORT`, and
`OTHER` are unaffected). A `TRANSFORM` `TaskRun` cleans the CSV already
profiled by a prior `SYNC` run -- it never invents its own input, and it
never writes to the source file.

**Requesting a cleaning run.** `POST /tasks/{id}/runs` accepts an optional
JSON body, `{"source_task_run_id": "<uuid>"}`. Omitting the body entirely
behaves exactly as before Module 6 (unaffected for `SYNC`/`EXPORT`/`OTHER`
tasks). For a `TRANSFORM` task the field is required -- it identifies which
prior `SYNC` run's `DataProfile` to clean -- and must reference a TaskRun
in the caller's own organization (`400` if missing, `404` if not found or
cross-org). Supplying it for a non-`TRANSFORM` task is rejected (`400`).

**Cleaning pipeline.** `app/cleaning/rules.py` and `app/cleaning/engine.py`
are pure, deterministic functions -- no I/O, no randomness -- applied in a
fixed order per cell: trim/collapse whitespace, normalize blank-equivalent
values (`"N/A"`, `"-"`, `"null"`, `"none"`, ...) to the empty string, then
coerce non-conforming values toward the column's `DataProfile`-reported
`inferred_type` (integer/decimal/boolean/date/datetime). Order matters --
later rules depend on earlier ones having already normalized their input.
Duplicate rows are flagged (via the same normalized-tuple comparison
`csv_profiler` already uses) but never auto-removed. Every applied change
carries a fixed, rule-specific confidence value; a run's overall
`confidence_score` is the *minimum* across its applied changes, not an
average, so one uncertain change pulls the reported confidence down rather
than being diluted by many trivial ones. `CLEANING_ENGINE_VERSION` (`"1.0"`)
is recorded on every `CleaningRun` so a future rule-set change never
leaves an existing run's provenance ambiguous.

**Output.** `CleaningHandler` re-reads the exact source file
`CsvProfilingHandler` already profiled (same tenant-scoped
`CSV_INPUT_ROOT/{organization_id}/` path) and writes the cleaned CSV to a
*separate*, also tenant-scoped root, `CSV_OUTPUT_ROOT/{organization_id}/`
-- the source file is never opened for writing anywhere in this module.
The cleaned output is hashed (SHA-256) and re-profiled via the existing,
unchanged `profile_csv`, giving a concrete before/after quality comparison
(row count, missing-value total, duplicate count) alongside the change log.

**Persistence and idempotency.** One immutable `CleaningRun` row per
cleaning `TaskRun` (`uq_cleaning_runs_task_run_id`), plus a bounded set of
per-cell `CleaningChange` rows (capped at `CLEANING_MAX_PERSISTED_CHANGES`,
default `10,000`) -- `CleaningRun.total_changes_count` and
`changes_by_rule` are always the true totals even when the individual rows
are capped, so nothing is silently lost from the aggregate. Retries are
idempotent via the same unique-constraint-plus-refetch pattern
`CsvProfilingHandler` already uses.

**Approval workflow.** Nothing produced by this module is treated as
authoritative without an explicit human decision. Every `CleaningRun`
starts `pending_review` and follows a small, fixed state machine:

- `POST /tasks/{id}/runs/{run_id}/cleaning/approve` — `pending_review` -> `approved`
- `POST /tasks/{id}/runs/{run_id}/cleaning/reject` — `pending_review` -> `rejected`
- `POST /tasks/{id}/runs/{run_id}/cleaning/rollback` — `approved` -> `rolled_back`

Any other starting status returns `409 Conflict`. Rollback is a pure status
transition, not a destructive operation: the output file and every
`CleaningChange` row are left untouched, since the source file was never
touched in the first place and the cleaned output lives at a separate
location from day one.

**Reading results.**
`GET /tasks/{id}/runs/{run_id}/cleaning` returns the run summary (counts,
confidence, output location/hash, approval status); `404` if the run isn't
visible to the caller's org or no cleaning result exists yet.
`GET /tasks/{id}/runs/{run_id}/cleaning/changes` returns the paginated
per-cell change log, same shape as the Module 4 task-run-events endpoint.

**Known limitations.** CSV only, inherited from Module 5's own scope --
non-`CSV_UPLOAD` sources fail permanently under `TRANSFORM`, same as
`SYNC`. AI-assisted correction is explicitly out of scope for this module
(a defined extension point, not built). Rollback is whole-run only; there
is no cell-level selective undo. Rolled-back and rejected runs leave their
output files on disk rather than deleting them, by design -- a storage
retention policy is deferred, not solved here.

## Health Endpoint

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
| `CSV_INPUT_ROOT` | Server-controlled root; each org is confined to `CSV_INPUT_ROOT/{organization_id}/` (default `./data/csv`) |
| `CSV_MAX_FILE_SIZE_BYTES` | Max CSV size read into memory (default `26214400`, 25 MB) |
| `CSV_MAX_ROWS` / `CSV_MAX_COLUMNS` / `CSV_MAX_CELL_LENGTH` | Bounds enforced during load (defaults `100000` / `500` / `100000`) |
| `CSV_MAX_DISTINCT_VALUES` / `CSV_MAX_SAMPLE_VALUES` | Per-column bounds on retained distinct/sample values in a profile (defaults `100` / `10`) |
| `CSV_OUTPUT_ROOT` | Server-controlled root for cleaned CSV output; each org confined to `CSV_OUTPUT_ROOT/{organization_id}/`, always distinct from `CSV_INPUT_ROOT` (default `./data/csv_cleaned`) |
| `CLEANING_MAX_PERSISTED_CHANGES` | Max `CleaningChange` rows persisted per cleaning run; the aggregate `total_changes_count` on `CleaningRun` is always accurate even when capped (default `10000`) |

## License

Proprietary — all rights reserved.
