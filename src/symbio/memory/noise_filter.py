"""Noise Filter - 记忆噪声过滤器

提供规则过滤和分类器过滤两种策略，用于清除低质量记忆。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("memory.noise_filter")


class NoiseType(str, Enum):
    """噪声类型"""

    GREETING = "greeting"
    SHORT_TEXT = "short_text"
    QUESTION = "question"
    DUPLICATE = "duplicate"
    LOW_QUALITY = "low_quality"
    FILLER = "filler"


class FilterResult(BaseModel):
    """过滤结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    original_text: str = ""
    is_noise: bool = False
    noise_type: Optional[NoiseType] = None
    confidence: float = 0.0
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class FilterStats(BaseModel):
    """过滤统计"""

    total_processed: int = 0
    total_filtered: int = 0
    filter_rate: float = 0.0
    by_type: dict[str, int] = Field(default_factory=dict)


class BaseNoiseFilter(ABC):
    """噪声过滤器基类"""

    @abstractmethod
    def check(self, text: str) -> FilterResult:
        """检查文本是否为噪声"""
        ...


class RuleBasedNoiseFilter(BaseNoiseFilter):
    """基于规则的噪声过滤器"""

    GREETING_PATTERNS = [
        r"^(?:你好|hi|hello|hey|嗨|您好|good\s+(?:morning|afternoon|evening))[\s!！。.]*$",
        r"^(?:谢谢|thanks|thank\s+you|thx|感谢)[\s!！。.]*$",
        r"^(?:再见|bye|goodbye|拜拜|see\s+you)[\s!！。.]*$",
        r"^(?:ok|okay|好的|行|嗯|知道了|understood)[\s!！。.]*$",
    ]

    QUESTION_PATTERNS = [
        r"^(?:什么|怎么|如何|为什么|哪里|谁|哪个|多少|几|是否|能否|可以)",
        r"^(?:what|how|why|where|who|which|when|can|could|would|should|is|are|do|does)",
        r"[？\?]$",
    ]

    def __init__(
        self,
        min_text_length: int = 5,
        max_greeting_length: int = 30,
    ):
        self._min_text_length = min_text_length
        self._max_greeting_length = max_greeting_length
        self._greeting_patterns = [re.compile(p, re.IGNORECASE) for p in self.GREETING_PATTERNS]
        self._question_patterns = [re.compile(p, re.IGNORECASE) for p in self.QUESTION_PATTERNS]

    def check(self, text: str) -> FilterResult:
        text_stripped = text.strip()

        # 检查空文本
        if not text_stripped:
            return FilterResult(
                original_text=text,
                is_noise=True,
                noise_type=NoiseType.SHORT_TEXT,
                confidence=1.0,
                reason="Empty text",
            )

        # 检查短文本
        if len(text_stripped) < self._min_text_length:
            return FilterResult(
                original_text=text,
                is_noise=True,
                noise_type=NoiseType.SHORT_TEXT,
                confidence=0.9,
                reason=f"Text too short ({len(text_stripped)} < {self._min_text_length})",
            )

        # 检查问候语
        if len(text_stripped) <= self._max_greeting_length:
            for pattern in self._greeting_patterns:
                if pattern.match(text_stripped):
                    return FilterResult(
                        original_text=text,
                        is_noise=True,
                        noise_type=NoiseType.GREETING,
                        confidence=0.95,
                        reason="Greeting detected",
                    )

        # 检查纯问题（无上下文的问题不适合作为记忆）
        if len(text_stripped) <= 50:
            for pattern in self._question_patterns:
                if pattern.match(text_stripped) or pattern.search(text_stripped):
                    # 只有过短的纯问题才过滤
                    if len(text_stripped) < 20:
                        return FilterResult(
                            original_text=text,
                            is_noise=True,
                            noise_type=NoiseType.QUESTION,
                            confidence=0.7,
                            reason="Short question without context",
                        )

        return FilterResult(
            original_text=text,
            is_noise=False,
            confidence=0.0,
            reason="Passed all rule checks",
        )


class ClassifierNoiseFilter(BaseNoiseFilter):
    """基于特征分类的噪声过滤器

    使用多维度特征评分来判断文本质量。
    """

    def __init__(
        self,
        quality_threshold: float = 0.3,
    ):
        self._quality_threshold = quality_threshold

    def check(self, text: str) -> FilterResult:
        score = self._compute_quality_score(text)
        is_noise = score < self._quality_threshold

        noise_type = None
        reason = f"Quality score: {score:.2f}"
        if is_noise:
            if self._is_filler(text):
                noise_type = NoiseType.FILLER
                reason = "Filler content detected"
            else:
                noise_type = NoiseType.LOW_QUALITY
                reason = f"Low quality score ({score:.2f} < {self._quality_threshold})"

        return FilterResult(
            original_text=text,
            is_noise=is_noise,
            noise_type=noise_type,
            confidence=abs(score - self._quality_threshold),
            reason=reason,
        )

    def _compute_quality_score(self, text: str) -> float:
        """计算文本质量分数 (0.0 ~ 1.0)"""
        if not text.strip():
            return 0.0

        scores = []

        # 1. 长度分数
        length = len(text.strip())
        length_score = min(length / 100.0, 1.0)
        scores.append(length_score * 0.2)

        # 2. 词汇多样性
        words = text.split()
        if words:
            unique_ratio = len(set(words)) / len(words)
            scores.append(unique_ratio * 0.25)
        else:
            scores.append(0.0)

        # 3. 信息密度（非停用词比例）
        stop_words = {
            "的",
            "了",
            "是",
            "在",
            "我",
            "有",
            "和",
            "就",
            "不",
            "a",
            "the",
            "is",
            "are",
            "was",
            "were",
        }
        if words:
            content_words = [w for w in words if w.lower() not in stop_words]
            density = len(content_words) / len(words)
            scores.append(density * 0.25)
        else:
            scores.append(0.0)

        # 4. 结构分数（有标点、分段等）
        has_punctuation = bool(re.search(r"[。！？.!?,，;；:：]", text))
        has_newlines = "\n" in text
        structure_score = 0.0
        if has_punctuation:
            structure_score += 0.5
        if has_newlines:
            structure_score += 0.5
        scores.append(structure_score * 0.15)

        # 5. 字符多样性
        unique_chars = len(set(text))
        char_diversity = min(unique_chars / 20.0, 1.0)
        scores.append(char_diversity * 0.15)

        return min(sum(scores), 1.0)

    def _is_filler(self, text: str) -> bool:
        """检查是否为填充内容"""
        filler_patterns = [
            r"^(?:嗯+|啊+|哦+|呃+|哈+|\.+|…+|\?+|!+)$",
            r"^(?:um+|uh+|er+|ah+|hmm+|lol+|haha+)$",
            r"^(?:\.+|\?+|!+|\.\.\.+)$",
        ]
        text_lower = text.strip().lower()
        for pattern in filler_patterns:
            if re.match(pattern, text_lower):
                return True
        return False


class CombinedNoiseFilter:
    """组合噪声过滤器

    组合规则过滤和分类器过滤，提供综合判断。
    """

    def __init__(
        self,
        rule_filter: Optional[RuleBasedNoiseFilter] = None,
        classifier_filter: Optional[ClassifierNoiseFilter] = None,
    ):
        self._rule_filter = rule_filter or RuleBasedNoiseFilter()
        self._classifier_filter = classifier_filter or ClassifierNoiseFilter()
        self._stats = FilterStats()

    def check(self, text: str) -> FilterResult:
        """检查文本是否为噪声

        先用规则过滤，通过后再用分类器过滤。
        """
        self._stats.total_processed += 1

        # 规则过滤
        rule_result = self._rule_filter.check(text)
        if rule_result.is_noise:
            self._stats.total_filtered += 1
            noise_key = rule_result.noise_type.value if rule_result.noise_type else "unknown"
            self._stats.by_type[noise_key] = self._stats.by_type.get(noise_key, 0) + 1
            self._update_filter_rate()
            return rule_result

        # 分类器过滤
        classifier_result = self._classifier_filter.check(text)
        if classifier_result.is_noise:
            self._stats.total_filtered += 1
            noise_key = (
                classifier_result.noise_type.value if classifier_result.noise_type else "unknown"
            )
            self._stats.by_type[noise_key] = self._stats.by_type.get(noise_key, 0) + 1
            self._update_filter_rate()
            return classifier_result

        self._update_filter_rate()
        return FilterResult(
            original_text=text,
            is_noise=False,
            confidence=0.0,
            reason="Passed all filters",
        )

    def filter_batch(self, texts: list[str]) -> list[FilterResult]:
        """批量过滤"""
        return [self.check(text) for text in texts]

    def get_stats(self) -> FilterStats:
        """获取统计"""
        return self._stats.model_copy()

    def reset_stats(self) -> None:
        """重置统计"""
        self._stats = FilterStats()

    def _update_filter_rate(self) -> None:
        if self._stats.total_processed > 0:
            self._stats.filter_rate = self._stats.total_filtered / self._stats.total_processed
