"""submit_task 工具 - Agent 必须调用此工具才能结束任务

这是防止 Agent 过早宣布完成的核心机制：
- Agent 不允许通过自然语言 EOS 结束任务
- 必须调用 submit_task 工具提交结果
- submit_task 自动验证 checklist 完成状态
- 验证不通过则拒绝提交，Agent 必须继续工作
"""

from __future__ import annotations

import time
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

        if result.is_valid:
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
            failed_details = "\n".join(f"  - {f}" for f in result.failed_checks)
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
                },
                "required": ["checklist"],
            },
            strict=False,
        )
