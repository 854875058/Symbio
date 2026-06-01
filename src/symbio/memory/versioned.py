"""Versioned Memory - 带版本控制的记忆系统

支持版本创建、回滚、差异比较和冲突检测。
"""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("memory.versioned")


class ConflictType(str, Enum):
    """冲突类型"""
    TIMESTAMP = "timestamp"      # 时间戳冲突
    CONTENT = "content"          # 内容冲突
    CONCURRENT = "concurrent"    # 并发修改冲突


class ConflictResolution(str, Enum):
    """冲突解决策略"""
    ACCEPT_MINE = "accept_mine"
    ACCEPT_THEIRS = "accept_theirs"
    MERGE = "merge"
    MANUAL = "manual"


class MemoryVersion(BaseModel):
    """记忆版本"""
    version_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str = ""
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    parent_version_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    created_by: str = ""
    change_description: str = ""


class VersionDiff(BaseModel):
    """版本差异"""
    diff_id: str = Field(default_factory=lambda: str(uuid4()))
    from_version_id: str = ""
    to_version_id: str = ""
    content_changed: bool = False
    metadata_changed: bool = False
    tags_changed: bool = False
    importance_changed: bool = False
    changes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class ConflictRecord(BaseModel):
    """冲突记录"""
    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str = ""
    conflict_type: ConflictType = ConflictType.CONTENT
    local_version: MemoryVersion = Field(default_factory=MemoryVersion)
    remote_version: MemoryVersion = Field(default_factory=MemoryVersion)
    resolution: Optional[ConflictResolution] = None
    resolved_version: Optional[MemoryVersion] = None
    detected_at: datetime = Field(default_factory=datetime.now)


class VersionedMemory:
    """带版本控制的记忆管理器

    支持：
    - 版本创建与追踪
    - 版本回滚
    - 版本差异比较
    - 冲突检测与解决
    """

    def __init__(self):
        # memory_id -> list of versions (chronological)
        self._versions: dict[str, list[MemoryVersion]] = {}
        # memory_id -> current version index
        self._current_index: dict[str, int] = {}
        # conflict records
        self._conflicts: list[ConflictRecord] = []
        logger.info("VersionedMemory 创建")

    def create_version(
        self,
        memory_id: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        importance: float = 0.5,
        created_by: str = "",
        change_description: str = "",
    ) -> MemoryVersion:
        """创建新版本

        Args:
            memory_id: 记忆 ID
            content: 记忆内容
            metadata: 元数据
            tags: 标签
            importance: 重要性
            created_by: 创建者
            change_description: 变更描述

        Returns:
            新创建的版本
        """
        parent_version_id = ""
        if memory_id in self._versions and self._versions[memory_id]:
            current_idx = self._current_index.get(memory_id, len(self._versions[memory_id]) - 1)
            parent_version_id = self._versions[memory_id][current_idx].version_id

        version = MemoryVersion(
            memory_id=memory_id,
            content=content,
            metadata=metadata or {},
            tags=tags or [],
            importance=importance,
            parent_version_id=parent_version_id,
            created_by=created_by,
            change_description=change_description,
        )

        if memory_id not in self._versions:
            self._versions[memory_id] = []

        self._versions[memory_id].append(version)
        self._current_index[memory_id] = len(self._versions[memory_id]) - 1

        logger.info(
            f"创建版本: memory_id={memory_id}, version_id={version.version_id}, "
            f"total_versions={len(self._versions[memory_id])}"
        )
        return version

    def get_current(self, memory_id: str) -> Optional[MemoryVersion]:
        """获取当前版本"""
        if memory_id not in self._versions:
            return None
        versions = self._versions[memory_id]
        if not versions:
            return None
        idx = self._current_index.get(memory_id, len(versions) - 1)
        return versions[idx]

    def get_version(self, memory_id: str, version_id: str) -> Optional[MemoryVersion]:
        """获取指定版本"""
        if memory_id not in self._versions:
            return None
        for v in self._versions[memory_id]:
            if v.version_id == version_id:
                return v
        return None

    def get_history(self, memory_id: str) -> list[MemoryVersion]:
        """获取版本历史"""
        return list(self._versions.get(memory_id, []))

    def rollback(
        self,
        memory_id: str,
        version_id: str,
    ) -> Optional[MemoryVersion]:
        """回滚到指定版本

        Args:
            memory_id: 记忆 ID
            version_id: 目标版本 ID

        Returns:
            回滚后的版本，失败返回 None
        """
        if memory_id not in self._versions:
            logger.warning(f"回滚失败: memory_id={memory_id} 不存在")
            return None

        for i, v in enumerate(self._versions[memory_id]):
            if v.version_id == version_id:
                self._current_index[memory_id] = i
                logger.info(f"回滚成功: memory_id={memory_id} -> version_id={version_id}")
                return v

        logger.warning(f"回滚失败: version_id={version_id} 不存在")
        return None

    def diff(
        self,
        memory_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> Optional[VersionDiff]:
        """比较两个版本的差异

        Args:
            memory_id: 记忆 ID
            from_version_id: 源版本 ID
            to_version_id: 目标版本 ID

        Returns:
            版本差异，版本不存在返回 None
        """
        from_version = self.get_version(memory_id, from_version_id)
        to_version = self.get_version(memory_id, to_version_id)

        if from_version is None or to_version is None:
            return None

        changes: dict[str, Any] = {}
        content_changed = from_version.content != to_version.content
        metadata_changed = from_version.metadata != to_version.metadata
        tags_changed = set(from_version.tags) != set(to_version.tags)
        importance_changed = from_version.importance != to_version.importance

        if content_changed:
            changes["content"] = {"from": from_version.content, "to": to_version.content}
        if metadata_changed:
            changes["metadata"] = {"from": from_version.metadata, "to": to_version.metadata}
        if tags_changed:
            changes["tags"] = {
                "added": list(set(to_version.tags) - set(from_version.tags)),
                "removed": list(set(from_version.tags) - set(to_version.tags)),
            }
        if importance_changed:
            changes["importance"] = {"from": from_version.importance, "to": to_version.importance}

        return VersionDiff(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            content_changed=content_changed,
            metadata_changed=metadata_changed,
            tags_changed=tags_changed,
            importance_changed=importance_changed,
            changes=changes,
        )

    def detect_conflict(
        self,
        memory_id: str,
        local_version: MemoryVersion,
        remote_version: MemoryVersion,
    ) -> Optional[ConflictRecord]:
        """检测两个版本之间的冲突

        Args:
            memory_id: 记忆 ID
            local_version: 本地版本
            remote_version: 远程版本

        Returns:
            冲突记录，无冲突返回 None
        """
        # 检查是否基于同一父版本（并发修改）
        if (local_version.parent_version_id and
                local_version.parent_version_id == remote_version.parent_version_id):
            conflict = ConflictRecord(
                memory_id=memory_id,
                conflict_type=ConflictType.CONCURRENT,
                local_version=local_version,
                remote_version=remote_version,
            )
            self._conflicts.append(conflict)
            logger.warning(f"检测到并发冲突: memory_id={memory_id}")
            return conflict

        # 内容冲突
        if (local_version.content != remote_version.content and
                local_version.version_id != remote_version.version_id):
            conflict = ConflictRecord(
                memory_id=memory_id,
                conflict_type=ConflictType.CONTENT,
                local_version=local_version,
                remote_version=remote_version,
            )
            self._conflicts.append(conflict)
            logger.warning(f"检测到内容冲突: memory_id={memory_id}")
            return conflict

        return None

    def resolve_conflict(
        self,
        conflict_id: str,
        resolution: ConflictResolution,
        merged_content: Optional[str] = None,
    ) -> Optional[MemoryVersion]:
        """解决冲突

        Args:
            conflict_id: 冲突 ID
            resolution: 解决策略
            merged_content: 合并内容（仅 MERGE 策略需要）

        Returns:
            解决后的版本
        """
        conflict = None
        for c in self._conflicts:
            if c.conflict_id == conflict_id:
                conflict = c
                break

        if conflict is None:
            return None

        conflict.resolution = resolution

        if resolution == ConflictResolution.ACCEPT_MINE:
            resolved = conflict.local_version.model_copy()
        elif resolution == ConflictResolution.ACCEPT_THEIRS:
            resolved = conflict.remote_version.model_copy()
        elif resolution == ConflictResolution.MERGE:
            content = merged_content or (
                conflict.local_version.content + "\n---\n" + conflict.remote_version.content
            )
            resolved = conflict.local_version.model_copy(update={"content": content})
        else:
            return None

        resolved.version_id = str(uuid4())
        resolved.created_at = datetime.now()
        resolved.change_description = f"Conflict resolved: {resolution.value}"
        conflict.resolved_version = resolved

        logger.info(f"冲突已解决: conflict_id={conflict_id}, strategy={resolution.value}")
        return resolved

    def get_conflicts(self, memory_id: Optional[str] = None) -> list[ConflictRecord]:
        """获取冲突记录"""
        if memory_id:
            return [c for c in self._conflicts if c.memory_id == memory_id]
        return list(self._conflicts)

    @property
    def total_memories(self) -> int:
        """管理的记忆总数"""
        return len(self._versions)

    @property
    def total_versions(self) -> int:
        """所有记忆的版本总数"""
        return sum(len(v) for v in self._versions.values())
