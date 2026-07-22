# Module 11 — Operational Review Queue: Implementation Notes

Implemented exactly per the approved Revision 3 design
(`docs/module-11-operational-review-queue-design.md` once committed).
This document records implementation-level decisions that were necessary
to produce working code but do not change the approved architecture,
plus one design-document correction discovered during implementation.

## 1. Design-document correction (not an architectural change)

**`match_decisions.reason` is NOT NULL and already populated.** Revision
3's Section 5 mapping table stated this column doesn't exist and should
be NULL for the `match_decision` branch. Reading
`backend/app/models/match_decision.py` directly (line 100:
`reason: Mapped[str] = mapped_column(Text(), nullable=False)`) shows the
column exists and is already populated by the matching engine with a
human-readable explanation of the pairwise comparison. The implementation
populates `ReviewQueueItemRead.reason` from this existing column for the
`match_decision` branch instead of leaving it NULL. The field's name,
type, and nullability in the approved contract are unchanged — only this
one branch's SQL now correctly reads an already-existing column instead
of a placeholder. Covered by a dedicated regression test,
`test_match_decision_reason_is_populated_not_null`.

## 2. Column-count reconciliation (not an architectural change)

Revision 3 Section 4 describes an "eleven-column shape" but its own
listed columns, plus Section 7's separately-retained `label` field, total
twelve (`organization_id`, `reference_id`, `task_id`, `task_run_id`,
`data_source_id`, `review_category`, `review_type`, `source`, `label`,
`confidence_score`, `reason`, `created_at`). The implementation includes
all twelve — `label` was never intended to be dropped (Section 7 explicitly
retains it) — this simply corrects an internal miscount in the design
document's prose.

## 3. `ExportRun` has no `confidence_score` column

Not stated explicitly in Revision 3's per-branch mapping table (which
listed `confidence_score` as a native column for all four `PENDING_REVIEW`
run-type branches). Reading `backend/app/models/export_run.py` confirms
Module 9's row-level deduplication has no scoring concept — the column
doesn't exist on `export_runs`. The `export_run` branch substitutes a
typed NULL (`cast(null(), Float)`), exactly the same "legitimately NULL,
not a gap to invent data around" treatment the design already applies to
`reason` on the four run-type branches.

## 4. Search join placement: once, against the aggregated subquery

Revision 3 Section 6 specifies Task Name and Dataset Name search but
doesn't prescribe exactly where the join happens. Rather than joining
`tasks`/`data_sources` inside each of the nine branches (a gratuitous
join for the four plain run-type branches, whose `task_id`/`data_source_id`
are already native columns), the implementation joins `tasks` and
`data_sources` exactly once, against the already-unioned subquery, and
only when a search term is supplied and isn't a UUID. This satisfies the
design's "every join documented and justified, no gratuitous joins"
requirement more strictly than per-branch joining would, and was verified
end-to-end against both engines (Section 8 below).

## 5. UNION ALL branch count: nine physical branches, not eight

Revision 3 Section 5 already anticipated this: `artifact_download_events`'
own CHECK constraint (exactly one of `cleaning_run_id` /
`standardization_run_id` / `export_run_id` is set) means the download
branch is three separate joined SELECTs — one per possible parent run
table — rather than one branch with a three-way `COALESCE` join. The
implementation follows this exactly as specified; noted here only because
"nine physical branches, eight classification outcomes" is easy to
miscount from the schema alone.

## 6. Index evidence — methodology and result (Section 3/9/12 of the design)

Before writing the migration, a representative PostgreSQL 16 database was
seeded (3 organizations; 250–750 rows per run-type table per organization;
600–1,800 rows for `task_runs`/`match_decisions`/`artifact_download_events`;
a realistic status/decision/outcome distribution — 15% `pending_review`,
70% `approved`, 10% `rejected`, 5% `rolled_back` for run tables; 80%/20%
`duplicate`/`ambiguous` for match decisions; 70% `completed` and a mix of
failure outcomes for download events — never a uniform or trivially small
dataset).

**At that near-term scale**, `EXPLAIN (ANALYZE, BUFFERS)` showed the
existing `organization_id`-only index already producing an efficient
Bitmap Heap Scan (~7–13 heap blocks, sub-millisecond) for every one of the
six candidate queries. Adding the composite index changed the query
plan's shape (moving the status/decision filter from a post-scan
`Filter` into the `Index Cond` itself) but did not measurably reduce
buffer reads or execution time — both were statistically indistinguishable
before and after.

**A second pass seeded one organization with ~20,750 `cleaning_runs`
rows** (a deliberate large-tenant scenario). With only the
`organization_id` index present, the planner fell back to a full
**Sequential Scan** for the queue's actual `status = 'pending_review'`
filter (~15% selectivity): 3.2ms, ~851 buffer touches, essentially the
whole table. Adding the composite `(organization_id, status)` index
dropped the same query to a **Bitmap Heap Scan** at 1.4ms — roughly a
2.25x improvement — touching only matching rows' heap blocks.
**Critically**, re-running the same index against a high-selectivity
filter on the same large dataset (`status = 'approved'`, ~70% of rows)
showed the planner correctly **ignoring** the new index and keeping the
Sequential Scan — confirming the index is not redundant: it is used
exactly where the queue's own inherently low-selectivity workload
(pending/failed/ambiguous/integrity-failure items are always a minority
of an organization's rows) benefits, and left unused where it would not
help.

**Decision: add all six composite indexes** (`ix_cleaning_runs_org_status`,
`ix_standardization_runs_org_status`, `ix_match_runs_org_status`,
`ix_export_runs_org_status`, `ix_task_runs_org_status`,
`ix_match_decisions_org_decision`), migration
`c5d6e7f8a9b0_review_queue_indexes.py`. The other five tables were not
independently large-scale-tested — disclosed explicitly, not asserted as
equally verified — and are added by structural analogy: each is the
identical shape (a UUID `organization_id` FK plus a small closed-vocabulary
string status/decision column, only `organization_id` indexed today), so
the same selectivity argument applies. Full raw `EXPLAIN` output for every
query cited above was captured during this session and is available on
request; this document summarizes the methodology and conclusion per the
design's own reproducibility requirement.

Migration adds indexes only — no table, column, or enum change. Verified
clean on a fresh `downgrade base → upgrade head → downgrade -1 → upgrade
head` cycle on both SQLite and real PostgreSQL 16, with all six indexes
confirmed present via `pg_indexes` after a fresh `alembic upgrade head`.

## 7. API integration points — corrected from Revision 2's original claim

Revision 2 stated "nothing else is touched" beside the two new files;
Revision 3 corrected this (Section 14) once `main.py`'s router-registration
pattern was actually read. The implementation touches exactly one existing
file: `backend/app/main.py`, two lines added (one import, one
`app.include_router(...)` call), following the identical pattern already
used for every existing router. `git diff main --stat` confirms this is
the only modified file in the entire branch; every other file is new.

## 8. Response envelope — corrected against the actual `PaginatedResponse`

Confirmed by reading `backend/app/schemas/pagination.py` directly: the
project's real pagination envelope is `{items, total, limit, offset}` —
not `page`/`page_size`/`total_pages`, which don't exist anywhere in this
project. `ReviewQueueResponse` matches that real convention exactly
(`items`, `total`, `limit`, `offset`) plus one addition (`summary`) — not
a nested pagination-inside-pagination envelope, and not a competing
naming scheme.

## 9. `priority` — confirmed absent, not merely undocumented

Per Revision 3's explicit removal requirement: no `priority` field exists
in `ReviewQueueItemRead`, no `LOW`/`MEDIUM`/`HIGH`/`CRITICAL` vocabulary
exists anywhere, `sort=priority` is rejected with `422` (not silently
accepted and redirected), and a dedicated API test
(`test_review_queue_surfaces_real_pending_review_cleaning_run`) asserts
`"priority" not in item` against a real response body. Confirmed via
repository-wide grep: the only remaining occurrence of the word
"priority" in the new code is the docstring explaining its absence.

## 10. Testing summary

23 new tests at initial implementation: 15 unit tests against the pure
aggregation module directly (`tests/test_review_queue_query.py` — all
nine branches' classification, cross-org isolation, every filter
dimension, both sort modes including NULLS-last behavior, pagination
correctness, summary/total correctness, and the `match_decisions.reason`
regression) plus 8 API-level tests (`tests/test_review_queue_api.py` —
auth required, a real worker-produced `pending_review` item surfaced
correctly end-to-end, tenant isolation via real HTTP requests, `422` on
invalid `review_category`/`review_type`/`sort`, and pagination query
params). See Section 12 for 8 additional tests added during the
post-approval production review's required fixes (31 Module 11 tests
total). All 526 tests (495 baseline + 31 Module 11) pass on both SQLite
and real PostgreSQL 16, with zero changes to any pre-existing test file.

## 11. Scope confirmation

`git diff main --stat` against this branch shows exactly one modified
file (`backend/app/main.py`, +2 lines) and new files under `app/api/
review_queue.py`, `app/review_queue/__init__.py`, `app/review_queue/
query.py`, `app/schemas/review_queue.py`, the migration, two test files,
and (added during the post-approval fixes, Section 12) a standalone
`backend/scripts/review_queue_index_evidence/` directory. No worker,
handler, model, or existing schema file was touched. No pre-existing
test was modified (only extended with new test functions). No existing
API endpoint's contract changed.

## 12. Post-approval production review: required fixes

An independent implementation review (Revision 3 architecture unchanged
throughout) returned "APPROVED AFTER REQUIRED FIXES" and required seven
fixes, none of which touch the approved API contract, classification
vocabulary, source mappings, migration architecture, or tenant-isolation
model. All seven are implemented in `backend/app/review_queue/query.py`
unless noted otherwise:

1. **Deterministic sort tie-breaking.** Both `ORDER BY` clauses in
   `fetch_review_queue()` now append `reference_id ASC` as a secondary
   key — `created_at ASC, reference_id ASC` for the default sort, and
   `confidence_score ASC NULLS LAST, reference_id ASC` for the
   score-based sort. This matters because PostgreSQL's `now()` is the
   *transaction* timestamp (constant across every statement in one
   transaction), so rows written together can legitimately share an
   identical `created_at`; without a stable secondary key, LIMIT/OFFSET
   pagination across such a tie was not guaranteed to be stable. Applied
   inside the database query, before LIMIT/OFFSET — never a Python-side
   sort. Regression tests: `test_sort_created_at_tie_is_broken_by_
   reference_id`, `test_sort_created_at_tie_pagination_has_no_overlap_
   or_gaps`, `test_sort_confidence_score_tie_is_broken_by_reference_id`
   (all in `tests/test_review_queue_query.py`, using a dedicated fixture
   that deliberately gives multiple rows an identical `created_at`/
   `confidence_score`).
2. **Whitespace-only search normalization.** `_apply_filters()` now
   trims `filters.search` exactly once, before any UUID-parsing or
   text-search branching; a value that is empty after trimming (`""`,
   `" "`, `"   "`) is treated identically to no search parameter at all
   — no predicate is added, never the previous accidental
   `LIKE '%%'` match-everything fallback. Regression test:
   `test_whitespace_only_search_behaves_as_no_search`.
3. **Malformed UUID search regression test added**
   (`test_search_malformed_uuid_falls_back_to_substring_search`) — locks
   in the pre-existing correct behavior (a `ValueError` from
   `uuid.UUID(...)` falls back to the Task Name / Dataset Name substring
   search path) with both a no-match and a genuine-match case, plus a
   cross-tenant check. No code change was required for this fix; the
   fallback was already correct, only untested.
4. **Combined-filter tests added**
   (`test_combined_review_category_and_source_filter`,
   `test_combined_review_category_review_type_and_source_filter`) —
   verify the independent `.in_()` predicates combine with AND semantics
   as intended, including a cross-tenant check under a combined filter.
   No code change was required; `_apply_filters()` already ANDs every
   active condition together.
5. **End-to-end queue transition test added**
   (`test_review_queue_item_disappears_after_approval_via_existing_
   endpoint` in `tests/test_review_queue_api.py`) — builds a real
   `pending_review` cleaning run, confirms it appears in `GET
   /review-queue`, approves it through the existing, unmodified
   `POST /tasks/{task_id}/runs/{run_id}/cleaning/approve` endpoint (no
   Module 11 write path exists or was added), and confirms the item is
   gone from the very next `GET /review-queue` call with `total` and
   `summary.pending_reviews` both correctly decremented. Proves the
   queue reads authoritative state directly, with no cache or duplicated
   state of its own.
6. **Removed the unused `session` parameter from `_apply_filters()`.**
   It was never referenced in the function body. Its one caller
   (`fetch_review_queue()`) was updated accordingly. Cleanup only — no
   behavioral change.
7. **Preserved the index-evidence scripts** at
   `backend/scripts/review_queue_index_evidence/` (`README.md`,
   `seed_representative.py`, `seed_large_scale.py`) — the same
   methodology described in Section 6 above, sanitized of the original
   sandbox-specific absolute path (now resolved relative to the script's
   own file location) and the original hardcoded organization UUID (now
   looked up by a stable slug, `evidence-org-0`, so the two scripts chain
   correctly on any fresh disposable database). Never imported by the
   application or test suite; requires an explicit `DATABASE_URL` and
   raises immediately if unset, and the README states in its first
   paragraph never to run either script against a production or shared
   database. Includes reproducible `EXPLAIN (ANALYZE, BUFFERS)`
   instructions and a targeted cleanup query (`DELETE FROM organizations
   WHERE slug LIKE 'evidence-org-%'`, which cascades via the existing
   tenant-aware foreign keys) as an alternative to discarding the whole
   disposable database.

Post-fix verification: 526 tests (495 baseline + 31 Module 11) pass on
both SQLite and real PostgreSQL 16; the Alembic
`downgrade -1` / `upgrade head` cycle was re-verified against PostgreSQL
16 after these fixes with all six indexes confirmed present via
`pg_indexes`; `c5d6e7f8a9b0` remains the single Alembic head; no
existing approval endpoint, public response field, or query parameter
changed.
