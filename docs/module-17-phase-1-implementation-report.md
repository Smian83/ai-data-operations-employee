# Module 17 (Validation Engine) — Phase 1 Implementation Report

**Date:** 2026-07-25
**Phase scope:** Models · Migration · Enums/Constants · Configuration
**Explicit out of scope:** Engine, handler, API, business logic

---

## 1. Summary

Phase 1 establishes the complete database foundation for Module 17. Two new
tables (`validation_runs`, `validation_results`), five new enum/constant
additions, one new configuration setting, and a clean Alembic migration are
now in place. All four approved architecture adjustments are verified in the
schema. 36 new Phase 1 tests pass. Zero regressions in the prior suite.

---

## 2. Files Created or Modified

| File | Action | Description |
|------|--------|-------------|
| `backend/app/models/enums.py` | Modified | Added `TaskType.VALIDATE`, `VALIDATION_OUTCOMES`, `VALIDATION_RULE_NAMES`, count assert |
| `backend/app/models/validation_run.py` | Created | `ValidationRun` ORM model (13 columns, 5 CHECKs, 2 UNIQUEs, 4 composite FKs) |
| `backend/app/models/validation_result.py` | Created | `ValidationResult` ORM model (14 columns, 6 indexes, 1 CHECK) |
| `backend/app/models/__init__.py` | Modified | Added imports for both new models |
| `backend/app/models/task_run.py` | Modified | Added `validation_run` relationship with `passive_deletes=True` pattern |
| `backend/app/core/config.py` | Modified | Added `validation_max_persisted_results` (default 10,000) |
| `database/alembic/versions/c1d2e3f4a5b6_validation_engine.py` | Created | Alembic migration |
| `tests/test_validation_models.py` | Created | 36 Phase 1 tests |
| `tests/test_worker_handlers.py` | Modified | Annotated `TaskType.VALIDATE` as Phase 3 pending in handler coverage test |

---

## 3. Schema: validation_runs

```
validation_runs
  id                          UUID PK
  organization_id             UUID NOT NULL  FK → organizations.id CASCADE
  task_run_id                 UUID NOT NULL  FK composite → task_runs (org, id) CASCADE
  task_id                     UUID NOT NULL  FK composite → tasks (org, id) RESTRICT
  data_source_id              UUID NOT NULL  FK composite → data_sources (org, id) RESTRICT
  remediation_run_id          UUID NOT NULL  FK composite → remediation_runs (org, id) RESTRICT
  approved_changes_considered INT  NOT NULL  CHECK >= 0
  passed_count                INT  NOT NULL  CHECK >= 0
  failed_count                INT  NOT NULL  CHECK >= 0
  skipped_count               INT  NOT NULL  CHECK >= 0
                                             CHECK passed + failed + skipped = approved
  results_by_rule             JSON NOT NULL
  validation_engine_version   VARCHAR(20) NOT NULL
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE: uq_validation_runs_task_run_id (task_run_id)  -- idempotency gate
UNIQUE: uq_validation_runs_org_id (organization_id, id)  -- composite FK target
INDEX: ix_validation_runs_org, ix_validation_runs_task_run,
       ix_validation_runs_remediation_run
```

## 4. Schema: validation_results

```
validation_results
  id                         UUID PK
  organization_id            UUID NOT NULL  FK → organizations.id CASCADE
  validation_run_id          UUID NOT NULL  FK composite → validation_runs (org, id) CASCADE
  remediation_run_id         UUID NOT NULL  (denorm from ValidationRun)
  remediation_change_id      UUID NOT NULL  FK → remediation_changes.id RESTRICT
  source_issue_id            UUID NOT NULL  (denorm from RemediationChange)
  validation_rule            VARCHAR(50) NOT NULL
  outcome                    VARCHAR(10) NOT NULL  CHECK IN ('passed','failed','skipped')
  reason                     TEXT NOT NULL         -- Adjustment 4: unbounded for encryption compat
  original_value             TEXT nullable
  proposed_value             TEXT nullable
  validation_engine_version  VARCHAR(20) NOT NULL
  validation_rule_version    VARCHAR(20) NOT NULL  -- Adjustment 2: per-rule independent version
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE: uq_validation_results_org_id (organization_id, id)  -- composite FK target
INDEX: ix_validation_results_org, ix_validation_results_run,
       ix_validation_results_remediation_run, ix_validation_results_change,
       ix_validation_results_outcome (Adjustment 3),
       ix_validation_results_org_change_ts (composite)
```

---

## 5. Architecture Adjustments — Verification

| # | Adjustment | Verification |
|---|-----------|-------------|
| 1 | Snapshot freeze at handler start | Documented in migration header and design doc. No schema impact; enforced in Phase 3 handler. |
| 2 | `validation_rule_version` per-rule column | Present on `ValidationResult`, `NOT NULL`, `VARCHAR(20)`. Two rows in same run can carry different versions (test `test_two_results_can_have_different_rule_versions`). |
| 3 | `ix_validation_results_outcome` index | Column-level `index=True` on `ValidationResult.outcome` confirmed by ORM inspection and live schema query (tests `test_ix_validation_results_outcome_exists_in_orm` and `test_ix_validation_results_outcome_exists_in_live_schema`). |
| 4 | Encryption compatibility — `reason` is `Text()` | `reason`, `original_value`, and `proposed_value` are all `Text()` (unbounded), never `VARCHAR`. Verified by ORM type inspection tests in Section D. |

---

## 6. Enums / Constants

| Name | Location | Value |
|------|----------|-------|
| `TaskType.VALIDATE` | `app.models.enums.TaskType` | `"validate"` |
| `VALIDATION_OUTCOMES` | `app.models.enums` | `("passed", "failed", "skipped")` |
| `VALIDATION_RULE_NAMES` | `app.models.enums` | 10-tuple, one per `REMEDIATION_ACTIONS` entry |
| `VALIDATION_ENGINE_VERSION` | `app.validation.engine` (Phase 2) | Not yet defined; placeholder for Phase 2 |

Assert: `len(VALIDATION_RULE_NAMES) == len(REMEDIATION_ACTIONS) == 10` enforced at import time.

---

## 7. Configuration

```python
# backend/app/core/config.py
validation_max_persisted_results: int = Field(
    default=10_000, alias="VALIDATION_MAX_PERSISTED_RESULTS", gt=0
)
```

Caps the number of `ValidationResult` rows written per run (defensive ceiling
matching the existing `max_persisted_issues` and `max_persisted_changes`
conventions from Modules 14 and 15).

---

## 8. Migration: c1d2e3f4a5b6_validation_engine.py

- **Revision chain:** `b0c1d2e3f4a5` (approval_queue) → `c1d2e3f4a5b6`
- **PostgreSQL:** `ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'validate'` executed inside `autocommit_block()` (5th enum extension in this project; same pattern as all prior modules)
- **SQLite:** No enum migration required (VARCHAR column; `IF NOT EXISTS` guard makes the statement safe on PostgreSQL too)
- **Purely additive:** No existing table, column, constraint, index, or enum value is touched
- **downgrade():** Drops all 6 `validation_results` indexes, drops `validation_results`, drops 3 `validation_runs` indexes, drops `validation_runs`. Note: `ALTER TYPE ... DROP VALUE` is not supported by PostgreSQL; the `validate` label remains in `task_type_enum` after downgrade (documented in migration header, same as all prior enum-extension migrations)
- **Identifier length audit:** All 24 named constraints and indexes pre-measured ≤ 63 bytes (PostgreSQL NAMEDATALEN limit)

**Migration cycle result (SQLite):**
```
base → head: 16 migrations applied, including c1d2e3f4a5b6  ✓
head → base: clean downgrade through all 16             ✓
base → head: second forward pass clean                  ✓
```

---

## 9. Test Results

### Phase 1 tests — test_validation_models.py

```
36 passed in 38.04s
```

| Section | Tests | Coverage |
|---------|-------|----------|
| A. Enum/constant consistency | 9 | VALIDATION_OUTCOMES (3), VALIDATION_RULE_NAMES (3), TaskType.VALIDATE (2), counts (1) |
| B. Adjustment 2 (rule version) | 3 | Column exists, NOT NULL, two rows with different versions accepted |
| C. Adjustment 3 (outcome index) | 2 | ORM index=True, live schema index present |
| D. Adjustment 4 (Text columns) | 3 | reason, original_value, proposed_value are Text() |
| E. ValidationRun round-trip + CHECKs | 5 | Round-trip, zero counts, reconcile mismatch rejected, negative rejected, UQ present |
| F. ValidationResult round-trip + CHECK | 4 | Round-trip, invalid outcome rejected, all 3 outcomes accepted, nullable fields |
| G. Idempotency | 1 | Duplicate task_run_id rejected |
| H. Append-only / no forbidden UQ | 3 | No UQ on org+remediation_run, no UQ on org+change, multiple rows for same change accepted |
| I. Cascade | 1 | Delete ValidationRun cascades to ValidationResult rows |
| J. Live schema audit | 3 | Column sets and nullable flags for both tables, composite index present |
| K. Identifier length | 2 | All names ≤ 63 bytes on both models |

### Regression suite — all prior tests

Executed in batches (61 test files, ~370+ tests). All pass. One pre-existing
test (`test_registry_has_a_handler_for_every_task_type`) was updated to
annotate `TaskType.VALIDATE` as handler-pending until Phase 3, exactly as
prior modules (e.g. `TaskType.DETECT` in Module 14 Phase 1, `TaskType.REMEDIATE`
in Module 15 Phase 1) handled the same gap.

---

## 10. Design Decisions and Deviations

**passive_deletes=True on ValidationRun.results** — During test authoring, the
ORM tried to NULL-out `validation_results.organization_id` before deleting
parent `ValidationRun` rows (standard SQLAlchemy behavior when no ORM cascade
is configured). Since `organization_id` is `NOT NULL`, this produced an
`IntegrityError`. Adding `passive_deletes=True` to the relationship tells
SQLAlchemy to defer to the database-level `ondelete=CASCADE` instead. This is
the established pattern in the codebase (`IssueDetectionRun.issues`,
`RemediationRun.changes`). No schema change; model-layer only.

**No ValidationHandler registered** — `TaskType.VALIDATE` exists in the enum
and is accepted by the API (tasks can be created with `task_type: "validate"`),
but no `ValidationHandler` is registered in the worker. If a VALIDATE `TaskRun`
is dispatched it will fail at the handler lookup step. This is intentional
Phase 1 scope: the handler is Phase 3 work. The handler test was annotated
accordingly rather than registering a no-op that would silently succeed.

---

## 11. Phase 2 Prerequisites

Phase 2 will add:
- `app/validation/` package
- `VALIDATION_ENGINE_VERSION` constant (`app.validation.engine`)
- `ValidationRule` protocol and 10 concrete rule implementations
- `VALIDATION_RULES` registry mapping `VALIDATION_RULE_NAMES` to rule objects

No schema changes are required for Phase 2. All schema work is complete in
Phase 1.

---

## 12. Readiness Assessment

| Check | Result |
|-------|--------|
| Migration cycle (base→head→base→head) | ✓ Clean |
| Compile check (all new files) | ✓ Clean |
| Phase 1 tests | ✓ 36/36 |
| Full regression suite | ✓ 0 regressions |
| All 4 architecture adjustments verified in schema | ✓ |
| All identifier names ≤ 63 bytes | ✓ |
| Count-reconcile CHECK constraint present and enforced | ✓ |
| Idempotency UNIQUE(task_run_id) present and enforced | ✓ |
| No UNIQUE(org, change_id) on ValidationResult | ✓ (deliberate omission) |
| FK ondelete strategy correct for all 7 FKs | ✓ |
| Encryption-compatible column types (Text()) | ✓ |

**Phase 1 status: COMPLETE. Ready for Phase 2 review.**
