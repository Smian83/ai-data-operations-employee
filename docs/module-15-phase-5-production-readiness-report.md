# Module 15 (Deterministic Cleaning/Remediation Engine) — Phase 5 Report

Final verification, documentation, and production readiness assessment for Modules 14 (Issue Detection Engine) and 15 (Deterministic Cleaning/Remediation Engine). Covers all ten verification requirements, complete documentation of every remediation action / skip reason / API endpoint / configuration option, known limitations, technical debt, and a readiness recommendation. Module 16 (Approval Queue) has **not** been started, per explicit instruction.

---

## 1. Final Implementation Summary

Module 15 proposes deterministic, rule-based corrections for a subset of the Issues Module 14 already detected. It never writes a corrected file, never modifies source data, and has no approval/apply/rollback concept — every `RemediationChange` row is a proposal only, fully traceable back to the exact Issue that caused it. Five phases, all complete on `main` (no dedicated branch, per instruction):

- **Phase 1** — models, migration, config (`remediation_runs`, `remediation_changes`, `remediation_column_rules`, `remediation_dataset_configs`; additive `issue_detection_runs.source_sha256`).
- **Phase 2** — pure engine (`app/remediation/engine.py`) + 10 actions + registry.
- **Phase 3** — `RemediationHandler`, registered for `TaskType.REMEDIATE`.
- **Phase 4** — read-only summary + paginated changes API.
- **Phase 5** (this report) — architecture review, verification, documentation. One genuine defect found and fixed (Section 3).

## 2. Files Added/Modified (Module 15, cumulative across all phases)

**New:**
`backend/app/models/remediation_run.py`, `remediation_change.py`, `remediation_column_rule.py`, `remediation_dataset_config.py`; `backend/app/remediation/` (package: `types.py`, `skip_reasons.py`, `reasons.py`, `registry.py`, `engine.py`, `actions/` — `trim.py`, `collapse.py`, `capitalization.py`, `booleans.py`, `numeric.py`, `phones.py`, `dates.py`, `enum_values.py`, `duplicates.py`); `backend/app/worker/handlers/remediation.py`; `backend/app/schemas/remediation.py`; `database/alembic/versions/a9b0c1d2e3f4_deterministic_cleaning_engine.py`; `docs/module-15-deterministic-cleaning-engine-design.md`; `tests/test_remediation_actions.py`, `test_remediation_engine.py`, `test_remediation_registry.py`, `test_remediation_repeatability.py`, `test_remediation_handler.py`, `test_remediation_api.py`.

**Modified:** `backend/app/api/tasks.py` (Phase 3: `source_task_run_id`-required tuple; Phase 4: summary + changes endpoints); `backend/app/worker/handlers/__init__.py` (registry swap); `backend/app/models/issue_detection_run.py`/`issue.py` (Phase 1 additive touches); `PROJECT_CONTEXT.md` (this phase).

**Phase 5 fix:** `backend/app/remediation/actions/capitalization.py` (import and reuse `app.standardization.rules.casing`'s `lower_case`/`upper_case`/`title_case` instead of raw `str.lower`/`str.upper`/`str.title`); `tests/test_remediation_actions.py` (new regression test).

## 3. Duplicated-Logic Finding and Fix

`standardize_capitalization`'s `"title"` target used Python's built-in `str.title()`. That method has a well-known bug: it treats an apostrophe as a word boundary, so `"don't".title() == "Don'T"`. Module 7's `app/standardization/rules/casing.py::title_case()` was written specifically to avoid this (its own docstring documents the bug it avoids), but Module 15's action never imported it — silently reintroducing a bug a sibling module had already solved.

**Fix:** `capitalization.py` now imports `lower_case`/`upper_case`/`title_case` from `app.standardization.rules.casing` directly and uses them in `_APPLY`. A new regression test, `test_capitalization_title_target_avoids_str_title_apostrophe_bug`, proves `"don't panic, o'brien"` now renders as `"Don't Panic, O'brien"` (verified: `title_case("don't panic, o'brien") == "Don't Panic, O'brien"`).

No other duplicated-logic findings. Every other action either reuses an existing function/value verbatim (`trim_whitespace`/`collapse_multiple_spaces` reuse Module 14's own `Issue.suggested_fix`; `normalize_boolean`/`normalize_phone`/`normalize_numeric` reuse Module 6/7's `standardize_boolean`/`standardize_phone`/`standardize_numeric`; `remove_duplicate_row`/`remove_duplicate_primary_key` re-identify nothing Module 14 didn't already flag) or is justified as genuinely new logic with no existing equivalent to reuse (`normalize_date`'s format-driven parse/render, `normalize_enum_value`'s narrower "exactly one candidate" question).

## 4. Verification Results (all ten dimensions)

| # | Dimension | Result |
|---|---|---|
| 1 | No duplicated business logic | One genuine finding, fixed (Section 3) |
| 2 | Deterministic behavior | Confirmed — no `random`/`uuid`/`datetime.now`/DB access anywhere in `app/remediation/engine.py` or any of the 10 actions; fixed `(row_number, column_name, issue_type)` sort key throughout |
| 3 | Audit trail completeness | Confirmed — every `RemediationChange` traces to exactly one `source_issue_id`; `RemediationRun` carries engine version, all counts, and both `changes_by_action`/`skipped_by_reason` breakdowns; both tables append-only/immutable |
| 4 | Tenant isolation | Confirmed — every query in `RemediationHandler` and both API endpoints is scoped by `organization_id`; `_get_remediation_run_or_404` chains task → task_run → remediation_run, all org-scoped |
| 5 | Idempotency | Confirmed — `RemediationHandler` uses the project-wide IntegrityError-catch-and-refetch pattern on the unique `task_run_id` constraint |
| 6 | Migration safety + rollback safety | Confirmed — `downgrade()` drops tables/columns in correct dependency order; full `base → head → base → head` cycle run this phase, clean both directions |
| 7 | API documentation completeness | Confirmed — both endpoints carry extensive docstrings (FastAPI's documented-by-docstring convention, consistent with every other endpoint in `tasks.py`; no endpoint in this file uses explicit `summary=`/`tags=`) |
| 8 | Internal consistency | Confirmed — `REMEDIATION_ACTIONS` enum, `REMEDIATION_RULES` registry, and `CHANGE_REASONS` keys are set-identical (10/10); `skip_reasons._ALL` has exactly 10 unique entries matching its own docstring |
| 9 | Full regression suite | **825 passed, 8 skipped** (PostgreSQL-only tests), 0 failed, across all 57 test files |
| 10 | Migration cycle / ORM audit / compile check | `python -m py_compile` clean across `app/`; SQLite `PRAGMA table_info` for all 4 new tables matches the ORM models exactly, including `target_date_format` (this session's date-format fix) |

## 5. Module 15 Architecture Diagram

```
                    ┌─────────────────────────┐
                    │   IssueDetectionRun     │  (Module 14, upstream)
                    │  source_sha256          │
                    └───────────┬─────────────┘
                                │ source_task_run_id (org-scoped lookup)
                                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                    RemediationHandler                       │
  │  (impure layer — the only place I/O happens)                │
  │                                                               │
  │  1. Resolve upstream IssueDetectionRun (org-scoped)          │
  │  2. Reject NULL/mismatched source_sha256                    │
  │  3. Existing-run short-circuit (idempotency)                │
  │  4. Re-read source CSV, verify hash                         │
  │  5. Fetch persisted Issues                                   │
  │  6. Resolve column/dataset config                            │
  │  7. Call pure engine ───────────────┐                        │
  │  8. Persist RemediationRun +         │                        │
  │     RemediationChange rows           │                        │
  └───────────────────────────────────┼──────────────────────────┘
                                       ▼
                    ┌──────────────────────────────────┐
                    │   app/remediation/engine.py       │
                    │   remediate() — PURE              │
                    │   no I/O, no randomness, no DB    │
                    │                                    │
                    │   for each Issue (sorted):         │
                    │     rule = REGISTRY[issue_type]    │
                    │     outcome = rule.propose(...)    │
                    │     → RemediationChange  OR        │
                    │     → SkippedIssue(reason)         │
                    └──────────────────┬─────────────────┘
                                       │
                    ┌──────────────────▼─────────────────┐
                    │  10 RemediationRule actions         │
                    │  (app/remediation/actions/)         │
                    │  trim · collapse · capitalization · │
                    │  booleans · numeric · phones ·      │
                    │  dates · enum_values · duplicates×2 │
                    └──────────────────────────────────────┘
                                       │
              ┌────────────────────────┴───────────────────────┐
              ▼                                                  ▼
    ┌───────────────────┐                              ┌──────────────────────┐
    │   RemediationRun    │  (1 per TaskRun, summary)    │  RemediationChange     │  (N per run, proposal)
    │  - counts            │                              │  - source_issue_id      │
    │  - changes_by_action  │◄─────────  changes  ────────│  - row/col/action        │
    │  - skipped_by_reason   │                              │  - original/proposed      │
    │  - engine_version        │                              │  - reason                   │
    └────────────┬──────────────┘                              └──────────────────────────┘
                 │
                 ▼
    GET .../remediation           (summary — cheap aggregates + 1 join + 1 GROUP BY)
    GET .../remediation/changes   (paginated, filterable, tenant-scoped, DTOs only)
```

## 6. Complete Deterministic Remediation Workflow

1. A `REMEDIATE` `TaskRun` is created with `source_task_run_id` pointing at a completed `DETECT` `TaskRun`.
2. The worker claims it and dispatches to `RemediationHandler`.
3. The handler resolves the upstream `IssueDetectionRun`, scoped to the same `organization_id` — missing, wrong-org, or wrong-type are all indistinguishable permanent failures.
4. If `IssueDetectionRun.source_sha256` is NULL (pre-hash-tracking data) or the handler's fresh read of the source CSV hashes differently, the run fails permanently with a distinct message for each case — remediation never proceeds against a file that isn't provably the one Module 14 scanned.
5. If a `RemediationRun` already exists for this `task_run_id` (retry), it's returned as-is — no re-read, no re-compute.
6. Persisted `Issue` rows for the upstream run are loaded, plus per-column (`RemediationColumnRule`) and per-dataset (`RemediationDatasetConfig`) configuration, data-source-specific overriding org-wide.
7. The pure `remediate()` engine sorts issues deterministically, dispatches each to its registered `RemediationRule`, and returns a `RemediationResult` (changes + skipped, both sorted, both exact counts).
8. The handler persists one `RemediationRun` (aggregate counts, `changes_by_action`, `skipped_by_reason`, `remediation_engine_version`) and up to `REMEDIATION_MAX_PERSISTED_CHANGES` `RemediationChange` rows, inside a transaction with IntegrityError-catch-and-refetch for idempotency.
9. Nothing is ever applied, approved, or written back to source data — the workflow ends here in this module.

## 7. Remediation Actions (10)

| Action | Issue Type(s) | Logic | Reuses |
|---|---|---|---|
| `trim_whitespace` | LEADING/TRAILING_WHITESPACE | Uses Module 14's own `Issue.suggested_fix` verbatim | Module 14 |
| `collapse_multiple_spaces` | MULTIPLE_INTERNAL_SPACES | Uses Module 14's own `Issue.suggested_fix` verbatim | Module 14 |
| `standardize_capitalization` | INCONSISTENT_CAPITALIZATION | Applies configured `capitalization_target` (`lower`/`upper`/`title`) | Module 7 `casing.py` (fixed Phase 5) |
| `normalize_boolean` | BOOLEAN_INCONSISTENCY | Canonicalizes recognized boolean tokens | Module 6/7 `standardize_boolean` |
| `normalize_phone` | INVALID_PHONE | E.164 normalization via configured `default_country` | Module 6/7 `standardize_phone` |
| `normalize_numeric` | INVALID_NUMERIC | Locale-aware numeric coercion | Module 6/7 `standardize_numeric` |
| `normalize_date` | INVALID_DATE | Parses against required `source_date_format`, renders via required `target_date_format` | New (no existing equivalent) |
| `normalize_enum_value` | INVALID_ENUM_VALUE | Case/whitespace-insensitive match against exactly one `allowed_values` candidate | New (narrower than Module 14's own check) |
| `remove_duplicate_row` | DUPLICATE_ROW | Opt-in exclusion proposal, no value | New (re-identifies nothing) |
| `remove_duplicate_primary_key` | DUPLICATE_PRIMARY_KEY | Opt-in exclusion proposal, no value | New (re-identifies nothing) |

## 8. Skip Reasons (10, three families)

**Structural (2)** — the Issue can't be acted on regardless of configuration: `issue_type_not_remediable`, `row_not_found_in_dataset`.

**Missing configuration (6)** — a required gate wasn't satisfied (never guessed): `capitalization_target_not_configured`, `source_date_format_not_configured`, `target_date_format_not_configured`, `default_country_not_configured`, `allowed_values_not_configured`, `duplicate_removal_not_enabled`.

**No safe change (2)** — the gate was satisfied but no change was warranted: `no_deterministic_change_available` (already conforms, or too ambiguous to touch), `persisted_change_limit_reached` (defensive ceiling hit).

## 9. API Endpoints

- **`GET /tasks/{task_id}/runs/{run_id}/remediation`** — summary: run metadata, `issues_considered_count`/`total_changes_count`/`issues_skipped_count`, `changes_by_action`/`changes_by_column`/`skipped_by_reason`, `remediation_engine_version`, `dataset_sha256` (joined from upstream `IssueDetectionRun`), `processing_duration_ms` (from `TaskRun.started_at`/`finished_at`). 404 if not visible to the caller's org or no remediation result exists yet.
- **`GET /tasks/{task_id}/runs/{run_id}/remediation/changes`** — paginated (`limit`/`offset`), filterable by `action`/`column_name`/`row_number` (individually or combined), stable `(row_number, id)` ordering, `RemediationChangeRead` DTOs only. `total_changes_count` on the summary endpoint always reflects every persisted change, independent of any filter applied here.

Both are read-only; no write, approval, or apply endpoint exists anywhere in this module.

## 10. Configuration Options

- **`RemediationColumnRule`** (data-source-specific overrides org-wide, per `column_name`): `source_date_format`, `target_date_format`, `default_country` (ISO-3166 alpha-2), `capitalization_target` (`lower`/`upper`/`title`).
- **`RemediationDatasetConfig`** (one per data source): `remove_duplicate_rows_enabled` (default `False`), `remove_duplicate_primary_keys_enabled` (default `False`).
- **`REMEDIATION_MAX_PERSISTED_CHANGES`** (`settings.remediation_max_persisted_changes`, default `10,000`) — defensive ceiling on persisted `RemediationChange` rows per run; not expected to bind in practice since `issues_considered_count` is already bounded by Module 14's own `ISSUE_DETECTION_MAX_PERSISTED_ISSUES`.

None of these tables have a CRUD API yet — configured by direct row insertion, matching `IssueDetectionColumnRule`'s own pre-API state after Module 14 Phase 1.

## 11. Known Limitations

- No approval, apply, or rollback of any kind — proposing and applying a correction are two separate future decisions; only the first exists in this module.
- No CRUD API for `RemediationColumnRule`/`RemediationDatasetConfig`.
- No `skip_reason` filter on the changes endpoint (skipped Issues are never persisted as rows — see the Phase 4 `AskUserQuestion` decision).
- Date normalization requires both formats explicitly configured — never inferred.
- Capitalization "title" target is deliberately not locale-aware (no special-casing of "McDonald"/"O'Brien"), matching Module 7's own named scope limit.
- No bulk-action endpoints, no CSV/export surface.

## 12. Technical Debt

None identified as blocking. The one substantive item — the capitalization duplication/bug — was fixed within this phase, not deferred.

## 13. Recommended Improvements Before Module 16

- Consider a CRUD API for `RemediationColumnRule`/`RemediationDatasetConfig` before or alongside Module 16, since an approval workflow will make these configuration tables more actively tuned by end users than they've needed to be so far.
- No other changes recommended — Module 15's read-only, propose-only scope is intentionally minimal and complete for what it claims to do.

## 14. Readiness Assessment

**100%** ready for merge, pending explicit product-owner approval. All ten Phase 5 verification dimensions pass, the full regression suite is green (825 passed / 8 skipped, 0 failed), the migration cycle is clean in both directions, and the one genuine defect found during review (capitalization apostrophe bug) has been fixed and covered by a new regression test. No blocking gaps identified.

---

**Module 16 (Approval Queue) has not been started.** Awaiting review and approval of this report before any further work begins.
