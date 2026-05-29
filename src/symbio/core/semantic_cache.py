"""语义缓存引擎 - 基于向量相似度的 LLM 响应复用与 Prompt Cache 整合"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import lancedb
import pyarrow as pa
from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("semantic_cache")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class InvalidationStrategy(str, Enum):
    """缓存失效策略类型"""
    TTL = "ttl"              # 基于时间过期
    VERSION = "version"      # 基于版本号变更
    CONTEXT = "context"      # 基于上下文 hash 变更
    MANUAL = "manual"        # 手动失效


class CacheEntry(BaseModel):
    """单条语义缓存记录"""
    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    query_text: str                                  # 原始查询文本
    response_text: str                               # LLM 响应内容
    model: str = ""                                  # 生成响应的模型
    embedding: list[float] = Field(default_factory=list)  # 查询文本的向量

    # 失效控制
    version: str = "1.0.0"                           # 关联的 Prompt / Skill 版本
    context_hash: str = ""                           # 上下文指纹（系统提示词、工具列表等）
    ttl_seconds: int = 3600                          # 生存时间（秒）
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None            # 过期时间（由 ttl 计算）

    # Prompt Cache 整合
    prompt_cache_prefix: str = ""                    # 可复用的前缀缓存标识
    prompt_prefix_hash: str = ""                     # 前缀内容的 hash，用于前缀匹配
    cache_control_hint: str = ""                     # 传递给 LLM API 的缓存控制标记

    # 统计
    hit_count: int = 0                               # 命中次数
    last_hit_at: Optional[datetime] = None           # 最近一次命中时间

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self) -> bool:
        """判断是否已过期"""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def is_version_valid(self, current_version: str) -> bool:
        """判断版本是否仍然有效"""
        return self.version == current_version

    def is_context_valid(self, current_context_hash: str) -> bool:
        """判断上下文是否仍然有效"""
        if not self.context_hash or not current_context_hash:
            return True
        return self.context_hash == current_context_hash


class CacheStats(BaseModel):
    """缓存命中率统计"""
    total_queries: int = 0           # 总查询次数
    cache_hits: int = 0              # 命中次数
    cache_misses: int = 0            # 未命中次数
    hit_rate: float = 0.0            # 命中率
    avg_similarity_on_hit: float = 0.0   # 命中时的平均相似度
    total_entries: int = 0           # 当前缓存条目数
    expired_entries: int = 0         # 已过期但未清理的条目数
    prompt_cache_aligned: int = 0    # 已对齐 Prompt Cache 的条目数
    estimated_token_saved: int = 0   # 估算节省的 Token 数
    last_reset_at: datetime = Field(default_factory=datetime.now)

    def update_hit_rate(self) -> None:
        """重新计算命中率"""
        total = self.cache_hits + self.cache_misses
        self.hit_rate = self.cache_hits / total if total > 0 else 0.0


class SemanticCacheConfig(BaseModel):
    """语义缓存配置"""
    enabled: bool = True
    lancedb_path: str = ""                           # 为空则自动拼接
    table_name: str = "semantic_cache"
    embedding_model: str = ""                        # 为空则使用 MemoryConfig
    embedding_dim: int = 0                           # 为空则使用 MemoryConfig
    similarity_threshold: float = 0.92               # 语义缓存要求高阈值
    default_ttl_seconds: int = 3600                  # 默认 1 小时
    max_entries: int = 50000                         # 最大缓存条目
    cleanup_interval_seconds: int = 300              # 清理间隔（秒）
    prompt_cache_enabled: bool = True                # 启用 Prompt Cache 整合
    stats_reset_interval_seconds: int = 86400        # 统计重置间隔（默认每天）


# ---------------------------------------------------------------------------
# LanceDB Schema
# ---------------------------------------------------------------------------

CACHE_TABLE_SCHEMA = pa.schema([
    pa.field("entry_id", pa.string()),
    pa.field("query_text", pa.string()),
    pa.field("response_text", pa.string()),
    pa.field("model", pa.string()),
    pa.field("vector", pa.list_(pa.float32())),
    pa.field("version", pa.string()),
    pa.field("context_hash", pa.string()),
    pa.field("ttl_seconds", pa.int64()),
    pa.field("created_at", pa.string()),
    pa.field("expires_at", pa.string()),
    pa.field("prompt_cache_prefix", pa.string()),
    pa.field("prompt_prefix_hash", pa.string()),
    pa.field("cache_control_hint", pa.string()),
    pa.field("hit_count", pa.int64()),
    pa.field("last_hit_at", pa.string()),
    pa.field("metadata_json", pa.string()),
])


# ---------------------------------------------------------------------------
# 语义缓存引擎
# ---------------------------------------------------------------------------

class SemanticCacheEngine:
    """语义缓存引擎

    核心能力：
    1. 向量相似度匹配 — "帮我写个快排"和"实现快速排序算法"语义相同，结果可复用
    2. 智能失效策略 — 基于 TTL、版本号、上下文变更的多维失效
    3. Prompt Cache 深度整合 — 语义缓存 + 前缀缓存双重优化
    4. 命中率统计 — 实时追踪缓存效果，指导调优

    Usage:
        engine = SemanticCacheEngine()
        await engine.initialize()

        # 查询缓存
        hit = await engine.get("帮我写个快排")

        # 写入缓存
        await engine.put(
            query="帮我写个快排",
            response="def quicksort(arr): ...",
            model="claude-sonnet-4-20250514",
        )

        # 获取统计
        stats = engine.get_stats()
    """

    def __init__(self, config: Optional[SemanticCacheConfig] = None):
        settings = get_settings()
        self._config = config or SemanticCacheConfig()

        # 从全局配置补全默认值
        if not self._config.lancedb_path:
            self._config.lancedb_path = str(
                Path(settings.memory.lancedb_path) / "semantic_cache"
            )
        if not self._config.embedding_model:
            self._config.embedding_model = settings.memory.embedding_model
        if not self._config.embedding_dim:
            self._config.embedding_dim = settings.memory.embedding_dim

        self._db: Optional[lancedb.DBConnection] = None
        self._table: Optional[lancedb.table.Table] = None

        # 内存中的统计（定期持久化到 LanceDB metadata 行）
        self._stats = CacheStats()
        # embedding 缓存（query_text -> embedding），避免重复调用 API
        self._embedding_cache: dict[str, list[float]] = {}
        self._embedding_cache_max = 2000

        # 后台清理任务句柄
        self._cleanup_task: Optional[asyncio.Task] = None
        self._initialized = False

        logger.info(
            f"SemanticCacheEngine 创建: path={self._config.lancedb_path}, "
            f"threshold={self._config.similarity_threshold}, "
            f"prompt_cache={'on' if self._config.prompt_cache_enabled else 'off'}"
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化 LanceDB 连接和表结构"""
        if self._initialized:
            return

        db_path = Path(self._config.lancedb_path)
        db_path.mkdir(parents=True, exist_ok=True)

        self._db = await asyncio.to_thread(lancedb.connect, str(db_path))
        logger.info(f"LanceDB 连接成功: {db_path}")

        # 尝试打开已有表，不存在则创建
        table_names = await asyncio.to_thread(self._db.table_names)
        if self._config.table_name in table_names:
            self._table = await asyncio.to_thread(
                self._db.open_table, self._config.table_name
            )
            row_count = await asyncio.to_thread(
                self._table.count_rows
            )
            logger.info(f"打开已有缓存表: {self._config.table_name}, 条目数={row_count}")
        else:
            # 创建空表
            self._table = await asyncio.to_thread(
                self._db.create_table,
                self._config.table_name,
                schema=CACHE_TABLE_SCHEMA,
            )
            logger.info(f"创建缓存表: {self._config.table_name}")

        self._initialized = True

        # 启动后台清理
        self._start_cleanup_task()

    async def close(self) -> None:
        """关闭引擎，停止后台任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._initialized = False
        logger.info("SemanticCacheEngine 已关闭")

    def _start_cleanup_task(self) -> None:
        """启动后台过期清理任务"""
        async def _cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(self._config.cleanup_interval_seconds)
                    await self._cleanup_expired()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"后台清理异常: {e}")

        self._cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.debug(
            f"后台清理任务已启动，间隔={self._config.cleanup_interval_seconds}s"
        )

    # ------------------------------------------------------------------
    # 核心读写
    # ------------------------------------------------------------------

    async def get(
        self,
        query: str,
        *,
        current_version: str = "",
        current_context_hash: str = "",
        model: Optional[str] = None,
    ) -> Optional[CacheEntry]:
        """语义匹配查询缓存

        Args:
            query: 用户查询文本
            current_version: 当前 Prompt/Skill 版本，用于版本失效校验
            current_context_hash: 当前上下文 hash，用于上下文失效校验
            model: 指定模型（仅命中同模型缓存时返回）

        Returns:
            命中的 CacheEntry，未命中返回 None
        """
        if not self._initialized:
            await self.initialize()

        self._stats.total_queries += 1

        # 1. 生成查询向量
        embedding = await self._get_embedding(query)
        if not embedding:
            self._stats.cache_misses += 1
            self._stats.update_hit_rate()
            logger.debug(f"Embedding 生成失败，缓存未命中: {query[:50]}")
            return None

        # 2. 向量相似度检索
        try:
            results = await asyncio.to_thread(
                self._table.search,
                embedding,
                vector_column_name="vector",
            )
            results = await asyncio.to_thread(
                results.limit, 5
            )
            rows = await asyncio.to_thread(results.to_list)
        except Exception as e:
            self._stats.cache_misses += 1
            self._stats.update_hit_rate()
            logger.warning(f"LanceDB 检索异常: {e}")
            return None

        if not rows:
            self._stats.cache_misses += 1
            self._stats.update_hit_rate()
            logger.debug(f"缓存为空，未命中: {query[:50]}")
            return None

        # 3. 找到第一个满足所有条件的条目
        for row in rows:
            distance = row.get("_distance", 1.0)
            similarity = 1.0 - distance  # LanceDB 默认 L2 距离，转为相似度

            if similarity < self._config.similarity_threshold:
                continue

            entry = self._row_to_entry(row)

            # TTL 失效检查
            if entry.is_expired():
                logger.debug(f"缓存已过期: {entry.entry_id}")
                continue

            # 版本失效检查
            if current_version and not entry.is_version_valid(current_version):
                logger.debug(
                    f"缓存版本不匹配: entry_version={entry.version}, "
                    f"current={current_version}"
                )
                continue

            # 上下文失效检查
            if current_context_hash and not entry.is_context_valid(current_context_hash):
                logger.debug(
                    f"缓存上下文不匹配: entry_context={entry.context_hash[:16]}, "
                    f"current={current_context_hash[:16]}"
                )
                continue

            # 模型过滤
            if model and entry.model and entry.model != model:
                logger.debug(
                    f"缓存模型不匹配: entry_model={entry.model}, requested={model}"
                )
                continue

            # 4. 命中 — 更新统计
            entry.hit_count += 1
            entry.last_hit_at = datetime.now()
            await self._update_entry_hits(entry.entry_id, entry.hit_count)

            self._stats.cache_hits += 1
            self._stats.update_hit_rate()
            if self._stats.avg_similarity_on_hit == 0.0:
                self._stats.avg_similarity_on_hit = similarity
            else:
                # 滚动平均
                self._stats.avg_similarity_on_hit = (
                    self._stats.avg_similarity_on_hit * 0.9 + similarity * 0.1
                )
            self._stats.estimated_token_saved += len(entry.response_text) // 4

            logger.info(
                f"缓存命中: similarity={similarity:.4f}, "
                f"entry_id={entry.entry_id}, hit_count={entry.hit_count}"
            )
            return entry

        # 全部不满足
        self._stats.cache_misses += 1
        self._stats.update_hit_rate()
        logger.debug(f"无满足阈值的缓存条目: {query[:50]}")
        return None

    async def put(
        self,
        query: str,
        response: str,
        *,
        model: str = "",
        version: str = "1.0.0",
        context_hash: str = "",
        ttl_seconds: Optional[int] = None,
        prompt_cache_prefix: str = "",
        prompt_prefix_hash: str = "",
        cache_control_hint: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> CacheEntry:
        """写入语义缓存条目

        Args:
            query: 查询文本
            response: LLM 响应内容
            model: 生成响应的模型
            version: Prompt/Skill 版本号
            context_hash: 上下文指纹
            ttl_seconds: 生存时间，None 使用默认值
            prompt_cache_prefix: 可复用的前缀缓存标识
            prompt_prefix_hash: 前缀内容 hash
            cache_control_hint: LLM API 缓存控制标记
            metadata: 额外元数据

        Returns:
            写入的 CacheEntry
        """
        if not self._initialized:
            await self.initialize()

        effective_ttl = ttl_seconds if ttl_seconds is not None else self._config.default_ttl_seconds
        now = datetime.now()

        # 生成向量
        embedding = await self._get_embedding(query)

        entry = CacheEntry(
            query_text=query,
            response_text=response,
            model=model,
            embedding=embedding or [],
            version=version,
            context_hash=context_hash,
            ttl_seconds=effective_ttl,
            created_at=now,
            expires_at=now + timedelta(seconds=effective_ttl),
            prompt_cache_prefix=prompt_cache_prefix,
            prompt_prefix_hash=prompt_prefix_hash,
            cache_control_hint=cache_control_hint,
            metadata=metadata or {},
        )

        # 写入 LanceDB
        row = self._entry_to_row(entry)
        try:
            await asyncio.to_thread(self._table.add, [row])
            logger.info(
                f"缓存写入: entry_id={entry.entry_id}, "
                f"query={query[:50]}, ttl={effective_ttl}s"
            )
        except Exception as e:
            logger.error(f"缓存写入失败: {e}")
            raise

        # 容量检查
        await self._enforce_max_entries()

        return entry

    # ------------------------------------------------------------------
    # 缓存失效
    # ------------------------------------------------------------------

    async def invalidate_entry(self, entry_id: str) -> bool:
        """手动失效单条缓存

        Args:
            entry_id: 缓存条目 ID

        Returns:
            是否成功删除
        """
        if not self._initialized:
            await self.initialize()

        try:
            await asyncio.to_thread(
                self._table.delete, f"entry_id = '{entry_id}'"
            )
            logger.info(f"手动失效缓存: {entry_id}")
            return True
        except Exception as e:
            logger.warning(f"缓存失效失败: {entry_id}, error={e}")
            return False

    async def invalidate_by_version(self, version: str) -> int:
        """按版本号批量失效缓存

        当 Prompt 或 Skill 版本升级时，旧版本缓存全部失效。

        Args:
            version: 要失效的版本号

        Returns:
            失效的条目数
        """
        if not self._initialized:
            await self.initialize()

        try:
            # 先查出要删除的数量
            count_before = await asyncio.to_thread(self._table.count_rows)
            await asyncio.to_thread(
                self._table.delete, f"version = '{version}'"
            )
            count_after = await asyncio.to_thread(self._table.count_rows)
            deleted = count_before - count_after
            if deleted > 0:
                logger.info(f"按版本失效缓存: version={version}, 删除 {deleted} 条")
            return deleted
        except Exception as e:
            logger.error(f"按版本失效缓存失败: {e}")
            return 0

    async def invalidate_by_context(self, context_hash: str) -> int:
        """按上下文 hash 批量失效缓存

        当系统提示词、工具列表等上下文发生变更时，旧上下文缓存失效。

        Args:
            context_hash: 要失效的上下文 hash

        Returns:
            失效的条目数
        """
        if not self._initialized:
            await self.initialize()

        try:
            count_before = await asyncio.to_thread(self._table.count_rows)
            await asyncio.to_thread(
                self._table.delete, f"context_hash = '{context_hash}'"
            )
            count_after = await asyncio.to_thread(self._table.count_rows)
            deleted = count_before - count_after
            if deleted > 0:
                logger.info(
                    f"按上下文失效缓存: context={context_hash[:16]}, 删除 {deleted} 条"
                )
            return deleted
        except Exception as e:
            logger.error(f"按上下文失效缓存失败: {e}")
            return 0

    async def invalidate_all(self) -> None:
        """清空全部缓存"""
        if not self._initialized:
            await self.initialize()

        try:
            # 重建空表
            if self._config.table_name in await asyncio.to_thread(
                self._db.table_names
            ):
                await asyncio.to_thread(
                    self._db.drop_table, self._config.table_name
                )
            self._table = await asyncio.to_thread(
                self._db.create_table,
                self._config.table_name,
                schema=CACHE_TABLE_SCHEMA,
            )
            logger.warning("全部缓存已清空")
        except Exception as e:
            logger.error(f"清空缓存失败: {e}")
            raise

    async def _cleanup_expired(self) -> int:
        """清理所有已过期的缓存条目

        Returns:
            清理的条目数
        """
        if not self._initialized or self._table is None:
            return 0

        now_iso = datetime.now().isoformat()
        try:
            count_before = await asyncio.to_thread(self._table.count_rows)
            # 删除 expires_at 不为空且已过期的条目
            await asyncio.to_thread(
                self._table.delete,
                f"expires_at IS NOT NULL AND expires_at < '{now_iso}'",
            )
            count_after = await asyncio.to_thread(self._table.count_rows)
            deleted = count_before - count_after
            if deleted > 0:
                logger.info(f"清理过期缓存: {deleted} 条")
            return deleted
        except Exception as e:
            logger.error(f"清理过期缓存异常: {e}")
            return 0

    # ------------------------------------------------------------------
    # Prompt Cache 整合
    # ------------------------------------------------------------------

    async def align_prompt_cache(
        self,
        prompt_prefix: str,
        cache_control_hint: str = "ephemeral",
    ) -> str:
        """对齐 Prompt Cache 前缀

        计算前缀的 hash，用于后续 put 时关联。当多个缓存条目共享同一前缀时，
        Orchestrator 可以将该前缀作为 LLM API 请求的缓存控制区域，实现
        "语义缓存 + 前缀缓存"双重优化。

        Args:
            prompt_prefix: 系统提示词或公共前缀文本
            cache_control_hint: 缓存控制标记（ephemeral / persistent）

        Returns:
            前缀 hash（hex string）
        """
        prefix_hash = hashlib.sha256(
            prompt_prefix.encode("utf-8")
        ).hexdigest()[:32]

        logger.debug(
            f"Prompt Cache 前缀对齐: hash={prefix_hash}, "
            f"hint={cache_control_hint}, prefix_len={len(prompt_prefix)}"
        )
        return prefix_hash

    async def get_prompt_cache_entries(
        self,
        prompt_prefix_hash: str,
    ) -> list[CacheEntry]:
        """获取共享同一前缀的所有缓存条目

        供 Orchestrator 在构造 LLM 请求时参考，决定哪些请求可以复用
        前缀缓存，减少 input token 成本。

        Args:
            prompt_prefix_hash: 前缀 hash

        Returns:
            匹配的缓存条目列表
        """
        if not self._initialized:
            await self.initialize()

        try:
            results = await asyncio.to_thread(
                self._table.query,
                where=f"prompt_prefix_hash = '{prompt_prefix_hash}'",
            )
            rows = await asyncio.to_thread(results.to_list)
            entries = []
            for row in rows:
                entry = self._row_to_entry(row)
                if not entry.is_expired():
                    entries.append(entry)
            logger.debug(
                f"Prompt Cache 条目查询: hash={prompt_prefix_hash}, "
                f"命中 {len(entries)} 条"
            )
            return entries
        except Exception as e:
            logger.warning(f"Prompt Cache 条目查询失败: {e}")
            return []

    async def get_prompt_cache_stats(self) -> dict[str, Any]:
        """获取 Prompt Cache 整合统计

        Returns:
            包含前缀复用率等指标的字典
        """
        if not self._initialized:
            await self.initialize()

        try:
            all_rows = await asyncio.to_thread(
                self._table.query,
                where="prompt_prefix_hash IS NOT NULL AND prompt_prefix_hash != ''",
            )
            rows = await asyncio.to_thread(all_rows.to_list)

            # 按 prefix_hash 分组统计
            prefix_groups: dict[str, int] = {}
            total_aligned = 0
            for row in rows:
                h = row.get("prompt_prefix_hash", "")
                if h:
                    prefix_groups[h] = prefix_groups.get(h, 0) + 1
                    total_aligned += 1

            # 可复用前缀数（至少 2 个条目共享同一前缀）
            reusable_prefixes = sum(
                1 for count in prefix_groups.values() if count >= 2
            )

            return {
                "total_aligned_entries": total_aligned,
                "unique_prefixes": len(prefix_groups),
                "reusable_prefixes": reusable_prefixes,
                "prefix_distribution": prefix_groups,
            }
        except Exception as e:
            logger.warning(f"Prompt Cache 统计查询失败: {e}")
            return {
                "total_aligned_entries": 0,
                "unique_prefixes": 0,
                "reusable_prefixes": 0,
                "prefix_distribution": {},
            }

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self) -> CacheStats:
        """获取缓存命中率统计"""
        return self._stats.model_copy()

    def reset_stats(self) -> None:
        """重置统计数据"""
        self._stats = CacheStats()
        logger.info("缓存统计数据已重置")

    async def get_entry_count(self) -> int:
        """获取当前缓存条目总数"""
        if not self._initialized:
            await self.initialize()
        try:
            return await asyncio.to_thread(self._table.count_rows)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量表示

        优先使用内存缓存，缓存未命中则调用 embedding API。
        当前实现使用 httpx 调用 OpenAI 兼容的 embedding 接口。

        Args:
            text: 待向量化的文本

        Returns:
            向量列表，失败返回空列表
        """
        # 内存缓存命中
        cache_key = text.strip()
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        settings = get_settings()
        api_key = settings.model.openai_api_key
        base_url = settings.model.openai_base_url
        model = self._config.embedding_model

        if not api_key:
            logger.warning(
                "未配置 OpenAI API Key，无法生成 embedding，"
                "语义缓存将使用降级模式（仅精确匹配）"
            )
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
                    json={
                        "model": model,
                        "input": text,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                embedding = data["data"][0]["embedding"]

                # 写入内存缓存
                if len(self._embedding_cache) < self._embedding_cache_max:
                    self._embedding_cache[cache_key] = embedding

                return embedding
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 行 <-> Entry 转换
    # ------------------------------------------------------------------

    def _entry_to_row(self, entry: CacheEntry) -> dict[str, Any]:
        """CacheEntry -> LanceDB row dict"""
        dim = self._config.embedding_dim or 1536
        return {
            "entry_id": entry.entry_id,
            "query_text": entry.query_text,
            "response_text": entry.response_text,
            "model": entry.model,
            "vector": entry.embedding if entry.embedding else [0.0] * dim,
            "version": entry.version,
            "context_hash": entry.context_hash,
            "ttl_seconds": entry.ttl_seconds,
            "created_at": entry.created_at.isoformat(),
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else "",
            "prompt_cache_prefix": entry.prompt_cache_prefix,
            "prompt_prefix_hash": entry.prompt_prefix_hash,
            "cache_control_hint": entry.cache_control_hint,
            "hit_count": entry.hit_count,
            "last_hit_at": entry.last_hit_at.isoformat() if entry.last_hit_at else "",
            "metadata_json": json.dumps(entry.metadata, ensure_ascii=False),
        }

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> CacheEntry:
        """LanceDB row dict -> CacheEntry"""
        created_at = row.get("created_at", "")
        expires_at = row.get("expires_at", "")
        last_hit_at = row.get("last_hit_at", "")
        metadata_json = row.get("metadata_json", "{}")

        return CacheEntry(
            entry_id=row.get("entry_id", ""),
            query_text=row.get("query_text", ""),
            response_text=row.get("response_text", ""),
            model=row.get("model", ""),
            embedding=row.get("vector", []),
            version=row.get("version", "1.0.0"),
            context_hash=row.get("context_hash", ""),
            ttl_seconds=int(row.get("ttl_seconds", 3600)),
            created_at=(
                datetime.fromisoformat(created_at)
                if created_at
                else datetime.now()
            ),
            expires_at=(
                datetime.fromisoformat(expires_at) if expires_at else None
            ),
            prompt_cache_prefix=row.get("prompt_cache_prefix", ""),
            prompt_prefix_hash=row.get("prompt_prefix_hash", ""),
            cache_control_hint=row.get("cache_control_hint", ""),
            hit_count=int(row.get("hit_count", 0)),
            last_hit_at=(
                datetime.fromisoformat(last_hit_at) if last_hit_at else None
            ),
            metadata=json.loads(metadata_json) if metadata_json else {},
        )

    async def _update_entry_hits(
        self, entry_id: str, new_hit_count: int
    ) -> None:
        """更新条目的命中计数（删除后重新插入）"""
        try:
            # LanceDB 不支持原生 update，用 delete + add 实现
            results = await asyncio.to_thread(
                self._table.query,
                where=f"entry_id = '{entry_id}'",
            )
            rows = await asyncio.to_thread(results.to_list)
            if rows:
                row = rows[0]
                row["hit_count"] = new_hit_count
                row["last_hit_at"] = datetime.now().isoformat()
                await asyncio.to_thread(
                    self._table.delete, f"entry_id = '{entry_id}'"
                )
                await asyncio.to_thread(self._table.add, [row])
        except Exception as e:
            logger.debug(f"更新命中计数失败（非致命）: {e}")

    async def _enforce_max_entries(self) -> None:
        """确保缓存条目不超过上限，超出时按 LRU 淘汰"""
        try:
            count = await asyncio.to_thread(self._table.count_rows)
            if count <= self._config.max_entries:
                return

            excess = count - self._config.max_entries
            logger.info(
                f"缓存条目超限: {count}/{self._config.max_entries}, "
                f"淘汰 {excess} 条"
            )

            # 查询所有条目的 entry_id 和 last_hit_at，避免加载全量数据到 pandas
            results = await asyncio.to_thread(
                self._table.query,
                columns=["entry_id", "last_hit_at"],
            )
            rows = await asyncio.to_thread(results.to_list)

            if not rows:
                return

            # last_hit_at 为空的视为最老，优先淘汰
            rows.sort(key=lambda r: r.get("last_hit_at") or "")
            to_evict = rows[:excess]

            for evict_row in to_evict:
                evict_id = evict_row["entry_id"]
                await asyncio.to_thread(
                    self._table.delete, f"entry_id = '{evict_id}'"
                )

            logger.info(f"LRU 淘汰完成: {excess} 条")
        except Exception as e:
            logger.error(f"容量控制失败: {e}")

    async def get_all_entries(self, limit: int = 100) -> list[CacheEntry]:
        """获取所有缓存条目（用于调试和监控）

        Args:
            limit: 最大返回数量

        Returns:
            CacheEntry 列表
        """
        if not self._initialized:
            await self.initialize()

        try:
            results = await asyncio.to_thread(
                self._table.query,
                where="1=1",
            )
            results = await asyncio.to_thread(results.limit, limit)
            rows = await asyncio.to_thread(results.to_list)
            entries = [self._row_to_entry(row) for row in rows]
            logger.debug(f"获取全部缓存条目: {len(entries)} 条")
            return entries
        except Exception as e:
            logger.warning(f"获取全部缓存条目失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 便捷工厂
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> SemanticCacheEngine:
        """使用默认配置创建引擎实例"""
        return cls(SemanticCacheConfig())

    @classmethod
    def create_with_config(
        cls,
        *,
        similarity_threshold: float = 0.92,
        default_ttl_seconds: int = 3600,
        max_entries: int = 50000,
        prompt_cache_enabled: bool = True,
    ) -> SemanticCacheEngine:
        """使用自定义参数创建引擎实例"""
        return cls(
            SemanticCacheConfig(
                similarity_threshold=similarity_threshold,
                default_ttl_seconds=default_ttl_seconds,
                max_entries=max_entries,
                prompt_cache_enabled=prompt_cache_enabled,
            )
        )
