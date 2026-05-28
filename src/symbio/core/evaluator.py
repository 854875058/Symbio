"""复杂度评估器 - 评估任务复杂度"""

from __future__ import annotations

import re

from symbio.utils.logger import get_logger
from symbio.utils.types import Intent, TaskComplexity

logger = get_logger("evaluator")

# 关键词权重
COMPLEXITY_KEYWORDS = {
    # 高复杂度关键词
    "high": [
        "架构设计", "系统设计", "重构", "优化", "分析", "审查",
        "architecture", "design", "refactor", "optimize", "analyze", "review",
        "复杂", "complex", "高级", "advanced", "深度", "deep",
        "安全", "security", "性能", "performance", "分布式", "distributed",
    ],
    # 中复杂度关键词
    "medium": [
        "实现", "implement", "开发", "develop", "创建", "create",
        "修改", "modify", "更新", "update", "修复", "fix",
        "测试", "test", "调试", "debug", "集成", "integrate",
    ],
    # 低复杂度关键词
    "low": [
        "查询", "query", "搜索", "search", "读取", "read",
        "列出", "list", "显示", "show", "获取", "get",
        "简单", "simple", "基础", "basic", "快速", "quick",
    ],
}


class ComplexityEvaluator:
    """任务复杂度评估器

    从多个维度评估任务复杂度：
    1. 文本长度
    2. 关键词匹配
    3. 工具依赖
    4. 上下文依赖
    """

    def __init__(self):
        self._weights = {
            "length": 0.2,
            "keywords": 0.4,
            "tools": 0.2,
            "context": 0.2,
        }

    async def evaluate(self, intent: Intent) -> TaskComplexity:
        """评估任务复杂度

        Args:
            intent: 用户意图

        Returns:
            复杂度等级
        """
        scores = {}

        # 1. 文本长度评分
        scores["length"] = self._score_length(intent.raw_text)

        # 2. 关键词评分
        scores["keywords"] = self._score_keywords(intent.raw_text)

        # 3. 工具依赖评分
        scores["tools"] = self._score_tools(intent.requires_tools)

        # 4. 上下文依赖评分
        scores["context"] = self._score_context(intent.requires_memory)

        # 加权平均
        total_score = sum(
            scores[dim] * self._weights[dim]
            for dim in scores
        )

        # 映射到复杂度等级
        complexity = self._score_to_complexity(total_score)

        logger.debug(
            f"复杂度评估: scores={scores}, total={total_score:.2f}, "
            f"result={complexity.value}"
        )

        return complexity

    def _score_length(self, text: str) -> float:
        """基于文本长度评分"""
        length = len(text)
        if length < 50:
            return 0.2
        elif length < 200:
            return 0.4
        elif length < 500:
            return 0.6
        elif length < 1000:
            return 0.8
        else:
            return 1.0

    def _score_keywords(self, text: str) -> float:
        """基于关键词评分"""
        text_lower = text.lower()

        # 检查高复杂度关键词
        for keyword in COMPLEXITY_KEYWORDS["high"]:
            if keyword.lower() in text_lower:
                return 0.8

        # 检查中复杂度关键词
        for keyword in COMPLEXITY_KEYWORDS["medium"]:
            if keyword.lower() in text_lower:
                return 0.5

        # 检查低复杂度关键词
        for keyword in COMPLEXITY_KEYWORDS["low"]:
            if keyword.lower() in text_lower:
                return 0.2

        # 默认中等复杂度
        return 0.5

    def _score_tools(self, tools: list[str]) -> float:
        """基于工具依赖评分"""
        if not tools:
            return 0.2

        # 工具数量越多，复杂度越高
        tool_count = len(tools)
        if tool_count <= 1:
            return 0.3
        elif tool_count <= 3:
            return 0.5
        elif tool_count <= 5:
            return 0.7
        else:
            return 0.9

    def _score_context(self, requires_memory: bool) -> float:
        """基于上下文依赖评分"""
        return 0.6 if requires_memory else 0.3

    def _score_to_complexity(self, score: float) -> TaskComplexity:
        """将分数映射到复杂度等级"""
        if score < 0.35:
            return TaskComplexity.LOW
        elif score < 0.65:
            return TaskComplexity.MEDIUM
        else:
            return TaskComplexity.HIGH

    def evaluate_simple(self, text: str) -> TaskComplexity:
        """简化的复杂度评估（不需要 Intent 对象）

        Args:
            text: 用户输入文本

        Returns:
            复杂度等级
        """
        intent = Intent(raw_text=text)
        # 同步版本，简化处理
        score = self._score_keywords(text)
        return self._score_to_complexity(score)
