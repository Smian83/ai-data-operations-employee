# Module 15 — Deterministic Cleaning Engine (Remediation Engine)

Status: **ARCHITECTURE APPROVED. Implementation not yet started — no code
written.** This revision incorporates all 10 approved decisions from
architecture review. Superseded content from the draft (open questions,
nullable `source_issue_id`, business rules, email-casing, a materialized
output file) has been removed rather than marked historical, so this
document is the single current source of truth for Module 15.

## 1. What this module is

Module 15 consumes the `Issue` rows produced by one completed Module 14
`IssueDetectionRun` and, for each issue type that has a safe, deterministic,
non-guessing fix, computes and persists a **proposed** correction —
`original_value`, `proposed_value`, the rule applied, a deterministic
reason, and `confidence = 1.0`. It never writes a modified copy of the
dataset, never touches the source file, and has no approval workflow: the
persisted `RemediationChange` rows themselves are the deliverable, not an
input to a later apply/approve step. This mirrors Module 14's own shape
closely on purpose — Module 14 persists `Issue` rows without ever writing
an output file; Module 15 persists `RemediationChange` rows the same way.
Actually materializing a cleaned CSV from these proposals is explicitly not
part of this module (see Section 7, Deferred).

Wherever Modules 6, 7, or 9 already have a deterministic transformation
function for a fix Module 15 needs, that function is imported and called
directly — never reimplemented. New logic is added only where nothing
reusable exists (capitalization policy, enum-value normalization, date
parsing under an explicit configured format, and duplicate-row/key
proposal-only exclusion).

## 2. Two required, additive touches to Module 14

Two decisions below require small, backward-compatible schema changes to
tables Module 14 already owns. Both are additive (new nullable column / new
constraint), neither changes any existing behavior or breaks any existing
row, and both are called out explicitly here rather than folded silently
into "new Module 15 tables," per the standing "do not modify previous
modules unless absolutely required" rule.

1. **`issue_detection_runs.source_sha256`** (new, nullable `String(64)`).
   Decision 4 requires Module 15 to verify it is remediating the *same*
   dataset version Module 14 scanned, not just the same `data_source_id`.
   `app.profiling.csv_loader.load_csv` already computes a `source_sha256`
   for every file it loads (`LoadedCsv.source_sha256`) — `DataProfile`
   already stores it (Module 5); `IssueDetectionRun` currently does not.
   Adding it is a one-line change to `IssueDetectionHandler` (pass
   `loaded.source_sha256` through, a value it already computes) plus the
   migration. Existing `IssueDetectionRun` rows get `NULL` — Module 15
   treats a `NULL` upstream hash as "predates version tracking, re-run
   detection before remediation," a permanent failure with a clear message,
   never a silent skip of the check.
2. **`UniqueConstraint(organization_id, id)` on `issues`.** This project's
   own established rule (learned the hard way in Module 6: "add it at the
   same time the table is created, not after the first FK-target failure
   surfaces") requires any table that will be a composite-FK *target* to
   carry this constraint. `RemediationChange.source_issue_id` is a required
   FK into `issues` (decision 6), so `issues` needs it now. It does not
   exist today because Module 14 never needed `Issue` to be an FK target.

Both are one Alembic migration, additive only, run as part of Module 15
Phase 1. No other Module 14 file changes.

## 3. Data model

### `TaskType.REMEDIATE`
New native Postgres enum value (`ALTER TYPE task_type_enum ADD VALUE
'remediate'`), same pattern as `standardize`/`match`.

### `RemediationRun`
One row per REMEDIATE `TaskRun`. No approval fields at all (no `status`,
no `approved_by`/`rejected_by`/`rolled_back_by`) — direct structural
sibling of `IssueDetectionRun`, not of `CleaningRun`.

- `id`, `organization_id`, `task_run_id`, `task_id`, `data_source_id`
- `source_task_run_id` — the Module 14 `IssueDetectionRun`'s own
  `TaskRun.id` (same "denormalized from `TaskRun.source_task_run_id`"
  convention every prior chained module uses)
- `issues_considered_count` — how many `Issue` rows were available to act
  on (bounded by whatever Module 14 actually persisted for that run;
  see Risk R6)
- `total_changes_count` — how many resulted in a `RemediationChange` (every
  considered `Issue` yields at most one `RemediationChange`, since
  `source_issue_id` is required and 1:1)
- `issues_skipped_count` — considered but no safe deterministic fix applied
  (unconfigured column, ambiguous input, etc.) — stored explicitly rather
  than left as an implied `considered - changed` subtraction, matching
  every other run table's "the summary is always pre-computed and exact"
  convention
- `changes_by_action` (JSON, `{action: count}`)
- `remediation_engine_version`
- `created_at`

No `output_file_path`, `output_sha256`, or `row_count` — there is no
output file (Section 1).

### `RemediationChange`
Append-only, immutable, one row per applied proposal.

- `id`, `organization_id`, `remediation_run_id`
- `source_issue_id` — **required** (`NOT NULL`), composite FK
  `(organization_id, source_issue_id) -> issues(organization_id, id)`.
  Every `RemediationChange` traces to exactly one Module 14 `Issue`; no
  change is ever created without one (decision 6).
- `row_number`, `column_name` (nullable — `NULL` for whole-row proposals:
  `remove_duplicate_row`/`remove_duplicate_primary_key`)
- `action` — closed vocabulary, one `CHECK` constraint: `trim_whitespace`,
  `collapse_multiple_spaces`, `standardize_capitalization`,
  `normalize_boolean`, `normalize_date`, `normalize_phone`,
  `normalize_numeric`, `normalize_enum_value`, `remove_duplicate_row`,
  `remove_duplicate_primary_key`
- `original_value` (nullable — `NULL` only for `remove_duplicate_row`,
  matching `Issue.original_value`'s own nullability for that issue type)
- `proposed_value` (nullable — `NULL` for the two removal actions, which
  propose *exclusion*, not a replacement value; named `proposed_value`
  rather than `cleaned_value` deliberately, so the column name itself
  reflects decision 5 — nothing here has been applied)
- `reason` (text, deterministic, mirrors `CleaningChange.reason`)
- `confidence` (`Float`, `CHECK (confidence = 1.0)` — rule-based only, per
  the requirement; stored as a real column rather than hardcoded so the
  schema shape stays consistent with `CleaningChange`/`StandardizationChange`
  for any future shared API/reporting code)
- `created_at`

### `RemediationColumnRule`
Organization-configured, per-column governance — structural sibling of
`IssueDetectionColumnRule` (same data-source-specific-overrides-org-wide,
two-partial-unique-index pattern on `lower(trim(column_name))`). Holds only
the configuration Module 14's own `IssueDetectionColumnRule` doesn't already
capture (enum normalization reuses `IssueDetectionColumnRule.allowed_values`
directly, read-only — no duplication):

- `id`, `organization_id`, `data_source_id` (nullable = org-wide),
  `column_name`
- `source_date_format` (nullable `String` — a `strptime` format string,
  e.g. `"%m/%d/%Y"`; decision 10 — required before any `normalize_date`
  proposal is ever generated for that column)
- `default_country` (nullable `String(2)` — ISO 3166-1 alpha-2; required
  before any `normalize_phone` proposal, same "never guess a country"
  reasoning Module 7 already established)
- `capitalization_target` (nullable: `lower`/`upper`/`title`; required
  before any `standardize_capitalization` proposal — Module 14's own
  "dominant pattern" is never treated as correct automatically)
- `is_active`, `created_by`, `created_at`

### `RemediationDatasetConfig`
Deliberately a **separate**, smaller table from `RemediationColumnRule` —
duplicate-row/key removal is a whole-dataset decision, not a per-column
one, so folding it into the column-scoped table would overload that
table's unique-index semantics with a second, unrelated nullability
dimension.

- `id`, `organization_id`, `data_source_id` (required — no org-wide
  fallback; removal is consequential enough that it must be opted into
  per data source explicitly, not inherited from a blanket org default)
- `remove_duplicate_rows_enabled` (`bool`, default `False`)
- `remove_duplicate_primary_keys_enabled` (`bool`, default `False`)
- `is_active`, `created_by`, `created_at`
- `UniqueConstraint(organization_id, data_source_id)`

Absence of a row for a given `data_source_id` means both flags are `False`
— no proposal for either removal action is ever generated.

## 4. Requirement-to-implementation mapping (final)

| Action | Consumes issue_type | Logic | Gate |
|---|---|---|---|
| Trim whitespace | `leading_whitespace`, `trailing_whitespace` | `Issue.suggested_fix` (already computed by Module 14 — zero recomputation, zero drift risk) | none — always proposed |
| Collapse multiple spaces | `multiple_internal_spaces` | `Issue.suggested_fix` | none — always proposed |
| Standardize capitalization | `inconsistent_capitalization` | New: apply configured target case via `str.lower()`/`.upper()`/`.title()` | `RemediationColumnRule.capitalization_target` must be set |
| Normalize booleans | `boolean_inconsistency` | `app.standardization.rules.values.standardize_boolean(value, output_form=None)` (reused, Module 7) | none — always proposed when the function returns a change |
| Normalize dates | `invalid_date` | New: single deterministic `datetime.strptime(value, source_date_format)` then `.isoformat()` — **not** `standardize_date` reuse; see R1 | `RemediationColumnRule.source_date_format` must be set |
| Normalize phone numbers | `invalid_phone` | `app.standardization.rules.contact.standardize_phone(value, country)` (reused, Module 7) | `RemediationColumnRule.default_country` must be set |
| Normalize numeric formatting | `invalid_numeric` | `app.standardization.rules.values.standardize_numeric(value, locale=None)` (reused, Module 7) | none — function itself is conservative |
| Normalize enum values | `invalid_enum_value` | New: case/whitespace-insensitive match against `IssueDetectionColumnRule.allowed_values` (read directly, not duplicated); proposed only when exactly one case-insensitive match exists | `IssueDetectionColumnRule.allowed_values` must already be configured (it is, by construction — that's what produced the issue) |
| Remove duplicate rows | `duplicate_row` | Propose exclusion of the exact `row_number` Module 14 already identified — no re-detection | `RemediationDatasetConfig.remove_duplicate_rows_enabled = true` |
| Remove duplicate primary keys | `duplicate_primary_key` | Propose exclusion of the exact `row_number` Module 14 already identified | `RemediationDatasetConfig.remove_duplicate_primary_keys_enabled = true` |

**Explicitly excluded from this module** (all confirmed by decisions
7–9): organization-specific business rules (deferred to Module 21), email
casing normalization (no Module 14 issue type maps to it — decision 8),
null-value normalization (Module 14 detects `null_value` but there is no
"explicit configured replacement" concept for it today — decision 9). Issue
types with no action in scope, detected-only by design: `missing_value`,
`empty_string`, `required_field_violation`, `outlier`,
`broken_fk_reference`.

## 5. Engine shape

`app/remediation/engine.py::remediate(dataset, issues, column_config,
dataset_config) -> RemediationResult` — pure, no I/O, same shape as
`app.cleaning.engine.clean` / `app.detection.engine.detect_issues`. One
small pure function per action under `app/remediation/actions/`
(`trim.py`, `collapse.py`, `capitalization.py`, `booleans.py`, `dates.py`,
`phones.py`, `numeric.py`, `enum_values.py`, `duplicates.py`), each either
calling the reused Module 6/7 function directly or containing the small
amount of genuinely new logic — never a second implementation of anything
that already exists. Because nothing is materialized, every action reads
`original_value` from the `Issue` itself (not from a running "current
state" of the row), so there is no ordering dependency between actions —
a cell-level proposal and a row-removal proposal on the same row never
interact or need to be sequenced.

`RemediationHandler` (`app/worker/handlers/remediation.py`, registered for
`TaskType.REMEDIATE`) is the only impure layer:

1. Resolve the upstream `IssueDetectionRun` via `source_task_run_id`,
   scoped to `organization_id` (decision 4). Missing → permanent failure.
2. Reject if `IssueDetectionRun.source_sha256 IS NULL` (predates tracking —
   permanent failure, explicit message).
3. Re-read the current source file (same tenant-scoped
   `resolve_source_path`/`load_csv` Module 14 already uses) and compare its
   freshly computed `sha256` to `IssueDetectionRun.source_sha256`. Mismatch
   → permanent failure ("source data has changed since detection; re-run
   detection before remediation").
4. Fetch the `Issue` rows for that `IssueDetectionRun`.
5. Resolve `RemediationColumnRule` (data-source-specific overriding
   org-wide, same resolution order as every prior config table) and
   `RemediationDatasetConfig` for this `data_source_id`.
6. Call the pure engine, persist `RemediationRun` + `RemediationChange`
   rows in one short transaction (same unique-`task_run_id`,
   `IntegrityError`-catch-and-refetch idempotency pattern every handler
   uses).

## 6. Phase plan

**Phase 1 — Models, migration, config.** `TaskType.REMEDIATE`;
`RemediationRun`, `RemediationChange`, `RemediationColumnRule`,
`RemediationDatasetConfig` models; the one additive Module 14 migration
(Section 2: `issue_detection_runs.source_sha256` +
`UniqueConstraint(organization_id, id)` on `issues`); the one-line
`IssueDetectionHandler` change to populate the new column; new
`RemediationLimits`-style config settings (max persisted changes, same
bounded-cap convention every prior module uses). Model + migration tests
only.

**Phase 2 — Pure remediation engine.** All 10 action functions,
`remediate()` orchestration, deterministic sort/aggregation, unit tests per
action (including the reused-function edge cases each already documents),
repeatability tests. No handler, no API yet.

**Phase 3 — RemediationHandler + worker registration.** Source-version
verification (Section 5, steps 1–3), config resolution, engine invocation,
persistence, registry entry for `TaskType.REMEDIATE`. Handler integration
tests: idempotency, tenant isolation, stale-source-hash rejection,
missing-upstream-run rejection, `NULL`-hash rejection, confirmation the
source file is never opened for writing.

**Phase 4 — Read-only API (proposed, confirm before starting).**
`GET .../remediation` (summary — cheap, precomputed counts only) and
`GET .../remediation/changes` (paginated, filterable by
`action`/`column_name`/`row_number`, same declarative filter-column
mapping convention as Module 14 Phase 3). Not in the original requirement
list; flagged as its own phase so Phases 1–3 can ship and be reviewed
independently of it.

**Phase 5 — Verification, docs, report.** Full regression suite, SQLite +
PostgreSQL migration cycle (covering both the new Module 15 tables and the
additive Module 14 change), `PROJECT_CONTEXT.md`/README updates, final
report.

Each phase stops for review before the next begins.

## 7. Deferred / explicitly out of scope

- Organization-specific business rules — Module 21.
- Email-casing normalization, null-value normalization — no qualifying
  Module 14 issue mapping exists today (decisions 8–9); revisit if/when one
  does.
- **Materializing an actual cleaned CSV file from `RemediationChange`
  proposals.** Decision 5 scopes this module to computing and storing
  proposals only. Producing a real output file from an accepted set of
  proposals is a distinct future capability (an "apply" step of some kind)
  deliberately not built here, consistent with "human approval" and
  "export improvements" both being out of scope for Module 15 itself.
- Any approval/rollback/apply workflow.

## 8. Risks

- **R1 — Date parsing must not guess.** `standardize_date` (Module 7) and
  Module 14's own `invalid_date` detector are both strictly
  `fromisoformat`-based, so reusing `standardize_date` verbatim would
  correctly no-op on nearly everything Module 14 flags — it isn't a
  reusable fix for this issue type. Resolved by decision 10: a new, narrow
  `strptime`-based function using only the column's explicitly configured
  `source_date_format`. No multi-format "try a few things" fallback is
  built, ever — that is exactly the MM/DD-vs-DD/MM guessing trap this
  project has consistently avoided elsewhere.
- **R2 — Phone remediation only fires with a configured `default_country`.**
  Module 14's `invalid_phone` detector requires a leading `+` to attempt
  validation at all, so most flagged values carry no country signal.
  Expected, not a bug — most `invalid_phone` issues will be correctly
  skipped unless the column has a configured country.
- **R3 — Stale-source-hash rejection makes Module 15 more brittle to
  upstream re-syncs than other chained modules.** Modules 7/8/9 tie to an
  *approved* run, which is a human-gated point-in-time guarantee. Module 15
  ties to a *file content hash*, which is stricter: if the source file is
  re-synced even byte-identically-in-content-but-different-encoding, or
  genuinely changed, remediation fails permanently until detection re-runs.
  This is the correct behavior per decision 4, but should be documented
  clearly as an operational expectation, not discovered by surprise.
- **R4 — Confidence is fixed at 1.0 by `CHECK` constraint.** This is a
  strong, deliberate simplification: unlike Module 6/7 (which vary
  confidence per rule, e.g. date reparsing at 0.7), Module 15 provides no
  signal at all about "safer" vs. "riskier" proposals within the same run.
  This is what was asked for ("confidence = 100%, rule-based only") — flagged
  simply so it's understood as an intentional flattening, not an oversight.
- **R5 — Duplicate `IssueDetectionRun` re-consumption.** A second
  `RemediationRun` against a different, later `IssueDetectionRun` for the
  same `data_source_id` is simply a new, independent run — same
  per-`TaskRun` idempotency guarantee every handler already provides, no
  special reconciliation between the two `RemediationRun`s.
- **R6 — Capped `Issue` rows.** Module 14 persists at most
  `issue_detection_max_persisted_issues` `Issue` rows per run even though
  `total_issues_found` may be larger. `RemediationRun.issues_considered_count`
  reflects only what was actually persisted and fetchable — never silently
  implies full coverage of `total_issues_found`.

## 9. Edge cases

- **Zero-issue `IssueDetectionRun`.** Still produces a `RemediationRun`
  (idempotent-per-`TaskRun` guarantee preserved) with
  `issues_considered_count = 0`, `total_changes_count = 0`.
- **An `Issue` whose reused function returns "unchanged."** Recorded as
  skipped (`issues_skipped_count` increments), not silently dropped —
  visible in the run summary as a real, expected outcome, not
  indistinguishable from "nothing to do."
- **A configured `capitalization_target`/`source_date_format`/
  `default_country` that itself produces no change** (value already
  conforms). No `RemediationChange` row — same "no-op means no `Change`
  row" convention every rule engine in this codebase already follows.
- **A column with no configured `RemediationColumnRule` at all.** Every
  `inconsistent_capitalization`/`invalid_date`/`invalid_phone` `Issue` for
  that column is skipped, even though Module 14 already detected it —
  never guessed, matching Module 14's own "never guess a column's meaning"
  principle one layer downstream.
- **Multi-column primary keys.** Module 15 does not re-derive which
  columns form the key — it consumes Module 14's already-computed
  `duplicate_primary_key` `Issue` rows verbatim (which already encode the
  composite key via `column_name = "col_a+col_b"`), proposing exclusion of
  exactly the flagged `row_number`.
- **`RemediationDatasetConfig` row missing for a `data_source_id`.** Both
  removal actions are simply never proposed — not an error, the documented
  default-off behavior.
- **Historical `IssueDetectionRun` rows with `source_sha256 IS NULL`**
  (created before this module's migration). Always a permanent failure
  with an explicit, actionable message — never silently treated as "hash
  check passed" or "hash check skipped."

## 10. Test strategy

Same three-layer approach as every prior module:

- **Unit tests per action** — conforming input, no-op/already-correct
  input, the gate itself absent (must skip, never guess), and the specific
  edge cases each reused Module 6/7 function documents (numeric locale
  ambiguity, phone country resolution, etc.) plus the new logic's own
  cases (capitalization target absent, enum case-insensitive match
  finding zero or more-than-one candidate, strptime format mismatch).
- **Engine integration tests** — multi-action datasets, deterministic
  aggregation (`changes_by_action`, `issues_skipped_count`), the
  capped-Issue-count interaction (R6), zero-issue no-op runs,
  repeatability (identical `Issue` set + config → byte-identical
  `RemediationChange` list across repeated calls).
- **Handler integration tests** — real CSV files, real
  `IssueDetectionRun`/`Issue` fixtures with a real `source_sha256`; a
  dedicated test that mutates the source file after detection and confirms
  remediation is rejected (R3/decision 4's core guarantee); tenant
  isolation; idempotent retries; `NULL`-hash rejection; confirmation the
  source file's hash is identical before and after a remediation run (it
  is never opened for writing at all).
- **Migration tests** — the additive `issue_detection_runs.source_sha256`
  column and the new `issues` unique constraint, confirmed not to break any
  existing Module 14 model test or constraint.
- **API tests** (Phase 4, if approved) — same pagination/filter/404/tenant
  coverage as Module 14 Phase 3, applied to the new endpoints.
- **Full regression suite + SQLite/PostgreSQL migration cycle** before
  every phase's stop-and-report.
