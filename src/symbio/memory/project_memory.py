"""项目级记忆隔离 - 按项目 ID 隔离记忆空间，支持跨项目知识迁移。"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.embedding import LocalEmbedder
from symbio.utils.logger import get_logger

logger = get_logger("memory.project")


def _escape_sql_literal(value: str) -> str:
    """转义 where 子句字符串字面量中的单引号。"""
    return str(value).replace("'", "''")


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

        # 内存存储：project_id -> {memory_id -> ProjectMemoryItem}
        self._projects: dict[str, ProjectScope] = {}
        self._memories: dict[str, dict[str, ProjectMemoryItem]] = {}
        self._transfers: list[TransferRecord] = []

        # 向量索引
        self._db = None
        self._table = None
        self._projects_table = None
        self._initialized = False

        # 无 API Key 时的本地降级 embedding（构造不加载模型，首次使用才 load）
        self._local_embedder = LocalEmbedder()

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

            self._db = await asyncio.to_thread(lancedb.connect, str(db_path))
            logger.info(f"LanceDB 连接成功: {db_path}")

            # 检查或创建表
            table_names = await asyncio.to_thread(self._db.table_names)
            if "project_memories" in table_names:
                self._table = await asyncio.to_thread(self._db.open_table, "project_memories")
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
                self._table = await asyncio.to_thread(
                    self._db.create_table, "project_memories", schema=schema
                )

            # 项目元数据独立成表：没有记忆的项目也必须能在重启后列出
            if "projects" in table_names:
                self._projects_table = await asyncio.to_thread(self._db.open_table, "projects")
            else:
                projects_schema = pa.schema(
                    [
                        pa.field("project_id", pa.string()),
                        pa.field("project_name", pa.string()),
                        pa.field("description", pa.string()),
                        pa.field("tags_json", pa.string()),
                        pa.field("created_at", pa.string()),
                        pa.field("metadata_json", pa.string()),
                    ]
                )
                self._projects_table = await asyncio.to_thread(
                    self._db.create_table, "projects", schema=projects_schema
                )

            self._initialized = True
            await self._load_from_disk()

        except ImportError:
            logger.warning("lancedb 未安装，项目记忆将使用纯内存模式")
            self._initialized = True
        except Exception as e:
            logger.error(f"项目记忆管理器初始化失败: {e}")
            raise

    async def _load_from_disk(self) -> None:
        """从 LanceDB 回填内存索引。

        检索/枚举全部走内存字典，若不回填，重启后已持久化的记忆将不可见。
        """
        await self._load_projects_from_disk()

        if self._table is None:
            return

        try:
            rows = await asyncio.to_thread(lambda: self._table.search(None).limit(0).to_list())
        except Exception as e:
            logger.error(f"项目记忆加载失败: {e}")
            return

        loaded = 0
        with self._lock:
            for row in rows:
                try:
                    item = self._row_to_item(row)
                except Exception as e:  # 单行损坏不应阻断整体加载
                    logger.warning(f"跳过损坏的记忆行: {e}")
                    continue
                if item.project_id not in self._projects:
                    self.create_project(item.project_id)
                self._memories[item.project_id][item.memory_id] = item
                loaded += 1

        if loaded:
            logger.info(f"从磁盘加载项目记忆: {loaded} 条, 项目 {len(self._projects)} 个")

    async def _load_projects_from_disk(self) -> None:
        """回填项目元数据，使没有记忆的空项目重启后依然可见。"""
        if self._projects_table is None:
            return

        try:
            rows = await asyncio.to_thread(
                lambda: self._projects_table.search(None).limit(0).to_list()
            )
        except Exception as e:
            logger.error(f"项目列表加载失败: {e}")
            return

        with self._lock:
            for row in rows:
                project_id = row.get("project_id", "")
                if not project_id or project_id in self._projects:
                    continue
                created_at = row.get("created_at") or ""
                try:
                    scope = ProjectScope(
                        project_id=project_id,
                        project_name=row.get("project_name", ""),
                        description=row.get("description", ""),
                        tags=json.loads(row.get("tags_json") or "[]"),
                        created_at=(
                            datetime.fromisoformat(created_at) if created_at else datetime.now()
                        ),
                        metadata=json.loads(row.get("metadata_json") or "{}"),
                    )
                except Exception as e:
                    logger.warning(f"跳过损坏的项目行: project={project_id}, error={e}")
                    continue
                self._projects[project_id] = scope
                self._memories.setdefault(project_id, {})

    async def _persist_project(self, scope: ProjectScope) -> None:
        """持久化项目元数据（同 project_id 先删后插，保证幂等）。"""
        if self._projects_table is None:
            return

        row = {
            "project_id": scope.project_id,
            "project_name": scope.project_name,
            "description": scope.description,
            "tags_json": json.dumps(scope.tags, ensure_ascii=False),
            "created_at": scope.created_at.isoformat(),
            "metadata_json": json.dumps(scope.metadata, ensure_ascii=False),
        }
        where = f"project_id = '{_escape_sql_literal(scope.project_id)}'"
        try:
            await asyncio.to_thread(self._projects_table.delete, where)
            await asyncio.to_thread(self._projects_table.add, [row])
        except Exception as e:
            logger.error(f"项目元数据持久化失败: project={scope.project_id}, error={e}")

    @staticmethod
    def _row_to_item(row: dict[str, Any]) -> ProjectMemoryItem:
        """LanceDB row -> ProjectMemoryItem"""
        tags_json = row.get("tags_json") or "[]"
        metadata_json = row.get("metadata_json") or "{}"
        created_at = row.get("created_at") or ""
        last_accessed = row.get("last_accessed") or ""
        vector = row.get("vector") or []

        return ProjectMemoryItem(
            memory_id=row.get("memory_id", ""),
            project_id=row.get("project_id", ""),
            content=row.get("content", ""),
            memory_type=row.get("memory_type", "semantic"),
            importance=float(row.get("importance", 0.5)),
            tags=json.loads(tags_json),
            source=row.get("source", ""),
            embedding=[float(x) for x in vector],
            created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(),
            last_accessed=(
                datetime.fromisoformat(last_accessed) if last_accessed else datetime.now()
            ),
            access_count=int(row.get("access_count", 0)),
            metadata=json.loads(metadata_json),
        )

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
        """删除项目及其所有记忆（仅内存索引）

        注意：这是同步方法，不触碰持久化层。需要连带清除已落盘的记忆时，
        改用 :meth:`delete_project_async`。

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

    async def create_project_async(
        self,
        project_id: str,
        project_name: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> ProjectScope:
        """创建项目并落盘。

        :meth:`create_project` 是同步方法，只更新内存索引；需要项目在重启后
        依然可见时（例如 CLI / API 入口）应改用本方法。
        """
        scope = self.create_project(
            project_id,
            project_name=project_name,
            description=description,
            tags=tags,
        )
        await self._persist_project(scope)
        return scope

    async def delete_project_async(self, project_id: str) -> bool:
        """删除项目及其所有记忆，并同步清除持久化数据。"""
        if not self.delete_project(project_id):
            return False

        where = f"project_id = '{_escape_sql_literal(project_id)}'"
        await self._delete_persisted(where)
        if self._projects_table is not None:
            try:
                await asyncio.to_thread(self._projects_table.delete, where)
            except Exception as e:
                logger.error(f"项目元数据删除失败: project={project_id}, error={e}")
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
            created_scope = None
            if project_id not in self._projects:
                created_scope = self.create_project(project_id)

        # 隐式创建的项目也要落盘，否则重启后项目列表缺失
        if created_scope is not None:
            await self._persist_project(created_scope)

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

        # 持久化。注意必须用 `is not None`：LanceTable 实现了 __len__ 而没有
        # __bool__，空表的布尔值为 False，写成 `if self._table:` 会让第一条记忆
        # 永远写不进去，此后表恒为空 —— 持久化整体静默失效。
        if self._table is not None:
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
        """删除记忆（内存索引与持久化同时删除）"""
        with self._lock:
            project_memories = self._memories.get(project_id)
            if not project_memories or memory_id not in project_memories:
                return False

            del project_memories[memory_id]

        await self._delete_persisted(f"memory_id = '{_escape_sql_literal(memory_id)}'")
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
            new_items: list[ProjectMemoryItem] = []
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
                    new_items.append(new_item)
                    transferred.append(mid)

        # 迁移产生的副本同样要落盘，否则重启后迁移结果丢失
        for item in new_items:
            await self._persist_memory(item)

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
        """生成文本的向量表示。

        无 API Key 或调用失败时降级为本地 embedding（sentence-transformers →
        字符哈希），保证语义检索始终可用而不是静默退化成纯关键词匹配。
        """
        settings = get_settings()
        api_key = settings.model.openai_api_key
        base_url = settings.model.openai_base_url
        model = settings.memory.embedding_model

        if not api_key:
            return await asyncio.to_thread(self._local_embedder.embed, text)

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
            logger.warning(f"Embedding API 调用失败，降级为本地 embedding: {e}")
            return await asyncio.to_thread(self._local_embedder.embed, text)

    async def _persist_memory(self, memory: ProjectMemoryItem) -> None:
        """持久化记忆到 LanceDB"""
        if self._table is None:
            return

        try:
            # 占位维度取实际生效的 embedder 维度：schema 是变长 list，混入
            # 1536 维全零占位会让后续余弦相似度计算维度不匹配而整条跳过。
            dim = self._local_embedder.dim
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

    async def _delete_persisted(self, where: str) -> None:
        """按 where 条件删除持久化行（表不存在时静默跳过）。"""
        if self._table is None:
            return

        try:
            await asyncio.to_thread(self._table.delete, where)
        except Exception as e:
            logger.error(f"记忆持久化删除失败: where={where}, error={e}")

    async def close(self) -> None:
        """释放持久化句柄。"""
        self._table = None
        self._db = None
        self._initialized = False

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
