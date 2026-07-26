"""
Module 14: the Issue Detection Engine. Pure, deterministic, strictly
read-only analysis of a loaded CSV -- this package never opens a file for
writing, never mutates a row it is given, and never calls out to an
AI/LLM. See app.detection.engine.detect_issues (the only entry point) and
app.detection.base.DetectionRule (the interface every rule implements).

Package layout:
  types.py     -- pure dataclasses shared by every rule (no I/O).
  base.py      -- the DetectionRule interface every rule implements.
  issue_types.py / severities.py -- named constants for the two closed
      vocabularies in app.models.enums.ISSUE_TYPES/ISSUE_SEVERITIES.
      Rules reference these, never a bare string literal (approved
      Module 14 Phase 2 correction #2).
  rules/       -- one module per related group of rules; each rule is its
      own independent class.
  registry.py  -- DETECTION_RULES, the tuple engine.py iterates. Adding a
      new rule is exactly: write the class, add it to this tuple. Nothing
      in engine.py ever changes (approved Module 14 Phase 2 correction #3).
  engine.py    -- detect_issues(dataset, limits) -> DetectionResult: the
      only function that iterates DETECTION_RULES.

app.worker.handlers.issue_detection is the sole caller of engine.py, and
the only place in this package's call graph that touches a file, a
database session, or a Settings object -- everything under app.detection
itself is pure functions and dataclasses.
"""
