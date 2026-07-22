"""版本化记忆与冲突解决 - 记忆版本历史管理、4层冲突解决机制。

核心能力：
1. VersionedMemory - 包装 MemoryManager，为每次更新创建版本快照
2. ConflictResolver - 4层冲突解决（时间戳→来源可信度→因果推理→用户确认）
3. 持久化版本历史与冲突记录到独立的 SQLite 数据库
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from difflib import unified_diff
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.memory.manager import MemoryItem, MemoryManager
from symbio.utils.logger import get_logger

logger = get_logger("memory.versioning")


# ---------------------------------------------------------------------------
# 枚举与数据模型
# ---------------------------------------------------------------------------


class CredibilitySource(str, Enum):
    """来源可信度权重（数值越高越可信）。

    L2 层冲突解决使用此权重决定哪个来源优先。
    """

    CODE_CONFIG = "code_config"  # 代码/配置文件来源（最高）
    ADMIN = "admin"  # 管理员操作
    USER = "user"  # 用户输入
    AGENT = "agent"  # AI 代理生成
    EXTERNAL = "external"  # 外部数据源（最低）

    @property
    def weight(self) -> int:
        """返回可信度权重值。"""
        _weights = {
            CredibilitySource.CODE_CONFIG: 5,
            CredibilitySource.ADMIN: 4,
            CredibilitySource.USER: 3,
            CredibilitySource.AGENT: 2,
            CredibilitySource.EXTERNAL: 1,
        }
        return _weights[self]

    @classmethod
    def from_source_string(cls, source: str) -> CredibilitySource:
        """从来源字符串推断可信度等级。

        匹配逻辑（按优先级）：
        - 包含 code / config / file / path 关键词 -> CODE_CONFIG
        - 包含 admin / root / system 关键词 -> ADMIN
        - 包含 user / human / manual 关键词 -> USER
        - 包含 agent / llm / ai / gpt / claude 关键词 -> AGENT
        - 其他 -> EXTERNAL
        """
        s = source.lower()
        if any(kw in s for kw in ("code", "config", "file", "path", "source")):
            return cls.CODE_CONFIG
        if any(kw in s for kw in ("admin", "root", "system")):
            return cls.ADMIN
        if any(kw in s for kw in ("user", "human", "manual")):
            return cls.USER
        if any(kw in s for kw in ("agent", "llm", "ai", "gpt", "claude", "assistant")):
            return cls.AGENT
        return cls.EXTERNAL


class MemoryVersion(BaseModel):
    """记忆版本快照 —— 记录某条记忆在特定时间点的完整状态。"""

    version_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str  # 关联的原始记忆 ID
    version_number: int = 1  # 版本序号（从 1 开始递增）

    # 记忆内容快照
    content: str = ""
    memory_type: str = "short_term"
    priority: str = "normal"
    status: str = "active"
    source: str = ""
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    metadata: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    # 版本元信息
    created_at: datetime = Field(default_factory=datetime.now)
    created_by: str = ""  # 操作者标识（user_id / agent_id / system）
    change_description: str = ""  # 变更说明

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于 SQLite 存储）。"""
        return {
            "version_id": self.version_id,
            "memory_id": self.memory_id,
            "version_number": self.version_number,
            "content": self.content,
            "memory_type": self.memory_type,
            "priority": self.priority,
            "status": self.status,
            "source": self.source,
            "tags_json": json.dumps(self.tags, ensure_ascii=False),
            "importance": self.importance,
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False),
            "context_json": json.dumps(self.context, ensure_ascii=False),
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "change_description": self.change_description,
        }

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> MemoryVersion:
        """从 SQLite 行构造 MemoryVersion。"""
        return cls(
            version_id=row["version_id"],
            memory_id=row["memory_id"],
            version_number=row["version_number"],
            content=row["content"],
            memory_type=row["memory_type"],
            priority=row["priority"],
            status=row["status"],
            source=row["source"],
            tags=json.loads(row["tags_json"] or "[]"),
            importance=row["importance"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            context=json.loads(row["context_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"] or "",
            change_description=row["change_description"] or "",
        )


class ConflictType(str, Enum):
    """冲突类型"""

    CONTENT_CONTRADICTION = "content_contradiction"  # 内容逻辑矛盾
    PRIORITY_MISMATCH = "priority_mismatch"  # 优先级冲突
    SOURCE_CONFLICT = "source_conflict"  # 来源冲突
    TEMPORAL_CONFLICT = "temporal_conflict"  # 时序冲突
    SEMANTIC_DRIFT = "semantic_drift"  # 语义漂移（内容变化过大）


class ConflictStatus(str, Enum):
    """冲突状态"""

    PENDING = "pending"  # 待解决
    AUTO_RESOLVED = "auto_resolved"  # 自动解决
    USER_CONFIRMED = "user_confirmed"  # 用户确认
    ESCALATED = "escalated"  # 已升级（需要人工介入）
    DISMISSED = "dismissed"  # 已忽略


class ConflictInfo(BaseModel):
    """冲突信息 —— 描述两条记忆之间的冲突。"""

    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str  # 冲突涉及的记忆 ID
    old_version: Optional[MemoryVersion] = None  # 旧版本
    new_version: Optional[MemoryVersion] = None  # 新版本

    conflict_type: ConflictType = ConflictType.CONTENT_CONTRADICTION
    severity: float = 0.5  # 冲突严重程度 (0-1)
    description: str = ""  # 冲突描述

    # L3 因果推理细节
    contradiction_details: list[str] = Field(default_factory=list)

    # 状态
    status: ConflictStatus = ConflictStatus.PENDING
    detected_at: datetime = Field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "conflict_id": self.conflict_id,
            "memory_id": self.memory_id,
            "old_version_json": self.old_version.model_dump_json() if self.old_version else "null",
            "new_version_json": self.new_version.model_dump_json() if self.new_version else "null",
            "conflict_type": self.conflict_type.value,
            "severity": self.severity,
            "description": self.description,
            "contradiction_details_json": json.dumps(
                self.contradiction_details, ensure_ascii=False
            ),
            "status": self.status.value,
            "detected_at": self.detected_at.isoformat(),
        }

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> ConflictInfo:
        """从 SQLite 行构造 ConflictInfo。"""
        old_ver_data = (
            json.loads(row["old_version_json"])
            if row["old_version_json"] and row["old_version_json"] != "null"
            else None
        )
        new_ver_data = (
            json.loads(row["new_version_json"])
            if row["new_version_json"] and row["new_version_json"] != "null"
            else None
        )
        return cls(
            conflict_id=row["conflict_id"],
            memory_id=row["memory_id"],
            old_version=MemoryVersion(**old_ver_data) if old_ver_data else None,
            new_version=MemoryVersion(**new_ver_data) if new_ver_data else None,
            conflict_type=ConflictType(row["conflict_type"]),
            severity=row["severity"],
            description=row["description"] or "",
            contradiction_details=json.loads(row["contradiction_details_json"] or "[]"),
            status=ConflictStatus(row["status"]),
            detected_at=datetime.fromisoformat(row["detected_at"]),
        )


class ResolutionStrategy(str, Enum):
    """解决策略"""

    TIMESTAMP = "timestamp"  # L1: 时间戳优先
    CREDIBILITY = "credibility"  # L2: 来源可信度
    CAUSAL_REASONING = "causal_reasoning"  # L3: 因果推理
    USER_CONFIRM = "user_confirm"  # L4: 用户确认
    MERGE = "merge"  # 合并策略（取两者之长）
    KEEP_BOTH = "keep_both"  # 保留两者（标记为不同版本）


class Resolution(BaseModel):
    """冲突解决结果。"""

    resolution_id: str = Field(default_factory=lambda: str(uuid4()))
    conflict_id: str  # 关联的冲突 ID
    memory_id: str

    strategy: ResolutionStrategy = ResolutionStrategy.TIMESTAMP
    chosen_version: Optional[MemoryVersion] = None  # 被选中的版本
    merged_content: str = ""  # 合并后的内容（MERGE 策略）
    reasoning: str = ""  # 解决理由
    needs_user_confirmation: bool = False  # 是否需要用户确认

    resolved_at: datetime = Field(default_factory=datetime.now)
    resolved_by: str = ""  # system / user_id

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "resolution_id": self.resolution_id,
            "conflict_id": self.conflict_id,
            "memory_id": self.memory_id,
            "strategy": self.strategy.value,
            "chosen_version_json": self.chosen_version.model_dump_json()
            if self.chosen_version
            else "null",
            "merged_content": self.merged_content,
            "reasoning": self.reasoning,
            "needs_user_confirmation": 1 if self.needs_user_confirmation else 0,
            "resolved_at": self.resolved_at.isoformat(),
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Resolution:
        """从 SQLite 行构造 Resolution。"""
        chosen_data = (
            json.loads(row["chosen_version_json"])
            if row["chosen_version_json"] and row["chosen_version_json"] != "null"
            else None
        )
        return cls(
            resolution_id=row["resolution_id"],
            conflict_id=row["conflict_id"],
            memory_id=row["memory_id"],
            strategy=ResolutionStrategy(row["strategy"]),
            chosen_version=MemoryVersion(**chosen_data) if chosen_data else None,
            merged_content=row["merged_content"] or "",
            reasoning=row["reasoning"] or "",
            needs_user_confirmation=bool(row["needs_user_confirmation"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]),
            resolved_by=row["resolved_by"] or "",
        )


# ---------------------------------------------------------------------------
# 版本化记忆管理器
# ---------------------------------------------------------------------------


class VersionedMemory:
    """版本化记忆管理器 —— 包装 MemoryManager，为每次更新创建版本快照。

    核心能力：
    1. 每次记忆更新自动创建新版本（追加而非覆盖）
    2. 查询任意记忆的所有历史版本
    3. 回滚到指定历史版本
    4. 对比两个版本之间的差异

    版本存储使用独立的 SQLite 数据库，与 MemoryManager 的 LanceDB 存储分离。

    Usage:
        vm = VersionedMemory(memory_manager)
        await vm.initialize()

        # 保存版本（通常在 MemoryManager 更新后调用）
        await vm.save_version(memory_item, change_description="用户修改了偏好设置")

        # 查询历史
        versions = await vm.get_versions(memory_id)
        diff = vm.get_diff(memory_id, v1=1, v2=3)

        # 回滚
        rolled_back = await vm.rollback(memory_id, version=2)
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        db_path: str | Path | None = None,
    ):
        self._manager = memory_manager

        settings = get_settings()
        resolved_path = db_path or Path(settings.memory.lancedb_path) / "versioning.db"
        self._db_path = Path(resolved_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db: Optional[aiosqlite.Connection] = None
        self._initialized = False

        logger.info(f"VersionedMemory 创建: db_path={self._db_path}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 SQLite 数据库和表结构。"""
        if self._initialized:
            return

        try:
            self._db = await aiosqlite.connect(str(self._db_path))
            # 使用 Row 工厂以支持按列名访问
            self._db.row_factory = aiosqlite.Row
            # 启用 WAL 模式以提升并发性能
            await self._db.execute("PRAGMA journal_mode=WAL")
            # 启用外键约束
            await self._db.execute("PRAGMA foreign_keys=ON")

            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_versions (
                    version_id       TEXT PRIMARY KEY,
                    memory_id        TEXT NOT NULL,
                    version_number   INTEGER NOT NULL,
                    content          TEXT NOT NULL DEFAULT '',
                    memory_type      TEXT NOT NULL DEFAULT 'short_term',
                    priority         TEXT NOT NULL DEFAULT 'normal',
                    status           TEXT NOT NULL DEFAULT 'active',
                    source           TEXT NOT NULL DEFAULT '',
                    tags_json        TEXT NOT NULL DEFAULT '[]',
                    importance       REAL NOT NULL DEFAULT 0.5,
                    metadata_json    TEXT NOT NULL DEFAULT '{}',
                    context_json     TEXT NOT NULL DEFAULT '{}',
                    created_at       TEXT NOT NULL,
                    created_by       TEXT NOT NULL DEFAULT '',
                    change_description TEXT NOT NULL DEFAULT ''
                )
            """)
            # 索引：按 memory_id 和版本号查询
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_versions_memory_id
                ON memory_versions (memory_id, version_number)
            """)
            # 索引：按 memory_id 和时间排序
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_versions_memory_time
                ON memory_versions (memory_id, created_at)
            """)

            await self._db.commit()
            self._initialized = True
            logger.info("VersionedMemory 数据库初始化完成")

        except Exception as e:
            logger.error(f"VersionedMemory 初始化失败: {e}")
            raise

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
        self._initialized = False
        logger.info("VersionedMemory 已关闭")

    # ------------------------------------------------------------------
    # 版本操作
    # ------------------------------------------------------------------

    async def save_version(
        self,
        memory: MemoryItem,
        *,
        created_by: str = "system",
        change_description: str = "",
    ) -> MemoryVersion:
        """为记忆保存一个新版本快照。

        通常在 MemoryManager 完成记忆更新后调用此方法，
        将更新后的状态追加为新版本。

        Args:
            memory: 当前记忆条目（更新后的状态）
            created_by: 操作者标识
            change_description: 变更说明

        Returns:
            创建的版本快照
        """
        if not self._initialized:
            await self.initialize()

        # 计算新版本号
        version_number = await self._get_next_version_number(memory.memory_id)

        version = MemoryVersion(
            memory_id=memory.memory_id,
            version_number=version_number,
            content=memory.content,
            memory_type=memory.memory_type.value,
            priority=memory.priority.value,
            status=memory.status.value,
            source=memory.source,
            tags=memory.tags.copy(),
            importance=memory.importance,
            metadata=memory.metadata.copy(),
            context=memory.context.copy(),
            created_by=created_by,
            change_description=change_description,
        )

        row = version.to_dict()
        placeholders = ", ".join(f":{k}" for k in row)
        columns = ", ".join(row.keys())

        await self._db.execute(
            f"INSERT INTO memory_versions ({columns}) VALUES ({placeholders})",
            row,
        )
        await self._db.commit()

        logger.info(
            f"保存版本: memory_id={memory.memory_id}, version={version_number}, by={created_by}"
        )
        return version

    async def get_versions(
        self,
        memory_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryVersion]:
        """获取指定记忆的所有版本历史（按版本号升序）。

        Args:
            memory_id: 记忆 ID
            limit: 最大返回数
            offset: 跳过前 N 条

        Returns:
            版本列表（从旧到新）
        """
        if not self._initialized:
            await self.initialize()

        cursor = await self._db.execute(
            """
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY version_number ASC
            LIMIT ? OFFSET ?
            """,
            (memory_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [MemoryVersion.from_row(row) for row in rows]

    async def get_latest_version(self, memory_id: str) -> Optional[MemoryVersion]:
        """获取指定记忆的最新版本。"""
        if not self._initialized:
            await self.initialize()

        cursor = await self._db.execute(
            """
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (memory_id,),
        )
        row = await cursor.fetchone()
        return MemoryVersion.from_row(row) if row else None

    async def rollback(
        self,
        memory_id: str,
        version: int,
        *,
        created_by: str = "system",
    ) -> MemoryVersion:
        """回滚指定记忆到指定版本。

        回滚操作会：
        1. 读取目标版本的内容
        2. 创建一个新版本（版本号递增），内容为回滚目标版本的内容
        3. 不会删除中间版本（保留完整历史）

        Args:
            memory_id: 记忆 ID
            version: 目标版本号
            created_by: 操作者标识

        Returns:
            回滚后创建的新版本

        Raises:
            ValueError: 指定版本不存在
        """
        if not self._initialized:
            await self.initialize()

        # 查找目标版本
        cursor = await self._db.execute(
            "SELECT * FROM memory_versions WHERE memory_id = ? AND version_number = ?",
            (memory_id, version),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"版本不存在: memory_id={memory_id}, version={version}")

        target = MemoryVersion.from_row(row)

        # 创建回滚版本（版本号递增）
        rollback_version = MemoryVersion(
            memory_id=memory_id,
            version_number=await self._get_next_version_number(memory_id),
            content=target.content,
            memory_type=target.memory_type,
            priority=target.priority,
            status=target.status,
            source=target.source,
            tags=target.tags.copy(),
            importance=target.importance,
            metadata={**target.metadata, "rollback_from_version": target.version_number},
            context=target.context.copy(),
            created_by=created_by,
            change_description=f"回滚到版本 {version}",
        )

        insert_row = rollback_version.to_dict()
        placeholders = ", ".join(f":{k}" for k in insert_row)
        columns = ", ".join(insert_row.keys())

        await self._db.execute(
            f"INSERT INTO memory_versions ({columns}) VALUES ({placeholders})",
            insert_row,
        )
        await self._db.commit()

        logger.info(
            f"回滚完成: memory_id={memory_id}, rollback_to={version}, "
            f"new_version={rollback_version.version_number}"
        )
        return rollback_version

    def get_diff(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        versions: Optional[list[MemoryVersion]] = None,
    ) -> dict[str, Any]:
        """对比同一记忆的两个版本之间的差异。

        Args:
            memory_id: 记忆 ID
            v1: 版本号 1（旧）
            v2: 版本号 2（新）
            versions: 可选的版本列表（避免重复查询，用于同步场景）

        Returns:
            差异信息字典，包含 content_diff, metadata_changes, field_changes
        """
        # 从列表中查找版本
        ver1 = None
        ver2 = None
        if versions:
            for v in versions:
                if v.version_number == v1:
                    ver1 = v
                if v.version_number == v2:
                    ver2 = v

        if not ver1 or not ver2:
            return {
                "error": f"版本 {v1} 或 {v2} 未找到，请先调用 get_versions() 获取版本列表",
                "memory_id": memory_id,
                "v1": v1,
                "v2": v2,
            }

        # 内容差异（unified diff 格式）
        old_lines = ver1.content.splitlines(keepends=True)
        new_lines = ver2.content.splitlines(keepends=True)
        content_diff = list(
            unified_diff(
                old_lines,
                new_lines,
                fromfile=f"v{v1}",
                tofile=f"v{v2}",
                lineterm="",
            )
        )

        # 字段级变更
        field_changes: list[dict[str, Any]] = []
        for field_name in ("memory_type", "priority", "status", "source", "importance"):
            old_val = getattr(ver1, field_name)
            new_val = getattr(ver2, field_name)
            if old_val != new_val:
                field_changes.append(
                    {
                        "field": field_name,
                        "old": old_val,
                        "new": new_val,
                    }
                )

        # 标签变更
        tags_added = set(ver2.tags) - set(ver1.tags)
        tags_removed = set(ver1.tags) - set(ver2.tags)
        if tags_added or tags_removed:
            field_changes.append(
                {
                    "field": "tags",
                    "added": sorted(tags_added),
                    "removed": sorted(tags_removed),
                }
            )

        # 元数据变更
        metadata_changes: dict[str, Any] = {}
        all_keys = set(ver1.metadata.keys()) | set(ver2.metadata.keys())
        for key in all_keys:
            old_v = ver1.metadata.get(key)
            new_v = ver2.metadata.get(key)
            if old_v != new_v:
                metadata_changes[key] = {"old": old_v, "new": new_v}

        return {
            "memory_id": memory_id,
            "v1": v1,
            "v2": v2,
            "content_diff": content_diff,
            "content_changed": ver1.content != ver2.content,
            "field_changes": field_changes,
            "metadata_changes": metadata_changes,
            "v1_created_at": ver1.created_at.isoformat(),
            "v2_created_at": ver2.created_at.isoformat(),
            "v1_created_by": ver1.created_by,
            "v2_created_by": ver2.created_by,
        }

    async def get_version_count(self, memory_id: str) -> int:
        """获取指定记忆的版本总数。"""
        if not self._initialized:
            await self.initialize()

        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM memory_versions WHERE memory_id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def list_tracked_memories(self) -> list[str]:
        """列出所有有版本历史的 memory_id。"""
        if not self._initialized:
            await self.initialize()

        cursor = await self._db.execute(
            "SELECT DISTINCT memory_id FROM memory_versions ORDER BY memory_id"
        )
        rows = await cursor.fetchall()
        return [row["memory_id"] for row in rows]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _get_next_version_number(self, memory_id: str) -> int:
        """计算下一个版本号。"""
        cursor = await self._db.execute(
            "SELECT MAX(version_number) FROM memory_versions WHERE memory_id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        max_ver = row[0] if row and row[0] is not None else 0
        return max_ver + 1


# ---------------------------------------------------------------------------
# 冲突解决器
# ---------------------------------------------------------------------------

# L3 因果推理使用的否定/矛盾模式
_NEGATION_PATTERNS = [
    # 中文否定
    (r"不(?:是|会|能|应该|可以|需要)", r"(?:是|会|能|应该|可以|需要)"),
    (r"没(?:有|法)", r"(?:有|法)"),
    (r"非", r"是"),
    (r"禁止", r"(?:允许|启用|开启)"),
    (r"关闭", r"(?:打开|启用|开启)"),
    (r"禁用", r"(?:启用|激活)"),
    # 英文否定
    (r"\bnot\b", r"\b(is|are|was|were|can|should|will|must)\b"),
    (r"\bnever\b", r"\b(always|often|sometimes)\b"),
    (r"\bno\b", r"\b(yes|some|many)\b"),
    (r"\bdisable[ds]?\b", r"\b(enabl(?:e[ds]?|ing))\b"),
    (r"\bfalse\b", r"\btrue\b"),
    (r"\boff\b", r"\bon\b"),
]


class ConflictResolver:
    """4 层冲突解决器。

    解决流程（按层级递进，每层解决不了则进入下一层）：
    - L1: 时间戳优先 —— 最新版本通常更准确
    - L2: 来源可信度 —— 代码/配置 > 管理员 > 用户 > 代理 > 外部
    - L3: 因果推理 —— 检测内容中的逻辑矛盾
    - L4: 用户确认 —— 将无法自动解决的冲突标记为待人工审核

    Usage:
        resolver = ConflictResolver()
        await resolver.initialize()

        # 检测冲突
        conflict = resolver.detect_conflict(old_memory, new_memory)

        # 解决冲突
        resolution = await resolver.resolve(conflict, strategy=ResolutionStrategy.TIMESTAMP)
    """

    def __init__(self, db_path: str | Path | None = None):
        settings = get_settings()
        resolved_path = db_path or Path(settings.memory.lancedb_path) / "conflicts.db"
        self._db_path = Path(resolved_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db: Optional[aiosqlite.Connection] = None
        self._initialized = False

        logger.info(f"ConflictResolver 创建: db_path={self._db_path}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 SQLite 数据库和表结构。"""
        if self._initialized:
            return

        try:
            self._db = await aiosqlite.connect(str(self._db_path))
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")

            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS conflicts (
                    conflict_id                TEXT PRIMARY KEY,
                    memory_id                  TEXT NOT NULL,
                    old_version_json           TEXT,
                    new_version_json           TEXT,
                    conflict_type              TEXT NOT NULL,
                    severity                   REAL NOT NULL DEFAULT 0.5,
                    description                TEXT NOT NULL DEFAULT '',
                    contradiction_details_json TEXT NOT NULL DEFAULT '[]',
                    status                     TEXT NOT NULL DEFAULT 'pending',
                    detected_at                TEXT NOT NULL
                )
            """)

            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS resolutions (
                    resolution_id          TEXT PRIMARY KEY,
                    conflict_id            TEXT NOT NULL,
                    memory_id              TEXT NOT NULL,
                    strategy               TEXT NOT NULL,
                    chosen_version_json    TEXT,
                    merged_content         TEXT NOT NULL DEFAULT '',
                    reasoning              TEXT NOT NULL DEFAULT '',
                    needs_user_confirmation INTEGER NOT NULL DEFAULT 0,
                    resolved_at            TEXT NOT NULL,
                    resolved_by            TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (conflict_id) REFERENCES conflicts(conflict_id)
                )
            """)

            # 索引
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_conflicts_memory
                ON conflicts (memory_id, status)
            """)
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_resolutions_conflict
                ON resolutions (conflict_id)
            """)
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_resolutions_memory
                ON resolutions (memory_id)
            """)

            await self._db.commit()
            self._initialized = True
            logger.info("ConflictResolver 数据库初始化完成")

        except Exception as e:
            logger.error(f"ConflictResolver 初始化失败: {e}")
            raise

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._db:
            await self._db.close()
            self._db = None
        self._initialized = False
        logger.info("ConflictResolver 已关闭")

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------

    def detect_conflict(
        self,
        old_memory: MemoryItem | MemoryVersion,
        new_memory: MemoryItem | MemoryVersion,
    ) -> Optional[ConflictInfo]:
        """检测两条记忆之间是否存在冲突（L1 + L3 层面）。

        检测逻辑：
        1. 比较内容是否发生显著变化
        2. 检测内容中的逻辑矛盾（否定模式匹配）
        3. 评估冲突严重程度

        Args:
            old_memory: 旧记忆（可以是 MemoryItem 或 MemoryVersion）
            new_memory: 新记忆（可以是 MemoryItem 或 MemoryVersion）

        Returns:
            ConflictInfo（检测到冲突时）或 None（无冲突）
        """
        old_content = old_memory.content.strip()
        new_content = new_memory.content.strip()

        # 内容完全相同，无冲突
        if old_content == new_content:
            return None

        # 提取标准化字段
        old_source = getattr(old_memory, "source", "")
        new_source = getattr(new_memory, "source", "")
        old_importance = getattr(old_memory, "importance", 0.5)
        new_importance = getattr(new_memory, "importance", 0.5)
        memory_id = getattr(old_memory, "memory_id", "")

        # 构建旧版本快照（如果输入是 MemoryItem）
        old_version = self._to_version(old_memory)
        new_version = self._to_version(new_memory)

        contradictions: list[str] = []
        conflict_type = ConflictType.CONTENT_CONTRADICTION
        severity = 0.0

        # --- L3: 因果推理：检测内容中的逻辑矛盾 ---
        contradiction_severity = self._detect_contradictions(old_content, new_content)
        if contradiction_severity > 0:
            contradictions.append(f"内容存在逻辑矛盾（矛盾度: {contradiction_severity:.2f}）")
            severity = max(severity, contradiction_severity)

        # --- 检测语义漂移（内容变化过大）---
        content_similarity = self._content_similarity(old_content, new_content)
        if content_similarity < 0.3 and len(old_content) > 20 and len(new_content) > 20:
            conflict_type = ConflictType.SEMANTIC_DRIFT
            contradictions.append(
                f"内容变化过大（相似度: {content_similarity:.2f}），可能为语义漂移"
            )
            severity = max(severity, 1.0 - content_similarity)

        # --- 检测来源冲突 ---
        if old_source and new_source and old_source != new_source:
            old_cred = CredibilitySource.from_source_string(old_source)
            new_cred = CredibilitySource.from_source_string(new_source)
            if abs(old_cred.weight - new_cred.weight) >= 2:
                conflict_type = ConflictType.SOURCE_CONFLICT
                contradictions.append(
                    f"来源可信度差异较大: {old_source}({old_cred.weight}) "
                    f"vs {new_source}({new_cred.weight})"
                )
                severity = max(severity, abs(old_cred.weight - new_cred.weight) / 5.0)

        # --- 检测优先级冲突 ---
        if abs(old_importance - new_importance) > 0.4:
            conflict_type = ConflictType.PRIORITY_MISMATCH
            contradictions.append(f"重要性差异过大: {old_importance:.2f} vs {new_importance:.2f}")
            severity = max(severity, abs(old_importance - new_importance))

        # 无显著冲突
        if severity < 0.1:
            return None

        conflict = ConflictInfo(
            memory_id=memory_id,
            old_version=old_version,
            new_version=new_version,
            conflict_type=conflict_type,
            severity=min(severity, 1.0),
            description=(f"检测到 {conflict_type.value} 类型冲突: 严重程度 {severity:.2f}"),
            contradiction_details=contradictions,
        )

        logger.info(
            f"冲突检测: memory_id={memory_id}, type={conflict_type.value}, severity={severity:.2f}"
        )
        return conflict

    # ------------------------------------------------------------------
    # 冲突解决
    # ------------------------------------------------------------------

    async def resolve(
        self,
        conflict: ConflictInfo,
        strategy: Optional[ResolutionStrategy] = None,
    ) -> Resolution:
        """解决冲突。

        如果未指定策略，则自动按 L1 -> L2 -> L3 -> L4 逐层尝试。

        Args:
            conflict: 冲突信息
            strategy: 指定解决策略（None 则自动选择）

        Returns:
            解决结果
        """
        if not self._initialized:
            await self.initialize()

        # 持久化冲突记录
        await self._persist_conflict(conflict)

        # 自动选择策略
        if strategy is None:
            strategy = self._select_strategy(conflict)

        resolution: Resolution

        if strategy == ResolutionStrategy.TIMESTAMP:
            resolution = self._resolve_by_timestamp(conflict)
        elif strategy == ResolutionStrategy.CREDIBILITY:
            resolution = self._resolve_by_credibility(conflict)
        elif strategy == ResolutionStrategy.CAUSAL_REASONING:
            resolution = self._resolve_by_causal(conflict)
        elif strategy == ResolutionStrategy.MERGE:
            resolution = self._resolve_by_merge(conflict)
        elif strategy == ResolutionStrategy.KEEP_BOTH:
            resolution = self._resolve_keep_both(conflict)
        else:
            # USER_CONFIRM 或其他未实现的策略 -> 升级到用户确认
            resolution = self._resolve_user_confirm(conflict)

        # 持久化解 决结果
        await self._persist_resolution(resolution)

        # 更新冲突状态
        new_status = (
            ConflictStatus.USER_CONFIRMED
            if resolution.needs_user_confirmation
            else ConflictStatus.AUTO_RESOLVED
        )
        await self._db.execute(
            "UPDATE conflicts SET status = ? WHERE conflict_id = ?",
            (new_status.value, conflict.conflict_id),
        )
        await self._db.commit()

        logger.info(
            f"冲突解决: conflict_id={conflict.conflict_id}, "
            f"strategy={strategy.value}, needs_confirm={resolution.needs_user_confirmation}"
        )
        return resolution

    async def get_pending_conflicts(self, memory_id: Optional[str] = None) -> list[ConflictInfo]:
        """获取待解决的冲突列表。"""
        if not self._initialized:
            await self.initialize()

        if memory_id:
            cursor = await self._db.execute(
                "SELECT * FROM conflicts WHERE status = 'pending' AND memory_id = ? ORDER BY detected_at DESC",
                (memory_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM conflicts WHERE status = 'pending' ORDER BY detected_at DESC"
            )
        rows = await cursor.fetchall()
        return [ConflictInfo.from_row(row) for row in rows]

    async def get_resolution(self, conflict_id: str) -> Optional[Resolution]:
        """获取指定冲突的解决结果。"""
        if not self._initialized:
            await self.initialize()

        cursor = await self._db.execute(
            "SELECT * FROM resolutions WHERE conflict_id = ? ORDER BY resolved_at DESC LIMIT 1",
            (conflict_id,),
        )
        row = await cursor.fetchone()
        return Resolution.from_row(row) if row else None

    async def confirm_resolution(
        self,
        conflict_id: str,
        user_id: str = "user",
        override_version: Optional[MemoryVersion] = None,
    ) -> Resolution:
        """用户确认冲突解决结果（L4 层）。

        Args:
            conflict_id: 冲突 ID
            user_id: 确认用户 ID
            override_version: 用户选择覆盖的版本（可选）

        Returns:
            更新后的解决结果
        """
        if not self._initialized:
            await self.initialize()

        # 获取现有解决结果
        resolution = await self.get_resolution(conflict_id)
        if not resolution:
            raise ValueError(f"未找到冲突解决记录: {conflict_id}")

        # 如果用户提供了覆盖版本
        if override_version:
            resolution.chosen_version = override_version
            resolution.reasoning += (
                f"\n用户 {user_id} 手动选择了版本 {override_version.version_number}"
            )

        resolution.needs_user_confirmation = False
        resolution.resolved_by = user_id

        # 更新冲突状态
        await self._db.execute(
            "UPDATE conflicts SET status = 'user_confirmed' WHERE conflict_id = ?",
            (conflict_id,),
        )

        # 更新解决记录
        await self._db.execute(
            """
            UPDATE resolutions
            SET chosen_version_json = ?,
                reasoning = ?,
                needs_user_confirmation = 0,
                resolved_by = ?
            WHERE conflict_id = ?
            """,
            (
                resolution.chosen_version.model_dump_json()
                if resolution.chosen_version
                else "null",
                resolution.reasoning,
                user_id,
                conflict_id,
            ),
        )
        await self._db.commit()

        logger.info(f"用户确认冲突解决: conflict_id={conflict_id}, user={user_id}")
        return resolution

    # ------------------------------------------------------------------
    # L1: 时间戳优先
    # ------------------------------------------------------------------

    def _resolve_by_timestamp(self, conflict: ConflictInfo) -> Resolution:
        """L1: 时间戳优先 —— 最新版本通常更准确。"""
        chosen = conflict.new_version  # 新版本优先
        if not chosen:
            chosen = conflict.old_version

        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.TIMESTAMP,
            chosen_version=chosen,
            reasoning=(
                f"L1 时间戳优先策略: 选择较新版本 "
                f"(created_at={chosen.created_at.isoformat() if chosen else 'N/A'})"
            ),
            needs_user_confirmation=conflict.severity > 0.8,
        )

    # ------------------------------------------------------------------
    # L2: 来源可信度
    # ------------------------------------------------------------------

    def _resolve_by_credibility(self, conflict: ConflictInfo) -> Resolution:
        """L2: 来源可信度 —— 高可信度来源优先。"""
        old_ver = conflict.old_version
        new_ver = conflict.new_version

        old_cred = CredibilitySource.from_source_string(old_ver.source if old_ver else "")
        new_cred = CredibilitySource.from_source_string(new_ver.source if new_ver else "")

        if new_cred.weight > old_cred.weight:
            chosen = new_ver
            reasoning = (
                f"L2 来源可信度策略: 新版本来源 {new_ver.source if new_ver else 'N/A'} "
                f"(权重 {new_cred.weight}) > 旧版本来源 "
                f"{old_ver.source if old_ver else 'N/A'} (权重 {old_cred.weight})"
            )
        elif old_cred.weight > new_cred.weight:
            chosen = old_ver
            reasoning = (
                f"L2 来源可信度策略: 旧版本来源 {old_ver.source if old_ver else 'N/A'} "
                f"(权重 {old_cred.weight}) > 新版本来源 "
                f"{new_ver.source if new_ver else 'N/A'} (权重 {new_cred.weight})"
            )
        else:
            # 可信度相同，回退到时间戳
            chosen = new_ver
            reasoning = (
                f"L2 来源可信度策略: 两者可信度相同({old_cred.weight}), 回退到时间戳优先选择新版本"
            )

        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.CREDIBILITY,
            chosen_version=chosen,
            reasoning=reasoning,
            needs_user_confirmation=conflict.severity > 0.85,
        )

    # ------------------------------------------------------------------
    # L3: 因果推理
    # ------------------------------------------------------------------

    def _resolve_by_causal(self, conflict: ConflictInfo) -> Resolution:
        """L3: 因果推理 —— 分析矛盾内容的逻辑关系。"""
        old_ver = conflict.old_version
        new_ver = conflict.new_version

        # 如果没有矛盾细节，认为无需特殊推理
        if not conflict.contradiction_details:
            return Resolution(
                conflict_id=conflict.conflict_id,
                memory_id=conflict.memory_id,
                strategy=ResolutionStrategy.CAUSAL_REASONING,
                chosen_version=new_ver,
                reasoning="L3 因果推理: 未检测到逻辑矛盾，选择新版本",
                needs_user_confirmation=False,
            )

        # 分析矛盾严重程度
        # 如果新版本更长且包含更多具体信息，倾向于新版本
        old_len = len(old_ver.content) if old_ver else 0
        new_len = len(new_ver.content) if new_ver else 0

        # 信息量更大的版本通常更有价值
        if new_len > old_len * 1.5:
            chosen = new_ver
            reasoning = (
                f"L3 因果推理: 新版本信息量更大({new_len} > {old_len} chars), "
                f"矛盾详情: {'; '.join(conflict.contradiction_details)}"
            )
        elif old_len > new_len * 1.5:
            chosen = old_ver
            reasoning = (
                f"L3 因果推理: 旧版本信息量更大({old_len} > {new_len} chars), "
                f"矛盾详情: {'; '.join(conflict.contradiction_details)}"
            )
        else:
            # 信息量相当，需要用户确认
            chosen = new_ver
            reasoning = (
                f"L3 因果推理: 检测到逻辑矛盾且信息量相当，"
                f"暂时选择新版本，建议用户确认。"
                f"矛盾详情: {'; '.join(conflict.contradiction_details)}"
            )

        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.CAUSAL_REASONING,
            chosen_version=chosen,
            reasoning=reasoning,
            # 有逻辑矛盾时，高严重程度需要用户确认
            needs_user_confirmation=conflict.severity > 0.6,
        )

    # ------------------------------------------------------------------
    # L4: 用户确认
    # ------------------------------------------------------------------

    def _resolve_user_confirm(self, conflict: ConflictInfo) -> Resolution:
        """L4: 用户确认 —— 将冲突标记为待人工审核。"""
        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.USER_CONFIRM,
            chosen_version=conflict.new_version,  # 临时选择新版本
            reasoning=(
                f"L4 用户确认: 冲突严重程度 {conflict.severity:.2f}，"
                f"需要人工介入确认。"
                f"矛盾详情: {'; '.join(conflict.contradiction_details) or '无'}"
            ),
            needs_user_confirmation=True,
        )

    # ------------------------------------------------------------------
    # 辅助解决策略
    # ------------------------------------------------------------------

    def _resolve_by_merge(self, conflict: ConflictInfo) -> Resolution:
        """合并策略 —— 尝试合并两个版本的内容。"""
        old_content = conflict.old_version.content if conflict.old_version else ""
        new_content = conflict.new_version.content if conflict.new_version else ""

        # 简单合并：去重后拼接
        old_sentences = set(self._split_sentences(old_content))
        new_sentences = set(self._split_sentences(new_content))

        # 保留两者独有的 + 新版本中共有的
        merged_sentences = list(old_sentences - new_sentences) + list(new_sentences)
        merged_content = " ".join(merged_sentences)

        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.MERGE,
            chosen_version=conflict.new_version,
            merged_content=merged_content,
            reasoning=(
                f"合并策略: 保留旧版本独有内容({len(old_sentences - new_sentences)}句) "
                f"+ 新版本全部内容({len(new_sentences)}句)"
            ),
            needs_user_confirmation=conflict.severity > 0.7,
        )

    def _resolve_keep_both(self, conflict: ConflictInfo) -> Resolution:
        """保留两者策略 —— 两个版本都保留。"""
        return Resolution(
            conflict_id=conflict.conflict_id,
            memory_id=conflict.memory_id,
            strategy=ResolutionStrategy.KEEP_BOTH,
            chosen_version=conflict.new_version,
            reasoning=(
                f"保留两者策略: 两个版本都保留为不同版本。"
                f"旧版本: v{conflict.old_version.version_number if conflict.old_version else 'N/A'}, "
                f"新版本: v{conflict.new_version.version_number if conflict.new_version else 'N/A'}"
            ),
            needs_user_confirmation=False,
        )

    # ------------------------------------------------------------------
    # 策略自动选择
    # ------------------------------------------------------------------

    def _select_strategy(self, conflict: ConflictInfo) -> ResolutionStrategy:
        """自动选择解决策略（L1 -> L2 -> L3 -> L4 逐层尝试）。"""
        # L1: 低严重程度直接用时间戳
        if conflict.severity < 0.3:
            return ResolutionStrategy.TIMESTAMP

        # L2: 来源可信度差异明显
        old_source = conflict.old_version.source if conflict.old_version else ""
        new_source = conflict.new_version.source if conflict.new_version else ""
        if old_source and new_source and old_source != new_source:
            old_cred = CredibilitySource.from_source_string(old_source)
            new_cred = CredibilitySource.from_source_string(new_source)
            if abs(old_cred.weight - new_cred.weight) >= 2:
                return ResolutionStrategy.CREDIBILITY

        # L3: 有逻辑矛盾
        if conflict.contradiction_details:
            if conflict.severity < 0.7:
                return ResolutionStrategy.CAUSAL_REASONING

        # L4: 高严重程度 -> 用户确认
        if conflict.severity >= 0.7:
            return ResolutionStrategy.USER_CONFIRM

        # 默认回退到时间戳
        return ResolutionStrategy.TIMESTAMP

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _detect_contradictions(self, old_text: str, new_text: str) -> float:
        """检测两段文本之间的逻辑矛盾（L3 核心逻辑）。

        使用否定模式匹配来检测一个文本中的肯定陈述
        是否被另一个文本中的否定陈述所反驳。

        Returns:
            矛盾严重程度 (0.0 ~ 1.0)
        """
        if not old_text or not new_text:
            return 0.0

        old_lower = old_text.lower()
        new_lower = new_text.lower()

        contradiction_score = 0.0
        max_checks = 0

        for neg_pattern, pos_pattern in _NEGATION_PATTERNS:
            # 检查 old 中有否定，new 中有对应的肯定
            neg_in_old = bool(re.search(neg_pattern, old_lower))
            pos_in_new = bool(re.search(pos_pattern, new_lower))
            if neg_in_old and pos_in_new:
                contradiction_score += 1

            # 检查 old 中有肯定，new 中有对应的否定
            neg_in_new = bool(re.search(neg_pattern, new_lower))
            pos_in_old = bool(re.search(pos_pattern, old_lower))
            if neg_in_new and pos_in_old:
                contradiction_score += 1

            max_checks += 2

        if max_checks == 0:
            return 0.0

        # 归一化到 0-1
        return min(contradiction_score / 3.0, 1.0)  # 3 个矛盾点达到最大

    @staticmethod
    def _content_similarity(text1: str, text2: str) -> float:
        """计算两段文本的简单相似度（基于字符级 Jaccard）。"""
        if not text1 or not text2:
            return 0.0

        # 使用 2-gram 字符集
        def ngrams(text: str, n: int = 2) -> set[str]:
            return {text[i : i + n] for i in range(len(text) - n + 1)}

        set1 = ngrams(text1.lower())
        set2 = ngrams(text2.lower())

        if not set1 or not set2:
            return 0.0

        intersection = set1 & set2
        union = set1 | set2
        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def _to_version(memory: MemoryItem | MemoryVersion) -> MemoryVersion:
        """将 MemoryItem 或 MemoryVersion 统一转换为 MemoryVersion。"""
        if isinstance(memory, MemoryVersion):
            return memory
        return MemoryVersion(
            memory_id=memory.memory_id,
            content=memory.content,
            memory_type=memory.memory_type.value,
            priority=memory.priority.value,
            status=memory.status.value,
            source=memory.source,
            tags=memory.tags.copy(),
            importance=memory.importance,
            metadata=memory.metadata.copy(),
            context=memory.context.copy(),
        )

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """将文本按句子拆分。"""
        # 中英文句子分隔
        sentences = re.split(r"[。！？.!?\n]+", text)
        return [s.strip() for s in sentences if s.strip()]

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    async def _persist_conflict(self, conflict: ConflictInfo) -> None:
        """持久化冲突记录。"""
        row = conflict.to_dict()
        placeholders = ", ".join(f":{k}" for k in row)
        columns = ", ".join(row.keys())

        await self._db.execute(
            f"INSERT OR REPLACE INTO conflicts ({columns}) VALUES ({placeholders})",
            row,
        )
        await self._db.commit()

    async def _persist_resolution(self, resolution: Resolution) -> None:
        """持久化解 决结果。"""
        row = resolution.to_dict()
        placeholders = ", ".join(f":{k}" for k in row)
        columns = ", ".join(row.keys())

        await self._db.execute(
            f"INSERT OR REPLACE INTO resolutions ({columns}) VALUES ({placeholders})",
            row,
        )
        await self._db.commit()
