"""
Module 17: Validation Engine.

Pure validation of approved RemediationChange proposals. No I/O, no
database/session references, no mutation of any upstream row anywhere
in this package. This package is the structural sibling of
app.remediation (Module 15) and app.detection (Module 14).

Entry point: app.validation.engine.validate()
"""
