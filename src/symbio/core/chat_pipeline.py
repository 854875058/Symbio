"""聊天运行时管线：语义缓存 → 上下文剪枝 → 成本记录。

把成本优化模块真正接入对话链路：
- 语义缓存（SemanticCacheEngine）：相似问题直接复用历史回答，上下文指纹保证等价性
- 上下文剪枝（ContextPruner）：超长历史裁剪到 Token 预算内
- 成本监控（CostTracker / BudgetManager）：每次调用持久化用量，支持预算告警

模型路由与工具懒加载由 Router 与 ToolLazyLoader 分别承担，
本模块负责把剩余三层接到 /api/chat 与 /ws/chat。

所有方法都不抛异常：任何一层失败只记日志并退回"无优化"路径，
绝不能因为优化层故障而阻断对话。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Optional

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("chat_pipeline")

# 缓存条目带上下文指纹，只有"同样的历史 + 相似的提问"才允许复用回答
_CONTEXT_SEPARATOR = "\x1e"


class ChatPipeline:
    """对话链路的成本优化管线（进程级单例，通过 get_chat_pipeline 获取）。"""

    def __init__(self) -> None:
        self._cache: Any = None
        self._cache_init_failed = False
        self._cache_lock = asyncio.Lock()
        self._pruner: Any = None
        self._cost_tracker: Any = None
        self._budget_manager: Any = None

    # ------------------------------------------------------------------
    # 语义缓存
    # ------------------------------------------------------------------

    def cache_available(self) -> bool:
        """语义缓存是否可用。

        有 OpenAI embedding key 时用远程 embedding；没有也行——SemanticCache
        默认带本地 embedding 降级（开箱即用）。仅当用户在配置里显式关闭
        semantic_cache_enabled 时整层关闭。
        """
        if self._cache_init_failed:
            return False
        cost = getattr(get_settings(), "cost", None)
        return bool(getattr(cost, "semantic_cache_enabled", True))

    async def _get_cache(self) -> Any:
        if not self.cache_available():
            return None
        if self._cache is None:
            async with self._cache_lock:
                if self._cache is None and not self._cache_init_failed:
                    try:
                        from symbio.core.semantic_cache import SemanticCacheEngine

                        cache = SemanticCacheEngine()
                        await cache.initialize()
                        self._cache = cache
                        logger.info("语义缓存已接入对话链路")
                    except Exception as e:
                        logger.warning(f"语义缓存初始化失败，本进程内禁用: {e}")
                        self._cache_init_failed = True
        return self._cache

    @staticmethod
    def context_hash(history: list[dict]) -> str:
        """对当前提问之前的历史做指纹，避免把依赖上下文的回答错误复用。"""
        payload = _CONTEXT_SEPARATOR.join(
            f"{m.get('role', '')}:{m.get('content', '')}" for m in history
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def lookup_cache(
        self, query: str, *, model: str = "", context_hash: str = ""
    ) -> Optional[dict[str, Any]]:
        """语义匹配查询缓存，命中返回 {content, entry_id, hit_count}。"""
        cache = await self._get_cache()
        if cache is None:
            return None
        try:
            entry = await cache.get(query, current_context_hash=context_hash, model=model or None)
        except Exception as e:
            logger.warning(f"语义缓存查询异常，按未命中处理: {e}")
            return None
        if entry is None:
            return None
        return {
            "content": entry.response_text,
            "entry_id": entry.entry_id,
            "hit_count": entry.hit_count,
        }

    async def store_cache(
        self, query: str, response: str, *, model: str = "", context_hash: str = ""
    ) -> None:
        """把新生成的回答写入语义缓存。"""
        cache = await self._get_cache()
        if cache is None:
            return
        try:
            await cache.put(query, response, model=model, context_hash=context_hash)
        except Exception as e:
            logger.warning(f"语义缓存写入失败（忽略）: {e}")

    async def cache_stats(self) -> dict[str, Any]:
        """缓存命中率统计，供成本仪表盘展示。"""
        if self._cache is None:
            return {
                "enabled": self.cache_available(),
                "total_queries": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "hit_rate": 0.0,
                "estimated_token_saved": 0,
                "entries": 0,
            }
        try:
            stats = self._cache.get_stats()
            entries = await self._cache.get_entry_count()
            return {
                "enabled": True,
                "total_queries": stats.total_queries,
                "cache_hits": stats.cache_hits,
                "cache_misses": stats.cache_misses,
                "hit_rate": stats.hit_rate,
                "estimated_token_saved": stats.estimated_token_saved,
                "entries": entries,
            }
        except Exception as e:
            logger.warning(f"读取缓存统计失败: {e}")
            return {"enabled": True, "error": str(e)}

    # ------------------------------------------------------------------
    # 上下文剪枝
    # ------------------------------------------------------------------

    def prune_history(
        self, messages: list[dict], *, max_tokens: Optional[int] = None
    ) -> tuple[list[dict], Optional[dict[str, Any]]]:
        """把超长对话历史裁剪到 Token 预算内。

        Args:
            messages: Anthropic 格式消息列表 [{"role", "content"}, ...]
            max_tokens: 目标预算，默认取 settings.cost.context_max_tokens

        Returns:
            (裁剪后的消息列表, 剪枝摘要 | None未剪枝)
        """
        if not messages:
            return messages, None
        try:
            from symbio.core.context_pruner import (
                ContextMessage,
                ContextPruner,
                MessageRole,
                PruneStrategy,
            )

            settings = get_settings()
            target = max_tokens or getattr(
                getattr(settings, "cost", None), "context_max_tokens", 8000
            )

            if self._pruner is None:
                self._pruner = ContextPruner.create_default()

            role_map = {
                "user": MessageRole.USER,
                "assistant": MessageRole.ASSISTANT,
                "system": MessageRole.SYSTEM,
                "tool": MessageRole.TOOL,
            }
            ctx_messages = []
            for i, m in enumerate(messages):
                content = str(m.get("content", ""))
                ctx_messages.append(
                    ContextMessage(
                        message_id=str(i),
                        role=role_map.get(m.get("role", "user"), MessageRole.USER),
                        content=content,
                        token_count=ContextPruner.estimate_token_count(content),
                    )
                )

            original_tokens = sum(m.token_count for m in ctx_messages)
            if original_tokens <= target:
                return messages, None

            result = self._pruner.prune(
                ctx_messages,
                strategy=PruneStrategy.FULL,
                target_token_count=target,
            )
            removed = set(result.removed_messages)
            if not removed:
                return messages, None

            pruned = [m for i, m in enumerate(messages) if str(i) not in removed]
            # 最后一条（当前提问）必须保留
            if messages and (not pruned or pruned[-1] is not messages[-1]):
                pruned.append(messages[-1])
            pruned = self._normalize_alternation(pruned)

            info = {
                "original_tokens": result.original_token_count,
                "pruned_tokens": result.pruned_token_count,
                "compression_ratio": result.compression_ratio,
                "removed_count": len(removed),
                "strategy": result.strategy_used.value,
            }
            logger.info(
                f"上下文剪枝: {info['original_tokens']} -> {info['pruned_tokens']} tokens, "
                f"移除 {info['removed_count']} 条消息"
            )
            return pruned, info
        except Exception as e:
            logger.warning(f"上下文剪枝失败，使用原始历史: {e}")
            return messages, None

    @staticmethod
    def _normalize_alternation(messages: list[dict]) -> list[dict]:
        """剪枝可能破坏 user/assistant 交替结构，这里合并同角色相邻消息并保证首条是 user。"""
        normalized: list[dict] = []
        for m in messages:
            if normalized and normalized[-1].get("role") == m.get("role"):
                normalized[-1] = {
                    "role": m.get("role"),
                    "content": f"{normalized[-1].get('content', '')}\n\n{m.get('content', '')}",
                }
            else:
                normalized.append(dict(m))
        while normalized and normalized[0].get("role") != "user":
            normalized.pop(0)
        return normalized

    # ------------------------------------------------------------------
    # 成本记录与预算
    # ------------------------------------------------------------------

    async def _get_cost_tracker(self) -> Any:
        if self._cost_tracker is None:
            try:
                from symbio.core.cost_monitor import get_cost_tracker

                self._cost_tracker = await get_cost_tracker()
            except Exception as e:
                logger.warning(f"成本追踪器初始化失败: {e}")
        return self._cost_tracker

    async def _get_budget_manager(self) -> Any:
        if self._budget_manager is None:
            try:
                from symbio.core.cost_monitor import get_budget_manager

                self._budget_manager = await get_budget_manager()
            except Exception as e:
                logger.warning(f"预算管理器初始化失败: {e}")
        return self._budget_manager

    async def record_usage(
        self,
        *,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        agent_name: str = "chat",
    ) -> None:
        """把一次 LLM 调用的 Token 用量持久化到成本数据库。"""
        tracker = await self._get_cost_tracker()
        if tracker is None:
            return
        try:
            await tracker.record_usage(
                task_id=session_id,
                agent_name=agent_name,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception as e:
            logger.warning(f"成本记录失败（忽略）: {e}")

    async def cost_summary(self, period_hours: int = 24) -> dict[str, Any]:
        """成本摘要，供 /api/costs/summary 使用。"""
        tracker = await self._get_cost_tracker()
        if tracker is None:
            return {"available": False}
        try:
            summary = await tracker.get_summary(period_hours)
            data = summary.model_dump(mode="json")
            data["available"] = True
            return data
        except Exception as e:
            logger.warning(f"读取成本摘要失败: {e}")
            return {"available": False, "error": str(e)}

    async def budget_status(self, project_id: str = "default") -> dict[str, Any]:
        """预算状态，供仪表盘和告警使用。"""
        manager = await self._get_budget_manager()
        if manager is None:
            return {"available": False}
        try:
            status = await manager.check_budget(project_id)
            data = status.model_dump(mode="json")
            data["available"] = True
            return data
        except Exception as e:
            logger.warning(f"读取预算状态失败: {e}")
            return {"available": False, "error": str(e)}

    async def set_budget(self, project_id: str, monthly_limit_tokens: int) -> dict[str, Any]:
        """设置项目月度 Token 预算。"""
        manager = await self._get_budget_manager()
        if manager is None:
            return {"available": False}
        await manager.set_budget(project_id, monthly_limit_tokens)
        return await self.budget_status(project_id)


_pipeline: Optional[ChatPipeline] = None


def get_chat_pipeline() -> ChatPipeline:
    """进程级单例。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = ChatPipeline()
    return _pipeline


def reset_chat_pipeline() -> None:
    """测试用：重置单例。"""
    global _pipeline
    _pipeline = None


async def shutdown_chat_pipeline() -> None:
    """关闭聊天管线持有的进程级资源并重置单例。"""
    global _pipeline
    from symbio.core.cost_monitor import shutdown_cost_monitor

    await shutdown_cost_monitor()
    if _pipeline is not None:
        _pipeline._cost_tracker = None
        _pipeline._budget_manager = None
    _pipeline = None
