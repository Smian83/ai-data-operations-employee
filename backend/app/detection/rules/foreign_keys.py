"""Broken foreign-key reference detection (Module 14 requirement item 18)
-- DEFERRED. Per the approved Module 14 corrections: "Do not claim broken
foreign-key detection is complete if relationship metadata does not
exist. Implement the interface now, document it as deferred, and exclude
it from Module 14's completed detector count."

This project has no relationship-metadata concept for CSV data sources
today -- nothing records "column X in this data source references
column Y in that other data source". Without that, there is nothing a
deterministic rule could check a value against, and guessing one would
violate the same "never infer" principle every other rule in this package
follows. BrokenFkReferenceRule is therefore a real, registered
DetectionRule (app.detection.registry.DETECTION_RULES includes it, and
app.models.enums.ISSUE_TYPES/the ck_issues_issue_type_valid CHECK already
support its issue_type) whose detect() always yields nothing. It is NOT
counted among Module 14's 17 completed detectors.

A future module that adds relationship metadata can either replace this
class's body or register a new rule for the same issue_type -- either way
requires no change to app.detection.engine or app.detection.base, exactly
the "create rule, register rule, done" extensibility every other rule in
this package already demonstrates."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import BROKEN_FK_REFERENCE
from app.detection.types import DetectionDataset, Finding


class BrokenFkReferenceRule:
    issue_type = BROKEN_FK_REFERENCE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        return
        yield  # pragma: no cover -- makes this a generator function, never reached.
