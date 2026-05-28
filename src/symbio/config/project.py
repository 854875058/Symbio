"""项目级配置隔离 - 多项目管理、配置继承与覆盖"""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("config.project")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class ProjectStatus(str, Enum):
    """项目状态"""
    ACTIVE = "active"
    ARCHIVED = "archived"
    TEMPLATE = "template"


class ProjectProfile(BaseModel):
    """项目配置档案"""
    project_id: str
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    parent_project_id: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfigLayer(BaseModel):
    """配置层级"""
    layer_name: str
    priority: int
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 配置合并器
# ---------------------------------------------------------------------------

class ConfigMerger:
    """配置合并器 - 支持深度合并和覆盖"""

    @staticmethod
    def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """深度合并两个配置字典

        override 中的值会覆盖 base 中的值。
        嵌套字典会递归合并, 而非整体替换。

        Args:
            base: 基础配置
            override: 覆盖配置

        Returns:
            合并后的配置
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigMerger.deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def apply_overrides(config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        """应用覆盖配置

        使用点分路径支持深层覆盖, 例如:
            {"database.host": "localhost"} 覆盖 config["database"]["host"]

        Args:
            config: 原始配置
            overrides: 覆盖项 (支持点分路径)

        Returns:
            应用覆盖后的配置
        """
        result = copy.deepcopy(config)
        for dotted_path, value in overrides.items():
            keys = dotted_path.split(".")
            target = result
            for key in keys[:-1]:
                if key not in target or not isinstance(target[key], dict):
                    target[key] = {}
                target = target[key]
            target[keys[-1]] = copy.deepcopy(value)
        return result

    @staticmethod
    def compute_diff(
        base: dict[str, Any], override: dict[str, Any], prefix: str = ""
    ) -> dict[str, Any]:
        """计算配置差异

        Args:
            base: 基础配置
            override: 对比配置
            prefix: 路径前缀 (递归用)

        Returns:
            差异字典
        """
        diff: dict[str, Any] = {}
        all_keys = set(base.keys()) | set(override.keys())
        for key in all_keys:
            full_path = f"{prefix}.{key}" if prefix else key
            if key not in base:
                diff[full_path] = {"type": "added", "value": override[key]}
            elif key not in override:
                diff[full_path] = {"type": "removed", "value": base[key]}
            elif isinstance(base[key], dict) and isinstance(override[key], dict):
                nested_diff = ConfigMerger.compute_diff(base[key], override[key], full_path)
                diff.update(nested_diff)
            elif base[key] != override[key]:
                diff[full_path] = {
                    "type": "changed",
                    "old": base[key],
                    "new": override[key],
                }
        return diff


# ---------------------------------------------------------------------------
# 项目配置管理器
# ---------------------------------------------------------------------------

class ProjectConfigManager:
    """项目级配置管理器

    支持多项目管理、配置继承与覆盖。

    用法:
        manager = ProjectConfigManager("./projects")
        manager.create_project("proj-a", "项目A", config={...})
        child = manager.create_project("proj-b", "项目B", parent_project_id="proj-a")
        resolved = manager.resolve_config("proj-b")
    """

    def __init__(self, storage_dir: str | Path = "./project_configs"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, ProjectProfile] = {}
        self._global_config: dict[str, Any] = {}
        self._merger = ConfigMerger()
        self._load_all()

    def _load_all(self) -> None:
        """从磁盘加载所有项目配置"""
        for config_file in self._storage_dir.glob("*.yaml"):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data:
                    profile = ProjectProfile(**data)
                    self._projects[profile.project_id] = profile
            except Exception as exc:
                logger.error(f"加载项目配置失败: {config_file} - {exc}")
        logger.info(f"已加载 {len(self._projects)} 个项目配置")

    def _save(self, project_id: str) -> None:
        """保存项目配置到磁盘"""
        profile = self._projects.get(project_id)
        if not profile:
            return

        config_file = self._storage_dir / f"{project_id}.yaml"
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(profile.model_dump(), f, default_flow_style=False, allow_unicode=True)
        logger.debug(f"保存项目配置: {project_id}")

    def set_global_config(self, config: dict[str, Any]) -> None:
        """设置全局基础配置 (所有项目的最终兜底)"""
        self._global_config = copy.deepcopy(config)
        logger.info("全局配置已更新")

    def create_project(
        self,
        project_id: str,
        name: str,
        description: str = "",
        config: dict[str, Any] | None = None,
        parent_project_id: str | None = None,
        tags: list[str] | None = None,
    ) -> ProjectProfile:
        """创建新项目

        Args:
            project_id: 项目唯一标识
            name: 项目名称
            description: 项目描述
            config: 项目配置
            parent_project_id: 父项目 ID (用于配置继承)
            tags: 标签列表

        Returns:
            创建的项目档案

        Raises:
            ValueError: 项目 ID 已存在或父项目不存在
        """
        if project_id in self._projects:
            raise ValueError(f"项目已存在: {project_id}")

        if parent_project_id and parent_project_id not in self._projects:
            raise ValueError(f"父项目不存在: {parent_project_id}")

        profile = ProjectProfile(
            project_id=project_id,
            name=name,
            description=description,
            config=config or {},
            parent_project_id=parent_project_id,
            tags=tags or [],
        )
        self._projects[project_id] = profile
        self._save(project_id)
        logger.info(f"创建项目: {project_id} - {name}")
        return profile

    def update_project_config(
        self,
        project_id: str,
        config: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> ProjectProfile:
        """更新项目配置

        Args:
            project_id: 项目 ID
            config: 更新配置 (合并)
            overrides: 更新覆盖项

        Returns:
            更新后的项目档案
        """
        profile = self._get_project(project_id)

        if config is not None:
            profile.config = self._merger.deep_merge(profile.config, config)
        if overrides is not None:
            profile.overrides.update(overrides)

        profile.updated_at = datetime.now()
        self._save(project_id)
        logger.info(f"更新项目配置: {project_id}")
        return profile

    def resolve_config(self, project_id: str) -> dict[str, Any]:
        """解析项目最终配置

        继承链: global -> parent -> ... -> project -> overrides

        Args:
            project_id: 项目 ID

        Returns:
            合并后的最终配置
        """
        profile = self._get_project(project_id)

        # 构建继承链
        chain: list[ProjectProfile] = []
        current: ProjectProfile | None = profile
        visited: set[str] = set()
        while current:
            if current.project_id in visited:
                logger.warning(f"检测到循环继承: {current.project_id}")
                break
            visited.add(current.project_id)
            chain.append(current)
            current = (
                self._projects.get(current.parent_project_id)
                if current.parent_project_id
                else None
            )

        # 从最远祖先开始合并
        chain.reverse()
        merged = copy.deepcopy(self._global_config)
        for ancestor in chain:
            merged = self._merger.deep_merge(merged, ancestor.config)
            merged = self._merger.apply_overrides(merged, ancestor.overrides)

        return merged

    def delete_project(self, project_id: str, force: bool = False) -> None:
        """删除项目

        Args:
            project_id: 项目 ID
            force: 是否强制删除 (即使有子项目)
        """
        profile = self._get_project(project_id)

        # 检查子项目
        children = [p for p in self._projects.values() if p.parent_project_id == project_id]
        if children and not force:
            child_names = [c.project_id for c in children]
            raise ValueError(f"项目 {project_id} 有子项目: {child_names}, 使用 force=True 强制删除")

        # 删除子项目的继承关系
        for child in children:
            child.parent_project_id = profile.parent_project_id
            self._save(child.project_id)

        del self._projects[project_id]
        config_file = self._storage_dir / f"{project_id}.yaml"
        if config_file.exists():
            config_file.unlink()

        logger.info(f"删除项目: {project_id}")

    def archive_project(self, project_id: str) -> ProjectProfile:
        """归档项目"""
        profile = self._get_project(project_id)
        profile.status = ProjectStatus.ARCHIVED
        profile.updated_at = datetime.now()
        self._save(project_id)
        logger.info(f"归档项目: {project_id}")
        return profile

    def get_project(self, project_id: str) -> ProjectProfile:
        """获取项目档案"""
        return self._get_project(project_id)

    def _get_project(self, project_id: str) -> ProjectProfile:
        """获取项目档案 (内部方法)"""
        profile = self._projects.get(project_id)
        if not profile:
            raise ValueError(f"项目不存在: {project_id}")
        return profile

    def list_projects(
        self,
        status: ProjectStatus | None = None,
        tags: list[str] | None = None,
    ) -> list[ProjectProfile]:
        """列出项目

        Args:
            status: 按状态过滤
            tags: 按标签过滤

        Returns:
            项目列表
        """
        projects = list(self._projects.values())
        if status:
            projects = [p for p in projects if p.status == status]
        if tags:
            projects = [p for p in projects if any(t in p.tags for t in tags)]
        return projects

    def get_inheritance_chain(self, project_id: str) -> list[str]:
        """获取项目继承链

        Args:
            project_id: 项目 ID

        Returns:
            从根到当前项目的 ID 列表
        """
        chain: list[str] = []
        current: str | None = project_id
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            chain.append(current)
            profile = self._projects.get(current)
            current = profile.parent_project_id if profile else None
        chain.reverse()
        return chain

    def diff_configs(self, project_id_a: str, project_id_b: str) -> dict[str, Any]:
        """比较两个项目的最终配置差异

        Args:
            project_id_a: 项目 A
            project_id_b: 项目 B

        Returns:
            差异字典
        """
        config_a = self.resolve_config(project_id_a)
        config_b = self.resolve_config(project_id_b)
        return self._merger.compute_diff(config_a, config_b)

    def export_project(self, project_id: str, output_path: str | Path) -> None:
        """导出项目配置到文件"""
        profile = self._get_project(project_id)
        resolved = self.resolve_config(project_id)
        export_data = {
            "project": profile.model_dump(),
            "resolved_config": resolved,
        }
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            yaml.dump(export_data, f, default_flow_style=False, allow_unicode=True)
        logger.info(f"导出项目配置: {project_id} -> {output_path}")

    def import_project(self, import_path: str | Path) -> ProjectProfile:
        """从文件导入项目配置"""
        path = Path(import_path)
        if not path.exists():
            raise FileNotFoundError(f"导入文件不存在: {import_path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        project_data = data.get("project", data)
        profile = ProjectProfile(**project_data)
        self._projects[profile.project_id] = profile
        self._save(profile.project_id)
        logger.info(f"导入项目配置: {profile.project_id}")
        return profile
