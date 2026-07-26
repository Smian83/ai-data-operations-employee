"""Handler registry: maps TaskType -> ExecutionHandler. Adding a real
connector for a task type means writing a new handler module and
registering it here -- the engine itself never changes.

Module 5 update: SYNC now maps to CsvProfilingHandler, the first real
(non-diagnostic) handler -- exactly the "follow-up module" Module 4's own
docstring called out. This is a deliberate behavior change, not a purely
additive one: any Task with task_type=SYNC now executes real CSV-profiling
logic instead of a no-op, and requires an active CSV_UPLOAD data source or
fails permanently (see CsvProfilingHandler.execute).

Module 6 update: TRANSFORM now maps to CleaningHandler, following the
exact same pattern -- also a deliberate, non-additive behavior change: any
Task with task_type=TRANSFORM now executes real CSV-cleaning logic instead
of a no-op, and requires a completed DataProfile for the TaskRun's
source_task_run_id or fails permanently (see CleaningHandler.execute).

Module 7 update: a NEW TaskType value, STANDARDIZE, now maps to
StandardizationHandler -- purely additive to the registry (TRANSFORM,
SYNC, EXPORT, and OTHER are all unaffected), since no existing task_type
was available to reuse (see docs/module-7-data-standardization-engine-
design.md Section 2 for why). A Task with task_type=STANDARDIZE requires
an APPROVED CleaningRun for the TaskRun's source_task_run_id or fails
permanently (see StandardizationHandler.execute).

Module 8 update: another NEW TaskType value, MATCH, now maps to
MatchHandler -- purely additive again (SYNC, TRANSFORM, STANDARDIZE,
EXPORT, and OTHER are all unaffected). A Task with task_type=MATCH
requires an APPROVED StandardizationRun for the TaskRun's source_task_
run_id or fails permanently (see MatchHandler.execute). Unlike every
handler before it, MatchHandler writes no output file at all -- see
docs/module-8-data-matching-deduplication-design.md Section 2.

Module 9 update: EXPORT now maps to ExportHandler -- the first registry
update in this project's history that requires NO new TaskType value at
all, since TaskType.EXPORT already existed (see app.models.enums) and
was simply reserved on NoOpHandler until now. Same deliberate,
non-additive behavior-change pattern as Module 5/6's SYNC/TRANSFORM
updates: any existing Task with task_type=EXPORT now executes real
deduplicated-CSV-materialization logic instead of a no-op. A Task with
task_type=EXPORT requires an APPROVED MatchRun for the TaskRun's
source_task_run_id or fails permanently (see ExportHandler.execute).
ExportHandler is the first handler since StandardizationHandler to write
a real output file -- see
docs/module-9-data-export-engine-design.md Section 2. OTHER remains on
NoOpHandler until its own follow-up module, if ever.

Module 14 Phase 1 update: another NEW TaskType value, DETECT, was
registered on NoOpHandler temporarily -- same placeholder pattern
OTHER/EXPORT/STANDARDIZE/MATCH all passed through before their own real
handler existed, keeping test_registry_has_a_handler_for_every_task_type
(tests/test_worker_handlers.py) green while Module 14's database layer
(models/migration/config only) landed ahead of its worker handler and
API, by explicit, approved phase-scoping.

Module 14 Phase 2 update: DETECT now maps to the real
IssueDetectionHandler, replacing that placeholder -- the same
purely-in-place swap Module 9 performed for EXPORT's own NoOpHandler
placeholder. A Task with task_type=DETECT now executes real,
strictly-read-only issue-detection logic against the data source's raw
synced CSV instead of a no-op (see IssueDetectionHandler.execute).

Module 15 Phase 1 update: another NEW TaskType value, REMEDIATE, was
registered on NoOpHandler temporarily -- same placeholder pattern DETECT
itself passed through in Module 14 Phase 1, keeping
test_registry_has_a_handler_for_every_task_type
(tests/test_worker_handlers.py) green while Module 15's database layer
(models/migration/config only) landed ahead of its worker handler and
API, by explicit, approved phase-scoping.

Module 15 Phase 3 update: REMEDIATE now maps to the real
RemediationHandler, replacing that placeholder -- the same
purely-in-place swap Module 14 Phase 2 performed for DETECT's own
NoOpHandler placeholder. A Task with task_type=REMEDIATE now executes
real, strictly-read-only remediation-proposal logic against the upstream
Module 14 IssueDetectionRun's Issues instead of a no-op (see
RemediationHandler.execute).

Module 17 Phase 1 update: another NEW TaskType value, VALIDATE, was
registered on NoOpHandler temporarily -- same placeholder pattern DETECT
and REMEDIATE both passed through, keeping
test_registry_has_a_handler_for_every_task_type
(tests/test_worker_handlers.py) green while Module 17's database layer
(models/migration/config only) landed ahead of its worker handler and
API, by explicit, approved phase-scoping.

Module 17 Phase 3 update: VALIDATE now maps to the real
ValidationHandler, replacing that placeholder -- the same purely-in-place
swap Module 15 Phase 3 performed for REMEDIATE's own NoOpHandler
placeholder. A Task with task_type=VALIDATE now executes real,
strictly-read-only validation logic against the upstream Module 15
RemediationRun's approved RemediationChange proposals instead of a no-op
(see ValidationHandler.execute).

Module 18 Phase 1 update: another NEW TaskType value, QUALITY_CTRL, is
registered on NoOpHandler temporarily -- same placeholder pattern DETECT,
REMEDIATE, and VALIDATE each passed through, keeping
test_registry_has_a_handler_for_every_task_type
(tests/test_worker_handlers.py) green while Module 18's database layer
(models/migration/config only) lands ahead of its worker handler and API,
by explicit, approved phase-scoping."""
from app.models.enums import TaskType
from app.worker.handlers.apply_remediations import ApprovedChangesApplicatorHandler
from app.worker.handlers.base import ExecutionHandler
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.export import ExportHandler
from app.worker.handlers.issue_detection import IssueDetectionHandler
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.noop import NoOpHandler
from app.worker.handlers.quality_control import QualityControlHandler
from app.worker.handlers.remediation import RemediationHandler
from app.worker.handlers.standardization import StandardizationHandler
from app.worker.handlers.validation import ValidationHandler
from app.worker.handlers.clean_export import CleanExportHandler

HANDLER_REGISTRY: dict[TaskType, ExecutionHandler] = {
    TaskType.SYNC: CsvProfilingHandler(),
    TaskType.TRANSFORM: CleaningHandler(),
    TaskType.EXPORT: ExportHandler(),
    TaskType.OTHER: NoOpHandler(),
    TaskType.STANDARDIZE: StandardizationHandler(),
    TaskType.MATCH: MatchHandler(),
    TaskType.DETECT: IssueDetectionHandler(),
    TaskType.REMEDIATE: RemediationHandler(),
    # APPLY_REMEDIATIONS: new materialization step between Module 16 (approval)
    # and Module 17 (validation). Reads the approved ExportRun artifact,
    # applies only approved RemediationChange proposals (filtered through
    # Module 16 RemediationChangeDecision rows), and writes an immutable
    # remediated CSV to csv_remediated_root. Unlike REMEDIATE (proposals only),
    # this handler DOES write an output file -- see
    # app.worker.handlers.apply_remediations for the full algorithm.
    TaskType.APPLY_REMEDIATIONS: ApprovedChangesApplicatorHandler(),
    TaskType.VALIDATE: ValidationHandler(),
    # Module 18 Phase 3: NoOpHandler replaced with QualityControlHandler.
    TaskType.QUALITY_CTRL: QualityControlHandler(),
    # Module 19 Phase 3: NoOpHandler replaced with CleanExportHandler.
    # Unlike every prior "read-only" module (DETECT/REMEDIATE/VALIDATE/
    # QUALITY_CTRL), CLEAN_EXPORT produces a file artifact -- the final
    # approved, quality-controlled export in CSV (default) or XLSX format.
    # The primary trigger is the API (POST /jobs/{job_id}/exports);
    # this handler supports worker-dispatched CLEAN_EXPORT TaskRuns.
    TaskType.CLEAN_EXPORT: CleanExportHandler(),
}


def get_handler(task_type: TaskType) -> ExecutionHandler:
    try:
        return HANDLER_REGISTRY[task_type]
    except KeyError:
        raise PermanentHandlerLookupError(f"No execution handler registered for task_type={task_type}")


class PermanentHandlerLookupError(Exception):
    """Raised when a Task's task_type has no registered handler. Treated by
    the runner as a permanent failure -- retrying will never help."""
