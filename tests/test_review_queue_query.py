"""Module 11 unit tests for the pure, framework-independent aggregation
module (app/review_queue/query.py). Builds minimal, directly-inserted ORM
rows (no HTTP, no handlers) covering all nine physical branches / eight
(review_category, review_type) classification outcomes, and asserts
against fetch_review_queue() directly -- exactly the "unit-testable
without a running API" property the approved design requires (Section 17
Phase 3). Runs against whatever DATABASE_URL the suite is pointed at
(SQLite in the sandbox, real PostgreSQL during the dedicated verification
pass), matching every other test file in this suite."""
import uuid
from datetime import datetime, timedelta, timezone

from app.models.artifact_download_event import ArtifactDownloadEvent
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.export_run import ExportRun
from app.models.match_decision import MatchDecision
from app.models.match_run import MatchRun
from app.models.organization import Organization
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.review_queue.query import ReviewQueueFilters, fetch_review_queue


def _base_run_kwargs(org_id, task_id, task_run_id, data_source_id, source_task_run_id, created_at):
    return dict(
        id=uuid.uuid4(), organization_id=org_id, task_run_id=task_run_id, task_id=task_id,
        data_source_id=data_source_id, source_task_run_id=source_task_run_id,
        output_file_path="/tenant/out.csv", output_sha256="a" * 64,
        confidence_score=0.9, status="pending_review", created_at=created_at,
    )


def _seed_full_queue(db_session, org_id: uuid.UUID, task_name: str = "Evidence Task", dataset_name: str = "Evidence Dataset"):
    """Builds exactly one item for each of the eight classification
    outcomes, plus one outcome='started' ArtifactDownloadEvent (which must
    never appear in results -- Section 4/18), all under the same org,
    task, and data source so Task Name / Dataset Name search has a single
    unambiguous target. Returns a dict of the created rows' ids for
    assertions."""
    now = datetime.now(timezone.utc)
    org = Organization(id=org_id, name=f"Org {org_id}", slug=f"org-{org_id.hex[:8]}")
    db_session.add(org)
    db_session.flush()
    data_source = DataSource(
        id=uuid.uuid4(), organization_id=org_id, name=dataset_name,
        source_type="csv_upload", connection_metadata={}, is_active=True,
    )
    db_session.add(data_source)
    task = Task(
        id=uuid.uuid4(), organization_id=org_id, data_source_id=data_source.id,
        name=task_name, task_type="transform", is_active=True,
    )
    db_session.add(task)
    db_session.flush()

    def new_task_run(status="success", offset_seconds=0, **kw):
        created_at = now - timedelta(seconds=offset_seconds)
        if status in ("success", "failed") and "started_at" not in kw:
            kw["started_at"] = created_at
        if status in ("success", "failed") and "finished_at" not in kw:
            kw["finished_at"] = created_at
        tr = TaskRun(
            id=uuid.uuid4(), organization_id=org_id, task_id=task.id, status=status,
            idempotency_key=uuid.uuid4(), attempt_count=1,
            created_at=created_at,
            **kw,
        )
        db_session.add(tr)
        db_session.flush()
        return tr

    ids = {}

    cleaning_tr = new_task_run(offset_seconds=100)
    cleaning_run = CleaningRun(
        **_base_run_kwargs(org_id, task.id, cleaning_tr.id, data_source.id, cleaning_tr.id, cleaning_tr.created_at),
        row_count=10, total_changes_count=1, changes_by_rule={}, duplicate_row_count=0,
        post_clean_row_count=10, post_clean_missing_value_total=0, post_clean_duplicate_row_count=0,
        cleaning_engine_version="1.0",
    )
    db_session.add(cleaning_run)
    ids["cleaning_run"] = cleaning_run.id

    std_tr = new_task_run(offset_seconds=90)
    std_run = StandardizationRun(
        **_base_run_kwargs(org_id, task.id, std_tr.id, data_source.id, std_tr.id, std_tr.created_at),
        row_count=10, total_changes_count=1, changes_by_rule={}, standardization_engine_version="1.0",
    )
    db_session.add(std_run)
    ids["standardization_run"] = std_run.id

    match_tr = new_task_run(offset_seconds=80)
    match_kwargs = _base_run_kwargs(org_id, task.id, match_tr.id, data_source.id, match_tr.id, match_tr.created_at)
    match_kwargs.pop("output_file_path")
    match_kwargs.pop("output_sha256")
    match_run = MatchRun(
        **match_kwargs,
        row_count=10, total_comparisons_count=20, duplicate_group_count=1,
        duplicate_pairs_count=1, ambiguous_pairs_count=1, skipped_block_count=0,
        decisions_by_rule={}, match_engine_version="1.0",
    )
    db_session.add(match_run)
    db_session.flush()
    ids["match_run"] = match_run.id

    export_tr = new_task_run(offset_seconds=70)
    export_kwargs = _base_run_kwargs(org_id, task.id, export_tr.id, data_source.id, export_tr.id, export_tr.created_at)
    export_kwargs.pop("confidence_score")
    export_run = ExportRun(
        **export_kwargs, match_run_id=match_run.id,
        source_row_count=10, row_count=10, excluded_row_count=0,
        duplicate_groups_materialized_count=0, output_file_size_bytes=100,
        output_column_count=3, export_timestamp=now, csv_format_version=1,
        export_engine_version="1.0",
    )
    db_session.add(export_run)
    ids["export_run"] = export_run.id

    ambiguous_decision = MatchDecision(
        id=uuid.uuid4(), organization_id=org_id, match_run_id=match_run.id, match_group_id=None,
        record_a_row_index=0, record_b_row_index=1, rule_name="exact_row_match",
        field_comparisons={}, total_score=0.6, threshold_used=0.8, decision="ambiguous",
        confidence_score=0.6, reason="Fields disagree on email", rule_version="1.0",
        created_at=now - timedelta(seconds=60),
    )
    db_session.add(ambiguous_decision)
    ids["match_decision"] = ambiguous_decision.id

    failed_tr = new_task_run(
        status="failed", offset_seconds=50, error_message="Simulated failure",
    )
    ids["task_run"] = failed_tr.id

    download_source_kwargs = _base_run_kwargs(
        org_id, task.id, new_task_run(offset_seconds=40).id, data_source.id,
        None, now - timedelta(seconds=40),
    )
    download_source_kwargs["source_task_run_id"] = download_source_kwargs["task_run_id"]
    download_source_kwargs["status"] = "approved"
    cleaning_for_download = CleaningRun(
        **download_source_kwargs,
        row_count=10, total_changes_count=1, changes_by_rule={}, duplicate_row_count=0,
        post_clean_row_count=10, post_clean_missing_value_total=0, post_clean_duplicate_row_count=0,
        cleaning_engine_version="1.0",
    )
    db_session.add(cleaning_for_download)
    db_session.flush()

    integrity_failure = ArtifactDownloadEvent(
        id=uuid.uuid4(), organization_id=org_id, artifact_type="cleaning",
        cleaning_run_id=cleaning_for_download.id,
        run_status_at_request="approved", outcome="integrity_failed",
        failure_reason_code="hash_mismatch", verified_sha256="a" * 64, bytes_served=0,
        created_at=now - timedelta(seconds=30), completed_at=now - timedelta(seconds=29),
    )
    db_session.add(integrity_failure)
    ids["artifact_download_event_integrity"] = integrity_failure.id

    delivery_failure = ArtifactDownloadEvent(
        id=uuid.uuid4(), organization_id=org_id, artifact_type="cleaning",
        cleaning_run_id=cleaning_for_download.id,
        run_status_at_request="approved", outcome="file_missing",
        failure_reason_code="file_not_found", bytes_served=0,
        created_at=now - timedelta(seconds=20), completed_at=now - timedelta(seconds=19),
    )
    db_session.add(delivery_failure)
    ids["artifact_download_event_failed"] = delivery_failure.id

    # Deliberately-excluded 'started' row (Section 4/18) -- must NEVER
    # appear in any query result below.
    started_event = ArtifactDownloadEvent(
        id=uuid.uuid4(), organization_id=org_id, artifact_type="cleaning",
        cleaning_run_id=cleaning_for_download.id,
        run_status_at_request="approved", outcome="started", bytes_served=0,
        created_at=now - timedelta(seconds=10), completed_at=None,
    )
    db_session.add(started_event)
    ids["artifact_download_event_started"] = started_event.id

    db_session.commit()
    ids["organization_id"] = org_id
    ids["task_id"] = task.id
    ids["data_source_id"] = data_source.id
    return ids


def test_all_eight_classification_outcomes_present(db_session):
    org_id = uuid.uuid4()
    ids = _seed_full_queue(db_session, org_id)

    page = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "created_at", limit=100, offset=0)

    outcomes = {(i["review_category"], i["review_type"], i["source"]) for i in page.items}
    assert ("PROCESSING", "PENDING_REVIEW", "cleaning_run") in outcomes
    assert ("PROCESSING", "PENDING_REVIEW", "standardization_run") in outcomes
    assert ("MATCHING", "PENDING_REVIEW", "match_run") in outcomes
    assert ("EXPORT", "PENDING_REVIEW", "export_run") in outcomes
    assert ("MATCHING", "AMBIGUOUS", "match_decision") in outcomes
    assert ("SYSTEM", "FAILED", "task_run") in outcomes
    assert ("DOWNLOAD", "INTEGRITY_FAILURE", "artifact_download_event") in outcomes
    assert ("DOWNLOAD", "FAILED", "artifact_download_event") in outcomes
    # 8 real items + 1 deliberately-excluded 'started' row that must NOT appear.
    assert page.total == 8
    started_ids = {ids["artifact_download_event_started"]}
    returned_ids = {i["reference_id"] for i in page.items}
    assert started_ids.isdisjoint(returned_ids)


def test_match_decision_reason_is_populated_not_null(db_session):
    """Regression test: the approved design's own Section 5 mapping table
    incorrectly claimed match_decisions has no reason column and should
    be NULL. The column exists and is NOT NULL -- this branch must
    populate it, not leave it NULL."""
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(review_type=("AMBIGUOUS",)), "created_at", limit=10, offset=0
    )
    assert len(page.items) == 1
    assert page.items[0]["reason"] == "Fields disagree on email"


def test_cross_organization_isolation(db_session):
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    _seed_full_queue(db_session, org_a, task_name="Org A Task", dataset_name="Org A Dataset")
    _seed_full_queue(db_session, org_b, task_name="Org B Task", dataset_name="Org B Dataset")

    page_a = fetch_review_queue(db_session, org_a, ReviewQueueFilters(), "created_at", limit=100, offset=0)
    assert page_a.total == 8
    assert all(i["organization_id"] == org_a for i in page_a.items)

    page_b = fetch_review_queue(db_session, org_b, ReviewQueueFilters(), "created_at", limit=100, offset=0)
    assert page_b.total == 8
    assert all(i["organization_id"] == org_b for i in page_b.items)


def test_review_category_filter(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(review_category=("DOWNLOAD",)), "created_at", limit=10, offset=0
    )
    assert page.total == 2
    assert all(i["review_category"] == "DOWNLOAD" for i in page.items)
    assert page.summary["download_failures"] == 2
    assert page.summary["pending_reviews"] == 0


def test_review_type_filter(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(review_type=("PENDING_REVIEW",)), "created_at", limit=10, offset=0
    )
    assert page.total == 4
    assert page.summary["pending_reviews"] == 4


def test_source_filter(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(source=("task_run",)), "created_at", limit=10, offset=0
    )
    assert page.total == 1
    assert page.items[0]["source"] == "task_run"


def test_search_matches_task_name(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id, task_name="Unique Searchable Task Name")
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(search="searchable task"), "created_at", limit=10, offset=0
    )
    assert page.total == 8  # every item shares the same task


def test_search_matches_dataset_name(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id, dataset_name="Unique Searchable Dataset")
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(search="searchable dataset"), "created_at", limit=10, offset=0
    )
    assert page.total == 8


def test_search_no_match_returns_empty(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(search="totally unrelated term xyz"), "created_at", limit=10, offset=0
    )
    assert page.total == 0
    assert page.items == []
    assert page.summary["total_items"] == 0


def test_search_exact_task_id_match(db_session):
    org_id = uuid.uuid4()
    ids = _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(search=str(ids["task_id"])), "created_at", limit=10, offset=0
    )
    # Every item shares the same task_id except the "for_download" cleaning
    # run's own task_run (still same task though) -- all 8 share task_id.
    assert page.total == 8


def test_search_exact_reference_id_match(db_session):
    org_id = uuid.uuid4()
    ids = _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(
        db_session, org_id, ReviewQueueFilters(search=str(ids["cleaning_run"])), "created_at", limit=10, offset=0
    )
    assert page.total == 1
    assert page.items[0]["reference_id"] == ids["cleaning_run"]


def test_sort_created_at_ascending_is_default(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "created_at", limit=100, offset=0)
    timestamps = [i["created_at"] for i in page.items]
    assert timestamps == sorted(timestamps)


def test_sort_confidence_score_ascending_nulls_last(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "confidence_score", limit=100, offset=0)
    scores = [i["confidence_score"] for i in page.items]
    non_null = [s for s in scores if s is not None]
    assert non_null == sorted(non_null)
    # Every NULL-scored item (task_run, both artifact_download_event rows)
    # must sort after every scored item.
    first_null_index = next(i for i, s in enumerate(scores) if s is None)
    assert all(s is not None for s in scores[:first_null_index])
    assert all(s is None for s in scores[first_null_index:])


def test_pagination_limit_and_offset(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page1 = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "created_at", limit=3, offset=0)
    page2 = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "created_at", limit=3, offset=3)
    assert len(page1.items) == 3
    assert len(page2.items) == 3
    assert page1.total == 8
    assert page2.total == 8
    ids_page1 = {i["reference_id"] for i in page1.items}
    ids_page2 = {i["reference_id"] for i in page2.items}
    assert ids_page1.isdisjoint(ids_page2)


def test_summary_total_matches_items_total_field(db_session):
    org_id = uuid.uuid4()
    _seed_full_queue(db_session, org_id)
    page = fetch_review_queue(db_session, org_id, ReviewQueueFilters(), "created_at", limit=2, offset=0)
    # total and summary.total_items must both reflect the FULL filtered
    # count, ignoring limit/offset -- only `items` is paginated.
    assert len(page.items) == 2
    assert page.total == 8
    assert page.summary["total_items"] == 8
