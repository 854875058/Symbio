"""上下文剪枝器 - 语义级上下文压缩、决策关键点提取、工具输出裁剪"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("context_pruner")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class PruneStrategy(str, Enum):
    """剪枝策略类型"""
    SEMANTIC = "semantic"          # 语义级压缩
    KEYPOINT = "keypoint"          # 决策关键点提取
    TOOL_OUTPUT = "tool_output"    # 工具输出裁剪
    DELTA = "delta"                # 状态增量提取
    FULL = "full"                  # 全策略组合


class ImportanceLevel(str, Enum):
    """内容重要性等级"""
    CRITICAL = "critical"    # 关键信息，不可裁剪
    HIGH = "high"            # 高重要性，尽量保留
    MEDIUM = "medium"        # 中等重要性，可压缩
    LOW = "low"              # 低重要性，可裁剪
    DISCARDABLE = "discardable"  # 可丢弃


class MessageRole(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContextMessage(BaseModel):
    """上下文消息"""
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    role: MessageRole
    content: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    token_count: int = 0
    importance: ImportanceLevel = ImportanceLevel.MEDIUM
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionKeyPoint(BaseModel):
    """决策关键点"""
    keypoint_id: str = Field(default_factory=lambda: str(uuid4()))
    source_message_id: str
    summary: str
    decision_type: str = ""          # plan / action / conclusion / error
    confidence: float = 0.8
    related_entities: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)


class StateDelta(BaseModel):
    """状态增量"""
    delta_id: str = Field(default_factory=lambda: str(uuid4()))
    source_message_id: str
    variable_name: str
    old_value: Any = None
    new_value: Any = None
    change_type: str = "update"      # create / update / delete
    timestamp: datetime = Field(default_factory=datetime.now)


class PruneResult(BaseModel):
    """剪枝结果"""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    original_token_count: int = 0
    pruned_token_count: int = 0
    compression_ratio: float = 0.0
    removed_messages: list[str] = Field(default_factory=list)
    compressed_messages: list[str] = Field(default_factory=list)
    keypoints: list[DecisionKeyPoint] = Field(default_factory=list)
    state_deltas: list[StateDelta] = Field(default_factory=list)
    strategy_used: PruneStrategy = PruneStrategy.FULL
    created_at: datetime = Field(default_factory=datetime.now)


class PrunerConfig(BaseModel):
    """剪枝器配置"""
    max_context_tokens: int = 8000       # 目标最大 Token 数
    min_important_messages: int = 5       # 至少保留的重要消息数
    tool_output_max_tokens: int = 500     # 工具输出最大保留 Token 数
    keypoint_confidence_threshold: float = 0.6  # 关键点置信度阈值
    preserve_system_message: bool = True  # 始终保留系统消息
    preserve_recent_n: int = 3            # 保留最近 N 条消息
    enable_delta_tracking: bool = True    # 启用状态增量追踪


# ---------------------------------------------------------------------------
# 上下文剪枝器
# ---------------------------------------------------------------------------

class ContextPruner:
    """上下文剪枝器

    核心能力：
    1. 语义级上下文压缩 - 识别冗余信息，保留语义核心
    2. 决策关键点提取 - 从对话历史中提取决策依据
    3. 工具输出裁剪 - 截断冗长的工具返回结果
    4. 状态增量提取 - 追踪状态变更，替代全量快照

    Usage:
        pruner = ContextPruner()
        result = pruner.prune(messages, strategy=PruneStrategy.FULL)
    """

    # 工具输出中可裁剪的模式
    TRUNCATABLE_PATTERNS = [
        r"(?:file|directory) listing.*?(?:\n\s*\S+){10,}",
        r"(?:log|trace) output.*?(?:\n.*?){20,}",
        r"stack trace.*?(?:\n\s+at\s+.*?){5,}",
        r"(?:JSON|XML) response.*?\{[\s\S]{500,}\}",
        r"(?:diff|patch) output.*?(?:\n[+-].*?){10,}",
    ]

    # 决策关键词
    DECISION_KEYWORDS = [
        "决定", "选择", "确认", "判断", "结论", "方案", "计划",
        "decide", "choose", "confirm", "determine", "conclude", "plan",
        "因此", "所以", "基于以上", "综上", "总结",
        "therefore", "thus", "based on", "in summary",
    ]

    # 状态变更模式
    STATE_CHANGE_PATTERNS = [
        r"(?:设置|设置为|设为|更新为|改为|变更为)\s*[：:]\s*(.+)",
        r"(?:set|update|change|modify)\s+(?:to|into)\s+(.+)",
        r"=\s*(.+?)(?:\s*$|\s*[;,\n])",
    ]

    def __init__(self, config: Optional[PrunerConfig] = None):
        self._config = config or PrunerConfig()
        logger.info(
            f"ContextPruner 创建: max_tokens={self._config.max_context_tokens}, "
            f"preserve_recent={self._config.preserve_recent_n}"
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def prune(
        self,
        messages: list[ContextMessage],
        strategy: PruneStrategy = PruneStrategy.FULL,
        target_token_count: Optional[int] = None,
    ) -> PruneResult:
        """执行上下文剪枝

        Args:
            messages: 原始上下文消息列表
            strategy: 剪枝策略
            target_token_count: 目标 Token 数，None 使用配置默认值

        Returns:
            剪枝结果
        """
        if not messages:
            return PruneResult(strategy_used=strategy)

        target = target_token_count or self._config.max_context_tokens
        original_tokens = sum(m.token_count for m in messages)

        logger.info(
            f"开始剪枝: messages={len(messages)}, "
            f"tokens={original_tokens}, target={target}, strategy={strategy.value}"
        )

        # 如果已经在目标范围内，直接返回
        if original_tokens <= target:
            logger.info(f"上下文已在目标范围内，无需剪枝")
            return PruneResult(
                original_token_count=original_tokens,
                pruned_token_count=original_tokens,
                compression_ratio=1.0,
                strategy_used=strategy,
            )

        # 根据策略执行剪枝
        if strategy == PruneStrategy.SEMANTIC:
            pruned, removed_ids, compressed_ids = self._prune_semantic(messages, target)
        elif strategy == PruneStrategy.KEYPOINT:
            pruned, removed_ids, compressed_ids = self._prune_keypoint(messages, target)
        elif strategy == PruneStrategy.TOOL_OUTPUT:
            pruned, removed_ids, compressed_ids = self._prune_tool_output(messages, target)
        elif strategy == PruneStrategy.DELTA:
            pruned, removed_ids, compressed_ids = self._prune_delta(messages, target)
        else:  # FULL
            pruned, removed_ids, compressed_ids = self._prune_full(messages, target)

        pruned_tokens = sum(m.token_count for m in pruned)
        ratio = pruned_tokens / original_tokens if original_tokens > 0 else 1.0

        # 提取关键点和状态增量
        keypoints = self.extract_keypoints(messages)
        state_deltas = self.extract_state_deltas(messages)

        result = PruneResult(
            original_token_count=original_tokens,
            pruned_token_count=pruned_tokens,
            compression_ratio=ratio,
            removed_messages=removed_ids,
            compressed_messages=compressed_ids,
            keypoints=keypoints,
            state_deltas=state_deltas,
            strategy_used=strategy,
        )

        logger.info(
            f"剪枝完成: {original_tokens} -> {pruned_tokens} tokens "
            f"(ratio={ratio:.2%}), removed={len(removed_ids)}, "
            f"compressed={len(compressed_ids)}, keypoints={len(keypoints)}"
        )
        return result

    # ------------------------------------------------------------------
    # 语义级压缩
    # ------------------------------------------------------------------

    def _prune_semantic(
        self,
        messages: list[ContextMessage],
        target: int,
    ) -> tuple[list[ContextMessage], list[str], list[str]]:
        """语义级上下文压缩

        策略：
        1. 按重要性排序
        2. 优先保留高重要性消息
        3. 合并连续的低重要性消息
        """
        removed_ids: list[str] = []
        compressed_ids: list[str] = []

        # 分离保护消息和可裁剪消息
        protected, flexible = self._separate_by_importance(messages)

        # 按重要性降序排列可裁剪消息
        importance_order = {
            ImportanceLevel.CRITICAL: 0,
            ImportanceLevel.HIGH: 1,
            ImportanceLevel.MEDIUM: 2,
            ImportanceLevel.LOW: 3,
            ImportanceLevel.DISCARDABLE: 4,
        }
        flexible.sort(key=lambda m: importance_order.get(m.importance, 2), reverse=False)

        # 计算保护消息的 Token 总量
        protected_tokens = sum(m.token_count for m in protected)
        remaining_budget = target - protected_tokens

        # 从高重要性开始保留，直到预算用尽
        kept_flexible: list[ContextMessage] = []
        for msg in flexible:
            if remaining_budget <= 0:
                removed_ids.append(msg.message_id)
                continue

            if msg.token_count <= remaining_budget:
                kept_flexible.append(msg)
                remaining_budget -= msg.token_count
            else:
                # 尝试压缩此消息
                compressed = self._compress_message(msg, remaining_budget)
                if compressed.token_count > 0:
                    kept_flexible.append(compressed)
                    compressed_ids.append(msg.message_id)
                    remaining_budget -= compressed.token_count
                else:
                    removed_ids.append(msg.message_id)

        # 合并并按时间排序
        result = protected + kept_flexible
        result.sort(key=lambda m: m.timestamp)

        return result, removed_ids, compressed_ids

    # ------------------------------------------------------------------
    # 决策关键点提取
    # ------------------------------------------------------------------

    def _prune_keypoint(
        self,
        messages: list[ContextMessage],
        target: int,
    ) -> tuple[list[ContextMessage], list[str], list[str]]:
        """基于决策关键点的剪枝

        策略：
        1. 提取所有决策关键点
        2. 保留包含关键点的消息
        3. 丢弃无关键信息的冗余消息
        """
        removed_ids: list[str] = []
        compressed_ids: list[str] = []

        # 提取关键点
        keypoints = self.extract_keypoints(messages)
        keypoint_source_ids = {kp.source_message_id for kp in keypoints
                               if kp.confidence >= self._config.keypoint_confidence_threshold}

        # 分离保护消息和可裁剪消息
        protected, flexible = self._separate_by_importance(messages)

        # 在可裁剪消息中，保留包含关键点的
        keypoint_messages = [m for m in flexible if m.message_id in keypoint_source_ids]
        non_keypoint_messages = [m for m in flexible if m.message_id not in keypoint_source_ids]

        # 计算预算
        protected_tokens = sum(m.token_count for m in protected)
        keypoint_tokens = sum(m.token_count for m in keypoint_messages)
        remaining_budget = target - protected_tokens - keypoint_tokens

        # 尝试保留部分非关键点消息
        kept_non_keypoint: list[ContextMessage] = []
        if remaining_budget > 0:
            # 优先保留最近的消息
            non_keypoint_messages.sort(key=lambda m: m.timestamp, reverse=True)
            for msg in non_keypoint_messages:
                if msg.token_count <= remaining_budget:
                    kept_non_keypoint.append(msg)
                    remaining_budget -= msg.token_count
                else:
                    compressed = self._compress_message(msg, remaining_budget)
                    if compressed.token_count > 0:
                        kept_non_keypoint.append(compressed)
                        compressed_ids.append(msg.message_id)
                        remaining_budget -= compressed.token_count
                    else:
                        removed_ids.append(msg.message_id)
        else:
            removed_ids.extend(m.message_id for m in non_keypoint_messages)

        result = protected + keypoint_messages + kept_non_keypoint
        result.sort(key=lambda m: m.timestamp)

        return result, removed_ids, compressed_ids

    # ------------------------------------------------------------------
    # 工具输出裁剪
    # ------------------------------------------------------------------

    def _prune_tool_output(
        self,
        messages: list[ContextMessage],
        target: int,
    ) -> tuple[list[ContextMessage], list[str], list[str]]:
        """工具输出裁剪

        策略：
        1. 截断冗长的工具输出
        2. 保留工具调用的关键摘要
        3. 移除重复的工具调用结果
        """
        removed_ids: list[str] = []
        compressed_ids: list[str] = []
        result: list[ContextMessage] = []

        # 按工具名分组，追踪已见输出的 hash
        seen_tool_outputs: dict[str, set[str]] = {}

        for msg in messages:
            # 非工具消息直接保留
            if msg.role != MessageRole.TOOL:
                result.append(msg)
                continue

            tool_name = msg.tool_name or "unknown"
            content_hash = hashlib.md5(msg.content.encode()).hexdigest()[:16]

            # 检查是否为重复输出
            if tool_name not in seen_tool_outputs:
                seen_tool_outputs[tool_name] = set()

            if content_hash in seen_tool_outputs[tool_name]:
                # 重复输出，标记为已移除
                removed_ids.append(msg.message_id)
                logger.debug(f"移除重复工具输出: tool={tool_name}, hash={content_hash}")
                continue

            seen_tool_outputs[tool_name].add(content_hash)

            # 检查是否需要截断
            if msg.token_count > self._config.tool_output_max_tokens:
                compressed = self._compress_tool_output(msg)
                result.append(compressed)
                compressed_ids.append(msg.message_id)
            else:
                result.append(msg)

        # 检查是否达到目标
        current_tokens = sum(m.token_count for m in result)
        if current_tokens > target:
            # 进一步压缩：移除最老的工具输出
            tool_messages = [m for m in result if m.role == MessageRole.TOOL]
            tool_messages.sort(key=lambda m: m.timestamp)

            for msg in tool_messages:
                if current_tokens <= target:
                    break
                result.remove(msg)
                removed_ids.append(msg.message_id)
                current_tokens -= msg.token_count

        return result, removed_ids, compressed_ids

    # ------------------------------------------------------------------
    # 状态增量提取
    # ------------------------------------------------------------------

    def _prune_delta(
        self,
        messages: list[ContextMessage],
        target: int,
    ) -> tuple[list[ContextMessage], list[str], list[str]]:
        """基于状态增量的剪枝

        策略：
        1. 提取状态增量
        2. 用增量摘要替代完整历史
        3. 保留最新状态和关键变更
        """
        removed_ids: list[str] = []
        compressed_ids: list[str] = []

        # 提取状态增量
        deltas = self.extract_state_deltas(messages)
        delta_source_ids = {d.source_message_id for d in deltas}

        # 分离保护消息和可裁剪消息
        protected, flexible = self._separate_by_importance(messages)

        # 在可裁剪消息中，保留包含状态变更的
        delta_messages = [m for m in flexible if m.message_id in delta_source_ids]
        non_delta_messages = [m for m in flexible if m.message_id not in delta_source_ids]

        # 计算预算
        protected_tokens = sum(m.token_count for m in protected)
        delta_tokens = sum(m.token_count for m in delta_messages)
        remaining_budget = target - protected_tokens - delta_tokens

        # 尝试保留部分非增量消息（优先保留最近的）
        kept_non_delta: list[ContextMessage] = []
        if remaining_budget > 0:
            non_delta_messages.sort(key=lambda m: m.timestamp, reverse=True)
            for msg in non_delta_messages:
                if msg.token_count <= remaining_budget:
                    kept_non_delta.append(msg)
                    remaining_budget -= msg.token_count
                else:
                    removed_ids.append(msg.message_id)
        else:
            removed_ids.extend(m.message_id for m in non_delta_messages)

        result = protected + delta_messages + kept_non_delta
        result.sort(key=lambda m: m.timestamp)

        return result, removed_ids, compressed_ids

    # ------------------------------------------------------------------
    # 全策略组合
    # ------------------------------------------------------------------

    def _prune_full(
        self,
        messages: list[ContextMessage],
        target: int,
    ) -> tuple[list[ContextMessage], list[str], list[str]]:
        """全策略组合剪枝

        按优先级依次执行：
        1. 工具输出裁剪（最低风险）
        2. 语义级压缩
        3. 决策关键点提取
        4. 状态增量提取（最高压缩率）
        """
        all_removed_ids: list[str] = []
        all_compressed_ids: list[str] = []
        current_messages = messages

        # 阶段1：工具输出裁剪
        current_tokens = sum(m.token_count for m in current_messages)
        if current_tokens > target:
            current_messages, removed, compressed = self._prune_tool_output(
                current_messages, target
            )
            all_removed_ids.extend(removed)
            all_compressed_ids.extend(compressed)
            logger.debug(
                f"阶段1(工具输出裁剪): {current_tokens} -> "
                f"{sum(m.token_count for m in current_messages)}"
            )

        # 阶段2：语义级压缩
        current_tokens = sum(m.token_count for m in current_messages)
        if current_tokens > target:
            current_messages, removed, compressed = self._prune_semantic(
                current_messages, target
            )
            all_removed_ids.extend(removed)
            all_compressed_ids.extend(compressed)
            logger.debug(
                f"阶段2(语义压缩): {current_tokens} -> "
                f"{sum(m.token_count for m in current_messages)}"
            )

        # 阶段3：决策关键点提取
        current_tokens = sum(m.token_count for m in current_messages)
        if current_tokens > target:
            current_messages, removed, compressed = self._prune_keypoint(
                current_messages, target
            )
            all_removed_ids.extend(removed)
            all_compressed_ids.extend(compressed)
            logger.debug(
                f"阶段3(关键点提取): {current_tokens} -> "
                f"{sum(m.token_count for m in current_messages)}"
            )

        # 阶段4：状态增量提取
        current_tokens = sum(m.token_count for m in current_messages)
        if current_tokens > target:
            current_messages, removed, compressed = self._prune_delta(
                current_messages, target
            )
            all_removed_ids.extend(removed)
            all_compressed_ids.extend(compressed)
            logger.debug(
                f"阶段4(状态增量): {current_tokens} -> "
                f"{sum(m.token_count for m in current_messages)}"
            )

        return current_messages, all_removed_ids, all_compressed_ids

    # ------------------------------------------------------------------
    # 决策关键点提取
    # ------------------------------------------------------------------

    def extract_keypoints(self, messages: list[ContextMessage]) -> list[DecisionKeyPoint]:
        """从消息列表中提取决策关键点

        Args:
            messages: 消息列表

        Returns:
            决策关键点列表
        """
        keypoints: list[DecisionKeyPoint] = []

        for msg in messages:
            if msg.role not in (MessageRole.ASSISTANT, MessageRole.USER):
                continue

            content = msg.content.lower()

            # 检测决策关键词
            matched_keywords = [
                kw for kw in self.DECISION_KEYWORDS if kw in content
            ]

            if not matched_keywords:
                continue

            # 计算置信度：关键词匹配数量 / 总关键词数
            confidence = min(len(matched_keywords) / 3.0, 1.0)

            # 提取相关实体（简单实现：提取引号内的内容）
            entities = re.findall(r'["\'](.*?)["\']|`(.*?)`', msg.content)
            flat_entities = [e[0] or e[1] for e in entities]

            # 判断决策类型
            decision_type = self._classify_decision(content)

            kp = DecisionKeyPoint(
                source_message_id=msg.message_id,
                summary=self._extract_summary(msg.content),
                decision_type=decision_type,
                confidence=confidence,
                related_entities=flat_entities[:5],
            )
            keypoints.append(kp)

        logger.debug(f"提取决策关键点: {len(keypoints)} 个")
        return keypoints

    def _classify_decision(self, content: str) -> str:
        """分类决策类型"""
        if any(w in content for w in ["计划", "方案", "plan", "strategy"]):
            return "plan"
        if any(w in content for w in ["执行", "运行", "调用", "execute", "run", "call"]):
            return "action"
        if any(w in content for w in ["结论", "总结", "conclude", "summary"]):
            return "conclusion"
        if any(w in content for w in ["错误", "失败", "error", "fail"]):
            return "error"
        return "decision"

    def _extract_summary(self, content: str, max_length: int = 100) -> str:
        """提取内容摘要"""
        # 取第一句话或前 max_length 个字符
        sentences = re.split(r'[。！？\n.!?]', content)
        summary = sentences[0].strip() if sentences else content
        if len(summary) > max_length:
            summary = summary[:max_length] + "..."
        return summary

    # ------------------------------------------------------------------
    # 状态增量提取
    # ------------------------------------------------------------------

    def extract_state_deltas(self, messages: list[ContextMessage]) -> list[StateDelta]:
        """从消息列表中提取状态增量

        Args:
            messages: 消息列表

        Returns:
            状态增量列表
        """
        deltas: list[StateDelta] = []

        for msg in messages:
            if msg.role != MessageRole.ASSISTANT:
                continue

            # 检测状态变更模式
            for pattern in self.STATE_CHANGE_PATTERNS:
                matches = re.findall(pattern, msg.content, re.IGNORECASE)
                for match in matches:
                    delta = StateDelta(
                        source_message_id=msg.message_id,
                        variable_name=self._extract_variable_name(msg.content, match),
                        new_value=match.strip(),
                        change_type="update",
                    )
                    deltas.append(delta)

        logger.debug(f"提取状态增量: {len(deltas)} 个")
        return deltas

    def _extract_variable_name(self, content: str, value: str) -> str:
        """从内容中提取变量名"""
        # 尝试提取 "变量名 = 值" 模式
        pattern = rf"(\w+)\s*[=:]\s*{re.escape(value)}"
        match = re.search(pattern, content)
        if match:
            return match.group(1)
        return "unknown"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _separate_by_importance(
        self,
        messages: list[ContextMessage],
    ) -> tuple[list[ContextMessage], list[ContextMessage]]:
        """按重要性分离消息

        Returns:
            (保护消息列表, 可裁剪消息列表)
        """
        protected: list[ContextMessage] = []
        flexible: list[ContextMessage] = []

        for i, msg in enumerate(messages):
            # 系统消息始终保护
            if self._config.preserve_system_message and msg.role == MessageRole.SYSTEM:
                protected.append(msg)
                continue

            # 最近 N 条消息保护
            if i >= len(messages) - self._config.preserve_recent_n:
                protected.append(msg)
                continue

            # 关键重要性消息保护
            if msg.importance == ImportanceLevel.CRITICAL:
                protected.append(msg)
                continue

            flexible.append(msg)

        return protected, flexible

    def _compress_message(
        self,
        message: ContextMessage,
        target_tokens: int,
    ) -> ContextMessage:
        """压缩单条消息到目标 Token 数

        Args:
            message: 原始消息
            target_tokens: 目标 Token 数

        Returns:
            压缩后的消息
        """
        if target_tokens <= 0:
            return ContextMessage(
                role=message.role,
                content="",
                token_count=0,
                importance=message.importance,
            )

        # 简单截断（按字符比例估算）
        ratio = target_tokens / message.token_count if message.token_count > 0 else 1.0
        target_chars = int(len(message.content) * ratio)

        if target_chars >= len(message.content):
            return message

        # 截断并添加省略标记
        compressed_content = message.content[:target_chars] + "\n... [已压缩]"

        return ContextMessage(
            message_id=message.message_id,
            role=message.role,
            content=compressed_content,
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            token_count=target_tokens,
            importance=message.importance,
            timestamp=message.timestamp,
            metadata={**message.metadata, "compressed": True},
        )

    def _compress_tool_output(self, message: ContextMessage) -> ContextMessage:
        """压缩工具输出

        策略：
        1. 保留开头和结尾
        2. 移除中间的详细输出
        3. 添加摘要标记
        """
        content = message.content
        max_tokens = self._config.tool_output_max_tokens

        if message.token_count <= max_tokens:
            return message

        # 估算字符数（粗略：1 token ~= 4 字符）
        max_chars = max_tokens * 4

        if len(content) <= max_chars:
            return message

        # 保留前 60% 和后 20%
        head_chars = int(max_chars * 0.6)
        tail_chars = int(max_chars * 0.2)

        head = content[:head_chars]
        tail = content[-tail_chars:] if tail_chars > 0 else ""
        omitted = len(content) - head_chars - tail_chars

        compressed_content = f"{head}\n\n... [已省略 {omitted} 字符] ...\n\n{tail}"

        return ContextMessage(
            message_id=message.message_id,
            role=message.role,
            content=compressed_content,
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            token_count=max_tokens,
            importance=message.importance,
            timestamp=message.timestamp,
            metadata={**message.metadata, "tool_compressed": True},
        )

    def classify_importance(self, message: ContextMessage) -> ImportanceLevel:
        """自动分类消息重要性

        Args:
            message: 待分类的消息

        Returns:
            重要性等级
        """
        # 系统消息始终为关键
        if message.role == MessageRole.SYSTEM:
            return ImportanceLevel.CRITICAL

        content = message.content.lower()

        # 包含错误信息为高重要性
        if any(w in content for w in ["error", "错误", "失败", "exception", "traceback"]):
            return ImportanceLevel.HIGH

        # 包含决策关键词为高重要性
        if any(kw in content for kw in self.DECISION_KEYWORDS):
            return ImportanceLevel.HIGH

        # 工具输出为中等重要性
        if message.role == MessageRole.TOOL:
            if message.token_count > self._config.tool_output_max_tokens:
                return ImportanceLevel.LOW
            return ImportanceLevel.MEDIUM

        # 短消息为低重要性
        if message.token_count < 50:
            return ImportanceLevel.LOW

        return ImportanceLevel.MEDIUM

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> ContextPruner:
        """使用默认配置创建"""
        return cls(PrunerConfig())

    @classmethod
    def create_with_config(
        cls,
        *,
        max_context_tokens: int = 8000,
        preserve_recent_n: int = 3,
        tool_output_max_tokens: int = 500,
    ) -> ContextPruner:
        """使用自定义参数创建"""
        return cls(
            PrunerConfig(
                max_context_tokens=max_context_tokens,
                preserve_recent_n=preserve_recent_n,
                tool_output_max_tokens=tool_output_max_tokens,
            )
        )
