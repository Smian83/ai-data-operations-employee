"""operational review queue indexes

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-22 09:00:00.000000

Module 11 -- Operational Review Queue. This migration adds no table, no
column, and no enum value -- see docs/module-11-operational-review-queue-
design.md Section 12/17. It adds exactly six composite indexes, one per
source table the queue's GET /review-queue aggregation filters on.

Evidence, not assumption (Revision 3's explicit requirement): before this
migration was written, EXPLAIN (ANALYZE, BUFFERS) was captured against a
representative seeded PostgreSQL 16 database (3 organizations, several
hundred rows per source table per organization with a realistic status/
decision/outcome distribution, not a uniform or trivially small dataset).
At that near-term scale, the existing organization_id-only index already
produces an efficient Bitmap Heap Scan (~10 heap blocks, sub-millisecond),
and these composite indexes made no measurable difference -- confirming
they introduce no regression risk at current volumes.

A second pass seeded one organization with ~20,750 cleaning_runs rows (a
deliberately large-tenant scenario) with the org_id-only index in place:
the planner fell back to a full Seq Scan for the queue's actual
'pending_review' filter (~15% selectivity), costing 3.2ms and touching
essentially the whole table. Adding this migration's composite
(organization_id, status) index dropped that same query to a Bitmap Heap
Scan at 1.4ms -- roughly a 2.25x improvement, and the query touched only
matching rows' heap blocks instead of the whole table. Critically, the
same index was re-tested against a high-selectivity filter on the same
large dataset (status = 'approved', ~70% of rows): the planner correctly
chose to ignore the new index and kept using a Seq Scan, confirming the
index is not redundant -- it is used exactly where the queue's own
low-selectivity workload (pending_review/failed/ambiguous/integrity-
failure items are always a minority of an organization's total rows)
benefits, and left unused where it would not help.

The remaining five indexes (standardization_runs, match_runs, export_runs,
task_runs, match_decisions) were not independently large-scale-tested --
this is disclosed explicitly, not asserted as equally verified. They are
added by structural analogy: each is the identical shape (a UUID
organization_id foreign key plus a small, closed-vocabulary string status/
decision column, with only organization_id indexed today), so the same
selectivity argument applies. See docs/module-11-operational-review-queue-
design.md Section 12 and the Module 11 implementation notes for the full
methodology and raw EXPLAIN output.

All six index names were measured against PostgreSQL's 63-byte
(NAMEDATALEN) identifier limit before being written here -- the longest,
ix_standardization_runs_org_status, is 35 characters, comfortably under
the limit.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_cleaning_runs_org_status", "cleaning_runs",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_standardization_runs_org_status", "standardization_runs",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_match_runs_org_status", "match_runs",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_export_runs_org_status", "export_runs",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_task_runs_org_status", "task_runs",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_match_decisions_org_decision", "match_decisions",
        ["organization_id", "decision"],
    )


def downgrade() -> None:
    op.drop_index("ix_match_decisions_org_decision", table_name="match_decisions")
    op.drop_index("ix_task_runs_org_status", table_name="task_runs")
    op.drop_index("ix_export_runs_org_status", table_name="export_runs")
    op.drop_index("ix_match_runs_org_status", table_name="match_runs")
    op.drop_index("ix_standardization_runs_org_status", table_name="standardization_runs")
    op.drop_index("ix_cleaning_runs_org_status", table_name="cleaning_runs")
