"""记忆管理器 - 短期/长期记忆管理、语义检索"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("memory_manager")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class MemoryType(str, Enum):
    """记忆类型"""
    SHORT_TERM = "short_term"    # 短期记忆（对话上下文）
    LONG_TERM = "long_term"      # 长期记忆（持久化知识）
    EPISODIC = "episodic"        # 情景记忆（事件记录）
    SEMANTIC = "semantic"        # 语义记忆（概念知识）
    PROCEDURAL = "procedural"    # 程序记忆（操作步骤）


class MemoryPriority(str, Enum):
    """记忆优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class MemoryStatus(str, Enum):
    """记忆状态"""
    ACTIVE = "active"            # 活跃
    CONSOLIDATED = "consolidated"  # 已巩固（从短期转为长期）
    ARCHIVED = "archived"        # 已归档
    FORGOTTEN = "forgotten"      # 已遗忘


class MemoryItem(BaseModel):
    """记忆条目"""
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str                                  # 记忆内容
    memory_type: MemoryType = MemoryType.SHORT_TERM
    priority: MemoryPriority = MemoryPriority.NORMAL
    status: MemoryStatus = MemoryStatus.ACTIVE

    # 向量表示
    embedding: list[float] = Field(default_factory=list)

    # 关联信息
    session_id: str = ""
    user_id: str = ""
    source: str = ""                              # 来源标识
    tags: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)

    # 统计信息
    importance: float = 0.5                       # 重要性评分 (0-1)
    access_count: int = 0                         # 访问次数
    decay_rate: float = 0.01                      # 衰减速率

    # 时间信息
    created_at: datetime = Field(default_factory=datetime.now)
    last_accessed: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None         # 过期时间（短期记忆）

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self) -> bool:
        """检查是否过期"""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def update_access(self) -> None:
        """更新访问记录"""
        self.access_count += 1
        self.last_accessed = datetime.now()

    def calculate_relevance(self, current_time: Optional[datetime] = None) -> float:
        """计算当前相关性（考虑时间衰减）"""
        now = current_time or datetime.now()
        time_diff = (now - self.last_accessed).total_seconds() / 3600  # 小时
        time_decay = max(0, 1.0 - (self.decay_rate * time_diff))

        # 综合评分：重要性 * 时间衰减 * 访问频率
        access_boost = min(self.access_count / 10.0, 0.5)
        relevance = (self.importance * 0.5 + time_decay * 0.3 + access_boost * 0.2)

        return min(max(relevance, 0.0), 1.0)


class ConversationTurn(BaseModel):
    """对话轮次"""
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    role: str                                     # user / assistant / system
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationSession(BaseModel):
    """对话会话"""
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = ""
    turns: list[ConversationTurn] = Field(default_factory=list)
    summary: str = ""                             # 会话摘要
    created_at: datetime = Field(default_factory=datetime.now)
    last_active: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return len(self.turns)


class SearchResult(BaseModel):
    """检索结果"""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    memory: MemoryItem
    score: float = 0.0                            # 相似度评分
    match_type: str = "semantic"                  # semantic / keyword / exact
    highlight: str = ""                           # 高亮片段


class MemoryStats(BaseModel):
    """记忆统计"""
    total_memories: int = 0
    short_term_count: int = 0
    long_term_count: int = 0
    episodic_count: int = 0
    semantic_count: int = 0
    procedural_count: int = 0
    active_sessions: int = 0
    total_conversations: int = 0
    avg_importance: float = 0.0
    last_consolidation: Optional[datetime] = None


class MemoryManagerConfig(BaseModel):
    """记忆管理器配置"""
    # 短期记忆
    short_term_window: int = 20                   # 短期记忆窗口大小
    short_term_ttl_hours: int = 24                # 短期记忆 TTL（小时）
    auto_summarize_threshold: int = 50            # 自动摘要阈值（消息数）

    # 长期记忆
    max_long_term_memories: int = 10000           # 最大长期记忆数
    consolidation_threshold: float = 0.7          # 巩固阈值（重要性）

    # 语义检索
    embedding_model: str = ""                     # 为空则使用全局配置
    embedding_dim: int = 0                        # 为空则使用全局配置
    similarity_threshold: float = 0.7             # 相似度阈值
    max_search_results: int = 10                  # 最大检索结果数

    # 衰减
    enable_decay: bool = True                     # 启用记忆衰减
    decay_check_interval_hours: int = 6           # 衰减检查间隔

    # 持久化
    lancedb_path: str = ""                        # LanceDB 存储路径
    table_name: str = "memories"


# ---------------------------------------------------------------------------
# 记忆管理器
# ---------------------------------------------------------------------------

class MemoryManager:
    """记忆管理器

    核心能力：
    1. 短期/长期记忆管理 - 对话上下文与持久化知识分离
    2. 语义检索 - 基于向量相似度的记忆召回
    3. 记忆巩固 - 自动将重要短期记忆转为长期记忆
    4. 记忆衰减 - 基于时间和访问频率的记忆衰减

    记忆类型：
    - 短期记忆：对话上下文，自动过期
    - 长期记忆：持久化知识，手动或自动巩固
    - 情景记忆：事件记录
    - 语义记忆：概念知识
    - 程序记忆：操作步骤

    Usage:
        manager = MemoryManager()
        await manager.initialize()

        # 添加记忆
        await manager.add_memory("用户喜欢 Python", MemoryType.LONG_TERM)

        # 检索记忆
        results = await manager.search("用户偏好什么编程语言？")

        # 管理会话
        manager.add_conversation_turn("user", "你好")
        manager.add_conversation_turn("assistant", "你好！有什么可以帮你的？")
    """

    def __init__(self, config: Optional[MemoryManagerConfig] = None):
        settings = get_settings()
        self._config = config or MemoryManagerConfig()

        # 从全局配置补全默认值
        if not self._config.embedding_model:
            self._config.embedding_model = settings.memory.embedding_model
        if not self._config.embedding_dim:
            self._config.embedding_dim = settings.memory.embedding_dim
        if not self._config.lancedb_path:
            self._config.lancedb_path = str(
                Path(settings.memory.lancedb_path) / "memory_manager"
            )

        # 内存存储
        self._short_term: dict[str, MemoryItem] = {}    # memory_id -> item
        self._long_term: dict[str, MemoryItem] = {}
        self._sessions: dict[str, ConversationSession] = {}

        # 向量索引（通过 LanceDB）
        self._db = None
        self._table = None
        self._initialized = False

        # Embedding 缓存
        self._embedding_cache: dict[str, list[float]] = {}

        # 后台任务
        self._decay_task: Optional[asyncio.Task] = None

        logger.info(
            f"MemoryManager 创建: short_term_window={self._config.short_term_window}, "
            f"max_long_term={self._config.max_long_term_memories}, "
            f"similarity_threshold={self._config.similarity_threshold}"
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 LanceDB 连接和表结构"""
        if self._initialized:
            return

        try:
            import lancedb
            import pyarrow as pa

            db_path = Path(self._config.lancedb_path)
            db_path.mkdir(parents=True, exist_ok=True)

            self._db = await asyncio.to_thread(lancedb.connect, str(db_path))
            logger.info(f"LanceDB 连接成功: {db_path}")

            # 检查或创建表
            table_names = await asyncio.to_thread(self._db.table_names)
            if self._config.table_name in table_names:
                self._table = await asyncio.to_thread(
                    self._db.open_table, self._config.table_name
                )
                logger.info(f"打开已有记忆表: {self._config.table_name}")
            else:
                schema = pa.schema([
                    pa.field("memory_id", pa.string()),
                    pa.field("content", pa.string()),
                    pa.field("memory_type", pa.string()),
                    pa.field("priority", pa.string()),
                    pa.field("status", pa.string()),
                    pa.field("vector", pa.list_(pa.float32())),
                    pa.field("session_id", pa.string()),
                    pa.field("user_id", pa.string()),
                    pa.field("source", pa.string()),
                    pa.field("tags_json", pa.string()),
                    pa.field("importance", pa.float64()),
                    pa.field("access_count", pa.int64()),
                    pa.field("created_at", pa.string()),
                    pa.field("last_accessed", pa.string()),
                    pa.field("expires_at", pa.string()),
                    pa.field("metadata_json", pa.string()),
                ])
                self._table = await asyncio.to_thread(
                    self._db.create_table,
                    self._config.table_name,
                    schema=schema,
                )
                logger.info(f"创建记忆表: {self._config.table_name}")

            self._initialized = True

            # 启动衰减检查
            if self._config.enable_decay:
                self._start_decay_task()

        except ImportError:
            logger.warning("lancedb 未安装，记忆管理器将使用纯内存模式")
            self._initialized = True
        except Exception as e:
            logger.error(f"记忆管理器初始化失败: {e}")
            raise

    async def close(self) -> None:
        """关闭管理器"""
        if self._decay_task and not self._decay_task.done():
            self._decay_task.cancel()
            try:
                await self._decay_task
            except asyncio.CancelledError:
                pass
        self._initialized = False
        logger.info("MemoryManager 已关闭")

    def _start_decay_task(self) -> None:
        """启动后台衰减检查任务"""
        async def _decay_loop():
            while True:
                try:
                    await asyncio.sleep(self._config.decay_check_interval_hours * 3600)
                    await self._apply_decay()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"衰减检查异常: {e}")

        self._decay_task = asyncio.create_task(_decay_loop())
        logger.debug("衰减检查任务已启动")

    # ------------------------------------------------------------------
    # 记忆添加
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        content: str,
        memory_type: MemoryType = MemoryType.SHORT_TERM,
        *,
        priority: MemoryPriority = MemoryPriority.NORMAL,
        session_id: str = "",
        user_id: str = "",
        source: str = "",
        tags: Optional[list[str]] = None,
        importance: float = 0.5,
        ttl_hours: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MemoryItem:
        """添加记忆

        Args:
            content: 记忆内容
            memory_type: 记忆类型
            priority: 优先级
            session_id: 会话 ID
            user_id: 用户 ID
            source: 来源标识
            tags: 标签列表
            importance: 重要性评分 (0-1)
            ttl_hours: TTL（小时），仅短期记忆
            metadata: 额外元数据

        Returns:
            创建的记忆条目
        """
        if not self._initialized:
            await self.initialize()

        # 计算过期时间
        expires_at = None
        if memory_type == MemoryType.SHORT_TERM:
            ttl = ttl_hours or self._config.short_term_ttl_hours
            expires_at = datetime.now() + timedelta(hours=ttl)

        # 生成 embedding
        embedding = await self._get_embedding(content)

        item = MemoryItem(
            content=content,
            memory_type=memory_type,
            priority=priority,
            session_id=session_id,
            user_id=user_id,
            source=source,
            tags=tags or [],
            importance=importance,
            embedding=embedding,
            expires_at=expires_at,
            metadata=metadata or {},
        )

        # 存储到内存
        if memory_type == MemoryType.SHORT_TERM:
            self._short_term[item.memory_id] = item

            # 窗口大小控制
            await self._enforce_short_term_window()
        else:
            self._long_term[item.memory_id] = item

            # 长期记忆容量控制
            await self._enforce_long_term_capacity()

        # 持久化到 LanceDB
        if self._table:
            await self._persist_memory(item)

        logger.info(
            f"添加记忆: id={item.memory_id}, type={memory_type.value}, "
            f"importance={importance}, content={content[:50]}"
        )
        return item

    async def add_conversation_turn(
        self,
        role: str,
        content: str,
        session_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> ConversationTurn:
        """添加对话轮次

        Args:
            role: 角色 (user/assistant/system)
            content: 内容
            session_id: 会话 ID
            metadata: 元数据

        Returns:
            对话轮次
        """
        turn = ConversationTurn(
            role=role,
            content=content,
            metadata=metadata or {},
        )

        # 获取或创建会话
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
        else:
            session = ConversationSession(
                session_id=session_id or str(uuid4()),
            )
            self._sessions[session.session_id] = session

        session.turns.append(turn)
        session.last_active = datetime.now()

        # 自动摘要检查
        if len(session.turns) >= self._config.auto_summarize_threshold:
            await self._auto_summarize_session(session)

        logger.debug(
            f"添加对话轮次: session={session.session_id}, role={role}, "
            f"turns={len(session.turns)}"
        )
        return turn

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        memory_types: Optional[list[MemoryType]] = None,
        session_id: Optional[str] = None,
        max_results: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
    ) -> list[SearchResult]:
        """语义检索记忆

        Args:
            query: 查询文本
            memory_types: 过滤记忆类型
            session_id: 过滤会话 ID
            max_results: 最大结果数
            similarity_threshold: 相似度阈值

        Returns:
            检索结果列表
        """
        if not self._initialized:
            await self.initialize()

        max_results = max_results or self._config.max_search_results
        threshold = similarity_threshold or self._config.similarity_threshold

        # 生成查询向量
        query_embedding = await self._get_embedding(query)
        if not query_embedding:
            logger.warning("查询 embedding 生成失败，回退到关键词搜索")
            return self._keyword_search(query, max_results)

        results: list[SearchResult] = []

        # 从 LanceDB 检索
        if self._table:
            try:
                search_results = await asyncio.to_thread(
                    self._table.search,
                    query_embedding,
                    vector_column_name="vector",
                )
                search_results = await asyncio.to_thread(
                    search_results.limit, max_results * 2  # 多检索一些用于过滤
                )
                rows = await asyncio.to_thread(search_results.to_list)

                for row in rows:
                    distance = row.get("_distance", 1.0)
                    similarity = 1.0 - distance

                    if similarity < threshold:
                        continue

                    # 过滤记忆类型
                    row_type = row.get("memory_type", "")
                    if memory_types:
                        if row_type not in [mt.value for mt in memory_types]:
                            continue

                    # 过滤会话
                    if session_id and row.get("session_id", "") != session_id:
                        continue

                    memory = self._row_to_memory(row)
                    results.append(SearchResult(
                        memory=memory,
                        score=similarity,
                        match_type="semantic",
                    ))

                    if len(results) >= max_results:
                        break

            except Exception as e:
                logger.warning(f"LanceDB 检索异常: {e}")

        # 从内存中检索（补充）
        if len(results) < max_results:
            memory_results = self._search_in_memory(
                query_embedding, threshold, memory_types, session_id,
                max_results - len(results)
            )
            results.extend(memory_results)

        # 按分数排序
        results.sort(key=lambda r: r.score, reverse=True)

        # 更新访问记录
        for result in results[:max_results]:
            result.memory.update_access()

        logger.info(
            f"语义检索: query={query[:50]}, results={len(results)}, "
            f"top_score={results[0].score:.4f if results else 0}"
        )
        return results[:max_results]

    def _search_in_memory(
        self,
        query_embedding: list[float],
        threshold: float,
        memory_types: Optional[list[MemoryType]],
        session_id: Optional[str],
        max_results: int,
    ) -> list[SearchResult]:
        """从内存中检索"""
        results: list[SearchResult] = []

        all_memories = list(self._short_term.values()) + list(self._long_term.values())

        for memory in all_memories:
            if not memory.embedding:
                continue

            # 过滤
            if memory_types and memory.memory_type not in memory_types:
                continue
            if session_id and memory.session_id != session_id:
                continue

            # 计算相似度
            similarity = self._cosine_similarity(query_embedding, memory.embedding)
            if similarity >= threshold:
                results.append(SearchResult(
                    memory=memory,
                    score=similarity,
                    match_type="semantic",
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def _keyword_search(self, query: str, max_results: int) -> list[SearchResult]:
        """关键词搜索（回退方案）"""
        query_lower = query.lower()
        results: list[SearchResult] = []

        all_memories = list(self._short_term.values()) + list(self._long_term.values())

        for memory in all_memories:
            content_lower = memory.content.lower()
            if query_lower in content_lower:
                # 计算简单匹配分数
                match_ratio = len(query_lower) / len(content_lower)
                results.append(SearchResult(
                    memory=memory,
                    score=match_ratio,
                    match_type="keyword",
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    # ------------------------------------------------------------------
    # 记忆巩固
    # ------------------------------------------------------------------

    async def consolidate(self) -> int:
        """巩固短期记忆到长期记忆

        基于重要性和访问频率决定哪些短期记忆值得保留。

        Returns:
            巩固的记忆数
        """
        consolidated = 0
        to_consolidate: list[MemoryItem] = []

        for memory in list(self._short_term.values()):
            # 跳过已过期的
            if memory.is_expired():
                continue

            # 计算巩固分数
            relevance = memory.calculate_relevance()

            if relevance >= self._config.consolidation_threshold:
                to_consolidate.append(memory)

        for memory in to_consolidate:
            # 转为长期记忆
            memory.memory_type = MemoryType.LONG_TERM
            memory.status = MemoryStatus.CONSOLIDATED
            memory.expires_at = None

            # 移动存储
            del self._short_term[memory.memory_id]
            self._long_term[memory.memory_id] = memory

            # 更新持久化
            if self._table:
                await self._update_memory(memory)

            consolidated += 1

        if consolidated > 0:
            logger.info(f"记忆巩固完成: {consolidated} 条短期记忆转为长期记忆")

        return consolidated

    async def _auto_summarize_session(self, session: ConversationSession) -> None:
        """自动摘要会话"""
        # 简单实现：取前几轮和后几轮作为摘要
        turns = session.turns
        if len(turns) < 5:
            return

        summary_parts = []
        # 取前 2 轮
        for turn in turns[:2]:
            summary_parts.append(f"[{turn.role}] {turn.content[:100]}")

        summary_parts.append("...")

        # 取后 2 轮
        for turn in turns[-2:]:
            summary_parts.append(f"[{turn.role}] {turn.content[:100]}")

        session.summary = "\n".join(summary_parts)

        # 将摘要作为长期记忆保存
        await self.add_memory(
            content=session.summary,
            memory_type=MemoryType.EPISODIC,
            session_id=session.session_id,
            importance=0.6,
            tags=["conversation_summary"],
        )

        logger.debug(f"会话自动摘要: session={session.session_id}")

    # ------------------------------------------------------------------
    # 记忆衰减
    # ------------------------------------------------------------------

    async def _apply_decay(self) -> None:
        """应用记忆衰减"""
        decayed = 0
        forgotten = 0

        # 处理短期记忆
        for memory_id in list(self._short_term.keys()):
            memory = self._short_term[memory_id]
            if memory.is_expired():
                del self._short_term[memory_id]
                forgotten += 1
                continue

            relevance = memory.calculate_relevance()
            if relevance < 0.1:
                memory.status = MemoryStatus.FORGOTTEN
                del self._short_term[memory_id]
                forgotten += 1

        # 处理长期记忆（衰减但不删除）
        for memory in self._long_term.values():
            relevance = memory.calculate_relevance()
            if relevance < 0.2:
                memory.status = MemoryStatus.ARCHIVED
                decayed += 1

        if decayed > 0 or forgotten > 0:
            logger.info(f"记忆衰减: decayed={decayed}, forgotten={forgotten}")

    async def _enforce_short_term_window(self) -> None:
        """强制短期记忆窗口大小"""
        if len(self._short_term) <= self._config.short_term_window:
            return

        # 按重要性排序，移除低重要性的
        sorted_memories = sorted(
            self._short_term.values(),
            key=lambda m: m.calculate_relevance(),
        )

        excess = len(self._short_term) - self._config.short_term_window
        for memory in sorted_memories[:excess]:
            del self._short_term[memory.memory_id]

    async def _enforce_long_term_capacity(self) -> None:
        """强制长期记忆容量"""
        if len(self._long_term) <= self._config.max_long_term_memories:
            return

        # 按相关性排序，归档低相关性的
        sorted_memories = sorted(
            self._long_term.values(),
            key=lambda m: m.calculate_relevance(),
        )

        excess = len(self._long_term) - self._config.max_long_term_memories
        for memory in sorted_memories[:excess]:
            memory.status = MemoryStatus.ARCHIVED

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[ConversationSession]:
        """获取会话"""
        return self._sessions.get(session_id)

    def get_recent_turns(
        self,
        session_id: str,
        n: Optional[int] = None,
    ) -> list[ConversationTurn]:
        """获取最近 N 轮对话"""
        session = self._sessions.get(session_id)
        if not session:
            return []

        n = n or self._config.short_term_window
        return session.turns[-n:]

    def clear_session(self, session_id: str) -> bool:
        """清除会话"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.debug(f"清除会话: {session_id}")
            return True
        return False

    # ------------------------------------------------------------------
    # 统计接口
    # ------------------------------------------------------------------

    def get_stats(self) -> MemoryStats:
        """获取记忆统计"""
        all_memories = list(self._short_term.values()) + list(self._long_term.values())

        type_counts: dict[str, int] = {}
        total_importance = 0.0

        for memory in all_memories:
            type_key = memory.memory_type.value
            type_counts[type_key] = type_counts.get(type_key, 0) + 1
            total_importance += memory.importance

        avg_importance = total_importance / len(all_memories) if all_memories else 0.0

        return MemoryStats(
            total_memories=len(all_memories),
            short_term_count=len(self._short_term),
            long_term_count=len(self._long_term),
            episodic_count=type_counts.get("episodic", 0),
            semantic_count=type_counts.get("semantic", 0),
            procedural_count=type_counts.get("procedural", 0),
            active_sessions=len(self._sessions),
            avg_importance=avg_importance,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量表示"""
        cache_key = text.strip()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        settings = get_settings()
        api_key = settings.model.openai_api_key
        base_url = settings.model.openai_base_url
        model = self._config.embedding_model

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
                embedding = data["data"][0]["embedding"]

                # 缓存
                if len(self._embedding_cache) < 2000:
                    self._embedding_cache[cache_key] = embedding

                return embedding
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            return []

    async def _persist_memory(self, memory: MemoryItem) -> None:
        """持久化记忆到 LanceDB"""
        if not self._table:
            return

        try:
            dim = self._config.embedding_dim or 1536
            row = {
                "memory_id": memory.memory_id,
                "content": memory.content,
                "memory_type": memory.memory_type.value,
                "priority": memory.priority.value,
                "status": memory.status.value,
                "vector": memory.embedding if memory.embedding else [0.0] * dim,
                "session_id": memory.session_id,
                "user_id": memory.user_id,
                "source": memory.source,
                "tags_json": json.dumps(memory.tags, ensure_ascii=False),
                "importance": memory.importance,
                "access_count": memory.access_count,
                "created_at": memory.created_at.isoformat(),
                "last_accessed": memory.last_accessed.isoformat(),
                "expires_at": memory.expires_at.isoformat() if memory.expires_at else "",
                "metadata_json": json.dumps(memory.metadata, ensure_ascii=False),
            }
            await asyncio.to_thread(self._table.add, [row])
        except Exception as e:
            logger.error(f"记忆持久化失败: {e}")

    async def _update_memory(self, memory: MemoryItem) -> None:
        """更新持久化的记忆"""
        if not self._table:
            return

        try:
            # 删除旧记录
            await asyncio.to_thread(
                self._table.delete,
                f"memory_id = '{memory.memory_id}'"
            )
            # 插入新记录
            await self._persist_memory(memory)
        except Exception as e:
            logger.error(f"记忆更新失败: {e}")

    @staticmethod
    def _row_to_memory(row: dict[str, Any]) -> MemoryItem:
        """LanceDB 行转 MemoryItem"""
        return MemoryItem(
            memory_id=row.get("memory_id", ""),
            content=row.get("content", ""),
            memory_type=MemoryType(row.get("memory_type", "short_term")),
            priority=MemoryPriority(row.get("priority", "normal")),
            status=MemoryStatus(row.get("status", "active")),
            embedding=row.get("vector", []),
            session_id=row.get("session_id", ""),
            user_id=row.get("user_id", ""),
            source=row.get("source", ""),
            tags=json.loads(row.get("tags_json", "[]")),
            importance=float(row.get("importance", 0.5)),
            access_count=int(row.get("access_count", 0)),
            created_at=(
                datetime.fromisoformat(row["created_at"])
                if row.get("created_at") else datetime.now()
            ),
            last_accessed=(
                datetime.fromisoformat(row["last_accessed"])
                if row.get("last_accessed") else datetime.now()
            ),
            expires_at=(
                datetime.fromisoformat(row["expires_at"])
                if row.get("expires_at") else None
            ),
            metadata=json.loads(row.get("metadata_json", "{}")),
        )

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

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> MemoryManager:
        """使用默认配置创建"""
        return cls(MemoryManagerConfig())

    @classmethod
    def create_with_config(
        cls,
        *,
        short_term_window: int = 20,
        max_long_term_memories: int = 10000,
        similarity_threshold: float = 0.7,
    ) -> MemoryManager:
        """使用自定义参数创建"""
        return cls(MemoryManagerConfig(
            short_term_window=short_term_window,
            max_long_term_memories=max_long_term_memories,
            similarity_threshold=similarity_threshold,
        ))
