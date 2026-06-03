"""Tests for DAG-first replanning policy."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.execution_models import ReplanDecisionType
from symbio.core.replanner import Replanner


def test_transient_tool_error_retries_until_limit():
    decision = Replanner(max_retries=2).decide(
        "node-1",
        {"kind": "tool_transient_error", "retry_count": 1},
    )

    assert decision.decision == ReplanDecisionType.RETRY
    assert decision.node_id == "node-1"


def test_transient_tool_error_fails_after_retry_limit():
    decision = Replanner(max_retries=1).decide(
        "node-1",
        {"kind": "tool_transient_error", "retry_count": 1},
    )

    assert decision.decision == ReplanDecisionType.FAIL


def test_verification_failure_returns_local_patch_with_repair_node():
    decision = Replanner().decide(
        "node-1",
        {"kind": "verification_failure", "details": "tests failed"},
    )

    assert decision.decision == ReplanDecisionType.LOCAL_PATCH
    assert any(
        mutation.get("action") == "add_node"
        and mutation.get("node_id") == "node-1:repair"
        and mutation.get("node_action") == "repair"
        and mutation.get("executor") == "general"
        and mutation.get("description")
        and mutation.get("dependencies") == ["node-1"]
        for mutation in decision.mutations
    )


def test_replan_count_limit_fails_local_patch():
    decision = Replanner(max_replan_count=1).decide(
        "node-1",
        {"kind": "verification_failure", "replan_count": 1},
    )

    assert decision.decision == ReplanDecisionType.FAIL


def test_requirement_ambiguity_waits_for_clarification():
    decision = Replanner().decide(
        "node-1",
        {"kind": "requirement_ambiguity"},
    )

    assert decision.decision == ReplanDecisionType.WAITING_CLARIFICATION


def test_permission_required_waits_hitl():
    decision = Replanner().decide(
        "node-1",
        {"kind": "permission_required"},
    )

    assert decision.decision == ReplanDecisionType.WAITING_HITL


def test_unknown_failure_fails():
    decision = Replanner().decide(
        "node-1",
        {"kind": "unexpected"},
    )

    assert decision.decision == ReplanDecisionType.FAIL
