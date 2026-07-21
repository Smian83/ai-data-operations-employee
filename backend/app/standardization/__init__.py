"""Module 7: deterministic, rule-based data standardization. Mirrors
app.cleaning's package shape (types/engine/rules), split further given
the larger rule surface -- one rules/ sub-module per field-type family.
Every function in this package is pure: no I/O, no randomness, no
wall-clock dependence, no AI/ML. See
docs/module-7-data-standardization-engine-design.md."""
