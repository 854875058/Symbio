"""Deterministic planner/reviewer loop scaffolding.

This module provides the structured runtime contract for a future dedicated
planner/reviewer agent loop. It does not call an LLM or claim external agent
execution; all decisions are deterministic and testable.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from symbio.core.workflow_policy import WorkflowPolicy
from symbio.utils.types import Intent, Task, TaskComplexity


def _ensure_json_ready(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return value


class PlannerReviewerStatus(str, Enum):
    """Top-level outcome for the planner/reviewer gate."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    ADVISORY = "advisory"


class ReviewFindingSeverity(str, Enum):
    """Severity for deterministic review findings."""

    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class PlanStep(BaseModel):
    """A planner/reviewer phase that downstream orchestration can execute."""

    step_id: str
    kind: str
    title: str
    description: str
    required: bool = True
    evidence_keys: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "metadata")


class ReviewFinding(BaseModel):
    """A finding emitted by spec or quality review."""

    source: str
    severity: ReviewFindingSeverity = ReviewFindingSeverity.INFO
    message: str
    recommendation: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence")
    @classmethod
    def validate_evidence_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "evidence")


class ReviewResult(BaseModel):
    """Structured result for one deterministic review phase."""

    stage: str
    status: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    checked_items: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence")
    @classmethod
    def validate_evidence_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "evidence")


class PlannerReviewerResult(BaseModel):
    """Structured handoff from the planner/reviewer loop."""

    status: PlannerReviewerStatus
    approved: bool = False
    task_text: str
    action: str = ""
    complexity: TaskComplexity = TaskComplexity.MEDIUM
    plan: list[PlanStep] = Field(default_factory=list)
    spec_review: ReviewResult
    quality_review: ReviewResult
    blocking_findings: list[ReviewFinding] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence")
    @classmethod
    def validate_evidence_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "evidence")


class PlannerReviewerLoop:
    """Build a deterministic plan/spec/quality review gate for larger work."""

    FEATURE_ACTIONS = {
        "write_code",
        "code_review",
        "file_operation",
        "git_operation",
    }
    FEATURE_KEYWORDS = {
        "implement",
        "add",
        "build",
        "feature",
        "refactor",
        "fix",
        "bug",
        "test",
        "实现",
        "新增",
        "添加",
        "修复",
        "重构",
    }

    def run(
        self,
        *,
        task: Task | None = None,
        intent: Intent | None = None,
        text: str = "",
        workflow_policy: WorkflowPolicy | dict[str, Any] | None = None,
        complexity: TaskComplexity | str | None = None,
        injected_findings: list[ReviewFinding] | None = None,
    ) -> PlannerReviewerResult:
        """Run the deterministic planning gate for an intent or text input."""
        normalized_intent = self._normalize_intent(task, intent, text, complexity)
        policy = self._normalize_policy(task, workflow_policy)
        findings = list(injected_findings or [])

        if not self._requires_blocking_loop(normalized_intent, policy):
            return self._skipped_result(normalized_intent, policy)

        plan = self._build_plan(normalized_intent, policy)
        spec_review = self._review(
            stage="spec_review",
            findings=[
                finding for finding in findings if finding.source == "spec_review"
            ],
            checked_items=[
                "task intent captured",
                "workflow policy requirements captured",
                "blocking uncertainties surfaced",
            ],
        )
        quality_review = self._review(
            stage="quality_review",
            findings=[
                finding for finding in findings if finding.source == "quality_review"
            ],
            checked_items=[
                "verification evidence required",
                "implementation remains deterministic",
                "LLM handoff explicitly deferred",
            ],
        )
        blocking_findings = [
            finding
            for finding in [*spec_review.findings, *quality_review.findings]
            if finding.severity == ReviewFindingSeverity.BLOCKING
        ]
        approved = not blocking_findings

        return PlannerReviewerResult(
            status=PlannerReviewerStatus.APPROVED
            if approved
            else PlannerReviewerStatus.BLOCKED,
            approved=approved,
            task_text=normalized_intent.raw_text,
            action=normalized_intent.action,
            complexity=normalized_intent.estimated_complexity,
            plan=plan,
            spec_review=spec_review,
            quality_review=quality_review,
            blocking_findings=blocking_findings,
            evidence=self._base_evidence(
                normalized_intent,
                policy,
                decision="full_loop",
                phase_count=len(plan),
            ),
        )

    def _normalize_intent(
        self,
        task: Task | None,
        intent: Intent | None,
        text: str,
        complexity: TaskComplexity | str | None,
    ) -> Intent:
        if task is not None:
            intent = task.intent
        if intent is None:
            intent = Intent(raw_text=text, estimated_complexity=self._complexity(complexity))
        elif complexity is not None:
            intent = intent.model_copy(update={"estimated_complexity": self._complexity(complexity)})
        return intent

    @staticmethod
    def _normalize_policy(
        task: Task | None,
        workflow_policy: WorkflowPolicy | dict[str, Any] | None,
    ) -> WorkflowPolicy:
        if workflow_policy is None and task is not None:
            workflow_policy = task.metadata.get("workflow_policy")
        if isinstance(workflow_policy, WorkflowPolicy):
            return workflow_policy
        if isinstance(workflow_policy, dict):
            return WorkflowPolicy(**workflow_policy)
        return WorkflowPolicy()

    @staticmethod
    def _complexity(value: TaskComplexity | str | None) -> TaskComplexity:
        if isinstance(value, TaskComplexity):
            return value
        if isinstance(value, str):
            return TaskComplexity(value.lower())
        return TaskComplexity.MEDIUM

    def _requires_blocking_loop(
        self,
        intent: Intent,
        policy: WorkflowPolicy,
    ) -> bool:
        if intent.estimated_complexity == TaskComplexity.HIGH:
            return True
        if intent.action in self.FEATURE_ACTIONS:
            return True
        if policy.require_plan or policy.require_spec_review or policy.require_tdd:
            return True
        text = f"{intent.raw_text} {intent.action}".lower()
        return any(keyword in text for keyword in self.FEATURE_KEYWORDS)

    def _skipped_result(
        self,
        intent: Intent,
        policy: WorkflowPolicy,
    ) -> PlannerReviewerResult:
        return PlannerReviewerResult(
            status=PlannerReviewerStatus.SKIPPED,
            approved=False,
            task_text=intent.raw_text,
            action=intent.action,
            complexity=intent.estimated_complexity,
            plan=[],
            spec_review=ReviewResult(
                stage="spec_review",
                status="skipped",
                evidence={"reason": "lightweight_chat"},
            ),
            quality_review=ReviewResult(
                stage="quality_review",
                status="skipped",
                evidence={"reason": "lightweight_chat"},
            ),
            evidence={
                **self._base_evidence(intent, policy, decision="skip"),
                "skip_reason": "lightweight_chat",
            },
        )

    def _build_plan(
        self,
        intent: Intent,
        policy: WorkflowPolicy,
    ) -> list[PlanStep]:
        policy_flags = self._policy_flags(policy)
        return [
            PlanStep(
                step_id="planner",
                kind="plan",
                title="Planner pass",
                description="Create a structured implementation plan from the task intent and workflow policy.",
                evidence_keys=["task_text", "workflow_policy", "complexity"],
                metadata={"policy": policy_flags},
            ),
            PlanStep(
                step_id="spec-review",
                kind="spec_review",
                title="Spec review",
                description="Review the plan against task scope, ambiguity, and acceptance criteria before execution.",
                evidence_keys=["spec_review.status", "spec_review.findings"],
                metadata={"requires_spec_review": policy.require_spec_review},
            ),
            PlanStep(
                step_id="quality-review",
                kind="quality_review",
                title="Quality review",
                description="Review verification expectations, deterministic evidence, and completion gates.",
                evidence_keys=["quality_review.status", "quality_review.findings"],
                metadata={
                    "requires_verification_before_completion": policy.require_verification_before_completion,
                    "complexity": intent.estimated_complexity.value,
                },
            ),
        ]

    @staticmethod
    def _review(
        stage: str,
        findings: list[ReviewFinding],
        checked_items: list[str],
    ) -> ReviewResult:
        status = "blocked" if any(
            finding.severity == ReviewFindingSeverity.BLOCKING
            for finding in findings
        ) else "passed"
        return ReviewResult(
            stage=stage,
            status=status,
            findings=findings,
            checked_items=checked_items,
            evidence={"deterministic": True},
        )

    def _base_evidence(
        self,
        intent: Intent,
        policy: WorkflowPolicy,
        *,
        decision: str,
        phase_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "loop": "deterministic_planner_reviewer",
            "llm_used": False,
            "decision": decision,
            "task_text": intent.raw_text,
            "action": intent.action,
            "complexity": intent.estimated_complexity.value,
            "phase_count": phase_count,
            "workflow_policy": self._policy_flags(policy),
        }

    @staticmethod
    def _policy_flags(policy: WorkflowPolicy) -> dict[str, Any]:
        return policy.model_dump(mode="json")


__all__ = [
    "PlanStep",
    "PlannerReviewerLoop",
    "PlannerReviewerResult",
    "PlannerReviewerStatus",
    "ReviewFinding",
    "ReviewFindingSeverity",
    "ReviewResult",
]
