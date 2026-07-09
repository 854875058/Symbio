"""SubAgent Manager — 动态创建、生命周期管理与结果聚合。

核心职责：
1. 根据任务分解结果动态选择并执行子任务
2. 按依赖拓扑串行分组、组内并行执行
3. 在子任务间传递上下文与中间结果
4. 聚合所有子任务结果为统一输出
5. 通过 EventBus 广播子任务生命周期事件
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from symbio.distributed import RayExecutor

from symbio.agents.base import BaseAgent
from symbio.agents.registry import AgentRegistry
from symbio.core.decomposer import SubTask
from symbio.core.event_bus import Event, EventBus, EventType
from symbio.core.rate_limiter import RateLimiter
from symbio.utils.logger import get_logger
from symbio.utils.types import AgentState, Intent, Result, Task, TokenUsage

logger = get_logger("subagent_manager")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class SubAgentTask(BaseModel):
    """传递给子 Agent 的任务描述。"""

    subtask_id: str
    name: str
    description: str
    action: str
    agent_type: str = "general"
    parameters: dict[str, Any] = Field(default_factory=dict)
    parent_task_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class SubAgentResult(BaseModel):
    """单个子任务的执行结果。"""

    subtask_id: str
    agent_name: str
    success: bool
    content: str = ""
    token_usage: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    error: Optional[str] = None


class AggregatedResult(BaseModel):
    """所有子任务结果的聚合。"""

    task_id: str
    total_subtasks: int = 0
    completed_subtasks: int = 0
    failed_subtasks: int = 0
    results: list[SubAgentResult] = Field(default_factory=list)
    combined_content: str = ""
    total_token_usage: dict[str, Any] = Field(default_factory=dict)
    success: bool = False


# ---------------------------------------------------------------------------
# SubAgentManager
# ---------------------------------------------------------------------------


class SubAgentManager:
    """SubAgent 管理器。

    负责按依赖顺序调度子任务、并行执行同组子任务、聚合结果。
    """

    def __init__(
        self,
        registry: AgentRegistry,
        event_bus: EventBus,
        rate_limiter: RateLimiter,
        executor: Optional["RayExecutor"] = None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus
        self.rate_limiter = rate_limiter
        # 可选的分布式执行器（RayExecutor）。为 None 时组内并行走进程内 asyncio
        # （默认、现有行为）；注入且可用时，组内子任务分发到 Ray Actor 池跨进程执行。
        self._executor = executor
        self._active_subagents: dict[str, BaseAgent] = {}
        self._results: dict[str, SubAgentResult] = {}

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def execute_subtasks(
        self,
        subtasks: list[SubTask],
        parent_task: Task,
        execution_order: list[list[str]],
    ) -> AggregatedResult:
        """按照 ``execution_order`` 执行子任务并聚合结果。

        ``execution_order`` 是一个二维列表，外层为串行分组，内层为同一组中
        可并行执行的子任务 ID。

        Args:
            subtasks: 子任务列表。
            parent_task: 父任务，用于提取 parent_task_id 和共享上下文。
            execution_order: 执行顺序，形如 ``[["id_a", "id_b"], ["id_c"]]``。

        Returns:
            聚合后的执行结果。
        """
        self._results.clear()
        subtask_map: dict[str, SubTask] = {st.subtask_id: st for st in subtasks}

        logger.info(
            f"开始执行 {len(subtasks)} 个子任务, "
            f"父任务={parent_task.task_id}, 分组数={len(execution_order)}"
        )

        # 是否走 Ray 分布式：注入了 executor 且 Ray 可用才启用，否则回退 asyncio
        use_ray = self._executor is not None and self._executor.available()

        # 逐组串行执行
        for group_idx, group in enumerate(execution_order):
            logger.debug(f"执行第 {group_idx + 1} 组: {group}")
            group_ids = [sid for sid in group if sid in subtask_map]
            if use_ray:
                await self._execute_group_on_ray(
                    [subtask_map[sid] for sid in group_ids], parent_task
                )
            else:
                coros = [
                    self._execute_single_subtask(subtask_map[sid], parent_task)
                    for sid in group_ids
                ]
                await asyncio.gather(*coros)

        # 聚合结果
        aggregated = self._aggregate(parent_task.task_id)
        logger.info(
            f"子任务执行完毕: 总计={aggregated.total_subtasks}, "
            f"成功={aggregated.completed_subtasks}, "
            f"失败={aggregated.failed_subtasks}, "
            f"整体成功={aggregated.success}"
        )
        return aggregated

    # ------------------------------------------------------------------
    # 单子任务执行
    # ------------------------------------------------------------------

    async def _execute_single_subtask(
        self,
        subtask: SubTask,
        parent_task: Task,
    ) -> None:
        """执行单个子任务，将结果存入 ``self._results``。"""

        start_time = time.monotonic()

        # 1. 选择 Agent
        agent = self._resolve_agent(subtask)
        agent_name = agent.name if agent else "unknown"

        # 广播 AGENT_SPAWNED
        await self._emit_event(
            EventType.AGENT_SPAWNED,
            {
                "subtask_id": subtask.subtask_id,
                "subtask_name": subtask.name,
                "agent_name": agent_name,
                "parent_task_id": parent_task.task_id,
            },
        )

        # 2. 构建子任务上下文（父上下文 + 依赖结果）
        merged_context = self._build_context(subtask, parent_task)

        # 3. 构建 Task 对象
        task = Task(
            intent=Intent(
                raw_text=subtask.description or subtask.name,
                action=subtask.action,
                parameters={**subtask.parameters, **merged_context},
            ),
            parent_task_id=parent_task.task_id,
            metadata={
                "subtask_id": subtask.subtask_id,
                "subtask_name": subtask.name,
                "agent_type": subtask.suggested_agent,
            },
        )

        # 4. 执行（含速率限制与错误隔离）
        try:
            if agent is None:
                raise RuntimeError(
                    f"未找到适合子任务 '{subtask.name}' 的 Agent"
                )

            model = task.model or "default"
            await self.rate_limiter.acquire(model)

            self._active_subagents[subtask.subtask_id] = agent
            result: Result = await agent.execute(task)

            duration_ms = (time.monotonic() - start_time) * 1000
            token_dict = self._token_usage_to_dict(result.token_usage)

            sub_result = SubAgentResult(
                subtask_id=subtask.subtask_id,
                agent_name=agent_name,
                success=result.success,
                content=result.content,
                token_usage=token_dict,
                duration_ms=duration_ms,
            )

            # 广播 AGENT_COMPLETED
            await self._emit_event(
                EventType.AGENT_COMPLETED,
                {
                    "subtask_id": subtask.subtask_id,
                    "agent_name": agent_name,
                    "success": result.success,
                    "duration_ms": duration_ms,
                },
            )

        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            error_msg = str(exc)
            logger.error(
                f"子任务 '{subtask.name}' (id={subtask.subtask_id}) "
                f"执行失败: {error_msg}"
            )

            sub_result = SubAgentResult(
                subtask_id=subtask.subtask_id,
                agent_name=agent_name,
                success=False,
                content="",
                duration_ms=duration_ms,
                error=error_msg,
            )

            # 广播 AGENT_FAILED
            await self._emit_event(
                EventType.AGENT_FAILED,
                {
                    "subtask_id": subtask.subtask_id,
                    "agent_name": agent_name,
                    "error": error_msg,
                    "duration_ms": duration_ms,
                },
            )

        finally:
            self._active_subagents.pop(subtask.subtask_id, None)

        self._results[subtask.subtask_id] = sub_result

    # ------------------------------------------------------------------
    # Ray 分布式组执行
    # ------------------------------------------------------------------

    async def _execute_group_on_ray(
        self,
        group: list[SubTask],
        parent_task: Task,
    ) -> None:
        """把同组子任务分发到 Ray Actor 池跨进程并行执行。

        与 asyncio 路径的差异：
        - Agent 在 worker 进程内按 name 重建，主进程只传 (agent_name, task_dict)。
        - EventBus 不可跨进程序列化，故 SPAWNED 事件在提交前于主进程补发，
          COMPLETED/FAILED 事件在收集结果后于主进程补发——保持事件语义不变。
        - 速率限制仍在主进程 acquire（提交即视为一次调用）。
        """
        submissions = []  # (subtask, agent_name, ref) 或 (subtask, agent_name, None, error)
        for subtask in group:
            agent = self._resolve_agent(subtask)
            agent_name = agent.name if agent else "unknown"

            await self._emit_event(
                EventType.AGENT_SPAWNED,
                {
                    "subtask_id": subtask.subtask_id,
                    "subtask_name": subtask.name,
                    "agent_name": agent_name,
                    "parent_task_id": parent_task.task_id,
                },
            )

            if agent is None:
                submissions.append((subtask, agent_name, None, "未找到合适的 Agent"))
                continue

            merged_context = self._build_context(subtask, parent_task)
            task = Task(
                intent=Intent(
                    raw_text=subtask.description or subtask.name,
                    action=subtask.action,
                    parameters={**subtask.parameters, **merged_context},
                ),
                parent_task_id=parent_task.task_id,
                metadata={
                    "subtask_id": subtask.subtask_id,
                    "subtask_name": subtask.name,
                    "agent_type": subtask.suggested_agent,
                },
            )
            await self.rate_limiter.acquire(task.model or "default")
            ref = self._executor.submit(agent_name, task)
            submissions.append((subtask, agent_name, ref, None))

        # 收集所有已提交的 ObjectRef（gather 阻塞在线程池里跑，不卡事件循环）
        refs = [s[2] for s in submissions if s[2] is not None]
        outputs = await asyncio.to_thread(self._executor.gather, refs) if refs else []
        out_iter = iter(outputs)

        for subtask, agent_name, ref, pre_error in submissions:
            start_ok = ref is not None and pre_error is None
            if not start_ok:
                sub_result = SubAgentResult(
                    subtask_id=subtask.subtask_id,
                    agent_name=agent_name,
                    success=False,
                    content="",
                    error=pre_error or "提交失败",
                )
            else:
                out = next(out_iter)
                if out.get("ok"):
                    rd = out["result"]
                    sub_result = SubAgentResult(
                        subtask_id=subtask.subtask_id,
                        agent_name=agent_name,
                        success=bool(rd.get("success", False)),
                        content=rd.get("content", ""),
                        token_usage=self._token_usage_to_dict(
                            TokenUsage(**rd["token_usage"]) if rd.get("token_usage") else TokenUsage()
                        ),
                    )
                else:
                    sub_result = SubAgentResult(
                        subtask_id=subtask.subtask_id,
                        agent_name=agent_name,
                        success=False,
                        content="",
                        error=out.get("error", "worker 执行失败"),
                    )

            evt = EventType.AGENT_COMPLETED if sub_result.success else EventType.AGENT_FAILED
            payload = {
                "subtask_id": subtask.subtask_id,
                "agent_name": agent_name,
                "success": sub_result.success,
            }
            if not sub_result.success and sub_result.error:
                payload["error"] = sub_result.error
            await self._emit_event(evt, payload)

            self._results[subtask.subtask_id] = sub_result

    # ------------------------------------------------------------------
    # Agent 选择
    # ------------------------------------------------------------------

    def _resolve_agent(self, subtask: SubTask) -> Optional[BaseAgent]:
        """为子任务选择最佳 Agent。

        优先使用 ``suggested_agent``，其次通过 ``registry.find_best``，
        最后回退到 ``"general"`` 类型。
        """
        # 1. suggested_agent 直接指定
        if subtask.suggested_agent:
            agent = self.registry.get(subtask.suggested_agent)
            if agent is not None:
                logger.debug(
                    f"子任务 '{subtask.name}' 使用建议 Agent: "
                    f"{subtask.suggested_agent}"
                )
                return agent

        # 2. 通过 Intent 匹配
        intent = Intent(
            raw_text=subtask.description or subtask.name,
            action=subtask.action,
            parameters=subtask.parameters,
        )
        agent = self.registry.find_best(intent)
        if agent is not None:
            logger.debug(
                f"子任务 '{subtask.name}' 通过意图匹配 Agent: {agent.name}"
            )
            return agent

        # 3. 回退到 general
        fallback = self.registry.get("general")
        if fallback is not None:
            logger.debug(
                f"子任务 '{subtask.name}' 回退到 general Agent"
            )
            return fallback

        logger.warning(f"子任务 '{subtask.name}' 未找到任何可用 Agent")
        return None

    # ------------------------------------------------------------------
    # 上下文构建
    # ------------------------------------------------------------------

    def _build_context(
        self,
        subtask: SubTask,
        parent_task: Task,
    ) -> dict[str, Any]:
        """合并父任务上下文、子任务自身上下文及依赖子任务的结果。

        合并优先级（后者覆盖前者）：
        1. 父任务 metadata
        2. 子任务自身 context
        3. 依赖子任务的结果（以 ``dependency_results`` 键注入）
        """
        merged: dict[str, Any] = {}

        # 父任务共享上下文
        if parent_task.metadata:
            merged.update(parent_task.metadata)

        # 子任务参数作为上下文
        if subtask.parameters:
            merged.update(subtask.parameters)

        # 依赖子任务的结果
        dependency_results: dict[str, dict[str, Any]] = {}
        for dep_id in subtask.dependencies:
            if dep_id in self._results:
                dep_result = self._results[dep_id]
                dependency_results[dep_id] = {
                    "success": dep_result.success,
                    "content": dep_result.content,
                    "agent_name": dep_result.agent_name,
                }
        if dependency_results:
            merged["dependency_results"] = dependency_results

        return merged

    # ------------------------------------------------------------------
    # 结果聚合
    # ------------------------------------------------------------------

    def _aggregate(self, task_id: str) -> AggregatedResult:
        """将所有子任务结果聚合为 ``AggregatedResult``。"""
        all_results = list(self._results.values())
        completed = [r for r in all_results if r.success]
        failed = [r for r in all_results if not r.success]

        # 合并内容：为每个成功结果添加段落标题
        sections: list[str] = []
        for r in completed:
            header = f"## [{r.agent_name}] 子任务 {r.subtask_id}"
            sections.append(f"{header}\n{r.content}")
        combined_content = "\n\n".join(sections)

        # 汇总 token 用量
        total_usage = self._merge_token_usage(all_results)

        overall_success = len(failed) == 0 and len(all_results) > 0

        return AggregatedResult(
            task_id=task_id,
            total_subtasks=len(all_results),
            completed_subtasks=len(completed),
            failed_subtasks=len(failed),
            results=all_results,
            combined_content=combined_content,
            total_token_usage=total_usage,
            success=overall_success,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _token_usage_to_dict(usage: TokenUsage) -> dict[str, Any]:
        """将 ``TokenUsage`` 转换为普通字典。"""
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "model": usage.model,
            "cost_usd": usage.cost_usd,
        }

    @staticmethod
    def _merge_token_usage(
        results: list[SubAgentResult],
    ) -> dict[str, Any]:
        """汇总多个子任务结果中的 token 用量。"""
        total_input = 0
        total_output = 0
        total_tokens = 0
        total_cost = 0.0
        models_seen: set[str] = set()

        for r in results:
            usage = r.token_usage
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
            total_tokens += usage.get("total_tokens", 0)
            total_cost += usage.get("cost_usd", 0.0)
            model = usage.get("model", "")
            if model:
                models_seen.add(model)

        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "cost_usd": total_cost,
            "models": sorted(models_seen),
        }

    async def _emit_event(
        self,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        """向 EventBus 发送事件。"""
        await self.event_bus.emit(
            Event(
                type=event_type,
                data=data,
                source="subagent_manager",
            )
        )

    def get_active_subagents(self) -> dict[str, str]:
        """返回当前活跃子 Agent 的 ``{subtask_id: agent_name}`` 映射。"""
        return {
            sid: agent.name
            for sid, agent in self._active_subagents.items()
        }

    def get_results(self) -> dict[str, SubAgentResult]:
        """返回已完成子任务的结果映射。"""
        return dict(self._results)
