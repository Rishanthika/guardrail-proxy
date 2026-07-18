"""
Unit tests for SafeConditionEvaluator — proving policy.yaml's `condition`
strings are evaluated safely (whitelisted AST walk) rather than via
eval()/exec(). This is the piece that makes it safe to let policy.yaml be
freely editable config rather than trusted code.
"""

from __future__ import annotations

import pytest

from src.condition_evaluator import SafeConditionEvaluator, UnsafeConditionError

evaluator = SafeConditionEvaluator()


# -----------------------------------------------------------------------------
# The exact expressions used in config/policy.yaml must work correctly.
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "condition,parameters,expected",
    [
        ("parameters.record_count > 100", {"record_count": 500}, True),
        ("parameters.record_count > 100", {"record_count": 5}, False),
        ("parameters.record_count <= 100", {"record_count": 5}, True),
        (
            "not parameters.email_to.endswith('@company.com')",
            {"email_to": "attacker@gmail.com"},
            True,
        ),
        (
            "not parameters.email_to.endswith('@company.com')",
            {"email_to": "boss@company.com"},
            False,
        ),
        (
            "parameters.email_to.endswith('@company.com')",
            {"email_to": "boss@company.com"},
            True,
        ),
        (
            "'confidential' in parameters.file_path",
            {"file_path": "/var/data/confidential_plan.txt"},
            True,
        ),
        ("'confidential' in parameters.file_path", {"file_path": "/var/data/plan.txt"}, False),
    ],
)
def test_conditions_matching_policy_yaml(condition, parameters, expected):
    assert evaluator.evaluate(condition, parameters) is expected


def test_missing_parameter_is_treated_as_no_match_not_a_crash():
    # record_count was never supplied — comparing None > 100 should resolve
    # to False (condition doesn't match), not raise.
    assert evaluator.evaluate("parameters.record_count > 100", {}) is False


def test_dunder_attribute_lookup_is_a_dict_get_not_real_introspection():
    # parameters.__class__ resolves via dict.get('__class__') -> None.
    # It does NOT return the actual `dict` type, so there is nothing here
    # for an attacker to pivot from into object internals.
    assert evaluator.evaluate("parameters.__class__", {}) is False


# -----------------------------------------------------------------------------
# Anything outside the whitelist must be rejected before evaluation, proving
# this is not a disguised eval().
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "malicious_condition",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read()",
        "exec('parameters')",
        "eval('1')",
        "().__class__.__bases__[0]",
        "some_other_variable > 1",
        "parameters.record_count.__class__.__mro__",
    ],
)
def test_unsafe_expressions_are_rejected(malicious_condition):
    with pytest.raises(UnsafeConditionError):
        evaluator.evaluate(malicious_condition, {"record_count": 1})


def test_syntax_errors_are_rejected_cleanly():
    with pytest.raises(UnsafeConditionError):
        evaluator.evaluate("parameters.record_count >", {"record_count": 1})
