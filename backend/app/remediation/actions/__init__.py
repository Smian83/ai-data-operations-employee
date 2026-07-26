"""One module per Module 15 action. Each module exposes a small pure
`propose_...` function (the actual logic, independently unit-testable)
plus a thin class implementing app.remediation.base.RemediationRule that
wraps it for app.remediation.registry. Every module either calls an
existing app.standardization.rules function directly (booleans, phones,
numeric) or contains the small amount of genuinely new logic the Module
15 design doc identifies (capitalization, dates, enum_values, duplicates)
-- never a second implementation of anything that already exists
elsewhere in this project. trim/collapse contain no transformation logic
at all: both reuse Issue.suggested_fix, already computed by Module 14."""
