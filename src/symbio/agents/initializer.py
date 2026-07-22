"""初始化 Agent - 任务开始时自动生成完成清单

核心职责：
1. 分析用户意图，判断任务类型
2. 根据任务类型生成对应的 Checklist 条目
3. 尝试调用 LLM 进行更精细的任务分解（可选降级）
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

from symbio.agents.checklist import ChecklistItem, TaskChecklist
from symbio.utils.logger import get_logger
from symbio.utils.types import Intent

logger = get_logger("initializer")

# ---------------------------------------------------------------------------
# 按 action 类型的 Checklist 模板
# ---------------------------------------------------------------------------

_CHECKLIST_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "chat": [
        {"name": "回复用户问题", "description": "理解用户问题并给出清晰回答"},
    ],
    "write_code": [
        {"name": "理解需求", "description": "分析需求，明确输入输出"},
        {"name": "编写代码", "description": "实现功能代码", "files": [], "test": ""},
        {"name": "验证代码可运行", "description": "确保代码无语法错误且可执行"},
    ],
    "code_review": [
        {"name": "加载代码", "description": "读取待审查的代码文件"},
        {"name": "静态分析", "description": "检查代码质量、潜在 bug、风格问题"},
        {"name": "生成报告", "description": "输出审查结论和改进建议"},
    ],
    "analyze_data": [
        {"name": "加载数据", "description": "读取并解析数据源"},
        {"name": "数据清洗", "description": "处理缺失值、异常值"},
        {"name": "分析统计", "description": "执行统计分析和可视化"},
        {"name": "生成报告", "description": "汇总分析结果"},
    ],
    "search": [
        {"name": "确定搜索范围", "description": "明确搜索关键词和范围"},
        {"name": "执行搜索", "description": "调用搜索工具获取结果"},
        {"name": "整理结果", "description": "筛选并格式化搜索结果"},
    ],
    "file_operation": [
        {"name": "确认文件路径", "description": "验证文件路径合法性"},
        {"name": "执行操作", "description": "执行文件读写操作"},
        {"name": "验证结果", "description": "确认操作结果正确"},
    ],
    "git_operation": [
        {"name": "确认操作类型", "description": "明确 git 操作（commit/push/pull 等）"},
        {"name": "执行 Git 操作", "description": "执行 git 命令"},
        {"name": "验证结果", "description": "确认 git 操作成功"},
    ],
}


# ---------------------------------------------------------------------------
# 初始化 Agent
# ---------------------------------------------------------------------------


class InitializerAgent:
    """初始化 Agent - 在任务开始时分析需求并生成 Checklist

    使用方式：
        agent = InitializerAgent()
        checklist = await agent.generate_checklist(intent, task_id)
    """

    def __init__(
        self,
        anthropic_base_url: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
    ) -> None:
        self._anthropic_base_url = anthropic_base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        self._anthropic_api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def generate_checklist(self, intent: Intent, task_id: str) -> TaskChecklist:
        """根据用户意图生成任务完成清单

        策略：
        1. 简单任务（chat）直接使用模板
        2. 其他任务先尝试 LLM 分解
        3. LLM 不可用时降级到模板

        Args:
            intent: 用户意图
            task_id: 任务 ID

        Returns:
            TaskChecklist 实例
        """
        action = intent.action or "chat"

        # 简单任务直接用模板
        if action == "chat":
            logger.info(f"任务 {task_id} 为简单对话，使用单条模板")
            return self._from_template(action, task_id, intent)

        # 尝试 LLM 分解
        llm_items = await self._try_llm_decomposition(intent)
        if llm_items is not None:
            logger.info(f"任务 {task_id} 使用 LLM 分解生成 {len(llm_items)} 个条目")
            return TaskChecklist(
                task_id=task_id,
                items=llm_items,
            )

        # 降级到模板
        logger.info(f"任务 {task_id} LLM 不可用，降级到模板 (action={action})")
        return self._from_template(action, task_id, intent)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _from_template(self, action: str, task_id: str, intent: Intent) -> TaskChecklist:
        """从预定义模板生成 Checklist"""
        template = _CHECKLIST_TEMPLATES.get(action, _CHECKLIST_TEMPLATES["chat"])

        items: list[ChecklistItem] = []
        for entry in template:
            files = list(entry.get("files", []))
            # 如果模板中 files 为空列表但 intent 有 file_paths，填充进去
            if not files and intent.parameters.get("file_paths"):
                if entry.get("name") in ("编写代码", "执行操作"):
                    files = list(intent.parameters["file_paths"])

            items.append(
                ChecklistItem(
                    name=entry["name"],
                    description=entry.get("description", ""),
                    files=files,
                    test=entry.get("test", ""),
                )
            )

        return TaskChecklist(task_id=task_id, items=items)

    async def _try_llm_decomposition(self, intent: Intent) -> Optional[list[ChecklistItem]]:
        """尝试使用 LLM 将任务分解为 Checklist 条目

        Returns:
            ChecklistItem 列表，LLM 不可用时返回 None
        """
        if not self._anthropic_base_url or not self._anthropic_api_key:
            return None

        try:
            url = self._anthropic_base_url.rstrip("/") + "/v1/messages"
            headers = {
                "x-api-key": self._anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            system_prompt = (
                "你是一个任务分解助手。根据用户意图，将任务分解为具体的执行步骤。\n"
                "每个步骤是一个 JSON 对象，包含：\n"
                '  - "name": 步骤名称（简短）\n'
                '  - "description": 步骤描述\n'
                '  - "files": 预期产出文件路径列表（可选）\n'
                '  - "test": 验证命令（可选，如 "python -c ..."）\n'
                "请返回一个 JSON 数组，不要包含其他文本。"
            )

            user_text = (
                f"任务类型: {intent.action}\n"
                f"用户输入: {intent.raw_text}\n"
                f"参数: {json.dumps(intent.parameters, ensure_ascii=False)}"
            )

            body = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_text}],
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()

            data = resp.json()
            text = data["content"][0]["text"].strip()

            # 解析 JSON 数组
            # 尝试从回复中提取 JSON 数组
            if "[" in text and "]" in text:
                start = text.index("[")
                end = text.rindex("]") + 1
                items_data = json.loads(text[start:end])
            else:
                items_data = json.loads(text)

            items: list[ChecklistItem] = []
            for entry in items_data:
                if isinstance(entry, dict) and "name" in entry:
                    items.append(
                        ChecklistItem(
                            name=entry["name"],
                            description=entry.get("description", ""),
                            files=entry.get("files", []),
                            test=entry.get("test", ""),
                        )
                    )

            return items if items else None

        except Exception as exc:
            logger.warning(f"LLM 分解失败，将降级到模板: {exc}")
            return None
