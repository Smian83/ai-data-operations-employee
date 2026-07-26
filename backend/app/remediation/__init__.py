"""
Module 15: the Deterministic Cleaning (Remediation) Engine. Pure,
deterministic, no-AI computation of PROPOSED corrections for a subset of
the Issue rows a Module 14 IssueDetectionRun already persisted -- this
package never opens a file, never touches a database session, never
mutates the dataset it is given, and never writes a materialized output
file. See app.remediation.engine.remediate (the only entry point) and
app.remediation.base.RemediationRule (the interface every action
implements).

Package layout:
  types.py       -- pure dataclasses shared by every action (no I/O):
      RemediationDataset/RemediationIssueInput (inputs), RemediationColumn
      Config/RemediationDatasetConfigInput (resolved config), RemediationLimits,
      RuleOutcome (one action's verdict), RemediationChangeProposal/
      SkippedIssue/RemediationResult (outputs).
  skip_reasons.py -- the closed vocabulary of reasons a considered Issue
      produced no RemediationChange. Every skip the engine can ever
      produce uses exactly one of these constants -- never a free-text
      string -- so skip behavior itself stays auditable and testable.
  base.py        -- the RemediationRule interface every action implements.
  actions/       -- one module per action, each a small pure function
      (`propose_...`) plus a thin RemediationRule class wrapping it for
      the registry. Reuses app.standardization.rules functions directly
      wherever Module 7 already has the deterministic transformation
      Module 15 needs (booleans, phones, numeric) -- never a second
      implementation of logic that already exists elsewhere in this
      project.
  registry.py    -- REMEDIATION_RULES, the tuple engine.py dispatches
      through, plus get_rule_for_issue_type() for O(1) per-Issue lookup.
      Adding a new action is exactly: write the propose_ function + Rule
      class, add one line to the tuple. Nothing in engine.py ever changes.
  engine.py      -- remediate(dataset, issues, column_config, dataset_config,
      limits) -> RemediationResult: the only function that iterates
      REMEDIATION_RULES. Also owns REMEDIATION_ENGINE_VERSION, the small
      version string a later RemediationHandler persists verbatim onto
      RemediationRun.remediation_engine_version -- same "engine module
      owns its own version constant, handler imports and persists it"
      convention app.detection.engine.DETECTION_ENGINE_VERSION and
      app.cleaning.engine.CLEANING_ENGINE_VERSION already established.

A later Module 15 phase (RemediationHandler, app.worker.handlers.
remediation, not part of this package) is the sole caller of engine.py
and the only place in this call graph that touches a file, a database
session, or a Settings object -- everything under app.remediation itself
is pure functions and dataclasses, exactly the app.detection precedent
this package mirrors.
"""
