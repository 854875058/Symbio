"""Feedback Collector — 收集与管理显式/隐式反馈数据。

支持：
- 显式反馈：评分 (1-5)、评论、标签
- 隐式反馈：用户行为分析（点击、停留时长、重试、放弃等）
- 异步 SQLite 存储
- 统计聚合查询
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("feedback")


# =============================================================================
# 1. 数据模型
# =============================================================================


class FeedbackType(str, Enum):
    """反馈类型枚举。"""
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class ImplicitActionType(str, Enum):
    """隐式行为类型。"""
    CLICK = "click"
    SCROLL = "scroll"
    DWELL = "dwell"           # 停留
    COPY = "copy"             # 复制结果
    REGENERATE = "regenerate" # 重新生成
    ACCEPT = "accept"         # 采纳结果
    REJECT = "reject"         # 拒绝结果
    ABANDON = "abandon"       # 中途放弃
    RETRY = "retry"           # 重试
    SHARE = "share"           # 分享
    BOOKMARK = "bookmark"     # 收藏


class ExplicitFeedback(BaseModel):
    """显式反馈记录。"""

    feedback_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(description="会话 ID")
    task_id: str = Field(default="", description="关联的任务 ID")
    prompt_id: str = Field(default="", description="关联的 Prompt 版本 ID")
    user_id: str = Field(default="", description="用户 ID")
    rating: float = Field(ge=1.0, le=5.0, description="评分 1-5")
    comment: str = Field(default="", description="评论文本")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")
    created_at: datetime = Field(default_factory=datetime.now)


class ImplicitFeedback(BaseModel):
    """隐式反馈记录。"""

    feedback_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = Field(description="会话 ID")
    task_id: str = Field(default="", description="关联的任务 ID")
    prompt_id: str = Field(default="", description="关联的 Prompt 版本 ID")
    user_id: str = Field(default="", description="用户 ID")
    action_type: ImplicitActionType = Field(description="行为类型")
    duration_ms: int = Field(default=0, description="行为持续时间（毫秒）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")
    created_at: datetime = Field(default_factory=datetime.now)


class FeedbackQuery(BaseModel):
    """反馈查询条件。"""

    session_id: Optional[str] = None
    task_id: Optional[str] = None
    prompt_id: Optional[str] = None
    user_id: Optional[str] = None
    feedback_type: Optional[FeedbackType] = None
    min_rating: Optional[float] = None
    max_rating: Optional[float] = None
    action_type: Optional[ImplicitActionType] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    limit: int = Field(default=100, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


class FeedbackStats(BaseModel):
    """反馈统计结果。"""

    total_explicit: int = Field(default=0, description="显式反馈总数")
    total_implicit: int = Field(default=0, description="隐式反馈总数")
    average_rating: float = Field(default=0.0, description="平均评分")
    rating_distribution: dict[str, int] = Field(
        default_factory=dict, description="评分分布 {score: count}"
    )
    action_distribution: dict[str, int] = Field(
        default_factory=dict, description="行为类型分布 {action: count}"
    )
    unique_users: int = Field(default=0, description="唯一用户数")
    unique_sessions: int = Field(default=0, description="唯一会话数")
    top_tags: list[tuple[str, int]] = Field(
        default_factory=list, description="热门标签 [(tag, count)]"
    )
    acceptance_rate: float = Field(default=0.0, description="采纳率")
    abandonment_rate: float = Field(default=0.0, description="放弃率")


# =============================================================================
# 2. 反馈收集器
# =============================================================================


class FeedbackCollector:
    """反馈收集器 — 收集、存储、查询显式与隐式反馈。

    使用 aiosqlite 异步存储，支持：
    - collect_explicit(): 收集显式反馈（评分、评论）
    - collect_implicit(): 收集隐式反馈（行为数据）
    - query_feedback(): 按条件查询反馈
    - get_stats(): 获取统计汇总

    Usage::

        async with FeedbackCollector("feedback.db") as collector:
            await collector.collect_explicit(ExplicitFeedback(
                session_id="s1", rating=4.5, comment="很好"
            ))
            stats = await collector.get_stats()
    """

    def __init__(self, db_path: str = "symbio_feedback.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> FeedbackCollector:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立数据库连接并初始化表结构。"""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"FeedbackCollector connected to {self._db_path}")

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug("FeedbackCollector connection closed")

    async def _create_tables(self) -> None:
        """创建反馈表。"""
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS explicit_feedback (
                feedback_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT DEFAULT '',
                prompt_id TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                rating REAL NOT NULL,
                comment TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_explicit_session
                ON explicit_feedback(session_id);
            CREATE INDEX IF NOT EXISTS idx_explicit_task
                ON explicit_feedback(task_id);
            CREATE INDEX IF NOT EXISTS idx_explicit_prompt
                ON explicit_feedback(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_explicit_user
                ON explicit_feedback(user_id);
            CREATE INDEX IF NOT EXISTS idx_explicit_rating
                ON explicit_feedback(rating);
            CREATE INDEX IF NOT EXISTS idx_explicit_created
                ON explicit_feedback(created_at);

            CREATE TABLE IF NOT EXISTS implicit_feedback (
                feedback_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT DEFAULT '',
                prompt_id TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                action_type TEXT NOT NULL,
                duration_ms INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_implicit_session
                ON implicit_feedback(session_id);
            CREATE INDEX IF NOT EXISTS idx_implicit_task
                ON implicit_feedback(task_id);
            CREATE INDEX IF NOT EXISTS idx_implicit_prompt
                ON implicit_feedback(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_implicit_user
                ON implicit_feedback(user_id);
            CREATE INDEX IF NOT EXISTS idx_implicit_action
                ON implicit_feedback(action_type);
            CREATE INDEX IF NOT EXISTS idx_implicit_created
                ON implicit_feedback(created_at);
        """)
        await self._db.commit()

    # -------------------------------------------------------------------------
    # 收集接口
    # -------------------------------------------------------------------------

    async def collect_explicit(self, feedback: ExplicitFeedback) -> str:
        """收集显式反馈。

        Args:
            feedback: 显式反馈记录

        Returns:
            反馈 ID
        """
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO explicit_feedback
                (feedback_id, session_id, task_id, prompt_id, user_id,
                 rating, comment, tags, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.feedback_id,
                feedback.session_id,
                feedback.task_id,
                feedback.prompt_id,
                feedback.user_id,
                feedback.rating,
                feedback.comment,
                json.dumps(feedback.tags, ensure_ascii=False),
                json.dumps(feedback.metadata, ensure_ascii=False),
                feedback.created_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Collected explicit feedback {feedback.feedback_id}: "
            f"rating={feedback.rating}, session={feedback.session_id}"
        )
        return feedback.feedback_id

    async def collect_implicit(self, feedback: ImplicitFeedback) -> str:
        """收集隐式反馈。

        Args:
            feedback: 隐式反馈记录

        Returns:
            反馈 ID
        """
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO implicit_feedback
                (feedback_id, session_id, task_id, prompt_id, user_id,
                 action_type, duration_ms, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.feedback_id,
                feedback.session_id,
                feedback.task_id,
                feedback.prompt_id,
                feedback.user_id,
                feedback.action_type.value,
                feedback.duration_ms,
                json.dumps(feedback.metadata, ensure_ascii=False),
                feedback.created_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Collected implicit feedback {feedback.feedback_id}: "
            f"action={feedback.action_type.value}, session={feedback.session_id}"
        )
        return feedback.feedback_id

    async def collect_explicit_batch(self, feedbacks: list[ExplicitFeedback]) -> int:
        """批量收集显式反馈。

        Args:
            feedbacks: 显式反馈列表

        Returns:
            成功插入的记录数
        """
        assert self._db is not None
        rows = [
            (
                f.feedback_id,
                f.session_id,
                f.task_id,
                f.prompt_id,
                f.user_id,
                f.rating,
                f.comment,
                json.dumps(f.tags, ensure_ascii=False),
                json.dumps(f.metadata, ensure_ascii=False),
                f.created_at.isoformat(),
            )
            for f in feedbacks
        ]
        await self._db.executemany(
            """
            INSERT INTO explicit_feedback
                (feedback_id, session_id, task_id, prompt_id, user_id,
                 rating, comment, tags, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._db.commit()
        logger.info(f"Batch collected {len(rows)} explicit feedbacks")
        return len(rows)

    async def collect_implicit_batch(self, feedbacks: list[ImplicitFeedback]) -> int:
        """批量收集隐式反馈。

        Args:
            feedbacks: 隐式反馈列表

        Returns:
            成功插入的记录数
        """
        assert self._db is not None
        rows = [
            (
                f.feedback_id,
                f.session_id,
                f.task_id,
                f.prompt_id,
                f.user_id,
                f.action_type.value,
                f.duration_ms,
                json.dumps(f.metadata, ensure_ascii=False),
                f.created_at.isoformat(),
            )
            for f in feedbacks
        ]
        await self._db.executemany(
            """
            INSERT INTO implicit_feedback
                (feedback_id, session_id, task_id, prompt_id, user_id,
                 action_type, duration_ms, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self._db.commit()
        logger.info(f"Batch collected {len(rows)} implicit feedbacks")
        return len(rows)

    # -------------------------------------------------------------------------
    # 查询接口
    # -------------------------------------------------------------------------

    async def query_feedback(self, query: FeedbackQuery) -> dict[str, list[dict[str, Any]]]:
        """按条件查询反馈数据。

        Args:
            query: 查询条件

        Returns:
            {"explicit": [...], "implicit": [...]} 两个列表
        """
        result: dict[str, list[dict[str, Any]]] = {"explicit": [], "implicit": []}

        if query.feedback_type is None or query.feedback_type == FeedbackType.EXPLICIT:
            result["explicit"] = await self._query_explicit(query)

        if query.feedback_type is None or query.feedback_type == FeedbackType.IMPLICIT:
            result["implicit"] = await self._query_implicit(query)

        return result

    async def _query_explicit(self, query: FeedbackQuery) -> list[dict[str, Any]]:
        """查询显式反馈。"""
        assert self._db is not None
        conditions: list[str] = []
        params: list[Any] = []

        if query.session_id:
            conditions.append("session_id = ?")
            params.append(query.session_id)
        if query.task_id:
            conditions.append("task_id = ?")
            params.append(query.task_id)
        if query.prompt_id:
            conditions.append("prompt_id = ?")
            params.append(query.prompt_id)
        if query.user_id:
            conditions.append("user_id = ?")
            params.append(query.user_id)
        if query.min_rating is not None:
            conditions.append("rating >= ?")
            params.append(query.min_rating)
        if query.max_rating is not None:
            conditions.append("rating <= ?")
            params.append(query.max_rating)
        if query.start_time:
            conditions.append("created_at >= ?")
            params.append(query.start_time.isoformat())
        if query.end_time:
            conditions.append("created_at <= ?")
            params.append(query.end_time.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT * FROM explicit_feedback
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([query.limit, query.offset])

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def _query_implicit(self, query: FeedbackQuery) -> list[dict[str, Any]]:
        """查询隐式反馈。"""
        assert self._db is not None
        conditions: list[str] = []
        params: list[Any] = []

        if query.session_id:
            conditions.append("session_id = ?")
            params.append(query.session_id)
        if query.task_id:
            conditions.append("task_id = ?")
            params.append(query.task_id)
        if query.prompt_id:
            conditions.append("prompt_id = ?")
            params.append(query.prompt_id)
        if query.user_id:
            conditions.append("user_id = ?")
            params.append(query.user_id)
        if query.action_type:
            conditions.append("action_type = ?")
            params.append(query.action_type.value)
        if query.start_time:
            conditions.append("created_at >= ?")
            params.append(query.start_time.isoformat())
        if query.end_time:
            conditions.append("created_at <= ?")
            params.append(query.end_time.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT * FROM implicit_feedback
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([query.limit, query.offset])

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # 统计接口
    # -------------------------------------------------------------------------

    async def get_stats(
        self,
        prompt_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> FeedbackStats:
        """获取反馈统计数据。

        Args:
            prompt_id: 可选，按 Prompt 版本过滤
            start_time: 可选，起始时间
            end_time: 可选，结束时间

        Returns:
            统计结果
        """
        assert self._db is not None

        # 构建过滤条件
        explicit_where, explicit_params = self._build_stats_where(
            "explicit_feedback", prompt_id, start_time, end_time
        )
        implicit_where, implicit_params = self._build_stats_where(
            "implicit_feedback", prompt_id, start_time, end_time
        )

        stats = FeedbackStats()

        # 显式反馈统计
        cursor = await self._db.execute(
            f"SELECT COUNT(*) as cnt, COALESCE(AVG(rating), 0) as avg_r "
            f"FROM explicit_feedback WHERE {explicit_where}",
            explicit_params,
        )
        row = await cursor.fetchone()
        if row:
            stats.total_explicit = row[0]
            stats.average_rating = round(row[1], 3)

        # 评分分布
        cursor = await self._db.execute(
            f"SELECT CAST(rating AS INTEGER) as r, COUNT(*) as cnt "
            f"FROM explicit_feedback WHERE {explicit_where} GROUP BY r",
            explicit_params,
        )
        rows = await cursor.fetchall()
        for r in rows:
            stats.rating_distribution[str(r[0])] = r[1]

        # 标签统计
        cursor = await self._db.execute(
            f"SELECT tags FROM explicit_feedback WHERE {explicit_where} AND tags != '[]'",
            explicit_params,
        )
        rows = await cursor.fetchall()
        tag_counter: dict[str, int] = {}
        for r in rows:
            try:
                tags = json.loads(r[0])
                for tag in tags:
                    tag_counter[tag] = tag_counter.get(tag, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        stats.top_tags = sorted(tag_counter.items(), key=lambda x: -x[1])[:20]

        # 隐式反馈统计
        cursor = await self._db.execute(
            f"SELECT COUNT(*) FROM implicit_feedback WHERE {implicit_where}",
            implicit_params,
        )
        row = await cursor.fetchone()
        if row:
            stats.total_implicit = row[0]

        # 行为类型分布
        cursor = await self._db.execute(
            f"SELECT action_type, COUNT(*) as cnt "
            f"FROM implicit_feedback WHERE {implicit_where} GROUP BY action_type",
            implicit_params,
        )
        rows = await cursor.fetchall()
        for r in rows:
            stats.action_distribution[r[0]] = r[1]

        # 采纳率与放弃率
        total_actions = sum(stats.action_distribution.values())
        if total_actions > 0:
            accepts = stats.action_distribution.get(ImplicitActionType.ACCEPT.value, 0)
            abandons = stats.action_distribution.get(ImplicitActionType.ABANDON.value, 0)
            stats.acceptance_rate = round(accepts / total_actions, 4)
            stats.abandonment_rate = round(abandons / total_actions, 4)

        # 唯一用户与会话
        cursor = await self._db.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM explicit_feedback WHERE {explicit_where}",
            explicit_params,
        )
        row = await cursor.fetchone()
        unique_users_explicit = row[0] if row else 0

        cursor = await self._db.execute(
            f"SELECT COUNT(DISTINCT user_id) FROM implicit_feedback WHERE {implicit_where}",
            implicit_params,
        )
        row = await cursor.fetchone()
        unique_users_implicit = row[0] if row else 0
        stats.unique_users = max(unique_users_explicit, unique_users_implicit)

        cursor = await self._db.execute(
            f"SELECT COUNT(DISTINCT session_id) FROM explicit_feedback WHERE {explicit_where}",
            explicit_params,
        )
        row = await cursor.fetchone()
        stats.unique_sessions = row[0] if row else 0

        logger.info(
            f"Feedback stats: explicit={stats.total_explicit}, "
            f"implicit={stats.total_implicit}, avg_rating={stats.average_rating}"
        )
        return stats

    @staticmethod
    def _build_stats_where(
        table: str,
        prompt_id: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> tuple[str, list[Any]]:
        """构建统计查询的 WHERE 子句。"""
        conditions = ["1=1"]
        params: list[Any] = []

        if prompt_id:
            conditions.append("prompt_id = ?")
            params.append(prompt_id)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time.isoformat())

        return " AND ".join(conditions), params

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将数据库行转为字典，处理 JSON 字段。"""
        d = dict(row)
        for key in ("tags", "metadata"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
