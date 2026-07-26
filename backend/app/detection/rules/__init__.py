"""Individual detection rules -- each class is completely independent
(approved Module 14 Phase 2 correction #3): it implements
app.detection.base.DetectionRule on its own, reads only the
DetectionDataset it is given, and knows nothing about any other rule.
Grouped into a handful of modules purely for file organization (related
rules sharing a topic); this is not a dependency between the rules
themselves. See app.detection.registry.DETECTION_RULES for the tuple that
actually wires each class into the engine."""
