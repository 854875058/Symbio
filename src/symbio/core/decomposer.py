"""任务分解器 - 使用 LLM 将复杂任务拆分为带依赖关系的子任务。

核心职责：
1. 接收用户意图，调用 LLM 分析并拆解为子任务
2. 计算子任务之间的拓扑排序和并行执行分组
3. 检测是否需要多智能体辩论
4. LLM 不可用时提供安全回退（单子任务）
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger
from symbio.utils.types import Intent, TaskComplexity

logger = get_logger("decomposer")

# ---------------------------------------------------------------------------
# 辩论检测关键词
# ---------------------------------------------------------------------------

_DEBATE_KEYWORDS: set[str] = {
    "对比", "评估", "选择", "比较", "权衡", "评判", "分析优劣",
    "compare", "evaluate", "choose", "assess", "weigh", "trade-off",
}

# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class SubTask(BaseModel):
    """分解后的子任务。"""

    subtask_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str
    action: str  # e.g. "chat", "code_review", "write_code", "analyze_data"
    dependencies: list[str] = Field(default_factory=list)  # list of subtask_ids this depends on
    estimated_complexity: TaskComplexity = TaskComplexity.MEDIUM
    suggested_agent: str = "general"  # which agent type to use
    parameters: dict[str, Any] = Field(default_factory=dict)


class DecompositionResult(BaseModel):
    """任务分解结果。"""

    task_id: str
    original_intent: str
    subtasks: list[SubTask]
    execution_order: list[list[str]] = Field(default_factory=list)  # parallel execution groups
    needs_debate: bool = False  # whether this task should trigger multi-agent debate
    reasoning: str = ""  # LLM's reasoning about the decomposition


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a task decomposer for an AI agent system. Analyze the user's request and break it into smaller, independent subtasks.

Respond with ONLY a JSON object, no markdown fences:
{
  "reasoning": "your analysis of the task",
  "needs_debate": false,
  "subtasks": [
    {
      "name": "subtask name",
      "description": "what to do",
      "action": "chat|code_review|write_code|analyze_data|search|file_operation|git_operation",
      "dependencies": [],
      "estimated_complexity": "low|medium|high",
      "suggested_agent": "general",
      "parameters": {}
    }
  ]
}

Rules:
- If the task is simple (greeting, simple question), return a single subtask
- If the task has multiple independent parts, split them
- If parts have dependencies, express them via the dependencies field (use the subtask name as identifier)
- For complex analysis/decision tasks, set needs_debate to true
- The "dependencies" field should contain the "name" values of other subtasks this one depends on"""


# ---------------------------------------------------------------------------
# Task Decomposer
# ---------------------------------------------------------------------------


class TaskDecomposer:
    """使用 LLM 将复杂任务分解为子任务。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def decompose(self, intent: Intent, task_id: str) -> DecompositionResult:
        """分解复杂任务为子任务。

        Args:
            intent: 解析后的用户意图。
            task_id: 关联的任务 ID。

        Returns:
            分解结果，包含子任务列表和执行顺序。
        """
        logger.info(f"开始分解任务: task_id={task_id}, action={intent.action}")

        # 尝试 LLM 分解
        llm_result = await self._call_llm(intent.raw_text)

        if llm_result is not None:
            result = self._build_result(task_id, intent.raw_text, llm_result)
        else:
            # LLM 不可用或解析失败，安全回退
            logger.warning("LLM 分解失败，使用单子任务回退")
            result = self._fallback(task_id, intent)

        # 计算拓扑执行顺序
        result.execution_order = self._calculate_execution_order(result.subtasks)

        # 检测是否需要辩论（覆盖 LLM 判断）
        if self._should_debate(intent, result):
            result.needs_debate = True

        logger.info(
            f"任务分解完成: task_id={task_id}, subtasks={len(result.subtasks)}, "
            f"groups={len(result.execution_order)}, needs_debate={result.needs_debate}"
        )
        return result

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    async def _call_llm(self, user_text: str) -> dict[str, Any] | None:
        """调用 Anthropic API 进行任务分解。

        优先使用 Anthropic（httpx），与 orchestrator._call_llm_anthropic 保持一致。

        Args:
            user_text: 用户原始输入。

        Returns:
            LLM 返回的 JSON dict，失败时返回 None。
        """
        mc = self.settings.model
        if not mc.anthropic_api_key:
            logger.debug("未配置 Anthropic API Key，跳过 LLM 分解")
            return None

        try:
            url = mc.anthropic_base_url.rstrip("/") + "/v1/messages"
            headers = {
                "x-api-key": mc.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": mc.model_low,  # 规划任务使用低成本模型
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_text}],
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()

            data = resp.json()
            text = data["content"][0]["text"].strip()

            # 解析 JSON（兼容 LLM 可能输出 markdown fences 的情况）
            text = self._strip_code_fences(text)
            result = json.loads(text)

            if not isinstance(result, dict):
                logger.warning(f"LLM 返回非 dict 类型: {type(result)}")
                return None

            return result

        except json.JSONDecodeError as exc:
            logger.warning(f"LLM 返回 JSON 解析失败: {exc}")
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(f"Anthropic API HTTP 错误: {exc.response.status_code}")
            return None
        except Exception as exc:
            logger.warning(f"Anthropic 任务分解调用失败: {exc}")
            return None

    # ------------------------------------------------------------------
    # 结果构建
    # ------------------------------------------------------------------

    def _build_result(
        self,
        task_id: str,
        original_intent: str,
        llm_data: dict[str, Any],
    ) -> DecompositionResult:
        """将 LLM 返回的 dict 构建为 DecompositionResult。

        Args:
            task_id: 任务 ID。
            original_intent: 用户原始输入。
            llm_data: LLM 返回的 JSON dict。

        Returns:
            构建好的分解结果。
        """
        raw_subtasks = llm_data.get("subtasks", [])
        if not isinstance(raw_subtasks, list) or len(raw_subtasks) == 0:
            logger.warning("LLM 返回的 subtasks 为空或非法，使用回退")
            return self._fallback(
                task_id,
                Intent(raw_text=original_intent),
            )

        # 先构建 name -> subtask_id 映射，用于解析 dependencies
        name_to_id: dict[str, str] = {}
        subtasks: list[SubTask] = []

        for raw_st in raw_subtasks:
            if not isinstance(raw_st, dict):
                continue
            st_id = str(uuid4())
            name = raw_st.get("name", f"subtask_{st_id[:8]}")
            name_to_id[name] = st_id

            subtasks.append(SubTask(
                subtask_id=st_id,
                name=name,
                description=raw_st.get("description", ""),
                action=raw_st.get("action", "chat"),
                dependencies=[],  # 先留空，后面统一解析
                estimated_complexity=self._parse_complexity(
                    raw_st.get("estimated_complexity", "medium")
                ),
                suggested_agent=raw_st.get("suggested_agent", "general"),
                parameters=raw_st.get("parameters", {}),
            ))

        # 第二遍：解析 dependencies（name -> subtask_id）
        for subtask, raw_st in zip(subtasks, raw_subtasks):
            if not isinstance(raw_st, dict):
                continue
            raw_deps = raw_st.get("dependencies", [])
            if isinstance(raw_deps, list):
                for dep_name in raw_deps:
                    dep_id = name_to_id.get(dep_name)
                    if dep_id and dep_id != subtask.subtask_id:
                        subtask.dependencies.append(dep_id)
                    elif dep_name and dep_name not in name_to_id:
                        logger.warning(
                            f"子任务 '{subtask.name}' 依赖 '{dep_name}' 未找到，已忽略"
                        )

        return DecompositionResult(
            task_id=task_id,
            original_intent=original_intent,
            subtasks=subtasks,
            needs_debate=bool(llm_data.get("needs_debate", False)),
            reasoning=str(llm_data.get("reasoning", "")),
        )

    # ------------------------------------------------------------------
    # 拓扑排序与执行分组
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_execution_order(subtasks: list[SubTask]) -> list[list[str]]:
        """计算子任务的拓扑执行顺序，将无依赖关系的子任务分为并行组。

        使用 Kahn 算法进行拓扑排序，同时将同一层级的节点归为一个并行组。

        Args:
            subtasks: 子任务列表。

        Returns:
            并行执行分组，每个内层列表是一组可并行执行的 subtask_id。
        """
        if not subtasks:
            return []

        # 构建邻接表和入度表
        id_set = {st.subtask_id for st in subtasks}
        in_degree: dict[str, int] = {sid: 0 for sid in id_set}
        dependents: dict[str, list[str]] = {sid: [] for sid in id_set}

        for st in subtasks:
            for dep_id in st.dependencies:
                if dep_id in id_set:
                    dependents[dep_id].append(st.subtask_id)
                    in_degree[st.subtask_id] += 1

        # Kahn 算法：逐层提取入度为 0 的节点
        queue: deque[str] = deque(
            sid for sid, deg in in_degree.items() if deg == 0
        )
        execution_order: list[list[str]] = []

        while queue:
            # 当前层所有入度为 0 的节点可并行执行
            level: list[str] = list(queue)
            queue.clear()
            execution_order.append(level)

            for sid in level:
                for dependent_id in dependents[sid]:
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        queue.append(dependent_id)

        # 检测环：如果排序后的节点数不等于总数，说明存在循环依赖
        total_sorted = sum(len(group) for group in execution_order)
        if total_sorted < len(subtasks):
            orphan_ids = id_set - {sid for group in execution_order for sid in group}
            logger.warning(
                f"检测到循环依赖，以下子任务无法排序: {orphan_ids}，追加到最后一组"
            )
            execution_order.append(list(orphan_ids))

        return execution_order

    # ------------------------------------------------------------------
    # 辩论检测
    # ------------------------------------------------------------------

    @staticmethod
    def _should_debate(intent: Intent, result: DecompositionResult) -> bool:
        """判断是否需要触发多智能体辩论。

        条件（满足任一即触发）：
        1. 任务复杂度为 HIGH
        2. 用户输入包含辩论相关关键词
        3. LLM 已标记 needs_debate

        Args:
            intent: 用户意图。
            result: 分解结果。

        Returns:
            是否需要辩论。
        """
        # LLM 已判断需要辩论
        if result.needs_debate:
            return True

        # 高复杂度任务
        if intent.estimated_complexity == TaskComplexity.HIGH:
            return True

        # 关键词匹配（不区分大小写）
        text_lower = intent.raw_text.lower()
        for keyword in _DEBATE_KEYWORDS:
            if keyword in text_lower:
                return True

        return False

    # ------------------------------------------------------------------
    # 安全回退
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback(task_id: str, intent: Intent) -> DecompositionResult:
        """LLM 不可用时的安全回退：将整个任务作为单个子任务。

        Args:
            task_id: 任务 ID。
            intent: 用户意图。

        Returns:
            包含单个子任务的分解结果。
        """
        subtask = SubTask(
            name="execute_task",
            description=intent.raw_text,
            action=intent.action or "chat",
            dependencies=[],
            estimated_complexity=intent.estimated_complexity,
            suggested_agent="general",
            parameters=intent.parameters,
        )
        return DecompositionResult(
            task_id=task_id,
            original_intent=intent.raw_text,
            subtasks=[subtask],
            needs_debate=False,
            reasoning="LLM 不可用，回退为单子任务执行",
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_complexity(value: str) -> TaskComplexity:
        """将字符串解析为 TaskComplexity 枚举。"""
        mapping = {
            "low": TaskComplexity.LOW,
            "medium": TaskComplexity.MEDIUM,
            "high": TaskComplexity.HIGH,
        }
        return mapping.get(value.lower(), TaskComplexity.MEDIUM)

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """去除 LLM 输出中可能包含的 markdown 代码围栏。"""
        text = text.strip()
        if text.startswith("```"):
            # 去掉首行 ```json 或 ```
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1:]
            # 去掉末尾 ```
            if text.endswith("```"):
                text = text[:-3].strip()
        return text
