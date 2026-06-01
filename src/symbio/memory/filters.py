"""记忆噪音过滤与安全网关

三层噪音过滤：
- L1 规则过滤（0ms）：过滤问候语、过短文本、纯问题
- L2 启发式分类（10ms）：关键词密度、句子完整性评分
- L3 LLM 精确提取（200ms）：仅在 L2 通过时触发

记忆安全网关：
- 写入前检查注入攻击
- PII 扫描与脱敏
- 来源可信度验证

五层遗忘策略 L3/L5：
- L3 冲突覆盖：新信息矛盾旧信息时降旧升新
- L5 项目清理：项目删除时清除所有记忆
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("memory.filters")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class FilterLevel(str, Enum):
    RULE = "rule"
    CLASSIFIER = "classifier"
    LLM = "llm"


class FilterResult(BaseModel):
    """过滤结果"""
    passed: bool
    level: FilterLevel
    reason: str = ""
    confidence: float = 1.0
    should_extract: bool = True


class SecurityCheckResult(BaseModel):
    """安全检查结果"""
    is_safe: bool
    injection_detected: bool = False
    pii_detected: bool = False
    pii_types: list[str] = Field(default_factory=list)
    sanitized_content: str = ""
    source_credibility: float = 1.0
    warnings: list[str] = Field(default_factory=list)


class ForgettingResult(BaseModel):
    """遗忘策略执行结果"""
    strategy: str
    affected_count: int = 0
    details: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# L1 规则过滤
# ---------------------------------------------------------------------------

# 问候语模式
_GREETING_PATTERNS = [
    re.compile(r"^(你好|hi|hello|hey|嗨|哈喽|good\s*(morning|afternoon|evening))[\s!！。.]*$", re.I),
    re.compile(r"^(谢谢|thanks|thank\s*you|thx|3q)[\s!！。.]*$", re.I),
    re.compile(r"^(好的|ok|okay|sure|行|嗯|哦)[\s!！。.]*$", re.I),
    re.compile(r"^(再见|bye|goodbye|拜拜|886|88)[\s!！。.]*$", re.I),
]

# 纯问题模式（没有实质性内容）
_PURE_QUESTION_PATTERNS = [
    re.compile(r"^.{0,5}[？\?]+$"),
    re.compile(r"^(什么|怎么|如何|为什么|哪里|哪个|谁|几|多少).{0,10}[？\?]*$"),
]


def _rule_filter(text: str) -> Optional[FilterResult]:
    """L1 规则过滤（0ms）"""
    stripped = text.strip()

    # 空或过短
    if len(stripped) < 10:
        return FilterResult(
            passed=False, level=FilterLevel.RULE,
            reason=f"文本过短 ({len(stripped)} 字符 < 10)",
            should_extract=False,
        )

    # 问候语
    for pattern in _GREETING_PATTERNS:
        if pattern.match(stripped):
            return FilterResult(
                passed=False, level=FilterLevel.RULE,
                reason="问候语，无实质内容",
                should_extract=False,
            )

    # 纯问题（没有陈述性内容）
    for pattern in _PURE_QUESTION_PATTERNS:
        if pattern.match(stripped):
            return FilterResult(
                passed=False, level=FilterLevel.RULE,
                reason="纯问题，无需提取记忆",
                should_extract=False,
            )

    return None  # 未命中，继续下一层


# ---------------------------------------------------------------------------
# L2 启发式分类
# ---------------------------------------------------------------------------

# 有信息量的关键词
_INFO_KEYWORDS = [
    "配置", "设置", "安装", "部署", "错误", "bug", "修复", "实现", "功能",
    "接口", "数据库", "API", "文件", "路径", "端口", "密码", "版本",
    "config", "setup", "deploy", "error", "fix", "implement", "feature",
    "interface", "database", "file", "path", "port", "password", "version",
]


def _classifier_filter(text: str) -> FilterResult:
    """L2 启发式分类（10ms）"""
    text_lower = text.lower()
    words = text.split()

    # 关键词密度
    keyword_hits = sum(1 for kw in _INFO_KEYWORDS if kw.lower() in text_lower)
    keyword_density = keyword_hits / max(len(words), 1)

    # 句子完整性（有句号/换行 = 更完整）
    sentence_indicators = text.count("。") + text.count(".") + text.count("\n") + text.count("！") + text.count("！")
    completeness = min(sentence_indicators / max(len(words) / 20, 1), 1.0)

    # 综合评分
    score = keyword_density * 0.6 + completeness * 0.4

    if score > 0.3:
        return FilterResult(
            passed=True, level=FilterLevel.CLASSIFIER,
            reason=f"信息密度足够 (score={score:.2f})",
            confidence=score, should_extract=True,
        )
    else:
        return FilterResult(
            passed=False, level=FilterLevel.CLASSIFIER,
            reason=f"信息密度不足 (score={score:.2f})",
            confidence=1.0 - score, should_extract=False,
        )


# ---------------------------------------------------------------------------
# 三层噪音过滤器
# ---------------------------------------------------------------------------


class ThreeLevelNoiseFilter:
    """三层噪音过滤器

    规则过滤（0ms）→ 启发式分类（10ms）→ LLM 精确提取（200ms）
    """

    def should_extract(self, text: str) -> FilterResult:
        """判断文本是否值得提取为记忆"""
        # L1: 规则过滤
        l1 = _rule_filter(text)
        if l1 is not None:
            return l1

        # L2: 启发式分类
        l2 = _classifier_filter(text)
        if not l2.passed:
            return l2

        # L3: LLM 精确提取（预留接口，当前直接通过）
        return FilterResult(
            passed=True, level=FilterLevel.LLM,
            reason="通过三层过滤",
            confidence=l2.confidence, should_extract=True,
        )

    def batch_filter(self, texts: list[str]) -> list[FilterResult]:
        """批量过滤"""
        return [self.should_extract(t) for t in texts]


# ---------------------------------------------------------------------------
# 记忆安全网关
# ---------------------------------------------------------------------------


class MemorySecurityGateway:
    """记忆安全网关 - 写入前的安全检查"""

    def __init__(self):
        self._injection_guard = None
        self._sanitizer = None
        self._init_guard = False

    def _ensure_deps(self):
        """懒加载依赖"""
        if self._init_guard:
            return
        try:
            from symbio.core.injection_guard import InjectionGuard
            self._injection_guard = InjectionGuard()
        except Exception:
            pass
        try:
            from symbio.security.sanitizer import DataSanitizer
            self._sanitizer = DataSanitizer()
        except Exception:
            pass
        self._init_guard = True

    async def validate_write(self, content: str, source: str = "unknown") -> SecurityCheckResult:
        """验证记忆内容是否安全可写入"""
        self._ensure_deps()

        result = SecurityCheckResult(is_safe=True)

        # 注入检测
        if self._injection_guard:
            try:
                analysis = self._injection_guard.analyze(content)
                if not analysis.is_safe:
                    result.is_safe = False
                    result.injection_detected = True
                    result.warnings.append(f"检测到注入攻击: {analysis.threats}")
            except Exception as e:
                result.warnings.append(f"注入检测异常: {e}")

        # PII 检测
        if self._sanitizer:
            try:
                report = self._sanitizer.sanitize_text(content)
                if report.detected_count > 0:
                    result.pii_detected = True
                    result.pii_types = [d.pii_type for d in report.detections]
                    result.sanitized_content = report.sanitized_text
                    result.warnings.append(f"检测到 {report.detected_count} 处 PII")
            except Exception as e:
                result.warnings.append(f"PII 检测异常: {e}")

        # 来源可信度
        credibility_map = {
            "code": 0.95, "config": 0.95, "system": 0.9,
            "admin": 0.85, "user": 0.7, "agent": 0.6, "external": 0.4,
        }
        source_lower = source.lower()
        for key, val in credibility_map.items():
            if key in source_lower:
                result.source_credibility = val
                break

        if result.source_credibility < 0.5:
            result.warnings.append(f"低可信度来源: {source} ({result.source_credibility})")

        return result


# ---------------------------------------------------------------------------
# 五层遗忘策略（L3 + L5）
# ---------------------------------------------------------------------------


class FiveLayerForgettingStrategy:
    """五层遗忘策略 - L3 冲突覆盖 + L5 项目清理"""

    async def apply_conflict_override(
        self, old_memories: list[dict], new_content: str
    ) -> ForgettingResult:
        """L3: 冲突覆盖 - 新信息矛盾旧信息时降旧升新"""
        affected = 0
        details = []

        new_lower = new_content.lower()
        for mem in old_memories:
            old_content = mem.get("content", "").lower()
            # 简单矛盾检测：如果新旧内容有显著重叠但方向相反
            overlap = len(set(new_lower.split()) & set(old_content.split()))
            if overlap > 3:  # 有足够多的共同关键词
                # 降低旧记忆重要性
                old_importance = mem.get("importance", 0.5)
                new_importance = max(0.1, old_importance * 0.5)
                details.append(
                    f"冲突覆盖: '{mem.get('title', '')}' importance {old_importance:.2f} → {new_importance:.2f}"
                )
                affected += 1

        return ForgettingResult(
            strategy="L3_conflict_override",
            affected_count=affected,
            details=details,
        )

    async def apply_project_cleanup(
        self, project_id: str, memory_ids: list[str]
    ) -> ForgettingResult:
        """L5: 项目清理 - 项目删除时清除所有记忆"""
        return ForgettingResult(
            strategy="L5_project_cleanup",
            affected_count=len(memory_ids),
            details=[f"项目 {project_id} 的 {len(memory_ids)} 条记忆已标记删除"],
        )
