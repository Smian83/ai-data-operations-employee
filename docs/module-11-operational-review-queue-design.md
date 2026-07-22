# Module 11 — Operational Review Queue
## FINAL DESIGN SPECIFICATION — REVISION 3 (Approved)

---

This spec was written against the actual current codebase (`main` at commit `944737b3d56c58885270f4081fa4bb0dde3b2cbe`, tag `v0.10`, Alembic head `b4c5d6e7f8a9`, SQLAlchemy `2.0.37` per `backend/requirements.txt`). It replaces Revision 2 in full. Every field, column, join, and existing-schema claim below was re-confirmed against the actual model and schema files, including two corrections to earlier draft claims: the project's real pagination envelope is `{items, total, limit, offset}` (`app/schemas/pagination.py`), not `page`/`page_size`, and `main.py` registers every router individually (`from app.api.X import router as X_router` + `app.include_router(X_router)`), so Module 11 is not integration-seam-free.

Approved for implementation. See `docs/module-11-operational-review-queue-implementation-notes.md` for implementation-level decisions and one design-document correction discovered during implementation (`match_decisions.reason` is NOT NULL and already populated — Section 5's mapping table below is superseded by that note for the `match_decision` branch).

## 1. Goals

- Provide a single, tenant-scoped, read-only operational surface answering "what in my organization currently requires human attention," built entirely as one database-level, portable SQL aggregation over seven existing authoritative tables.
- Classify every item through a small, closed `review_category` / `review_type` pair reflecting only what Version 1 actually sources (Section 6) — not a speculative superset.
- Support filter, search, sort, pagination, and a lightweight summary, with every one of those operations pushed to the database — never Python-side filtering of a fully materialized row set.
- Read directly from `cleaning_runs`, `standardization_runs`, `match_runs`, `export_runs`, `match_decisions`, `task_runs`, and `artifact_download_events` at request time. No duplicated state, no synchronization job, no cache treated as authoritative, no `review_items` table — under any circumstance.
- Never modify, wrap, replace, or shadow the `pending_review → approved | rejected` / `approved → rolled_back` state machines already authoritative in Modules 6–9. The existing approve/reject/rollback endpoints in `tasks.py` remain the only place a decision is ever recorded.

## 2. Non-Goals

Explicitly out of scope for this module, permanently or until a future module is separately designed and approved: assignment, claiming, ownership, locking, notifications, escalation timers, reviewer roles, dual approval, dashboard analytics, reporting, a `priority` field or any prioritization logic, caching as a source of truth, a `review_items` table, and any new approval/rejection/rollback action. The existing approve/reject/rollback endpoints remain the only authoritative write path for any of these run types. `priority` is removed entirely from this revision (Section 7) — not reserved, not present as an inert field. Reintroducing it is a future module's decision, made when real prioritization behavior exists to back it, not before.

## 3. Architecture

```
GET /review-queue
  │
  ├─ resolves current_user's organization_id (unchanged Module 2 dependency)
  │
  ├─ builds ONE portable SQLAlchemy Core UNION ALL construct (Section 4)
  │  over nine physical branches (Section 5), each already projecting
  │  into one normalized twelve-column shape and each already filtered to
  │  organization_id = current_user.organization_id
  │
  ├─ applies category/type/source/search predicates (Section 8) as WHERE
  │  clauses against the UNION ALL subquery — not against materialized
  │  Python objects
  │
  ├─ Query A: SELECT ... ORDER BY ... LIMIT/OFFSET against the filtered
  │  subquery → the requested page only (Section 9)
  │
  ├─ Query B: SELECT review_category, review_type, COUNT(*) ... GROUP BY
  │  review_category, review_type against the same filtered subquery
  │  (no LIMIT/OFFSET, no ORDER BY) → at most eight grouped count rows,
  │  aggregated in Python into the summary object and the total count
  │  (Section 10)
  │
  └─ returns one ReviewQueueResponse (Section 7) combining Query A's page
     and Query B's summary/total — exactly two database round trips
     per request, regardless of how many rows match
```

No worker, no handler, no `TaskType`, no new table. The only new database object under consideration is a small set of evidence-justified indexes (Section 12), not a table, view, or materialized aggregation.

## 4. Database-Level UNION ALL Strategy

Built with SQLAlchemy Core's `sqlalchemy.union_all()` over nine `select()` constructs (Section 5), each projecting the identical column shape in the identical order — `organization_id`, `reference_id`, `task_id`, `task_run_id`, `data_source_id`, `review_category`, `review_type`, `source`, `label`, `confidence_score`, `reason`, `created_at` (twelve columns — see implementation notes for a reconciliation of this design document's own internal column count). This is a `CompoundSelect`, wrapped once via `.subquery()`, against which every filter, search predicate, sort, and the two final queries (Section 3) are issued — the aggregation, not a Python list, is the thing every operation is applied to.

**Portability requirements, stated explicitly per the requirement that no PostgreSQL-only feature may be required without a tested SQLite fallback:**

- `UNION ALL` itself: identical, standard SQL on both engines — no divergence.
- Each branch's `NULL` placeholder columns (e.g., `confidence_score` in the `task_run` branch, which has no such column) must be typed explicitly via `sqlalchemy.null().cast(...)`, not a bare Python `None` — untyped NULLs across UNION branches are a documented source of type-inference mismatches between SQLite and PostgreSQL.
- String literals for `review_category`/`review_type`/`source`/`label` per branch use `sqlalchemy.literal(...)`, standard and portable.
- `CASE WHEN` (used in the three `artifact_download_event` branches, to distinguish `INTEGRITY_FAILURE` from `FAILED` by `outcome`) is standard ANSI SQL, supported identically by both engines — no divergence.
- Every FK/UUID column referenced already uses this project's existing `Uuid()` type decorator uniformly across SQLite and PostgreSQL (established Module 2 precedent, unchanged here) — no new UUID-handling risk is introduced by this module.

## 5. Source Normalization Mapping

**Nine physical branches** (not eight — `artifact_download_events`' exactly-one-of-three-run-reference structure, per its own CHECK constraint, requires three separate joined branches rather than one branch with a three-way `COALESCE` join, to document every join precisely rather than build a join complex enough to obscure it):

| # | Physical branch | Filter | Joins required (and why) |
|---|---|---|---|
| 1 | `cleaning_runs` | `status = 'pending_review'` | `tasks` on `task_id` (Task Name search, Section 8) — no other join needed; `data_source_id` is native |
| 2 | `standardization_runs` | `status = 'pending_review'` | same as #1 |
| 3 | `match_runs` | `status = 'pending_review'` | same as #1 |
| 4 | `export_runs` | `status = 'pending_review'` | same as #1 |
| 5 | `match_decisions` | `decision = 'ambiguous'` | `match_runs` on `match_run_id` — required to obtain `task_id`, `task_run_id`, and `data_source_id`, none of which `match_decisions` itself stores; `tasks` on the `task_id` obtained from that same join (no second independent join) |
| 6 | `task_runs` | `status = 'failed'` | `tasks` on `task_id` — required both for Task Name search and to obtain `data_source_id` (`task_runs` has no `data_source_id` column; only `tasks` does) |
| 7 | `artifact_download_events` | `cleaning_run_id IS NOT NULL AND outcome IN ('integrity_failed','file_missing','stream_failed')` | `cleaning_runs` on `cleaning_run_id` — required to obtain `task_id`, `task_run_id`, `data_source_id`; `tasks` transitively for name search |
| 8 | `artifact_download_events` | `standardization_run_id IS NOT NULL AND outcome IN (...)` | `standardization_runs`, same shape as #7 |
| 9 | `artifact_download_events` | `export_run_id IS NOT NULL AND outcome IN (...)` | `export_runs`, same shape as #7 |

**Per-column derivation, branch by branch — no field is claimed present without stating exactly how:**

| Field | 1–4 (Runs) | 5 (Decision) | 6 (TaskRun) | 7–9 (Download) |
|---|---|---|---|---|
| `organization_id` | native | native (`match_decisions.organization_id`) | native | native |
| `reference_id` | native `id` | native `id` | native `id` | native `id` |
| `task_id` | native | via `match_runs` join (already required) | native | via parent-run join (already required) |
| `task_run_id` | native | via `match_runs` join (already required) | = `reference_id` (the row itself is the task run) | via parent-run join (already required) |
| `data_source_id` | native | via `match_runs` join (already required) | via `tasks` join (already required) | via parent-run join (already required) |
| `confidence_score` | native (**except `export_runs`, which has no such column — see implementation notes**) | native | NULL (no such column on `task_runs`) | NULL (no such column on `artifact_download_events`) |
| `reason` | NULL (no free-text reason column on any of the four run tables) | **native `match_decisions.reason` — see implementation notes; this design document's original claim of NULL was incorrect** | native `error_message` | native `failure_reason_code` |
| `created_at` | native | native | native | native |

Every join listed is one already required for another field this same branch needs — no join exists solely to eliminate a legitimate `NULL`.

## 6. Classification Vocabulary

`REVIEW_CATEGORIES` reflects only what Version 1 actually sources — no speculative placeholder categories:

```
REVIEW_CATEGORIES = ("PROCESSING", "MATCHING", "EXPORT", "DOWNLOAD", "SYSTEM")

REVIEW_TYPES = ("PENDING_REVIEW", "FAILED", "AMBIGUOUS", "INTEGRITY_FAILURE")
```

Both are plain string tuples, matching this project's established controlled-vocabulary precedent (`CLEANING_RUN_STATUSES`, `MATCH_DECISION_TYPES`, `ARTIFACT_DOWNLOAD_OUTCOMES`) — never a native Postgres enum.

**Adding a category or type is treated as additive, non-breaking API evolution** — the same guarantee this project already gives `TaskType`. A future module that needs a genuinely new category (e.g. an AI-assisted-correction category, when that module actually exists) adds it the same way Module 7 added `STANDARDIZE` to `TaskType` — a real, evidenced need at the time it ships, not a guess reserved now.

`source` (open-ended): `"cleaning_run"`, `"standardization_run"`, `"match_run"`, `"export_run"`, `"match_decision"`, `"task_run"`, `"artifact_download_event"`. Growing this set is always additive and never requires touching `REVIEW_CATEGORIES`/`REVIEW_TYPES`.

**Final V1 mapping:**

| source | review_category | review_type | condition |
|---|---|---|---|
| `cleaning_run` | PROCESSING | PENDING_REVIEW | always |
| `standardization_run` | PROCESSING | PENDING_REVIEW | always |
| `match_run` | MATCHING | PENDING_REVIEW | always |
| `export_run` | EXPORT | PENDING_REVIEW | always |
| `match_decision` | MATCHING | AMBIGUOUS | always |
| `task_run` | SYSTEM | FAILED | always |
| `artifact_download_event` | DOWNLOAD | INTEGRITY_FAILURE | `outcome = 'integrity_failed'` |
| `artifact_download_event` | DOWNLOAD | FAILED | `outcome IN ('file_missing','stream_failed')` |

`outcome = 'started'` remains excluded (Section 18).

## 7. Final Response Schemas

**`priority` is removed completely** — no field, no vocabulary, no sort option, no inert placeholder anywhere.

```
ReviewQueueItemRead:
  review_category: str
  review_type: str
  source: str
  label: str
  organization_id: UUID
  reference_id: UUID
  task_id: UUID | None
  task_run_id: UUID | None
  data_source_id: UUID | None
  confidence_score: float | None
  reason: str | None
  created_at: datetime
```

`label` is retained (computed as a SQL string literal per branch, Section 4).

```
ReviewQueueSummary:
  total_items: int
  pending_reviews: int
  ambiguous_matches: int
  failed_runs: int
  download_failures: int
```

**Response envelope — matches the actual existing `PaginatedResponse` convention.** `app/schemas/pagination.py`'s real `PaginatedResponse[T]` is `{items, total, limit, offset}` — not `page`/`page_size`/`total_pages`. The correct choice is a single, flat schema matching that convention's field names exactly, plus one addition:

```
ReviewQueueResponse:
  items: list[ReviewQueueItemRead]
  total: int
  limit: int
  offset: int
  summary: ReviewQueueSummary
```

## 8. Filtering and Search Rules

`review_category`, `review_type`, `source` — each optional, repeatable, applied as `IN (...)` predicates against the UNION ALL subquery's own literal columns.

**Search — four fields:**

- **Task Name** — case-insensitive substring: `func.lower(Task.name).contains(func.lower(term))`.
- **Dataset Name** — same pattern against `DataSource.name`.
- **Task ID** — exact match only in V1. No prefix matching (UUID-to-text casting portability not verified).
- **Run ID** (`reference_id`) — same exact-match-only treatment.
- **Artifact Filename — removed from Version 1** (same unverified-cast reasoning). Deferred, not silently dropped.

No search field requires materializing rows into Python before filtering.

## 9. Sorting and Pagination

Two sort options: `created_at` ascending (default) or `confidence_score` ascending with NULLs last (portable idiom: a `CASE WHEN ... IS NULL THEN 1 ELSE 0 END` ordering bucket, ascending, works identically on both engines without relying on dialect-specific `NULLS LAST` syntax).

Pagination uses `limit`/`offset`, matching `PaginatedResponse`'s existing convention exactly.

## 10. Summary Calculation

Computed via Query B: `SELECT review_category, review_type, COUNT(*) FROM (<filtered UNION ALL subquery, no LIMIT/OFFSET>) GROUP BY review_category, review_type`. At most eight grouped rows regardless of how many individual items match. `total_items` is the sum of all returned counts (reused directly as `ReviewQueueResponse.total`); `pending_reviews` sums every `PENDING_REVIEW` group; `ambiguous_matches` is the `AMBIGUOUS` group; `failed_runs` is the `SYSTEM`+`FAILED` group; `download_failures` sums both `DOWNLOAD` groups.

**Exactly two database queries per request.**

## 11. Security and Tenant Isolation

Every one of the nine branches filters `organization_id = current_user.organization_id` as the first predicate in its own SELECT. `GET /review-queue` sits behind the same single auth dependency as every existing endpoint.

## 12. Database and Index Plan

No new tables, columns, or enum values. No index is proposed as required without evidence — see implementation notes for the methodology and result. Evidence-gathering must include representative row counts and realistic status/decision/outcome distributions (not a uniform or trivially small dataset), baseline and post-index `EXPLAIN (ANALYZE, BUFFERS)` output, a redundancy check against a high-selectivity query, SQLite regression verification, and PostgreSQL migration upgrade/downgrade verification.

## 13. Performance Characteristics

Two database queries per request, each operating entirely within the database engine — no Python-side merge, filter, sort, or pagination of a fully materialized cross-source row set.

## 14. API Integration Points

`backend/app/main.py` — one new import line and one new `app.include_router(...)` line, following the exact existing pattern. New files only otherwise: `backend/app/api/review_queue.py`, `backend/app/schemas/review_queue.py`, `backend/app/review_queue/`. No change to `backend/app/schemas/__init__.py` or `backend/app/models/__init__.py`. A new Alembic migration file only if index evidence justifies one. `PROJECT_CONTEXT.md` updated per this project's established convention during the merge phase.

**Unchanged guarantees:** `tasks.py`'s approve/reject/rollback endpoints are not modified. No model file's columns, constraints, or relationships change. No worker or handler file changes. No existing state machine changes.

## 15. Acceptance Criteria

`GET /review-queue` returns every item from all eight (category, type) combinations, correctly classified, scoped strictly to the requesting organization with zero cross-tenant leakage under any filter, search, or sort combination. Filtering by `review_category`/`review_type`/`source` returns only matching items, computed entirely via database-level WHERE predicates. Search matches Task Name and Dataset Name (case-insensitive substring) and Task ID/Run ID (exact match only). `summary` and `total` are always consistent with the filtered result set and computed via database-level `GROUP BY`/`COUNT`. Only `limit` rows are ever materialized per request. No existing endpoint's contract changes. The full existing test suite continues to pass unmodified on both SQLite and PostgreSQL. No approve/reject/rollback semantics are reachable through this router. No `priority` field, vocabulary, or sort option exists anywhere.

## 16. Testing Strategy

Unit tests for the UNION ALL construction (each branch's normalized shape, classification literals, and NULLs per Section 5's mapping), the search predicates (identical results on both engines), the sort expressions (identical ordering despite differing SQL idioms), and the summary/total aggregation. API tests building at least one item per branch, confirming classification, cross-organization isolation, each filter dimension, summary/total correctness, pagination limits, and that an item transitioning out of scope via an existing approve/reject/rollback endpoint disappears from the next queue call.

## 17. Implementation Phases

Phase 1 — index evidence gathering. Phase 2 — schemas. Phase 3 — pure UNION ALL construction module. Phase 4 — API endpoint plus `main.py` registration. Phase 5 — unit and API tests. Phase 6 — SQLite and PostgreSQL verification, full regression suite, production review.

## 18. Known Limitations

`priority` does not exist in this release in any form. `outcome = 'started'` `ArtifactDownloadEvent` rows are never surfaced regardless of age. Task ID and Run ID search are exact-match only. Artifact filename search is removed from V1 entirely. No reviewer-role or dual-approval concept exists. The index plan commits to a reproducible evidence-gathering process (see implementation notes for the result).

MODULE 11 DESIGN STATUS: APPROVED AND IMPLEMENTED
