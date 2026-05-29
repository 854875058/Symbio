"""Prompt Cache 对齐器 - 前缀确定性对齐、缓存命中率估算"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("cache_aligner")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class CacheProvider(str, Enum):
    """缓存提供商"""
    ANTHROPIC = "anthropic"      # Anthropic Prompt Cache
    OPENAI = "openai"            # OpenAI Prefix Caching
    CUSTOM = "custom"            # 自定义缓存系统


class AlignmentStrategy(str, Enum):
    """对齐策略"""
    EXACT = "exact"              # 精确前缀匹配
    PREFIX = "prefix"            # 前缀匹配（允许后缀不同）
    HASH = "hash"                # 哈希指纹匹配
    SEMANTIC = "semantic"        # 语义相似度匹配


class PrefixSegment(BaseModel):
    """前缀段 - 可缓存的内容片段"""
    segment_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    content_hash: str = ""
    token_count: int = 0
    segment_type: str = "text"   # system / tool / context / text
    is_static: bool = True       # 是否为静态内容（不会变化）
    cache_priority: int = 0      # 缓存优先级（越高越优先）
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """初始化后计算 hash"""
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                self.content.encode("utf-8")
            ).hexdigest()[:32]


class CacheableRequest(BaseModel):
    """可缓存的请求"""
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    model: str
    system_prompt: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    prefix_segments: list[PrefixSegment] = Field(default_factory=list)
    total_tokens: int = 0
    cache_control_hints: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class AlignmentResult(BaseModel):
    """对齐结果"""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str
    aligned_prefix_hash: str = ""
    aligned_segments: list[str] = Field(default_factory=list)
    cache_hit_probability: float = 0.0
    estimated_token_savings: int = 0
    alignment_strategy: AlignmentStrategy = AlignmentStrategy.PREFIX
    provider: CacheProvider = CacheProvider.ANTHROPIC
    created_at: datetime = Field(default_factory=datetime.now)


class HitRateEstimate(BaseModel):
    """命中率估算"""
    estimate_id: str = Field(default_factory=lambda: str(uuid4()))
    prefix_hash: str
    total_requests: int = 0
    estimated_hits: int = 0
    hit_rate: float = 0.0
    avg_prefix_tokens: int = 0
    total_token_savings: int = 0
    cost_savings_usd: float = 0.0
    confidence: float = 0.8
    created_at: datetime = Field(default_factory=datetime.now)


class PrefixGroup(BaseModel):
    """前缀分组 - 共享同一前缀的请求组"""
    group_id: str = Field(default_factory=lambda: str(uuid4()))
    prefix_hash: str
    prefix_content: str = ""
    prefix_tokens: int = 0
    request_count: int = 0
    request_ids: list[str] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.now)
    last_seen: datetime = Field(default_factory=datetime.now)


class CacheAlignerConfig(BaseModel):
    """对齐器配置"""
    provider: CacheProvider = CacheProvider.ANTHROPIC
    strategy: AlignmentStrategy = AlignmentStrategy.PREFIX
    min_prefix_tokens: int = 1024          # 最小可缓存前缀 Token 数
    max_prefix_tokens: int = 200000        # 最大前缀 Token 数
    enable_static_segmentation: bool = True  # 启用静态段分割
    enable_hit_rate_tracking: bool = True    # 启用命中率追踪
    hit_rate_window_size: int = 1000         # 命中率计算窗口大小
    cost_per_input_token: float = 0.000003   # 每个输入 Token 的成本（美元）


# ---------------------------------------------------------------------------
# Prompt Cache 对齐器
# ---------------------------------------------------------------------------

class CacheAligner:
    """Prompt Cache 对齐器

    核心能力：
    1. 前缀确定性对齐 - 将请求拆分为可缓存的前缀段，最大化缓存复用
    2. 缓存命中率估算 - 基于历史请求模式预测缓存命中概率
    3. 多提供商支持 - 适配 Anthropic、OpenAI 等不同缓存机制

    Usage:
        aligner = CacheAligner()
        segments = aligner.segment_request(request)
        result = aligner.align(request)
        estimate = aligner.estimate_hit_rate(prefix_hash)
    """

    def __init__(self, config: Optional[CacheAlignerConfig] = None):
        self._config = config or CacheAlignerConfig()

        # 前缀分组索引：prefix_hash -> PrefixGroup
        self._prefix_groups: dict[str, PrefixGroup] = {}

        # 请求历史（滑动窗口）
        self._request_history: list[CacheableRequest] = []
        self._hit_history: list[bool] = []

        # 静态内容指纹缓存
        self._static_fingerprints: dict[str, str] = {}  # content -> hash

        logger.info(
            f"CacheAligner 创建: provider={self._config.provider.value}, "
            f"strategy={self._config.strategy.value}, "
            f"min_prefix_tokens={self._config.min_prefix_tokens}"
        )

    # ------------------------------------------------------------------
    # 请求分段
    # ------------------------------------------------------------------

    def segment_request(self, request: CacheableRequest) -> list[PrefixSegment]:
        """将请求拆分为可缓存的前缀段

        分段策略（按缓存稳定性从高到低）：
        1. 系统提示词（最稳定）
        2. 工具定义（较稳定）
        3. 静态上下文（稳定）
        4. 历史消息（较不稳定）
        5. 用户输入（最不稳定）

        Args:
            request: 待分段的请求

        Returns:
            前缀段列表（按缓存优先级排序）
        """
        segments: list[PrefixSegment] = []

        # 1. 系统提示词段
        if request.system_prompt:
            system_tokens = self._estimate_tokens(request.system_prompt)
            segments.append(PrefixSegment(
                content=request.system_prompt,
                token_count=system_tokens,
                segment_type="system",
                is_static=True,
                cache_priority=100,
            ))

        # 2. 工具定义段
        if request.tools:
            tools_json = json.dumps(request.tools, ensure_ascii=False, sort_keys=True)
            tools_tokens = self._estimate_tokens(tools_json)
            segments.append(PrefixSegment(
                content=tools_json,
                token_count=tools_tokens,
                segment_type="tool",
                is_static=True,
                cache_priority=90,
            ))

        # 3. 历史消息段（分组）
        if request.messages:
            # 将连续的历史消息合并为段
            current_segment_content = ""
            current_segment_tokens = 0

            for i, msg in enumerate(request.messages):
                msg_json = json.dumps(msg, ensure_ascii=False)
                msg_tokens = self._estimate_tokens(msg_json)

                # 如果是最后一条消息（用户输入），单独成段
                if i == len(request.messages) - 1:
                    # 先保存之前的累积段
                    if current_segment_content:
                        segments.append(PrefixSegment(
                            content=current_segment_content,
                            token_count=current_segment_tokens,
                            segment_type="context",
                            is_static=False,
                            cache_priority=50,
                        ))

                    # 用户输入段（不缓存）
                    segments.append(PrefixSegment(
                        content=msg_json,
                        token_count=msg_tokens,
                        segment_type="text",
                        is_static=False,
                        cache_priority=0,
                    ))
                else:
                    # 累积历史消息
                    if current_segment_content:
                        current_segment_content += "\n" + msg_json
                    else:
                        current_segment_content = msg_json
                    current_segment_tokens += msg_tokens

                    # 如果累积到一定大小，生成一个段
                    if current_segment_tokens >= self._config.min_prefix_tokens:
                        segments.append(PrefixSegment(
                            content=current_segment_content,
                            token_count=current_segment_tokens,
                            segment_type="context",
                            is_static=False,
                            cache_priority=50,
                        ))
                        current_segment_content = ""
                        current_segment_tokens = 0

            # 保存剩余的累积段
            if current_segment_content:
                segments.append(PrefixSegment(
                    content=current_segment_content,
                    token_count=current_segment_tokens,
                    segment_type="context",
                    is_static=False,
                    cache_priority=50,
                ))

        # 按缓存优先级降序排列
        segments.sort(key=lambda s: s.cache_priority, reverse=True)

        logger.debug(
            f"请求分段完成: {len(segments)} 个段, "
            f"total_tokens={request.total_tokens}"
        )
        return segments

    # ------------------------------------------------------------------
    # 前缀对齐
    # ------------------------------------------------------------------

    def align(self, request: CacheableRequest) -> AlignmentResult:
        """对齐请求前缀以最大化缓存命中

        Args:
            request: 待对齐的请求

        Returns:
            对齐结果
        """
        # 分段
        segments = self.segment_request(request)
        request.prefix_segments = segments

        # 构建可缓存前缀
        cacheable_segments: list[PrefixSegment] = []
        prefix_tokens = 0

        for segment in segments:
            # 只缓存静态或高优先级段
            if segment.cache_priority >= 50:
                if prefix_tokens + segment.token_count <= self._config.max_prefix_tokens:
                    cacheable_segments.append(segment)
                    prefix_tokens += segment.token_count

        # 检查是否达到最小前缀要求
        if prefix_tokens < self._config.min_prefix_tokens:
            logger.debug(
                f"前缀 Token 不足: {prefix_tokens} < {self._config.min_prefix_tokens}"
            )
            return AlignmentResult(
                request_id=request.request_id,
                aligned_prefix_hash="",
                cache_hit_probability=0.0,
                estimated_token_savings=0,
                alignment_strategy=self._config.strategy,
                provider=self._config.provider,
            )

        # 计算前缀 hash
        prefix_content = "".join(s.content for s in cacheable_segments)
        prefix_hash = hashlib.sha256(
            prefix_content.encode("utf-8")
        ).hexdigest()[:32]

        # 计算命中概率
        hit_probability = self._calculate_hit_probability(prefix_hash, prefix_tokens)

        # 计算 Token 节省
        token_savings = int(prefix_tokens * hit_probability)

        # 记录到前缀分组
        self._record_prefix_group(prefix_hash, prefix_content, prefix_tokens, request.request_id)

        # 生成缓存控制提示
        cache_hints = self._generate_cache_control_hints(
            cacheable_segments, self._config.provider
        )
        request.cache_control_hints = cache_hints

        result = AlignmentResult(
            request_id=request.request_id,
            aligned_prefix_hash=prefix_hash,
            aligned_segments=[s.segment_id for s in cacheable_segments],
            cache_hit_probability=hit_probability,
            estimated_token_savings=token_savings,
            alignment_strategy=self._config.strategy,
            provider=self._config.provider,
        )

        logger.info(
            f"前缀对齐完成: hash={prefix_hash[:16]}, "
            f"prefix_tokens={prefix_tokens}, "
            f"hit_probability={hit_probability:.2%}, "
            f"token_savings={token_savings}"
        )
        return result

    # ------------------------------------------------------------------
    # 命中率估算
    # ------------------------------------------------------------------

    def estimate_hit_rate(self, prefix_hash: str) -> HitRateEstimate:
        """估算指定前缀的缓存命中率

        基于历史请求模式和前缀分组统计。

        Args:
            prefix_hash: 前缀 hash

        Returns:
            命中率估算
        """
        group = self._prefix_groups.get(prefix_hash)

        if not group:
            return HitRateEstimate(
                prefix_hash=prefix_hash,
                total_requests=0,
                estimated_hits=0,
                hit_rate=0.0,
                confidence=0.0,
            )

        # 基于请求数量估算命中率
        # 第一个请求不可能命中，后续请求命中概率递增
        total_requests = group.request_count
        if total_requests <= 1:
            hit_rate = 0.0
            estimated_hits = 0
        else:
            # 假设第一个请求 miss，后续请求都 hit
            estimated_hits = total_requests - 1
            hit_rate = estimated_hits / total_requests

        # 计算 Token 节省
        total_token_savings = group.prefix_tokens * estimated_hits
        cost_savings = total_token_savings * self._config.cost_per_input_token

        # 置信度：基于样本量
        confidence = min(total_requests / 100.0, 1.0)

        estimate = HitRateEstimate(
            prefix_hash=prefix_hash,
            total_requests=total_requests,
            estimated_hits=estimated_hits,
            hit_rate=hit_rate,
            avg_prefix_tokens=group.prefix_tokens,
            total_token_savings=total_token_savings,
            cost_savings_usd=cost_savings,
            confidence=confidence,
        )

        logger.debug(
            f"命中率估算: hash={prefix_hash[:16]}, "
            f"hit_rate={hit_rate:.2%}, "
            f"cost_savings=${cost_savings:.4f}"
        )
        return estimate

    def get_aggregate_hit_rate(self) -> HitRateEstimate:
        """获取全局聚合命中率估算

        Returns:
            聚合命中率估算
        """
        total_requests = 0
        total_hits = 0
        total_token_savings = 0
        total_prefix_tokens = 0

        for group in self._prefix_groups.values():
            total_requests += group.request_count
            # 每个前缀组中，第一个请求 miss，后续请求 hit
            if group.request_count > 1:
                group_hits = group.request_count - 1
                total_hits += group_hits
                # 每次 hit 节省的 token = 该前缀的 token 数
                total_token_savings += group.prefix_tokens * group_hits
            total_prefix_tokens += group.prefix_tokens * group.request_count

        hit_rate = total_hits / total_requests if total_requests > 0 else 0.0
        avg_prefix_tokens = total_prefix_tokens // max(total_requests, 1)
        cost_savings = total_token_savings * self._config.cost_per_input_token

        return HitRateEstimate(
            prefix_hash="aggregate",
            total_requests=total_requests,
            estimated_hits=total_hits,
            hit_rate=hit_rate,
            avg_prefix_tokens=avg_prefix_tokens,
            total_token_savings=total_token_savings,
            cost_savings_usd=cost_savings,
            confidence=min(total_requests / 100.0, 1.0),
        )

    # ------------------------------------------------------------------
    # 前缀分组管理
    # ------------------------------------------------------------------

    def get_prefix_groups(self) -> list[PrefixGroup]:
        """获取所有前缀分组

        Returns:
            前缀分组列表
        """
        return list(self._prefix_groups.values())

    def get_top_prefix_groups(self, n: int = 10) -> list[PrefixGroup]:
        """获取请求量最大的前 N 个前缀分组

        Args:
            n: 返回数量

        Returns:
            前缀分组列表（按请求量降序）
        """
        groups = sorted(
            self._prefix_groups.values(),
            key=lambda g: g.request_count,
            reverse=True,
        )
        return groups[:n]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _record_prefix_group(
        self,
        prefix_hash: str,
        prefix_content: str,
        prefix_tokens: int,
        request_id: str,
    ) -> None:
        """记录请求到前缀分组"""
        if prefix_hash in self._prefix_groups:
            group = self._prefix_groups[prefix_hash]
            group.request_count += 1
            group.request_ids.append(request_id)
            group.last_seen = datetime.now()
        else:
            self._prefix_groups[prefix_hash] = PrefixGroup(
                prefix_hash=prefix_hash,
                prefix_content=prefix_content[:500],  # 只保存摘要
                prefix_tokens=prefix_tokens,
                request_count=1,
                request_ids=[request_id],
            )

    def _calculate_hit_probability(self, prefix_hash: str, prefix_tokens: int) -> float:
        """计算缓存命中概率

        考虑因素：
        1. 历史请求中该前缀出现的频率
        2. 前缀长度（越长越容易被缓存）
        3. 缓存提供商的特性
        """
        group = self._prefix_groups.get(prefix_hash)

        if not group:
            # 首次出现，命中概率为 0
            return 0.0

        # 基础概率：历史请求频率
        base_probability = min(group.request_count / 10.0, 0.9)

        # 前缀长度调整：长前缀更容易被缓存
        length_factor = min(prefix_tokens / self._config.min_prefix_tokens, 2.0)
        length_factor = min(length_factor, 1.5)  # 上限 1.5 倍

        # 提供商调整
        provider_factor = {
            CacheProvider.ANTHROPIC: 1.0,
            CacheProvider.OPENAI: 0.9,
            CacheProvider.CUSTOM: 0.8,
        }.get(self._config.provider, 0.8)

        probability = base_probability * length_factor * provider_factor
        return min(probability, 0.99)

    def _generate_cache_control_hints(
        self,
        segments: list[PrefixSegment],
        provider: CacheProvider,
    ) -> dict[str, str]:
        """生成缓存控制提示

        Args:
            segments: 前缀段列表
            provider: 缓存提供商

        Returns:
            缓存控制提示字典
        """
        hints: dict[str, str] = {}

        if provider == CacheProvider.ANTHROPIC:
            # Anthropic 的 cache_control 格式
            for i, segment in enumerate(segments):
                if segment.cache_priority >= 50:
                    hints[f"segment_{i}_cache_control"] = "ephemeral"
        elif provider == CacheProvider.OPENAI:
            # OpenAI 的 prefix caching（自动，无需特殊提示）
            hints["prefix_caching"] = "enabled"

        return hints

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的 Token 数

        简单估算：英文约 4 字符/token，中文约 2 字符/token
        """
        # 统计中文字符数
        chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
        other_chars = len(text) - chinese_chars

        # 估算
        tokens = (chinese_chars // 2) + (other_chars // 4)
        return max(tokens, 1)

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> CacheAligner:
        """使用默认配置创建"""
        return cls(CacheAlignerConfig())

    @classmethod
    def create_for_anthropic(cls) -> CacheAligner:
        """创建适配 Anthropic 的对齐器"""
        return cls(CacheAlignerConfig(
            provider=CacheProvider.ANTHROPIC,
            min_prefix_tokens=1024,
        ))

    @classmethod
    def create_for_openai(cls) -> CacheAligner:
        """创建适配 OpenAI 的对齐器"""
        return cls(CacheAlignerConfig(
            provider=CacheProvider.OPENAI,
            min_prefix_tokens=512,
        ))
