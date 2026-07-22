"""成本监控仪表盘与预算管理模块。

提供三大核心能力：
1. CostTracker  - 逐任务/Agent/模型的 Token 消耗追踪，SQLite 持久化
2. BudgetManager - 项目级月度 Token 预算管理，超阈值自动降级模型
3. CacheKeepAlive - Prompt Cache 保活 ping（每 4 分钟，~10 tokens/次）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("cost_monitor")

# ---------------------------------------------------------------------------
# 默认数据库路径
# ---------------------------------------------------------------------------

_DEFAULT_DB_DIR = Path.home() / ".symbio" / "data"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "cost_monitor.db"


# ---------------------------------------------------------------------------
# 模型降级映射
# ---------------------------------------------------------------------------

# Sonnet -> Haiku 降级表（key 为高成本模型，value 为低成本替代）
_DOWNGRADE_MAP: dict[str, str] = {
    "claude-sonnet-4-20250514": "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022": "claude-3-5-haiku-20241022",
    "claude-3-sonnet-20240229": "claude-3-haiku-20240307",
    # Opus -> Sonnet 降级
    "claude-opus-4-20250514": "claude-sonnet-4-20250514",
    "claude-3-opus-20240229": "claude-3-5-sonnet-20241022",
}

# 降级链的终点（不可再降级的模型）
_TERMINAL_MODELS = {"claude-3-5-haiku-20241022", "claude-3-haiku-20240307"}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class UsageRecord(BaseModel):
    """单次 Token 使用记录"""

    record_id: str = ""
    task_id: str
    agent_name: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AgentCost(BaseModel):
    """Agent 维度的成本汇总"""

    agent_name: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    models_used: list[str] = Field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""


class ModelCost(BaseModel):
    """模型维度的成本汇总"""

    model: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    agents_using: list[str] = Field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""


class CostSummary(BaseModel):
    """时间窗口内的成本摘要"""

    period_hours: int = 24
    start_time: str = ""
    end_time: str = ""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_requests: int = 0

    unique_agents: int = 0
    unique_models: int = 0
    unique_tasks: int = 0

    top_agent: str = ""
    top_model: str = ""

    agents: list[AgentCost] = Field(default_factory=list)
    models: list[ModelCost] = Field(default_factory=list)


class BudgetStatus(BaseModel):
    """项目预算状态"""

    project_id: str
    monthly_limit_tokens: int = 0
    consumed_tokens: int = 0
    remaining_tokens: int = 0
    percentage_used: float = 0.0  # 0.0 ~ 1.0
    is_exceeded: bool = False
    should_downgrade: bool = False  # usage > 80%
    downgrade_model: str = ""  # 建议降级到的模型
    period_start: str = ""  # 本月起始时间
    period_end: str = ""  # 本月结束时间
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Token 消耗追踪器，持久化到 SQLite。

    支持按任务、Agent、模型三个维度记录和查询 Token 使用量。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db: Optional[aiosqlite.Connection] = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """确保数据库连接已建立，表已创建。"""
        if self._db is not None:
            return self._db

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS usage_records (
                record_id   TEXT PRIMARY KEY,
                task_id     TEXT NOT NULL,
                agent_name  TEXT NOT NULL,
                model       TEXT NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens  INTEGER NOT NULL DEFAULT 0,
                timestamp   TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_task
            ON usage_records(task_id)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_agent
            ON usage_records(agent_name)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_model
            ON usage_records(model)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp
            ON usage_records(timestamp)
        """)
        await self._db.commit()
        return self._db

    async def record_usage(
        self,
        task_id: str,
        agent_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> UsageRecord:
        """记录一次 Token 使用。

        Args:
            task_id: 任务 ID。
            agent_name: Agent 名称。
            model: 模型标识。
            input_tokens: 输入 Token 数。
            output_tokens: 输出 Token 数。

        Returns:
            写入的 UsageRecord 实例。
        """
        db = await self._ensure_db()
        record = UsageRecord(
            record_id=str(uuid4()),
            task_id=task_id,
            agent_name=agent_name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await db.execute(
            """
            INSERT INTO usage_records
                (record_id, task_id, agent_name, model, input_tokens, output_tokens, total_tokens, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.task_id,
                record.agent_name,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.total_tokens,
                record.timestamp,
            ),
        )
        await db.commit()
        logger.debug(
            "记录 Token 使用: task={} agent={} model={} tokens={}",
            task_id,
            agent_name,
            model,
            record.total_tokens,
        )
        return record

    async def get_summary(self, period_hours: int = 24) -> CostSummary:
        """获取指定时间窗口内的成本摘要。

        Args:
            period_hours: 回溯小时数，默认 24。

        Returns:
            CostSummary 实例。
        """
        db = await self._ensure_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=period_hours)).isoformat()

        # 总量
        cursor = await db.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0)  AS total_input,
                COALESCE(SUM(output_tokens), 0) AS total_output,
                COALESCE(SUM(total_tokens), 0)  AS total,
                COUNT(*)                        AS req_count
            FROM usage_records
            WHERE timestamp >= ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()

        # 按 Agent 分组
        cursor = await db.execute(
            """
            SELECT
                agent_name,
                SUM(input_tokens)  AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(total_tokens)  AS total,
                COUNT(*)           AS req_count,
                GROUP_CONCAT(DISTINCT model) AS models_csv,
                MIN(timestamp)     AS first_seen,
                MAX(timestamp)     AS last_seen
            FROM usage_records
            WHERE timestamp >= ?
            GROUP BY agent_name
            ORDER BY total DESC
            """,
            (cutoff,),
        )
        agent_rows = await cursor.fetchall()

        # 按模型分组
        cursor = await db.execute(
            """
            SELECT
                model,
                SUM(input_tokens)  AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(total_tokens)  AS total,
                COUNT(*)           AS req_count,
                GROUP_CONCAT(DISTINCT agent_name) AS agents_csv,
                MIN(timestamp)     AS first_seen,
                MAX(timestamp)     AS last_seen
            FROM usage_records
            WHERE timestamp >= ?
            GROUP BY model
            ORDER BY total DESC
            """,
            (cutoff,),
        )
        model_rows = await cursor.fetchall()

        # 唯一计数
        cursor = await db.execute(
            """
            SELECT
                COUNT(DISTINCT agent_name) AS agents,
                COUNT(DISTINCT model)      AS models,
                COUNT(DISTINCT task_id)    AS tasks
            FROM usage_records
            WHERE timestamp >= ?
            """,
            (cutoff,),
        )
        distinct = await cursor.fetchone()

        agents = [
            AgentCost(
                agent_name=r["agent_name"],
                total_input_tokens=r["total_input"],
                total_output_tokens=r["total_output"],
                total_tokens=r["total"],
                request_count=r["req_count"],
                models_used=r["models_csv"].split(",") if r["models_csv"] else [],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
            )
            for r in agent_rows
        ]

        models = [
            ModelCost(
                model=r["model"],
                total_input_tokens=r["total_input"],
                total_output_tokens=r["total_output"],
                total_tokens=r["total"],
                request_count=r["req_count"],
                agents_using=r["agents_csv"].split(",") if r["agents_csv"] else [],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
            )
            for r in model_rows
        ]

        now = datetime.now(timezone.utc)
        return CostSummary(
            period_hours=period_hours,
            start_time=cutoff,
            end_time=now.isoformat(),
            total_input_tokens=row["total_input"],
            total_output_tokens=row["total_output"],
            total_tokens=row["total"],
            total_requests=row["req_count"],
            unique_agents=distinct["agents"],
            unique_models=distinct["models"],
            unique_tasks=distinct["tasks"],
            top_agent=agents[0].agent_name if agents else "",
            top_model=models[0].model if models else "",
            agents=agents,
            models=models,
        )

    async def get_by_agent(self, period_hours: int = 24) -> dict[str, AgentCost]:
        """按 Agent 分组返回成本。"""
        summary = await self.get_summary(period_hours)
        return {a.agent_name: a for a in summary.agents}

    async def get_by_model(self, period_hours: int = 24) -> dict[str, ModelCost]:
        """按模型分组返回成本。"""
        summary = await self.get_summary(period_hours)
        return {m.model: m for m in summary.models}

    async def get_task_total(self, task_id: str) -> int:
        """获取单个任务的总 Token 消耗。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM usage_records WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return row["total"]

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None


# ---------------------------------------------------------------------------
# BudgetManager
# ---------------------------------------------------------------------------


class BudgetManager:
    """项目级月度 Token 预算管理。

    功能：
    - 设置/查询项目月度 Token 预算
    - 使用率 > 80% 时建议降级模型（Sonnet -> Haiku）
    - 使用率 > 100% 时标记超出
    """

    def __init__(self, cost_tracker: CostTracker, db_path: str | Path | None = None) -> None:
        self._tracker = cost_tracker
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db: Optional[aiosqlite.Connection] = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """确保数据库连接已建立，预算表已创建。"""
        if self._db is not None:
            return self._db

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS project_budgets (
                project_id          TEXT PRIMARY KEY,
                monthly_limit_tokens INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await self._db.commit()
        return self._db

    async def set_budget(self, project_id: str, monthly_limit_tokens: int) -> None:
        """设置或更新项目月度 Token 预算。

        Args:
            project_id: 项目标识。
            monthly_limit_tokens: 月度 Token 上限（0 表示不限）。
        """
        db = await self._ensure_db()
        await db.execute(
            """
            INSERT INTO project_budgets (project_id, monthly_limit_tokens, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(project_id) DO UPDATE SET
                monthly_limit_tokens = excluded.monthly_limit_tokens,
                updated_at = datetime('now')
            """,
            (project_id, monthly_limit_tokens),
        )
        await db.commit()
        logger.info("项目 {} 月度预算设置为 {} tokens", project_id, monthly_limit_tokens)

    async def get_budget_limit(self, project_id: str) -> int:
        """获取项目的月度 Token 预算。未设置时返回 0（不限）。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT monthly_limit_tokens FROM project_budgets WHERE project_id = ?",
            (project_id,),
        )
        row = await cursor.fetchone()
        return row["monthly_limit_tokens"] if row else 0

    async def check_budget(self, project_id: str) -> BudgetStatus:
        """检查项目预算使用情况。

        统计当月已消耗的 Token 总量，计算使用比例。

        Args:
            project_id: 项目标识（对应 task_id 前缀或独立标识）。

        Returns:
            BudgetStatus 实例。
        """
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 下月第一天
        if now.month == 12:
            month_end = now.replace(
                year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            month_end = now.replace(
                month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
            )

        limit = await self.get_budget_limit(project_id)

        # 统计当月消耗：匹配 task_id 以 project_id 开头的记录
        db_tracker = await self._tracker._ensure_db()
        cursor = await db_tracker.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS consumed
            FROM usage_records
            WHERE task_id LIKE ? AND timestamp >= ?
            """,
            (f"{project_id}%", month_start.isoformat()),
        )
        row = await cursor.fetchone()
        consumed = row["consumed"]

        remaining = max(0, limit - consumed) if limit > 0 else -1  # -1 表示无限制
        pct = (consumed / limit) if limit > 0 else 0.0

        # 确定是否应降级及降级目标
        should_down = pct >= 0.80 and limit > 0
        downgrade_target = ""
        if should_down:
            # 查找最常用的模型并给出降级建议
            cursor = await db_tracker.execute(
                """
                SELECT model, SUM(total_tokens) AS total
                FROM usage_records
                WHERE task_id LIKE ? AND timestamp >= ?
                GROUP BY model
                ORDER BY total DESC
                LIMIT 1
                """,
                (f"{project_id}%", month_start.isoformat()),
            )
            top_model_row = await cursor.fetchone()
            if top_model_row and top_model_row["model"] in _DOWNGRADE_MAP:
                downgrade_target = _DOWNGRADE_MAP[top_model_row["model"]]

        return BudgetStatus(
            project_id=project_id,
            monthly_limit_tokens=limit,
            consumed_tokens=consumed,
            remaining_tokens=remaining,
            percentage_used=pct,
            is_exceeded=pct >= 1.0 and limit > 0,
            should_downgrade=should_down,
            downgrade_model=downgrade_target,
            period_start=month_start.isoformat(),
            period_end=month_end.isoformat(),
        )

    async def should_downgrade(self, project_id: str) -> bool:
        """判断是否应降级模型。

        当月使用率超过 80% 时返回 True。

        Args:
            project_id: 项目标识。

        Returns:
            是否应降级。
        """
        status = await self.check_budget(project_id)
        return status.should_downgrade

    async def get_downgrade_model(self, current_model: str, project_id: str) -> str:
        """获取降级后的模型。

        若未超阈值或模型不可降级，返回原模型。

        Args:
            current_model: 当前模型标识。
            project_id: 项目标识。

        Returns:
            降级后的模型标识，或原模型。
        """
        if not await self.should_downgrade(project_id):
            return current_model
        return _DOWNGRADE_MAP.get(current_model, current_model)

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None


# ---------------------------------------------------------------------------
# CacheKeepAlive
# ---------------------------------------------------------------------------


class CacheKeepAlive:
    """Prompt Cache 保活服务。

    每 4 分钟发送一次最小 Token ping（~10 tokens），
    防止 Anthropic Prompt Cache 过期（默认 TTL 5 分钟）。

    用法::

        keepalive = CacheKeepAlive(send_fn=my_send_fn)
        keepalive.start()
        # ... 业务运行中 ...
        keepalive.stop()
    """

    # ping 间隔（秒），4 分钟 = 240 秒
    PING_INTERVAL_SECONDS: float = 240.0

    # 最小 ping prompt
    _PING_PROMPT = "ping"

    def __init__(
        self,
        send_fn: Any | None = None,
        cache_prefix: str = "",
        interval_seconds: float | None = None,
    ) -> None:
        """
        Args:
            send_fn: 异步发送函数 ``async def send_fn(prompt, model, max_tokens)``,
                     由外部注入实际的 API 调用。若为 None 则仅记录日志。
            cache_prefix: 需要保活的缓存前缀标识（用于日志）。
            interval_seconds: 自定义 ping 间隔（秒），默认 240。
        """
        self._send_fn = send_fn
        self._cache_prefix = cache_prefix
        self._interval = interval_seconds or self.PING_INTERVAL_SECONDS
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._ping_count: int = 0

    @property
    def is_running(self) -> bool:
        """保活任务是否正在运行。"""
        return self._task is not None and not self._task.done()

    @property
    def ping_count(self) -> int:
        """已发送的 ping 次数。"""
        return self._ping_count

    def start(self) -> None:
        """启动保活后台任务。"""
        if self.is_running:
            logger.warning("CacheKeepAlive 已在运行中，忽略重复启动")
            return

        self._stop_event.clear()
        self._task = asyncio.ensure_future(self._run_loop())
        logger.info(
            "CacheKeepAlive 已启动 (间隔={}s, prefix={})",
            self._interval,
            self._cache_prefix or "(default)",
        )

    def stop(self) -> None:
        """停止保活后台任务。"""
        if not self.is_running:
            return

        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("CacheKeepAlive 已停止 (共发送 {} 次 ping)", self._ping_count)

    async def _run_loop(self) -> None:
        """保活主循环。"""
        while not self._stop_event.is_set():
            try:
                await self._ping()
                self._ping_count += 1
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("CacheKeepAlive ping 异常")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # 如果 wait 返回说明 stop_event 被设置了
                break
            except asyncio.TimeoutError:
                # 超时 = 间隔到了，继续下一次 ping
                continue

    async def _ping(self) -> None:
        """执行一次 ping。"""
        if self._send_fn is not None:
            try:
                await self._send_fn(
                    prompt=self._PING_PROMPT,
                    model="claude-3-5-haiku-20241022",
                    max_tokens=1,
                )
                logger.debug("CacheKeepAlive ping #{} 已发送", self._ping_count + 1)
            except Exception:
                logger.warning("CacheKeepAlive ping 发送失败", exc_info=True)
        else:
            logger.debug("CacheKeepAlive ping #{} (无 send_fn，仅日志)", self._ping_count + 1)

    async def stop_async(self) -> None:
        """异步停止保活任务，等待任务结束。"""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("CacheKeepAlive 已异步停止 (共发送 {} 次 ping)", self._ping_count)


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------

_cost_tracker_instance: Optional[CostTracker] = None
_budget_manager_instance: Optional[BudgetManager] = None


async def get_cost_tracker(db_path: str | Path | None = None) -> CostTracker:
    """获取全局 CostTracker 单例。"""
    global _cost_tracker_instance
    if _cost_tracker_instance is None:
        _cost_tracker_instance = CostTracker(db_path=db_path)
        await _cost_tracker_instance._ensure_db()
    return _cost_tracker_instance


async def get_budget_manager(
    cost_tracker: CostTracker | None = None,
    db_path: str | Path | None = None,
) -> BudgetManager:
    """获取全局 BudgetManager 单例。"""
    global _budget_manager_instance
    if _budget_manager_instance is None:
        if cost_tracker is None:
            cost_tracker = await get_cost_tracker(db_path)
        _budget_manager_instance = BudgetManager(cost_tracker=cost_tracker, db_path=db_path)
        await _budget_manager_instance._ensure_db()
    return _budget_manager_instance


async def shutdown_cost_monitor() -> None:
    """关闭全局 CostTracker 和 BudgetManager 连接。"""
    global _cost_tracker_instance, _budget_manager_instance
    if _cost_tracker_instance is not None:
        await _cost_tracker_instance.close()
        _cost_tracker_instance = None
    if _budget_manager_instance is not None:
        await _budget_manager_instance.close()
        _budget_manager_instance = None
