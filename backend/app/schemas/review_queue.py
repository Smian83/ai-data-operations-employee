"""Module 11 -- Operational Review Queue response schemas.

Read-only, aggregation-only response contract. See
docs/module-11-operational-review-queue-design.md (Revision 3) for the
full design. Two small, closed, plain-string-tuple controlled
vocabularies -- REVIEW_CATEGORIES and REVIEW_TYPES -- classify every item;
`source` (a plain str, deliberately NOT a closed tuple) identifies the
fine-grained origin and is expected to grow additively as future modules
join the queue, exactly the same "small closed set vs. open growing set"
split this project already uses elsewhere (e.g. TaskType is closed and
rarely extended; a hypothetical open field would not be).

No `priority` field exists anywhere in this contract -- deliberately
removed during design review (Revision 2 proposed a reserved, always-NULL
field; Revision 3 removed it entirely, since an inert field provides no
real compatibility benefit and an API sort option that silently falls
back to another sort is misleading). Reintroducing prioritization is a
future module's decision, made only once real, tested prioritization
behavior exists.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

# Small, closed, additive-by-design controlled vocabularies -- plain string
# tuples, matching this project's established precedent (CLEANING_RUN_
# STATUSES, MATCH_DECISION_TYPES, ARTIFACT_DOWNLOAD_OUTCOMES), never a
# native Postgres enum. Adding a value here is treated as additive,
# non-breaking API evolution, the same guarantee already given TaskType.
REVIEW_CATEGORIES = ("PROCESSING", "MATCHING", "EXPORT", "DOWNLOAD", "SYSTEM")

REVIEW_TYPES = ("PENDING_REVIEW", "FAILED", "AMBIGUOUS", "INTEGRITY_FAILURE")


class ReviewQueueItemRead(BaseModel):
    """One consistent shape for every item, regardless of source. Fields
    that don't apply to a given source are NULL -- never a differently
    shaped object per source. See design Section 5/7."""

    model_config = ConfigDict(from_attributes=True)

    review_category: str
    review_type: str
    source: str
    label: str
    organization_id: uuid.UUID
    reference_id: uuid.UUID
    task_id: uuid.UUID | None
    task_run_id: uuid.UUID | None
    data_source_id: uuid.UUID | None
    confidence_score: float | None
    reason: str | None
    created_at: datetime


class ReviewQueueSummary(BaseModel):
    """Computed via database-level GROUP BY over the same filtered
    (pre-pagination) result set the request's `items` are drawn from --
    never by loading the full filtered row set into Python. See design
    Section 10."""

    total_items: int
    pending_reviews: int
    ambiguous_matches: int
    failed_runs: int
    download_failures: int


class ReviewQueueResponse(BaseModel):
    """Field-for-field identical to this project's existing
    PaginatedResponse[T] envelope (items/total/limit/offset), plus one
    addition (`summary`) -- not a nested pagination-inside-pagination
    envelope. See design Section 7."""

    items: list[ReviewQueueItemRead]
    total: int
    limit: int
    offset: int
    summary: ReviewQueueSummary
