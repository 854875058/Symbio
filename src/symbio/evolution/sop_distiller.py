"""SOP Distiller & Async Trajectory Capture — 从成功轨迹提取 SOP，异步捕获轨迹步骤。

支持：
- 从成功轨迹中蒸馏标准操作流程（SOP）
- 异步内存队列捕获轨迹步骤（< 0.1ms per step）
- 后台批量写入（100 条或 5 秒间隔）
- 背压控制：队列 > 10000 时丢弃低优先级事件
- 内置种子 SOP 覆盖常见任务类型
"""

from __future__ import annotations

import asyncio
import statistics
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger
from symbio.utils.types import TrajectoryStep

logger = get_logger("sop_distiller")


# =============================================================================
# 1. 数据模型
# =============================================================================


class DecisionPoint(BaseModel):
    """轨迹中的关键决策点。"""

    point_id: str = Field(default_factory=lambda: str(uuid4()))
    step_id: int = Field(description="对应的轨迹步骤编号")
    description: str = Field(description="决策描述")
    alternatives: list[str] = Field(default_factory=list, description="备选方案")
    chosen_action: str = Field(default="", description="最终选择的操作")
    rationale: str = Field(default="", description="选择理由")
    impact: str = Field(default="medium", description="影响程度: low / medium / high")


class SOP(BaseModel):
    """标准操作流程（Standard Operating Procedure）。"""

    sop_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(description="SOP 名称")
    description: str = Field(default="", description="SOP 描述")
    task_type: str = Field(default="", description="适用任务类型")
    steps: list[str] = Field(default_factory=list, description="操作步骤列表")
    decision_points: list[DecisionPoint] = Field(default_factory=list, description="关键决策点")
    success_rate: float = Field(default=1.0, description="成功率 (0.0 ~ 1.0)")
    avg_tokens: int = Field(default=0, description="平均 token 消耗")
    avg_steps: float = Field(default=0.0, description="平均步骤数")
    avg_duration_ms: int = Field(default=0, description="平均耗时(ms)")
    source_trajectory_ids: list[str] = Field(default_factory=list, description="来源轨迹 ID")
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SOPQualityThresholds(BaseModel):
    """SOP 蒸馏质量阈值配置。"""

    min_success_rate: float = Field(default=1.0, description="最低成功率，必须 100% 成功")
    max_tokens_ratio: float = Field(default=1.5, description="token 上限 = 中位数 * 此系数")
    max_steps_ratio: float = Field(default=1.5, description="步骤上限 = 中位数 * 此系数")
    max_retries: int = Field(default=1, description="最大允许重试次数")


class TrajectoryQueueStats(BaseModel):
    """异步轨迹捕获队列统计。"""

    total_enqueued: int = Field(default=0, description="累计入队数")
    total_written: int = Field(default=0, description="累计写入数")
    total_dropped: int = Field(default=0, description="累计丢弃数")
    queue_size: int = Field(default=0, description="当前队列长度")
    batch_count: int = Field(default=0, description="累计批次数")
    last_batch_at: Optional[datetime] = Field(default=None, description="最后一批写入时间")
    is_running: bool = Field(default=False, description="后台消费者是否运行中")


class StepPriority(str, Enum):
    """轨迹步骤优先级，用于背压控制。"""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class PrioritizedStep(BaseModel):
    """带优先级的轨迹步骤包装。"""

    step: TrajectoryStep
    trajectory_id: str = ""
    priority: StepPriority = StepPriority.NORMAL
    enqueued_at: datetime = Field(default_factory=datetime.now)


# =============================================================================
# 2. SOPDistiller — SOP 蒸馏器
# =============================================================================


class SOPDistiller:
    """从成功轨迹中蒸馏标准操作流程。

    质量阈值：
    - success_rate == 100%（轨迹必须完全成功）
    - tokens < median * 1.5（token 消耗不超过中位数的 1.5 倍）
    - steps < median * 1.5（步骤数不超过中位数的 1.5 倍）
    - retries <= 1（最多允许 1 次重试）
    """

    def __init__(
        self,
        thresholds: Optional[SOPQualityThresholds] = None,
    ) -> None:
        self._thresholds = thresholds or SOPQualityThresholds()
        # 历史统计缓存，用于计算中位数
        self._token_history: list[int] = []
        self._step_history: list[int] = []
        self._lock = threading.Lock()

    @property
    def thresholds(self) -> SOPQualityThresholds:
        """当前质量阈值配置。"""
        return self._thresholds

    def update_statistics(
        self,
        token_counts: list[int],
        step_counts: list[int],
    ) -> None:
        """更新历史统计数据，用于计算中位数基准线。"""
        with self._lock:
            self._token_history.extend(token_counts)
            self._step_history.extend(step_counts)
            logger.debug(
                "统计更新: token_history=%d, step_history=%d",
                len(self._token_history),
                len(self._step_history),
            )

    def distill(
        self,
        trajectory: "TrajectoryData",
    ) -> Optional[SOP]:
        """从轨迹中蒸馏 SOP。

        Args:
            trajectory: 包含 steps, success, token_count, retry_count 等信息。

        Returns:
            符合质量阈值时返回 SOP，否则返回 None。
        """
        # 必须成功
        if not trajectory.success:
            logger.debug("轨迹 %s 未成功，跳过", trajectory.trajectory_id)
            return None

        # 重试次数检查
        if trajectory.retry_count > self._thresholds.max_retries:
            logger.debug(
                "轨迹 %s 重试次数 %d 超过阈值 %d",
                trajectory.trajectory_id,
                trajectory.retry_count,
                self._thresholds.max_retries,
            )
            return None

        # 中位数基准检查（有历史数据时才检查）
        with self._lock:
            if self._token_history:
                median_tokens = statistics.median(self._token_history)
                token_limit = median_tokens * self._thresholds.max_tokens_ratio
                if trajectory.token_count > token_limit:
                    logger.debug(
                        "轨迹 %s token %d 超过中位数阈值 %.0f",
                        trajectory.trajectory_id,
                        trajectory.token_count,
                        token_limit,
                    )
                    return None

            if self._step_history:
                median_steps = statistics.median(self._step_history)
                step_limit = median_steps * self._thresholds.max_steps_ratio
                if len(trajectory.steps) > step_limit:
                    logger.debug(
                        "轨迹 %s 步骤数 %d 超过中位数阈值 %.0f",
                        trajectory.trajectory_id,
                        len(trajectory.steps),
                        step_limit,
                    )
                    return None

        # 识别决策点
        decision_points = self.identify_decision_points(trajectory.steps)

        # 提取步骤描述
        step_descriptions = self._extract_step_descriptions(trajectory.steps)

        # 构建 SOP
        sop = SOP(
            name=self._generate_sop_name(trajectory),
            description=f"从轨迹 {trajectory.trajectory_id} 蒸馏的操作流程",
            task_type=trajectory.task_type,
            steps=step_descriptions,
            decision_points=decision_points,
            success_rate=1.0,
            avg_tokens=trajectory.token_count,
            avg_steps=float(len(trajectory.steps)),
            avg_duration_ms=trajectory.duration_ms,
            source_trajectory_ids=[trajectory.trajectory_id],
            metadata=trajectory.metadata,
        )

        # 更新统计
        with self._lock:
            self._token_history.append(trajectory.token_count)
            self._step_history.append(len(trajectory.steps))

        logger.info(
            "SOP 蒸馏成功: %s (%d 步骤, %d 决策点)",
            sop.name,
            len(sop.steps),
            len(sop.decision_points),
        )
        return sop

    def identify_decision_points(
        self,
        steps: list[TrajectoryStep],
    ) -> list[DecisionPoint]:
        """识别轨迹中的关键决策点。

        规则：
        - 有多个 tool_calls 的步骤（需要选择工具）
        - thought 中包含比较/选择关键词的步骤
        - 失败后重试并切换策略的步骤
        """
        decision_points: list[DecisionPoint] = []
        choice_keywords = [
            "alternatively",
            "instead",
            "另一个方法",
            "换一种",
            "选择",
            "也可以",
            " or use ",
            " or try ",
            "better approach",
            "try a different",
            "another way",
        ]

        for i, step in enumerate(steps):
            reasons: list[str] = []

            # 多工具调用 → 需要选择
            if len(step.tool_calls) > 1:
                reasons.append(f"选择工具: {[tc.tool_name for tc in step.tool_calls]}")

            # 思维中包含选择/比较关键词
            thought_lower = step.thought.lower()
            matched = [kw for kw in choice_keywords if kw in thought_lower]
            if matched:
                reasons.append(f"决策关键词: {matched}")

            # 失败后切换策略（前一步有失败的 tool_result）
            if i > 0:
                prev = steps[i - 1]
                if any(not tr.success for tr in prev.tool_results):
                    reasons.append("前一步工具失败，切换策略")

            if reasons:
                alternatives = []
                if len(step.tool_calls) > 1:
                    alternatives = [tc.tool_name for tc in step.tool_calls]
                dp = DecisionPoint(
                    step_id=step.step_id,
                    description="; ".join(reasons),
                    alternatives=alternatives,
                    chosen_action=step.action,
                    rationale=step.thought[:200] if step.thought else "",
                    impact="high" if len(step.tool_calls) > 1 else "medium",
                )
                decision_points.append(dp)

        return decision_points

    def _extract_step_descriptions(
        self,
        steps: list[TrajectoryStep],
    ) -> list[str]:
        """从轨迹步骤中提取简洁描述。"""
        descriptions: list[str] = []
        for step in steps:
            parts: list[str] = []
            if step.thought:
                # 截取前 120 字符作为摘要
                thought_brief = step.thought[:120]
                if len(step.thought) > 120:
                    thought_brief += "..."
                parts.append(thought_brief)
            if step.action:
                parts.append(f"Action: {step.action}")
            if step.tool_calls:
                tool_names = [tc.tool_name for tc in step.tool_calls]
                parts.append(f"Tools: {', '.join(tool_names)}")
            if step.observation:
                obs_brief = step.observation[:80]
                if len(step.observation) > 80:
                    obs_brief += "..."
                parts.append(f"Result: {obs_brief}")
            descriptions.append(" | ".join(parts) if parts else f"Step {step.step_id}")
        return descriptions

    def _generate_sop_name(self, trajectory: "TrajectoryData") -> str:
        """基于任务类型和摘要生成 SOP 名称。"""
        task_type = trajectory.task_type or "general"
        short_id = trajectory.trajectory_id[:8]
        return f"SOP-{task_type}-{short_id}"


# TrajectoryData 用于 SOPDistiller 输入（轻量数据类，不依赖完整 Trajectory 模型）
class TrajectoryData(BaseModel):
    """蒸馏输入数据，封装轨迹关键信息。"""

    trajectory_id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: str = Field(default="", description="任务类型")
    steps: list[TrajectoryStep] = Field(default_factory=list)
    success: bool = Field(default=False)
    token_count: int = Field(default=0, description="总 token 消耗")
    retry_count: int = Field(default=0, description="重试次数")
    duration_ms: int = Field(default=0, description="总耗时(ms)")
    metadata: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# 3. AsyncTrajectoryCapture — 异步轨迹捕获
# =============================================================================


class AsyncTrajectoryCapture:
    """异步轨迹捕获器：内存队列 + 后台批量写入。

    性能目标：每次 capture_step < 0.1ms（纯内存入队）。
    背压策略：队列 > 10000 时丢弃 LOW 优先级事件。
    批量策略：100 条或 5 秒间隔触发写入。
    """

    DEFAULT_QUEUE_MAX = 10_000
    DEFAULT_BATCH_SIZE = 100
    DEFAULT_FLUSH_INTERVAL = 5.0  # 秒

    def __init__(
        self,
        writer: Callable[[list[PrioritizedStep]], None],
        queue_max: int = DEFAULT_QUEUE_MAX,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        """初始化异步捕获器。

        Args:
            writer: 批量写入回调，接收 PrioritizedStep 列表。
            queue_max: 队列容量上限，超过后触发背压。
            batch_size: 每批写入条数。
            flush_interval: 定时刷新间隔（秒）。
        """
        self._writer = writer
        self._queue_max = queue_max
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        self._queue: asyncio.Queue[PrioritizedStep] | None = None
        self._consumer_task: asyncio.Task | None = None
        self._running = False
        self._stats = TrajectoryQueueStats()
        self._closed = False

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """确保有可用的事件循环。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop

    def start(self) -> None:
        """启动后台消费者。"""
        if self._running:
            logger.warning("AsyncTrajectoryCapture 已在运行")
            return

        loop = self._ensure_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_max * 2)  # 留出缓冲
        self._running = True
        self._stats.is_running = True
        self._closed = False

        if loop.is_running():
            # 在已有事件循环中（如 FastAPI），通过 ensure_future 启动
            self._consumer_task = asyncio.ensure_future(self._consumer_loop(), loop=loop)
        else:
            self._consumer_task = loop.create_task(self._consumer_loop())

        logger.info(
            "AsyncTrajectoryCapture 已启动 (batch=%d, interval=%.1fs, max=%d)",
            self._batch_size,
            self._flush_interval,
            self._queue_max,
        )

    async def start_async(self) -> None:
        """异步启动后台消费者（用于 async 上下文）。"""
        if self._running:
            logger.warning("AsyncTrajectoryCapture 已在运行")
            return

        self._queue = asyncio.Queue(maxsize=self._queue_max * 2)
        self._running = True
        self._stats.is_running = True
        self._closed = False

        self._consumer_task = asyncio.create_task(self._consumer_loop())

        logger.info(
            "AsyncTrajectoryCapture 已启动 (batch=%d, interval=%.1fs, max=%d)",
            self._batch_size,
            self._flush_interval,
            self._queue_max,
        )

    def stop(self) -> None:
        """停止后台消费者并刷新剩余数据。"""
        if not self._running:
            return

        self._running = False
        self._stats.is_running = False

        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()

        # 尝试同步刷新剩余队列
        self._flush_remaining()

        self._closed = True
        logger.info(
            "AsyncTrajectoryCapture 已停止 (enqueued=%d, written=%d, dropped=%d)",
            self._stats.total_enqueued,
            self._stats.total_written,
            self._stats.total_dropped,
        )

    async def stop_async(self) -> None:
        """异步停止后台消费者并刷新剩余数据。"""
        if not self._running:
            return

        self._running = False
        self._stats.is_running = False

        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        # 异步刷新剩余队列
        await self._flush_remaining_async()

        self._closed = True
        logger.info(
            "AsyncTrajectoryCapture 已停止 (enqueued=%d, written=%d, dropped=%d)",
            self._stats.total_enqueued,
            self._stats.total_written,
            self._stats.total_dropped,
        )

    def capture_step(
        self,
        step: TrajectoryStep,
        trajectory_id: str = "",
        priority: StepPriority = StepPriority.NORMAL,
    ) -> bool:
        """将轨迹步骤入队。

        性能目标：< 0.1ms（非阻塞入队）。

        Args:
            step: 轨迹步骤。
            trajectory_id: 关联的轨迹 ID。
            priority: 步骤优先级。

        Returns:
            True 表示成功入队，False 表示被背压丢弃。
        """
        if not self._running or self._queue is None:
            logger.warning("AsyncTrajectoryCapture 未启动，步骤被丢弃")
            return False

        # 背压控制
        if self._queue.qsize() > self._queue_max:
            if priority == StepPriority.LOW:
                self._stats.total_dropped += 1
                logger.debug("背压丢弃 LOW 优先级步骤")
                return False

        item = PrioritizedStep(
            step=step,
            trajectory_id=trajectory_id,
            priority=priority,
        )

        try:
            self._queue.put_nowait(item)
            self._stats.total_enqueued += 1
            self._stats.queue_size = self._queue.qsize()
            return True
        except asyncio.QueueFull:
            # 队列满了，丢弃 LOW 优先级
            if priority == StepPriority.LOW:
                self._stats.total_dropped += 1
                return False
            # 非 LOW 优先级，尝试腾出空间
            try:
                # 丢弃一个 LOW 优先级的来腾空间
                self._evict_low_priority()
                self._queue.put_nowait(item)
                self._stats.total_enqueued += 1
                return True
            except (asyncio.QueueFull, ValueError):
                self._stats.total_dropped += 1
                return False

    @property
    def stats(self) -> TrajectoryQueueStats:
        """当前队列统计。"""
        if self._queue is not None:
            self._stats.queue_size = self._queue.qsize()
        return self._stats.model_copy()

    async def _consumer_loop(self) -> None:
        """后台消费者主循环：批量写入 + 定时刷新。"""
        logger.debug("消费者循环启动")
        try:
            while self._running:
                batch: list[PrioritizedStep] = []
                deadline = time.monotonic() + self._flush_interval

                # 收集批次
                while len(batch) < self._batch_size and self._running:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(
                            self._queue.get(), timeout=min(remaining, 0.5)
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break
                    except asyncio.CancelledError:
                        break

                # 写入批次
                if batch:
                    self._write_batch(batch)

        except asyncio.CancelledError:
            logger.debug("消费者循环被取消")
        except Exception as e:
            logger.error("消费者循环异常: %s", e)
        finally:
            logger.debug("消费者循环退出")

    def _write_batch(self, batch: list[PrioritizedStep]) -> None:
        """执行批量写入。"""
        try:
            self._writer(batch)
            self._stats.total_written += len(batch)
            self._stats.batch_count += 1
            self._stats.last_batch_at = datetime.now()
            logger.debug("批量写入 %d 条", len(batch))
        except Exception as e:
            logger.error("批量写入失败: %s", e)

    def _evict_low_priority(self) -> None:
        """从队列中移除一个 LOW 优先级的项目来腾空间。"""
        if self._queue is None:
            raise ValueError("队列未初始化")

        # 将队列内容全部取出，丢弃一个 LOW，其余放回
        items: list[PrioritizedStep] = []
        evicted = False
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if not evicted and item.priority == StepPriority.LOW:
                    evicted = True
                    self._stats.total_dropped += 1
                else:
                    items.append(item)
            except asyncio.QueueEmpty:
                break

        for item in items:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._stats.total_dropped += 1

    def _flush_remaining(self) -> None:
        """同步刷新队列中剩余的数据。"""
        if self._queue is None:
            return
        batch: list[PrioritizedStep] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(batch) >= self._batch_size:
                self._write_batch(batch)
                batch = []
        if batch:
            self._write_batch(batch)

    async def _flush_remaining_async(self) -> None:
        """异步刷新队列中剩余的数据。"""
        if self._queue is None:
            return
        batch: list[PrioritizedStep] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(batch) >= self._batch_size:
                self._write_batch(batch)
                batch = []
        if batch:
            self._write_batch(batch)


# =============================================================================
# 4. SeedSOP — 内置种子 SOP
# =============================================================================


class SeedSOP:
    """内置种子 SOP，覆盖常见任务类型。

    包括：代码生成、调试、数据分析、API 集成、部署。
    """

    @staticmethod
    def get_seeds() -> list[SOP]:
        """获取所有种子 SOP。"""
        return [
            SeedSOP._code_generation(),
            SeedSOP._debugging(),
            SeedSOP._data_analysis(),
            SeedSOP._api_integration(),
            SeedSOP._deployment(),
        ]

    @staticmethod
    def _code_generation() -> SOP:
        """代码生成 SOP。"""
        return SOP(
            name="SOP-CodeGeneration-Seed",
            description="代码生成任务的标准操作流程",
            task_type="code_generation",
            steps=[
                "1. 理解需求：解析用户需求，确认输入/输出/约束",
                "2. 设计方案：确定架构、数据结构、函数签名",
                "3. 编写代码：按设计方案逐步实现",
                "4. 自检：检查语法、类型、边界条件",
                "5. 测试：编写并运行单元测试",
                "6. 交付：格式化代码，添加必要注释",
            ],
            decision_points=[
                DecisionPoint(
                    step_id=2,
                    description="选择技术方案和数据结构",
                    alternatives=["面向对象", "函数式", "混合方案"],
                    chosen_action="根据复杂度选择合适范式",
                    impact="high",
                ),
                DecisionPoint(
                    step_id=3,
                    description="选择实现策略",
                    alternatives=["从头实现", "使用现有库", "模板改造"],
                    chosen_action="评估后选择最高效的方案",
                    impact="medium",
                ),
            ],
            success_rate=1.0,
            avg_tokens=3000,
            avg_steps=6.0,
            avg_duration_ms=5000,
            metadata={"version": "1.0", "author": "seed"},
        )

    @staticmethod
    def _debugging() -> SOP:
        """调试 SOP。"""
        return SOP(
            name="SOP-Debugging-Seed",
            description="调试任务的标准操作流程",
            task_type="debugging",
            steps=[
                "1. 复现问题：确认错误信息和触发条件",
                "2. 定位根因：分析堆栈、日志、代码路径",
                "3. 最小复现：构造最小可复现用例",
                "4. 制定修复方案：评估修复影响范围",
                "5. 实施修复：编写修复代码",
                "6. 验证修复：运行测试确认问题解决且无回归",
            ],
            decision_points=[
                DecisionPoint(
                    step_id=2,
                    description="选择调试策略",
                    alternatives=["二分定位", "日志追踪", "断点调试", "代码审查"],
                    chosen_action="根据问题类型选择最高效的定位方式",
                    impact="high",
                ),
                DecisionPoint(
                    step_id=4,
                    description="选择修复方案",
                    alternatives=["局部修复", "重构相关模块", "临时绕过"],
                    chosen_action="评估风险后选择最稳妥的方案",
                    impact="high",
                ),
            ],
            success_rate=1.0,
            avg_tokens=2500,
            avg_steps=6.0,
            avg_duration_ms=4000,
            metadata={"version": "1.0", "author": "seed"},
        )

    @staticmethod
    def _data_analysis() -> SOP:
        """数据分析 SOP。"""
        return SOP(
            name="SOP-DataAnalysis-Seed",
            description="数据分析任务的标准操作流程",
            task_type="data_analysis",
            steps=[
                "1. 数据探索：了解数据结构、类型、量级",
                "2. 数据清洗：处理缺失值、异常值、格式问题",
                "3. 特征工程：提取/构造分析所需特征",
                "4. 分析计算：执行统计分析、聚合、建模",
                "5. 可视化：生成图表展示发现",
                "6. 撰写报告：总结发现并给出建议",
            ],
            decision_points=[
                DecisionPoint(
                    step_id=2,
                    description="选择缺失值处理策略",
                    alternatives=["删除", "均值填充", "中位数填充", "插值", "标记"],
                    chosen_action="根据缺失比例和数据分布选择",
                    impact="medium",
                ),
                DecisionPoint(
                    step_id=4,
                    description="选择分析方法",
                    alternatives=["描述性统计", "回归分析", "聚类", "时序分析"],
                    chosen_action="根据业务问题选择匹配的分析方法",
                    impact="high",
                ),
            ],
            success_rate=1.0,
            avg_tokens=4000,
            avg_steps=6.0,
            avg_duration_ms=6000,
            metadata={"version": "1.0", "author": "seed"},
        )

    @staticmethod
    def _api_integration() -> SOP:
        """API 集成 SOP。"""
        return SOP(
            name="SOP-APIIntegration-Seed",
            description="API 集成任务的标准操作流程",
            task_type="api_integration",
            steps=[
                "1. 阅读文档：理解 API 端点、认证方式、速率限制",
                "2. 环境准备：配置密钥、SDK、依赖",
                "3. 编写客户端：封装 API 调用、错误处理、重试逻辑",
                "4. 测试连通性：验证认证和基本调用",
                "5. 业务集成：将 API 调用嵌入业务流程",
                "6. 边界处理：实现超时、降级、限流策略",
            ],
            decision_points=[
                DecisionPoint(
                    step_id=1,
                    description="选择认证方式",
                    alternatives=["API Key", "OAuth2", "JWT", "Basic Auth"],
                    chosen_action="按 API 文档要求选择",
                    impact="medium",
                ),
                DecisionPoint(
                    step_id=3,
                    description="选择 HTTP 客户端和重试策略",
                    alternatives=["requests", "httpx", "aiohttp"],
                    chosen_action="根据同步/异步需求选择",
                    impact="medium",
                ),
            ],
            success_rate=1.0,
            avg_tokens=3500,
            avg_steps=6.0,
            avg_duration_ms=5500,
            metadata={"version": "1.0", "author": "seed"},
        )

    @staticmethod
    def _deployment() -> SOP:
        """部署 SOP。"""
        return SOP(
            name="SOP-Deployment-Seed",
            description="部署任务的标准操作流程",
            task_type="deployment",
            steps=[
                "1. 环境检查：确认目标环境配置和依赖",
                "2. 构建打包：生成部署产物（容器镜像/包）",
                "3. 配置管理：准备环境变量、配置文件",
                "4. 部署执行：推送产物到目标环境",
                "5. 健康检查：验证服务启动和基本功能",
                "6. 监控回滚：观察指标，准备回滚方案",
            ],
            decision_points=[
                DecisionPoint(
                    step_id=2,
                    description="选择部署方式",
                    alternatives=["容器化", "Serverless", "VM 直部署", "K8s"],
                    chosen_action="根据基础设施和规模选择",
                    impact="high",
                ),
                DecisionPoint(
                    step_id=4,
                    description="选择部署策略",
                    alternatives=["蓝绿部署", "滚动更新", "金丝雀发布", "直接替换"],
                    chosen_action="根据可用性要求选择",
                    impact="high",
                ),
            ],
            success_rate=1.0,
            avg_tokens=3000,
            avg_steps=6.0,
            avg_duration_ms=4500,
            metadata={"version": "1.0", "author": "seed"},
        )
