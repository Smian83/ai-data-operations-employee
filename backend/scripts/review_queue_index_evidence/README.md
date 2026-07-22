# Module 11 -- Operational Review Queue: index evidence scripts

Preserves, in reproducible form, the methodology used to justify the six
composite indexes added by
`database/alembic/versions/c5d6e7f8a9b0_review_queue_indexes.py` (see
that migration's own docstring and
`docs/module-11-operational-review-queue-implementation-notes.md`
Section 6 for the narrative summary and reported results).

These scripts are standalone. They are never imported by the application
or the test suite, and nothing here runs automatically -- they must be
invoked manually, on purpose, against a disposable database.

**Never run these against a production or shared database.** Both
scripts insert tens of thousands of rows and are only meaningful against
an empty or throwaway PostgreSQL database.

## Expected environment

- PostgreSQL 16 (the version this project targets and the version the
  original evidence was captured against -- see `docker/docker-compose.yml`).
- The schema must already be migrated: run `alembic upgrade head` from
  `backend/` against your target database *before* running either script.
  Do not rely on `Base.metadata.create_all()` -- several models use
  `create_type=False` on enum-like columns deliberately (see
  `app/models/data_source.py`), so only Alembic can create the schema.
- `DATABASE_URL` must be set in the environment, pointing at the
  disposable database, e.g.:
  `postgresql+psycopg://postgres:@localhost:5432/review_queue_evidence`

## Usage

```
cd backend
alembic upgrade head          # against the disposable database only
python3 scripts/review_queue_index_evidence/seed_representative.py
python3 scripts/review_queue_index_evidence/seed_large_scale.py
```

`seed_representative.py` creates 3 organizations with several hundred
rows per source table per organization, at a realistic (non-uniform)
status/decision/outcome distribution -- the near-term-scale pass. It
prints the created organization ids and, notably, the id of "Evidence
Org 0", which `seed_large_scale.py` looks up by name and extends.

`seed_large_scale.py` must be run after `seed_representative.py` against
the same database. It adds ~20,000 additional `cleaning_runs` rows (and
their backing `task_runs`) to "Evidence Org 0" -- the deliberate
large-tenant scenario that actually showed a measurable index benefit.

## Reproducing the EXPLAIN comparison

Against the seeded database, compare the planner's choice for the
queue's actual low-selectivity filter, before and after the six indexes
exist:

```sql
-- Baseline (before migration, or after DROP INDEX -- see below)
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM cleaning_runs
WHERE organization_id = '<evidence-org-0-id>' AND status = 'pending_review';

-- After the migration's indexes exist
-- (same query -- expect Bitmap Heap Scan via ix_cleaning_runs_org_status
-- instead of a Seq Scan, once the large-scale data from
-- seed_large_scale.py is present)
```

To confirm non-redundancy (the index should NOT be preferred for a
high-selectivity filter on the same large dataset):

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM cleaning_runs
WHERE organization_id = '<evidence-org-0-id>' AND status = 'approved';
-- Expect the planner to keep using a Seq Scan here even with the index
-- present -- 'approved' is ~70% of rows, too high-selectivity to benefit.
```

To test before-migration behavior without re-provisioning the database,
you can temporarily drop just the one index under test and re-run the
first EXPLAIN:

```sql
DROP INDEX IF EXISTS ix_cleaning_runs_org_status;
-- ...re-run the baseline EXPLAIN...
-- then restore it:
CREATE INDEX ix_cleaning_runs_org_status ON cleaning_runs (organization_id, status);
```

Or simply `alembic downgrade -1` / `alembic upgrade head` to restore all
six indexes via the real migration.

The other five indexes (`standardization_runs`, `match_runs`,
`export_runs`, `task_runs`, `match_decisions`) were not independently
large-scale-tested -- only `cleaning_runs` was extended to ~20,000 rows.
The same EXPLAIN pattern above can be adapted to any of them (swap the
table/status-or-decision column and the corresponding index name) if
you want to extend the evidence.

## Cleanup

The safest cleanup is to discard the disposable database entirely
(`DROP DATABASE ...` or destroy the container/volume it lived in) --
these scripts are not meant to run against anything you'd want to keep.

If you'd rather clean up in place, every row these scripts create is
traceable to organizations named `Evidence Org 0/1/2` with slugs
`evidence-org-0/1/2`. Because every table with rows created here has a
tenant-aware composite foreign key back to `organizations`
(`ON DELETE CASCADE`), deleting the organizations cascades everything:

```sql
DELETE FROM organizations WHERE slug LIKE 'evidence-org-%';
```

No credentials, secrets, or machine-specific paths are embedded in
either script -- `DATABASE_URL` must be supplied by the caller's
environment, and both scripts locate the `backend/` package via a path
computed relative to their own file location.
