"""Module 11 -- Operational Review Queue: pure, framework-independent
database-level aggregation.

Builds one portable SQLAlchemy Core UNION ALL construct over nine
physical branches (see docs/module-11-operational-review-queue-design.md
Revision 3, Sections 4/5), each normalized into an identical column shape.
Every filter, search predicate, sort, and page/summary computation is
applied against that aggregated construct at the database layer -- no
branch's rows are ever fully materialized into Python before being
filtered, sorted, or paginated (the specific anti-pattern the approved
design was corrected away from in Revision 2 -> Revision 3).

Implementation-level clarification (does not change the approved
architecture -- see Module 11 implementation notes): Revision 3 Section 4
describes an "eleven-column shape" but its own list, plus Section 7's
separately-retained `label` field, actually total twelve columns
(organization_id, reference_id, task_id, task_run_id, data_source_id,
review_category, review_type, source, label, confidence_score, reason,
created_at). This module implements all twelve, reconciling that internal
count discrepancy in the design document rather than dropping `label`
(which Section 7 explicitly says is retained) or omitting one of the
other eleven.

Implementation-level clarification #2: `match_decisions.reason` is NOT
NULL and already populated by the matching engine (confirmed by reading
backend/app/models/match_decision.py directly) -- Revision 3's Section 5
mapping table incorrectly stated this column doesn't exist and should be
NULL. This module populates `reason` from that existing column for the
`match_decision` branch instead of leaving it NULL. The field's name,
type, and nullability in the approved contract are unchanged; only this
one branch's SQL now correctly reads an already-existing column instead
of a placeholder NULL.

Implementation-level clarification #3 (search join placement): rather
than joining `tasks`/`data_sources` inside every one of the nine branches
(which would be a gratuitous join for the four plain run-type branches,
whose task_id/data_source_id are already native columns), the Task Name
and Dataset Name search joins are applied exactly once, against the
already-unioned subquery, and only when a search term is supplied. This
satisfies the design's "every join documented and justified, no
gratuitous joins" requirement more strictly than joining per branch would.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import (
    CompoundSelect,
    Select,
    String,
    Float,
    case,
    cast,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.orm import Session

from app.models.artifact_download_event import ArtifactDownloadEvent
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.export_run import ExportRun
from app.models.match_decision import MatchDecision
from app.models.match_run import MatchRun
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun

SortOption = Literal["created_at", "confidence_score"]


def _pending_review_branch(model, category: str, source: str, label: str, organization_id: uuid.UUID) -> Select:
    """Branches 1-4 (Section 5): cleaning_runs, standardization_runs,
    match_runs, export_runs -- all four already carry organization_id,
    id, task_id, task_run_id, data_source_id, confidence_score, and
    created_at natively. No join is required for any of these columns.
    `reason` has no natural free-text column on any of the four run
    tables -- legitimately NULL, not a gap to join around. organization_id
    is filtered as the FIRST predicate (Section 11).

    ExportRun carries no confidence_score column at all (Module 9's row-
    level deduplication has no scoring concept -- confirmed by reading
    models/export_run.py directly) -- a typed NULL is substituted for
    that one branch, exactly the same "legitimately NULL, not a gap to
    join or invent data around" treatment already applied to `reason`."""
    confidence_score_column = (
        model.confidence_score.label("confidence_score")
        if hasattr(model, "confidence_score")
        else cast(null(), Float).label("confidence_score")
    )
    return (
        select(
            model.organization_id.label("organization_id"),
            model.id.label("reference_id"),
            model.task_id.label("task_id"),
            model.task_run_id.label("task_run_id"),
            model.data_source_id.label("data_source_id"),
            literal(category).label("review_category"),
            literal("PENDING_REVIEW").label("review_type"),
            literal(source).label("source"),
            literal(label).label("label"),
            confidence_score_column,
            cast(null(), String).label("reason"),
            model.created_at.label("created_at"),
        )
        .where(model.organization_id == organization_id, model.status == "pending_review")
    )


def _ambiguous_match_decision_branch(organization_id: uuid.UUID) -> Select:
    """Branch 5 (Section 5): match_decisions has no task_id, task_run_id,
    or data_source_id of its own -- all three are obtained via the join
    to match_runs (already required for those three fields; task_id is
    not fetched a second time via a separate join). `reason` uses the
    existing, already-populated match_decisions.reason column (see
    module docstring, clarification #2) -- not NULL."""
    return (
        select(
            MatchDecision.organization_id.label("organization_id"),
            MatchDecision.id.label("reference_id"),
            MatchRun.task_id.label("task_id"),
            MatchRun.task_run_id.label("task_run_id"),
            MatchRun.data_source_id.label("data_source_id"),
            literal("MATCHING").label("review_category"),
            literal("AMBIGUOUS").label("review_type"),
            literal("match_decision").label("source"),
            literal("Ambiguous Match Decision").label("label"),
            MatchDecision.confidence_score.label("confidence_score"),
            MatchDecision.reason.label("reason"),
            MatchDecision.created_at.label("created_at"),
        )
        .join(MatchRun, MatchRun.id == MatchDecision.match_run_id)
        .where(MatchDecision.organization_id == organization_id, MatchDecision.decision == "ambiguous")
    )


def _failed_task_run_branch(organization_id: uuid.UUID) -> Select:
    """Branch 6 (Section 5): task_runs has no data_source_id of its own
    (only tasks does) -- the join to tasks is required for that one
    field. task_run_id = reference_id (the row itself is the task run).
    confidence_score is legitimately NULL -- no such column exists on
    task_runs."""
    return (
        select(
            TaskRun.organization_id.label("organization_id"),
            TaskRun.id.label("reference_id"),
            TaskRun.task_id.label("task_id"),
            TaskRun.id.label("task_run_id"),
            Task.data_source_id.label("data_source_id"),
            literal("SYSTEM").label("review_category"),
            literal("FAILED").label("review_type"),
            literal("task_run").label("source"),
            literal("Failed Processing Run").label("label"),
            cast(null(), Float).label("confidence_score"),
            TaskRun.error_message.label("reason"),
            TaskRun.created_at.label("created_at"),
        )
        .join(Task, Task.id == TaskRun.task_id)
        .where(TaskRun.organization_id == organization_id, TaskRun.status == "failed")
    )


_DOWNLOAD_PARENTS = (
    ("cleaning_run_id", CleaningRun),
    ("standardization_run_id", StandardizationRun),
    ("export_run_id", ExportRun),
)


def _artifact_download_failure_branch(run_id_column: str, parent_model, organization_id: uuid.UUID) -> Select:
    """Branches 7-9 (Section 5): artifact_download_events' exactly-one-
    of-three-run-reference structure (its own CHECK constraint) means
    this is three separate joined branches, one per possible parent run
    type, rather than one branch with a three-way COALESCE join -- kept
    explicit and simple to read rather than obscured behind a single
    complex join. Each joins its one relevant parent run table to obtain
    task_id/task_run_id/data_source_id, exactly the fields already
    required for every other branch's contract. review_type is a
    portable CASE WHEN, standard ANSI SQL on both engines. outcome =
    'started' is excluded entirely (see Section 4/18 of the design --
    an in-flight download is not, by itself, evidence of a stuck
    request)."""
    fk_column = getattr(ArtifactDownloadEvent, run_id_column)
    return (
        select(
            ArtifactDownloadEvent.organization_id.label("organization_id"),
            ArtifactDownloadEvent.id.label("reference_id"),
            parent_model.task_id.label("task_id"),
            parent_model.task_run_id.label("task_run_id"),
            parent_model.data_source_id.label("data_source_id"),
            literal("DOWNLOAD").label("review_category"),
            case(
                (ArtifactDownloadEvent.outcome == "integrity_failed", literal("INTEGRITY_FAILURE")),
                else_=literal("FAILED"),
            ).label("review_type"),
            literal("artifact_download_event").label("source"),
            case(
                (
                    ArtifactDownloadEvent.outcome == "integrity_failed",
                    literal("Artifact Integrity Verification Failed"),
                ),
                else_=literal("Artifact Delivery Failed"),
            ).label("label"),
            cast(null(), Float).label("confidence_score"),
            ArtifactDownloadEvent.failure_reason_code.label("reason"),
            ArtifactDownloadEvent.created_at.label("created_at"),
        )
        .join(parent_model, parent_model.id == fk_column)
        .where(
            ArtifactDownloadEvent.organization_id == organization_id,
            fk_column.is_not(None),
            ArtifactDownloadEvent.outcome.in_(("integrity_failed", "file_missing", "stream_failed")),
        )
    )


def _build_union(organization_id: uuid.UUID) -> CompoundSelect:
    """The nine physical branches (Section 5), each already scoped to
    `organization_id` as the FIRST predicate in its own SELECT -- before
    its own status/decision/outcome filter, before any join, before the
    UNION ALL combines it with any other branch (Section 11 -- tenant
    isolation)."""
    branches = [
        _pending_review_branch(
            CleaningRun, "PROCESSING", "cleaning_run", "Cleaning Run Awaiting Review", organization_id
        ),
        _pending_review_branch(
            StandardizationRun,
            "PROCESSING",
            "standardization_run",
            "Standardization Run Awaiting Review",
            organization_id,
        ),
        _pending_review_branch(
            MatchRun, "MATCHING", "match_run", "Match Run Awaiting Review", organization_id
        ),
        _pending_review_branch(
            ExportRun, "EXPORT", "export_run", "Export Run Awaiting Review", organization_id
        ),
        _ambiguous_match_decision_branch(organization_id),
        _failed_task_run_branch(organization_id),
    ] + [
        _artifact_download_failure_branch(run_id_column, parent_model, organization_id)
        for run_id_column, parent_model in _DOWNLOAD_PARENTS
    ]
    return union_all(*branches)


@dataclass(frozen=True)
class ReviewQueueFilters:
    review_category: tuple[str, ...] | None = None
    review_type: tuple[str, ...] | None = None
    source: tuple[str, ...] | None = None
    search: str | None = None


@dataclass(frozen=True)
class ReviewQueuePage:
    items: list[dict]
    total: int
    summary: dict


def _apply_filters(rq, filters: ReviewQueueFilters, session: Session):
    """Applies category/type/source/search predicates directly against
    the already-unioned subquery's own columns. The Task Name / Dataset
    Name search join happens exactly once here, against the aggregated
    subquery -- not once per branch (see module docstring, clarification
    #3) -- and only when a search term is supplied."""
    conditions = []
    if filters.review_category:
        conditions.append(rq.c.review_category.in_(filters.review_category))
    if filters.review_type:
        conditions.append(rq.c.review_type.in_(filters.review_type))
    if filters.source:
        conditions.append(rq.c.source.in_(filters.source))

    base = select(rq)
    if filters.search:
        term = filters.search.strip()
        try:
            term_uuid = uuid.UUID(term)
        except ValueError:
            term_uuid = None

        if term_uuid is not None:
            # Task ID / Run ID -- exact match only in V1 (Section 6/8):
            # UUID-to-text prefix-cast portability across SQLite and
            # PostgreSQL has not been verified, so no prefix matching
            # ships here.
            conditions.append(
                (rq.c.task_id == term_uuid) | (rq.c.reference_id == term_uuid)
            )
        else:
            like_term = f"%{term.lower()}%"
            base = base.outerjoin(Task, Task.id == rq.c.task_id).outerjoin(
                DataSource, DataSource.id == rq.c.data_source_id
            )
            conditions.append(
                func.lower(Task.name).contains(like_term)
                | func.lower(DataSource.name).contains(like_term)
            )

    if conditions:
        combined = conditions[0]
        for extra in conditions[1:]:
            combined = combined & extra
        base = base.where(combined)
    return base


def fetch_review_queue(
    session: Session,
    organization_id: uuid.UUID,
    filters: ReviewQueueFilters,
    sort: SortOption,
    limit: int,
    offset: int,
) -> ReviewQueuePage:
    """Exactly two database queries, regardless of how many rows match
    (Section 3/10): Query A returns the requested page only; Query B
    returns at most eight grouped (review_category, review_type) count
    rows, aggregated here into the summary object and reused directly as
    `total` -- never a third query, and never a full materialization of
    the filtered row set."""
    union_subquery = _build_union(organization_id).subquery("rq")
    filtered = _apply_filters(union_subquery, filters, session)

    if sort == "confidence_score":
        order = (
            case((union_subquery.c.confidence_score.is_(None), 1), else_=0),
            union_subquery.c.confidence_score.asc(),
        )
    else:
        order = (union_subquery.c.created_at.asc(),)

    page_query = filtered.order_by(*order).limit(limit).offset(offset)
    rows = session.execute(page_query).mappings().all()
    items = [dict(row) for row in rows]

    # Query B: GROUP BY over the SAME filtered predicate set Query A used
    # (built from filtered.subquery(), not a second independent filter
    # construction) -- at most eight grouped rows, never the full
    # filtered row set (Section 10).
    filtered_subquery = filtered.subquery()
    summary_query = select(
        filtered_subquery.c.review_category,
        filtered_subquery.c.review_type,
        func.count().label("item_count"),
    ).group_by(filtered_subquery.c.review_category, filtered_subquery.c.review_type)
    summary_rows = session.execute(summary_query).all()

    total = 0
    pending_reviews = 0
    ambiguous_matches = 0
    failed_runs = 0
    download_failures = 0
    for category, review_type, count in summary_rows:
        total += count
        if review_type == "PENDING_REVIEW":
            pending_reviews += count
        if review_type == "AMBIGUOUS":
            ambiguous_matches += count
        if category == "SYSTEM" and review_type == "FAILED":
            failed_runs += count
        if category == "DOWNLOAD":
            download_failures += count

    summary = {
        "total_items": total,
        "pending_reviews": pending_reviews,
        "ambiguous_matches": ambiguous_matches,
        "failed_runs": failed_runs,
        "download_failures": download_failures,
    }
    return ReviewQueuePage(items=items, total=total, summary=summary)
