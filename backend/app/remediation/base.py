"""The RemediationRule interface every action in app.remediation.actions
implements. Deliberately the entire contract the engine (app.remediation.
engine.remediate) depends on -- it only ever knows "a RemediationRule owns
one or more issue_types and a propose() method that returns a RuleOutcome
given one RemediationIssueInput and its resolved config"; it never knows
or cares how any individual action decides its outcome. Adding a new
action never requires touching this file or engine.py, only writing a new
action module and adding one line to app.remediation.registry.
REMEDIATION_RULES."""
from __future__ import annotations

from typing import Protocol

from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)


class RemediationRule(Protocol):
    """Every implementation must be a pure function of its arguments -- no
    I/O, no wall-clock/locale/random dependence. Calling propose() twice
    with the same issue/column_config/dataset_config must return an
    identical RuleOutcome every time; this is the engine's determinism/
    repeatability acceptance criterion (see tests/test_remediation_
    repeatability.py), mirroring app.detection.base.DetectionRule's
    identical guarantee."""

    #: One or more app.detection.issue_types constants this rule handles.
    #: Usually exactly one; trim_whitespace is the sole exception (it
    #: handles both LEADING_WHITESPACE and TRAILING_WHITESPACE with
    #: identical logic, since both are the same action per the Module 15
    #: requirements). Never a bare string literal.
    issue_types: tuple[str, ...]

    #: Always one of app.models.enums.REMEDIATION_ACTIONS -- never a bare
    #: string literal.
    action: str

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        """Return this rule's verdict for one Issue already known to have
        issue_type in self.issue_types. column_config is the resolved
        config for issue.column_name (None if unconfigured or the issue
        has no single column, e.g. duplicate_row); dataset_config is
        always present (see RemediationDatasetConfigInput's own
        docstring). A rule that does not need one or the other simply
        ignores it, exactly as app.detection rules ignore the parts of
        DetectionDataset they don't ever ask for."""
        ...
