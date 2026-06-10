"""Tests for submit_task workflow-policy enforcement."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.tools.submit_task import SubmitTaskTool


def _completed_checklist() -> dict:
    return {
        "task_id": "task-1",
        "items": [
            {
                "name": "finish implementation",
                "status": "completed",
            }
        ],
    }


def _strict_workflow_policy() -> dict:
    return {
        "require_plan": True,
        "require_tdd": True,
        "require_root_cause_before_fix": True,
        "require_verification_before_completion": True,
        "require_spec_review": True,
        "allow_assumptions": False,
    }


async def test_submit_task_allows_legacy_checklist_only_submission():
    result = await SubmitTaskTool().execute(
        checklist=_completed_checklist(),
        summary="Done.",
    )

    assert result.success is True


async def test_submit_task_rejects_missing_workflow_policy_evidence_from_metadata():
    result = await SubmitTaskTool().execute(
        checklist=_completed_checklist(),
        summary="Done.",
        metadata={"workflow_policy": _strict_workflow_policy()},
    )

    assert result.success is False
    assert "workflow policy requires plan evidence" in result.output
    assert "workflow policy requires TDD/test evidence" in result.output
    assert "workflow policy requires root cause evidence" in result.output
    assert "workflow policy requires verification evidence" in result.output
    assert "workflow policy requires spec/scope review evidence" in result.output
    assert "ambiguity and assumptions" in result.output


async def test_submit_task_accepts_workflow_evidence_from_metadata():
    result = await SubmitTaskTool().execute(
        checklist=_completed_checklist(),
        summary="Done.",
        metadata={
            "workflow_policy": _strict_workflow_policy(),
            "workflow_evidence": {
                "plan": "Implement focused submit_task policy validation.",
                "tests": ["tests/test_submit_task.py"],
                "root_cause": "submit_task only validated checklist state.",
                "verification": "pytest tests/test_submit_task.py",
                "spec_review": "Only submit_task and focused tests changed.",
                "no_unresolved_ambiguity": True,
            },
        },
    )

    assert result.success is True


async def test_submit_task_accepts_top_level_workflow_evidence():
    result = await SubmitTaskTool().execute(
        checklist=_completed_checklist(),
        summary="Done.",
        workflow_policy={
            "require_plan": True,
            "require_verification_before_completion": True,
            "allow_assumptions": True,
        },
        workflow_evidence={
            "implementation_plan": "Make submit_task validate policy evidence.",
            "verification_run": "pytest tests/test_submit_task.py",
        },
    )

    assert result.success is True
