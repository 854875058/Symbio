"""Execution pipeline -串联模型解析、预算拦截、执行策略、验证四个阶段。

Pipeline 是 Orchestrator 和各执行引擎之间的胶水层，不重写现有模块，
只负责按顺序调用各阶段并传递上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from symbio.utils.logger import get_logger
from symbio.utils.types import Result, Task

logger = get_logger("pipeline")


# ---------------------------------------------------------------------------
# Pipeline context - 在各阶段之间传递状态
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    """在管道各阶段之间共享的执行上下文。"""

    task: Task
    model_id: str | None = None
    budget_ticket: Any | None = None
    execution_mode: str = "dag"  # "dag" | "debate" | "subagent"
    root_span: Any | None = None
    result: Result | None = None


# ---------------------------------------------------------------------------
# Stage result - 控制管道流向
# ---------------------------------------------------------------------------

@dataclass
class StageResult:
    """阶段返回值，决定管道是否继续。"""

    should_short_circuit: bool = False
    final_result: Result | None = None

    @classmethod
    def continue_(cls) -> StageResult:
        """继续执行下一阶段。"""
        return cls(should_short_circuit=False)

    @classmethod
    def short_circuit(cls, result: Result) -> StageResult:
        """短路：跳过后续阶段，直接返回结果。"""
        return cls(should_short_circuit=True, final_result=result)


# ---------------------------------------------------------------------------
# Stage interface
# ---------------------------------------------------------------------------

class PipelineStage:
    """管道阶段基类。"""

    async def process(self, ctx: PipelineContext) -> StageResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Concrete stages (forward declarations - implemented in subsequent commits)
# ---------------------------------------------------------------------------

class ModelResolutionStage(PipelineStage):
    """阶段 1：解析最终模型 ID。"""

    def __init__(self, router: Any) -> None:
        self.router = router

    async def process(self, ctx: PipelineContext) -> StageResult:
        task = ctx.task

        # 1. 用户显式指定的模型优先
        override = task.metadata.get("model_override") if task.metadata else None
        if override and self.router.is_available(override):
            ctx.model_id = override
            logger.debug(f"使用用户指定模型: {override}")
            return StageResult.continue_()

        # 2. 按复杂度自动选择
        complexity = task.intent.estimated_complexity
        ctx.model_id = self.router.select(complexity)
        logger.debug(f"自动选择模型: {ctx.model_id} (complexity={complexity})")
        return StageResult.continue_()


class BudgetGateStage(PipelineStage):
    """阶段 2：事前预算检查。"""

    def __init__(self, guardrail: Any, budget_manager: Any = None) -> None:
        self.guardrail = guardrail
        self.budget_manager = budget_manager

    async def process(self, ctx: PipelineContext) -> StageResult:
        task = ctx.task

        # 1. 检查月度预算
        if self.budget_manager is not None:
            try:
                monthly_status = await self.budget_manager.check_monthly_budget()
                if getattr(monthly_status, "exceeded", False):
                    return StageResult.short_circuit(
                        Result(
                            task_id=task.task_id,
                            success=False,
                            content=(
                                f"月度预算已超限"
                                f"（${monthly_status.spent:.2f}/${monthly_status.limit:.2f}），"
                                f"任务被拦截。"
                            ),
                        )
                    )
            except Exception as exc:
                logger.warning(f"月度预算检查失败（不阻断）: {exc}")

        # 2. 签发资源票据
        ctx.budget_ticket = self.guardrail.issue_ticket(task.task_id)
        return StageResult.continue_()


class ExecutionStrategyStage(PipelineStage):
    """阶段 3：选择执行策略（DAG / 辩论 / SubAgent）。"""

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def process(self, ctx: PipelineContext) -> StageResult:
        task = ctx.task
        metadata = task.metadata or {}

        needs_debate = metadata.get("needs_debate", False)
        decomposition = metadata.get("decomposition")

        if needs_debate:
            ctx.execution_mode = "debate"
            debate_config = metadata.get("debate_config")
            ctx.result = await self.orchestrator._execute_with_debate(
                task, debate_config, root_span=ctx.root_span
            )
        elif decomposition and len(getattr(decomposition, "subtasks", [])) > 1:
            ctx.execution_mode = "subagent"
            ctx.result = await self.orchestrator._execute_with_subagents(
                task, decomposition, root_span=ctx.root_span
            )
        else:
            ctx.execution_mode = "dag"
            ctx.result = await self.orchestrator._execute_via_dag(
                task, root_span=ctx.root_span, release_ticket=False
            )

        return StageResult.continue_()


class VerificationStage(PipelineStage):
    """阶段 4：验证执行结果。"""

    def __init__(self, testing_agent: Any = None) -> None:
        self.testing_agent = testing_agent

    async def process(self, ctx: PipelineContext) -> StageResult:
        # 如果执行已经失败，跳过验证
        if ctx.result is not None and not ctx.result.success:
            return StageResult.continue_()

        # 当前阶段：基于结果内容做基本证据检查
        # 后续迭代可接入 TestingAgent 做深度验证
        if ctx.result is not None and ctx.result.content:
            content = ctx.result.content
            if "error" in content.lower() and "traceback" in content.lower():
                ctx.result = Result(
                    task_id=ctx.task.task_id,
                    success=False,
                    content=f"验证失败: 结果包含错误信息",
                )

        return StageResult.continue_()


# ---------------------------------------------------------------------------
# Pipeline 主类
# ---------------------------------------------------------------------------

class ExecutionPipeline:
    """执行管道：串联各阶段，替代 Orchestrator 中的直接调用。"""

    def __init__(
        self,
        *,
        model_resolution: ModelResolutionStage,
        budget_gate: BudgetGateStage,
        execution_strategy: ExecutionStrategyStage,
        verification: VerificationStage,
    ) -> None:
        self.stages: list[PipelineStage] = [
            model_resolution,
            budget_gate,
            execution_strategy,
            verification,
        ]

    async def execute(self, task: Task, *, root_span: Any = None) -> Result:
        """运行完整管道。"""
        ctx = PipelineContext(task=task, root_span=root_span)

        for stage in self.stages:
            stage_name = type(stage).__name__
            logger.debug(f"管道阶段: {stage_name}")
            result = await stage.process(ctx)
            if result.should_short_circuit:
                logger.debug(f"管道短路于 {stage_name}")
                return result.final_result  # type: ignore[return-value]

        if ctx.result is None:
            return Result(
                task_id=task.task_id,
                success=False,
                content="执行管道未产生结果",
            )

        return ctx.result


def build_pipeline(orchestrator: Any) -> ExecutionPipeline:
    """从 Orchestrator 实例构建默认管道。"""
    from symbio.config.settings import get_settings

    settings = get_settings()
    budget_manager = getattr(orchestrator, "budget_manager", None)

    return ExecutionPipeline(
        model_resolution=ModelResolutionStage(orchestrator.router),
        budget_gate=BudgetGateStage(orchestrator.guardrail, budget_manager),
        execution_strategy=ExecutionStrategyStage(orchestrator),
        verification=VerificationStage(),
    )
