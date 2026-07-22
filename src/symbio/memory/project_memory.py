"""项目级记忆隔离 - 按项目 ID 隔离记忆空间，支持跨项目知识迁移。"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("memory.project")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class ProjectScope(BaseModel):
    """项目作用域定义"""

    project_id: str
    project_name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectMemoryItem(BaseModel):
    """项目级记忆条目"""

    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    content: str
    memory_type: str = "semantic"  # semantic / episodic / procedural
    importance: float = 0.5
    tags: list[str] = Field(default_factory=list)
    source: str = ""  # 来源：cc / codex / manual
    embedding: list[float] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    last_accessed: datetime = Field(default_factory=datetime.now)
    access_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def update_access(self) -> None:
        """更新访问记录"""
        self.access_count += 1
        self.last_accessed = datetime.now()


class TransferRecord(BaseModel):
    """知识迁移记录"""

    transfer_id: str = Field(default_factory=lambda: str(uuid4()))
    source_project: str
    target_project: str
    memory_ids: list[str]
    transferred_at: datetime = Field(default_factory=datetime.now)
    reason: str = ""


# ---------------------------------------------------------------------------
# 项目级记忆管理器
# ---------------------------------------------------------------------------


class ProjectMemoryManager:
    """项目级记忆管理器

    核心能力：
    1. 项目隔离 - 每个项目拥有独立的记忆空间
    2. 跨项目迁移 - 支持在项目间迁移知识
    3. 全局检索 - 可跨项目检索相关知识

    用法:
        manager = ProjectMemoryManager()

        # 添加项目记忆
        await manager.add_memory(
            project_id="proj_001",
            content="用户偏好使用 Python 3.11",
            source="cc",
        )

        # 检索项目记忆
        results = await manager.search(project_id="proj_001", query="用户偏好")

        # 跨项目迁移
        await manager.transfer_knowledge(
            source_project="proj_001",
            target_project="proj_002",
            memory_ids=["mem_001", "mem_002"],
        )
    """

    def __init__(self, storage_dir: str | Path | None = None):
        settings = get_settings()
        self._storage_dir = Path(storage_dir or settings.memory.lancedb_path) / "projects"
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # 内存存储：project_id -> {memory_id -> ProjectMemoryItem}
        self._projects: dict[str, ProjectScope] = {}
        self._memories: dict[str, dict[str, ProjectMemoryItem]] = {}
        self._transfers: list[TransferRecord] = []

        # 向量索引
        self._db = None
        self._table = None
        self._initialized = False

        self._lock = threading.RLock()

        logger.info(f"ProjectMemoryManager 创建: storage_dir={self._storage_dir}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化存储"""
        if self._initialized:
            return

        try:
            import lancedb
            import pyarrow as pa

            db_path = self._storage_dir / "lancedb"
            db_path.mkdir(parents=True, exist_ok=True)

            self._db = await __import__("asyncio").to_thread(lancedb.connect, str(db_path))
            logger.info(f"LanceDB 连接成功: {db_path}")

            # 检查或创建表
            table_names = await __import__("asyncio").to_thread(self._db.table_names)
            if "project_memories" in table_names:
                self._table = await __import__("asyncio").to_thread(
                    self._db.open_table, "project_memories"
                )
            else:
                schema = pa.schema(
                    [
                        pa.field("memory_id", pa.string()),
                        pa.field("project_id", pa.string()),
                        pa.field("content", pa.string()),
                        pa.field("memory_type", pa.string()),
                        pa.field("importance", pa.float64()),
                        pa.field("tags_json", pa.string()),
                        pa.field("source", pa.string()),
                        pa.field("vector", pa.list_(pa.float32())),
                        pa.field("created_at", pa.string()),
                        pa.field("last_accessed", pa.string()),
                        pa.field("access_count", pa.int64()),
                        pa.field("metadata_json", pa.string()),
                    ]
                )
                self._table = await __import__("asyncio").to_thread(
                    self._db.create_table, "project_memories", schema=schema
                )

            self._initialized = True

        except ImportError:
            logger.warning("lancedb 未安装，项目记忆将使用纯内存模式")
            self._initialized = True
        except Exception as e:
            logger.error(f"项目记忆管理器初始化失败: {e}")
            raise

    # ------------------------------------------------------------------
    # 项目管理
    # ------------------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        project_name: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> ProjectScope:
        """创建项目

        Args:
            project_id: 项目 ID
            project_name: 项目名称
            description: 项目描述
            tags: 项目标签

        Returns:
            项目作用域
        """
        with self._lock:
            if project_id in self._projects:
                logger.warning(f"项目已存在: {project_id}")
                return self._projects[project_id]

            scope = ProjectScope(
                project_id=project_id,
                project_name=project_name,
                description=description,
                tags=tags or [],
            )
            self._projects[project_id] = scope
            self._memories[project_id] = {}

            logger.info(f"创建项目: {project_id} ({project_name})")
            return scope

    def get_project(self, project_id: str) -> ProjectScope | None:
        """获取项目"""
        return self._projects.get(project_id)

    def list_projects(self) -> list[ProjectScope]:
        """列出所有项目"""
        return list(self._projects.values())

    def delete_project(self, project_id: str) -> bool:
        """删除项目及其所有记忆

        Args:
            project_id: 项目 ID

        Returns:
            是否成功删除
        """
        with self._lock:
            if project_id not in self._projects:
                return False

            del self._projects[project_id]
            del self._memories[project_id]

            logger.info(f"删除项目: {project_id}")
            return True

    # ------------------------------------------------------------------
    # 记忆操作
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        project_id: str,
        content: str,
        memory_type: str = "semantic",
        *,
        importance: float = 0.5,
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ProjectMemoryItem:
        """添加项目记忆

        Args:
            project_id: 项目 ID
            content: 记忆内容
            memory_type: 记忆类型 (semantic/episodic/procedural)
            importance: 重要性评分 (0-1)
            tags: 标签列表
            source: 来源标识 (cc/codex/manual)
            metadata: 额外元数据

        Returns:
            创建的记忆条目
        """
        if not self._initialized:
            await self.initialize()

        with self._lock:
            # 确保项目存在
            if project_id not in self._projects:
                self.create_project(project_id)

        # 生成 embedding
        embedding = await self._get_embedding(content)

        item = ProjectMemoryItem(
            project_id=project_id,
            content=content,
            memory_type=memory_type,
            importance=importance,
            tags=tags or [],
            source=source,
            embedding=embedding,
            metadata=metadata or {},
        )

        with self._lock:
            self._memories[project_id][item.memory_id] = item

        # 持久化
        if self._table:
            await self._persist_memory(item)

        logger.info(
            f"添加项目记忆: project={project_id}, id={item.memory_id}, "
            f"type={memory_type}, source={source}"
        )
        return item

    async def get_memory(self, project_id: str, memory_id: str) -> ProjectMemoryItem | None:
        """获取记忆"""
        with self._lock:
            project_memories = self._memories.get(project_id, {})
            return project_memories.get(memory_id)

    async def delete_memory(self, project_id: str, memory_id: str) -> bool:
        """删除记忆"""
        with self._lock:
            project_memories = self._memories.get(project_id)
            if not project_memories or memory_id not in project_memories:
                return False

            del project_memories[memory_id]

        logger.info(f"删除记忆: project={project_id}, memory={memory_id}")
        return True

    async def list_memories(
        self,
        project_id: str,
        memory_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[ProjectMemoryItem]:
        """列出项目记忆

        Args:
            project_id: 项目 ID
            memory_type: 过滤记忆类型
            tags: 过滤标签

        Returns:
            记忆列表
        """
        with self._lock:
            project_memories = self._memories.get(project_id, {})
            results = list(project_memories.values())

        # 过滤
        if memory_type:
            results = [m for m in results if m.memory_type == memory_type]
        if tags:
            tag_set = set(tags)
            results = [m for m in results if tag_set.intersection(m.tags)]

        return results

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        memory_types: list[str] | None = None,
        max_results: int = 10,
        similarity_threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """语义检索记忆

        Args:
            query: 查询文本
            project_id: 限定项目 ID（None 则跨项目）
            memory_types: 过滤记忆类型
            max_results: 最大结果数
            similarity_threshold: 相似度阈值

        Returns:
            检索结果列表
        """
        if not self._initialized:
            await self.initialize()

        query_embedding = await self._get_embedding(query)
        if not query_embedding:
            logger.warning("查询 embedding 生成失败，回退到关键词搜索")
            return self._keyword_search(query, project_id, max_results)

        results: list[dict[str, Any]] = []

        # 从内存检索
        with self._lock:
            if project_id:
                search_spaces = {project_id: self._memories.get(project_id, {})}
            else:
                search_spaces = self._memories

            for pid, memories in search_spaces.items():
                for memory in memories.values():
                    if not memory.embedding:
                        continue
                    if memory_types and memory.memory_type not in memory_types:
                        continue

                    similarity = self._cosine_similarity(query_embedding, memory.embedding)
                    if similarity >= similarity_threshold:
                        memory.update_access()
                        results.append(
                            {
                                "memory_id": memory.memory_id,
                                "project_id": pid,
                                "content": memory.content,
                                "memory_type": memory.memory_type,
                                "similarity": similarity,
                                "importance": memory.importance,
                                "source": memory.source,
                                "tags": memory.tags,
                            }
                        )

        # 按相似度排序
        results.sort(key=lambda r: r["similarity"], reverse=True)

        logger.info(
            f"项目记忆检索: query={query[:50]}, project={project_id or 'all'}, "
            f"results={len(results)}"
        )
        return results[:max_results]

    def _keyword_search(
        self, query: str, project_id: str | None, max_results: int
    ) -> list[dict[str, Any]]:
        """关键词搜索（回退方案）"""
        query_lower = query.lower()
        results: list[dict[str, Any]] = []

        with self._lock:
            if project_id:
                search_spaces = {project_id: self._memories.get(project_id, {})}
            else:
                search_spaces = self._memories

            for pid, memories in search_spaces.items():
                for memory in memories.values():
                    if query_lower in memory.content.lower():
                        match_ratio = len(query_lower) / len(memory.content.lower())
                        memory.update_access()
                        results.append(
                            {
                                "memory_id": memory.memory_id,
                                "project_id": pid,
                                "content": memory.content,
                                "memory_type": memory.memory_type,
                                "similarity": match_ratio,
                                "importance": memory.importance,
                                "source": memory.source,
                                "tags": memory.tags,
                            }
                        )

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:max_results]

    # ------------------------------------------------------------------
    # 知识迁移
    # ------------------------------------------------------------------

    async def transfer_knowledge(
        self,
        source_project: str,
        target_project: str,
        memory_ids: list[str],
        reason: str = "",
    ) -> TransferRecord:
        """跨项目迁移知识

        Args:
            source_project: 源项目 ID
            target_project: 目标项目 ID
            memory_ids: 要迁移的记忆 ID 列表
            reason: 迁移原因

        Returns:
            迁移记录

        Raises:
            ValueError: 源项目或记忆不存在
        """
        with self._lock:
            if source_project not in self._memories:
                raise ValueError(f"源项目不存在: {source_project}")

            if target_project not in self._projects:
                self.create_project(target_project)

            source_memories = self._memories[source_project]
            target_memories = self._memories[target_project]

            transferred: list[str] = []
            for mid in memory_ids:
                if mid in source_memories:
                    # 复制记忆到目标项目
                    original = source_memories[mid]
                    new_item = ProjectMemoryItem(
                        project_id=target_project,
                        content=original.content,
                        memory_type=original.memory_type,
                        importance=original.importance,
                        tags=original.tags.copy(),
                        source=f"transfer:{source_project}",
                        embedding=original.embedding.copy(),
                        metadata={
                            **original.metadata,
                            "transferred_from": source_project,
                            "original_id": mid,
                        },
                    )
                    target_memories[new_item.memory_id] = new_item
                    transferred.append(mid)

        record = TransferRecord(
            source_project=source_project,
            target_project=target_project,
            memory_ids=transferred,
            reason=reason,
        )
        self._transfers.append(record)

        logger.info(
            f"知识迁移: {source_project} -> {target_project}, 迁移 {len(transferred)} 条记忆"
        )
        return record

    def get_transfer_history(
        self,
        project_id: str | None = None,
    ) -> list[TransferRecord]:
        """获取迁移历史

        Args:
            project_id: 过滤项目 ID

        Returns:
            迁移记录列表
        """
        if project_id is None:
            return self._transfers.copy()
        return [
            r
            for r in self._transfers
            if r.source_project == project_id or r.target_project == project_id
        ]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_statistics(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            project_stats = {}
            total_memories = 0
            for pid, memories in self._memories.items():
                type_counts: dict[str, int] = {}
                for m in memories.values():
                    type_counts[m.memory_type] = type_counts.get(m.memory_type, 0) + 1

                project_stats[pid] = {
                    "total": len(memories),
                    "by_type": type_counts,
                }
                total_memories += len(memories)

        return {
            "total_projects": len(self._projects),
            "total_memories": total_memories,
            "total_transfers": len(self._transfers),
            "projects": project_stats,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量表示"""
        settings = get_settings()
        api_key = settings.model.openai_api_key
        base_url = settings.model.openai_base_url
        model = settings.memory.embedding_model

        if not api_key:
            logger.warning("未配置 API Key，无法生成 embedding")
            return []

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            return []

    async def _persist_memory(self, memory: ProjectMemoryItem) -> None:
        """持久化记忆到 LanceDB"""
        if not self._table:
            return

        try:
            import asyncio

            dim = get_settings().memory.embedding_dim or 1536
            row = {
                "memory_id": memory.memory_id,
                "project_id": memory.project_id,
                "content": memory.content,
                "memory_type": memory.memory_type,
                "importance": memory.importance,
                "tags_json": json.dumps(memory.tags, ensure_ascii=False),
                "source": memory.source,
                "vector": memory.embedding if memory.embedding else [0.0] * dim,
                "created_at": memory.created_at.isoformat(),
                "last_accessed": memory.last_accessed.isoformat(),
                "access_count": memory.access_count,
                "metadata_json": json.dumps(memory.metadata, ensure_ascii=False),
            }
            await asyncio.to_thread(self._table.add, [row])
        except Exception as e:
            logger.error(f"记忆持久化失败: {e}")

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算余弦相似度"""
        if not a or not b or len(a) != len(b):
            return 0.0

        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)
