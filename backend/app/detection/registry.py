"""DETECTION_RULES: every registered detection rule, in the fixed order
app.detection.engine.detect_issues iterates them. Adding a new detector is
exactly two steps (approved Module 14 Phase 2 correction #3):
  1. write the rule class in app.detection.rules (implementing
     app.detection.base.DetectionRule);
  2. add one line to the tuple below.
Nothing in app.detection.engine ever needs to change -- it only ever
knows "iterate DETECTION_RULES, call .detect(dataset) on each"."""
from app.detection.base import DetectionRule
from app.detection.rules.blank_values import EmptyStringRule, MissingValueRule, NullValueRule
from app.detection.rules.boolean import BooleanInconsistencyRule
from app.detection.rules.capitalization import InconsistentCapitalizationRule
from app.detection.rules.duplicates import DuplicatePrimaryKeyRule, DuplicateRowRule
from app.detection.rules.enum_values import InvalidEnumValueRule
from app.detection.rules.foreign_keys import BrokenFkReferenceRule
from app.detection.rules.formats import (
    InvalidDateRule,
    InvalidEmailRule,
    InvalidNumericRule,
    InvalidPhoneRule,
)
from app.detection.rules.outliers import OutlierRule
from app.detection.rules.required import RequiredFieldViolationRule
from app.detection.rules.whitespace import (
    LeadingWhitespaceRule,
    MultipleInternalSpacesRule,
    TrailingWhitespaceRule,
)

DETECTION_RULES: tuple[DetectionRule, ...] = (
    MissingValueRule(),
    EmptyStringRule(),
    NullValueRule(),
    DuplicateRowRule(),
    DuplicatePrimaryKeyRule(),
    InvalidEmailRule(),
    InvalidPhoneRule(),
    InvalidDateRule(),
    InvalidNumericRule(),
    RequiredFieldViolationRule(),
    LeadingWhitespaceRule(),
    TrailingWhitespaceRule(),
    MultipleInternalSpacesRule(),
    InconsistentCapitalizationRule(),
    BooleanInconsistencyRule(),
    InvalidEnumValueRule(),
    OutlierRule(),
    # Deferred -- see BrokenFkReferenceRule's own docstring. Registered
    # (not omitted) so the "17 completed, 1 deferred" count is visible
    # directly in this tuple, not just in documentation.
    BrokenFkReferenceRule(),
)

assert len(DETECTION_RULES) == 18
assert len({rule.issue_type for rule in DETECTION_RULES}) == 18, (
    "two or more registered rules share an issue_type -- each rule in "
    "DETECTION_RULES must own a distinct issue_type"
)
