"""Tests for agent workflow policy integration."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.workflow_policy import workflow_policy_for_intent
from symbio.utils.types import Intent


def test_feature_work_requires_plan_tdd_and_verification():
    policy = workflow_policy_for_intent(
        Intent(raw_text="Implement short approval codes", action="write_code")
    )

    assert policy.require_plan is True
    assert policy.require_tdd is True
    assert policy.require_verification_before_completion is True
    assert policy.require_spec_review is True
    assert policy.allow_assumptions is False
    assert any("tests" in item.lower() for item in policy.checklist)


def test_bug_work_requires_root_cause_and_regression_verification():
    policy = workflow_policy_for_intent(
        Intent(raw_text="Fix failing HITL callback test", action="write_code")
    )

    assert policy.require_root_cause_before_fix is True
    assert policy.require_tdd is True
    assert any("root cause" in item.lower() for item in policy.checklist)
    assert any("regression" in item.lower() for item in policy.checklist)


def test_simple_chat_keeps_policy_lightweight():
    policy = workflow_policy_for_intent(Intent(raw_text="hello", action="chat"))

    assert policy.require_plan is False
    assert policy.require_tdd is False
    assert policy.require_verification_before_completion is False
    assert policy.allow_assumptions is True
