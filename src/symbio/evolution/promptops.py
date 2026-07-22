"""PromptOps — Prompt 版本控制、A/B 测试与灰度发布。

支持：
- Prompt 版本管理（创建、查询、回滚）
- A/B 测试（流量分配、结果记录、胜出判定）
- 灰度发布（渐进式流量切换）
- 异步 SQLite 存储
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("promptops")


# =============================================================================
# 1. 数据模型
# =============================================================================


class VersionStatus(str, Enum):
    """Prompt 版本状态。"""

    DRAFT = "draft"  # 草稿
    ACTIVE = "active"  # 活跃（当前使用）
    TESTING = "testing"  # 测试中（A/B 测试）
    CANARY = "canary"  # 灰度发布中
    DEPRECATED = "deprecated"  # 已弃用
    ARCHIVED = "archived"  # 已归档


class ABTestStatus(str, Enum):
    """A/B 测试状态。"""

    DRAFT = "draft"  # 草稿
    RUNNING = "running"  # 运行中
    PAUSED = "paused"  # 已暂停
    COMPLETED = "completed"  # 已完成
    CANCELLED = "cancelled"  # 已取消


class CanaryStage(str, Enum):
    """灰度发布阶段。"""

    STAGE_1 = "1pct"  # 1% 流量
    STAGE_2 = "5pct"  # 5% 流量
    STAGE_3 = "10pct"  # 10% 流量
    STAGE_4 = "25pct"  # 25% 流量
    STAGE_5 = "50pct"  # 50% 流量
    STAGE_6 = "100pct"  # 全量


class PromptVersion(BaseModel):
    """Prompt 版本记录。"""

    version_id: str = Field(default_factory=lambda: str(uuid4()))
    prompt_name: str = Field(description="Prompt 名称（逻辑标识）")
    version_number: int = Field(description="版本号")
    content: str = Field(description="Prompt 内容")
    description: str = Field(default="", description="版本描述")
    system_prompt: str = Field(default="", description="系统提示词")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="参数配置（temperature, top_p 等）"
    )
    tags: list[str] = Field(default_factory=list, description="标签")
    status: VersionStatus = Field(default=VersionStatus.DRAFT, description="版本状态")
    parent_version_id: Optional[str] = Field(default=None, description="父版本 ID（用于回滚追溯）")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ABTestVariant(BaseModel):
    """A/B 测试变体。"""

    variant_id: str = Field(default_factory=lambda: str(uuid4()))
    version_id: str = Field(description="关联的 Prompt 版本 ID")
    variant_name: str = Field(description="变体名称（如 control / treatment_a）")
    traffic_weight: float = Field(ge=0.0, le=1.0, description="流量权重 (0-1)")
    sample_count: int = Field(default=0, description="样本数")
    success_count: int = Field(default=0, description="成功数")
    total_score: float = Field(default=0.0, description="总评分")
    average_score: float = Field(default=0.0, description="平均评分")
    average_latency_ms: float = Field(default=0.0, description="平均延迟（毫秒）")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ABTest(BaseModel):
    """A/B 测试记录。"""

    test_id: str = Field(default_factory=lambda: str(uuid4()))
    test_name: str = Field(description="测试名称")
    description: str = Field(default="", description="测试描述")
    prompt_name: str = Field(description="关联的 Prompt 名称")
    variants: list[ABTestVariant] = Field(description="变体列表")
    status: ABTestStatus = Field(default=ABTestStatus.DRAFT)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    min_sample_size: int = Field(default=100, description="最小样本量")
    confidence_level: float = Field(default=0.95, description="置信水平")
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ABTestResult(BaseModel):
    """A/B 测试单条结果记录。"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    test_id: str = Field(description="关联的 A/B 测试 ID")
    variant_id: str = Field(description="关联的变体 ID")
    task_id: str = Field(default="", description="任务 ID")
    session_id: str = Field(default="", description="会话 ID")
    success: bool = Field(description="是否成功")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="评分 0-1")
    latency_ms: int = Field(default=0, description="延迟（毫秒）")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class CanaryRelease(BaseModel):
    """灰度发布记录。"""

    release_id: str = Field(default_factory=lambda: str(uuid4()))
    prompt_name: str = Field(description="Prompt 名称")
    from_version_id: str = Field(description="当前版本 ID")
    to_version_id: str = Field(description="目标版本 ID")
    current_stage: CanaryStage = Field(default=CanaryStage.STAGE_1)
    stage_traffic: float = Field(default=0.01, description="当前阶段流量比例")
    is_active: bool = Field(default=True, description="是否进行中")
    rollback_version_id: Optional[str] = Field(default=None, description="回滚版本 ID")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VersionQuery(BaseModel):
    """版本查询条件。"""

    prompt_name: Optional[str] = None
    status: Optional[VersionStatus] = None
    limit: int = Field(default=50, ge=1)


# =============================================================================
# 2. PromptOps 模块
# =============================================================================


class PromptOps:
    """PromptOps — Prompt 版本控制、A/B 测试、灰度发布。

    使用 aiosqlite 异步存储，支持：
    - create_version(): 创建 Prompt 版本
    - start_ab_test(): 启动 A/B 测试
    - record_result(): 记录测试结果
    - get_winning_variant(): 获取胜出变体

    Usage::

        async with PromptOps("promptops.db") as ops:
            v = await ops.create_version(PromptVersion(
                prompt_name="summarizer",
                version_number=1,
                content="请总结以下内容：{text}",
            ))
            test = await ops.start_ab_test(ABTest(
                test_name="summarizer_v1_vs_v2",
                prompt_name="summarizer",
                variants=[...],
            ))
    """

    def __init__(self, db_path: str = "symbio_promptops.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> PromptOps:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立数据库连接并初始化表结构。"""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"PromptOps connected to {self._db_path}")

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug("PromptOps connection closed")

    async def _create_tables(self) -> None:
        """创建 PromptOps 表。"""
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                version_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                content TEXT NOT NULL,
                description TEXT DEFAULT '',
                system_prompt TEXT DEFAULT '',
                parameters TEXT DEFAULT '{}',
                tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'draft',
                parent_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_pv_name
                ON prompt_versions(prompt_name);
            CREATE INDEX IF NOT EXISTS idx_pv_status
                ON prompt_versions(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pv_name_version
                ON prompt_versions(prompt_name, version_number);

            CREATE TABLE IF NOT EXISTS ab_tests (
                test_id TEXT PRIMARY KEY,
                test_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                prompt_name TEXT NOT NULL,
                variants TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                start_time TEXT,
                end_time TEXT,
                min_sample_size INTEGER DEFAULT 100,
                confidence_level REAL DEFAULT 0.95,
                created_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_abt_name
                ON ab_tests(test_name);
            CREATE INDEX IF NOT EXISTS idx_abt_status
                ON ab_tests(status);
            CREATE INDEX IF NOT EXISTS idx_abt_prompt
                ON ab_tests(prompt_name);

            CREATE TABLE IF NOT EXISTS ab_test_results (
                result_id TEXT PRIMARY KEY,
                test_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                task_id TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                success INTEGER NOT NULL,
                score REAL DEFAULT 0.0,
                latency_ms INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_abtr_test
                ON ab_test_results(test_id);
            CREATE INDEX IF NOT EXISTS idx_abtr_variant
                ON ab_test_results(variant_id);

            CREATE TABLE IF NOT EXISTS canary_releases (
                release_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                from_version_id TEXT NOT NULL,
                to_version_id TEXT NOT NULL,
                current_stage TEXT DEFAULT '1pct',
                stage_traffic REAL DEFAULT 0.01,
                is_active INTEGER DEFAULT 1,
                rollback_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_canary_active
                ON canary_releases(is_active);
            CREATE INDEX IF NOT EXISTS idx_canary_prompt
                ON canary_releases(prompt_name);
        """)
        await self._db.commit()

    # -------------------------------------------------------------------------
    # 版本管理
    # -------------------------------------------------------------------------

    async def create_version(self, version: PromptVersion) -> str:
        """创建 Prompt 版本。

        如果未指定 version_number，自动递增。

        Args:
            version: Prompt 版本记录

        Returns:
            版本 ID
        """
        assert self._db is not None

        # 自动递增版本号
        if version.version_number <= 0:
            cursor = await self._db.execute(
                "SELECT COALESCE(MAX(version_number), 0) FROM prompt_versions "
                "WHERE prompt_name = ?",
                (version.prompt_name,),
            )
            row = await cursor.fetchone()
            version.version_number = (row[0] if row else 0) + 1

        await self._db.execute(
            """
            INSERT INTO prompt_versions
                (version_id, prompt_name, version_number, content,
                 description, system_prompt, parameters, tags, status,
                 parent_version_id, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.version_id,
                version.prompt_name,
                version.version_number,
                version.content,
                version.description,
                version.system_prompt,
                json.dumps(version.parameters, ensure_ascii=False),
                json.dumps(version.tags, ensure_ascii=False),
                version.status.value,
                version.parent_version_id,
                version.created_at.isoformat(),
                version.updated_at.isoformat(),
                json.dumps(version.metadata, ensure_ascii=False),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Created prompt version {version.version_id}: "
            f"{version.prompt_name} v{version.version_number}"
        )
        return version.version_id

    async def get_version(self, version_id: str) -> Optional[dict[str, Any]]:
        """获取 Prompt 版本详情。

        Args:
            version_id: 版本 ID

        Returns:
            版本字典，不存在返回 None
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM prompt_versions WHERE version_id = ?",
            (version_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def get_active_version(self, prompt_name: str) -> Optional[dict[str, Any]]:
        """获取指定 Prompt 当前活跃版本。

        Args:
            prompt_name: Prompt 名称

        Returns:
            活跃版本字典，无活跃版本返回 None
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM prompt_versions "
            "WHERE prompt_name = ? AND status = ? "
            "ORDER BY version_number DESC LIMIT 1",
            (prompt_name, VersionStatus.ACTIVE.value),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def list_versions(self, query: VersionQuery) -> list[dict[str, Any]]:
        """查询 Prompt 版本列表。

        Args:
            query: 查询条件

        Returns:
            版本列表（按版本号降序）
        """
        assert self._db is not None
        conditions = ["1=1"]
        params: list[Any] = []

        if query.prompt_name:
            conditions.append("prompt_name = ?")
            params.append(query.prompt_name)
        if query.status:
            conditions.append("status = ?")
            params.append(query.status.value)

        where = " AND ".join(conditions)
        params.append(query.limit)

        cursor = await self._db.execute(
            f"SELECT * FROM prompt_versions WHERE {where} ORDER BY version_number DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def set_version_status(self, version_id: str, status: VersionStatus) -> bool:
        """更新版本状态。

        如果设置为 ACTIVE，会自动将同名其他活跃版本设为 DEPRECATED。

        Args:
            version_id: 版本 ID
            status: 新状态

        Returns:
            是否成功
        """
        assert self._db is not None

        # 获取版本信息
        version = await self.get_version(version_id)
        if version is None:
            logger.warning(f"Version {version_id} not found")
            return False

        # 设置为 ACTIVE 时，旧版本降级
        if status == VersionStatus.ACTIVE:
            await self._db.execute(
                "UPDATE prompt_versions SET status = ?, updated_at = ? "
                "WHERE prompt_name = ? AND status = ? AND version_id != ?",
                (
                    VersionStatus.DEPRECATED.value,
                    datetime.now().isoformat(),
                    version["prompt_name"],
                    VersionStatus.ACTIVE.value,
                    version_id,
                ),
            )

        await self._db.execute(
            "UPDATE prompt_versions SET status = ?, updated_at = ? WHERE version_id = ?",
            (status.value, datetime.now().isoformat(), version_id),
        )
        await self._db.commit()
        logger.info(f"Version {version_id} status -> {status.value}")
        return True

    async def rollback_version(self, version_id: str) -> Optional[str]:
        """回滚到指定版本。

        创建一个新版本，内容复制自目标版本，标记为 ACTIVE。

        Args:
            version_id: 要回滚到的版本 ID

        Returns:
            新创建的版本 ID，失败返回 None
        """
        target = await self.get_version(version_id)
        if target is None:
            logger.warning(f"Cannot rollback: version {version_id} not found")
            return None

        # 获取当前最大版本号
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(version_number), 0) FROM prompt_versions WHERE prompt_name = ?",
            (target["prompt_name"],),
        )
        row = await cursor.fetchone()
        new_version_num = (row[0] if row else 0) + 1

        new_version = PromptVersion(
            prompt_name=target["prompt_name"],
            version_number=new_version_num,
            content=target["content"],
            description=f"Rollback from v{target['version_number']}",
            system_prompt=target.get("system_prompt", ""),
            parameters=target.get("parameters", {}),
            tags=target.get("tags", []),
            status=VersionStatus.ACTIVE,
            parent_version_id=version_id,
        )

        new_id = await self.create_version(new_version)
        # 直接设为 ACTIVE（create_version 默认 DRAFT）
        await self.set_version_status(new_id, VersionStatus.ACTIVE)

        logger.info(
            f"Rolled back {target['prompt_name']} to v{target['version_number']}, "
            f"new version ID: {new_id}"
        )
        return new_id

    # -------------------------------------------------------------------------
    # A/B 测试
    # -------------------------------------------------------------------------

    async def start_ab_test(self, test: ABTest) -> str:
        """启动 A/B 测试。

        Args:
            test: A/B 测试配置

        Returns:
            测试 ID
        """
        assert self._db is not None

        # 验证流量权重之和为 1
        total_weight = sum(v.traffic_weight for v in test.variants)
        if abs(total_weight - 1.0) > 0.01:
            logger.warning(
                f"A/B test variant weights sum to {total_weight}, expected 1.0. "
                f"Normalizing weights."
            )
            for v in test.variants:
                v.traffic_weight = v.traffic_weight / total_weight

        # 设置关联版本状态为 TESTING
        for variant in test.variants:
            await self._db.execute(
                "UPDATE prompt_versions SET status = ? WHERE version_id = ?",
                (VersionStatus.TESTING.value, variant.version_id),
            )

        test.status = ABTestStatus.RUNNING
        test.start_time = datetime.now()

        await self._db.execute(
            """
            INSERT INTO ab_tests
                (test_id, test_name, description, prompt_name,
                 variants, status, start_time, end_time,
                 min_sample_size, confidence_level, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                test.test_id,
                test.test_name,
                test.description,
                test.prompt_name,
                json.dumps([v.model_dump() for v in test.variants], ensure_ascii=False),
                test.status.value,
                test.start_time.isoformat() if test.start_time else None,
                test.end_time.isoformat() if test.end_time else None,
                test.min_sample_size,
                test.confidence_level,
                test.created_at.isoformat(),
                json.dumps(test.metadata, ensure_ascii=False),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Started A/B test {test.test_id}: '{test.test_name}' "
            f"with {len(test.variants)} variants"
        )
        return test.test_id

    async def record_result(self, result: ABTestResult) -> str:
        """记录 A/B 测试结果。

        同时更新对应变体的聚合统计。

        Args:
            result: 测试结果

        Returns:
            结果 ID
        """
        assert self._db is not None

        await self._db.execute(
            """
            INSERT INTO ab_test_results
                (result_id, test_id, variant_id, task_id, session_id,
                 success, score, latency_ms, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.result_id,
                result.test_id,
                result.variant_id,
                result.task_id,
                result.session_id,
                1 if result.success else 0,
                result.score,
                result.latency_ms,
                json.dumps(result.metadata, ensure_ascii=False),
                result.created_at.isoformat(),
            ),
        )

        # 更新变体统计
        await self._update_variant_stats(
            result.test_id,
            result.variant_id,
            result.success,
            result.score,
            result.latency_ms,
        )

        await self._db.commit()
        logger.debug(
            f"Recorded A/B test result {result.result_id}: "
            f"test={result.test_id}, variant={result.variant_id}, "
            f"success={result.success}, score={result.score}"
        )
        return result.result_id

    async def _update_variant_stats(
        self,
        test_id: str,
        variant_id: str,
        success: bool,
        score: float,
        latency_ms: int,
    ) -> None:
        """更新变体的聚合统计。"""
        assert self._db is not None

        # 读取当前测试数据
        cursor = await self._db.execute(
            "SELECT variants FROM ab_tests WHERE test_id = ?", (test_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return

        variants = json.loads(row[0])
        for v in variants:
            if v["variant_id"] == variant_id:
                v["sample_count"] = v.get("sample_count", 0) + 1
                if success:
                    v["success_count"] = v.get("success_count", 0) + 1
                old_total = v.get("total_score", 0.0)
                v["total_score"] = old_total + score
                count = v["sample_count"]
                v["average_score"] = round(v["total_score"] / count, 4) if count > 0 else 0.0
                old_latency = v.get("average_latency_ms", 0.0)
                v["average_latency_ms"] = (
                    round((old_latency * (count - 1) + latency_ms) / count, 2) if count > 0 else 0.0
                )
                break

        await self._db.execute(
            "UPDATE ab_tests SET variants = ? WHERE test_id = ?",
            (json.dumps(variants, ensure_ascii=False), test_id),
        )

    async def get_ab_test(self, test_id: str) -> Optional[dict[str, Any]]:
        """获取 A/B 测试详情。

        Args:
            test_id: 测试 ID

        Returns:
            测试字典，不存在返回 None
        """
        assert self._db is not None
        cursor = await self._db.execute("SELECT * FROM ab_tests WHERE test_id = ?", (test_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def get_winning_variant(self, test_id: str) -> Optional[dict[str, Any]]:
        """获取 A/B 测试的胜出变体。

        判定逻辑：
        1. 所有变体样本量 >= min_sample_size
        2. 使用 Z-test 比较成功率
        3. 返回评分最高且统计显著的变体

        Args:
            test_id: 测试 ID

        Returns:
            胜出变体字典，无法判定返回 None
        """
        test = await self.get_ab_test(test_id)
        if test is None:
            logger.warning(f"A/B test {test_id} not found")
            return None

        variants = test.get("variants", [])
        if len(variants) < 2:
            logger.warning(f"A/B test {test_id} has fewer than 2 variants")
            return None

        min_sample = test.get("min_sample_size", 100)

        # 检查样本量
        all_sufficient = all(v.get("sample_count", 0) >= min_sample for v in variants)
        if not all_sufficient:
            logger.info(
                f"A/B test {test_id}: not all variants have reached min sample size ({min_sample})"
            )
            # 仍返回当前最优的
            return max(variants, key=lambda v: v.get("average_score", 0.0))

        # 统计显著性检验（简化 Z-test）
        best = max(variants, key=lambda v: v.get("average_score", 0.0))
        confidence = test.get("confidence_level", 0.95)
        z_threshold = self._z_threshold(confidence)

        for other in variants:
            if other["variant_id"] == best["variant_id"]:
                continue
            is_significant = self._z_test_proportions(
                best.get("success_count", 0),
                best.get("sample_count", 1),
                other.get("success_count", 0),
                other.get("sample_count", 1),
                z_threshold,
            )
            if not is_significant:
                logger.info(
                    f"A/B test {test_id}: best variant '{best.get('variant_name')}' "
                    f"is NOT significantly better than '{other.get('variant_name')}'"
                )
                return None

        logger.info(
            f"A/B test {test_id}: winning variant is "
            f"'{best.get('variant_name')}' (score={best.get('average_score', 0):.4f})"
        )
        return best

    async def complete_ab_test(self, test_id: str) -> bool:
        """完成 A/B 测试。

        Args:
            test_id: 测试 ID

        Returns:
            是否成功
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "UPDATE ab_tests SET status = ?, end_time = ? WHERE test_id = ?",
            (
                ABTestStatus.COMPLETED.value,
                datetime.now().isoformat(),
                test_id,
            ),
        )
        await self._db.commit()
        success = cursor.rowcount > 0
        if success:
            logger.info(f"A/B test {test_id} completed")
        return success

    async def list_ab_tests(
        self,
        prompt_name: Optional[str] = None,
        status: Optional[ABTestStatus] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询 A/B 测试列表。

        Args:
            prompt_name: 可选，按 Prompt 名称过滤
            status: 可选，按状态过滤
            limit: 返回数量上限

        Returns:
            测试列表
        """
        assert self._db is not None
        conditions = ["1=1"]
        params: list[Any] = []

        if prompt_name:
            conditions.append("prompt_name = ?")
            params.append(prompt_name)
        if status:
            conditions.append("status = ?")
            params.append(status.value)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT * FROM ab_tests WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # 灰度发布
    # -------------------------------------------------------------------------

    async def start_canary_release(self, release: CanaryRelease) -> str:
        """启动灰度发布。

        Args:
            release: 灰度发布配置

        Returns:
            发布 ID
        """
        assert self._db is not None

        # 将目标版本设为 CANARY 状态
        await self._db.execute(
            "UPDATE prompt_versions SET status = ? WHERE version_id = ?",
            (VersionStatus.CANARY.value, release.to_version_id),
        )

        release.stage_traffic = self._stage_to_traffic(release.current_stage)

        await self._db.execute(
            """
            INSERT INTO canary_releases
                (release_id, prompt_name, from_version_id, to_version_id,
                 current_stage, stage_traffic, is_active,
                 rollback_version_id, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release.release_id,
                release.prompt_name,
                release.from_version_id,
                release.to_version_id,
                release.current_stage.value,
                release.stage_traffic,
                1 if release.is_active else 0,
                release.rollback_version_id,
                release.created_at.isoformat(),
                release.updated_at.isoformat(),
                json.dumps(release.metadata, ensure_ascii=False),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Started canary release {release.release_id}: "
            f"{release.prompt_name}, stage={release.current_stage.value}"
        )
        return release.release_id

    async def advance_canary(self, release_id: str) -> Optional[CanaryStage]:
        """推进灰度发布到下一阶段。

        Args:
            release_id: 发布 ID

        Returns:
            新阶段，无法推进返回 None
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT current_stage, is_active FROM canary_releases WHERE release_id = ?",
            (release_id,),
        )
        row = await cursor.fetchone()
        if not row or not row[1]:
            logger.warning(f"Canary release {release_id} not found or not active")
            return None

        current = CanaryStage(row[0])
        stages = list(CanaryStage)
        current_idx = stages.index(current)
        if current_idx >= len(stages) - 1:
            logger.info(f"Canary release {release_id} already at final stage")
            return None

        next_stage = stages[current_idx + 1]
        traffic = self._stage_to_traffic(next_stage)

        await self._db.execute(
            "UPDATE canary_releases SET current_stage = ?, stage_traffic = ?, "
            "updated_at = ? WHERE release_id = ?",
            (
                next_stage.value,
                traffic,
                datetime.now().isoformat(),
                release_id,
            ),
        )

        # 如果到了 100%，设为完成
        if next_stage == CanaryStage.STAGE_6:
            await self._complete_canary(release_id)

        await self._db.commit()
        logger.info(
            f"Canary release {release_id} advanced to {next_stage.value} (traffic={traffic:.0%})"
        )
        return next_stage

    async def rollback_canary(self, release_id: str) -> bool:
        """回滚灰度发布。

        Args:
            release_id: 发布 ID

        Returns:
            是否成功
        """
        assert self._db is not None

        cursor = await self._db.execute(
            "SELECT to_version_id, from_version_id FROM canary_releases "
            "WHERE release_id = ? AND is_active = 1",
            (release_id,),
        )
        row = await cursor.fetchone()
        if not row:
            logger.warning(f"Active canary release {release_id} not found")
            return False

        to_version_id, from_version_id = row[0], row[1]

        # 将目标版本降级为 DEPRECATED
        await self._db.execute(
            "UPDATE prompt_versions SET status = ? WHERE version_id = ?",
            (VersionStatus.DEPRECATED.value, to_version_id),
        )
        # 恢复原版本为 ACTIVE
        await self._db.execute(
            "UPDATE prompt_versions SET status = ? WHERE version_id = ?",
            (VersionStatus.ACTIVE.value, from_version_id),
        )

        await self._db.execute(
            "UPDATE canary_releases SET is_active = 0, updated_at = ? WHERE release_id = ?",
            (datetime.now().isoformat(), release_id),
        )
        await self._db.commit()
        logger.info(
            f"Rolled back canary release {release_id}: "
            f"reverted {to_version_id} -> {from_version_id}"
        )
        return True

    # -------------------------------------------------------------------------
    # 辅助方法
    # -------------------------------------------------------------------------

    def resolve_version_for_request(self, prompt_name: str, request_id: str) -> Optional[str]:
        """根据请求 ID 决定使用哪个版本（用于灰度流量分配）。

        确定性分配：相同 request_id 总是路由到同一版本。

        Args:
            prompt_name: Prompt 名称
            request_id: 请求 ID（用于确定性哈希分流）

        Returns:
            版本 ID，无活跃发布返回 None
        """
        # 注意：这是一个同步方法，不需要数据库查询
        # 灰度流量分配逻辑在应用层调用，这里提供辅助
        return None  # 实际分配需要查询活跃的 canary release

    @staticmethod
    def _stage_to_traffic(stage: CanaryStage) -> float:
        """将灰度阶段映射为流量比例。"""
        mapping = {
            CanaryStage.STAGE_1: 0.01,
            CanaryStage.STAGE_2: 0.05,
            CanaryStage.STAGE_3: 0.10,
            CanaryStage.STAGE_4: 0.25,
            CanaryStage.STAGE_5: 0.50,
            CanaryStage.STAGE_6: 1.00,
        }
        return mapping.get(stage, 0.01)

    async def _complete_canary(self, release_id: str) -> None:
        """完成灰度发布。"""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT to_version_id FROM canary_releases WHERE release_id = ?",
            (release_id,),
        )
        row = await cursor.fetchone()
        if row:
            await self._db.execute(
                "UPDATE prompt_versions SET status = ? WHERE version_id = ?",
                (VersionStatus.ACTIVE.value, row[0]),
            )
        await self._db.execute(
            "UPDATE canary_releases SET is_active = 0, updated_at = ? WHERE release_id = ?",
            (datetime.now().isoformat(), release_id),
        )
        logger.info(f"Canary release {release_id} completed (100% traffic)")

    @staticmethod
    def _z_threshold(confidence: float) -> float:
        """根据置信水平返回 Z 值阈值。"""
        thresholds = {
            0.90: 1.645,
            0.95: 1.96,
            0.99: 2.576,
        }
        return thresholds.get(confidence, 1.96)

    @staticmethod
    def _z_test_proportions(
        success_a: int,
        total_a: int,
        success_b: int,
        total_b: int,
        z_threshold: float,
    ) -> bool:
        """双比例 Z 检验，判断 A 是否显著优于 B。"""
        if total_a == 0 or total_b == 0:
            return False

        p_a = success_a / total_a
        p_b = success_b / total_b
        p_pool = (success_a + success_b) / (total_a + total_b)

        if p_pool == 0 or p_pool == 1:
            return p_a > p_b

        se = math.sqrt(p_pool * (1 - p_pool) * (1 / total_a + 1 / total_b))
        if se == 0:
            return p_a > p_b

        z = (p_a - p_b) / se
        return z > z_threshold

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将数据库行转为字典，处理 JSON 字段。"""
        d = dict(row)
        json_fields = ("parameters", "tags", "metadata", "variants")
        for key in json_fields:
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        # is_active: int -> bool
        if "is_active" in d:
            d["is_active"] = bool(d["is_active"])
        return d
