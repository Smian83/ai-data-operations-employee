"""Named constants for app.models.enums.ISSUE_SEVERITIES -- same
constants-not-literals reasoning as app.detection.issue_types."""
from app.models.enums import ISSUE_SEVERITIES

INFO = "INFO"
LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
CRITICAL = "CRITICAL"

_ALL = (INFO, LOW, MEDIUM, HIGH, CRITICAL)
assert _ALL == ISSUE_SEVERITIES, (
    "app.detection.severities has drifted from app.models.enums.ISSUE_SEVERITIES"
)
