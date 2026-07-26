# Module 16 (Approval Queue) — Architecture Design

Pre-implementation design, approved with three adjustments (Revision 1). See Section 2a for the complete list of changes from the initial draft. Implementation of Phase 1 (models, migration, enums — no API, no handlers) is in progress.

---

## 2a. Revision 1 — Approved Adjustments

Three changes from the initial draft, approved before implementation began:

**Adjustment 1 — Append-only decision history.**
The original design enforced `UNIQUE(organization_id, remediation_change_id)`, allowing exactly one decision per change. This is replaced with a pure append-only model: the unique constraint is removed, multiple rows per change are permitted, and the **latest row by `decision_timestamp`** is always the effective state. Old rows are never edited or deleted. A future admin-override capability can add a new row (reversing a decision) without modifying any existing row. Module 16's API still enforces one decision per change at the application layer; the schema simply does not enforce it at the database layer, leaving the override path open.

**Adjustment 2 — Expanded reviewer audit fields.**
`decided_by` → `reviewer_id` (FK to users, SET NULL on delete). Three new snapshot columns added: `reviewer_name VARCHAR(255)` (snapshot of `User.full_name` at decision time, or email if full_name is NULL), `reviewer_role VARCHAR(100)` (derived from `User.is_superuser` at decision time: `"superuser"` or `"user"`; nullable for future role systems). `decided_at` → `decision_timestamp`. All four fields together make a decision row auditable even if the User record later changes.

**Adjustment 3 — Future-proof apply fields.**
Two nullable columns added to `remediation_change_decisions` for future use by the Apply/Export module: `applied_at TIMESTAMP WITH TIMEZONE` and `applied_by UUID FK → users.id SET NULL`. Both are always NULL in Module 16. The future apply step will either populate these on a new append-only row (consistent with adjustment 1) or update an existing approved row — that design decision is deferred to the apply module. The schema is ready either way.

---

## 1. Purpose and Scope

Module 15 computed a set of deterministic `RemediationChange` proposals — each one traceable to a specific Module 14 `Issue`, proposing a correction for a specific value in the source dataset. Module 15 deliberately stopped there: it proposed, but never applied, approved, or rejected anything.

Module 16 adds the human layer: a reviewer can inspect those proposals, decide which ones they agree with, and record that decision as a permanent, auditable fact. An **approved** change means "a human reviewed this proposal and accepted it." A **rejected** change means "a human reviewed it and declined it." Neither decision causes any data to be written, modified, or exported — that is out of scope for this module and always a future, separate decision.

**In scope:**
- Reviewing `RemediationChange` proposals (read — already exists via Module 15's API)
- Approving or rejecting individual `RemediationChange` rows
- Bulk-approving or bulk-rejecting all pending changes in a `RemediationRun`
- Persisting every decision as an immutable, fully-audited row (reviewer, timestamp, decision, optional comment)
- Querying approval status per change and per run

**Out of scope (explicitly deferred):**
- Applying any change to source data
- Export or materialization of approved changes
- Validation logic, quality scoring, business rules
- AI-assisted or automatic approvals of any kind
- Rollback of a decision (decisions are terminal)

---

## 2. Architecture

### How Module 16 fits in the pipeline

```
IssueDetectionRun   (Module 14, immutable, read-only)
       │ source_task_run_id
       ▼
RemediationRun      (Module 15, immutable, proposals only)
       │
       ├── RemediationChange × N   (Module 15, immutable proposal rows)
       │           │
       │           └── RemediationChangeDecision (NEW, Module 16)
       │                   decision:    "approved" | "rejected"
       │                   decided_by:  User.id (SET NULL on delete)
       │                   decided_at:  TIMESTAMP WITH TIMEZONE
       │                   comment:     TEXT | NULL
       │
       └── Approval Queue API (Module 16)
               ├── GET  .../remediation/approval              (run-level summary)
               ├── POST .../remediation/approve               (bulk: all pending → approved)
               ├── POST .../remediation/reject                (bulk: all pending → rejected)
               ├── POST .../remediation/changes/{id}/approve  (single change)
               └── POST .../remediation/changes/{id}/reject   (single change)
```

### What is NOT touched

- `RemediationRun` — no new columns, no mutation of any existing column, no new relationship.
- `RemediationChange` — no new columns, no mutation. The original proposal row is permanently sealed.
- Source CSV files, `Issue` rows, `IssueDetectionRun` — not read, not touched.
- Any existing Module 15 endpoint behavior — changes to the `GET .../remediation/changes` and `GET .../remediation` response shapes are additive only (new nullable fields; existing fields unchanged).
- No worker handler — Module 16 is purely API-driven, synchronous. No background processing, no `TaskRun`, no new `TaskType`.

### Purity and I/O

All logic is in the API layer. No pure engine module is needed for this module: the "computation" is a trivial state check and a single `INSERT` (or `INSERT ... SELECT` for bulk), requiring no business logic beyond what can be expressed as a SQL statement and a state-guard check.

---

## 3. Data Model

### New table: `remediation_change_decisions`

Append-only audit table. One row per decision event. A change with no rows is **pending**. A change with one or more rows has an effective state equal to the row with the latest `decision_timestamp`. No rows are ever edited or deleted.

```
remediation_change_decisions
────────────────────────────────────────────────────────────────────────
Column                   Type                      Constraints
────────────────────────────────────────────────────────────────────────
id                       UUID                      PK, default uuid4
organization_id          UUID                      FK → organizations.id CASCADE
                                                   NOT NULL, indexed
remediation_run_id       UUID                      Composite FK (organization_id, id)
                                                   → remediation_runs  RESTRICT
                                                   NOT NULL, indexed
remediation_change_id    UUID                      Composite FK (organization_id, id)
                                                   → remediation_changes  RESTRICT
                                                   NOT NULL, indexed
decision                 VARCHAR(10)               NOT NULL
                                                   CHECK IN ('approved', 'rejected')
reviewer_id              UUID                      FK → users.id SET NULL, nullable
reviewer_name            VARCHAR(255)              nullable (snapshot at decision time)
reviewer_role            VARCHAR(100)              nullable (snapshot at decision time)
decision_timestamp       TIMESTAMP WITH TIMEZONE   NOT NULL (set by application)
comment                  TEXT                      nullable
applied_at               TIMESTAMP WITH TIMEZONE   nullable (always NULL in Module 16)
applied_by               UUID                      FK → users.id SET NULL, nullable
                                                   (always NULL in Module 16)
created_at               TIMESTAMP WITH TIMEZONE   NOT NULL, server_default=now()
────────────────────────────────────────────────────────────────────────

Constraints / Indexes
───────────────────────────────────────────────────────────────────────
uq_remediation_change_decisions_org_id      UNIQUE (organization_id, id)
fk_remediation_change_decisions_org         FK organization_id → organizations.id CASCADE
fk_remediation_change_decisions_org_run     FK (organization_id, remediation_run_id)
                                              → (remediation_runs.organization_id,
                                                 remediation_runs.id) RESTRICT
fk_remediation_change_decisions_org_change  FK (organization_id, remediation_change_id)
                                              → (remediation_changes.organization_id,
                                                 remediation_changes.id) RESTRICT
fk_remediation_change_decisions_reviewer    FK reviewer_id → users.id SET NULL
fk_remediation_change_decisions_applied_by  FK applied_by → users.id SET NULL
ck_remediation_change_decisions_valid       CHECK decision IN ('approved', 'rejected')
ix_remediation_change_decisions_org_chg_ts  INDEX (organization_id,
                                              remediation_change_id,
                                              decision_timestamp DESC)
```

**No `UNIQUE(organization_id, remediation_change_id)`** — Revision 1's core schema change. Multiple rows per change are permitted. The effective state is derived by ordering on `decision_timestamp DESC`. Module 16's API enforces one-decision-per-change at the application layer (409 on re-decision); the schema does not, preserving the future admin-override path.

**Why RESTRICT for both composite FK ondelete?**
A decision audit row must never silently disappear alongside the thing it decided. A `RemediationChange` cannot be deleted independently (it cascades only when its parent `RemediationRun` cascades, which cascades when `Organization` cascades). RESTRICT closes the theoretical gap without blocking any real delete path — same reasoning as `RemediationChange.source_issue_id`'s own RESTRICT.

**Why `uq_remediation_change_decisions_org_id`?**
Project-wide convention: every table that may be referenced as a composite FK target must have `UNIQUE(organization_id, id)`. Required for consistency and future-proofing.

**Why `remediation_run_id` on the decision table?**
Denormalized for query efficiency. Run-scoped queries ("how many of this run's changes are approved?") can be answered with `WHERE remediation_run_id = X` rather than joining through `remediation_changes`. Mirrors `RemediationChange`'s own denormalization of `remediation_run_id`.

**Why `reviewer_id`, `reviewer_name`, `reviewer_role` instead of just one FK?**
The FK (`reviewer_id`) preserves referential integrity; the snapshots (`reviewer_name`, `reviewer_role`) preserve the human-readable audit record even if the user's name/role changes after the fact. Both are needed for a complete audit trail. `SET NULL` on FK delete still preserves the snapshot values — the snapshots survive independently of the FK.

**Why `reviewer_id` is nullable?**
`ON DELETE SET NULL`, matching every prior `approved_by`/`rejected_by`/`rolled_back_by` column in this project.

**Why `decision_timestamp` is application-side?**
Convention: action timestamps are set by the API handler (`datetime.now(timezone.utc)`), not a `server_default`. Only `created_at` uses the server default.

**Why `applied_at`/`applied_by` are here, not on `RemediationChange`?**
The apply step in a future module will be a new event in this table's history — consistent with the append-only model. The apply module creates a new row (or updates the applied_at on an existing one); either approach is accommodated since the future design is not decided yet. Placing these columns here keeps all lifecycle events about a proposal in one table.

### Vocabulary constant in `enums.py`

```python
REMEDIATION_CHANGE_DECISION_VALUES = ("approved", "rejected")
```

Plain tuple, same "small internal closed vocabulary" pattern as `STANDARDIZATION_RUN_STATUSES`, `MATCH_DECISION_TYPES`, etc.

### No modifications to `RemediationRun` or `RemediationChange`

Both tables remain fully immutable. No new columns, no mutable state, no status field added to either.

### Identifier length audit (PostgreSQL's 63-byte NAMEDATALEN limit)

All names pre-measured before Phase 1 began:

| Identifier | Length |
|---|---|
| `uq_remediation_change_decisions_org_id` | 40 |
| `fk_remediation_change_decisions_org` | 37 |
| `fk_remediation_change_decisions_org_run` | 41 |
| `fk_remediation_change_decisions_org_change` | 44 |
| `fk_remediation_change_decisions_reviewer` | 42 |
| `fk_remediation_change_decisions_applied_by` | 44 |
| `ck_remediation_change_decisions_valid` | 38 |
| `ix_remediation_change_decisions_org_chg_ts` | 44 |

All safe (longest is 44 characters, well under 63).

---

## 4. State Transitions

Each `RemediationChange` passes through exactly one of two transitions during its lifetime:

```
PENDING   (no row in remediation_change_decisions for this change)
    │
    ├── POST .../changes/{id}/approve  →  APPROVED  (terminal)
    └── POST .../changes/{id}/reject   →  REJECTED  (terminal)
    │
    ├── POST .../remediation/approve   →  APPROVED  (terminal, bulk)
    └── POST .../remediation/reject    →  REJECTED  (terminal, bulk)
```

**Properties:**
- **Terminal**: APPROVED and REJECTED are final. There is no path back to PENDING and no path from APPROVED to REJECTED or vice versa.
- **No rollback state**: Unlike Modules 6/7/8/9, this module has no `rolled_back` state. The decisions are permanent facts, not positions that can be reversed. This matches "immutable history."
- **Pending is the absence of a row**: The `remediation_change_decisions` table has no "pending" rows. A missing row is the canonical representation of the PENDING state.
- **Conflict behavior**: A second attempt to decide an already-decided change returns `409 Conflict` regardless of whether the new decision matches the existing one (Module 16 scope; admin overrides via new append rows are a future capability).

**"Latest row wins" query pattern**: The effective state of a change is `SELECT ... ORDER BY decision_timestamp DESC LIMIT 1`. In PostgreSQL, the composite index on `(organization_id, remediation_change_id, decision_timestamp DESC)` makes this efficient per-change and per-run. For run-level aggregate queries, a `DISTINCT ON (remediation_change_id)` over ordered rows returns one effective-state row per change in a single pass.

**Run-level summary state** (derived at query time, never persisted):

| Condition | Derived Status |
|---|---|
| 0 changes in run | `decided` (vacuously — nothing to decide) |
| All changes pending | `pending` |
| Some decided, some pending | `partial` |
| All decided: all approved | `fully_approved` |
| All decided: all rejected | `fully_rejected` |
| All decided: mix of approved + rejected | `decided` |

This derived status appears only in the `GET .../remediation/approval` summary response, computed via a `GROUP BY` query over `remediation_change_decisions`. It is never stored.

---

## 5. API Design

All endpoints follow the established `tasks.py` conventions: `current_user` dependency, `organization_id` scoping, 404 for cross-org and not-found, 409 for state conflicts.

New endpoints are added directly to `backend/app/api/tasks.py` — no new router file, no `main.py` change, following every prior module's "lives alongside cleaning/standardization/remediation" placement.

### Existing endpoints extended (additive only)

**`GET /tasks/{task_id}/runs/{run_id}/remediation`** (Module 15 summary)
Adds one new field to `RemediationRunRead`:
```python
decisions_summary: RemediationDecisionsSummary | None = None
```
where `RemediationDecisionsSummary` has:
```python
pending_count:  int
approved_count: int
rejected_count: int
overall_status: str  # "pending" | "partial" | "fully_approved" | "fully_rejected" | "decided"
```
`None` when no `RemediationRun` exists yet for this `TaskRun` (same 404 path). Computed via a single `GROUP BY` subquery on `remediation_change_decisions` — never a Python-side aggregate over a fully materialized set.

**`GET /tasks/{task_id}/runs/{run_id}/remediation/changes`** (Module 15 paginated changes)
Adds one new field to `RemediationChangeRead`:
```python
decision: RemediationChangeDecisionRead | None = None
```
`None` means pending. Set when a decision row exists. Added via a LEFT JOIN on `(organization_id, remediation_change_id)` in the same query that already fetches changes. No second round-trip.

Both changes are backward-compatible (new nullable fields, no existing fields modified).

### New endpoints

---

#### `GET /tasks/{task_id}/runs/{run_id}/remediation/approval`

Summary of all approval decisions for a run.

**Auth:** authenticated user, scoped to `current_user.organization_id`.
**Returns:** `RemediationApprovalSummaryRead`
**404** if no `RemediationRun` exists for this `(task_id, run_id, org_id)` triple.

Response body:
```python
class RemediationApprovalSummaryRead(BaseModel):
    remediation_run_id:  uuid.UUID
    total_changes_count: int          # total persisted changes on this run
    pending_count:       int
    approved_count:      int
    rejected_count:      int
    overall_status:      str          # derived, see table in §4
```

---

#### `POST /tasks/{task_id}/runs/{run_id}/remediation/approve`

Bulk-approves all **pending** changes in this run. Already-decided changes are skipped (not an error).

**Auth:** authenticated user, scoped to org.
**Body:** `RemediationBulkDecisionRequest`
```python
class RemediationBulkDecisionRequest(BaseModel):
    comment: str | None = None
```
**Returns:** `RemediationApprovalSummaryRead` (reflects state after the operation)
**404** if `RemediationRun` not found.
**200** always (even if 0 new decisions were created — idempotent, not an error).

Implementation: a single `INSERT INTO remediation_change_decisions (...) SELECT ... FROM remediation_changes WHERE remediation_run_id = X AND organization_id = Y AND id NOT IN (SELECT remediation_change_id FROM remediation_change_decisions WHERE organization_id = Y)` with `decided_by`, `decided_at`, and `comment` populated at execution time. Bounded by the run's own `total_changes_count` (≤ `REMEDIATION_MAX_PERSISTED_CHANGES`, default 10,000). No Python-side loop.

---

#### `POST /tasks/{task_id}/runs/{run_id}/remediation/reject`

Bulk-rejects all **pending** changes in this run. Same shape as bulk approve.

**Auth / Body / Returns:** identical to `/approve`.
**404** if `RemediationRun` not found.

---

#### `POST /tasks/{task_id}/runs/{run_id}/remediation/changes/{change_id}/approve`

Approves a single `RemediationChange`.

**Auth:** authenticated user, scoped to org.
**Body:** `RemediationDecisionRequest`
```python
class RemediationDecisionRequest(BaseModel):
    comment: str | None = None
```
**Returns:** `RemediationChangeDecisionRead`
```python
class RemediationChangeDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id:                    uuid.UUID
    remediation_run_id:    uuid.UUID
    remediation_change_id: uuid.UUID
    decision:              str
    decided_by:            uuid.UUID | None
    decided_at:            datetime
    comment:               str | None
    created_at:            datetime
```
**404** if change not found, does not belong to this run, or belongs to a different org.
**409** if change already has a decision (regardless of whether the existing decision matches).

Idempotency: the UNIQUE constraint on `(organization_id, remediation_change_id)` makes a concurrent duplicate request fail at the DB layer. The API catches `IntegrityError`, refetches, and if the existing decision matches the attempted one, returns it as 200 (treating a concurrent same-decision as success); if the existing decision differs (e.g., one thread approved, another tried to reject), returns 409. This is the only case where idempotency behavior depends on which decision wins the race.

---

#### `POST /tasks/{task_id}/runs/{run_id}/remediation/changes/{change_id}/reject`

Rejects a single `RemediationChange`. Identical shape to `/{change_id}/approve`.

---

### Helper: `_get_remediation_change_or_404`

New private helper in `tasks.py`, mirrors `_get_remediation_run_or_404`:

```
1. Fetch RemediationRun via (task_id, run_id, org_id) → 404 if not found
2. Fetch RemediationChange via (remediation_run_id, change_id, org_id) → 404 if not found
3. Return both (needed for tenant-isolation chain)
```

The change lookup must chain through the run — a `change_id` that exists but belongs to a different run or org must be indistinguishable from not-found.

### No new router, no `main.py` change

All six new endpoints go into the existing `router = APIRouter(prefix="/tasks", tags=["tasks"])` in `tasks.py`. This matches every prior module's placement and requires no registration change.

---

## 6. Schemas (Pydantic)

File: `backend/app/schemas/remediation.py` (extend the existing file)

**New schemas:**
```python
RemediationDecisionRequest        # body for individual approve/reject
RemediationBulkDecisionRequest    # body for bulk approve/reject
RemediationChangeDecisionRead     # return for individual decision endpoints
RemediationDecisionsSummary       # embedded in RemediationRunRead
RemediationApprovalSummaryRead    # return for GET .../approval
```

**Extended existing schemas (additive):**
```python
RemediationChangeRead:
    + decision: RemediationChangeDecisionRead | None = None

RemediationRunRead:
    + decisions_summary: RemediationDecisionsSummary | None = None
```

---

## 7. Risks

**R1 — Bulk approve write volume.** A run with 10,000 changes (the `REMEDIATION_MAX_PERSISTED_CHANGES` ceiling) generates 10,000 `INSERT` rows on bulk approve. Using `INSERT ... SELECT` in a single SQL statement avoids N round-trips and is bounded by the existing cap — this is the correct shape. At worst it's a 10,000-row single-statement write, which PostgreSQL handles efficiently. No Python loop needed.

**R2 — Modifying Module 15 schemas/endpoints.** Extending `RemediationChangeRead` and `RemediationRunRead` with new optional fields modifies existing schemas. The change is additive (new nullable fields, no removed or renamed fields) — this is not a breaking change by the project's own definition. Existing Module 15 tests will need to be verified against the extended shapes, but should not break since they test field presence by name, not strict exact-match on the full response body.

**R3 — Concurrent individual + bulk decisions on the same change.** If a bulk approve and an individual reject are racing, one wins the `UNIQUE` constraint and the other gets `IntegrityError`. The API handles this correctly: individual endpoints return 409 on IntegrityError; bulk endpoint uses `ON CONFLICT DO NOTHING` (no error at all). The winning decision is always the one that committed first, and it is permanent.

**R4 — `decided_by` going NULL.** Users in this project are never hard-deleted (only `is_active = False`), so `decided_by` going NULL is a theoretical concern but unreachable in practice today. The FK is `SET NULL` anyway, matching every prior `approved_by` column, to close the schema-level gap without behavioral impact.

**R5 — Review queue (Module 11) not updated.** Module 11's `GET /review-queue` aggregates items from several tables via `UNION ALL`. A `RemediationRun` with pending approval decisions is a natural addition to that queue. However, Module 11's architecture was explicitly designed so that a new module joins it by adding one new `UNION ALL` branch — that work belongs in a future module (Module 16.1 or similar), not here. Leaving it out keeps Module 16's scope tight.

**R6 — `comment` input size.** `TEXT` in PostgreSQL is unbounded. The API layer should validate that comments do not exceed a reasonable length. Recommended: `max_length=2000` on the Pydantic field — matches no existing convention in this project (prior `comment`-type fields don't exist), so a new precedent, noted explicitly here for the implementation phase.

---

## 8. Edge Cases

| Case | Correct behavior |
|---|---|
| Bulk approve a run with 0 changes | Succeeds (no-op). Returns summary with all counts = 0, `overall_status = "decided"` (vacuously). |
| Bulk approve when all changes are already decided | Succeeds (no-op). No new rows. Returns current summary. |
| Bulk approve when some changes are already approved, some pending | Approves only the pending ones. Leaves approved ones unchanged. |
| Bulk approve when some changes are already rejected, some pending | Approves the pending ones. Leaves rejected ones unchanged. |
| Individual approve a change that was already approved | 409 Conflict — cannot re-decide. |
| Individual reject a change that was already approved | 409 Conflict — cannot reverse a decision. |
| `change_id` in URL belongs to a different run than `run_id` | 404 — indistinguishable from not found (tenant isolation, chained lookup). |
| `change_id` belongs to correct run but different org | 404 — indistinguishable from not found. |
| `run_id` belongs to different task than `task_id` | 404 (same `_get_remediation_run_or_404` chain Module 15 already uses). |
| `decided_by` user soft-deleted before query | Decision row intact; `decided_by` becomes NULL (SET NULL FK). |
| Approving a change from a duplicate-removal action (`proposed_value = NULL`) | Allowed — a NULL `proposed_value` is a valid proposal (means "exclude this row"). The decision mechanism does not inspect the proposed value. |
| `comment = ""` (empty string) | Stored as empty string. Not normalized to NULL. Consistent with this project's general "don't silently transform user input" convention. |
| Concurrent bulk approve from two requests | One wins via `INSERT ... SELECT` with `ON CONFLICT DO NOTHING`; the other finds all rows already committed and also succeeds with 0 new decisions. Both return 200. |
| Concurrent individual approve + individual reject for the same change | One wins via unique constraint; the other gets IntegrityError. If winner matches the loser's attempted decision: loser returns 200 (same-decision race treated as success). If they differ: loser returns 409. |
| Bulk approve on a run that has no `RemediationRun` row yet (task still running) | 404 — `_get_remediation_run_or_404` returns 404. Correct. |

---

## 9. Test Strategy

### `tests/test_remediation_decisions.py` (unit)

- `RemediationChangeDecisionRead` DTO round-trip (from ORM attributes)
- `RemediationDecisionsSummary` with all four combinations (all pending, all approved, all rejected, mixed)
- `REMEDIATION_CHANGE_DECISION_VALUES` matches `ck_remediation_change_decisions_valid` CHECK constraint values (runtime assertion)
- Check constraint name lengths ≤ 63 bytes (pre-verified in this doc, tested programmatically)

### `tests/test_remediation_approval_api.py` (API)

#### GET `.../remediation/approval`
- Success: returns correct `pending_count`, `approved_count`, `rejected_count`
- 404 when run does not exist
- 404 when run belongs to different org
- `overall_status` correct in all five derived-status cases (pending, partial, fully_approved, fully_rejected, decided/mixed)

#### POST `.../remediation/approve` (bulk)
- Success (all pending → all approved): counts match
- Success (some already decided): only pending ones approved, no 409
- No-op (all already decided): returns 200, counts unchanged
- No-op (0 changes in run): returns 200
- 404 when run not found
- 404 when run is cross-org

#### POST `.../remediation/reject` (bulk)
- Same test matrix as bulk approve

#### POST `.../remediation/changes/{change_id}/approve` (individual)
- Success: returns `RemediationChangeDecisionRead` with correct fields
- 404 when `change_id` not found
- 404 when `change_id` belongs to different run
- 404 when `change_id` belongs to correct run but different org
- 409 when already approved (same decision)
- 409 when already rejected (different decision)
- `decided_by` matches `current_user.id`
- `decided_at` is populated and recent
- `comment` round-trips correctly (present, null, empty string)

#### POST `.../remediation/changes/{change_id}/reject` (individual)
- Same matrix as individual approve

#### `GET .../remediation/changes` (Module 15 endpoint, extended)
- `decision = null` for pending changes (no regression)
- `decision` populated for decided changes (new behavior)
- Mixed: some pending, some decided — correct per-change decision embedding

#### `GET .../remediation` (Module 15 summary endpoint, extended)
- `decisions_summary = null` when 0 changes and no decisions
- `decisions_summary` reflects real counts after decisions made

### Idempotency / concurrency test

- Create one change, approve it twice (same request identity), second call returns 409 (decisions are terminal, not idempotent in the "same result" sense)
- Simulate `IntegrityError` on `remediation_change_decisions` insert: verify API returns correct 409 or 200 depending on existing decision

### Migration tests (standard per-module pattern)

- `base → head → base → head` cycle on fresh SQLite DB
- ORM vs live PRAGMA table_info: all columns, types, nullable flags match
- All named constraints present after `upgrade`, absent after `downgrade`

---

## 10. Phase Breakdown

### Phase 1 — Data model, migration, schemas

**Deliverables:**
- `backend/app/models/remediation_change_decision.py` — `RemediationChangeDecision` SQLAlchemy model
- `backend/app/models/enums.py` — add `REMEDIATION_CHANGE_DECISION_VALUES = ("approved", "rejected")`
- `database/alembic/versions/<hash>_approval_queue.py` — migration: single new table `remediation_change_decisions`, no changes to any existing table
- `backend/app/schemas/remediation.py` — add new schemas and extend `RemediationChangeRead`/`RemediationRunRead`
- `tests/test_remediation_decisions.py` — unit tests for model and schemas
- Verify: `base → head → base → head` clean on SQLite

**Nothing in the API layer is touched in Phase 1.**

---

### Phase 2 — Individual change approve/reject endpoints

**Deliverables:**
- `backend/app/api/tasks.py` — `_get_remediation_change_or_404` helper + two new endpoints: `POST .../changes/{change_id}/approve` and `POST .../changes/{change_id}/reject`
- Extend `GET .../remediation/changes` response to include `decision` via LEFT JOIN
- `tests/test_remediation_approval_api.py` — individual endpoint tests + extended changes-list tests
- Full regression suite green

**Phase 2 complete before Phase 3 begins.**

---

### Phase 3 — Bulk run-level approve/reject + approval summary endpoints

**Deliverables:**
- `backend/app/api/tasks.py` — three new endpoints: `POST .../remediation/approve`, `POST .../remediation/reject`, `GET .../remediation/approval`
- Extend `GET .../remediation` (Module 15 summary) response to include `decisions_summary` via GROUP BY subquery
- `tests/test_remediation_approval_api.py` — bulk endpoint tests + summary endpoint tests + extended run-summary tests
- Full regression suite green

---

### Phase 4 — Final verification, documentation, production readiness

**Deliverables:**
- Full regression suite re-run (all test files, confirm 825+ passed)
- `base → head → base → head` migration cycle on fresh SQLite DB
- ORM vs PRAGMA table_info audit for `remediation_change_decisions`
- `py_compile` across `app/`
- Architecture review: no duplicated logic, determinism confirmed, tenant isolation confirmed, audit trail confirmed, API docs complete, internal consistency (DECISION_VALUES enum == CHECK constraint values)
- Update `PROJECT_CONTEXT.md`
- Write `docs/module-16-phase-4-production-readiness-report.md`
- **Stop. Do not begin Module 17 without explicit approval.**

---

## 11. Internal Consistency Invariants

The following will be confirmed before Phase 4 completes (parallel to how Module 15 confirmed its REMEDIATION_ACTIONS/registry/skip_reasons alignment):

1. `REMEDIATION_CHANGE_DECISION_VALUES` tuple == CHECK constraint values in `ck_remediation_change_decisions_valid` — confirmed by runtime assertion in `remediation_change_decision.py`.
2. `decided_by` FK uses `SET NULL` — matches every prior `*_by` column in this project.
3. `uq_remediation_change_decisions_org_id` constraint exists — required for future composite FK target.
4. `remediation_run_id` is indexed — confirmed at model level.
5. All named identifiers ≤ 63 bytes — pre-verified in this document, test-confirmed in Phase 4.

---

## 12. What Module 16 Deliberately Does Not Do

- No apply/write step. Approved changes create no corrected CSV, no modified database row, nothing downstream.
- No automatic approvals. Every decision requires a human request to an API endpoint. No configuration can trigger auto-approval.
- No AI. No model scores a change or pre-populates a recommendation.
- No rollback of a decision. Once decided, the decision is permanent.
- No audit log of "who viewed" a change (only "who decided").
- No Module 11 review-queue integration. That is a future `UNION ALL` branch, not in this module's scope.
- No pagination or filtering on `remediation_change_decisions` directly — the decision is embedded in the existing changes endpoint (which is already paginated and filterable).
- No per-column or per-action approval policy. Every change is decided individually by a human.

---

## 13. Readiness Preconditions

Module 16 can begin only after:
1. Explicit approval of this architecture document.
2. The existing `RemediationRun` and `RemediationChange` tables are clean and confirmed as the inputs — which they are (Module 15 Phase 5 verified all 4 new tables, the ORM models, and the migration in both directions).
3. The migration chain has exactly one head (`a9b0c1d2e3f4`). Module 16's migration extends from that head. Confirmed clean as of Module 15 Phase 5.

---

*Last updated: Module 16 design phase. No code has been written. Awaiting approval.*
