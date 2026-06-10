"""Tests for deterministic planner/reviewer loop scaffolding."""

from __future__ import annotations

import json

from symbio.core.planner_reviewer import (
    PlannerReviewerLoop,
    PlannerReviewerStatus,
    ReviewFinding,
    ReviewFindingSeverity,
)
from symbio.core.workflow_policy import WorkflowPolicy
from symbio.utils.types import Intent, Task, TaskComplexity


def test_large_feature_work_runs_plan_spec_and_quality_reviews():
    policy = WorkflowPolicy(
        require_plan=True,
        require_spec_review=True,
        require_verification_before_completion=True,
        require_tdd=True,
    )
    intent = Intent(
        raw_text="Implement the dedicated planner reviewer loop",
        action="write_code",
        estimated_complexity=TaskComplexity.HIGH,
    )

    result = PlannerReviewerLoop().run(intent=intent, workflow_policy=policy)

    assert result.status == PlannerReviewerStatus.APPROVED
    assert result.approved is True
    assert [step.kind for step in result.plan] == ["plan", "spec_review", "quality_review"]
    assert result.spec_review.status == "passed"
    assert result.quality_review.status == "passed"
    assert result.evidence["loop"] == "deterministic_planner_reviewer"
    assert result.evidence["llm_used"] is False


def test_lightweight_chat_skips_blocking_loop_with_advisory_result():
    task = Task(
        intent=Intent(
            raw_text="Summarize this short note",
            action="chat",
            estimated_complexity=TaskComplexity.LOW,
        ),
        metadata={"workflow_policy": WorkflowPolicy()},
    )

    result = PlannerReviewerLoop().run(task=task)

    assert result.status == PlannerReviewerStatus.SKIPPED
    assert result.approved is False
    assert result.plan == []
    assert result.spec_review.status == "skipped"
    assert result.quality_review.status == "skipped"
    assert result.evidence["skip_reason"] == "lightweight_chat"


def test_blocking_review_finding_prevents_approved_status():
    finding = ReviewFinding(
        source="spec_review",
        severity=ReviewFindingSeverity.BLOCKING,
        message="Acceptance criteria are missing",
        recommendation="Ask for acceptance criteria before implementation",
    )

    result = PlannerReviewerLoop().run(
        text="Implement a new export workflow",
        workflow_policy=WorkflowPolicy(
            require_plan=True,
            require_spec_review=True,
            require_verification_before_completion=True,
        ),
        complexity=TaskComplexity.HIGH,
        injected_findings=[finding],
    )

    assert result.status == PlannerReviewerStatus.BLOCKED
    assert result.approved is False
    assert result.blocking_findings == [finding]
    assert result.spec_review.findings == [finding]


def test_planner_reviewer_result_is_json_serializable():
    result = PlannerReviewerLoop().run(
        text="Build a focused deterministic planner reviewer loop",
        workflow_policy={
            "require_plan": True,
            "require_spec_review": True,
            "require_verification_before_completion": True,
            "require_tdd": True,
        },
        complexity="high",
    )

    payload = result.model_dump(mode="json")

    assert payload["status"] == "approved"
    assert payload["plan"][0]["kind"] == "plan"
    assert payload["evidence"]["llm_used"] is False
    json.dumps(payload, ensure_ascii=False)
