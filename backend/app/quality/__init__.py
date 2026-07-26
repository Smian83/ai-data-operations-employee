"""app.quality — pure deterministic Quality Control Engine (Module 18 Phase 2).

The public API of this package is a single pure function:

    from app.quality.engine import quality_control

    result: QualityEngineResult = quality_control(inputs)

No I/O, no ORM session, no FastAPI, no randomness, no external calls anywhere
in this package. Every module is pure Python over frozen dataclasses.

Deterministic contract: identical QualityEngineInput produces byte-identical
QualityEngineResult every time. Verified by tests/test_quality_repeatability.py.
"""
