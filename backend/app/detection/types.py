"""Internal immutable value objects shared by the detection engine and
every rule. Mirrors app.profiling.types / app.cleaning.types' shape
exactly -- pure dataclasses, no I/O, no database/session references
anywhere in this module."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnRuleConfig:
    """One column's resolved governance, keyed by column_name in
    DetectionDataset.column_rules -- exactly what a single active
    IssueDetectionColumnRule row (data-source-scoped, falling back to an
    org-wide row) resolved down to. A column with no row at all is simply
    absent from that dict -- every field below then implicitly means "no
    check", never a guessed default. Built by
    app.worker.handlers.issue_detection from the database; this dataclass
    itself never touches the database.

    Per the approved Module 14 corrections: expected_type / is_required /
    is_primary_key / allowed_values / outlier_enabled /
    capitalization_check_enabled are the ONLY signal any rule may use to
    decide whether it runs on a column -- never the column's name, never
    a guess from its data.

    outlier_zscore_threshold is always already resolved by the time a
    rule sees it: app.worker.handlers.issue_detection fills in
    settings.issue_detection_outlier_zscore_threshold for any column
    whose own database row left it NULL, before ever building a
    DetectionDataset -- app.detection.rules.outliers itself never reads
    Settings. It stays optional here only because a directly-constructed
    ColumnRuleConfig (e.g. in a unit test) may reasonably leave it unset;
    that rule falls back to its own small internal constant in that case."""

    expected_type: str | None = None
    is_required: bool = False
    is_primary_key: bool = False
    allowed_values: tuple[str, ...] | None = None
    outlier_enabled: bool = False
    outlier_zscore_threshold: float | None = None
    capitalization_check_enabled: bool = False


@dataclass(frozen=True)
class Finding:
    """One detection finding, produced by exactly one DetectionRule.
    Field shape mirrors app.models.issue.Issue (minus id/organization_id/
    detection_run_id/created_at, which only exist once persisted).
    issue_type must always be one of app.detection.issue_types'
    constants; severity one of app.detection.severities'."""

    row_number: int
    column_name: str | None
    issue_type: str
    severity: str
    original_value: str | None
    suggested_fix: str | None
    confidence: float


@dataclass(frozen=True)
class DetectionDataset:
    """The read-only view of a loaded CSV every rule receives. Rules must
    never mutate headers/rows/structural_issues -- see this package's own
    docstring's read-only guarantee. `rows` is already uniform-length and
    padding-repaired by app.profiling.csv_loader.load_csv exactly the way
    app.cleaning.engine.clean also receives it; structural_issues is that
    same loader's own too_few_fields/too_many_fields/blank_header/
    duplicate_header records, reused here (not recomputed) so
    missing_value detection can tell a genuinely-padded cell apart from a
    cell that was really an empty string in the source file."""

    headers: list[str]
    rows: list[list[str]]
    structural_issues: list[dict] = field(default_factory=list)
    column_rules: dict[str, ColumnRuleConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class DetectionLimits:
    max_persisted_issues: int


@dataclass(frozen=True)
class DetectionResult:
    # Bounded to DetectionLimits.max_persisted_issues -- total_issues_found
    # below is always the true total even when this list is capped (same
    # bounded-but-never-silent pattern app.cleaning.types.CleaningResult
    # already established for changes/total_changes_count).
    findings: list[Finding]
    total_issues_found: int
    persisted_issue_count: int
    issues_by_severity: dict[str, int]
    issues_by_type: dict[str, int]
    rows_scanned: int
    columns_scanned: int
