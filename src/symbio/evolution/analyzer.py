"""Pattern Analyzer — 失败复盘、根因记录与成功路径识别。

支持：
- 失败任务自动复盘与模式分析
- Root Cause 记录与高频统计
- 成功路径识别与聚类
- 异步 SQLite 存储
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

logger = get_logger("analyzer")


# =============================================================================
# 1. 数据模型
# =============================================================================


class FailureSeverity(str, Enum):
    """失败严重程度。"""
    LOW = "low"           # 轻微：不影响核心功能
    MEDIUM = "medium"     # 中等：影响部分功能
    HIGH = "high"         # 严重：影响核心功能
    CRITICAL = "critical" # 致命：系统不可用


class FailureCategory(str, Enum):
    """失败类别。"""
    LOGIC_ERROR = "logic_error"         # 逻辑错误
    TIMEOUT = "timeout"                 # 超时
    RESOURCE_EXHAUSTED = "resource"     # 资源耗尽
    EXTERNAL_API = "external_api"       # 外部 API 故障
    INPUT_INVALID = "input_invalid"     # 输入无效
    PERMISSION = "permission"           # 权限不足
    MODEL_ERROR = "model_error"         # 模型生成错误
    TOOL_ERROR = "tool_error"           # 工具执行错误
    CONTEXT_OVERFLOW = "context_overflow"  # 上下文溢出
    UNKNOWN = "unknown"                 # 未知


class FailureAnalysis(BaseModel):
    """失败分析记录。"""

    analysis_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str = Field(description="关联任务 ID")
    trajectory_id: str = Field(default="", description="关联轨迹 ID")
    prompt_id: str = Field(default="", description="关联 Prompt 版本 ID")
    category: FailureCategory = Field(description="失败类别")
    severity: FailureSeverity = Field(default=FailureSeverity.MEDIUM, description="严重程度")
    description: str = Field(description="失败描述")
    error_message: str = Field(default="", description="错误信息")
    context_snapshot: dict[str, Any] = Field(
        default_factory=dict, description="失败时的上下文快照"
    )
    steps_to_failure: int = Field(default=0, description="失败前执行步数")
    root_cause_id: Optional[str] = Field(default=None, description="关联的根因 ID")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class RootCause(BaseModel):
    """根因记录。"""

    cause_id: str = Field(default_factory=lambda: str(uuid4()))
    category: FailureCategory = Field(description="失败类别")
    cause_summary: str = Field(description="根因摘要")
    cause_detail: str = Field(default="", description="根因详细分析")
    fix_suggestion: str = Field(default="", description="修复建议")
    occurrence_count: int = Field(default=1, description="出现次数")
    first_seen: datetime = Field(default_factory=datetime.now, description="首次出现时间")
    last_seen: datetime = Field(default_factory=datetime.now, description="最近出现时间")
    related_analysis_ids: list[str] = Field(
        default_factory=list, description="关联的失败分析 ID 列表"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_resolved: bool = Field(default=False, description="是否已解决")


class SuccessPath(BaseModel):
    """成功路径记录。"""

    path_id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: str = Field(description="任务类型")
    prompt_id: str = Field(default="", description="关联 Prompt 版本 ID")
    trajectory_id: str = Field(default="", description="关联轨迹 ID")
    steps: list[dict[str, Any]] = Field(
        default_factory=list, description="路径步骤列表"
    )
    step_count: int = Field(default=0, description="步骤数")
    total_duration_ms: int = Field(default=0, description="总耗时（毫秒）")
    quality_score: float = Field(default=0.0, description="质量评分")
    success_count: int = Field(default=1, description="成功次数")
    first_seen: datetime = Field(default_factory=datetime.now)
    last_seen: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    """分析结果汇总。"""

    total_failures: int = Field(default=0, description="失败总数")
    total_root_causes: int = Field(default=0, description="根因总数")
    total_success_paths: int = Field(default=0, description="成功路径总数")
    top_failure_categories: list[tuple[str, int]] = Field(
        default_factory=list, description="高频失败类别"
    )
    top_root_causes: list[dict[str, Any]] = Field(
        default_factory=list, description="高频根因"
    )
    top_success_paths: list[dict[str, Any]] = Field(
        default_factory=list, description="高频成功路径"
    )
    failure_rate: float = Field(default=0.0, description="失败率")
    average_steps_to_failure: float = Field(default=0.0, description="平均失败步数")


# =============================================================================
# 2. 模式分析器
# =============================================================================


class PatternAnalyzer:
    """模式分析器 — 失败复盘、根因记录、成功路径识别。

    使用 aiosqlite 异步存储，支持：
    - analyze_failure(): 分析失败任务并记录
    - record_root_cause(): 记录根因并关联失败分析
    - identify_success_paths(): 识别并聚合高频成功路径

    Usage::

        async with PatternAnalyzer("analysis.db") as analyzer:
            analysis = FailureAnalysis(
                task_id="t1",
                category=FailureCategory.TIMEOUT,
                description="任务执行超时",
            )
            await analyzer.analyze_failure(analysis)

            cause = RootCause(
                category=FailureCategory.TIMEOUT,
                cause_summary="模型推理时间过长",
                fix_suggestion="增加超时时间或优化 prompt",
            )
            await analyzer.record_root_cause(cause)
    """

    def __init__(self, db_path: str = "symbio_analysis.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> PatternAnalyzer:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立数据库连接并初始化表结构。"""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"PatternAnalyzer connected to {self._db_path}")

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug("PatternAnalyzer connection closed")

    async def _create_tables(self) -> None:
        """创建分析表。"""
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS failure_analyses (
                analysis_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                trajectory_id TEXT DEFAULT '',
                prompt_id TEXT DEFAULT '',
                category TEXT NOT NULL,
                severity TEXT DEFAULT 'medium',
                description TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                context_snapshot TEXT DEFAULT '{}',
                steps_to_failure INTEGER DEFAULT 0,
                root_cause_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_failure_task
                ON failure_analyses(task_id);
            CREATE INDEX IF NOT EXISTS idx_failure_category
                ON failure_analyses(category);
            CREATE INDEX IF NOT EXISTS idx_failure_severity
                ON failure_analyses(severity);
            CREATE INDEX IF NOT EXISTS idx_failure_root_cause
                ON failure_analyses(root_cause_id);
            CREATE INDEX IF NOT EXISTS idx_failure_created
                ON failure_analyses(created_at);

            CREATE TABLE IF NOT EXISTS root_causes (
                cause_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                cause_summary TEXT NOT NULL,
                cause_detail TEXT DEFAULT '',
                fix_suggestion TEXT DEFAULT '',
                occurrence_count INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                related_analysis_ids TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                is_resolved INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_cause_category
                ON root_causes(category);
            CREATE INDEX IF NOT EXISTS idx_cause_count
                ON root_causes(occurrence_count DESC);
            CREATE INDEX IF NOT EXISTS idx_cause_resolved
                ON root_causes(is_resolved);

            CREATE TABLE IF NOT EXISTS success_paths (
                path_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                prompt_id TEXT DEFAULT '',
                trajectory_id TEXT DEFAULT '',
                steps TEXT DEFAULT '[]',
                step_count INTEGER DEFAULT 0,
                total_duration_ms INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0.0,
                success_count INTEGER DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_success_task_type
                ON success_paths(task_type);
            CREATE INDEX IF NOT EXISTS idx_success_prompt
                ON success_paths(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_success_count
                ON success_paths(success_count DESC);
            CREATE INDEX IF NOT EXISTS idx_success_quality
                ON success_paths(quality_score DESC);
        """)
        await self._db.commit()

    # -------------------------------------------------------------------------
    # 失败分析
    # -------------------------------------------------------------------------

    async def analyze_failure(self, analysis: FailureAnalysis) -> str:
        """记录失败分析。

        Args:
            analysis: 失败分析记录

        Returns:
            分析 ID
        """
        assert self._db is not None

        # 查找匹配的已有根因
        if analysis.root_cause_id is None:
            matched_cause = await self._find_matching_root_cause(
                analysis.category, analysis.description
            )
            if matched_cause:
                analysis.root_cause_id = matched_cause
                # 增加根因出现次数
                await self._increment_root_cause(analysis.analysis_id, matched_cause)

        await self._db.execute(
            """
            INSERT INTO failure_analyses
                (analysis_id, task_id, trajectory_id, prompt_id,
                 category, severity, description, error_message,
                 context_snapshot, steps_to_failure, root_cause_id,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.analysis_id,
                analysis.task_id,
                analysis.trajectory_id,
                analysis.prompt_id,
                analysis.category.value,
                analysis.severity.value,
                analysis.description,
                analysis.error_message,
                json.dumps(analysis.context_snapshot, ensure_ascii=False),
                analysis.steps_to_failure,
                analysis.root_cause_id,
                json.dumps(analysis.metadata, ensure_ascii=False),
                analysis.created_at.isoformat(),
            ),
        )
        await self._db.commit()

        logger.info(
            f"Recorded failure analysis {analysis.analysis_id}: "
            f"category={analysis.category.value}, severity={analysis.severity.value}"
        )
        return analysis.analysis_id

    async def get_failure_analyses(
        self,
        category: Optional[FailureCategory] = None,
        severity: Optional[FailureSeverity] = None,
        prompt_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """查询失败分析记录。

        Args:
            category: 可选，按类别过滤
            severity: 可选，按严重程度过滤
            prompt_id: 可选，按 Prompt 版本过滤
            limit: 返回数量上限

        Returns:
            失败分析记录列表
        """
        assert self._db is not None
        conditions = ["1=1"]
        params: list[Any] = []

        if category:
            conditions.append("category = ?")
            params.append(category.value)
        if severity:
            conditions.append("severity = ?")
            params.append(severity.value)
        if prompt_id:
            conditions.append("prompt_id = ?")
            params.append(prompt_id)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM failure_analyses WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # 根因管理
    # -------------------------------------------------------------------------

    async def record_root_cause(self, cause: RootCause) -> str:
        """记录根因。

        如果已存在相同类别和摘要的根因，则合并（增加计数、更新时间）。

        Args:
            cause: 根因记录

        Returns:
            根因 ID
        """
        assert self._db is not None

        # 检查是否已存在匹配的根因
        cursor = await self._db.execute(
            "SELECT cause_id, occurrence_count, related_analysis_ids "
            "FROM root_causes WHERE category = ? AND cause_summary = ?",
            (cause.category.value, cause.cause_summary),
        )
        existing = await cursor.fetchone()

        if existing:
            # 合并已有根因
            cause_id = existing[0]
            new_count = existing[1] + cause.occurrence_count
            try:
                related = json.loads(existing[2])
            except (json.JSONDecodeError, TypeError):
                related = []
            related.extend(cause.related_analysis_ids)
            # 去重
            related = list(dict.fromkeys(related))

            await self._db.execute(
                """
                UPDATE root_causes
                SET occurrence_count = ?,
                    last_seen = ?,
                    related_analysis_ids = ?,
                    cause_detail = CASE
                        WHEN ? != '' THEN ?
                        ELSE cause_detail
                    END,
                    fix_suggestion = CASE
                        WHEN ? != '' THEN ?
                        ELSE fix_suggestion
                    END
                WHERE cause_id = ?
                """,
                (
                    new_count,
                    datetime.now().isoformat(),
                    json.dumps(related, ensure_ascii=False),
                    cause.cause_detail,
                    cause.cause_detail,
                    cause.fix_suggestion,
                    cause.fix_suggestion,
                    cause_id,
                ),
            )
            await self._db.commit()
            logger.info(
                f"Merged into existing root cause {cause_id}: "
                f"count={new_count}, summary='{cause.cause_summary}'"
            )
            return cause_id

        # 新增根因
        await self._db.execute(
            """
            INSERT INTO root_causes
                (cause_id, category, cause_summary, cause_detail,
                 fix_suggestion, occurrence_count, first_seen, last_seen,
                 related_analysis_ids, metadata, is_resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cause.cause_id,
                cause.category.value,
                cause.cause_summary,
                cause.cause_detail,
                cause.fix_suggestion,
                cause.occurrence_count,
                cause.first_seen.isoformat(),
                cause.last_seen.isoformat(),
                json.dumps(cause.related_analysis_ids, ensure_ascii=False),
                json.dumps(cause.metadata, ensure_ascii=False),
                1 if cause.is_resolved else 0,
            ),
        )
        await self._db.commit()
        logger.info(
            f"Recorded new root cause {cause.cause_id}: "
            f"category={cause.category.value}, summary='{cause.cause_summary}'"
        )
        return cause.cause_id

    async def get_root_causes(
        self,
        category: Optional[FailureCategory] = None,
        min_occurrences: int = 1,
        include_resolved: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """查询根因列表。

        Args:
            category: 可选，按类别过滤
            min_occurrences: 最小出现次数
            include_resolved: 是否包含已解决的根因
            limit: 返回数量上限

        Returns:
            根因记录列表（按出现次数降序）
        """
        assert self._db is not None
        conditions = ["occurrence_count >= ?"]
        params: list[Any] = [min_occurrences]

        if category:
            conditions.append("category = ?")
            params.append(category.value)
        if not include_resolved:
            conditions.append("is_resolved = 0")

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM root_causes WHERE {where} "
            f"ORDER BY occurrence_count DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def resolve_root_cause(self, cause_id: str) -> bool:
        """标记根因为已解决。

        Args:
            cause_id: 根因 ID

        Returns:
            是否成功标记
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "UPDATE root_causes SET is_resolved = 1 WHERE cause_id = ?",
            (cause_id,),
        )
        await self._db.commit()
        success = cursor.rowcount > 0
        if success:
            logger.info(f"Root cause {cause_id} marked as resolved")
        else:
            logger.warning(f"Root cause {cause_id} not found")
        return success

    # -------------------------------------------------------------------------
    # 成功路径识别
    # -------------------------------------------------------------------------

    async def record_success_path(self, path: SuccessPath) -> str:
        """记录成功路径。

        如果已存在相似路径（相同 task_type + step_count 范围 + step 指纹），
        则合并（增加成功次数、更新质量评分）。

        Args:
            path: 成功路径记录

        Returns:
            路径 ID
        """
        assert self._db is not None

        # 查找相似路径
        path_fingerprint = self._compute_path_fingerprint(path.steps)
        cursor = await self._db.execute(
            "SELECT path_id, success_count, quality_score, metadata "
            "FROM success_paths WHERE task_type = ? AND step_count = ? "
            "AND json_extract(metadata, '$.fingerprint') = ?",
            (path.task_type, path.step_count, path_fingerprint),
        )
        existing = await cursor.fetchone()

        if existing:
            path_id = existing[0]
            new_count = existing[1] + 1
            # 使用加权平均更新质量评分
            old_score = existing[2]
            new_score = (old_score * existing[1] + path.quality_score) / new_count
            try:
                meta = json.loads(existing[3])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            meta["fingerprint"] = path_fingerprint

            await self._db.execute(
                """
                UPDATE success_paths
                SET success_count = ?,
                    quality_score = ?,
                    last_seen = ?,
                    metadata = ?
                WHERE path_id = ?
                """,
                (
                    new_count,
                    round(new_score, 4),
                    datetime.now().isoformat(),
                    json.dumps(meta, ensure_ascii=False),
                    path_id,
                ),
            )
            await self._db.commit()
            logger.info(
                f"Merged success path {path_id}: count={new_count}, "
                f"task_type={path.task_type}"
            )
            return path_id

        # 新增路径
        meta = dict(path.metadata)
        meta["fingerprint"] = path_fingerprint

        await self._db.execute(
            """
            INSERT INTO success_paths
                (path_id, task_type, prompt_id, trajectory_id, steps,
                 step_count, total_duration_ms, quality_score,
                 success_count, first_seen, last_seen, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path.path_id,
                path.task_type,
                path.prompt_id,
                path.trajectory_id,
                json.dumps(path.steps, ensure_ascii=False),
                path.step_count,
                path.total_duration_ms,
                path.quality_score,
                path.success_count,
                path.first_seen.isoformat(),
                path.last_seen.isoformat(),
                json.dumps(meta, ensure_ascii=False),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Recorded new success path {path.path_id}: "
            f"task_type={path.task_type}, steps={path.step_count}"
        )
        return path.path_id

    async def identify_success_paths(
        self,
        task_type: Optional[str] = None,
        min_success_count: int = 2,
        min_quality_score: float = 0.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """识别高频成功路径。

        Args:
            task_type: 可选，按任务类型过滤
            min_success_count: 最小成功次数
            min_quality_score: 最低质量评分
            limit: 返回数量上限

        Returns:
            高频成功路径列表（按成功次数降序）
        """
        assert self._db is not None
        conditions = [
            "success_count >= ?",
            "quality_score >= ?",
        ]
        params: list[Any] = [min_success_count, min_quality_score]

        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM success_paths WHERE {where} "
            f"ORDER BY success_count DESC, quality_score DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        results = [self._row_to_dict(row) for row in rows]
        logger.info(
            f"Identified {len(results)} success paths "
            f"(min_count={min_success_count}, min_quality={min_quality_score})"
        )
        return results

    # -------------------------------------------------------------------------
    # 综合分析
    # -------------------------------------------------------------------------

    async def get_analysis_summary(self) -> AnalysisResult:
        """获取综合分析结果汇总。

        Returns:
            分析结果汇总
        """
        assert self._db is not None
        result = AnalysisResult()

        # 失败总数
        cursor = await self._db.execute("SELECT COUNT(*) FROM failure_analyses")
        row = await cursor.fetchone()
        result.total_failures = row[0] if row else 0

        # 根因总数
        cursor = await self._db.execute("SELECT COUNT(*) FROM root_causes")
        row = await cursor.fetchone()
        result.total_root_causes = row[0] if row else 0

        # 成功路径总数
        cursor = await self._db.execute("SELECT COUNT(*) FROM success_paths")
        row = await cursor.fetchone()
        result.total_success_paths = row[0] if row else 0

        # 高频失败类别
        cursor = await self._db.execute(
            "SELECT category, COUNT(*) as cnt FROM failure_analyses "
            "GROUP BY category ORDER BY cnt DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        result.top_failure_categories = [(r[0], r[1]) for r in rows]

        # 高频根因
        cursor = await self._db.execute(
            "SELECT cause_id, category, cause_summary, occurrence_count "
            "FROM root_causes ORDER BY occurrence_count DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        result.top_root_causes = [
            {
                "cause_id": r[0],
                "category": r[1],
                "cause_summary": r[2],
                "occurrence_count": r[3],
            }
            for r in rows
        ]

        # 高频成功路径
        cursor = await self._db.execute(
            "SELECT path_id, task_type, success_count, quality_score, step_count "
            "FROM success_paths ORDER BY success_count DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        result.top_success_paths = [
            {
                "path_id": r[0],
                "task_type": r[1],
                "success_count": r[2],
                "quality_score": r[3],
                "step_count": r[4],
            }
            for r in rows
        ]

        # 失败率（基于有记录的分析）
        total = result.total_failures + result.total_success_paths
        if total > 0:
            result.failure_rate = round(result.total_failures / total, 4)

        # 平均失败步数
        cursor = await self._db.execute(
            "SELECT COALESCE(AVG(steps_to_failure), 0) FROM failure_analyses "
            "WHERE steps_to_failure > 0"
        )
        row = await cursor.fetchone()
        result.average_steps_to_failure = round(row[0], 2) if row else 0.0

        logger.info(
            f"Analysis summary: failures={result.total_failures}, "
            f"causes={result.total_root_causes}, "
            f"success_paths={result.total_success_paths}"
        )
        return result

    # -------------------------------------------------------------------------
    # 内部方法
    # -------------------------------------------------------------------------

    async def _find_matching_root_cause(
        self, category: FailureCategory, description: str
    ) -> Optional[str]:
        """查找与失败分析匹配的已有根因。"""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT cause_id FROM root_causes WHERE category = ? AND is_resolved = 0 "
            "ORDER BY occurrence_count DESC LIMIT 1",
            (category.value,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def _increment_root_cause(
        self, analysis_id: str, cause_id: str
    ) -> None:
        """增加根因的出现次数并关联分析 ID。"""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT occurrence_count, related_analysis_ids FROM root_causes WHERE cause_id = ?",
            (cause_id,),
        )
        row = await cursor.fetchone()
        if row:
            new_count = row[0] + 1
            try:
                related = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                related = []
            related.append(analysis_id)
            related = list(dict.fromkeys(related))

            await self._db.execute(
                "UPDATE root_causes SET occurrence_count = ?, "
                "last_seen = ?, related_analysis_ids = ? WHERE cause_id = ?",
                (
                    new_count,
                    datetime.now().isoformat(),
                    json.dumps(related, ensure_ascii=False),
                    cause_id,
                ),
            )
            await self._db.commit()

    @staticmethod
    def _compute_path_fingerprint(steps: list[dict[str, Any]]) -> str:
        """计算路径步骤的指纹，用于相似路径匹配。

        提取每个步骤的核心动作类型，生成结构指纹。
        """
        import hashlib

        actions = []
        for step in steps:
            action = step.get("action", "")
            tool_name = step.get("tool_name", "")
            key = f"{action}:{tool_name}" if tool_name else action
            actions.append(key)

        fingerprint_str = "|".join(actions)
        return hashlib.sha256(fingerprint_str.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将数据库行转为字典，处理 JSON 字段。"""
        d = dict(row)
        json_fields = (
            "context_snapshot", "metadata", "related_analysis_ids", "steps"
        )
        for key in json_fields:
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        # is_resolved: int -> bool
        if "is_resolved" in d:
            d["is_resolved"] = bool(d["is_resolved"])
        return d
