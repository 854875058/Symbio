"""Skills 导入器 - 从多种来源导入 Skill 定义。"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from symbio.skills.registry import (
    SkillRegistry,
    SkillRegistration,
    SkillStatus,
    SkillType,
)
from symbio.utils.logger import get_logger

logger = get_logger("skills.importer")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class SkillSource(str, Enum):
    """Skill 来源"""
    FILE = "file"              # 本地文件
    DIRECTORY = "directory"    # 本地目录
    PACKAGE = "package"        # Python 包
    MARKETPLACE = "marketplace"  # 市场
    URL = "url"               # 远程 URL
    CLAUDE_MD = "claude_md"   # CLAUDE.md 文件


class ImportResult(BaseModel):
    """导入结果"""
    success: bool
    skill_id: str = ""
    skill_name: str = ""
    source: SkillSource = SkillSource.FILE
    source_path: str = ""
    error: str = ""
    warnings: list[str] = Field(default_factory=list)
    imported_at: datetime = Field(default_factory=datetime.now)


class BatchImportResult(BaseModel):
    """批量导入结果"""
    total: int = 0
    success_count: int = 0
    failure_count: int = 0
    results: list[ImportResult] = Field(default_factory=list)
    duration_ms: int = 0

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.success_count / self.total


class SkillDefinition(BaseModel):
    """Skill 定义（从文件/JSON 解析）"""
    name: str
    display_name: str = ""
    description: str = ""
    version: str = "1.0.0"
    skill_type: str = "custom"
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    entry_point: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Skills 导入器
# ---------------------------------------------------------------------------

class SkillImporter:
    """Skills 导入器

    支持从多种来源导入 Skill：
    1. YAML/JSON 文件 - 单个 Skill 定义文件
    2. 目录 - 扫描目录中的所有 Skill 定义
    3. Python 包 - 从 Python 包中加载 Skill 类
    4. CLAUDE.md - 从 CLAUDE.md 文件中提取 Skill 定义

    用法:
        from symbio.skills.registry import SkillRegistry

        registry = SkillRegistry()
        importer = SkillImporter(registry)

        # 从文件导入
        result = await importer.import_from_file("path/to/skill.yaml")

        # 从目录导入
        batch_result = await importer.import_from_directory("path/to/skills/")

        # 从 Python 包导入
        result = await importer.import_from_package("my_skills")

        # 从 CLAUDE.md 导入
        result = await importer.import_from_claude_md("path/to/CLAUDE.md")
    """

    def __init__(self, registry: SkillRegistry | None = None):
        self._registry = registry or SkillRegistry()
        self._import_history: list[ImportResult] = []

        logger.info("SkillImporter 创建")

    @property
    def registry(self) -> SkillRegistry:
        """获取关联的注册中心"""
        return self._registry

    # ------------------------------------------------------------------
    # 文件导入
    # ------------------------------------------------------------------

    async def import_from_file(
        self,
        file_path: str | Path,
        *,
        validate: bool = True,
    ) -> ImportResult:
        """从文件导入 Skill

        Args:
            file_path: 文件路径（支持 .yaml, .yml, .json）
            validate: 是否验证定义

        Returns:
            导入结果
        """
        path = Path(file_path)
        if not path.exists():
            return ImportResult(
                success=False,
                source=SkillSource.FILE,
                source_path=str(path),
                error=f"文件不存在: {path}",
            )

        try:
            # 读取文件
            content = path.read_text(encoding="utf-8")

            # 解析内容
            if path.suffix in (".yaml", ".yml"):
                data = yaml.safe_load(content)
            elif path.suffix == ".json":
                data = json.loads(content)
            else:
                return ImportResult(
                    success=False,
                    source=SkillSource.FILE,
                    source_path=str(path),
                    error=f"不支持的文件格式: {path.suffix}",
                )

            # 解析定义
            definition = self._parse_definition(data)

            # 验证
            warnings: list[str] = []
            if validate:
                warnings = self._validate_definition(definition)

            # 注册
            registration = self._create_registration(definition, source_path=str(path))
            self._registry.register(registration)

            result = ImportResult(
                success=True,
                skill_id=registration.skill_id,
                skill_name=registration.name,
                source=SkillSource.FILE,
                source_path=str(path),
                warnings=warnings,
            )
            self._import_history.append(result)

            logger.info(f"从文件导入 Skill: {registration.name} ({path})")
            return result

        except Exception as e:
            result = ImportResult(
                success=False,
                source=SkillSource.FILE,
                source_path=str(path),
                error=str(e),
            )
            self._import_history.append(result)
            logger.error(f"从文件导入失败 {path}: {e}")
            return result

    async def import_from_directory(
        self,
        dir_path: str | Path,
        *,
        recursive: bool = True,
        pattern: str = "*.{yaml,yml,json}",
    ) -> BatchImportResult:
        """从目录批量导入 Skill

        Args:
            dir_path: 目录路径
            recursive: 是否递归扫描
            pattern: 文件匹配模式

        Returns:
            批量导入结果
        """
        import time
        start_time = time.monotonic()

        dir_path = Path(dir_path)
        if not dir_path.is_dir():
            return BatchImportResult(
                total=0,
                success_count=0,
                failure_count=1,
                results=[ImportResult(
                    success=False,
                    source=SkillSource.DIRECTORY,
                    source_path=str(dir_path),
                    error=f"目录不存在: {dir_path}",
                )],
            )

        # 扫描文件
        files: list[Path] = []
        for ext in ("*.yaml", "*.yml", "*.json"):
            if recursive:
                files.extend(dir_path.rglob(ext))
            else:
                files.extend(dir_path.glob(ext))

        # 导入
        results: list[ImportResult] = []
        for file_path in files:
            result = await self.import_from_file(file_path)
            results.append(result)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        batch_result = BatchImportResult(
            total=len(results),
            success_count=sum(1 for r in results if r.success),
            failure_count=sum(1 for r in results if not r.success),
            results=results,
            duration_ms=duration_ms,
        )

        logger.info(
            f"从目录导入完成: {dir_path}, "
            f"成功={batch_result.success_count}, 失败={batch_result.failure_count}"
        )
        return batch_result

    # ------------------------------------------------------------------
    # Python 包导入
    # ------------------------------------------------------------------

    async def import_from_package(
        self,
        package_name: str,
        *,
        skill_class_name: str = "Skill",
    ) -> ImportResult:
        """从 Python 包导入 Skill

        Args:
            package_name: 包名
            skill_class_name: Skill 类名

        Returns:
            导入结果
        """
        try:
            # 导入包
            package = importlib.import_module(package_name)

            # 查找 Skill 类
            skill_class = getattr(package, skill_class_name, None)
            if skill_class is None:
                # 尝试在子模块中查找
                for attr_name in dir(package):
                    attr = getattr(package, attr_name)
                    if (
                        isinstance(attr, type)
                        and hasattr(attr, "name")
                        and hasattr(attr, "execute")
                    ):
                        skill_class = attr
                        break

            if skill_class is None:
                return ImportResult(
                    success=False,
                    source=SkillSource.PACKAGE,
                    source_path=package_name,
                    error=f"未找到 Skill 类: {skill_class_name}",
                )

            # 提取元数据
            name = getattr(skill_class, "name", package_name)
            description = getattr(skill_class, "description", "")
            version = getattr(skill_class, "version", "1.0.0")
            author = getattr(skill_class, "author", "")
            tags = getattr(skill_class, "tags", [])

            # 注册
            registration = SkillRegistration(
                name=name,
                description=description,
                version=version,
                author=author,
                tags=list(tags),
                skill_type=SkillType.CUSTOM,
                entry_point=f"{package_name}:{skill_class_name}",
                metadata={"package": package_name, "class": skill_class_name},
            )
            self._registry.register(registration)

            result = ImportResult(
                success=True,
                skill_id=registration.skill_id,
                skill_name=registration.name,
                source=SkillSource.PACKAGE,
                source_path=package_name,
            )
            self._import_history.append(result)

            logger.info(f"从包导入 Skill: {registration.name} ({package_name})")
            return result

        except Exception as e:
            result = ImportResult(
                success=False,
                source=SkillSource.PACKAGE,
                source_path=package_name,
                error=str(e),
            )
            self._import_history.append(result)
            logger.error(f"从包导入失败 {package_name}: {e}")
            return result

    async def import_from_module(
        self,
        module_path: str | Path,
        *,
        skill_class_name: str = "Skill",
    ) -> ImportResult:
        """从 Python 模块文件导入 Skill

        Args:
            module_path: 模块文件路径
            skill_class_name: Skill 类名

        Returns:
            导入结果
        """
        path = Path(module_path)
        if not path.exists():
            return ImportResult(
                success=False,
                source=SkillSource.FILE,
                source_path=str(path),
                error=f"模块文件不存在: {path}",
            )

        try:
            # 动态加载模块
            module_name = path.stem
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec is None or spec.loader is None:
                return ImportResult(
                    success=False,
                    source=SkillSource.FILE,
                    source_path=str(path),
                    error=f"无法加载模块: {path}",
                )

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # 查找 Skill 类
            skill_class = getattr(module, skill_class_name, None)
            if skill_class is None:
                return ImportResult(
                    success=False,
                    source=SkillSource.FILE,
                    source_path=str(path),
                    error=f"未找到 Skill 类: {skill_class_name}",
                )

            # 提取元数据
            name = getattr(skill_class, "name", module_name)
            description = getattr(skill_class, "description", "")
            version = getattr(skill_class, "version", "1.0.0")
            author = getattr(skill_class, "author", "")
            tags = getattr(skill_class, "tags", [])

            # 注册
            registration = SkillRegistration(
                name=name,
                description=description,
                version=version,
                author=author,
                tags=list(tags),
                skill_type=SkillType.CUSTOM,
                entry_point=str(path),
                metadata={"module_path": str(path), "class": skill_class_name},
            )
            self._registry.register(registration)

            result = ImportResult(
                success=True,
                skill_id=registration.skill_id,
                skill_name=registration.name,
                source=SkillSource.FILE,
                source_path=str(path),
            )
            self._import_history.append(result)

            logger.info(f"从模块导入 Skill: {registration.name} ({path})")
            return result

        except Exception as e:
            result = ImportResult(
                success=False,
                source=SkillSource.FILE,
                source_path=str(path),
                error=str(e),
            )
            self._import_history.append(result)
            logger.error(f"从模块导入失败 {path}: {e}")
            return result

    # ------------------------------------------------------------------
    # CLAUDE.md 导入
    # ------------------------------------------------------------------

    async def import_from_claude_md(
        self,
        file_path: str | Path,
    ) -> ImportResult:
        """从 CLAUDE.md 文件导入 Skill 定义

        CLAUDE.md 中的 Skill 定义格式:
        ```yaml
        # skill: name
        # description: ...
        # tags: [tag1, tag2]
        ```

        Args:
            file_path: CLAUDE.md 文件路径

        Returns:
            导入结果
        """
        path = Path(file_path)
        if not path.exists():
            return ImportResult(
                success=False,
                source=SkillSource.CLAUDE_MD,
                source_path=str(path),
                error=f"文件不存在: {path}",
            )

        try:
            content = path.read_text(encoding="utf-8")

            # 提取 Skill 定义块
            skills = self._extract_skills_from_markdown(content)

            if not skills:
                return ImportResult(
                    success=False,
                    source=SkillSource.CLAUDE_MD,
                    source_path=str(path),
                    error="未找到 Skill 定义",
                )

            # 注册第一个 Skill
            definition = skills[0]
            registration = self._create_registration(
                definition,
                source_path=str(path),
            )
            self._registry.register(registration)

            result = ImportResult(
                success=True,
                skill_id=registration.skill_id,
                skill_name=registration.name,
                source=SkillSource.CLAUDE_MD,
                source_path=str(path),
            )
            self._import_history.append(result)

            logger.info(f"从 CLAUDE.md 导入 Skill: {registration.name} ({path})")
            return result

        except Exception as e:
            result = ImportResult(
                success=False,
                source=SkillSource.CLAUDE_MD,
                source_path=str(path),
                error=str(e),
            )
            self._import_history.append(result)
            logger.error(f"从 CLAUDE.md 导入失败 {path}: {e}")
            return result

    # ------------------------------------------------------------------
    # 历史记录
    # ------------------------------------------------------------------

    def get_import_history(self) -> list[ImportResult]:
        """获取导入历史"""
        return self._import_history.copy()

    def clear_history(self) -> None:
        """清除导入历史"""
        self._import_history.clear()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _parse_definition(self, data: dict[str, Any]) -> SkillDefinition:
        """解析 Skill 定义"""
        return SkillDefinition(
            name=data.get("name", ""),
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            skill_type=data.get("skill_type", "custom"),
            author=data.get("author", ""),
            tags=data.get("tags", []),
            capabilities=data.get("capabilities", []),
            entry_point=data.get("entry_point", ""),
            config=data.get("config", {}),
            metadata=data.get("metadata", {}),
        )

    def _validate_definition(self, definition: SkillDefinition) -> list[str]:
        """验证 Skill 定义"""
        warnings: list[str] = []

        if not definition.name:
            warnings.append("缺少 name 字段")

        if not definition.description:
            warnings.append("缺少 description 字段")

        if definition.version and not self._is_valid_version(definition.version):
            warnings.append(f"版本格式不标准: {definition.version}")

        return warnings

    def _is_valid_version(self, version: str) -> bool:
        """检查版本格式"""
        import re
        return bool(re.match(r"^\d+\.\d+\.\d+$", version))

    def _create_registration(
        self,
        definition: SkillDefinition,
        source_path: str = "",
    ) -> SkillRegistration:
        """创建 SkillRegistration"""
        # 映射 skill_type
        type_map = {
            "tool": SkillType.TOOL,
            "agent": SkillType.AGENT,
            "workflow": SkillType.WORKFLOW,
            "integration": SkillType.INTEGRATION,
            "custom": SkillType.CUSTOM,
        }
        skill_type = type_map.get(definition.skill_type, SkillType.CUSTOM)

        metadata = definition.metadata.copy()
        if source_path:
            metadata["source_path"] = source_path

        return SkillRegistration(
            name=definition.name,
            display_name=definition.display_name or definition.name,
            description=definition.description,
            version=definition.version,
            skill_type=skill_type,
            author=definition.author,
            tags=definition.tags,
            capabilities=definition.capabilities,
            entry_point=definition.entry_point,
            config=definition.config,
            metadata=metadata,
        )

    def _extract_skills_from_markdown(
        self, content: str
    ) -> list[SkillDefinition]:
        """从 Markdown 中提取 Skill 定义"""
        import re

        skills: list[SkillDefinition] = []

        # 匹配 YAML 代码块中的 skill 定义
        yaml_pattern = re.compile(
            r"```(?:yaml|yml)\n(.*?)```", re.DOTALL
        )

        for match in yaml_pattern.finditer(content):
            yaml_content = match.group(1)
            try:
                data = yaml.safe_load(yaml_content)
                if isinstance(data, dict) and "name" in data:
                    skills.append(self._parse_definition(data))
            except Exception:
                continue

        # 匹配注释格式的 skill 定义
        comment_pattern = re.compile(
            r"#\s*skill:\s*(.+?)\n"
            r"(?:#\s*description:\s*(.+?)\n)?"
            r"(?:#\s*tags:\s*\[(.+?)\])?",
            re.IGNORECASE,
        )

        for match in comment_pattern.finditer(content):
            name = match.group(1).strip()
            description = match.group(2).strip() if match.group(2) else ""
            tags_str = match.group(3) if match.group(3) else ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []

            skills.append(SkillDefinition(
                name=name,
                description=description,
                tags=tags,
            ))

        return skills
