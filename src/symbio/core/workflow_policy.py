"""Workflow discipline policy for agent execution.

This module turns reusable engineering practices into structured guidance that
the orchestrator can inject into agent tasks. The goal is to make planning,
TDD, debugging, clarification, and verification part of the runtime contract
instead of relying on ad-hoc prompting.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from symbio.utils.types import Intent


class WorkflowPolicy(BaseModel):
    """Execution discipline attached to a task."""

    require_plan: bool = False
    require_clarification_on_ambiguity: bool = True
    require_tdd: bool = False
    require_root_cause_before_fix: bool = False
    require_verification_before_completion: bool = True
    require_spec_review: bool = False
    allow_assumptions: bool = False
    checklist: list[str] = Field(default_factory=list)

    def to_prompt(self) -> str:
        lines = [
            "Workflow policy:",
            "- If key requirements or acceptance criteria are unclear, stop and ask a concise clarifying question before implementation.",
        ]
        if self.require_plan:
            lines.append("- Start by producing a brief implementation plan before changing behavior.")
        if self.require_tdd:
            lines.extend([
                "- Use TDD: write or identify the failing test first, confirm it fails for the expected reason, then implement the minimal fix.",
                "- Do not claim a behavior change is complete without a relevant test or explicit verification.",
            ])
        if self.require_root_cause_before_fix:
            lines.append("- For bugs or test failures, investigate and state the root cause before proposing or applying a fix.")
        if self.require_spec_review:
            lines.append("- Check the final result against the requested scope; avoid unrequested extra behavior.")
        if self.require_verification_before_completion:
            lines.append("- Before completion, run or cite the exact verification that proves the result.")
        if not self.allow_assumptions:
            lines.append("- Do not silently invent missing constraints; ask or mark the uncertainty explicitly.")
        if self.checklist:
            lines.append("Required checkpoints:")
            lines.extend(f"- {item}" for item in self.checklist)
        return "\n".join(lines)


def workflow_policy_for_intent(intent: Intent) -> WorkflowPolicy:
    """Build a workflow policy from the parsed intent."""
    text = f"{intent.raw_text} {intent.action}".lower()
    action = intent.action

    is_code_work = action in {"write_code", "code_review", "file_operation", "git_operation"}
    is_bug_work = any(keyword in text for keyword in [
        "bug", "fix", "fail", "failure", "error", "crash", "regression",
        "修复", "失败", "报错", "错误", "崩溃", "不对", "有问题",
    ])
    is_feature_work = is_code_work or any(keyword in text for keyword in [
        "implement", "add", "build", "feature", "refactor",
        "实现", "添加", "新增", "开发", "重构",
    ])

    checklist = []
    if is_feature_work:
        checklist.extend([
            "Define expected behavior and acceptance criteria.",
            "Add or update focused tests before implementation when feasible.",
            "Implement the smallest change that satisfies the test and scope.",
        ])
    if is_bug_work:
        checklist.extend([
            "Reproduce or characterize the failure.",
            "Identify the root cause before changing code.",
            "Add a regression test or equivalent verification.",
        ])
    if is_feature_work or is_bug_work:
        checklist.append("Run targeted verification, then broader verification if the change touches shared behavior.")

    return WorkflowPolicy(
        require_plan=is_feature_work,
        require_clarification_on_ambiguity=True,
        require_tdd=is_feature_work or is_bug_work,
        require_root_cause_before_fix=is_bug_work,
        require_verification_before_completion=is_feature_work or is_bug_work,
        require_spec_review=is_feature_work,
        allow_assumptions=not (is_feature_work or is_bug_work),
        checklist=checklist,
    )
