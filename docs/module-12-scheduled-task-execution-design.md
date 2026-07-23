# Module 12 — Scheduled Task Execution: Design

Status: implemented (branch `module-12-scheduled-task-execution`), pending independent code review.

## 1. Problem

`Task.schedule` has existed since Module 3 as an unread, unvalidated free-text column. Every `TaskRun` in the system, for every task type, has only ever been created by an explicit, authenticated `POST /tasks/{id}/runs` call. There is no automatic triggering mechanism anywhere. Module 12 closes this gap for `SYNC` tasks only.

## 2. Core architectural rule

The scheduler creates `TaskRun` rows and advances `next_run_at`. It never executes anything, never calls a handler, never auto-chains pipeline stages, never approves/rejects/rolls back, and never bypasses the existing claim/lease/execute engine. A scheduler-created run is claimed and executed by the unmodified `claim_batch`/`execute_one` path exactly like a manually-created one, distinguished only by `triggered_by IS NULL`.

## 3. Schedule format

A fixed recurring interval expressed as a positive integer of elapsed UTC seconds (`Task.schedule_interval_seconds`) — no cron, no wall-clock/timezone alignment, no daily-at-time or weekday syntax. This format is timezone-agnostic by construction (no DST edge cases), trivially validated, and trivially computed on both SQLite and PostgreSQL.

`Task.schedule` (the pre-existing free-text column) is **deprecated**: read/write for backward compatibility, never read by the scheduler, never parsed, never migrated into `schedule_interval_seconds`. Its Pydantic field description states this explicitly. Setting `schedule` alone never produces a `next_run_at`.

## 4. Database changes

Two additive, nullable columns on `tasks`:

- `schedule_interval_seconds INTEGER NULL` — presence means "this task recurs every N seconds."
- `next_run_at TIMESTAMPTZ NULL` — the next due instant, UTC. Always both-NULL or both-set with the column above (`ck_tasks_schedule_consistency`).

`ck_tasks_schedule_interval_hard_floor` (`>= 30` seconds) is a fixed, non-configurable database safety floor — deliberately independent of `Settings.minimum_schedule_interval_seconds` (the real, operator-facing, configurable minimum, default 60s, enforced in Pydantic on every write path). A `CHECK` constraint cannot read an environment variable at row-write time, so these two "30"s (the DB literal and the Settings field's `ge=` bound) are a deliberately hand-kept-in-sync pair, not a single derived source of truth.

A partial index, `ix_tasks_scheduled_due (next_run_at, id) WHERE schedule_interval_seconds IS NOT NULL AND is_active = true`, backs the scheduler's poll query. `id` is a deterministic secondary sort key preventing starvation among tasks that share an identical `next_run_at`.

No new table. No `schedule_timezone`, `schedule_enabled`, `last_scheduled_run_at`, `trigger_type`, or `scheduled_for` — each was considered and rejected as unnecessary given the chosen format, the existing `is_active`/`triggered_by` columns, and the transactional design below (see Section 7).

**Migration caveat (discovered and fixed while authoring the migration):** SQLite cannot `ALTER TABLE ADD/DROP CONSTRAINT`, so `op.batch_alter_table` recreates the whole `tasks` table under the hood there. That table-copy reflects the table first, and cannot reflect `ix_tasks_org_name_active` (an expression-based partial index) — confirmed directly against a real SQLite database while authoring the migration, this silently dropped that index and never recreated it. The migration now explicitly drops and recreates `ix_tasks_org_name_active` around each `batch_alter_table` block, on both dialects (a harmless no-op-equivalent extra step on PostgreSQL, where `batch_alter_table` never touches the index).

## 5. Configuration

Four new settings in `app/core/config.py`, all bounded, all startup-validated (no silent clamping):

| Setting | Default | Bounds |
|---|---|---|
| `SCHEDULER_POLL_INTERVAL_SECONDS` | 15.0 | 1.0–300.0 |
| `SCHEDULER_CLAIM_BATCH_SIZE` | 50 | > 0 |
| `MINIMUM_SCHEDULE_INTERVAL_SECONDS` | 60 | ≥ 30 (the hard DB floor) |
| `MAXIMUM_SCHEDULE_INTERVAL_SECONDS` | 2,592,000 (30 days) | > 0, and ≥ minimum (cross-field `model_validator`) |

`MINIMUM_SCHEDULE_INTERVAL_SECONDS`/`MAXIMUM_SCHEDULE_INTERVAL_SECONDS` are enforced in a Pydantic `field_validator` on `schedule_interval_seconds` in `app/schemas/task.py` (reading `get_settings()` at validation time, since a static `Field(ge=, le=)` cannot reflect a runtime-configurable bound), producing `422` on violation.

## 6. Scheduler algorithm and transaction design

`app/worker/scheduler.py::run_due_schedules(db, worker_id, batch_size)` runs a **bounded loop of independent, single-task transactions** — not one transaction for the whole batch. Each iteration:

1. `SELECT id, organization_id, schedule_interval_seconds, next_run_at FROM tasks WHERE schedule_interval_seconds IS NOT NULL AND is_active = true AND next_run_at <= now() AND id NOT IN (:excluded_this_pass) ORDER BY next_run_at ASC, id ASC LIMIT 1`, row-locked with `FOR UPDATE SKIP LOCKED` on PostgreSQL (plain `SELECT` on SQLite via the existing `_supports_skip_locked()` helper).
2. If nothing found: commit (no-op) and stop.
3. A guarded `UPDATE tasks SET next_run_at = :claim_time + interval WHERE id = :id AND next_run_at = :old_next_run_at AND is_active = true AND schedule_interval_seconds IS NOT NULL`.
4. If `rowcount != 1`: rollback, add the task to a pass-local, in-memory `excluded_ids` set, continue to the next candidate.
5. Otherwise, create one `TaskRun` via the shared factory (`triggered_by=None`, `source_task_run_id=None`) and commit — both the `next_run_at` advance and the `TaskRun` insert in the same transaction.

This was chosen over one whole-batch transaction (Revision 1's original design) specifically for fault isolation: a crash or per-task exception only ever rolls back that one task, never any other already-committed task in the same pass, and row locks are held only for one task's own critical section rather than the whole batch's processing time.

**Starvation avoidance:** a task whose guarded update loses its race (or whose processing raises) is added to `excluded_ids` and never re-selected for the remainder of that pass — one permanently malformed task costs at most one wasted batch slot, never the whole batch. No persistent state or new table is used; the set is discarded when the call returns.

**Missed-schedule policy:** `next_run_at` is always recomputed as `claim_time + interval`, anchored to the moment of claiming, never to the old (possibly long-stale) value. Any number of missed periods collapses into exactly one catch-up `TaskRun` per task per pass.

**Duplicate prevention proof:** the row lock prevents two concurrent passes from selecting the same task on PostgreSQL; the guarded `UPDATE`'s `WHERE next_run_at = :old_next_run_at` is an independent second check that still holds even without real locking (SQLite). Proven against genuinely concurrent PostgreSQL sessions in `tests/test_scheduled_tasks_concurrency.py`, not inferred from SQLite.

## 7. Shared TaskRun construction

`app/services/task_run_factory.py::create_task_run_record()` is a single, minimal function (not a broader service layer) used by both the manual API path (`app/api/tasks.py::create_task_run`, `triggered_by=current_user.id`) and the scheduler (`triggered_by=None`). It only constructs and `db.add()`s the row — no commit/flush, no business-rule validation (that stays in the API layer), no HTTP logic, no authentication.

`triggered_by IS NULL` is retained as the sole signal distinguishing scheduler-created from manually-created runs. `TaskRun.triggered_by` has `ON DELETE SET NULL`, which could in theory collide with a manually-triggered run whose user was later hard-deleted — but no endpoint anywhere in this codebase hard-deletes a `User` row (confirmed by direct search), so this ambiguity is real at the schema level but unreachable through the application. `trigger_type`/`scheduled_for`/`schedule_occurrence_key` were all considered and rejected as unnecessary given this and the transactional guarantee in Section 6.

## 8. Worker-loop integration

`app/worker/runner.py::run_forever()` gains a third periodic branch, gated by `scheduler_poll_interval_seconds` via the same `time.monotonic()` pattern already used for reaping. It runs **before** `claim_batch` (not after), so a `TaskRun` the scheduler creates in this iteration can be claimed by the very same iteration's `claim_batch` call, rather than waiting a full `worker_poll_interval_seconds` for the next loop. The call is wrapped in its own `try/except` so an isolated scheduling failure never crashes the whole worker process — claiming, executing, and reaping continue regardless.

## 9. Non-goals

Cron syntax; wall-clock/timezone-aligned scheduling; automatic pipeline chaining (scheduled TRANSFORM/STANDARDIZE/MATCH/EXPORT); notifications; reviewer roles; retention management; new source connectors; AI/ML behavior; task reactivation after soft-delete (no such endpoint exists in this codebase today — a future module adding one must recompute `next_run_at` from the reactivation moment, not revive a stale stored value).

## 10. Metrics and their known limitation

`task_scheduler_passes_total`, `task_scheduler_runs_created_total`, `task_scheduler_errors_total` (Counters), and `task_scheduler_last_success_timestamp_seconds` (Gauge, updates on empty-but-successful passes too — that's the signal that distinguishes "idle" from "dead") were added to `app/worker/metrics.py`'s existing dedicated registry.

These inherit a **pre-existing, not newly introduced** limitation: `app/api/internal.py`'s `/internal/metrics` endpoint runs in the FastAPI process and reads that process's own copy of `metrics.registry`; the worker loop (where these counters actually increment) runs as a separate OS process with its own independent copy. `tasks_claimed_total`/`tasks_completed_total`/`tasks_failed_total`/`tasks_retried_total` are already unreachable via `/internal/metrics` today for the same reason (only `queue_depth` is genuinely live there, since it's computed fresh from a DB query per request). Closing this process-boundary gap is out of scope for Module 12; until then, the structured log lines in `app/worker/scheduler.py` are the reliably observable operator signal.

## 11. Test coverage

`tests/test_scheduled_tasks_config.py` (13 tests) — settings defaults, bounds, cross-field validation, no silent clamping.
`tests/test_scheduled_tasks_api.py` (19 tests) — create/update semantics, explicit-null vs omitted `PATCH`, `Task.schedule` non-activation, interval bounds, task-type gating (including changing `task_type` away from `SYNC` while a schedule is already set), manual-run independence, soft-delete.
`tests/test_scheduled_tasks_scheduler.py` (16 tests) — due/not-due/inactive selection, `next_run_at` advancement, deterministic ordering, batch-size enforcement, starvation avoidance, missed-schedule catch-up, claimability by the existing worker, tenant isolation, metrics/rollback behavior.
`tests/test_scheduled_tasks_concurrency.py` (6 tests, PostgreSQL-only, skipped on SQLite) — genuine concurrent-worker duplicate prevention via real threads and separate sessions against real row locking, plus direct `pg_indexes`/`pg_constraint` existence checks and a hard-floor-constraint bypass proof.
