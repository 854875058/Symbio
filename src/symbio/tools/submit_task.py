"""submit_task 工具 - Agent 必须调用此工具才能结束任务

这是防止 Agent 过早宣布完成的核心机制：
- Agent 不允许通过自然语言 EOS 结束任务
- 必须调用 submit_task 工具提交结果
- submit_task 自动验证 checklist 完成状态
- 验证不通过则拒绝提交，Agent 必须继续工作
"""

from __future__ import annotations

from typing import Any

from symbio.agents.checklist import ChecklistValidator, TaskChecklist
from symbio.tools.registry import (
    BaseTool,
    PermissionLevel,
    ToolPermission,
    ToolResult,
    ToolSchema,
)
from symbio.utils.logger import get_logger

logger = get_logger("submit_task")


_MISSING = object()


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return a plain dict for mapping-like or pydantic-model values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None


def _has_evidence(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, set)):
        return any(_has_evidence(item) for item in value)
    if isinstance(value, dict):
        return any(_has_evidence(item) for item in value.values())
    return True


def _has_any_evidence(evidence: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(_has_evidence(evidence.get(key)) for key in keys)


def _validate_workflow_policy_evidence(
    *,
    metadata: Any,
    workflow_policy: Any,
    workflow_evidence: Any,
) -> list[str]:
    metadata_dict = _as_dict(metadata)
    if metadata is not None and metadata_dict is None:
        return ["workflow policy metadata must be an object when provided"]

    policy = workflow_policy
    if policy is None and metadata_dict is not None:
        policy = metadata_dict.get("workflow_policy")
    if policy is None:
        return []

    policy_dict = _as_dict(policy)
    if policy_dict is None:
        return ["workflow_policy must be an object when provided"]

    evidence = workflow_evidence
    if evidence is _MISSING and metadata_dict is not None:
        evidence = metadata_dict.get("workflow_evidence", _MISSING)
    evidence_dict = _as_dict(None if evidence is _MISSING else evidence)
    if evidence is not _MISSING and evidence_dict is None:
        return ["workflow_evidence must be an object when provided"]
    evidence_dict = evidence_dict or {}

    failed: list[str] = []
    required_evidence: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("require_plan", "plan", ("plan", "implementation_plan")),
        ("require_tdd", "TDD/test evidence", ("tdd", "tests", "test_results", "failing_test")),
        (
            "require_root_cause_before_fix",
            "root cause",
            ("root_cause", "cause_analysis", "failure_analysis"),
        ),
        (
            "require_verification_before_completion",
            "verification",
            ("verification", "verification_run", "verification_runs", "commands"),
        ),
        (
            "require_spec_review",
            "spec/scope review",
            ("spec_review", "scope_review", "acceptance_review"),
        ),
    )

    for policy_key, label, evidence_keys in required_evidence:
        if policy_dict.get(policy_key) is True and not _has_any_evidence(evidence_dict, evidence_keys):
            failed.append(
                f"workflow policy requires {label} evidence "
                f"(provide one of: {', '.join(evidence_keys)})"
            )

    if policy_dict.get("allow_assumptions") is False:
        assumption_keys = (
            "no_unresolved_ambiguity",
            "clarification",
            "clarifications",
            "assumptions",
            "uncertainties",
        )
        if not _has_any_evidence(evidence_dict, assumption_keys):
            failed.append(
                "workflow policy requires evidence that ambiguity and assumptions were handled "
                f"(provide one of: {', '.join(assumption_keys)})"
            )

    return failed


class SubmitTaskTool(BaseTool):
    """任务提交工具

    Agent 必须调用此工具才能结束任务。
    自动验证 checklist 完成状态，不通过则拒绝。
    """

    name = "submit_task"
    description = (
        "提交已完成的任务进行验证。你必须在所有工作完成后调用此工具来结束任务。"
        "如果还有未完成的工作，调用此工具会被拒绝。"
    )
    version = "1.0.0"
    tags = ["core", "task", "verification"]
    permission = ToolPermission(level=PermissionLevel.READ_ONLY)

    def __init__(self):
        self._validator = ChecklistValidator()

    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行任务提交

        Args:
            checklist: TaskChecklist 对象或 dict
            summary: 任务完成摘要（可选）

        Returns:
            ToolResult: 验证结果
        """
        checklist_data = kwargs.get("checklist")
        summary = kwargs.get("summary", "")
        metadata = kwargs.get("metadata")
        workflow_policy = kwargs.get("workflow_policy")
        workflow_evidence = kwargs.get("workflow_evidence", _MISSING)

        if not checklist_data:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                output="错误: 缺少 checklist 参数。请提供任务完成清单。",
            )

        # 构建 TaskChecklist
        try:
            if isinstance(checklist_data, dict):
                checklist = TaskChecklist(**checklist_data)
            elif isinstance(checklist_data, TaskChecklist):
                checklist = checklist_data
            else:
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=False,
                    output=f"错误: checklist 类型无效，期望 dict 或 TaskChecklist，得到 {type(checklist_data).__name__}",
                )
        except Exception as e:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                output=f"错误: checklist 解析失败: {str(e)}",
            )

        # 验证
        result = await self._validator.validate(checklist)
        policy_failures = _validate_workflow_policy_evidence(
            metadata=metadata,
            workflow_policy=workflow_policy,
            workflow_evidence=workflow_evidence,
        )

        if result.is_valid and not policy_failures:
            logger.info(f"任务提交成功: {checklist.task_id}")
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=True,
                output=(
                    f"任务已成功提交并通过验证。\n\n"
                    f"{result.summary}\n\n"
                    f"完成摘要: {summary}"
                ),
            )
        else:
            logger.warning(f"任务提交被拒绝: {checklist.task_id}")
            failed_checks = [*result.failed_checks, *policy_failures]
            failed_details = "\n".join(f"  - {f}" for f in failed_checks)
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                output=(
                    f"任务提交被拒绝。请修复以下问题后重新提交:\n\n"
                    f"{failed_details}\n\n"
                    f"进度: {checklist.progress:.0%}\n"
                    f"待完成: {', '.join(i.name for i in checklist.pending_items) or '无'}"
                ),
            )

    def schema(self) -> ToolSchema:
        """返回 function-calling Schema"""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "checklist": {
                        "type": "object",
                        "description": "任务完成清单，包含所有条目的完成状态",
                        "properties": {
                            "task_id": {"type": "string", "description": "任务 ID"},
                            "items": {
                                "type": "array",
                                "description": "清单条目列表",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "条目名称"},
                                        "status": {
                                            "type": "string",
                                            "enum": ["completed", "failed", "skipped"],
                                            "description": "完成状态",
                                        },
                                        "files": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "产出文件路径",
                                        },
                                    },
                                    "required": ["name", "status"],
                                },
                            },
                        },
                        "required": ["task_id", "items"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "任务完成摘要",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "任务元数据；如包含 workflow_policy，则提交时必须提供相应 workflow_evidence",
                        "properties": {
                            "workflow_policy": {
                                "type": "object",
                                "description": "由编排器注入的工作流策略",
                            },
                            "workflow_evidence": {
                                "type": "object",
                                "description": "满足工作流策略的证据",
                            },
                        },
                    },
                    "workflow_policy": {
                        "type": "object",
                        "description": "工作流策略；通常来自任务 metadata.workflow_policy",
                    },
                    "workflow_evidence": {
                        "type": "object",
                        "description": "工作流策略证据，例如 plan、tests、root_cause、verification、spec_review",
                    },
                },
                "required": ["checklist"],
            },
            strict=False,
        )
