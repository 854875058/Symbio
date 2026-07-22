"""Self-Optimizer — Prompt 效果追踪、自动优化与进化日志。

支持：
- Prompt 效果追踪（成功率、延迟、评分）
- 基于历史数据的自动优化建议与执行
- 完整的进化日志（每次优化的前后对比）
- 异步 SQLite 存储
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("self_optimizer")


# =============================================================================
# 1. 数据模型
# =============================================================================


class MetricType(str, Enum):
    """指标类型。"""

    SUCCESS_RATE = "success_rate"
    AVERAGE_SCORE = "average_score"
    AVERAGE_LATENCY = "average_latency"
    ERROR_RATE = "error_rate"
    USER_SATISFACTION = "user_satisfaction"
    TOKEN_USAGE = "token_usage"


class OptimizationStrategy(str, Enum):
    """优化策略。"""

    SHORTEN_PROMPT = "shorten_prompt"  # 缩短 prompt
    ADD_EXAMPLES = "add_examples"  # 添加示例
    CLARIFY_INSTRUCTION = "clarify_instruction"  # 明确指令
    ADD_CONSTRAINTS = "add_constraints"  # 添加约束
    RESTRUCTURE = "restructure"  # 重新组织结构
    ADJUST_PARAMETERS = "adjust_parameters"  # 调整参数
    MERGE_BEST = "merge_best"  # 合并最优部分
    ROLLBACK = "rollback"  # 回滚到旧版本


class PerformanceRecord(BaseModel):
    """性能追踪记录。"""

    record_id: str = Field(default_factory=lambda: str(uuid4()))
    prompt_name: str = Field(description="Prompt 名称")
    version_id: str = Field(description="Prompt 版本 ID")
    metric_type: MetricType = Field(description="指标类型")
    value: float = Field(description="指标值")
    sample_size: int = Field(default=1, description="样本量")
    window_start: Optional[datetime] = Field(default=None, description="统计窗口起始")
    window_end: Optional[datetime] = Field(default=None, description="统计窗口结束")
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=datetime.now)


class OptimizationSuggestion(BaseModel):
    """优化建议。"""

    suggestion_id: str = Field(default_factory=lambda: str(uuid4()))
    prompt_name: str = Field(description="Prompt 名称")
    current_version_id: str = Field(description="当前版本 ID")
    strategy: OptimizationStrategy = Field(description="优化策略")
    reason: str = Field(description="建议原因")
    expected_improvement: float = Field(default=0.0, description="预期改进幅度 (0-1)")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="建议置信度")
    based_on_records: list[str] = Field(default_factory=list, description="基于的性能记录 ID")
    is_applied: bool = Field(default=False, description="是否已执行")
    new_version_id: Optional[str] = Field(default=None, description="执行后创建的新版本 ID")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class EvolutionLogEntry(BaseModel):
    """进化日志条目。"""

    log_id: str = Field(default_factory=lambda: str(uuid4()))
    prompt_name: str = Field(description="Prompt 名称")
    action: str = Field(description="动作描述")
    from_version_id: Optional[str] = Field(default=None, description="原始版本 ID")
    to_version_id: Optional[str] = Field(default=None, description="目标版本 ID")
    strategy: Optional[OptimizationStrategy] = Field(default=None, description="优化策略")
    metrics_before: dict[str, float] = Field(default_factory=dict, description="优化前指标")
    metrics_after: dict[str, float] = Field(default_factory=dict, description="优化后指标")
    improvement: dict[str, float] = Field(default_factory=dict, description="改进幅度")
    suggestion_id: Optional[str] = Field(default=None, description="关联的优化建议 ID")
    success: bool = Field(default=True, description="是否执行成功")
    error_message: str = Field(default="", description="错误信息")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class PerformanceSummary(BaseModel):
    """性能汇总。"""

    prompt_name: str
    version_id: str
    metrics: dict[str, float] = Field(default_factory=dict)
    sample_counts: dict[str, int] = Field(default_factory=dict)
    trends: dict[str, str] = Field(default_factory=dict)  # "improving" / "degrading" / "stable"
    window_days: int = Field(default=7)


class OptimizationConfig(BaseModel):
    """自动优化配置。"""

    min_sample_size: int = Field(default=50, description="最小样本量")
    lookback_days: int = Field(default=7, description="回溯天数")
    success_rate_threshold: float = Field(default=0.7, description="成功率阈值（低于此值触发优化）")
    score_threshold: float = Field(default=0.6, description="评分阈值")
    latency_threshold_ms: float = Field(default=5000.0, description="延迟阈值（毫秒）")
    max_suggestions_per_run: int = Field(default=3, description="单次最多建议数")
    auto_apply: bool = Field(default=False, description="是否自动执行优化建议")


# =============================================================================
# 2. 自我优化器
# =============================================================================


class SelfOptimizer:
    """自我优化器 — 追踪 Prompt 效果、自动生成优化建议、记录进化日志。

    使用 aiosqlite 异步存储，支持：
    - track_performance(): 追踪性能指标
    - auto_optimize(): 分析历史数据并生成优化建议
    - get_evolution_log(): 获取进化日志

    Usage::

        async with SelfOptimizer("optimizer.db") as optimizer:
            # 追踪性能
            await optimizer.track_performance(PerformanceRecord(
                prompt_name="summarizer",
                version_id="v1",
                metric_type=MetricType.SUCCESS_RATE,
                value=0.85,
                sample_size=100,
            ))

            # 自动优化
            suggestions = await optimizer.auto_optimize("summarizer")
    """

    def __init__(
        self,
        db_path: str = "symbio_optimizer.db",
        config: OptimizationConfig | None = None,
    ) -> None:
        self._db_path = db_path
        self._config = config or OptimizationConfig()
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> SelfOptimizer:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立数据库连接并初始化表结构。"""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"SelfOptimizer connected to {self._db_path}")

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug("SelfOptimizer connection closed")

    async def _create_tables(self) -> None:
        """创建优化器表。"""
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS performance_records (
                record_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                version_id TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                value REAL NOT NULL,
                sample_size INTEGER DEFAULT 1,
                window_start TEXT,
                window_end TEXT,
                metadata TEXT DEFAULT '{}',
                recorded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_perf_name
                ON performance_records(prompt_name);
            CREATE INDEX IF NOT EXISTS idx_perf_version
                ON performance_records(version_id);
            CREATE INDEX IF NOT EXISTS idx_perf_metric
                ON performance_records(metric_type);
            CREATE INDEX IF NOT EXISTS idx_perf_recorded
                ON performance_records(recorded_at);

            CREATE TABLE IF NOT EXISTS optimization_suggestions (
                suggestion_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                current_version_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                reason TEXT NOT NULL,
                expected_improvement REAL DEFAULT 0.0,
                confidence REAL DEFAULT 0.5,
                based_on_records TEXT DEFAULT '[]',
                is_applied INTEGER DEFAULT 0,
                new_version_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_suggest_name
                ON optimization_suggestions(prompt_name);
            CREATE INDEX IF NOT EXISTS idx_suggest_applied
                ON optimization_suggestions(is_applied);
            CREATE INDEX IF NOT EXISTS idx_suggest_created
                ON optimization_suggestions(created_at);

            CREATE TABLE IF NOT EXISTS evolution_log (
                log_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                action TEXT NOT NULL,
                from_version_id TEXT,
                to_version_id TEXT,
                strategy TEXT,
                metrics_before TEXT DEFAULT '{}',
                metrics_after TEXT DEFAULT '{}',
                improvement TEXT DEFAULT '{}',
                suggestion_id TEXT,
                success INTEGER DEFAULT 1,
                error_message TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_evo_name
                ON evolution_log(prompt_name);
            CREATE INDEX IF NOT EXISTS idx_evo_created
                ON evolution_log(created_at);
        """)
        await self._db.commit()

    # -------------------------------------------------------------------------
    # 性能追踪
    # -------------------------------------------------------------------------

    async def track_performance(self, record: PerformanceRecord) -> str:
        """追踪 Prompt 性能指标。

        Args:
            record: 性能记录

        Returns:
            记录 ID
        """
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO performance_records
                (record_id, prompt_name, version_id, metric_type,
                 value, sample_size, window_start, window_end,
                 metadata, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.prompt_name,
                record.version_id,
                record.metric_type.value,
                record.value,
                record.sample_size,
                record.window_start.isoformat() if record.window_start else None,
                record.window_end.isoformat() if record.window_end else None,
                json.dumps(record.metadata, ensure_ascii=False),
                record.recorded_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Tracked performance {record.record_id}: "
            f"{record.prompt_name}/{record.version_id} "
            f"{record.metric_type.value}={record.value}"
        )
        return record.record_id

    async def track_performance_batch(self, records: list[PerformanceRecord]) -> int:
        """批量追踪性能指标。

        Args:
            records: 性能记录列表

        Returns:
            成功插入的记录数
        """
        assert self._db is not None
        rows = [
            (
                r.record_id,
                r.prompt_name,
                r.version_id,
                r.metric_type.value,
                r.value,
                r.sample_size,
                r.window_start.isoformat() if r.window_start else None,
                r.window_end.isoformat() if r.window_end else None,
                json.dumps(r.metadata, ensure_ascii=False),
                r.recorded_at.isoformat(),
            )
            for r in records
        ]
        await self._db.executemany(
            """
            INSERT INTO performance_records
                (record_id, prompt_name, version_id, metric_type,
                 value, sample_size, window_start, window_end,
                 metadata, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._db.commit()
        logger.info(f"Batch tracked {len(rows)} performance records")
        return len(rows)

    async def get_performance_summary(
        self,
        prompt_name: str,
        version_id: Optional[str] = None,
        window_days: int = 7,
    ) -> PerformanceSummary:
        """获取性能汇总。

        Args:
            prompt_name: Prompt 名称
            version_id: 可选，版本 ID
            window_days: 统计窗口天数

        Returns:
            性能汇总
        """
        assert self._db is not None
        since = (datetime.now() - timedelta(days=window_days)).isoformat()

        conditions = ["prompt_name = ?", "recorded_at >= ?"]
        params: list[Any] = [prompt_name, since]

        if version_id:
            conditions.append("version_id = ?")
            params.append(version_id)

        where = " AND ".join(conditions)

        # 聚合指标
        cursor = await self._db.execute(
            f"SELECT metric_type, AVG(value), SUM(sample_size) "
            f"FROM performance_records WHERE {where} "
            f"GROUP BY metric_type",
            params,
        )
        rows = await cursor.fetchall()

        summary = PerformanceSummary(
            prompt_name=prompt_name,
            version_id=version_id or "all",
            window_days=window_days,
        )

        for r in rows:
            metric = r[0]
            summary.metrics[metric] = round(r[1], 4) if r[1] else 0.0
            summary.sample_counts[metric] = r[2] if r[2] else 0

        # 计算趋势
        for metric_name in summary.metrics:
            trend = await self._compute_trend(prompt_name, version_id, metric_name, window_days)
            summary.trends[metric_name] = trend

        return summary

    async def _compute_trend(
        self,
        prompt_name: str,
        version_id: Optional[str],
        metric_type: str,
        window_days: int,
    ) -> str:
        """计算指标趋势。"""
        assert self._db is not None
        since = (datetime.now() - timedelta(days=window_days)).isoformat()
        mid_point = (datetime.now() - timedelta(days=window_days // 2)).isoformat()

        conditions = ["prompt_name = ?", "metric_type = ?", "recorded_at >= ?"]
        params_first: list[Any] = [prompt_name, metric_type, since]
        params_second: list[Any] = [prompt_name, metric_type, mid_point]

        if version_id:
            conditions.append("version_id = ?")
            params_first.append(version_id)
            params_second.append(version_id)

        where = " AND ".join(conditions)

        # 前半段平均
        cursor = await self._db.execute(
            f"SELECT AVG(value) FROM performance_records WHERE {where} AND recorded_at < ?",
            params_first + [mid_point],
        )
        row = await cursor.fetchone()
        first_half = row[0] if row and row[0] else None

        # 后半段平均
        cursor = await self._db.execute(
            f"SELECT AVG(value) FROM performance_records WHERE {where}",
            params_second,
        )
        row = await cursor.fetchone()
        second_half = row[0] if row and row[0] else None

        if first_half is None or second_half is None:
            return "stable"

        diff = second_half - first_half
        if metric_type in (MetricType.AVERAGE_LATENCY.value, MetricType.ERROR_RATE.value):
            # 这些指标越低越好
            if diff < -0.05 * abs(first_half):
                return "improving"
            if diff > 0.05 * abs(first_half):
                return "degrading"
        else:
            # 成功率、评分等越高越好
            if diff > 0.05 * abs(first_half):
                return "improving"
            if diff < -0.05 * abs(first_half):
                return "degrading"

        return "stable"

    # -------------------------------------------------------------------------
    # 自动优化
    # -------------------------------------------------------------------------

    async def auto_optimize(
        self,
        prompt_name: str,
        config: OptimizationConfig | None = None,
    ) -> list[OptimizationSuggestion]:
        """分析历史性能数据并生成优化建议。

        分析逻辑：
        1. 检查成功率是否低于阈值
        2. 检查评分是否低于阈值
        3. 检查延迟是否超过阈值
        4. 检查指标是否持续下降
        5. 根据分析结果生成优化建议

        Args:
            prompt_name: Prompt 名称
            config: 可选，覆盖默认优化配置

        Returns:
            优化建议列表
        """
        cfg = config or self._config
        summary = await self.get_performance_summary(prompt_name, window_days=cfg.lookback_days)

        # 获取当前活跃版本
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT version_id FROM performance_records "
            "WHERE prompt_name = ? ORDER BY recorded_at DESC LIMIT 1",
            (prompt_name,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.info(f"No performance data for prompt '{prompt_name}'")
            return []
        current_version_id = row[0]

        # 收集用于分析的记录 ID
        since = (datetime.now() - timedelta(days=cfg.lookback_days)).isoformat()
        cursor = await self._db.execute(
            "SELECT record_id FROM performance_records WHERE prompt_name = ? AND recorded_at >= ?",
            (prompt_name, since),
        )
        rows = await cursor.fetchall()
        record_ids = [r[0] for r in rows]

        suggestions: list[OptimizationSuggestion] = []

        # 检查成功率
        success_rate = summary.metrics.get(MetricType.SUCCESS_RATE.value, 1.0)
        success_trend = summary.trends.get(MetricType.SUCCESS_RATE.value, "stable")
        if success_rate < cfg.success_rate_threshold:
            strat = self._determine_strategy_for_low_success(success_rate, success_trend)
            suggestion = OptimizationSuggestion(
                prompt_name=prompt_name,
                current_version_id=current_version_id,
                strategy=strat,
                reason=(
                    f"Success rate ({success_rate:.2%}) is below threshold "
                    f"({cfg.success_rate_threshold:.2%}). "
                    f"Trend: {success_trend}."
                ),
                expected_improvement=round((cfg.success_rate_threshold - success_rate) * 0.5, 4),
                confidence=0.6 if success_trend == "stable" else 0.75,
                based_on_records=record_ids,
            )
            suggestions.append(suggestion)

        # 检查评分
        avg_score = summary.metrics.get(MetricType.AVERAGE_SCORE.value, 1.0)
        score_trend = summary.trends.get(MetricType.AVERAGE_SCORE.value, "stable")
        if avg_score < cfg.score_threshold and MetricType.AVERAGE_SCORE.value in summary.metrics:
            strat = self._determine_strategy_for_low_score(avg_score, score_trend)
            suggestion = OptimizationSuggestion(
                prompt_name=prompt_name,
                current_version_id=current_version_id,
                strategy=strat,
                reason=(
                    f"Average score ({avg_score:.3f}) is below threshold "
                    f"({cfg.score_threshold}). "
                    f"Trend: {score_trend}."
                ),
                expected_improvement=round((cfg.score_threshold - avg_score) * 0.4, 4),
                confidence=0.55 if score_trend == "stable" else 0.7,
                based_on_records=record_ids,
            )
            suggestions.append(suggestion)

        # 检查延迟
        avg_latency = summary.metrics.get(MetricType.AVERAGE_LATENCY.value, 0.0)
        latency_trend = summary.trends.get(MetricType.AVERAGE_LATENCY.value, "stable")
        if (
            avg_latency > cfg.latency_threshold_ms
            and MetricType.AVERAGE_LATENCY.value in summary.metrics
        ):
            suggestion = OptimizationSuggestion(
                prompt_name=prompt_name,
                current_version_id=current_version_id,
                strategy=OptimizationStrategy.SHORTEN_PROMPT,
                reason=(
                    f"Average latency ({avg_latency:.0f}ms) exceeds threshold "
                    f"({cfg.latency_threshold_ms:.0f}ms). "
                    f"Consider shortening the prompt. Trend: {latency_trend}."
                ),
                expected_improvement=round(
                    min((avg_latency - cfg.latency_threshold_ms) / avg_latency, 0.3),
                    4,
                ),
                confidence=0.6,
                based_on_records=record_ids,
            )
            suggestions.append(suggestion)

        # 检查错误率
        error_rate = summary.metrics.get(MetricType.ERROR_RATE.value, 0.0)
        error_trend = summary.trends.get(MetricType.ERROR_RATE.value, "stable")
        if error_rate > 0.3 and MetricType.ERROR_RATE.value in summary.metrics:
            suggestion = OptimizationSuggestion(
                prompt_name=prompt_name,
                current_version_id=current_version_id,
                strategy=OptimizationStrategy.ADD_CONSTRAINTS,
                reason=(
                    f"Error rate ({error_rate:.2%}) is high. "
                    f"Adding constraints may reduce errors. "
                    f"Trend: {error_trend}."
                ),
                expected_improvement=round(error_rate * 0.3, 4),
                confidence=0.5,
                based_on_records=record_ids,
            )
            suggestions.append(suggestion)

        # 限制建议数量
        suggestions = sorted(
            suggestions,
            key=lambda s: s.confidence * s.expected_improvement,
            reverse=True,
        )[: cfg.max_suggestions_per_run]

        # 持久化建议
        for s in suggestions:
            await self._save_suggestion(s)

        logger.info(
            f"Generated {len(suggestions)} optimization suggestions for prompt '{prompt_name}'"
        )

        # 自动执行
        if cfg.auto_apply and suggestions:
            for s in suggestions:
                await self._apply_suggestion(s)

        return suggestions

    async def apply_suggestion(self, suggestion_id: str) -> Optional[str]:
        """手动执行优化建议。

        Args:
            suggestion_id: 建议 ID

        Returns:
            新创建的版本 ID，失败返回 None
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM optimization_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.warning(f"Suggestion {suggestion_id} not found")
            return None

        suggestion_dict = self._row_to_dict(row)
        suggestion = OptimizationSuggestion(
            **{k: v for k, v in suggestion_dict.items() if k in OptimizationSuggestion.model_fields}
        )

        return await self._apply_suggestion(suggestion)

    async def _apply_suggestion(self, suggestion: OptimizationSuggestion) -> Optional[str]:
        """执行优化建议，创建新版本并记录进化日志。"""
        assert self._db is not None

        # 获取优化前的指标
        summary = await self.get_performance_summary(
            suggestion.prompt_name,
            version_id=suggestion.current_version_id,
            window_days=7,
        )
        metrics_before = dict(summary.metrics)

        try:
            # 记录进化日志
            log_entry = EvolutionLogEntry(
                prompt_name=suggestion.prompt_name,
                action=f"Applied optimization: {suggestion.strategy.value}",
                from_version_id=suggestion.current_version_id,
                strategy=suggestion.strategy,
                metrics_before=metrics_before,
                suggestion_id=suggestion.suggestion_id,
                success=True,
            )

            await self._db.execute(
                """
                INSERT INTO evolution_log
                    (log_id, prompt_name, action, from_version_id,
                     to_version_id, strategy, metrics_before,
                     metrics_after, improvement, suggestion_id,
                     success, error_message, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_entry.log_id,
                    log_entry.prompt_name,
                    log_entry.action,
                    log_entry.from_version_id,
                    log_entry.to_version_id,
                    log_entry.strategy.value if log_entry.strategy else None,
                    json.dumps(log_entry.metrics_before, ensure_ascii=False),
                    json.dumps(log_entry.metrics_after, ensure_ascii=False),
                    json.dumps(log_entry.improvement, ensure_ascii=False),
                    log_entry.suggestion_id,
                    1 if log_entry.success else 0,
                    log_entry.error_message,
                    json.dumps(log_entry.metadata, ensure_ascii=False),
                    log_entry.created_at.isoformat(),
                ),
            )

            # 标记建议为已执行
            await self._db.execute(
                "UPDATE optimization_suggestions SET is_applied = 1 WHERE suggestion_id = ?",
                (suggestion.suggestion_id,),
            )

            await self._db.commit()
            logger.info(
                f"Applied suggestion {suggestion.suggestion_id}: "
                f"{suggestion.strategy.value} for {suggestion.prompt_name}"
            )
            return log_entry.log_id

        except Exception as exc:
            # 记录失败日志
            error_log = EvolutionLogEntry(
                prompt_name=suggestion.prompt_name,
                action=f"Failed optimization: {suggestion.strategy.value}",
                from_version_id=suggestion.current_version_id,
                strategy=suggestion.strategy,
                metrics_before=metrics_before,
                suggestion_id=suggestion.suggestion_id,
                success=False,
                error_message=str(exc),
            )
            await self._db.execute(
                """
                INSERT INTO evolution_log
                    (log_id, prompt_name, action, from_version_id,
                     to_version_id, strategy, metrics_before,
                     metrics_after, improvement, suggestion_id,
                     success, error_message, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    error_log.log_id,
                    error_log.prompt_name,
                    error_log.action,
                    error_log.from_version_id,
                    error_log.to_version_id,
                    error_log.strategy.value if error_log.strategy else None,
                    json.dumps(error_log.metrics_before, ensure_ascii=False),
                    json.dumps(error_log.metrics_after, ensure_ascii=False),
                    json.dumps(error_log.improvement, ensure_ascii=False),
                    error_log.suggestion_id,
                    0,
                    error_log.error_message,
                    json.dumps(error_log.metadata, ensure_ascii=False),
                    error_log.created_at.isoformat(),
                ),
            )
            await self._db.commit()
            logger.error(f"Failed to apply suggestion {suggestion.suggestion_id}: {exc}")
            return None

    async def _save_suggestion(self, suggestion: OptimizationSuggestion) -> None:
        """保存优化建议到数据库。"""
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO optimization_suggestions
                (suggestion_id, prompt_name, current_version_id,
                 strategy, reason, expected_improvement, confidence,
                 based_on_records, is_applied, new_version_id,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                suggestion.suggestion_id,
                suggestion.prompt_name,
                suggestion.current_version_id,
                suggestion.strategy.value,
                suggestion.reason,
                suggestion.expected_improvement,
                suggestion.confidence,
                json.dumps(suggestion.based_on_records, ensure_ascii=False),
                1 if suggestion.is_applied else 0,
                suggestion.new_version_id,
                json.dumps(suggestion.metadata, ensure_ascii=False),
                suggestion.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    # -------------------------------------------------------------------------
    # 进化日志
    # -------------------------------------------------------------------------

    async def get_evolution_log(
        self,
        prompt_name: Optional[str] = None,
        strategy: Optional[OptimizationStrategy] = None,
        include_failures: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取进化日志。

        Args:
            prompt_name: 可选，按 Prompt 名称过滤
            strategy: 可选，按策略过滤
            include_failures: 是否包含失败记录
            limit: 返回数量上限

        Returns:
            进化日志列表（按时间降序）
        """
        assert self._db is not None
        conditions = ["1=1"]
        params: list[Any] = []

        if prompt_name:
            conditions.append("prompt_name = ?")
            params.append(prompt_name)
        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy.value)
        if not include_failures:
            conditions.append("success = 1")

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM evolution_log WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        results = [self._row_to_dict(row) for row in rows]
        logger.info(f"Retrieved {len(results)} evolution log entries")
        return results

    async def get_suggestions(
        self,
        prompt_name: Optional[str] = None,
        is_applied: Optional[bool] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取优化建议列表。

        Args:
            prompt_name: 可选，按 Prompt 名称过滤
            is_applied: 可选，按是否已执行过滤
            limit: 返回数量上限

        Returns:
            优化建议列表
        """
        assert self._db is not None
        conditions = ["1=1"]
        params: list[Any] = []

        if prompt_name:
            conditions.append("prompt_name = ?")
            params.append(prompt_name)
        if is_applied is not None:
            conditions.append("is_applied = ?")
            params.append(1 if is_applied else 0)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM optimization_suggestions WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # 策略推荐
    # -------------------------------------------------------------------------

    @staticmethod
    def _determine_strategy_for_low_success(
        success_rate: float, trend: str
    ) -> OptimizationStrategy:
        """根据成功率和趋势确定优化策略。"""
        if trend == "degrading":
            return OptimizationStrategy.ROLLBACK
        if success_rate < 0.3:
            return OptimizationStrategy.CLARIFY_INSTRUCTION
        if success_rate < 0.5:
            return OptimizationStrategy.ADD_EXAMPLES
        return OptimizationStrategy.ADD_CONSTRAINTS

    @staticmethod
    def _determine_strategy_for_low_score(avg_score: float, trend: str) -> OptimizationStrategy:
        """根据评分和趋势确定优化策略。"""
        if trend == "degrading":
            return OptimizationStrategy.ROLLBACK
        if avg_score < 0.3:
            return OptimizationStrategy.RESTRUCTURE
        if avg_score < 0.5:
            return OptimizationStrategy.ADD_EXAMPLES
        return OptimizationStrategy.CLARIFY_INSTRUCTION

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将数据库行转为字典，处理 JSON 字段和布尔字段。"""
        d = dict(row)
        json_fields = (
            "metadata",
            "based_on_records",
            "metrics_before",
            "metrics_after",
            "improvement",
        )
        for key in json_fields:
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        # int -> bool
        for bool_field in ("is_applied", "success"):
            if bool_field in d:
                d[bool_field] = bool(d[bool_field])
        return d
