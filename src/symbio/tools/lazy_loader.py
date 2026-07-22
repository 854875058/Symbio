"""工具懒加载系统 — 按项目 / DAG 节点粒度按需加载工具 Schema，减少 LLM token 消耗。

两级隔离:
  1. 项目级静态隔离: 只加载当前项目启用的工具子集
  2. 节点级动态加载: DAG 节点执行前按需加载，执行后卸载
"""

from __future__ import annotations

import json
import threading
from typing import Any

from pydantic import BaseModel, Field

from symbio.tools.registry import ToolRegistry, ToolSchema, get_tool_registry
from symbio.utils.logger import get_logger

logger = get_logger("tool.lazy_loader")

# ---------------------------------------------------------------------------
# 估算 token 数: JSON 字符串长度 / 4（粗略近似）
# ---------------------------------------------------------------------------


def _estimate_tokens(schema_dict: dict[str, Any]) -> int:
    """估算一个 Schema 字典占用的 token 数（len(json) / 4）。"""
    return len(json.dumps(schema_dict, ensure_ascii=False)) // 4


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class ToolManifest(BaseModel):
    """单个工具的清单条目，用于懒加载决策。"""

    name: str = Field(description="工具名称，与 ToolRegistry 中一致。")
    description: str = Field(default="", description="工具描述。")
    schema_size_tokens: int = Field(default=0, description="该工具 Schema 估算的 token 数。")
    enabled: bool = Field(default=True, description="是否在当前项目中启用。")
    required_by: list[str] = Field(
        default_factory=list,
        description="需要此工具的 DAG 节点 ID 列表（运行时动态维护）。",
    )


class LazyLoadStats(BaseModel):
    """懒加载统计信息。"""

    total_tools: int = Field(default=0, description="注册中心总工具数。")
    loaded_tools: int = Field(default=0, description="当前已加载（未卸载）的工具数。")
    unloaded_tools: int = Field(default=0, description="当前已卸载的工具数。")
    tokens_saved: int = Field(default=0, description="累计节省的 token 数。")
    load_count: int = Field(default=0, description="累计加载次数。")
    unload_count: int = Field(default=0, description="累计卸载次数。")


# ---------------------------------------------------------------------------
# 懒加载器
# ---------------------------------------------------------------------------


class ToolLazyLoader:
    """工具懒加载器。

    职责:
    - 项目级静态隔离: 根据项目配置筛选启用的工具
    - 节点级动态加载: 按 DAG 节点需求按需加载 / 卸载工具 Schema
    - 跟踪 token 节省量

    典型使用流程::

        loader = ToolLazyLoader()
        loader.load_for_project({"enabled_tools": ["file", "git"]})
        schemas = loader.load_for_node({"node_id": "n1", "tools": ["file"]})
        # ... 节点执行完毕 ...
        loader.unload_node_tools("n1")
    """

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry or get_tool_registry()
        self._lock = threading.Lock()

        # 全量 manifest 缓存（name -> manifest）
        self._manifests: dict[str, ToolManifest] = {}

        # 当前已加载的工具 Schema 缓存（name -> schema dict）
        self._loaded_schemas: dict[str, ToolSchema] = {}

        # 节点 -> 该节点加载的工具名列表
        self._node_tools: dict[str, list[str]] = {}

        # 统计
        self._stats = LazyLoadStats()

        # 全量 schema token 总数（用于计算节省量）
        self._full_schema_tokens: int = 0

        # 初始化 manifest
        self._build_manifests()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _build_manifests(self) -> None:
        """从注册中心扫描所有工具，构建 manifest 列表。"""
        schema_dicts = self._registry.export_schemas()
        self._full_schema_tokens = 0

        for schema_dict in schema_dicts:
            tokens = _estimate_tokens(schema_dict)
            self._full_schema_tokens += tokens
            manifest = ToolManifest(
                name=schema_dict["name"],
                description=schema_dict.get("description", ""),
                schema_size_tokens=tokens,
                enabled=True,
            )
            self._manifests[manifest.name] = manifest

        self._stats.total_tools = len(self._manifests)
        logger.info(
            f"构建工具清单: {len(self._manifests)} 个工具, "
            f"全量 Schema 约 {self._full_schema_tokens} tokens"
        )

    # ------------------------------------------------------------------
    # 项目级静态隔离
    # ------------------------------------------------------------------

    def load_for_project(self, project_config: dict[str, Any]) -> list[str]:
        """根据项目配置筛选启用的工具，返回启用的工具名称列表。

        Args:
            project_config: 项目配置字典，需包含 ``enabled_tools`` 键，
                值为工具名称列表。若缺失则默认全部启用。

        Returns:
            当前项目启用的工具名称列表。
        """
        enabled_names: list[str] = project_config.get("enabled_tools", [])

        with self._lock:
            if enabled_names:
                # 精确匹配项目启用列表
                enabled_set = set(enabled_names)
                for name, manifest in self._manifests.items():
                    manifest.enabled = name in enabled_set
            else:
                # 未指定则全部启用
                for manifest in self._manifests.values():
                    manifest.enabled = True

            result = [m.name for m in self._manifests.values() if m.enabled]

        logger.info(f"项目级过滤: {len(result)}/{len(self._manifests)} 个工具启用")
        return result

    # ------------------------------------------------------------------
    # 节点级动态加载
    # ------------------------------------------------------------------

    def load_for_node(self, node_requirements: dict[str, Any]) -> list[ToolSchema]:
        """为指定 DAG 节点加载所需工具的 Schema。

        Args:
            node_requirements: 节点需求字典，需包含:
                - ``node_id`` (str): 节点标识
                - ``tools`` (list[str]): 该节点需要的工具名称列表

        Returns:
            已加载工具的 ``ToolSchema`` 列表。
        """
        node_id: str = node_requirements.get("node_id", "")
        tool_names: list[str] = node_requirements.get("tools", [])

        if not node_id:
            logger.warning("load_for_node: 未提供 node_id")
            return []

        schemas: list[ToolSchema] = []

        with self._lock:
            for name in tool_names:
                manifest = self._manifests.get(name)
                if manifest is None:
                    logger.warning(f"节点 {node_id} 请求的工具 '{name}' 不存在于清单中")
                    continue

                if not manifest.enabled:
                    logger.warning(f"节点 {node_id} 请求的工具 '{name}' 在当前项目中未启用，跳过")
                    continue

                # 注册节点依赖
                if node_id not in manifest.required_by:
                    manifest.required_by.append(node_id)

                # 如果尚未加载，从注册中心获取 schema
                if name not in self._loaded_schemas:
                    tool = self._registry.get(name)
                    if tool is None:
                        logger.warning(f"工具 '{name}' 在注册中心不存在")
                        continue
                    schema = tool.schema()
                    self._loaded_schemas[name] = schema
                    self._stats.load_count += 1
                    logger.debug(f"加载工具 Schema: {name} (~{manifest.schema_size_tokens} tokens)")

                schemas.append(self._loaded_schemas[name])

            # 记录节点关联
            self._node_tools[node_id] = [s.name for s in schemas]
            self._stats.loaded_tools = len(self._loaded_schemas)
            self._update_token_savings()

        logger.info(f"节点 {node_id} 加载了 {len(schemas)} 个工具: {[s.name for s in schemas]}")
        return schemas

    # ------------------------------------------------------------------
    # 节点级卸载
    # ------------------------------------------------------------------

    def unload_node_tools(self, node_id: str) -> int:
        """卸载指定 DAG 节点关联的工具。

        仅当某个工具不再被其他活跃节点引用时才真正从缓存中移除。

        Args:
            node_id: 节点标识。

        Returns:
            实际卸载的工具数量。
        """
        unloaded = 0

        with self._lock:
            tool_names = self._node_tools.pop(node_id, [])

            for name in tool_names:
                manifest = self._manifests.get(name)
                if manifest is None:
                    continue

                # 移除节点引用
                if node_id in manifest.required_by:
                    manifest.required_by.remove(node_id)

                # 如果没有其他节点需要此工具，卸载
                if not manifest.required_by and name in self._loaded_schemas:
                    del self._loaded_schemas[name]
                    self._stats.unload_count += 1
                    unloaded += 1
                    logger.debug(f"卸载工具 Schema: {name}")

            self._stats.loaded_tools = len(self._loaded_schemas)
            self._stats.unloaded_tools = self._stats.total_tools - self._stats.loaded_tools
            self._update_token_savings()

        if unloaded:
            logger.info(f"节点 {node_id} 卸载了 {unloaded} 个工具")
        return unloaded

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_manifest(self, name: str) -> ToolManifest | None:
        """获取指定工具的 manifest。"""
        return self._manifests.get(name)

    def list_manifests(self, enabled_only: bool = True) -> list[ToolManifest]:
        """列出所有工具 manifest。"""
        manifests = list(self._manifests.values())
        if enabled_only:
            manifests = [m for m in manifests if m.enabled]
        return manifests

    def get_loaded_schemas(self) -> list[ToolSchema]:
        """获取当前已加载的所有 Schema。"""
        return list(self._loaded_schemas.values())

    def get_stats(self) -> LazyLoadStats:
        """获取懒加载统计信息。"""
        with self._lock:
            self._update_token_savings()
            return self._stats.model_copy()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _update_token_savings(self) -> None:
        """更新 token 节省量（需在持有锁时调用）。"""
        loaded_tokens = sum(
            self._manifests[name].schema_size_tokens
            for name in self._loaded_schemas
            if name in self._manifests
        )
        self._stats.tokens_saved = self._full_schema_tokens - loaded_tokens

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置加载器状态（保留 manifest，清空加载缓存和统计）。"""
        with self._lock:
            self._loaded_schemas.clear()
            self._node_tools.clear()
            for manifest in self._manifests.values():
                manifest.required_by.clear()
            self._stats = LazyLoadStats(total_tools=len(self._manifests))
            logger.info("懒加载器已重置")

    def __repr__(self) -> str:
        return (
            f"<ToolLazyLoader total={self._stats.total_tools} loaded={len(self._loaded_schemas)}>"
        )
