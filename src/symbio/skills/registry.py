"""Skills 注册中心 - Skill 注册、发现与生命周期管理"""

from __future__ import annotations

import threading
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("skills.registry")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class SkillStatus(str, Enum):
    """Skill 状态"""
    REGISTERED = "registered"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEPRECATED = "deprecated"
    ERROR = "error"


class SkillType(str, Enum):
    """Skill 类型"""
    TOOL = "tool"           # 工具型 Skill
    AGENT = "agent"         # Agent 型 Skill
    WORKFLOW = "workflow"   # 工作流型 Skill
    INTEGRATION = "integration"  # 集成型 Skill
    CUSTOM = "custom"


class SkillDependency(BaseModel):
    """Skill 依赖"""
    name: str
    version: str = ""
    optional: bool = False


class SkillRegistration(BaseModel):
    """Skill 注册信息"""
    skill_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    display_name: str = ""
    description: str = ""
    version: str = "1.0.0"
    skill_type: SkillType = SkillType.CUSTOM
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    dependencies: list[SkillDependency] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    status: SkillStatus = SkillStatus.REGISTERED
    entry_point: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class SkillQuery(BaseModel):
    """Skill 查询条件"""
    name: Optional[str] = None
    skill_type: Optional[SkillType] = None
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    status: Optional[SkillStatus] = None
    author: Optional[str] = None
    source: Optional[str] = None  # 来源标识


# ---------------------------------------------------------------------------
# Skill 注册中心
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Skill 注册中心

    提供 Skill 的注册、发现、更新和注销功能。

    用法:
        registry = SkillRegistry()
        registry.register(SkillRegistration(
            name="code_review",
            description="代码审查 Skill",
            skill_type=SkillType.TOOL,
        ))
        skills = registry.discover(capabilities=["code_review"])
    """

    def __init__(self):
        self._skills: dict[str, SkillRegistration] = {}
        self._name_index: dict[str, str] = {}  # name -> skill_id
        self._tag_index: dict[str, set[str]] = {}  # tag -> {skill_ids}
        self._capability_index: dict[str, set[str]] = {}  # capability -> {skill_ids}
        self._event_handlers: dict[str, list[Callable[..., None]]] = {}
        self._lock = threading.RLock()

    def register(self, registration: SkillRegistration) -> SkillRegistration:
        """注册 Skill

        Args:
            registration: Skill 注册信息

        Returns:
            注册后的 Skill 信息

        Raises:
            ValueError: Skill 名称已存在
        """
        with self._lock:
            if registration.name in self._name_index:
                existing_id = self._name_index[registration.name]
                existing = self._skills[existing_id]
                if existing.version == registration.version:
                    raise ValueError(f"Skill 已存在: {registration.name} v{registration.version}")

            # 存储注册信息
            self._skills[registration.skill_id] = registration
            self._name_index[registration.name] = registration.skill_id

            # 更新索引
            for tag in registration.tags:
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].add(registration.skill_id)

            for cap in registration.capabilities:
                if cap not in self._capability_index:
                    self._capability_index[cap] = set()
                self._capability_index[cap].add(registration.skill_id)

        self._emit_event("registered", registration)
        logger.info(f"Skill 注册: {registration.name} v{registration.version} (id={registration.skill_id})")
        return registration

    def unregister(self, skill_id: str) -> bool:
        """注销 Skill

        Args:
            skill_id: Skill ID

        Returns:
            是否成功注销
        """
        with self._lock:
            registration = self._skills.pop(skill_id, None)
            if not registration:
                return False

            self._name_index.pop(registration.name, None)

            # 清理索引
            for tag in registration.tags:
                if tag in self._tag_index:
                    self._tag_index[tag].discard(skill_id)
                    if not self._tag_index[tag]:
                        del self._tag_index[tag]

            for cap in registration.capabilities:
                if cap in self._capability_index:
                    self._capability_index[cap].discard(skill_id)
                    if not self._capability_index[cap]:
                        del self._capability_index[cap]

        self._emit_event("unregistered", registration)
        logger.info(f"Skill 注销: {registration.name} (id={skill_id})")
        return True

    def get(self, skill_id: str) -> SkillRegistration | None:
        """获取 Skill 注册信息"""
        return self._skills.get(skill_id)

    def get_by_name(self, name: str) -> SkillRegistration | None:
        """通过名称获取 Skill"""
        skill_id = self._name_index.get(name)
        if skill_id:
            return self._skills.get(skill_id)
        return None

    def discover(
        self,
        name: str | None = None,
        skill_type: SkillType | None = None,
        tags: list[str] | None = None,
        capabilities: list[str] | None = None,
        status: SkillStatus | None = None,
    ) -> list[SkillRegistration]:
        """发现 Skill

        支持按名称、类型、标签、能力和状态进行筛选。

        Args:
            name: 名称 (模糊匹配)
            skill_type: Skill 类型
            tags: 标签列表 (OR 匹配)
            capabilities: 能力列表 (OR 匹配)
            status: 状态

        Returns:
            匹配的 Skill 列表
        """
        with self._lock:
            # 从索引快速缩小范围
            candidate_ids: set[str] | None = None

            if tags:
                tag_ids: set[str] = set()
                for tag in tags:
                    tag_ids.update(self._tag_index.get(tag, set()))
                candidate_ids = tag_ids

            if capabilities:
                cap_ids: set[str] = set()
                for cap in capabilities:
                    cap_ids.update(self._capability_index.get(cap, set()))
                candidate_ids = cap_ids if candidate_ids is None else candidate_ids & cap_ids

            if candidate_ids is None:
                candidates = list(self._skills.values())
            else:
                candidates = [self._skills[sid] for sid in candidate_ids if sid in self._skills]

            # 进一步筛选
            results: list[SkillRegistration] = []
            for skill in candidates:
                if name and name.lower() not in skill.name.lower():
                    continue
                if skill_type and skill.skill_type != skill_type:
                    continue
                if status and skill.status != status:
                    continue
                results.append(skill)

            return results

    def update_status(self, skill_id: str, status: SkillStatus) -> SkillRegistration:
        """更新 Skill 状态"""
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill:
                raise ValueError(f"Skill 不存在: {skill_id}")
            skill.status = status
            skill.updated_at = datetime.now()

        self._emit_event("status_changed", skill)
        logger.info(f"Skill 状态更新: {skill.name} -> {status.value}")
        return skill

    def update_config(self, skill_id: str, config: dict[str, Any]) -> SkillRegistration:
        """更新 Skill 配置"""
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill:
                raise ValueError(f"Skill 不存在: {skill_id}")
            skill.config.update(config)
            skill.updated_at = datetime.now()

        logger.info(f"Skill 配置更新: {skill.name}")
        return skill

    def on_event(self, event_type: str, handler: Callable[..., None]) -> None:
        """注册事件处理器"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def _emit_event(self, event_type: str, data: Any) -> None:
        """触发事件"""
        for handler in self._event_handlers.get(event_type, []):
            try:
                handler(data)
            except Exception as exc:
                logger.error(f"事件处理异常: {event_type} - {exc}")

    def list_all(self) -> list[SkillRegistration]:
        """列出所有已注册 Skill"""
        return list(self._skills.values())

    def search_skills(
        self,
        query: str,
        *,
        search_fields: list[str] | None = None,
        limit: int = 50,
    ) -> list[SkillRegistration]:
        """搜索 Skill

        支持按名称、描述、标签等字段进行模糊搜索。

        Args:
            query: 搜索关键词
            search_fields: 搜索字段列表（默认: name, description, tags）
            limit: 最大返回数量

        Returns:
            匹配的 Skill 列表，按相关性排序
        """
        if not query:
            return self.list_all()[:limit]

        search_fields = search_fields or ["name", "description", "tags"]
        query_lower = query.lower()
        results: list[tuple[float, SkillRegistration]] = []

        with self._lock:
            for skill in self._skills.values():
                score = 0.0

                # 名称匹配（权重最高）
                if "name" in search_fields:
                    if query_lower in skill.name.lower():
                        score += 3.0
                    if query_lower == skill.name.lower():
                        score += 5.0

                # 显示名称匹配
                if "display_name" in search_fields:
                    if query_lower in skill.display_name.lower():
                        score += 2.0

                # 描述匹配
                if "description" in search_fields:
                    if query_lower in skill.description.lower():
                        score += 1.0

                # 标签匹配
                if "tags" in search_fields:
                    for tag in skill.tags:
                        if query_lower in tag.lower():
                            score += 2.0
                        if query_lower == tag.lower():
                            score += 3.0

                # 能力匹配
                if "capabilities" in search_fields:
                    for cap in skill.capabilities:
                        if query_lower in cap.lower():
                            score += 1.5

                # 作者匹配
                if "author" in search_fields:
                    if query_lower in skill.author.lower():
                        score += 0.5

                if score > 0:
                    results.append((score, skill))

        # 按分数排序
        results.sort(key=lambda x: x[0], reverse=True)
        return [skill for _, skill in results[:limit]]

    def list_by_source(self, source: str) -> list[SkillRegistration]:
        """按来源列出 Skill

        Args:
            source: 来源标识（如 "file", "package", "marketplace"）

        Returns:
            指定来源的 Skill 列表
        """
        results: list[SkillRegistration] = []

        with self._lock:
            for skill in self._skills.values():
                skill_source = skill.metadata.get("source", "")
                if skill_source == source:
                    results.append(skill)

        return results

    def query(self, skill_query: SkillQuery) -> list[SkillRegistration]:
        """使用 SkillQuery 进行高级查询

        Args:
            skill_query: 查询条件

        Returns:
            匹配的 Skill 列表
        """
        with self._lock:
            candidates = list(self._skills.values())

        results: list[SkillRegistration] = []
        for skill in candidates:
            # 名称过滤
            if skill_query.name:
                if skill_query.name.lower() not in skill.name.lower():
                    continue

            # 类型过滤
            if skill_query.skill_type:
                if skill.skill_type != skill_query.skill_type:
                    continue

            # 标签过滤（OR 匹配）
            if skill_query.tags:
                if not set(skill_query.tags).intersection(skill.tags):
                    continue

            # 能力过滤（OR 匹配）
            if skill_query.capabilities:
                if not set(skill_query.capabilities).intersection(skill.capabilities):
                    continue

            # 状态过滤
            if skill_query.status:
                if skill.status != skill_query.status:
                    continue

            # 作者过滤
            if skill_query.author:
                if skill_query.author.lower() not in skill.author.lower():
                    continue

            # 来源过滤
            if skill_query.source:
                skill_source = skill.metadata.get("source", "")
                if skill_source != skill_query.source:
                    continue

            results.append(skill)

        return results

    def count(self) -> int:
        """获取注册数量"""
        return len(self._skills)

    def get_statistics(self) -> dict[str, Any]:
        """获取注册统计"""
        type_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for skill in self._skills.values():
            type_key = skill.skill_type.value
            type_counts[type_key] = type_counts.get(type_key, 0) + 1
            status_key = skill.status.value
            status_counts[status_key] = status_counts.get(status_key, 0) + 1

        return {
            "total": len(self._skills),
            "by_type": type_counts,
            "by_status": status_counts,
            "total_tags": len(self._tag_index),
            "total_capabilities": len(self._capability_index),
        }
