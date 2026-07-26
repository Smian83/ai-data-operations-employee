# Module 17 — Validation Engine: Architecture Design

**Status:** Architecture approved. Four required adjustments incorporated (see section 0). Phase 1 in progress.  
**Upstream:** Module 16 (Approval Queue) — `remediation_change_decisions` table, `RemediationChange` rows.  
**Alembic current head:** `b0c1d2e3f4a5` (approval_queue).  
**Proposed next revision ID:** `c1d2e3f4a5b6` (validation_engine).

---

## 0. Required Adjustments (post-approval, incorporated before any code)

### Adjustment 1 — Approved snapshot frozen at handler start

When a `VALIDATE` `TaskRun` begins execution, `ValidationHandler` resolves the set of approved `RemediationChange` IDs **once** — at the very start of `execute()`, before any validation logic runs — and treats that set as immutable for the lifetime of the run. Any `RemediationChangeDecision` rows inserted or superseded *after* this snapshot moment are invisible to the running handler, even if execution takes many seconds.

Concrete implementation rule: the handler performs its "fetch approved change IDs" queries, builds a frozen Python `frozenset[uuid.UUID]` of approved change IDs, and passes only those IDs to the validation engine. The engine never re-queries `remediation_change_decisions`. No decision table read occurs after the snapshot.

This is documented behavior, not a defect. `ValidationRun.approved_changes_considered` records the exact count from the frozen snapshot, making the snapshot's contents independently auditable: any discrepancy between the snapshot count and a later count of approved decisions is explainable by decisions made after the snapshot moment.

**Idempotency interaction:** The snapshot-then-validate sequence is inside the idempotency short-circuit. A second execution of the same `TaskRun` skips the snapshot entirely and returns the existing `ValidationRun` — the snapshot is never taken twice for the same run.

### Adjustment 2 — `validation_rule_version` on every `ValidationResult`

Every `ValidationResult` row carries **two** version fields:

- `validation_engine_version` (`String(20)`, NOT NULL) — the version of `app.validation.engine` in effect when the run executed. Same single constant as every prior module's engine version column.
- `validation_rule_version` (`String(20)`, NOT NULL) — the version of the **specific rule** that evaluated this change. Each `ValidationRule` implementation declares its own `rule_version: str` constant (e.g. `"1.0"`), independent of the engine version. If a rule's logic is updated (e.g. a stricter phone-format check) its `rule_version` is bumped independently, without bumping the engine version. This makes per-rule changes fully auditable at the result row level — a client can distinguish "this result came from an old version of `validate_normalize_phone`" vs. "this result came from a new version" without comparing engine versions.

Both columns are on `ValidationResult`. `ValidationRun` carries only `validation_engine_version` (the summary-level constant); per-rule versioning lives on the detail rows.

### Adjustment 3 — `ix_validation_results_outcome` index

An additional index on `validation_results.outcome` is added to the schema (and migration):

```
ix_validation_results_outcome  ON  validation_results(outcome)
```

This optimizes the most common API filter (`?outcome=failed`, `?outcome=passed`, `?outcome=skipped`), which will be the primary way operators inspect validation results. Combined with the existing `ix_validation_results_run` index on `validation_run_id`, the planner can use both to evaluate a filtered page query efficiently.

Total indexes on `validation_results`: 6 (org, run, remediation_run, change, outcome, composite org+change+created_at).

### Adjustment 4 — Encryption compatibility documented

The following `ValidationResult` columns are `Text()` (unbounded, not `VARCHAR`) specifically to accommodate future field-level encryption without requiring a schema migration:

- `reason` — fixed-vocabulary outcome reason; future encryption ciphertext length exceeds any fixed VARCHAR.
- Any future `comment` or `metadata` field added to `ValidationResult` must also use `Text()` for the same reason.

This mirrors the `RemediationChangeDecision.comment` precedent from Module 16. No encryption is implemented in Module 17. The schema is compatible: a future module that adds AES-GCM or Fernet encryption to audit fields can do so by changing only application-layer code, never a migration.

---

## 1. Architecture

### Position in the pipeline

```
Module 14 DETECT     →  IssueDetectionRun / Issue
Module 15 REMEDIATE  →  RemediationRun / RemediationChange (proposals)
Module 16 APPROVE    →  RemediationChangeDecision (approved | rejected)
Module 17 VALIDATE ──┘  ValidationRun / ValidationResult (passed | failed | skipped)
```

Module 17 sits **after** Module 16. Its input is the set of `RemediationChange` rows that have been **approved** (i.e., have at least one `RemediationChangeDecision` with `decision='approved'`). Its output is a set of `ValidationResult` rows — one per approved change — recording whether the proposed value satisfies the same deterministic rule that proposed it.

Module 17 is **strictly read-only** with respect to every upstream table. It never modifies any `RemediationChange`, `RemediationChangeDecision`, `Issue`, or source file. It never auto-approves or auto-rejects anything. It writes exactly two things: one `ValidationRun` summary row and N `ValidationResult` detail rows, both append-only.

### Structural siblings

| Module 17 concept | Closest prior sibling |
|-------------------|-----------------------|
| `ValidationRun` | `IssueDetectionRun`, `RemediationRun` — one per TaskRun, no approval state machine |
| `ValidationResult` | `Issue`, `RemediationChange` — append-only child detail rows |
| `ValidationHandler` | `IssueDetectionHandler`, `RemediationHandler` — the sole impure layer |
| `app.validation.*` | `app.detection.*`, `app.remediation.*` — pure, no I/O |
| `TaskType.VALIDATE` | `TaskType.DETECT`, `TaskType.REMEDIATE` |

Module 17 has **no approval state machine** — same reasoning as Modules 14 and 15: the engine never writes or modifies source data, so there is nothing for a human to approve, reject, or roll back. `ValidationRun` is a read-only audit artifact, like `IssueDetectionRun`.

### Additive-only rule

No existing model, migration, API endpoint, or test is modified. Endpoints in `app/api/tasks.py` are extended (new read-only GET endpoints added); no prior endpoint is changed. `app/models/enums.py` gains new constants in the established plain-tuple pattern.

---

## 2. Data Model

### 2a. `ValidationRun` (one per VALIDATE TaskRun)

**Table:** `validation_runs`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `Uuid` PK | `uuid4()` |
| `organization_id` | `Uuid` NOT NULL | FK `organizations.id` CASCADE; also composite FK target via `UNIQUE(org, id)` |
| `task_run_id` | `Uuid` NOT NULL | Composite FK `(org, task_run_id)` → `task_runs(org, id)` CASCADE; `UNIQUE(task_run_id)` for idempotency |
| `task_id` | `Uuid` NOT NULL | Composite FK `(org, task_id)` → `tasks(org, id)` RESTRICT |
| `data_source_id` | `Uuid` NOT NULL | Composite FK `(org, data_source_id)` → `data_sources(org, id)` RESTRICT |
| `remediation_run_id` | `Uuid` NOT NULL | Composite FK `(org, remediation_run_id)` → `remediation_runs(org, id)` RESTRICT — the specific `RemediationRun` whose approved changes this run validated |
| `approved_changes_considered` | `Integer` NOT NULL | How many approved `RemediationChange` rows were fetched for this run (bounded by `ValidationLimits.max_persisted_results`) |
| `passed_count` | `Integer` NOT NULL | Count of `ValidationResult` rows with `outcome='passed'` |
| `failed_count` | `Integer` NOT NULL | Count of `ValidationResult` rows with `outcome='failed'` |
| `skipped_count` | `Integer` NOT NULL | Count of `ValidationResult` rows with `outcome='skipped'` |
| `results_by_rule` | `JSON` NOT NULL | `{validation_rule: count}` — only rules with at least one result; same pattern as `RemediationRun.changes_by_action` |
| `validation_engine_version` | `String(20)` NOT NULL | `VALIDATION_ENGINE_VERSION` constant from `app.validation.engine` |
| `created_at` | `DateTime(tz=True)` NOT NULL | `server_default=func.now()` |

**Check constraints:**
- `approved_changes_considered >= 0`
- `passed_count >= 0`
- `failed_count >= 0`
- `skipped_count >= 0`
- `passed_count + failed_count + skipped_count = approved_changes_considered`

**Unique constraints:**
- `UNIQUE(task_run_id)` — idempotency gate (same as `IssueDetectionRun`, `RemediationRun`)
- `UNIQUE(organization_id, id)` — required so `ValidationResult` can reference via composite FK

**Indexes:**
- `ix_validation_runs_org` on `organization_id`
- `ix_validation_runs_task_run` on `task_run_id`
- `ix_validation_runs_remediation_run` on `remediation_run_id`

**No `processing_duration_ms` column** — same decision as `RemediationRun`: this is derived at the API layer from `TaskRun.started_at`/`finished_at`, never stored.

---

### 2b. `ValidationResult` (one per validated `RemediationChange`)

**Table:** `validation_results`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `Uuid` PK | `uuid4()` |
| `organization_id` | `Uuid` NOT NULL | FK `organizations.id` CASCADE |
| `validation_run_id` | `Uuid` NOT NULL | Composite FK `(org, validation_run_id)` → `validation_runs(org, id)` CASCADE |
| `remediation_run_id` | `Uuid` NOT NULL | Denormalized from `RemediationChange` — same "efficient run-scoped queries without extra join" rationale `RemediationChangeDecision` uses for the same field |
| `remediation_change_id` | `Uuid` NOT NULL | FK `remediation_changes.id` RESTRICT |
| `source_issue_id` | `Uuid` NOT NULL | Denormalized from `RemediationChange.source_issue_id` — required field per specification; avoids a JOIN for every API result row |
| `validation_rule` | `String(50)` NOT NULL | Name of the `ValidationRule` that ran (e.g. `'validate_trim_whitespace'`) |
| `outcome` | `String(10)` NOT NULL | `'passed'`, `'failed'`, or `'skipped'` — CHECK constraint |
| `reason` | `Text` NOT NULL | Human-readable explanation; always a constant from `app.validation.reasons` — never dynamically built at call sites |
| `original_value` | `Text` nullable | From `RemediationChange.original_value`; nullable because remove-duplicate actions have no single original value |
| `proposed_value` | `Text` nullable | From `RemediationChange.proposed_value`; nullable for the same reason |
| `validation_engine_version` | `String(20)` NOT NULL | `VALIDATION_ENGINE_VERSION` constant — denormalized onto every row for independent auditability without a join to `ValidationRun` |
| `validation_rule_version` | `String(20)` NOT NULL | The specific `ValidationRule.rule_version` constant for the rule that evaluated this change — independent of engine version; bumped per-rule when rule logic changes (Adjustment 2) |
| `created_at` | `DateTime(tz=True)` NOT NULL | `server_default=func.now()` |

**Check constraints:**
- `outcome IN ('passed', 'failed', 'skipped')` — cross-checked at import time against `VALIDATION_OUTCOMES` in `app.models.enums`

**Unique constraints:**
- `UNIQUE(organization_id, id)` — future composite FK target
- **No** `UNIQUE(organization_id, remediation_change_id)` — same deliberate omission as `RemediationChangeDecision`: a future re-validation pass creates new rows without erasing history

**Indexes:**
- `ix_validation_results_org` on `organization_id`
- `ix_validation_results_run` on `validation_run_id`
- `ix_validation_results_remediation_run` on `remediation_run_id`
- `ix_validation_results_change` on `remediation_change_id`
- `ix_validation_results_outcome` on `outcome` — Adjustment 3; optimizes the most common API filter (`?outcome=failed/passed/skipped`)
- Composite `ix_validation_results_org_change_ts` on `(organization_id, remediation_change_id, created_at)` — supports "latest result per change" queries if a future re-validation pass is ever added

**Why `reason` is `Text()` (unbounded):** Adjustment 4 — encryption compatibility. Future field-level encryption of `reason` (or any future `comment`/`metadata` field on this table) must not require a schema change. Any future `Text` field added here must also be `Text()`, not `VARCHAR`. No encryption is implemented in Module 17.

---

### 2c. New enum constants (additive)

In `app/models/enums.py` (plain-tuple pattern, never a native Postgres `ENUM` type):

```python
# Module 17: validation engine
VALIDATION_OUTCOMES = ("passed", "failed", "skipped")

# The closed vocabulary of named validation rules, one per REMEDIATION_ACTION.
# Cross-checked at import time against the engine's VALIDATION_RULES registry.
VALIDATION_RULE_NAMES = (
    "validate_trim_whitespace",
    "validate_collapse_multiple_spaces",
    "validate_standardize_capitalization",
    "validate_normalize_boolean",
    "validate_normalize_date",
    "validate_normalize_phone",
    "validate_normalize_numeric",
    "validate_normalize_enum_value",
    "validate_remove_duplicate_row",
    "validate_remove_duplicate_primary_key",
)
```

### 2d. New `TaskType.VALIDATE`

```python
# Module 17: another new value, same reasoning as DETECT/REMEDIATE.
VALIDATE = "validate"
```

This requires a new `ALTER TYPE task_type_enum ADD VALUE 'validate'` migration step (same as Modules 7/8/14/15), or — on SQLite — handled via the batch-alter pattern Module 12 documented.

---

## 3. Validation Lifecycle

```
[VALIDATE TaskRun created by operator]
         │
         ▼
ValidationHandler.execute(context)
         │
  ┌──────┴──────────────────────────────────────────────────────────────────┐
  │ Step 1: Resolve RemediationRun via source_task_run_id                   │
  │   context.task_run.source_task_run_id → TaskRun (org-scoped) →          │
  │   RemediationRun (org-scoped, unique per task_run)                      │
  │   Missing → PermanentExecutionError                                     │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ Step 2: Idempotency short-circuit                                        │
  │   SELECT ValidationRun WHERE task_run_id = current_task_run.id          │
  │   EXISTS → return existing summary (no re-read, no re-validate)          │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ Step 3: Snapshot approved RemediationChange IDs (Adjustment 1)          │
  │   Fetch all RemediationChangeDecision rows for this run (one batch      │
  │   query). Resolve latest decision per change in Python (first-win       │
  │   DESC). Build a FROZEN set of approved change IDs. This snapshot       │
  │   is immutable for the rest of execute() — no re-query of              │
  │   remediation_change_decisions ever occurs after this point.            │
  │   Fetch the actual RemediationChange rows for snapshot IDs only         │
  │   (second batch query).                                                 │
  │   Ordered: (row_number ASC, id ASC) — stable                           │
  │   Capped at settings.validation_max_persisted_results (defensive)       │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ Step 4: Call pure validation engine (no I/O)                            │
  │   validate(approved_changes) → ValidationRunResult                      │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ Step 5: Persist in one transaction                                       │
  │   INSERT ValidationRun                                                   │
  │   db.add_all([ValidationResult, ...]) — batch, no N+1                   │
  │   db.commit()                                                            │
  │   IntegrityError on dup task_run_id → SELECT and return existing        │
  └──────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  TaskRun.status = SUCCESS
```

**No upstream status gate.** The `RemediationRun` has no approval status (same as `IssueDetectionRun`) — there is nothing to check. The gate is structural: the handler only fetches approved `RemediationChange` rows (those with a `RemediationChangeDecision.decision='approved'`). If none exist, the run succeeds with `approved_changes_considered=0`, `passed_count=0`, `failed_count=0`, `skipped_count=0` — not an error.

**What "latest decision" means in Step 3:** Consistent with Module 16's "first-win by `decision_timestamp DESC` in Python" pattern, the handler batch-loads all decisions for the run in one query and resolves the latest per change in Python, avoiding `DISTINCT ON` (PostgreSQL-only). A change whose latest decision is `'approved'` is included; `'rejected'` or no decision is excluded.

---

## 4. Validation Rule Registry

### `ValidationRule` protocol (mirrors `RemediationRule`)

```python
class ValidationRule(Protocol):
    action: str              # must match one of REMEDIATION_ACTIONS exactly
    rule_name: str           # must match one of VALIDATION_RULE_NAMES exactly
    rule_version: str        # e.g. "1.0" — bumped independently per rule (Adjustment 2)

    def validate(
        self,
        change: ValidationChangeInput,
    ) -> ValidationOutcome:
        """Pure function. No I/O. Deterministic. Same input → same output, always."""
        ...
```

**`ValidationChangeInput`** (pure dataclass, no ORM):
```python
@dataclass(frozen=True)
class ValidationChangeInput:
    remediation_change_id: uuid.UUID
    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    action: str
    original_value: str | None
    proposed_value: str | None
    issue_type: str         # denormalized from Issue via join in handler
    issue_original_value: str | None  # from Issue.original_value
    suggested_fix: str | None         # from Issue.suggested_fix
```

**`ValidationOutcome`** (pure dataclass):
```python
@dataclass(frozen=True)
class ValidationOutcome:
    outcome: str          # 'passed' | 'failed' | 'skipped'
    reason: str           # always a constant from app.validation.reasons
```

### Registry

`app/validation/registry.py::VALIDATION_RULES: tuple[ValidationRule, ...]` — one rule per `REMEDIATION_ACTION`, same declaration-order convention as `REMEDIATION_RULES`. The engine dispatches by `action`, so `_RULES_BY_ACTION` is an O(1) lookup dict (same as `_RULES_BY_ISSUE_TYPE` in `app.remediation.registry`).

```python
VALIDATION_RULES: tuple[ValidationRule, ...] = (
    ValidateTrimWhitespaceRule(),
    ValidateCollapseMultipleSpacesRule(),
    ValidateStandardizeCapitalizationRule(),
    ValidateNormalizeBooleanRule(),
    ValidateNormalizeDateRule(),
    ValidateNormalizePhoneRule(),
    ValidateNormalizeNumericRule(),
    ValidateNormalizeEnumValueRule(),
    ValidateRemoveDuplicateRowRule(),
    ValidateRemoveDuplicatePrimaryKeyRule(),
)
assert len(VALIDATION_RULES) == 10
```

**Integrity assertions** (same pattern as `app.remediation.registry`):
- All `rule_name` values are unique.
- All `action` values are unique.
- Every `rule.action` is in `REMEDIATION_ACTIONS`.
- Every `rule.rule_name` is in `VALIDATION_RULE_NAMES`.

### What each rule checks

Every rule answers one question: **"Does `proposed_value` satisfy what the remediation action intended?"** Rules never re-run the full remediation engine; they apply only the specific predicate appropriate for each action.

| Action | Validation logic |
|--------|-----------------|
| `trim_whitespace` | `proposed_value == original_value.strip()` (or `== None` if original was None) |
| `collapse_multiple_spaces` | No leading/trailing ws in `proposed_value`; no `'  '` sequence; non-empty |
| `standardize_capitalization` | `proposed_value` matches configured capitalization target (import + reuse `app.cleaning`/`app.standardization` casing function) |
| `normalize_boolean` | `proposed_value` is a recognized canonical boolean string (reuse `app.standardization.rules.values.standardize_boolean`) |
| `normalize_date` | `proposed_value` parses under `target_date_format` and is non-empty |
| `normalize_phone` | `proposed_value` is valid E.164 format (`+` prefix + digits only, 7–15 digits) |
| `normalize_numeric` | `proposed_value` parses as a valid numeric (reuse `app.standardization`'s canonical numeric check) |
| `normalize_enum_value` | `proposed_value` is in the configured `allowed_values` set (exact, not case-insensitive — the remediation rule already resolved casing) |
| `remove_duplicate_row` | `proposed_value is None` AND `original_value is not None` (proposal is a row exclusion, not a replacement) |
| `remove_duplicate_primary_key` | Same as `remove_duplicate_row` |

**Skip condition:** A rule returns `outcome='skipped'` when it cannot evaluate — e.g., the original or proposed value is `None` in a context where it is required for comparison, or a required config (capitalization target, allowed values, date format) is absent from the `ValidationChangeInput`. A skip is not a failure; it means the validator had insufficient context, not that the proposed value is wrong.

**No configuration tables.** Module 17 validates against the *proposed value itself* — it does not re-read `RemediationColumnRule` or `RemediationDatasetConfig`. The validation question ("does the proposed value satisfy the rule?") is answerable from `proposed_value` + `action` alone for most rules. The two exceptions (capitalization target, allowed values) require config that Module 17 will denormalize into `ValidationChangeInput` during the handler's fetch step — read from the same config tables Module 15 already uses, never duplicated.

---

## 5. API Design

All 2 new endpoints are mounted under the existing `tasks` router in `app/api/tasks.py`. No new router file. No `main.py` changes.

**URL pattern:** `/tasks/{task_id}/runs/{run_id}/validation`

The 404 chain follows the established four-layer pattern:
`task → task_run → validation_run` (no per-result ID in the URL for the summary endpoint; per-result detail uses pagination).

### Endpoint 1 — Summary

```
GET /tasks/{task_id}/runs/{run_id}/validation
```

- **Auth:** `get_current_active_user` (active user, not superuser-gated — read-only)
- **Response:** `ValidationRunRead` (HTTP 200) — the precomputed `ValidationRun` summary row, plus `processing_duration_ms` derived from `TaskRun.started_at`/`finished_at`
- **404 chain:** task → task_run → validation_run (all scoped to `current_user.organization_id`)
- **No aggregation:** every count was precomputed by the handler at persist time; this endpoint loads one row from `validation_runs`, zero from `validation_results`

### Endpoint 2 — Paginated results

```
GET /tasks/{task_id}/runs/{run_id}/validation/results
```

- **Auth:** `get_current_active_user`
- **Response:** `PaginatedResponse[ValidationResultRead]` (HTTP 200)
- **Query parameters:**
  - `limit` / `offset` (default 50, max 100 — `PaginationParams` dependency, same as every prior list endpoint)
  - `outcome` (optional, one of `'passed' | 'failed' | 'skipped'`)
  - `validation_rule` (optional, one of `VALIDATION_RULE_NAMES`)
  - `column_name` (optional, free-text exact match — nullable; `null` matches whole-row results)
- **No N+1:** paginated row query uses `LIMIT`/`OFFSET`; total is a separate `COUNT(*)` with the same WHERE clause (same two-query-per-request pattern as Module 14's issues endpoint and Module 15's changes endpoint)
- **Filter implementation:** declarative `_RESULT_FILTER_COLUMNS` dict, same pattern as Module 14's `_ISSUE_FILTER_COLUMNS`

### Response schemas

**`ValidationRunRead`** (Pydantic, `from_attributes=True`):
```
id, organization_id, task_run_id, task_id, data_source_id, remediation_run_id,
approved_changes_considered, passed_count, failed_count, skipped_count,
results_by_rule, validation_engine_version, processing_duration_ms (derived),
created_at
```

**`ValidationResultRead`** (plain `BaseModel`, built field-by-field like `RemediationChangeRead`):
```
id, validation_run_id, remediation_run_id, remediation_change_id,
source_issue_id, validation_rule, outcome, reason,
original_value, proposed_value, validation_engine_version, created_at
```

---

## 6. Worker / Handler Design

**File:** `app/worker/handlers/validation.py`  
**Class:** `ValidationHandler`  
**Registration:** `HANDLER_REGISTRY[TaskType.VALIDATE] = ValidationHandler()` in `app/worker/handlers/__init__.py` (same one-line registry pattern every prior handler uses)

The handler is the **only impure layer**. It:
1. Reads from the DB (resolves `RemediationRun`, fetches approved changes + their decisions)
2. Calls the pure engine (`validate(changes) → ValidationRunResult`) — no I/O, no DB
3. Writes to the DB (inserts `ValidationRun` + batch `ValidationResult` rows)

It never opens, reads, or writes any file. It never touches `Issue`, `IssueDetectionRun`, `RemediationChangeDecision`, or `RemediationChange` with a write. Every upstream read is scoped to `context.task_run.organization_id`.

**Transaction boundary:** one `db.commit()` after both `ValidationRun` and all `ValidationResult` rows are added to the session via `db.add_all([...])`. If commit fails with `IntegrityError` on `UNIQUE(task_run_id)`, the handler re-fetches and returns the existing row (same pattern as every prior handler's idempotency path).

**Fetching approved changes (no N+1):**
```python
# One query: all changes for the run
changes = db.execute(
    select(RemediationChange)
    .where(
        RemediationChange.remediation_run_id == remediation_run.id,
        RemediationChange.organization_id == org_id,
    )
    .order_by(RemediationChange.row_number.asc(), RemediationChange.id.asc())
).scalars().all()

# One query: all decisions for those changes (batch IN)
change_ids = [c.id for c in changes]
# Resolve latest decision per change in Python (first-win DESC) — no DISTINCT ON
decision_rows = db.execute(
    select(RemediationChangeDecision.remediation_change_id, RemediationChangeDecision.decision)
    .where(
        RemediationChangeDecision.remediation_change_id.in_(change_ids),
        RemediationChangeDecision.organization_id == org_id,
    )
    .order_by(RemediationChangeDecision.decision_timestamp.desc())
).all()

# Build approved set
approved_change_ids = {
    change_id for change_id, decision in _first_win(decision_rows)
    if decision == 'approved'
}
```

**Fetching `issue_type` for each approved change (no N+1):**

`ValidationChangeInput` requires `issue_type` and `issue_original_value` from the upstream `Issue` row. One additional batch `IN` query against `issues` using the `source_issue_id` values of approved changes:

```python
issue_ids = [c.source_issue_id for c in approved_changes]
issue_rows = db.execute(
    select(Issue.id, Issue.issue_type, Issue.original_value, Issue.suggested_fix)
    .where(Issue.id.in_(issue_ids), Issue.organization_id == org_id)
).all()
```

Total queries before engine call: 3 (remediation_run + changes, decisions, issues). Total after engine call: 1 batch INSERT via `db.add_all`. No query inside any loop.

---

## 7. Idempotency Strategy

Module 17 uses the same strategy every prior handler uses, unchanged:

1. **Primary gate:** `UNIQUE(task_run_id)` on `validation_runs`. The handler checks for an existing row before re-running; a second execution of the same `TaskRun` returns the first run's summary without re-fetching anything.

2. **Race condition safety net:** If two workers race on the same `TaskRun`, one will fail with `IntegrityError` on `UNIQUE(task_run_id)`. The losing worker catches `IntegrityError`, re-fetches the winning worker's `ValidationRun` row, and returns it as if it were its own — identical semantics to every prior handler.

3. **Stable output under retries:** The approved-changes fetch uses a deterministic order (`row_number ASC, id ASC`) and the engine processes inputs in a fixed sort order. The same approved set → identical `ValidationResult` rows, every time. This is the module's repeatability invariant (tested in `test_validation_repeatability.py`).

4. **`validation_max_persisted_results` ceiling:** If the approved changes set exceeds this (defensive only, not expected in practice), the excess tail (in sort order) becomes `skipped` results with reason `PERSISTED_RESULT_LIMIT_REACHED`. The count invariant `passed + failed + skipped == approved_changes_considered` is preserved even in this case, same as `RemediationRun`'s own overflow handling.

---

## 8. Security Model

| Requirement | Mechanism |
|-------------|-----------|
| Authenticated user required | `Depends(get_current_active_user)` on both endpoints |
| Organization ownership verified | Every query scopes to `current_user.organization_id`; cross-org → 404 |
| No superuser required for reads | Read-only results contain no privileged data; superuser restriction only blocks writes |
| Reviewer identity never accepted from caller | Handler sets no reviewer fields — `ValidationResult` has no reviewer concept; the engine is automated, not human-initiated |
| Immutable audit | `ValidationResult` rows are never UPDATEd or DELETEd by any Module 17 code path |
| No internal IDs from another tenant | The 404 chain (task → task_run → validation_run) validates org ownership before any ID is returned |
| Encryption-compatible `reason` field | `Text()` (unbounded) — same rationale as `RemediationChangeDecision.comment` |
| No cross-tenant access | Every batch `IN` query includes `organization_id = org_id` as an explicit predicate, not just as an implicit join |

---

## 9. Risks

**R1 — "Approved" is resolved at run time, not frozen.**  
If a `RemediationChangeDecision` row is added or superseded (future admin-override module) between when the `VALIDATE` task is queued and when it runs, the validated set may differ from what the operator expected. Module 17's handler always operates on the "latest decision wins" snapshot at execution time. Mitigation: this is documented behavior, not a defect. The `ValidationRun.approved_changes_considered` count is the source of truth for what was validated.

**R2 — Approved changes with NULL `proposed_value` (remove-duplicate actions).**  
These proposals have `proposed_value=None` by design. The corresponding validation rules (`validate_remove_duplicate_row`, `validate_remove_duplicate_primary_key`) must handle this explicitly — a `None` proposed_value is correct for these actions, not a failure. Rules that naively check `proposed_value is not None` would produce false failures. Mitigation: explicit `if action in REMOVE_ACTIONS: ...` branches in each rule's `validate()` method.

**R3 — No config context in `ValidationChangeInput` by default.**  
Rules for `standardize_capitalization`, `normalize_enum_value`, and `normalize_date` need config (`capitalization_target`, `allowed_values`, `target_date_format`) to evaluate correctly. If this config is absent, the rule must `skipped` rather than guess. The handler must look up config during the fetch step (same `RemediationColumnRule`/`IssueDetectionColumnRule` resolution Module 15 already does) and embed it in `ValidationChangeInput`. Mitigation: `ValidationChangeInput` carries `column_config: ValidationColumnConfig | None` (defined in `app/validation/types.py`); rules that need config and find `None` return `skipped`.

**R4 — Module 15 and Module 17 share rule logic (reuse vs. duplication).**  
The validation engine must verify proposed values against the same deterministic functions Module 15 used to propose them. The risk is that Module 17 re-implements the same logic independently, allowing them to diverge. Mitigation: validation rules **import and call** the same `app.standardization` and `app.cleaning` functions Module 15 already uses. They never copy the logic. A validation rule is a thin predicate wrapper, not a reimplementation.

**R5 — Empty approved set.**  
If no changes are approved, the handler produces `approved_changes_considered=0` and no `ValidationResult` rows. This is a valid, non-error outcome. The API summary endpoint returns this run normally; the results endpoint returns `{items: [], total: 0}`. Mitigation: no special-casing required; the count invariant holds trivially.

**R6 — `validation_max_persisted_results` overflow.**  
Same risk and same mitigation as Module 15's `PERSISTED_CHANGE_LIMIT_REACHED`: tail changes become `skipped` with a documented reason, preserving the count invariant.

---

## 10. Edge Cases

| Case | Behavior |
|------|----------|
| No approved changes in run | `approved_changes_considered=0`, all counts 0, zero `ValidationResult` rows — valid run |
| `RemediationRun` has no decisions at all | Same as above — zero approved changes |
| Mix of approved and rejected in same run | Only approved changes are validated; rejected changes produce no `ValidationResult` row |
| Change with `proposed_value=None` (remove-duplicate) | Validation rule handles explicitly: `None` is the correct proposed value for exclusion proposals; rule returns `passed` |
| Change with `original_value=None` | Rule that needs `original_value` to compare returns `skipped` with `ORIGINAL_VALUE_MISSING` reason |
| Config missing for capitalization/enum/date change | Rule returns `skipped` with `CONFIG_NOT_AVAILABLE` reason |
| Proposed value equals original value | Rule returns `failed` with `PROPOSED_VALUE_UNCHANGED` reason — the remediation action claimed to fix something but didn't |
| Proposed value is empty string (trimmed to nothing) | Rule-specific: `trim_whitespace` → `failed` if original was not already empty; `normalize_*` → `failed` if target format produces empty |
| Two `VALIDATE` TaskRuns against the same `RemediationRun` | Both succeed; each produces its own `ValidationRun` with its own `ValidationResult` rows (different `task_run_id`, so `UNIQUE(task_run_id)` is not violated). Multiple validation runs against one remediation run are permitted — they reflect the state of approved changes at their respective execution times |
| Source `RemediationRun` missing (`source_task_run_id` not found) | `PermanentExecutionError` — same as Module 15's upstream-run-missing check |
| `RemediationChangeDecision` rows exist but all `rejected` | `approved_changes_considered=0`, valid empty run |

---

## 11. Test Strategy

### test_validation_models.py
- `ValidationRun` CHECK constraints hold (all four non-negative; sum invariant)
- `UNIQUE(task_run_id)` enforced at DB layer
- `UNIQUE(organization_id, id)` enforced at DB layer
- `ValidationResult` outcome CHECK constraint enforced
- Cascade delete: deleting `ValidationRun` cascades to `ValidationResult`
- RESTRICT on `remediation_change_id` FK

### test_validation_engine.py (pure, no DB)
- Each of the 10 rules returns `passed` for a valid proposed value
- Each rule returns `failed` for an invalid proposed value
- Each rule returns `skipped` when input is missing required fields/config
- Remove-duplicate rules handle `proposed_value=None` correctly
- Engine count invariant: `passed + failed + skipped == len(approved_changes_input)`
- Engine processes inputs in stable sort order regardless of input order

### test_validation_repeatability.py (pure, no DB)
- Same approved-changes input → byte-identical `ValidationRunResult` every call
- Mirrors `test_remediation_repeatability.py` structure exactly

### test_validation_handler.py (DB, SQLite)
- Happy path: VALIDATE run against a run with N approved changes
- Empty approved set: runs with zero approved changes produces valid zero-count run
- Idempotency: second `execute()` call on same `TaskRun` returns existing `ValidationRun`
- Race condition: concurrent inserts both resolve to the same `ValidationRun`
- PermanentExecutionError when `source_task_run_id` has no `RemediationRun`
- Config-missing → `skipped` results (not failures, not errors)

### test_validation_api.py (full HTTP, SQLite)
- Summary endpoint 200 with expected field set
- Summary endpoint 404 when `ValidationRun` does not exist
- Results endpoint 200 with correct pagination shape
- Results endpoint filtering by `outcome`, `validation_rule`, `column_name`
- Cross-tenant isolation: org A cannot see org B's validation runs
- Non-superuser (active user) can read validation results
- All 404 layers: missing task, missing task_run, missing validation_run
- Empty results set returns `{items: [], total: 0}`

**Target count:** ~50–60 new tests across all four files, consistent with prior module test counts (M14: 18+38=56 Phase 1+2; M15: 18+engine+37+32+18=105; M16: 18+37+45=100).

---

## 12. Phase Breakdown

### Phase 1 — Models, migration, enums, config (no engine, no API)
**Deliverables:**
- `backend/app/models/enums.py` — add `VALIDATION_OUTCOMES`, `VALIDATION_RULE_NAMES`; add `TaskType.VALIDATE`
- `backend/app/models/validation_run.py` — `ValidationRun` ORM model
- `backend/app/models/validation_result.py` — `ValidationResult` ORM model
- `backend/app/models/__init__.py` — register new models
- `backend/app/core/config.py` — add `validation_max_persisted_results: int` setting
- `database/alembic/versions/c1d2e3f4a5b6_validation_engine.py` — migration: `ALTER TYPE task_type_enum ADD VALUE 'validate'` (PostgreSQL path); new `validation_runs` table + indexes; new `validation_results` table + indexes. Pure additive; downgrade drops both tables and the indexes.
- Tests: `tests/test_validation_models.py` (~18 tests)
- Verification: migration cycle `base → head → base → head`, compile check

### Phase 2 — Pure validation engine (no DB, no API)
**Deliverables:**
- `backend/app/validation/__init__.py`
- `backend/app/validation/types.py` — `ValidationChangeInput`, `ValidationOutcome`, `ValidationColumnConfig`, `ValidationRunResult`, `ValidationLimits`
- `backend/app/validation/base.py` — `ValidationRule` Protocol
- `backend/app/validation/registry.py` — `VALIDATION_RULES`, `_RULES_BY_ACTION`, `get_rule_for_action()`
- `backend/app/validation/reasons.py` — closed vocabulary of outcome reasons
- `backend/app/validation/engine.py` — `VALIDATION_ENGINE_VERSION = "1.0"`, `validate()` function
- `backend/app/validation/rules/` — 10 rule modules, one per REMEDIATION_ACTION
- Tests: `tests/test_validation_engine.py`, `tests/test_validation_repeatability.py` (~30 tests)
- Verification: compile check, full regression suite

### Phase 3 — Handler, persistence, worker registration, idempotency (no API)
**Deliverables:**
- `backend/app/worker/handlers/validation.py` — `ValidationHandler` class
- `backend/app/worker/handlers/__init__.py` — register `TaskType.VALIDATE → ValidationHandler()`
- Tests: `tests/test_validation_handler.py` (~18 tests, including idempotency + empty-approved-set)
- Verification: full regression suite, migration cycle

### Phase 4 — Read-only API (summary + paginated results)
**Deliverables:**
- `backend/app/schemas/validation.py` — `ValidationRunRead`, `ValidationResultRead`
- `backend/app/api/tasks.py` — 2 new GET endpoints (additive), `_get_validation_run_or_404` helper
- Tests: `tests/test_validation_api.py` (~25 tests including cross-tenant, filtering, pagination, all 404 layers)
- Verification: full regression suite, migration cycle

### Phase 5 — Full verification, documentation, production-readiness review
**Deliverables:**
- Architecture review: no duplicated logic, determinism, tenant isolation, auth/authz, immutable audit, append-only results, API consistency, ORM↔schema parity, migration safety, rollback safety
- Security review: cross-tenant exposure, auth on reads, reviewer-identity-free (automated engine), encryption-compatible fields
- Performance review: N+1 avoidance (3 queries before engine, 1 batch insert after), efficient summary queries, index coverage, stable ordering
- `PROJECT_CONTEXT.md` update
- Final report: implementation summary, files, architecture diagram, validation workflow, API summary, security verification, performance verification, risks, technical debt, readiness score (0–100%)
- **Standing rule:** Do NOT begin Module 18. Stop after the Phase 5 report.

---

## Key Design Decisions and Rationale

**D1 — No config re-read in the validation engine.**  
The pure `validate()` function receives fully-resolved `ValidationColumnConfig` objects (pre-loaded by the handler), not raw config table references. This keeps the engine pure (no DB access) and consistent with how `remediate()` receives pre-resolved `RemediationColumnConfig`. The handler bears the cost of one additional config query; the engine remains testable in isolation.

**D2 — No `UNIQUE(organization_id, remediation_change_id)` on `validation_results`.**  
Same deliberate omission as `remediation_change_decisions` — a future re-validation pass can insert new rows without erasing history. The "latest row wins" convention (by `created_at`) applies here too.

**D3 — `source_issue_id` denormalized onto `ValidationResult`.**  
The spec requires `source_issue_id` in every result row. `RemediationChange.source_issue_id` is available at insert time without a join, so we denormalize it rather than require a join at every API read. Matches the `RemediationChangeDecision.remediation_run_id` denormalization precedent.

**D4 — No approval state machine.**  
`ValidationRun` is a read-only audit artifact (like `IssueDetectionRun`, `RemediationRun`). The validation engine never modifies anything, so there is nothing for a human to approve. Consistent with the established pattern that only modules which transform data get approval workflows.

**D5 — `TaskType.VALIDATE` instead of reusing an existing value.**  
Same reasoning as every prior module: all existing values already carry specific meanings in the handler registry. `VALIDATE` is the only honest name for "run the validation engine against an approved remediation run's changes."

**D6 — Rule logic reuses `app.standardization` and `app.cleaning` functions.**  
Validation rules are thin predicate wrappers, not reimplementations. They call the same functions Module 15's remediation rules call (e.g., `standardize_boolean`, `standardize_phone`). This makes Module 17's "does the proposed value satisfy the rule?" question trivially equivalent to "would Module 15 propose the same value?" — they use the same code path.

---

*End of Module 17 Architecture Design — awaiting approval before any code is written.*
