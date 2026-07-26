"""The DetectionRule interface every rule in app.detection.rules
implements. Deliberately the entire contract the engine (app.detection.
engine.detect_issues) depends on -- see this package's own docstring's
correction #3: the engine only ever knows "a DetectionRule has an
issue_type and a detect() method that yields Finding objects given a
DetectionDataset"; it never knows or cares how any individual rule
decides what to yield. Adding a new detector never requires touching this
file or engine.py, only writing a new rule class and adding one line to
app.detection.registry.DETECTION_RULES."""
from typing import Iterator, Protocol

from app.detection.types import DetectionDataset, Finding


class DetectionRule(Protocol):
    """Every implementation must be a pure function of its
    DetectionDataset argument -- no I/O, no wall-clock/locale/random
    dependence, no mutation of the dataset it is given (see
    DetectionDataset's own read-only guarantee). Calling detect() twice
    with the same dataset must yield the same findings in the same order
    every time; this is the engine's determinism/repeatability
    acceptance criterion (see tests/test_detection_repeatability.py)."""

    #: Always one of app.detection.issue_types' constants -- never a bare
    #: string literal (approved Module 14 Phase 2 correction #2).
    issue_type: str

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        """Yield zero or more Finding objects, each with
        issue_type == self.issue_type. Every Finding yielded here must
        already satisfy every CHECK constraint on app.models.issue.Issue
        (severity in ISSUE_SEVERITIES, 0 <= confidence <= 1, row_number
        >= 0) -- the handler persists these as-is, with no further
        validation or correction step of its own."""
        ...
