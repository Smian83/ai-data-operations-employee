# Module 18 — Quality Control Engine: Architecture Design (Rev 2)

**Status:** Architecture draft (Rev 2) — awaiting approval before any code is written.  
**Author:** AI Data Operations Employee  
**Depends on:** Modules 14 (Issue Detection), 15 (Remediation), 16 (Approval Queue), 17 (Validation)  
**Feeds into:** Module 19 (Clean Export Engine)

---

## 1. Architecture Overview

Module 18 is the final dataset-level quality gate before a cleaned dataset may be released via Module 19. It evaluates the **post-remediation, post-validation working dataset** — not the raw pre-remediation source. It never re-reads source CSV files. It derives authoritative post-change statistics from the persisted audit trail produced by Modules 15–17, then evaluates them against configurable quality thresholds.

The module follows the established three-layer pattern without exception:

```
app/quality/engine.py                  — pure function, no I/O, deterministic
app/worker/handlers/quality_control.py — impure handler, all DB access, one transaction
app/api/tasks.py (additions)           — read-only GET endpoints, no mutations
```

All quality computations are deterministic. Given the same inputs at any future point, the same `overall_score` and `release_recommendation` are reproduced. The `execution_snapshot` field on every `QualityControlRun` row records every input — IDs, versions, weights, thresholds — used to produce the decision.

---

## 2. Pipeline Placement

```
SYNC          → DataProfile (raw baseline statistics)
DETECT        → IssueDetectionRun + Issue rows
REMEDIATE     → RemediationRun + RemediationChange rows (proposals)
(human gate)  → RemediationChangeDecision rows (Module 16)
VALIDATE      → ValidationRun + ValidationResult rows (per-change verdicts)
QUALITY_CTRL  → QualityControlRun + QualityFinding rows   ← Module 18
(future)      EXPORT_CLEAN → cleaned output CSV            ← Module 19
```

`source_task_run_id` on the QUALITY_CTRL `TaskRun` must point to the completed VALIDATE `TaskRun`. The same chain rule every prior module uses.

**Module 19 consumption**: `ExportCleanHandler` (Module 19) queries `QualityControlRun WHERE validation_run_id = ? AND organization_id = ?`. If no row exists, or `release_recommendation = "FAIL"`, or `overall_score IS NULL`, the handler raises `PermanentExecutionError`. `"PASS"` and `"PASS_WITH_WARNINGS"` both permit export; the recommendation and overall score are recorded on the export run for traceability.

---

## 3. Authoritative Post-Remediation Dataset State

### 3.1 Design choice: computed effective-dataset statistics (Design A)

Module 18 does **not** use the original `DataProfile` as a proxy for the cleaned dataset. The original `DataProfile` was computed from the raw, un-remediated source file. Using it for quality evaluation would assess a dataset that no longer reflects what Module 15 proposed and Module 16 approved.

Instead, the handler computes **effective post-remediation statistics** by applying deltas from the approved-and-validated `RemediationChange` set on top of the `DataProfile` baseline. These computed statistics are stored as JSON in `QualityControlRun.post_remediation_stats` and are the sole source of truth for completeness, uniqueness, and per-column category evaluation.

### 3.2 What the handler computes (handler Step 3h — see Section 9)

Inputs: `DataProfile` (baseline) + all `RemediationChange` rows whose linked `ValidationResult.outcome = "passed"` (approved and validated).

```
effective_row_count =
    DataProfile.row_count
    − count(passed changes where action IN ("remove_duplicate_row",
                                            "remove_duplicate_primary_key"))

effective_duplicate_row_count =
    DataProfile.duplicate_row_count
    − count(passed changes where action IN ("remove_duplicate_row",
                                            "remove_duplicate_primary_key"))
    (floor 0 — cannot go below zero)

effective_missing_value_total =
    DataProfile.missing_value_total
    − count(passed changes where linked Issue.issue_type IN
            ("missing_value", "empty_string", "null_value"))
    (floor 0)

applied_changes_by_action =
    {action: count} for passed changes — mirrors RemediationRun.changes_by_action
    but restricted to those that also passed validation

applied_changes_by_column =
    {column_name: count} for passed changes — mirrors RemediationRun.changes_by_column

addressed_issue_ids =
    set of Issue IDs that have a passed ValidationResult (via source_issue_id linkage)
    — used by unresolved_risk category

unresolved_issue_ids =
    all HIGH/CRITICAL Issue IDs NOT in addressed_issue_ids
    — stored in snapshot for auditability
```

The cross-reference from `RemediationChange.source_issue_id` to `Issue.issue_type` is the mechanism that determines which changes addressed missing-value issues. This uses data already loaded in the batch load step — no extra queries.

### 3.3 Stored as `post_remediation_stats` JSON on `QualityControlRun`

```json
{
  "stats_schema_version": "1.0",
  "stats_generated_at": "<ISO-8601 UTC timestamp>",
  "stats_source": "computed_from_data_profile_delta",
  "data_profile_id": "<uuid>",
  "data_profile_source_sha256": "<hex>",
  "baseline_row_count": 10000,
  "baseline_duplicate_row_count": 200,
  "baseline_missing_value_total": 450,
  "effective_row_count": 9820,
  "effective_duplicate_row_count": 20,
  "effective_missing_value_total": 390,
  "applied_changes_by_action": {"trim_whitespace": 60, "remove_duplicate_row": 180},
  "applied_changes_by_column": {"email": 40, "phone": 20},
  "addressed_issue_count": 230,
  "unresolved_high_count": 0,
  "unresolved_critical_count": 0
}
```

**Provenance fields:**

`stats_schema_version`: version of this JSON schema (currently `"1.0"`). Allows forward-compatible schema evolution without a column migration.

`stats_generated_at`: ISO-8601 UTC timestamp at which these statistics were computed by the handler. Together with `execution_snapshot.frozen_at` this gives a full audit trail of when each stage of the decision was produced.

`stats_source`: how these statistics were produced. Currently always `"computed_from_data_profile_delta"` (baseline DataProfile adjusted by approved+validated RemediationChange deltas). Reserved for future computation strategies without a schema change.

`data_profile_source_sha256` is copied from `DataProfile.source_sha256` to establish which exact file version was the baseline. An auditor can verify the chain: file hash → DataProfile → effective stats → QC decision.

### 3.4 Missing post-remediation statistics is a permanent failure

If the handler cannot resolve `DataProfile` (the required baseline), it raises `PermanentExecutionError`. This is not a skippable condition. Without the baseline there is no way to compute effective statistics, and without effective statistics `completeness`, `uniqueness`, and `unresolved_risk` cannot be evaluated. The dataset must not receive a quality recommendation without this evidence.

This applies regardless of which categories are configured — the DataProfile must always be resolved before the engine is called.

---

## 4. Data Model

### 4.1 New ORM models

```
quality_control_runs   — one row per QUALITY_CTRL TaskRun, one per ValidationRun
quality_findings       — one row per finding, bounded per run, append-only
quality_thresholds     — configurable release thresholds (org-wide or data-source-specific)
```

No `quality_business_rules` table. See Section 5 (Business Rule Scope).

---

### 4.2 `QualityControlRun`

```python
class QualityControlRun(Base):
    __tablename__ = "quality_control_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_quality_control_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_quality_control_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_quality_control_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "validation_run_id"],
            ["validation_runs.organization_id", "validation_runs.id"],
            name="fk_quality_control_runs_org_validation_run",
            ondelete="RESTRICT",
        ),
        # Idempotency: one QC run per QUALITY_CTRL TaskRun execution.
        UniqueConstraint("task_run_id", name="uq_quality_control_runs_task_run_id"),
        # One authoritative release decision per ValidationRun — two
        # QUALITY_CTRL TaskRuns pointing at the same ValidationRun must
        # produce the same row, not competing decisions.
        UniqueConstraint(
            "validation_run_id", name="uq_quality_control_runs_validation_run_id"
        ),
        # Required so QualityFinding can reference this table via a composite FK.
        UniqueConstraint("organization_id", "id", name="uq_quality_control_runs_org_id"),
        # overall_score is nullable: NULL when no categories are applicable.
        # When non-null, it must be in [0, 100].
        CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0.0 AND overall_score <= 100.0)",
            name="ck_quality_control_runs_score_range",
        ),
        CheckConstraint(
            "total_findings >= 0",
            name="ck_quality_control_runs_total_findings_nonneg",
        ),
        CheckConstraint("blocking_count >= 0", name="ck_quality_control_runs_blocking_nonneg"),
        CheckConstraint("warning_count >= 0",  name="ck_quality_control_runs_warning_nonneg"),
        CheckConstraint("info_count >= 0",     name="ck_quality_control_runs_info_nonneg"),
        CheckConstraint(
            "blocking_count + warning_count + info_count = total_findings",
            name="ck_quality_control_runs_counts_reconcile",
        ),
        CheckConstraint(
            "release_recommendation IN ('PASS', 'PASS_WITH_WARNINGS', 'FAIL')",
            name="ck_quality_control_runs_recommendation_valid",
        ),
    )

    id:                     UUID (pk, default uuid4)
    organization_id:        UUID (FK organizations.id CASCADE, not null, index)
    task_run_id:            UUID (not null, index)
    task_id:                UUID (not null, index)
    data_source_id:         UUID (not null, index)
    validation_run_id:      UUID (not null, index)   # primary input

    # Denormalized upstream IDs resolved at handler time — stored for
    # direct API access and auditing without re-traversing the TaskRun chain.
    remediation_run_id:         UUID (not null, index)
    issue_detection_run_id:     UUID (not null, index)
    data_profile_id:            UUID (not null, index)

    quality_engine_version:     String(20) (not null)

    # NULL when no categories are applicable (see Section 11 — release rules).
    # Non-null means at least one category was evaluated.
    overall_score:              Float (nullable)

    release_recommendation:     String(30) (not null)
    # One of: "PASS" | "PASS_WITH_WARNINGS" | "FAIL"
    # Never derived from overall_score alone — see Section 8.

    total_findings:             Integer (not null)  # true total from engine
    blocking_count:             Integer (not null)
    warning_count:              Integer (not null)
    info_count:                 Integer (not null)

    # Per-category breakdown — stored as JSON so the summary endpoint
    # never needs to aggregate QualityFinding rows.
    category_scores:            JSON (not null)   # {category: float | null}
    category_statuses:          JSON (not null)   # {category: "passed"|"warning"|"failed"|"skipped"}
    category_weights_used:      JSON (not null)   # {category: weight} only for applicable categories

    # Computed effective post-remediation statistics (see Section 3).
    post_remediation_stats:     JSON (not null)

    # Full deterministic snapshot of every input used to produce this
    # decision — see Section 7 for exact schema.
    execution_snapshot:         JSON (not null)

    created_at:                 DateTime (server_default=func.now(), not null)
```

**No `processing_duration_ms` column** — derived at the API layer from `TaskRun.started_at / finished_at`, never stored. Identical rationale to `RemediationRun` and `ValidationRun`.

**Indexes** (all named, ≤ 63 chars):
- `ix_quality_control_runs_org_id` — on organization_id
- `ix_quality_control_runs_validation_run_id` — on validation_run_id (covered by UNIQUE)
- `ix_quality_control_runs_recommendation` — on release_recommendation

---

### 4.3 `QualityFinding`

```python
class QualityFinding(Base):
    __tablename__ = "quality_findings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "quality_control_run_id"],
            ["quality_control_runs.organization_id", "quality_control_runs.id"],
            name="fk_quality_findings_org_qc_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_issue_id"],
            ["issues.id"],
            name="fk_quality_findings_source_issue",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_quality_findings_remediation_change",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "validation_result_id"],
            ["validation_results.organization_id", "validation_results.id"],
            name="fk_quality_findings_org_validation_result",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id", name="uq_quality_findings_org_id"),
        CheckConstraint(
            "severity IN ('info', 'warning', 'blocking')",
            name="ck_quality_findings_severity_valid",
        ),
        CheckConstraint(
            "outcome IN ('passed', 'failed', 'skipped')",
            name="ck_quality_findings_outcome_valid",
        ),
        CheckConstraint(
            "affected_row_count IS NULL OR affected_row_count >= 0",
            name="ck_quality_findings_affected_row_count_nonneg",
        ),
        Index(
            "ix_quality_findings_org_category_severity",
            "organization_id", "category", "severity",
        ),
    )

    id:                     UUID (pk, default uuid4)
    organization_id:        UUID (FK organizations.id CASCADE, not null, index)
    quality_control_run_id: UUID (not null, index)

    category:               String(50)  (not null)
    rule_name:              String(100) (not null)
    rule_version:           String(20)  (not null)  # from the QualityCategory's own constant

    severity:               String(20)  (not null)   # info | warning | blocking
    outcome:                String(20)  (not null)   # passed | failed | skipped
    reason:                 Text()      (not null)   # encryption-compatible, unbounded

    affected_row_count:     Integer     (nullable)
    affected_column:        String(255) (nullable)

    # Provenance links — all nullable. Only set when the finding can be
    # traced to a specific upstream row; never guessed.
    source_issue_id:        UUID (nullable)
    remediation_change_id:  UUID (nullable)
    validation_result_id:   UUID (nullable)

    quality_engine_version: String(20) (not null)
    created_at:             DateTime (server_default=func.now(), not null)
```

**Indexes**:
- `ix_quality_findings_org_id`
- `ix_quality_findings_quality_control_run_id`
- `ix_quality_findings_org_category_severity` (composite)
- `ix_quality_findings_outcome`
- `ix_quality_findings_created_at`

---

### 4.4 `QualityThreshold` (configuration)

```python
class QualityThreshold(Base):
    __tablename__ = "quality_thresholds"
    __table_args__ = (
        # Two-partial-unique-index pattern: data-source-specific overrides
        # org-wide. NULL != NULL under SQL semantics requires two separate
        # indexes, same as RemediationColumnRule and IssueDetectionColumnRule.
        Index(
            "uq_quality_thresholds_org_scope",
            "organization_id",
            unique=True,
            postgresql_where="data_source_id IS NULL",
        ),
        Index(
            "uq_quality_thresholds_org_ds_scope",
            "organization_id", "data_source_id",
            unique=True,
            postgresql_where="data_source_id IS NOT NULL",
        ),
        CheckConstraint(
            "fail_score_threshold >= 0.0 AND fail_score_threshold <= 100.0",
            name="ck_quality_thresholds_fail_score_range",
        ),
        CheckConstraint(
            "pass_score_threshold >= 0.0 AND pass_score_threshold <= 100.0",
            name="ck_quality_thresholds_pass_score_range",
        ),
        CheckConstraint(
            "pass_score_threshold > fail_score_threshold",
            name="ck_quality_thresholds_pass_gt_fail",
        ),
        CheckConstraint(
            "max_validation_failure_rate >= 0.0 AND max_validation_failure_rate <= 1.0",
            name="ck_quality_thresholds_failure_rate_range",
        ),
        CheckConstraint(
            "max_validation_skip_rate >= 0.0 AND max_validation_skip_rate <= 1.0",
            name="ck_quality_thresholds_skip_rate_range",
        ),
        CheckConstraint(
            "max_high_severity_unresolved >= 0",
            name="ck_quality_thresholds_high_sev_nonneg",
        ),
        CheckConstraint(
            "max_critical_severity_unresolved >= 0",
            name="ck_quality_thresholds_crit_sev_nonneg",
        ),
        CheckConstraint(
            "max_warnings_for_clean_pass >= 0",
            name="ck_quality_thresholds_max_warnings_nonneg",
        ),
    )

    id:                               UUID (pk, default uuid4)
    organization_id:                  UUID (FK organizations.id CASCADE, not null, index)
    data_source_id:                   UUID (nullable)  # null = org-wide default

    is_active:                        Boolean (not null, default True)

    # Score thresholds — pass_score_threshold MUST be strictly greater
    # than fail_score_threshold. Enforced by DB CHECK above and by handler
    # pre-engine validation (Section 9, Step 4).
    fail_score_threshold:             Float (not null, default 60.0)
    pass_score_threshold:             Float (not null, default 85.0)
    # Scores in [fail, pass) → PASS_WITH_WARNINGS.

    # Validation-outcome tolerances.
    max_validation_failure_rate:      Float (not null, default 0.0)   # fraction [0,1]
    max_validation_skip_rate:         Float (not null, default 0.5)   # fraction [0,1]

    # Unresolved-issue severity tolerances.
    max_high_severity_unresolved:     Integer (not null, default 0)   # count, ≥ 0
    max_critical_severity_unresolved: Integer (not null, default 0)   # count, ≥ 0

    # Warning count below which the decision can be a clean PASS.
    # 0 = any warning prevents PASS (→ PASS_WITH_WARNINGS instead).
    max_warnings_for_clean_pass:      Integer (not null, default 0)   # ≥ 0

    # Per-category weight overrides. Keys must be in QUALITY_CATEGORIES;
    # values must be positive integers. Missing keys use engine defaults.
    # Validated by handler before engine call — invalid config →
    # PermanentExecutionError.
    category_weights:                 JSON (nullable)

    created_at:                       DateTime (server_default=func.now(), not null)
    updated_at:                       DateTime (onupdate=func.now(), nullable)
```

**No `quality_business_rules` table.** See Section 5.

---

## 5. Business Rule Scope and Module 21 Deferral

### 5.1 What Module 18 V1 does not build

Module 21 is the designated module for a reusable, organization-configured Business Rules Engine. Introducing a generic `quality_business_rules` table in Module 18 would create a competing architecture that Module 21 must either duplicate or reconcile. This is explicitly avoided.

Module 18 V1 contains **no `quality_business_rules` table** and **no write API for custom business rules**. The `business_rule_compliance` quality category is registered as a named slot in the registry but is always skipped in V1 (`is_applicable()` always returns False). The slot exists so Module 21 can wire in its own rule store without requiring a Module 18 registry change.

### 5.2 What Module 18 V1 does contain

All quality checks are built-in, deterministic, and driven entirely by:
- The computed effective post-remediation statistics (Section 3)
- The `ValidationRun` and `ValidationResult` rows
- The `IssueDetectionRun` and bounded `Issue` rows
- The `QualityThreshold` configuration (numeric thresholds only)

There is no free-form rule authoring, no regex configuration, no row-count constraints authored by the operator. All checks are fixed, versioned code inside `app/quality/categories/`.

### 5.3 Deferred to Module 21

- Operator-authored business rules (minimum row count, column regex, column value set, uniqueness constraints, custom SQL)
- The `quality_business_rules` table and its CRUD API
- The `business_rule_compliance` category becoming active (its `is_applicable()` will query Module 21's rule store when that store exists)
- Any cross-dataset or cross-source referential integrity rules

---

## 6. Rule and Category Registry

### 6.1 V1 category table

| # | Category | Default Weight | Applicable When | V1 Status |
|---|---|---|---|---|
| 1 | `completeness` | 20 | DataProfile resolved AND effective_row_count > 0 | Active |
| 2 | `uniqueness` | 15 | DataProfile resolved AND effective_row_count > 0 | Active |
| 3 | `validity` | 25 | ValidationRun.approved_changes_considered > 0 | Active |
| 4 | `consistency` | 10 | ValidationRun has results for consistency-related rules | Active |
| 5 | `referential_integrity` | 10 | Never — pending Module 21 FK metadata | Always skipped (no finding emitted) |
| 6 | `business_rule_compliance` | 15 | Never — pending Module 21 rule store | Always skipped (no finding emitted) |
| 7 | `unresolved_risk` | 25 | IssueDetectionRun.total_issues_found > 0 | Active |
| 8 | `validation_coverage` | 15 | ValidationRun.approved_changes_considered > 0 | Active |

**Always-skipped categories** (`referential_integrity`, `business_rule_compliance`): no `QualityFinding` is persisted. The category appears in `category_statuses` JSON as `"skipped"` and in `category_weights_used` as absent. The `category_scores` entry is absent (not zero). This is sufficient audit evidence. Section 6.1 is the permanent, version-controlled record of why those categories are skipped.

**Score impact**: skipped categories are excluded from both the numerator and denominator of the weighted average. Their absence does not drag the overall score down.

---

### 6.2 Consistency rules

The `consistency` category evaluates only rules where the concept of "uniformity across a column" is testable from ValidationResult outcomes. Specifically: if a column had multiple `validate_standardize_capitalization` results and some are `passed` while others are `failed`, that column is inconsistently normalized.

`is_applicable()` returns True if and only if `ValidationRun.results_by_rule` contains at least one of: `validate_standardize_capitalization`, `validate_normalize_boolean`, `validate_normalize_date`, `validate_normalize_enum_value`. If none of these rules produced results, the category is skipped.

---

### 6.3 Registry implementation

```python
# app/quality/registry.py

QUALITY_RULES: tuple[QualityCategory, ...] = (
    CompletenessCategory(),
    UniquenessCategory(),
    ValidityCategory(),
    ConsistencyCategory(),
    ReferentialIntegrityCategory(),     # always skips — V1 placeholder
    BusinessRuleComplianceCategory(),   # always skips — pending Module 21
    UnresolvedRiskCategory(),
    ValidationCoverageCategory(),
)

# Import-time assertions (same pattern as app.validation.registry)
assert len(QUALITY_RULES) == len(QUALITY_CATEGORIES) == 8
assert len({r.category_name for r in QUALITY_RULES}) == 8
assert all(r.category_name in QUALITY_CATEGORIES for r in QUALITY_RULES)
assert all(r.default_weight >= 0 for r in QUALITY_RULES)
# Always-skipped categories may have weight 0; active categories must have weight > 0.
_ALWAYS_SKIPPED = {"referential_integrity", "business_rule_compliance"}
assert all(
    r.default_weight > 0
    for r in QUALITY_RULES
    if r.category_name not in _ALWAYS_SKIPPED
)

_RULES_BY_CATEGORY: dict[str, QualityCategory] = {
    r.category_name: r for r in QUALITY_RULES
}
```

---

## 7. Execution Snapshot

The `execution_snapshot` JSON field on `QualityControlRun` is the permanent, deterministic record of every input the engine used. It must be populated before the engine is called and must not be modified after. An auditor can use it to independently verify the decision without querying any other table.

### 7.1 Exact schema

```json
{
  "snapshot_version": "1.0",
  "frozen_at": "<ISO-8601 UTC timestamp>",

  "engine": {
    "quality_engine_version": "1.0"
  },

  "inputs": {
    "validation_run_id": "<uuid>",
    "validation_result_count": 1200,
    "validation_result_ids_sha256": "<sha256 of sorted validation_result_id UUIDs, hex>",
    "remediation_run_id": "<uuid>",
    "issue_detection_run_id": "<uuid>",
    "data_profile_id": "<uuid>",
    "data_profile_source_sha256": "<sha256 of source file, hex>",
    "unresolved_high_critical_issue_ids": ["<uuid>", "..."],
    "unresolved_high_critical_issue_count": 2
  },

  "configuration": {
    "threshold_config_id": "<uuid | null>",
    "threshold_config_source": "data_source_specific | org_wide | built_in_defaults",
    "fail_score_threshold": 60.0,
    "pass_score_threshold": 85.0,
    "max_validation_failure_rate": 0.0,
    "max_validation_skip_rate": 0.5,
    "max_high_severity_unresolved": 0,
    "max_critical_severity_unresolved": 0,
    "max_warnings_for_clean_pass": 0
  },

  "categories": {
    "applicable": ["completeness", "uniqueness", "validity", "unresolved_risk", "validation_coverage"],
    "skipped": ["consistency", "referential_integrity", "business_rule_compliance"],
    "weights_applied": {
      "completeness": 20,
      "uniqueness": 15,
      "validity": 25,
      "unresolved_risk": 25,
      "validation_coverage": 15
    }
  }
}
```

**`validation_result_ids_sha256`**: SHA-256 of the concatenation of sorted `ValidationResult.id` UUID strings (sorted ascending). This allows future audit tools to verify the exact result set without embedding potentially 10,000+ UUIDs in the snapshot JSON. The count is stored separately for quick human inspection.

**`unresolved_high_critical_issue_ids`**: Direct list. HIGH/CRITICAL issues are bounded (IssueDetectionRun.total_issues_found may be large, but HIGH/CRITICAL issues are typically few). If this list exceeds a defined cap (e.g., 500 IDs), store the first 500 in sorted order and record `"unresolved_high_critical_issue_count"` as the true total — same bounded-but-never-silent pattern.

**Stable JSON**: all keys are sorted alphabetically; all list entries are sorted ascending. This guarantees two runs over identical inputs produce byte-identical snapshots, supporting future snapshot diffing.

---

## 8. Scoring Model

### 8.1 Category score (0–100)

Each applicable, non-skipped category produces a float score in [0.0, 100.0] using category-specific formula (see category detail below). A skipped category produces no score — it is absent from `category_scores`, not present as `null`.

### 8.2 Overall score

```
applicable_categories = [
    c for c in QUALITY_RULES
    if c.is_applicable(inputs) and category_status[c] != "skipped"
]

if not applicable_categories:
    overall_score = NULL   # see Section 11 — release rules for no-category case
else:
    weight_sum = sum(
        effective_weight(c, threshold.category_weights)
        for c in applicable_categories
    )
    # weight_sum > 0 guaranteed by pre-engine validation (Section 9, Step 4)
    overall_score = sum(
        category_scores[c] × effective_weight(c, threshold.category_weights)
        for c in applicable_categories
    ) / weight_sum
```

Where `effective_weight(c, overrides)` = `overrides[c.category_name]` if present and > 0, else `c.default_weight`.

### 8.3 Category status

| Status | Condition |
|---|---|
| `skipped` | `is_applicable()` returned False (absent from score computation) |
| `passed` | score ≥ 85.0 AND zero blocking or warning findings for this category |
| `warning` | score ≥ 60.0 AND zero blocking findings AND ≥ 1 warning finding |
| `failed` | score < 60.0 OR ≥ 1 blocking finding |

Category-status thresholds are internal constants, not configurable. The configurable thresholds in `QualityThreshold` apply only to the overall release decision.

---

## 9. Handler Design

`app/worker/handlers/quality_control.py` — seven steps.

### Step 1 — Resolve ValidationRun (org-scoped)

`SELECT ValidationRun WHERE task_run_id = source_task_run_id AND organization_id = ?`. Missing → `PermanentExecutionError`. A `source_task_run_id` belonging to another org or pointing to a non-VALIDATE TaskRun is indistinguishable from missing — permanent failure, no information leaked.

### Step 2 — Idempotency check

`SELECT QualityControlRun WHERE task_run_id = context.task_run.id`. If found, return the existing summary string. No writes.

**Second UNIQUE constraint idempotency**: if this `ValidationRun.id` already has a `QualityControlRun` (from a prior QUALITY_CTRL TaskRun), the IntegrityError on `uq_quality_control_runs_validation_run_id` at Step 6 will be caught and the existing row returned. This is the race-recovery path for "same ValidationRun, different TaskRun."

### Step 3 — Pre-engine configuration validation

Before loading any data, the handler validates the effective threshold configuration. **Invalid configuration raises `PermanentExecutionError` immediately** — it is not passed to the engine.

Validation rules (exhaustive, not illustrative):
- `fail_score_threshold >= 0.0 AND fail_score_threshold <= 100.0`
- `pass_score_threshold >= 0.0 AND pass_score_threshold <= 100.0`
- `pass_score_threshold > fail_score_threshold` (strict — equal is rejected)
- `max_validation_failure_rate >= 0.0 AND max_validation_failure_rate <= 1.0`
- `max_validation_skip_rate >= 0.0 AND max_validation_skip_rate <= 1.0`
- `max_high_severity_unresolved >= 0`
- `max_critical_severity_unresolved >= 0`
- `max_warnings_for_clean_pass >= 0`
- If `category_weights` is provided: every key is in `QUALITY_CATEGORIES`, every value is a positive number

The check for "total applicable weight > 0" is deferred to Step 9 (after applicability is determined), but if every active category's weight is 0 or overridden to 0, `PermanentExecutionError` is raised before the engine is called.

### Step 4 — Freeze input snapshot (eleven batch queries)

All data loaded before the engine is called. Any required row that is not found → `PermanentExecutionError`.

```
4a. Load RemediationRun via ValidationRun.remediation_run_id (org-scoped)
4b. Load REMEDIATE TaskRun via RemediationRun.task_run_id (org-scoped)
4c. Load DETECT TaskRun via REMEDIATE TaskRun.source_task_run_id (org-scoped)
4d. Load IssueDetectionRun WHERE task_run_id = DETECT TaskRun.id (org-scoped)
4e. Load SYNC TaskRun via DETECT TaskRun.source_task_run_id (org-scoped)
4f. Load DataProfile WHERE task_run_id = SYNC TaskRun.id (org-scoped)
     → Missing DataProfile: PermanentExecutionError (non-skippable, see Section 3.4)
4g. Load all ValidationResult rows for ValidationRun.id (org-scoped)
4h. Load all Issue rows for IssueDetectionRun.id (org-scoped, bounded)
4i. Load QualityThreshold (data-source-specific first, then org-wide)
     → Missing QualityThreshold: use built-in defaults (not an error)
```

### Step 5 — Compute effective post-remediation statistics

Using DataProfile (Step 4f) + the approved+passed subset of ValidationResult/RemediationChange rows (Steps 4g/4h), compute `post_remediation_stats` as defined in Section 3.2.

This step is pure Python — no additional DB queries.

### Step 6 — Validate total applicable weight

Determine which categories are applicable for this specific input. Compute the total applicable weight. If it is 0 or negative → `PermanentExecutionError` (this catches the case where an operator set all active category weights to zero). This check uses the inputs from Steps 4–5 and the configuration from Step 3.

### Step 7 — Build `QualityEngineInput` and call pure engine

```python
inputs = QualityEngineInput(
    organization_id=...,
    data_source_id=...,
    validation_run=ValidationRunSnapshot(...),
    validation_results=tuple(ValidationResultSnapshot(...) for r in result_rows),
    issue_detection_run=IssueDetectionRunSnapshot(...),
    data_profile=DataProfileSnapshot(...),
    effective_stats=EffectiveDatasetStats(...),  # from Step 5
    issues=tuple(IssueSnapshot(...) for i in issue_rows),
    thresholds=effective_thresholds,
    limits=QualityLimits(max_persisted_findings=settings.quality_max_persisted_findings),
)

result: QualityEngineResult = quality_control(inputs)
```

One call. No side effects. No retries.

### Step 8 — Build execution snapshot JSON

Construct the deterministic snapshot (Section 7) from the frozen inputs. This step is pure Python.

### Step 9 — Persist in exactly ONE transaction

```python
qc_run = QualityControlRun(
    id=uuid.uuid4(),
    organization_id=organization_id,
    task_run_id=context.task_run.id,
    validation_run_id=validation_run.id,
    ...,
    overall_score=result.overall_score,       # may be None
    release_recommendation=result.recommendation,
    post_remediation_stats=computed_stats,
    execution_snapshot=snapshot_json,
)
db.add(qc_run)

for finding in result.findings[:limits.max_persisted_findings]:
    db.add(QualityFinding(
        organization_id=organization_id,
        quality_control_run_id=qc_run.id,
        ...
    ))

try:
    db.commit()
except IntegrityError:
    db.rollback()
    # Catches both: UNIQUE(task_run_id) and UNIQUE(validation_run_id) conflicts.
    existing = db.execute(
        select(QualityControlRun).where(
            QualityControlRun.task_run_id == context.task_run.id
        )
    ).scalar_one_or_none()
    if existing is None:
        raise
    qc_run = existing
else:
    db.refresh(qc_run)

finally:
    db.close()
```

No partial commits. No nested transactions. `finally: db.close()` is unconditional.

---

## 10. Release Decision Rules

Evaluated in this exact order. The first matching condition wins. No exception.

### FAIL — if ANY of:

1. `overall_score IS NULL` (no applicable categories — see Section 11)
2. `blocking_count > 0` (any finding with `severity = "blocking"`)
3. `overall_score < thresholds.fail_score_threshold` (default 60.0)
4. `approved_changes_considered > 0` AND `failed_count / approved_changes_considered > thresholds.max_validation_failure_rate` (default 0.0)
5. `effective_stats.unresolved_critical_count > thresholds.max_critical_severity_unresolved` (default 0)
6. `effective_stats.unresolved_high_count > thresholds.max_high_severity_unresolved` (default 0)

### PASS — if ALL of:

1. No FAIL condition triggered
2. `overall_score >= thresholds.pass_score_threshold` (default 85.0)
3. `warning_count <= thresholds.max_warnings_for_clean_pass` (default 0)

### PASS_WITH_WARNINGS — all other cases:

- No FAIL condition triggered
- AND (`overall_score < pass_score_threshold` OR `warning_count > max_warnings_for_clean_pass`)

---

## 11. Missing-Evidence Release Rules

These cases have explicit, deterministic behavior. No case is left to implementation interpretation.

| Situation | `overall_score` | `release_recommendation` | Findings |
|---|---|---|---|
| No applicable categories (all skipped) | NULL | FAIL | 1 BLOCKING: `NO_APPLICABLE_CATEGORIES` |
| Missing DataProfile (baseline unavailable) | N/A | N/A — `PermanentExecutionError` raised in handler before engine | None persisted |
| Zero-row dataset (`effective_row_count = 0`) | Computed normally | FAIL (completeness score = 0 → BLOCKING finding `EMPTY_DATASET`) | BLOCKING finding on completeness |
| All ValidationResults skipped (`skipped_count = approved_changes_considered > 0`) | Computed (validation_coverage degrades) | FAIL if `skipped_count/total > max_validation_skip_rate` AND that causes blocking; otherwise PASS_WITH_WARNINGS | WARNING or BLOCKING on validation_coverage |
| ValidationRun with zero approved changes (`approved_changes_considered = 0`) | Computed (validity = 100, coverage = 100 vacuously; completeness/uniqueness/unresolved_risk still apply) | Depends on completeness/uniqueness/unresolved_risk outcomes | Per-category findings only |
| Missing QualityThreshold configuration | Built-in defaults used | Computed normally using defaults | No finding for missing config |
| QualityThreshold exists but `pass_score_threshold <= fail_score_threshold` | N/A — caught in Step 3 | N/A — `PermanentExecutionError` | None persisted |
| All category weights set to zero in `category_weights` override | N/A — caught in Step 6 | N/A — `PermanentExecutionError` | None persisted |
| All validation results passed AND no unresolved issues AND completeness/uniqueness normal | 100.0 (or close) | PASS | 0 findings |

**Zero-row dataset detail**: `completeness` checks whether there is data to evaluate. A dataset with zero rows cannot be considered complete — there is nothing there. The category produces a BLOCKING finding with reason `EMPTY_DATASET`. This forces `release_recommendation = FAIL` unconditionally regardless of other category scores. An empty dataset is never releasable.

---

## 12. Idempotency Strategy

Three layers:

1. **`UNIQUE(task_run_id)`** — the database prevents two rows for the same task execution.
2. **`UNIQUE(validation_run_id)`** — the database prevents two competing release decisions for the same ValidationRun, regardless of which TaskRun triggered the QC run.
3. **Early-exit (Step 2)** — before any compute, check for an existing row by `task_run_id` and return immediately.
4. **IntegrityError catch-and-refetch (Step 9)** — handles concurrent workers attempting to commit for the same `task_run_id` or `validation_run_id` simultaneously. Rollback, re-fetch by `task_run_id`, return the winner's row.

A second QUALITY_CTRL TaskRun submitted for the same ValidationRun produces the same output as the first — the winner's `QualityControlRun` row. This is the authoritative release decision; Module 19 reads exactly this row.

---

## 13. API Design

Two new read-only endpoints added to `app/api/tasks.py`. No new router file. No `main.py` change.

### `GET /tasks/{task_id}/runs/{run_id}/quality-control` → `QualityControlRunRead`

Three-layer 404 chain:
```
_get_active_task_or_404(db, task_id, org_id)
→ TaskRun WHERE id=run_id AND task_id AND organization_id
→ QualityControlRun WHERE task_run_id=run_id AND task_id AND organization_id
```

`processing_duration_ms`: derived from `TaskRun.started_at`/`finished_at`. None when `finished_at` IS NULL.

Authentication: `get_current_active_user` (no superuser gate — same as Modules 14, 15, 17 read endpoints).

**No `/categories` sub-endpoint**: all category data is inline in the summary response (`category_scores`, `category_statuses`, `category_weights_used`). A separate endpoint would duplicate without adding value.

### `GET /tasks/{task_id}/runs/{run_id}/quality-control/findings` → `PaginatedResponse[QualityFindingRead]`

Filters (all optional, combined via AND):

| Parameter | Type | Validates against | 422 if invalid |
|---|---|---|---|
| `category` | str | `QUALITY_CATEGORIES` | Yes |
| `severity` | str | `QUALITY_FINDING_SEVERITIES` | Yes |
| `outcome` | str | `QUALITY_FINDING_OUTCOMES` | Yes |
| `rule_name` | str | exact match, no LIKE | No |
| `affected_column` | str | exact match | No |

Pagination: `limit` (default 50, max 100; 422 if > 100), `offset` (default 0).

One COUNT query + one paginated SELECT per request. No per-finding sub-queries.

Ordering: `QualityFinding.created_at ASC, QualityFinding.id ASC` — stable for ties.

### DTOs

```python
class QualityControlRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    validation_run_id: uuid.UUID
    remediation_run_id: uuid.UUID
    issue_detection_run_id: uuid.UUID
    data_profile_id: uuid.UUID
    quality_engine_version: str
    overall_score: float | None          # None when no categories applicable
    release_recommendation: str
    total_findings: int
    blocking_count: int
    warning_count: int
    info_count: int
    category_scores: dict[str, float]    # absent keys = skipped categories
    category_statuses: dict[str, str]    # all 8 categories present
    category_weights_used: dict[str, float]  # only applicable categories
    post_remediation_stats: dict
    processing_duration_ms: int | None   # derived, not stored
    created_at: datetime

class QualityFindingRead(BaseModel):
    # Plain BaseModel — all fields set explicitly by the endpoint
    id: uuid.UUID
    quality_control_run_id: uuid.UUID
    category: str
    rule_name: str
    rule_version: str
    severity: str
    outcome: str
    reason: str
    affected_row_count: int | None
    affected_column: str | None
    source_issue_id: uuid.UUID | None
    remediation_change_id: uuid.UUID | None
    validation_result_id: uuid.UUID | None
    quality_engine_version: str
    created_at: datetime
```

---

## 14. Security Model

**Authentication**: `get_current_active_user` on all endpoints. No superuser gate — consistent with Modules 14, 15, 17.

**Tenant isolation**: every query includes `organization_id == current_user.organization_id`. The three-layer 404 chain hides cross-tenant resource existence. The handler's seven-step chain resolves all upstream rows with org-scoping at each step.

**Cross-tenant handler safety**: `source_task_run_id` pointing to another org's VALIDATE TaskRun returns None from org-scoped query → `PermanentExecutionError`. No information is leaked.

**Immutable findings**: `QualityFinding` rows are never updated or deleted. RESTRICT FKs on all three provenance columns prevent upstream rows from disappearing silently.

**No unauthorized writes**: the handler writes only `QualityControlRun` and `QualityFinding`. No Module 14/15/16/17 table is touched.

**Future encryption compatibility**: `reason` on `QualityFinding` is `Text()` (unbounded), same pattern as `ValidationResult.reason` and `RemediationChangeDecision.comment`.

**Configuration access control**: `QualityThreshold` has no write API in Module 18. Rows inserted directly — same pre-API state as `RemediationColumnRule` and `IssueDetectionColumnRule`.

---

## 15. Performance Model

**Expected query count (handler)**:

| Step | Queries |
|---|---|
| Resolve ValidationRun | 1 |
| Idempotency check | 1 |
| Config validation (in-memory) | 0 |
| Resolve RemediationRun | 1 |
| Resolve REMEDIATE TaskRun | 1 |
| Resolve DETECT TaskRun | 1 |
| Resolve IssueDetectionRun | 1 |
| Resolve SYNC TaskRun | 1 |
| Resolve DataProfile | 1 |
| Batch-load ValidationResults | 1 |
| Batch-load Issues | 1 |
| Load QualityThreshold | 1 |
| Persist (INSERT + COMMIT) | 1 |
| **Total** | **13** |

No N+1 queries. All batch loads return all rows in one query. All resolution queries use indexed columns.

**No CSV file reads** — the engine works from pre-computed statistics (DataProfile, IssueDetectionRun, ValidationRun aggregates) and bounded audit rows. For datasets of any size, the query count remains 13.

**Finding limit**: `QUALITY_MAX_PERSISTED_FINDINGS` (default 10,000). `QualityControlRun.total_findings` = true total from engine. DB rows capped at the limit. Discrepancy is the audit signal — no fake overflow finding inserted (same pattern as `IssueDetectionRun.total_issues_found` vs actual Issue rows).

**API query count**: 1 COUNT + 1 SELECT per findings request, plus 3–4 for the 404 chain.

---

## 16. Risks

| Risk | Mitigation |
|---|---|
| Effective-stats computation underestimates missing-value fixes (e.g., a passed `trim_whitespace` on a non-null value doesn't reduce missing_value_total) | Issue-type cross-reference is the mechanism: only changes whose linked Issue had type `missing_value / empty_string / null_value` reduce the count. The stat is an honest delta — not inflated, never rounded up. Named limitation in known limitations. |
| `UNIQUE(validation_run_id)` IntegrityError from a second QUALITY_CTRL TaskRun after the first completes | Caught by IntegrityError catch-and-refetch in Step 9. The second task returns the first run's row. Module 19 reads whichever row exists. |
| Resolution chain (7 queries) adds latency | All queries are on indexed PKs. Total round-trip overhead is negligible compared to the batch loads. Denormalized IDs on the run row mean the chain is traversed only once. |
| overall_score being nullable requires nullable Float column | Standard SQLAlchemy `Float(nullable=True)`. API DTO declares `float | None`. Module 19 handler checks `overall_score IS NOT NULL` before treating the recommendation as valid. |
| Configuration validated before engine call (Step 3) could fail on every retry | `PermanentExecutionError` — not retried. Correct: misconfiguration is not a transient error. The operator must fix the threshold before re-submitting. |

---

## 17. Edge Cases

See Section 11 for the complete missing-evidence matrix. Additional cases:

| Edge case | Behavior |
|---|---|
| Duplicate QC execution (same task_run_id) | UNIQUE(task_run_id) + early-exit in Step 2 |
| Two QC TaskRuns for same ValidationRun | UNIQUE(validation_run_id) IntegrityError → catch-and-refetch |
| Concurrent workers, same ValidationRun | One wins the UNIQUE commit; loser refetches winner's row |
| `effective_duplicate_row_count < 0` (more removals than counted duplicates) | Floor at 0; record delta in `post_remediation_stats.notes` — indicates an inconsistency between detection and remediation counts, not a crash |
| `blocking_count > 0` with `overall_score = 95` | FAIL unconditionally (score does not override blocking finding) |
| Category weight override sets one weight to 0 | That category is treated as skipped for scoring; its findings still persist |
| All active category weights overridden to 0 | `PermanentExecutionError` in Step 6 — zero total applicable weight |
| `consistency` applicable but all results are `skipped` | Score computed from skipped ValidationResults; findings are INFO-level; category_status = "warning" or "passed" depending on thresholds |

---

## 18. Test Strategy

### Phase 1 (models + migration)

- QualityControlRun: valid row, UNIQUE(task_run_id) violation, UNIQUE(validation_run_id) violation, CHECK(score range) violation, CHECK(counts reconcile) violation, CHECK(recommendation valid) violation, nullable overall_score = NULL passes constraint
- QualityFinding: valid row, FK violation (invalid quality_control_run_id), RESTRICT FK prevents Issue deletion when finding references it
- QualityThreshold: CHECK(pass_score > fail_score) violation, rate range violations, negative count violations
- Migration cycle: `base → head → base → head`, single head after upgrade

### Phase 2 (pure engine)

- Each of 6 active categories: correct score formula, correct finding severity/count, correct category_status
- `referential_integrity`, `business_rule_compliance`: `is_applicable()` = False, no finding emitted, absent from category_scores
- Overall score computation: weighted average with and without weight overrides
- Skipped categories correctly excluded from denominator
- Nullable overall_score: no applicable categories → NULL
- Zero-row dataset → completeness BLOCKING, FAIL
- All validation results skipped → validation_coverage degrades
- Release decision FAIL: each of the 6 FAIL conditions independently
- Release decision PASS: score ≥ pass_threshold, warning_count = 0
- Release decision PASS_WITH_WARNINGS: score between thresholds, or warnings present
- Determinism: same inputs → identical output on multiple calls
- Stable finding ordering: category ASC, rule_name ASC, affected_column ASC, created_at ASC
- Snapshot JSON is stable-sorted (deterministic byte output for identical inputs)

### Phase 3 (handler)

- Full chain: all 7 queries succeed, QC run + findings persisted correctly
- Missing ValidationRun → PermanentExecutionError
- Missing DataProfile → PermanentExecutionError
- Missing QualityThreshold → built-in defaults used, succeeds
- Invalid threshold config (pass_threshold ≤ fail_threshold) → PermanentExecutionError before engine
- All active category weights = 0 → PermanentExecutionError before engine
- Idempotency (same task_run_id) → returns existing row, no new insert
- UNIQUE(validation_run_id) race → IntegrityError caught, existing row returned
- Org isolation: source_task_run_id from another org → PermanentExecutionError
- Finding limit: max_persisted_findings enforced; total_findings = true total
- effective_stats computed correctly: row removals, missing-value fixes, delta floored at 0
- execution_snapshot JSON structure matches Section 7 schema
- thresholds_snapshot round-trip: stored as JSON, re-readable as equivalent config
- `finally: db.close()` always executes

### Phase 4 (API)

- Summary 200: all fields, correct types, overall_score = null for no-category case
- processing_duration_ms: derived correctly; None when finished_at IS NULL
- Summary 404: task missing, task_run missing, qc_run missing
- Cross-tenant 404
- Non-superuser access: 200
- Findings 200: pagination shape (items, total, limit, offset)
- Default limit 50, max 100 (422 if > 100)
- category filter: valid → filtered; invalid → 422
- severity filter: valid → filtered; invalid → 422
- outcome filter: valid → filtered; invalid → 422
- rule_name filter: exact match
- affected_column filter: exact match
- Combined filters: AND semantics
- Stable ordering
- total count reflects all filtered rows (not just current page)
- N+1 structural check

---

## 19. Phase Breakdown

### Phase 1 — Models, migration, enums, configuration

**Files created:**
- `backend/app/models/quality_control_run.py`
- `backend/app/models/quality_finding.py`
- `backend/app/models/quality_threshold.py`
- `database/alembic/versions/<hash>_quality_control_engine.py`

**Changes to `backend/app/models/enums.py`:**
- `TaskType.QUALITY_CTRL = "quality_ctrl"` with comment
- `QUALITY_CATEGORIES` (8 values)
- `QUALITY_FINDING_SEVERITIES = ("info", "warning", "blocking")`
- `QUALITY_FINDING_OUTCOMES = ("passed", "failed", "skipped")`
- `QUALITY_RELEASE_RECOMMENDATIONS = ("PASS", "PASS_WITH_WARNINGS", "FAIL")`
- `QUALITY_CATEGORY_STATUSES = ("passed", "warning", "failed", "skipped")`

**Changes to `backend/app/core/config.py`:**
- `quality_max_persisted_findings: int = Field(default=10_000, gt=0, alias="QUALITY_MAX_PERSISTED_FINDINGS")`

**Changes to `backend/app/worker/handlers/__init__.py`:**
- `TaskType.QUALITY_CTRL: NoOpHandler()` (placeholder)

**Tests:** `tests/test_quality_control_models.py`

No engine, no handler, no API.

---

### Phase 2 — Pure quality engine

**Files created:**
- `backend/app/quality/__init__.py`
- `backend/app/quality/types.py` — frozen dataclasses
- `backend/app/quality/base.py` — QualityCategory Protocol
- `backend/app/quality/reasons.py` — closed reason vocabulary, uniqueness assertion
- `backend/app/quality/registry.py` — QUALITY_RULES tuple, import-time assertions
- `backend/app/quality/engine.py` — `quality_control()`, `QUALITY_ENGINE_VERSION = "1.0"`
- `backend/app/quality/categories/completeness.py`
- `backend/app/quality/categories/uniqueness.py`
- `backend/app/quality/categories/validity.py`
- `backend/app/quality/categories/consistency.py`
- `backend/app/quality/categories/referential_integrity.py` (always skips)
- `backend/app/quality/categories/business_rule_compliance.py` (always skips)
- `backend/app/quality/categories/unresolved_risk.py`
- `backend/app/quality/categories/validation_coverage.py`

**Tests:** `tests/test_quality_engine.py`

No DB access, no handler, no API.

---

### Phase 3 — Handler + worker registration

**Files created:**
- `backend/app/worker/handlers/quality_control.py`

**Changes to `backend/app/worker/handlers/__init__.py`:**
- Replace NoOpHandler with QualityControlHandler()

**Tests:** `tests/test_quality_control_handler.py`

No API.

---

### Phase 4 — Read-only API

**Files created:**
- `backend/app/schemas/quality_control.py`

**Changes to `backend/app/api/tasks.py`:**
- Imports
- `_get_quality_control_run_or_404` helper
- `get_task_run_quality_control` endpoint
- `list_task_run_quality_control_findings` endpoint

**Tests:** `tests/test_quality_control_api.py`

---

### Phase 5 — Final verification, documentation, production-readiness report

Full regression suite, migration cycle, compile check, architecture review, PROJECT_CONTEXT.md update, final report.

---

## 20. Key Design Decisions and Rationale

**D1: Effective post-remediation statistics, not DataProfile (Section 3)**
The original DataProfile reflects the raw un-remediated dataset. Using it would evaluate a state that no longer exists after Modules 15–17 processed the data. The effective stats are computed deterministically from the approved+validated delta — no CSV reads, no guessing. The baseline DataProfile is retained as a reference point, not as the evaluand.

**D2: No `quality_business_rules` table (Section 5)**
Module 21 is designated for reusable business rules. A competing `quality_business_rules` table in Module 18 would create a structurally similar but architecturally disconnected system that Module 21 must reconcile or replace. The slot is registered (as always-skipped) so Module 21 can wire in without any Module 18 code change.

**D3: UNIQUE(validation_run_id) in addition to UNIQUE(task_run_id) (Section 4.2)**
One ValidationRun must have exactly one authoritative release decision. Multiple QUALITY_CTRL TaskRuns over the same ValidationRun would produce multiple, potentially conflicting, `release_recommendation` values — Module 19 cannot know which to trust. The second UNIQUE constraint enforces this at the database level; the IntegrityError catch-and-refetch handles the race case gracefully.

**D4: overall_score is nullable; no-category case → FAIL (Sections 4.2, 11)**
A score of 0 or 100 when no categories apply is misleading. NULL accurately represents "not computable." The CHECK constraint allows NULL. The release decision is FAIL with a BLOCKING finding — a dataset that cannot be evaluated must not be released. No new recommendation value is introduced; FAIL is the correct conservative result.

**D5: Configuration validated before engine call, not inside engine (Section 9, Step 3)**
The pure engine must remain free of I/O and side-effect concerns. Configuration validation is a handler-layer responsibility. Invalid configuration raises `PermanentExecutionError` in the handler — the engine never sees it. This keeps the engine purely a transformation from valid inputs to outputs.

**D6: Always-skipped categories emit no QualityFinding (Section 6.1)**
`category_statuses` JSON records `"referential_integrity": "skipped"` and `"business_rule_compliance": "skipped"`. This is sufficient audit evidence — an auditor knows from the snapshot and category_statuses exactly why those categories were not evaluated. Inserting one INFO finding per run for each always-skipped category would add noise without adding information.

**D7: Snapshot stores ID hash, not full ID list (Section 7)**
Storing 10,000 ValidationResult UUIDs in JSON would produce ~370KB of snapshot data per run. Instead, the snapshot stores the count and a SHA-256 of the sorted ID list. This provides tamper detection (any change to the result set changes the hash) without bloating the snapshot. IDs remain queryable via `SELECT id FROM validation_results WHERE validation_run_id = ?`.

**D8: Missing QualityThreshold uses built-in defaults, not PermanentExecutionError (Section 9, Step 4)**
An operator who has not yet configured thresholds should still receive a QC decision — using conservative, well-documented defaults. Making configuration mandatory would block the entire pipeline for new data sources. Operators who want stricter behavior must explicitly configure it. This matches how every prior module's configuration tables (RemediationColumnRule, IssueDetectionColumnRule) are optional — absence means "use defaults or skip."

**D9: pass_score_threshold must be strictly greater than fail_score_threshold (Sections 4.4, 9)**
Equal thresholds would produce ambiguous behavior: a score exactly equal to both would match neither the PASS nor the FAIL condition, landing in PASS_WITH_WARNINGS by default. Strict inequality removes the ambiguity entirely. Enforced by DB CHECK and by pre-engine handler validation.

---

## Appendix — Change Log (Rev 1 → Rev 2)

| # | Correction | Section(s) changed |
|---|---|---|
| 1 | Replaced DataProfile-as-quality-input with computed effective post-remediation statistics derived from approved+passed RemediationChange deltas. DataProfile retained only as baseline. Missing DataProfile is now PermanentExecutionError. | 3, 9 (Step 5), 11, 15, 16 |
| 2 | Removed `QualityBusinessRule` table and model entirely. Removed `quality_business_rules` from Phase 1 deliverables. Registered `business_rule_compliance` as always-skipped V1 slot. Added Section 5 documenting Module 21 deferral. | 4 (new model removed), 5 (new section), 6.1, 19 (Phase 1) |
| 3 | Added `UNIQUE(validation_run_id)` to `QualityControlRun`. Updated Section 12 (idempotency) to cover the second constraint and its IntegrityError handling. Added Module 19 consumption note in Section 2. | 2, 4.2, 12 |
| 4 | Made `overall_score` nullable (NULL when no categories applicable). Updated CHECK constraint. Changed no-category behavior from `overall_score = 100, PASS_WITH_WARNINGS` to `overall_score = NULL, FAIL`. Updated Section 11 table and Decision Rule 1. | 4.2, 8, 10, 11 |
| 5 | Moved all threshold and weight validation to handler Step 3 (pre-engine), raising PermanentExecutionError for any invalid configuration. Defined the exhaustive validation checklist. Added "total applicable weight > 0" check in new handler Step 6. Renumbered subsequent steps. | 9 (Steps 3 and 6 are new) |
| 6 | Always-skipped categories (`referential_integrity`, `business_rule_compliance`) now emit zero QualityFindings. Category appears in `category_statuses` as `"skipped"` only. Removed the INFO finding announcement. | 6.1, 6.3 |
| 7 | Replaced vague "snapshot JSON" with an exact schema (Section 7) specifying every key, value format, stable sort order, and the SHA-256 ID-hash approach for large ID lists. | 7 (section rewritten) |
| 8 | Replaced partial missing-evidence descriptions with a complete, exhaustive table in Section 11 covering every specified scenario with explicit `overall_score`, `release_recommendation`, and finding outcomes. | 11 (section rewritten) |
