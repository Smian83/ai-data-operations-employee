# Module 6 — Data Cleaning Engine
## Design Specification (Design Only — No Code)

---

## 1. Executive Summary

Module 6 adds real, deterministic data cleaning on top of Module 5's profiling output. It reuses Module 4's execution engine and Module 5's CSV loader/profiler without modification, adds one new handler and one new package, and introduces two new database tables plus one additive column on `task_runs`. The core guarantee is structural, not procedural: the original source file is never opened for writing by this module — cleaning always reads the source and writes to a separate, tenant-scoped output location, so "never overwrite original data" is enforced by what the code is capable of doing, not just by convention. Every changed value is recorded with its rule, reason, and confidence; nothing is applied without an explicit human approval step in this release. AI-assisted correction is deliberately out of scope and left as a defined extension point, not built now.

This spec was written against the actual current codebase (`main` and `module-6-data-cleaning-engine` both verified at commit `f94c9e2`, Alembic head `b2c3d4e5f6a7`) — every pattern referenced below (handler shape, tenant FK convention, idempotency mechanism, bounded/sampled persistence) is drawn from files that were read directly, not assumed.

## 2. Goals

- Perform real cleaning transformations on CSV data that Module 5 has already profiled.
- Never modify, overwrite, or delete the original source file, or any existing `DataProfile` row.
- Record every change with its original value, new value, producing rule, reason, and confidence score.
- Require explicit human approval before a cleaning result is treated as authoritative.
- Support instant, safe rollback.
- Keep this module's rules deterministic; leave a clean extension point for AI-assisted correction in a later module, without building it now.
- Integrate with Modules 3–5's existing execution, storage, and API patterns without redesigning any of them.

## 3. Scope

**In scope:** deterministic rule-based cleaning of `CSV_UPLOAD` data already profiled by Module 5; a fixed initial rule set; per-cell audit logging, bounded the same way Module 5 bounds profiling output; a cleaned-output file written to a new location; a human approval workflow (pending → approved/rejected → optionally rolled back); additive API endpoints; per-change and per-run confidence scoring with auto-approval disabled by default.

**Out of scope:** AI-assisted or ML-based correction (deferred, extension point only); non-CSV source types (Module 5's own limitation, inherited unchanged); any in-place file mutation; a UI; per-organization custom/configurable rule sets; streaming or incremental cleaning beyond Module 5's existing size bounds; cell-level selective rollback (only whole-run rollback in this release).

## 4. Architecture

Module 6 is a new handler plus a new data model sitting on top of unchanged infrastructure. It does not introduce a new execution substrate, a new worker loop, or a new locking/leasing mechanism — Module 4's engine claims, leases, heartbeats, retries, and reports on a cleaning run exactly as it does for a profiling run today.

```
Task (task_type = TRANSFORM)
  │
  ▼
TaskRun  (source_task_run_id → the SYNC run whose DataProfile is being cleaned)
  │
  ▼
Worker claims run  →  CleaningHandler.execute(context)
  │
  ├─ reads: DataProfile (Module 5, unchanged) + the source CSV
  │         (via Module 5's tenant-scoped loader, unchanged)
  │
  ├─ computes: cleaned rows + a bounded list of per-cell changes
  │            (pure, in-memory, no I/O)
  │
  └─ writes: one new output CSV (new location, never the source path)
             + one CleaningRun row + bounded CleaningChange rows
```

The only two integration seams into existing code are: (1) `HANDLER_REGISTRY[TaskType.TRANSFORM]` now points at `CleaningHandler` instead of `NoOpHandler`, and (2) `task_runs` gains one nullable, additive column. Everything else Module 6 needs is new.

## 5. Components

| Component | Responsibility |
|---|---|
| `CleaningHandler` | Implements the existing `ExecutionHandler` protocol; orchestrates the five-stage pipeline (Section 8); the only new worker-facing component. |
| `cleaning/rules.py` | The fixed, ordered set of deterministic cleaning rules (Section 9); pure functions, no I/O. |
| `cleaning/engine.py` | Runs the rule set over loaded rows in the required order; pure, no I/O. |
| `cleaning/types.py` | Value objects: limits, a loaded-for-cleaning row set, a single `Change`, and the aggregate `CleaningResult`. |
| `CleaningRun` (model) | One row per cleaning `TaskRun`; summary, confidence, output location, approval state. |
| `CleaningChange` (model) | Many rows per `CleaningRun`; one per recorded cell-level change, capped per Section 14. |
| New API endpoints | Read the cleaning result, list changes, approve/reject/roll back — all additive, all under the existing `tasks` router. |

## 6. Data Flow

1. A `DataProfile` already exists for some prior `SYNC` `TaskRun` (Module 5's output — unchanged, read-only input to this module).
2. A caller creates a `TransformTaskRun`, specifying which prior `SYNC` run's profile to clean (`source_task_run_id`).
3. The worker claims it and calls `CleaningHandler.execute`, which pulls the `DataProfile` and re-reads the same source CSV (same file, same tenant-scoped path — never a different or previously-unseen file).
4. The rule engine produces cleaned rows and a change list entirely in memory; nothing is written yet.
5. The cleaned rows are serialized to a brand-new file under a separate output root; the source file is never opened for writing at any point in this flow.
6. `CleaningRun` and (bounded) `CleaningChange` rows are persisted in one transaction, idempotently.
7. A human reviews the summary and either approves, rejects, or later rolls back — a pure status transition, no further data movement.

## 7. Worker Flow

No changes to `app/worker/engine.py`, `reaper.py`, `metrics.py`, or `runner.py`. A cleaning run is claimed, leased, heartbeated, retried, and completed by the exact same code path a profiling run uses today:

- Claim: `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL (unchanged), lease-token fencing (unchanged).
- Execution: the worker calls `handler.execute(context)` where `handler` is now `CleaningHandler` for `TaskType.TRANSFORM` — the dispatch itself is a one-entry registry change, not new dispatch logic.
- Completion/failure/retry: `complete_success` / `complete_failure` (unchanged) — a bug inside a cleaning rule surfaces as an unhandled exception, which the runner's existing generic catch-all already treats as a retryable failure, exactly as it would for any other handler.
- Reaper: an abandoned cleaning run is reclaimed on lease expiry exactly like an abandoned profiling run — no cleaning-specific recovery logic needed.

## 8. Cleaning Pipeline

Five stages inside `CleaningHandler.execute`, mirroring `CsvProfilingHandler`'s existing shape (load → compute pure result → persist idempotently → return a summary string):

1. **Resolve inputs.** Validate the data source is an active `CSV_UPLOAD`; validate `source_task_run_id` refers to a `DataProfile`-bearing run in the same org and task. Any failure here is permanent (bad configuration, not a transient condition).
2. **Load.** Re-run the existing, unchanged `load_csv` with the existing `CsvLimits` — no new size/row/column limit logic.
3. **Clean.** Run the rule engine (Section 9) over the loaded rows in the fixed required order, producing cleaned rows plus a change list.
4. **Materialize output.** Write cleaned rows to a new file under a tenant-scoped output root; hash it (SHA-256, same as Module 5) for the audit record.
5. **Persist and score.** Compute confidence (per-change and per-run), write `CleaningRun` + bounded `CleaningChange` rows in one transaction using the same unique-constraint-plus-refetch idempotency pattern `CsvProfilingHandler` already uses, and set the initial approval status.

## 9. Cleaning Rules

A fixed, ordered pipeline — not a configurable rules engine or expression language, deliberately, to keep behavior deterministic and auditable rather than clever. Order matters because later rules depend on earlier ones having already normalized their input:

1. **Structural repair** — same ragged-row handling Module 5's loader already flags (`too_few_fields` → pad, `too_many_fields` → truncate). Must run first.
2. **Whitespace normalization** — trim/collapse whitespace. Must precede type coercion and duplicate detection, both of which are whitespace-sensitive.
3. **Null/blank normalization** — canonicalize blank-equivalents (empty string, `"N/A"`, `"-"`, etc.) to a single missing-value representation, using the column's dominant type from the existing `DataProfile` as context.
4. **Type coercion** — for a column whose profiled `inferred_type` is concrete (not `"mixed"`/`"null"`), coerce non-conforming values toward that type's canonical form, reusing the exact type-detection logic already in `csv_profiler._value_type` rather than reimplementing it.
5. **Casing/format normalization** — light-touch only; no aggressive rewriting that could change meaning.
6. **Duplicate handling** — flag duplicates by default using the same normalized-tuple comparison the profiler already computes; do not delete rows automatically. Row removal exists as a separate, explicitly opt-in rule, off by default — the single most consequential action this engine can take defaults to the conservative choice.

Each rule carries a fixed confidence value reflecting how certain that class of correction is (mechanical repairs like whitespace-trim and row-shape fixes are 1.0; date reparsing, the least certain coercion, is lower). A `CleaningRun`'s overall confidence is the minimum across its applied changes — one uncertain change should pull the reported confidence down, not get diluted by many trivial ones.

## 10. Validation Integration

Two integration points, both reuse-only, no new validation logic:

- **Pre-clean:** cleaning requires an existing `DataProfile` for the source run — Module 5's output is Module 6's input contract, not a parallel system. Missing profile → permanent failure, same classification convention Module 5 already established for missing/unsupported sources.
- **Post-clean:** the cleaned rows are run back through the existing, unchanged `profile_csv` function to produce a comparison — row count, missing-value total, and duplicate count before versus after — giving the human approver a concrete, machine-computed quality signal using code that's already tested, not a new metric invented for this module.

## 11. Audit Logging

Two append-only records:

- `CleaningRun` — one per cleaning `TaskRun`, same one-row-per-run idempotency pattern as `DataProfile`.
- `CleaningChange` — many per run: row, column, original value, cleaned value, rule name, human-readable reason, and confidence. Never updated or deleted, including after rollback — the record of what was attempted and why persists regardless of what happens to its approval state.

Both follow the same tenant-scoping convention every table since Module 3 uses: `organization_id` on every row, composite FK to the parent scoped by organization, no new isolation model introduced.

## 12. Rollback Strategy

Rollback is a status transition, not a destructive operation — a direct consequence of the source file never being touched and the cleaned output living at a separate location. Rolling back sets `CleaningRun.status = ROLLED_BACK` and records who/when; the `CleaningChange` audit trail is untouched, so a rolled-back run's history remains fully inspectable. Nothing treats a cleaned output as authoritative except a read path gated on `status == APPROVED`; once rolled back, that gate simply closes. The underlying output file is not deleted automatically — it remains as a further audit artifact, subject to a future retention policy, not an immediate irreversible action. Rollback in this release is whole-run only; there is no partial/cell-level undo.

Approval itself follows a small, fixed state machine: pending review leads to either approval or rejection; only an approved run can later be rolled back. Every transition records the acting user and timestamp, matching the `created_by`/`triggered_by` convention already used throughout this codebase. This state machine is exposed through the new API endpoints in Section 16, not through any new worker-side mechanism.

## 13. Security

- **Tenant isolation** is enforced the same way Module 5's B1 fix established for CSV input: cleaning output is written under a per-organization output root, never a shared one, and every new table and endpoint follows the existing composite-FK tenant-scoping pattern. There is no new class of cross-tenant exposure being introduced — the same discipline that closed B1 is applied to the new output path from the start, not retrofitted later.
- **Auth**: every new endpoint sits behind the same `get_current_active_user` dependency every existing endpoint uses. No new authentication or authorization mechanism.
- **No new credential surface.** `CleaningHandler` receives the same `ExecutionContext` as every handler; it does not touch `CredentialProvider` or `DataSourceCredential` at all, since cleaning a `CSV_UPLOAD` source needs no live external credentials, matching `CsvProfilingHandler`'s existing scope.
- **File safety.** Output writing reuses the same bounded, safe I/O discipline as the input loader (no unbounded writes, no path built from unsanitized client input — the output path is server-derived from `organization_id` and an internally-generated run identifier, never from a client-supplied string).

## 14. Performance

- Reuses Module 5's existing row/column/cell-size bounds as-is — no new ceiling to define, since the reload goes through the identical loader.
- Rule evaluation is the same order of magnitude as Module 5's profiling pass (`rows × columns`, small constant per rule) — comparable cost run a second time, not a new performance class.
- `CleaningChange` persistence is explicitly capped (a new setting, default 10,000 rows per run) with an always-present aggregate count and a per-rule breakdown, so nothing is silently lost even when individual diffs are capped — the same bounded-but-never-silent pattern `DataProfile`'s truncated sample/distinct-value lists already established.
- Inherits Module 4's existing, already-documented property that a running handler cannot be preemptively cancelled mid-execution — the lease/timeout mechanism governs reclaim, not interruption. Not a regression Module 6 introduces; not something this module attempts to fix, since that would mean changing Module 4.

## 15. Database Impact

**One additive column on `task_runs`:** `source_task_run_id`, nullable, self-referential composite FK scoped by organization, `RESTRICT` on delete. Meaningful only for `TRANSFORM` runs; `NULL` for every existing row and every other task type. Zero impact on existing data or existing queries.

**Two new tables:**
- `cleaning_runs` — one row per cleaning `TaskRun`: identifiers, output file location and hash, summary counts, confidence, post-clean comparison metrics, approval state and who/when for each transition.
- `cleaning_changes` — many rows per `cleaning_runs` row, capped per Section 14: row/column location, original and cleaned value, rule name, reason, confidence.

`status` on `cleaning_runs` is a plain string, following the exact precedent `TaskRunEvent.event_type` already set in this codebase, not a new native Postgres enum — this avoids the one bug class (enum DDL owned in two places) this project has actually hit twice. `TaskType` is unchanged; `TRANSFORM` is reused, already reserved for this per the handler registry's own existing docstring.

Migration: one new file, `down_revision` pointing at the current head (`b2c3d4e5f6a7`) — a single new head, verified before merge exactly as every prior module's migration was.

## 16. API Impact

All additive; no existing endpoint's signature, response model, or behavior changes.

- Run creation gains one optional field identifying which prior run's profile to clean — existing callers that omit it are entirely unaffected, since every current run-creation call already sends no body.
- A read endpoint returning the cleaning summary for a run, mirroring the existing profile-read endpoint's exact shape and 404 semantics (not visible to the caller's org, or no cleaning result yet).
- A paginated read endpoint listing individual changes for a run, mirroring the existing task-run-events pagination pattern.
- Three action endpoints implementing the approval state machine from Section 12: approve, reject, roll back — each tenant-scoped, each requiring the run to be in the correct starting state (a conflict response otherwise), each recording the acting user.

All new endpoints live alongside the existing task-run sub-resource endpoints, following the precedent Module 5 already set rather than introducing a new router.

## 17. Folder Structure

```
backend/app/
  cleaning/                          (new package, mirrors profiling/)
    types.py
    rules.py
    engine.py
  worker/handlers/
    cleaning.py                      (new, mirrors csv_profiling.py)
  models/
    cleaning_run.py                  (new)
    cleaning_change.py               (new)
  schemas/
    cleaning_run.py                  (new)
    cleaning_change.py               (new)
  api/
    tasks.py                         (existing file, additive endpoints only)

database/alembic/versions/
  {new_revision}_data_cleaning_engine.py   (new, down_revision = current head)

tests/
  test_cleaning_rules.py             (new)
  test_cleaning_engine.py            (new)
  test_cleaning_handler.py           (new)
  test_cleaning_api.py               (new)
```

Direct structural mirror of the existing `profiling/` + `csv_profiling.py` + `data_profile.py` model/schema pair — no new organizational pattern to learn.

## 18. Testing Strategy

**Unit (pure, no DB, no client):** each rule tested in isolation for conforming input, already-clean input, and edge cases; the engine's ordering behavior specifically (a value needing trim-then-coerce must be tested to fail if steps run out of order); confidence aggregation including the zero-changes case; the persisted-changes cap.

**Integration:** full handler execution against a real fixture file under the existing tenant-scoped directory convention, including idempotency across retries using the same proof pattern as Module 5's own idempotency test; tenant isolation on every new endpoint and on file access, same proof shape as the B1/B2 fixes; the full approval state machine, including invalid-transition conflict responses; an explicit, automated assertion that the source file's hash is unchanged after a cleaning run — not just a design claim; the existing full test suite re-run to confirm zero regressions; the standing SQLite and real-PostgreSQL migration-cycle verification, plus a single-head check before merge.

## 19. Risks

- **Rule bugs silently producing wrong "clean" data.** Mitigated by construction, not just testing: nothing is applied without human approval by default, and every change is individually attributable to a named rule with a reason — a bad rule's output is visible and rejectable before it affects anything downstream.
- **Large per-run change volume.** A pathologically dirty file could generate far more individual changes than are practical to store row-for-row. Mitigated by the explicit cap plus always-present aggregate counts (Section 14) — bounded, not unbounded, and never silently lossy about the total.
- **Reusing `TaskType.TRANSFORM` instead of a dedicated enum value.** The trade-off is explicit: a new enum value would read more clearly in logs and payloads, at the cost of a Postgres `ALTER TYPE ADD VALUE` and touching three files for a distinction the handler registry's docstring already states in writing. Reusing `TRANSFORM` is the recommended and lower-risk path; proceeding with it.
- **Inherited Module 4 limitation:** no in-process cancellation of a long-running handler. Cleaning's cost is comparable to profiling's, which is already a known, accepted property of this system — not a new risk, but worth naming since cleaning is the second handler to actually do meaningful work under that constraint.
- **Output file accumulation.** Rolled-back and rejected runs leave their output files on disk rather than deleting them, by design (Section 12). This is a storage-growth consideration for operations, not a correctness risk, and is deferred to a retention policy rather than solved here.

## 20. Acceptance Criteria

- [ ] Full test suite green (existing 136 plus all new Module 6 tests).
- [ ] SQLite migration cycle clean; real PostgreSQL upgrade → downgrade → upgrade clean; exactly one Alembic head confirmed before merge.
- [ ] Automated proof (not just a design claim) that source files are byte-identical before and after any cleaning run.
- [ ] Every recorded change has a non-null rule name, reason, and confidence score.
- [ ] Rollback tested end-to-end and confirmed non-destructive.
- [ ] Tenant isolation proven on every new endpoint and on the handler's file/DB access, to the same standard as the B1/B2 fixes.
- [ ] Zero changes to any existing API endpoint's contract; zero changes to Modules 1–5 beyond the one additive column and the one registry-entry swap.
- [ ] Not merged into `main` until reviewed and approved.

---

No code, no migrations, no database models, no API endpoints, and no tests have been generated. This is the design only, waiting on approval before implementation begins.

---

FINAL DESIGN STATUS:
READY FOR REVIEW
