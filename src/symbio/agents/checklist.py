"""任务完成清单 - 防止 Agent 过早宣布完成

核心机制：
1. 任务开始时生成 Checklist，明确所有待完成项
2. Agent 必须调用 submit_task 工具才能结束
3. submit_task 自动验证：文件存在/非空、checklist 全部完成、无 TODO/FIXME
4. 验证不通过则拒绝提交，Agent 必须继续工作
"""

from __future__ import annotations

import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("checklist")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class ChecklistItemStatus(str, Enum):
    """清单条目状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ChecklistItem(BaseModel):
    """清单条目"""
    item_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    files: list[str] = Field(default_factory=list, description="预期产出文件路径")
    test: str = ""  # 验证命令或测试用例
    status: ChecklistItemStatus = ChecklistItemStatus.PENDING
    result: str = ""


class CompletionCriteria(BaseModel):
    """完成标准"""
    all_items_done: bool = True
    all_tests_pass: bool = True
    no_todo_comments: bool = False


class TaskChecklist(BaseModel):
    """任务完成清单"""
    checklist_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    items: list[ChecklistItem] = Field(default_factory=list)
    completion_criteria: CompletionCriteria = Field(default_factory=CompletionCriteria)
    created_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    completed_at: str = ""

    @property
    def is_complete(self) -> bool:
        """检查清单是否全部完成"""
        if not self.items:
            return True
        if self.completion_criteria.all_items_done:
            return all(
                item.status in (ChecklistItemStatus.COMPLETED, ChecklistItemStatus.SKIPPED)
                for item in self.items
            )
        return True

    @property
    def progress(self) -> float:
        """完成进度 0.0-1.0"""
        if not self.items:
            return 1.0
        done = sum(
            1 for i in self.items
            if i.status in (ChecklistItemStatus.COMPLETED, ChecklistItemStatus.SKIPPED)
        )
        return done / len(self.items)

    @property
    def pending_items(self) -> list[ChecklistItem]:
        """获取待完成条目"""
        return [i for i in self.items if i.status == ChecklistItemStatus.PENDING]

    @property
    def completed_items(self) -> list[ChecklistItem]:
        """获取已完成条目"""
        return [i for i in self.items if i.status == ChecklistItemStatus.COMPLETED]

    @property
    def failed_items(self) -> list[ChecklistItem]:
        """获取失败条目"""
        return [i for i in self.items if i.status == ChecklistItemStatus.FAILED]


class ValidationResult(BaseModel):
    """验证结果"""
    is_valid: bool
    passed_checks: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# 验证器
# ---------------------------------------------------------------------------

# TODO/FIXME 注释模式
_TODO_PATTERN = re.compile(
    r'#\s*TODO|#\s*FIXME|#\s*HACK|#\s*XXX|'
    r'//\s*TODO|//\s*FIXME|'
    r'/\*\s*TODO|/\*\s*FIXME',
    re.IGNORECASE,
)


class ChecklistValidator:
    """清单验证器 - 验证任务是否真正完成"""

    async def validate(self, checklist: TaskChecklist) -> ValidationResult:
        """验证清单完成状态

        检查项：
        1. 所有条目状态是否为 completed/skipped
        2. 已完成条目的产出文件是否存在且非空
        3. 如果启用了 no_todo_comments，扫描产出文件中的 TODO/FIXME
        """
        passed: list[str] = []
        failed: list[str] = []
        warnings: list[str] = []

        # 检查 1：条目状态
        pending = checklist.pending_items
        failed_items = checklist.failed_items

        if pending:
            names = ", ".join(i.name for i in pending)
            failed.append(f"还有 {len(pending)} 个条目未完成: {names}")

        if failed_items:
            names = ", ".join(i.name for i in failed_items)
            failed.append(f"有 {len(failed_items)} 个条目失败: {names}")

        if not pending and not failed_items:
            passed.append("所有条目已完成")

        # 检查 2：产出文件存在且非空
        for item in checklist.items:
            if item.status != ChecklistItemStatus.COMPLETED:
                continue
            for file_path in item.files:
                p = Path(file_path)
                if not p.exists():
                    failed.append(f"文件不存在: {file_path} (条目: {item.name})")
                elif p.stat().st_size == 0:
                    failed.append(f"文件为空: {file_path} (条目: {item.name})")
                else:
                    passed.append(f"文件存在且非空: {file_path}")

        # 检查 3：TODO/FIXME 扫描
        if checklist.completion_criteria.no_todo_comments:
            for item in checklist.items:
                if item.status != ChecklistItemStatus.COMPLETED:
                    continue
                for file_path in item.files:
                    p = Path(file_path)
                    if p.exists() and p.suffix in ('.py', '.js', '.ts', '.java', '.go', '.rs'):
                        try:
                            content = p.read_text(encoding='utf-8', errors='ignore')
                            todos = _TODO_PATTERN.findall(content)
                            if todos:
                                warnings.append(
                                    f"文件 {file_path} 包含 {len(todos)} 个 TODO/FIXME 注释"
                                )
                        except Exception:
                            pass

        is_valid = len(failed) == 0
        summary = (
            f"验证{'通过' if is_valid else '未通过'}: "
            f"{len(passed)} 项通过, {len(failed)} 项失败, {len(warnings)} 个警告"
        )

        result = ValidationResult(
            is_valid=is_valid,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=summary,
        )

        if is_valid:
            logger.info(f"清单验证通过: {checklist.checklist_id}")
        else:
            logger.warning(f"清单验证未通过: {checklist.checklist_id}, 失败项: {failed}")

        return result
