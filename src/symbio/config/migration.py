"""版本迁移工具 - 配置文件版本管理和自动迁移"""

from __future__ import annotations

import copy
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("config.migration")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class MigrationStatus(str, Enum):
    """迁移状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class MigrationStep(BaseModel):
    """单次迁移步骤"""
    step_id: int
    description: str
    from_version: str
    to_version: str
    status: MigrationStatus = MigrationStatus.PENDING
    applied_at: Optional[datetime] = None
    error: Optional[str] = None
    backup_path: Optional[str] = None


class MigrationPlan(BaseModel):
    """迁移计划"""
    plan_id: str
    from_version: str
    to_version: str
    steps: list[MigrationStep] = Field(default_factory=list)
    status: MigrationStatus = MigrationStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


class VersionInfo(BaseModel):
    """版本信息"""
    version: str
    description: str = ""
    config_schema: dict[str, Any] = Field(default_factory=dict)
    released_at: Optional[datetime] = None


class MigrationRecord(BaseModel):
    """迁移记录"""
    record_id: str
    config_path: str
    from_version: str
    to_version: str
    status: MigrationStatus
    steps_applied: int = 0
    total_steps: int = 0
    backup_path: str = ""
    error: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 版本解析器
# ---------------------------------------------------------------------------

class VersionParser:
    """语义化版本解析器"""

    @staticmethod
    def parse(version: str) -> tuple[int, int, int]:
        """解析版本号字符串

        Args:
            version: 版本号 (如 "1.2.3")

        Returns:
            (major, minor, patch) 元组
        """
        parts = version.strip().lstrip("v").split(".")
        if len(parts) != 3:
            raise ValueError(f"无效版本号格式: {version}, 期望格式: x.y.z")
        return int(parts[0]), int(parts[1]), int(parts[2])

    @staticmethod
    def compare(v1: str, v2: str) -> int:
        """比较两个版本号

        Args:
            v1: 版本号 1
            v2: 版本号 2

        Returns:
            -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
        """
        parsed1 = VersionParser.parse(v1)
        parsed2 = VersionParser.parse(v2)
        if parsed1 < parsed2:
            return -1
        elif parsed1 > parsed2:
            return 1
        return 0

    @staticmethod
    def is_compatible(v1: str, v2: str) -> bool:
        """检查两个版本是否兼容 (主版本号相同)"""
        major1, _, _ = VersionParser.parse(v1)
        major2, _, _ = VersionParser.parse(v2)
        return major1 == major2


# ---------------------------------------------------------------------------
# 迁移器注册中心
# ---------------------------------------------------------------------------

class MigrationRegistry:
    """迁移函数注册中心"""

    def __init__(self):
        self._migrations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._version_order: list[str] = []

    def register(
        self,
        from_version: str,
        to_version: str,
        migration_fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """注册迁移函数

        Args:
            from_version: 源版本
            to_version: 目标版本
            migration_fn: 迁移函数, 接收旧配置返回新配置
        """
        key = f"{from_version}->{to_version}"
        self._migrations[key] = migration_fn
        if from_version not in self._version_order:
            self._version_order.append(from_version)
        if to_version not in self._version_order:
            self._version_order.append(to_version)
        logger.info(f"注册迁移: {key}")

    def get_migration(
        self, from_version: str, to_version: str
    ) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
        """获取迁移函数"""
        key = f"{from_version}->{to_version}"
        return self._migrations.get(key)

    def get_migration_path(self, from_version: str, to_version: str) -> list[str]:
        """计算迁移路径

        Args:
            from_version: 起始版本
            to_version: 目标版本

        Returns:
            需要经过的版本列表
        """
        # 按版本号排序找到路径
        sorted_versions = sorted(
            set(self._version_order),
            key=lambda v: VersionParser.parse(v),
        )

        try:
            start_idx = sorted_versions.index(from_version)
            end_idx = sorted_versions.index(to_version)
        except ValueError:
            return []

        if start_idx >= end_idx:
            return []

        return sorted_versions[start_idx : end_idx + 1]

    def list_migrations(self) -> list[dict[str, str]]:
        """列出所有已注册的迁移"""
        return [
            {"key": key, "from": key.split("->")[0], "to": key.split("->")[1]}
            for key in self._migrations
        ]


# ---------------------------------------------------------------------------
# 内置迁移函数
# ---------------------------------------------------------------------------

def _migrate_v0_to_v1(config: dict[str, Any]) -> dict[str, Any]:
    """v0.x -> v1.0 迁移

    变更:
    - 顶层 model 字段拆分为 model.anthropic / model.openai
    - log_level 从字符串改为枚举
    - 新增 version 字段
    """
    result = copy.deepcopy(config)

    # 设置版本
    result["version"] = "1.0.0"

    # 迁移 model 配置
    if "model" in result and isinstance(result["model"], str):
        old_model = result["model"]
        result["model"] = {
            "anthropic": {"default_model": old_model},
            "openai": {},
        }

    # 确保 log_level 是字符串枚举值
    if "log_level" in result:
        level = str(result["log_level"]).upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            result["log_level"] = "INFO"

    return result


def _migrate_v1_to_v2(config: dict[str, Any]) -> dict[str, Any]:
    """v1.x -> v2.0 迁移

    变更:
    - memory 配置重构: lancedb_path 改为 storage.backend + storage.path
    - 新增 security 配置节
    - hitl 重命名为 human_loop
    """
    result = copy.deepcopy(config)
    result["version"] = "2.0.0"

    # 迁移 memory 配置
    if "memory" in result and isinstance(result["memory"], dict):
        memory = result["memory"]
        old_path = memory.pop("lancedb_path", "./data/lancedb")
        memory["storage"] = {
            "backend": "lancedb",
            "path": old_path,
        }

    # 新增 security 配置
    if "security" not in result:
        result["security"] = {
            "privacy": {"enabled": False},
            "audit": {"enabled": True},
        }

    # hitl -> human_loop
    if "hitl" in result:
        result["human_loop"] = result.pop("hitl")

    return result


# ---------------------------------------------------------------------------
# 配置迁移管理器
# ---------------------------------------------------------------------------

class ConfigMigrationManager:
    """配置迁移管理器

    管理配置文件的版本迁移, 支持自动检测版本、生成迁移计划、执行迁移和回滚。

    用法:
        manager = ConfigMigrationManager()
        manager.register_builtin_migrations()
        plan = manager.plan_migration("config.yaml", "2.0.0")
        manager.execute_migration(plan, "config.yaml")
    """

    def __init__(self, backup_dir: str | Path = "./config_backups"):
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._registry = MigrationRegistry()
        self._records: list[MigrationRecord] = []
        self._version_schema: dict[str, VersionInfo] = {}
        self._register_default_versions()

    def _register_default_versions(self) -> None:
        """注册默认版本信息"""
        for version, desc in [
            ("0.1.0", "初始版本"),
            ("1.0.0", "正式版本 - 模型配置重构"),
            ("2.0.0", "大版本 - 存储和安全重构"),
        ]:
            self._version_schema[version] = VersionInfo(
                version=version,
                description=desc,
                released_at=datetime.now(),
            )

    def register_builtin_migrations(self) -> None:
        """注册内置迁移函数"""
        self._registry.register("0.1.0", "1.0.0", _migrate_v0_to_v1)
        self._registry.register("1.0.0", "2.0.0", _migrate_v1_to_v2)
        logger.info("内置迁移函数已注册")

    def register_migration(
        self,
        from_version: str,
        to_version: str,
        migration_fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """注册自定义迁移函数"""
        self._registry.register(from_version, to_version, migration_fn)

    def detect_version(self, config_path: str | Path) -> str:
        """检测配置文件版本

        Args:
            config_path: 配置文件路径

        Returns:
            版本号字符串
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # 检查 version 字段
        version = config.get("version")
        if version:
            return str(version)

        # 推断版本: 没有 version 字段视为 0.1.0
        return "0.1.0"

    def plan_migration(
        self,
        config_path: str | Path,
        target_version: str,
    ) -> MigrationPlan:
        """生成迁移计划

        Args:
            config_path: 配置文件路径
            target_version: 目标版本

        Returns:
            迁移计划
        """
        current_version = self.detect_version(config_path)
        logger.info(f"计划迁移: {current_version} -> {target_version}")

        plan = MigrationPlan(
            plan_id=f"plan_{current_version}_{target_version}",
            from_version=current_version,
            to_version=target_version,
        )

        if current_version == target_version:
            logger.info("当前版本已是目标版本, 无需迁移")
            return plan

        # 获取迁移路径
        version_path = self._registry.get_migration_path(current_version, target_version)
        if not version_path:
            logger.warning(f"未找到从 {current_version} 到 {target_version} 的迁移路径")
            return plan

        # 生成迁移步骤
        for i in range(len(version_path) - 1):
            from_v = version_path[i]
            to_v = version_path[i + 1]
            migration_fn = self._registry.get_migration(from_v, to_v)
            if migration_fn:
                plan.steps.append(
                    MigrationStep(
                        step_id=len(plan.steps) + 1,
                        description=f"迁移 {from_v} -> {to_v}",
                        from_version=from_v,
                        to_version=to_v,
                    )
                )
            else:
                logger.warning(f"缺少迁移函数: {from_v} -> {to_v}")

        logger.info(f"迁移计划: {len(plan.steps)} 个步骤")
        return plan

    def execute_migration(
        self,
        plan: MigrationPlan,
        config_path: str | Path,
        dry_run: bool = False,
    ) -> MigrationPlan:
        """执行迁移计划

        Args:
            plan: 迁移计划
            config_path: 配置文件路径
            dry_run: 是否试运行 (不实际写入)

        Returns:
            更新后的迁移计划
        """
        path = Path(config_path)
        plan.status = MigrationStatus.RUNNING

        # 创建备份
        backup_path = ""
        if not dry_run:
            backup_path = str(self._backup_dir / f"{path.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
            if path.exists():
                import shutil
                shutil.copy2(path, backup_path)
                logger.info(f"配置备份: {backup_path}")

        # 加载配置
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # 逐步执行迁移
        current_config = copy.deepcopy(config)
        for step in plan.steps:
            step.status = MigrationStatus.RUNNING
            try:
                migration_fn = self._registry.get_migration(step.from_version, step.to_version)
                if not migration_fn:
                    raise RuntimeError(f"迁移函数不存在: {step.from_version} -> {step.to_version}")

                current_config = migration_fn(current_config)
                step.status = MigrationStatus.COMPLETED
                step.applied_at = datetime.now()
                step.backup_path = backup_path
                logger.info(f"迁移步骤完成: {step.description}")

            except Exception as exc:
                step.status = MigrationStatus.FAILED
                step.error = str(exc)
                plan.status = MigrationStatus.FAILED
                logger.error(f"迁移步骤失败: {step.description} - {exc}")

                # 记录失败
                self._records.append(
                    MigrationRecord(
                        record_id=f"rec_{datetime.now():%Y%m%d%H%M%S}",
                        config_path=str(path),
                        from_version=plan.from_version,
                        to_version=plan.to_version,
                        status=MigrationStatus.FAILED,
                        steps_applied=sum(1 for s in plan.steps if s.status == MigrationStatus.COMPLETED),
                        total_steps=len(plan.steps),
                        backup_path=backup_path,
                        error=str(exc),
                    )
                )
                return plan

        # 写入迁移后配置
        if not dry_run and plan.status != MigrationStatus.FAILED:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(current_config, f, default_flow_style=False, allow_unicode=True)
            logger.info(f"迁移后配置已写入: {path}")

        plan.status = MigrationStatus.COMPLETED
        plan.completed_at = datetime.now()

        # 记录成功
        self._records.append(
            MigrationRecord(
                record_id=f"rec_{datetime.now():%Y%m%d%H%M%S}",
                config_path=str(path),
                from_version=plan.from_version,
                to_version=plan.to_version,
                status=MigrationStatus.COMPLETED,
                steps_applied=len(plan.steps),
                total_steps=len(plan.steps),
                backup_path=backup_path,
                completed_at=datetime.now(),
            )
        )

        return plan

    def rollback(self, config_path: str | Path, backup_path: str | Path) -> None:
        """回滚到备份版本

        Args:
            config_path: 配置文件路径
            backup_path: 备份文件路径
        """
        import shutil

        src = Path(backup_path)
        dst = Path(config_path)
        if not src.exists():
            raise FileNotFoundError(f"备份文件不存在: {backup_path}")

        shutil.copy2(src, dst)
        logger.info(f"已回滚配置: {backup_path} -> {config_path}")

    def get_records(self) -> list[MigrationRecord]:
        """获取迁移记录"""
        return list(self._records)

    def list_available_migrations(self) -> list[dict[str, str]]:
        """列出可用迁移"""
        return self._registry.list_migrations()
