"""Module 15 Phase 2 registry contract tests. The module-level asserts in
app.remediation.registry already guard internal consistency at import
time (no duplicate actions, no duplicate issue_type claims, exact counts)
-- these tests instead cover the public contract callers (the engine, and
later phases) actually depend on."""
from app.models.enums import REMEDIATION_ACTIONS
from app.remediation.registry import REMEDIATION_RULES, get_rule_for_issue_type


def test_every_registered_action_is_in_the_closed_vocabulary():
    for rule in REMEDIATION_RULES:
        assert rule.action in REMEDIATION_ACTIONS


def test_every_action_in_the_closed_vocabulary_has_exactly_one_rule():
    registered_actions = {rule.action for rule in REMEDIATION_RULES}
    assert registered_actions == set(REMEDIATION_ACTIONS)


def test_unmapped_issue_type_returns_none_not_an_error():
    from app.detection.issue_types import MISSING_VALUE

    assert get_rule_for_issue_type(MISSING_VALUE) is None


def test_every_mapped_issue_type_resolves_to_a_rule_that_claims_it():
    for rule in REMEDIATION_RULES:
        for issue_type in rule.issue_types:
            resolved = get_rule_for_issue_type(issue_type)
            assert resolved is rule


def test_trim_whitespace_is_the_only_action_covering_two_issue_types():
    multi_issue_type_rules = [rule for rule in REMEDIATION_RULES if len(rule.issue_types) > 1]
    assert len(multi_issue_type_rules) == 1
    assert multi_issue_type_rules[0].action == "trim_whitespace"
