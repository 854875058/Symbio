"""Evaluation Pipeline — 评测管道，支持多维度评估与回归检测。

核心能力：
1. 加载测试套件（JSON 格式）并执行评测任务
2. 工具调用准确率评估 — 对比 expected vs actual tool calls
3. 输出质量评估 — 基于多维度指标评分
4. 评测报告生成 — 结构化报告 + 人类可读摘要
5. 回归检测 — 对比历史基线，自动发现退化项
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("eval_pipeline")


# =============================================================================
# 1. 枚举与基础数据模型
# =============================================================================


class EvalTaskType(str, Enum):
    """评测任务类型。"""

    TOOL_CALL_ACCURACY = "tool_call_accuracy"
    OUTPUT_QUALITY = "output_quality"
    CUSTOM = "custom"


class EvalStatus(str, Enum):
    """评测状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class SeverityLevel(str, Enum):
    """问题严重程度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR_LEVEL = "error"
    CRITICAL = "critical"


# =============================================================================
# 2. 测试套件数据模型
# =============================================================================


class ExpectedToolCall(BaseModel):
    """预期工具调用。"""

    tool_name: str = Field(description="工具名称")
    parameters: dict[str, Any] = Field(default_factory=dict, description="预期参数（部分匹配）")
    required: bool = Field(default=True, description="是否必须调用")
    min_call_count: int = Field(default=1, description="最少调用次数")
    max_call_count: Optional[int] = Field(default=None, description="最多调用次数")


class ActualToolCall(BaseModel):
    """实际工具调用记录。"""

    tool_name: str = Field(description="工具名称")
    parameters: dict[str, Any] = Field(default_factory=dict, description="实际参数")
    output: str = Field(default="", description="调用输出")
    success: bool = Field(default=True, description="是否成功")
    duration_ms: float = Field(default=0.0, description="耗时（毫秒）")


class QualityCriterion(BaseModel):
    """输出质量评估标准。"""

    name: str = Field(description="标准名称")
    description: str = Field(default="", description="标准描述")
    weight: float = Field(default=1.0, ge=0.0, le=10.0, description="权重")
    min_score: float = Field(default=0.0, ge=0.0, le=1.0, description="最低通过分")
    max_score: float = Field(default=1.0, ge=0.0, le=1.0, description="最高分")


class EvalTestCase(BaseModel):
    """单条评测用例。"""

    case_id: str = Field(default_factory=lambda: str(uuid4()), description="用例 ID")
    name: str = Field(description="用例名称")
    description: str = Field(default="", description="用例描述")
    task_type: EvalTaskType = Field(default=EvalTaskType.TOOL_CALL_ACCURACY, description="任务类型")
    tags: list[str] = Field(default_factory=list, description="标签")

    # 输入
    input_text: str = Field(default="", description="输入文本")
    input_data: dict[str, Any] = Field(default_factory=dict, description="结构化输入数据")

    # 工具调用准确率相关
    expected_tool_calls: list[ExpectedToolCall] = Field(
        default_factory=list, description="预期工具调用列表"
    )

    # 输出质量相关
    expected_output: str = Field(default="", description="期望输出（用于对比）")
    quality_criteria: list[QualityCriterion] = Field(
        default_factory=list, description="质量评估标准"
    )

    # 通用
    timeout_seconds: int = Field(default=60, description="超时秒数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class EvalTestSuite(BaseModel):
    """评测测试套件。"""

    suite_id: str = Field(default_factory=lambda: str(uuid4()), description="套件 ID")
    name: str = Field(description="套件名称")
    description: str = Field(default="", description="套件描述")
    version: str = Field(default="1.0.0", description="套件版本")
    cases: list[EvalTestCase] = Field(default_factory=list, description="评测用例列表")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


# =============================================================================
# 3. 评测结果数据模型
# =============================================================================


class ToolCallAccuracyResult(BaseModel):
    """工具调用准确率评测结果。"""

    total_expected: int = Field(default=0, description="预期工具调用总数")
    total_actual: int = Field(default=0, description="实际工具调用总数")
    matched: int = Field(default=0, description="匹配的工具调用数")
    missing: int = Field(default=0, description="缺失的工具调用数")
    unexpected: int = Field(default=0, description="意外的工具调用数")
    accuracy: float = Field(default=0.0, ge=0.0, le=1.0, description="准确率")
    precision: float = Field(default=0.0, ge=0.0, le=1.0, description="精确率")
    recall: float = Field(default=0.0, ge=0.0, le=1.0, description="召回率")
    f1_score: float = Field(default=0.0, ge=0.0, le=1.0, description="F1 分数")
    missing_tools: list[str] = Field(default_factory=list, description="缺失的工具名称列表")
    unexpected_tools: list[str] = Field(default_factory=list, description="意外的工具名称列表")
    details: list[dict[str, Any]] = Field(default_factory=list, description="逐条匹配详情")


class QualityScoreDetail(BaseModel):
    """单条质量标准的评分详情。"""

    criterion_name: str = Field(description="标准名称")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="得分")
    weight: float = Field(default=1.0, description="权重")
    weighted_score: float = Field(default=0.0, description="加权得分")
    passed: bool = Field(default=False, description="是否通过")
    reason: str = Field(default="", description="评分理由")


class OutputQualityResult(BaseModel):
    """输出质量评测结果。"""

    overall_score: float = Field(default=0.0, ge=0.0, le=1.0, description="综合得分")
    passed: bool = Field(default=False, description="是否通过")
    criterion_scores: list[QualityScoreDetail] = Field(
        default_factory=list, description="各标准评分详情"
    )
    similarity_score: float = Field(default=0.0, ge=0.0, le=1.0, description="与期望输出的相似度")
    length_ratio: float = Field(default=0.0, description="输出长度与期望长度的比值")


class EvalCaseResult(BaseModel):
    """单条评测用例的结果。"""

    case_id: str = Field(description="用例 ID")
    case_name: str = Field(description="用例名称")
    task_type: EvalTaskType = Field(description="任务类型")
    status: EvalStatus = Field(default=EvalStatus.PENDING, description="执行状态")
    duration_ms: float = Field(default=0.0, description="执行耗时（毫秒）")

    # 实际输出
    actual_output: str = Field(default="", description="实际输出文本")
    actual_tool_calls: list[ActualToolCall] = Field(
        default_factory=list, description="实际工具调用列表"
    )

    # 评测结果
    tool_call_result: Optional[ToolCallAccuracyResult] = Field(
        default=None, description="工具调用准确率结果"
    )
    quality_result: Optional[OutputQualityResult] = Field(
        default=None, description="输出质量结果"
    )

    # 通用
    error_message: str = Field(default="", description="错误信息")
    issues: list[dict[str, Any]] = Field(default_factory=list, description="发现的问题列表")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class EvalReport(BaseModel):
    """评测报告。"""

    report_id: str = Field(default_factory=lambda: str(uuid4()), description="报告 ID")
    suite_name: str = Field(description="套件名称")
    suite_version: str = Field(default="", description="套件版本")

    # 汇总统计
    total_cases: int = Field(default=0, description="总用例数")
    passed: int = Field(default=0, description="通过数")
    failed: int = Field(default=0, description="失败数")
    errored: int = Field(default=0, description="错误数")
    skipped: int = Field(default=0, description="跳过数")

    # 工具调用准确率汇总
    avg_tool_accuracy: float = Field(default=0.0, description="平均工具调用准确率")
    avg_tool_f1: float = Field(default=0.0, description="平均工具调用 F1 分数")

    # 输出质量汇总
    avg_quality_score: float = Field(default=0.0, description="平均输出质量得分")

    # 详细结果
    case_results: list[EvalCaseResult] = Field(default_factory=list, description="各用例结果")

    # 回归信息
    regressions: list[dict[str, Any]] = Field(default_factory=list, description="回归项列表")
    has_regression: bool = Field(default=False, description="是否存在回归")

    # 时间信息
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    completed_at: Optional[datetime] = Field(default=None, description="完成时间")
    duration_seconds: float = Field(default=0.0, description="总耗时（秒）")

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")

    @property
    def pass_rate(self) -> float:
        """通过率。"""
        if self.total_cases == 0:
            return 0.0
        return self.passed / self.total_cases

    def summary(self) -> str:
        """生成人类可读的报告摘要。"""
        lines = [
            "=" * 70,
            f"Evaluation Report: {self.suite_name} (v{self.suite_version})",
            "=" * 70,
            f"Duration:        {self.duration_seconds:.1f}s",
            f"Total cases:     {self.total_cases}",
            f"Passed:          {self.passed}",
            f"Failed:          {self.failed}",
            f"Errored:         {self.errored}",
            f"Skipped:         {self.skipped}",
            f"Pass rate:       {self.pass_rate:.1%}",
            "",
            "--- Tool Call Accuracy ---",
            f"Avg accuracy:    {self.avg_tool_accuracy:.3f}",
            f"Avg F1:          {self.avg_tool_f1:.3f}",
            "",
            "--- Output Quality ---",
            f"Avg quality:     {self.avg_quality_score:.3f}",
        ]

        if self.regressions:
            lines.append("")
            lines.append("--- Regressions ---")
            for reg in self.regressions:
                severity = reg.get("severity", "warning")
                name = reg.get("case_name", "unknown")
                desc = reg.get("description", "")
                lines.append(f"  [{severity.upper()}] {name}: {desc}")

        lines.append("=" * 70)
        return "\n".join(lines)


# =============================================================================
# 4. 基线数据模型（用于回归检测）
# =============================================================================


class BaselineMetric(BaseModel):
    """基线指标。"""

    case_id: str = Field(description="用例 ID")
    case_name: str = Field(description="用例名称")
    tool_accuracy: float = Field(default=0.0, description="工具调用准确率基线")
    tool_f1: float = Field(default=0.0, description="工具调用 F1 基线")
    quality_score: float = Field(default=0.0, description="输出质量基线")
    recorded_at: datetime = Field(default_factory=datetime.now, description="记录时间")


class BaselineStore(BaseModel):
    """基线存储。"""

    store_id: str = Field(default_factory=lambda: str(uuid4()))
    suite_name: str = Field(description="套件名称")
    metrics: list[BaselineMetric] = Field(default_factory=list, description="基线指标列表")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")


# =============================================================================
# 5. 评测执行器（外部调用接口）
# =============================================================================


class EvalExecutor(Protocol):
    """评测执行器协议 — 外部实现需遵循此接口。

    用于将评测用例转化为实际的 LLM 调用或 Agent 执行，
    返回实际输出和工具调用记录。
    """

    async def execute(
        self,
        case: EvalTestCase,
    ) -> tuple[str, list[ActualToolCall]]:
        """执行评测用例。

        Args:
            case: 评测用例定义

        Returns:
            (actual_output, actual_tool_calls) 元组
        """
        ...  # pragma: no cover


# =============================================================================
# 6. 内置评估器
# =============================================================================


class ToolCallEvaluator:
    """工具调用准确率评估器。

    对比 expected_tool_calls 与 actual_tool_calls，计算准确率、精确率、召回率和 F1。
    支持参数部分匹配和调用次数约束检查。
    """

    def evaluate(
        self,
        expected: list[ExpectedToolCall],
        actual: list[ActualToolCall],
    ) -> ToolCallAccuracyResult:
        """评估工具调用准确率。

        Args:
            expected: 预期工具调用列表
            actual: 实际工具调用列表

        Returns:
            工具调用准确率结果
        """
        result = ToolCallAccuracyResult(
            total_expected=len(expected),
            total_actual=len(actual),
        )

        if not expected and not actual:
            result.accuracy = 1.0
            result.precision = 1.0
            result.recall = 1.0
            result.f1_score = 1.0
            return result

        # 统计实际调用的工具名 -> 调用次数
        actual_counts: dict[str, int] = {}
        for tc in actual:
            actual_counts[tc.tool_name] = actual_counts.get(tc.tool_name, 0) + 1

        # 逐个检查预期工具调用
        matched_total = 0
        expected_total = 0
        details: list[dict[str, Any]] = []

        for exp in expected:
            count = actual_counts.get(exp.tool_name, 0)
            expected_total += max(exp.min_call_count, 1)

            if not exp.required and count == 0:
                # 非必须且未调用 -> 跳过，不算缺失
                details.append({
                    "tool_name": exp.tool_name,
                    "status": "optional_skip",
                    "expected_min": exp.min_call_count,
                    "actual_count": 0,
                })
                continue

            if count >= exp.min_call_count:
                if exp.max_call_count is not None and count > exp.max_call_count:
                    # 超过最大次数
                    matched_total += exp.max_call_count
                    details.append({
                        "tool_name": exp.tool_name,
                        "status": "over_called",
                        "expected_range": f"[{exp.min_call_count}, {exp.max_call_count}]",
                        "actual_count": count,
                    })
                else:
                    matched_total += count
                    details.append({
                        "tool_name": exp.tool_name,
                        "status": "matched",
                        "expected_min": exp.min_call_count,
                        "actual_count": count,
                    })
            else:
                # 调用次数不足
                matched_total += count
                result.missing += 1
                result.missing_tools.append(exp.tool_name)
                details.append({
                    "tool_name": exp.tool_name,
                    "status": "insufficient",
                    "expected_min": exp.min_call_count,
                    "actual_count": count,
                })

        # 检查意外调用（实际有但预期中没有的工具）
        expected_names = {exp.tool_name for exp in expected}
        for tc in actual:
            if tc.tool_name not in expected_names:
                result.unexpected += 1
                if tc.tool_name not in result.unexpected_tools:
                    result.unexpected_tools.append(tc.tool_name)

        result.matched = matched_total
        result.details = details

        # 计算指标
        # Recall = matched / total_expected (预期中被满足的比例)
        if expected_total > 0:
            result.recall = min(matched_total / expected_total, 1.0)
        else:
            result.recall = 1.0 if not actual else 0.0

        # Precision = matched / total_actual (实际调用中正确的比例)
        if result.total_actual > 0:
            result.precision = min(matched_total / result.total_actual, 1.0)
        else:
            result.precision = 1.0 if not expected else 0.0

        # F1
        if result.precision + result.recall > 0:
            result.f1_score = (
                2 * result.precision * result.recall
                / (result.precision + result.recall)
            )
        else:
            result.f1_score = 0.0

        # Accuracy = (matched + correct_non_calls) / total_possible
        # 简化：使用 F1 作为准确率的近似
        result.accuracy = result.f1_score

        return result


class OutputQualityEvaluator:
    """输出质量评估器。

    基于多维度标准对输出质量进行评分：
    - 长度合理性
    - 内容相似度（与期望输出对比）
    - 格式规范性
    - 自定义标准
    """

    def evaluate(
        self,
        actual_output: str,
        expected_output: str,
        criteria: list[QualityCriterion],
    ) -> OutputQualityResult:
        """评估输出质量。

        Args:
            actual_output: 实际输出
            expected_output: 期望输出
            criteria: 质量评估标准列表

        Returns:
            输出质量评估结果
        """
        result = OutputQualityResult()
        criterion_scores: list[QualityScoreDetail] = []

        # 1. 内置评估：相似度
        similarity = self._compute_similarity(actual_output, expected_output)
        result.similarity_score = similarity
        criterion_scores.append(
            QualityScoreDetail(
                criterion_name="content_similarity",
                score=similarity,
                weight=2.0,
                weighted_score=similarity * 2.0,
                passed=similarity >= 0.3,
                reason=f"与期望输出的相似度为 {similarity:.3f}",
            )
        )

        # 2. 内置评估：长度合理性
        length_ratio = self._compute_length_ratio(actual_output, expected_output)
        result.length_ratio = length_ratio
        length_score = self._score_length_ratio(length_ratio)
        criterion_scores.append(
            QualityScoreDetail(
                criterion_name="length_ratio",
                score=length_score,
                weight=1.0,
                weighted_score=length_score * 1.0,
                passed=0.5 <= length_ratio <= 2.0,
                reason=f"输出长度比为 {length_ratio:.2f}",
            )
        )

        # 3. 内置评估：非空与基本有效性
        validity_score = self._score_validity(actual_output)
        criterion_scores.append(
            QualityScoreDetail(
                criterion_name="validity",
                score=validity_score,
                weight=1.5,
                weighted_score=validity_score * 1.5,
                passed=validity_score >= 0.5,
                reason="输出内容有效性检查",
            )
        )

        # 4. 自定义标准评估
        for criterion in criteria:
            score = self._evaluate_custom_criterion(
                criterion, actual_output, expected_output
            )
            criterion_scores.append(score)

        # 计算综合得分
        total_weight = sum(cs.weight for cs in criterion_scores)
        if total_weight > 0:
            result.overall_score = round(
                sum(cs.weighted_score for cs in criterion_scores) / total_weight, 4
            )
        else:
            result.overall_score = 0.0

        # 判定是否通过：所有标准都通过且综合分 >= 0.5
        result.passed = (
            all(cs.passed for cs in criterion_scores)
            and result.overall_score >= 0.5
        )
        result.criterion_scores = criterion_scores

        return result

    @staticmethod
    def _compute_similarity(text_a: str, text_b: str) -> float:
        """计算两个文本的相似度（基于 n-gram Jaccard）。"""
        if not text_a and not text_b:
            return 1.0
        if not text_a or not text_b:
            return 0.0

        def _ngrams(text: str, n: int = 3) -> set[str]:
            text = text.lower().strip()
            if len(text) < n:
                return {text}
            return {text[i : i + n] for i in range(len(text) - n + 1)}

        ngrams_a = _ngrams(text_a)
        ngrams_b = _ngrams(text_b)

        intersection = ngrams_a & ngrams_b
        union = ngrams_a | ngrams_b

        if not union:
            return 0.0
        return len(intersection) / len(union)

    @staticmethod
    def _compute_length_ratio(actual: str, expected: str) -> float:
        """计算输出长度比。"""
        expected_len = len(expected.strip())
        actual_len = len(actual.strip())
        if expected_len == 0:
            return 1.0 if actual_len == 0 else float("inf")
        return actual_len / expected_len

    @staticmethod
    def _score_length_ratio(ratio: float) -> float:
        """根据长度比评分。"""
        if ratio == float("inf"):
            return 0.3
        if 0.8 <= ratio <= 1.2:
            return 1.0
        if 0.5 <= ratio <= 2.0:
            return 0.7
        if 0.2 <= ratio <= 3.0:
            return 0.4
        return 0.2

    @staticmethod
    def _score_validity(text: str) -> float:
        """评估输出文本的有效性。"""
        if not text or not text.strip():
            return 0.0

        stripped = text.strip()

        # 纯重复字符
        if len(set(stripped)) <= 2 and len(stripped) > 5:
            return 0.1

        # 纯标点
        cleaned = re.sub(r"[\s\W]", "", stripped)
        if len(cleaned) < 2:
            return 0.2

        # 过短
        if len(stripped) < 10:
            return 0.4

        return 1.0

    @staticmethod
    def _evaluate_custom_criterion(
        criterion: QualityCriterion,
        actual_output: str,
        expected_output: str,
    ) -> QualityScoreDetail:
        """评估自定义质量标准。

        基于标准名称匹配内置评估模式，否则使用默认的关键词匹配。
        """
        name = criterion.name.lower()
        score = 0.5  # 默认中性分
        reason = ""

        if "completeness" in name or "完整性" in criterion.description:
            # 完整性：检查输出是否包含期望输出的关键片段
            if expected_output:
                keywords = set(re.findall(r"[\w一-鿿]{2,}", expected_output.lower()))
                if keywords:
                    actual_lower = actual_output.lower()
                    hit = sum(1 for kw in keywords if kw in actual_lower)
                    score = hit / len(keywords)
                    reason = f"关键词命中率: {hit}/{len(keywords)}"
            else:
                score = 0.5
                reason = "无期望输出，无法评估完整性"

        elif "conciseness" in name or "简洁" in criterion.description:
            # 简洁性：输出越短越好（在合理范围内）
            if actual_output:
                ratio = len(actual_output) / max(len(expected_output), 1)
                if ratio <= 1.0:
                    score = 1.0
                elif ratio <= 2.0:
                    score = 0.7
                else:
                    score = 0.3
                reason = f"长度比: {ratio:.2f}"
            else:
                score = 0.0
                reason = "输出为空"

        elif "correctness" in name or "正确" in criterion.description:
            # 正确性：与期望输出的相似度
            score = OutputQualityEvaluator._compute_similarity(actual_output, expected_output)
            reason = f"相似度: {score:.3f}"

        elif "format" in name or "格式" in criterion.description:
            # 格式规范性：检查是否有合理的结构
            has_paragraphs = "\n" in actual_output
            has_punctuation = bool(re.search(r"[。！？.!?]", actual_output))
            score = 0.5
            if has_paragraphs:
                score += 0.25
            if has_punctuation:
                score += 0.25
            reason = f"段落: {has_paragraphs}, 标点: {has_punctuation}"

        else:
            # 未知标准：使用长度非空性作为默认
            score = 1.0 if actual_output.strip() else 0.0
            reason = "默认评估（非空检查）"

        passed = score >= criterion.min_score
        return QualityScoreDetail(
            criterion_name=criterion.name,
            score=round(score, 4),
            weight=criterion.weight,
            weighted_score=round(score * criterion.weight, 4),
            passed=passed,
            reason=reason,
        )


# =============================================================================
# 7. 回归检测器
# =============================================================================


class RegressionDetector:
    """回归检测器 — 对比历史基线，发现退化项。

    检测逻辑：
    - 工具调用准确率下降超过阈值 -> 回归
    - 输出质量得分下降超过阈值 -> 回归
    - 新增失败用例 -> 回归
    """

    def __init__(
        self,
        accuracy_threshold: float = 0.05,
        quality_threshold: float = 0.1,
    ) -> None:
        """初始化回归检测器。

        Args:
            accuracy_threshold: 准确率下降阈值（超过此值视为回归）
            quality_threshold: 质量得分下降阈值
        """
        self._accuracy_threshold = accuracy_threshold
        self._quality_threshold = quality_threshold

    def detect(
        self,
        current_results: list[EvalCaseResult],
        baseline: BaselineStore,
    ) -> list[dict[str, Any]]:
        """检测回归项。

        Args:
            current_results: 当前评测结果
            baseline: 历史基线

        Returns:
            回归项列表
        """
        regressions: list[dict[str, Any]] = []

        # 构建基线索引
        baseline_map: dict[str, BaselineMetric] = {}
        for metric in baseline.metrics:
            baseline_map[metric.case_id] = metric

        for result in current_results:
            if result.status == EvalStatus.SKIPPED:
                continue

            base = baseline_map.get(result.case_id)
            if base is None:
                # 新用例，无基线可比
                continue

            # 检查工具调用准确率回归
            if result.tool_call_result and base.tool_accuracy > 0:
                current_acc = result.tool_call_result.accuracy
                drop = base.tool_accuracy - current_acc
                if drop > self._accuracy_threshold:
                    regressions.append({
                        "case_id": result.case_id,
                        "case_name": result.case_name,
                        "type": "tool_accuracy_regression",
                        "severity": SeverityLevel.CRITICAL.value if drop > 0.2 else SeverityLevel.ERROR_LEVEL.value,
                        "description": (
                            f"工具调用准确率从 {base.tool_accuracy:.3f} 下降到 "
                            f"{current_acc:.3f} (下降 {drop:.3f})"
                        ),
                        "baseline_value": base.tool_accuracy,
                        "current_value": current_acc,
                        "drop": drop,
                    })

            # 检查输出质量回归
            if result.quality_result and base.quality_score > 0:
                current_quality = result.quality_result.overall_score
                drop = base.quality_score - current_quality
                if drop > self._quality_threshold:
                    regressions.append({
                        "case_id": result.case_id,
                        "case_name": result.case_name,
                        "type": "quality_regression",
                        "severity": SeverityLevel.CRITICAL.value if drop > 0.3 else SeverityLevel.ERROR_LEVEL.value,
                        "description": (
                            f"输出质量从 {base.quality_score:.3f} 下降到 "
                            f"{current_quality:.3f} (下降 {drop:.3f})"
                        ),
                        "baseline_value": base.quality_score,
                        "current_value": current_quality,
                        "drop": drop,
                    })

            # 检查新增失败
            if result.status in (EvalStatus.FAILED, EvalStatus.ERROR):
                # 基线中有记录说明之前通过过
                regressions.append({
                    "case_id": result.case_id,
                    "case_name": result.case_name,
                    "type": "new_failure",
                    "severity": SeverityLevel.CRITICAL.value,
                    "description": f"用例从通过变为 {result.status.value}",
                    "error_message": result.error_message,
                })

        return regressions


# =============================================================================
# 8. 基线管理器
# =============================================================================


class BaselineManager:
    """基线管理器 — 持久化和管理评测基线数据。"""

    def __init__(self, baseline_dir: str = "./data/baselines") -> None:
        self._baseline_dir = Path(baseline_dir)
        self._baseline_dir.mkdir(parents=True, exist_ok=True)

    def _baseline_path(self, suite_name: str) -> Path:
        """获取基线文件路径。"""
        safe_name = re.sub(r"[^\w\-]", "_", suite_name)
        return self._baseline_dir / f"{safe_name}_baseline.json"

    def load(self, suite_name: str) -> Optional[BaselineStore]:
        """加载基线数据。"""
        path = self._baseline_path(suite_name)
        if not path.exists():
            logger.info(f"No baseline found for suite '{suite_name}'")
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            store = BaselineStore(**data)
            logger.info(
                f"Loaded baseline for '{suite_name}': {len(store.metrics)} metrics"
            )
            return store
        except (json.JSONDecodeError, Exception) as exc:
            logger.error(f"Failed to load baseline for '{suite_name}': {exc}")
            return None

    def save(self, store: BaselineStore) -> None:
        """保存基线数据。"""
        path = self._baseline_path(store.suite_name)
        store.updated_at = datetime.now()

        try:
            path.write_text(
                store.model_dump_json(indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"Saved baseline for '{store.suite_name}': {len(store.metrics)} metrics"
            )
        except OSError as exc:
            logger.error(f"Failed to save baseline: {exc}")

    def update_from_report(self, report: EvalReport) -> BaselineStore:
        """根据评测报告更新基线。

        只更新通过的用例的基线指标。

        Args:
            report: 评测报告

        Returns:
            更新后的基线存储
        """
        existing = self.load(report.suite_name)
        if existing is None:
            existing = BaselineStore(suite_name=report.suite_name)

        # 构建已有指标索引
        metric_map: dict[str, BaselineMetric] = {
            m.case_id: m for m in existing.metrics
        }

        for case_result in report.case_results:
            if case_result.status not in (EvalStatus.PASSED, EvalStatus.FAILED):
                continue

            tool_acc = 0.0
            tool_f1 = 0.0
            if case_result.tool_call_result:
                tool_acc = case_result.tool_call_result.accuracy
                tool_f1 = case_result.tool_call_result.f1_score

            quality = 0.0
            if case_result.quality_result:
                quality = case_result.quality_result.overall_score

            metric = BaselineMetric(
                case_id=case_result.case_id,
                case_name=case_result.case_name,
                tool_accuracy=tool_acc,
                tool_f1=tool_f1,
                quality_score=quality,
            )
            metric_map[case_result.case_id] = metric

        existing.metrics = list(metric_map.values())
        self.save(existing)
        return existing


# =============================================================================
# 9. 测试套件加载器
# =============================================================================


class TestSuiteLoader:
    """测试套件加载器 — 从 JSON 文件加载评测套件。"""

    @staticmethod
    def load_from_file(file_path: str | Path) -> EvalTestSuite:
        """从 JSON 文件加载评测套件。

        Args:
            file_path: JSON 文件路径

        Returns:
            评测套件对象

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 文件格式错误
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Test suite file not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

        return TestSuiteLoader._parse_suite(data, str(path))

    @staticmethod
    def load_from_dir(dir_path: str | Path) -> list[EvalTestSuite]:
        """从目录加载所有评测套件 JSON 文件。

        Args:
            dir_path: 目录路径

        Returns:
            评测套件列表
        """
        path = Path(dir_path)
        if not path.is_dir():
            logger.warning(f"Directory not found: {path}")
            return []

        suites: list[EvalTestSuite] = []
        for json_file in sorted(path.glob("*.json")):
            try:
                suite = TestSuiteLoader.load_from_file(json_file)
                suites.append(suite)
                logger.info(f"Loaded suite '{suite.name}' from {json_file}")
            except (ValueError, FileNotFoundError) as exc:
                logger.warning(f"Failed to load {json_file}: {exc}")

        return suites

    @staticmethod
    def _parse_suite(data: dict[str, Any], source: str = "") -> EvalTestSuite:
        """解析 JSON 数据为评测套件。"""
        if "name" not in data:
            raise ValueError(f"Test suite must have a 'name' field (source: {source})")

        cases: list[EvalTestCase] = []
        for case_data in data.get("cases", []):
            # 解析预期工具调用
            expected_calls: list[ExpectedToolCall] = []
            for etc in case_data.get("expected_tool_calls", []):
                expected_calls.append(ExpectedToolCall(**etc))

            # 解析质量标准
            criteria: list[QualityCriterion] = []
            for qc in case_data.get("quality_criteria", []):
                criteria.append(QualityCriterion(**qc))

            # 解析任务类型
            task_type_str = case_data.get("task_type", "tool_call_accuracy")
            try:
                task_type = EvalTaskType(task_type_str)
            except ValueError:
                task_type = EvalTaskType.CUSTOM

            case = EvalTestCase(
                case_id=case_data.get("case_id", str(uuid4())),
                name=case_data.get("name", ""),
                description=case_data.get("description", ""),
                task_type=task_type,
                tags=case_data.get("tags", []),
                input_text=case_data.get("input_text", ""),
                input_data=case_data.get("input_data", {}),
                expected_tool_calls=expected_calls,
                expected_output=case_data.get("expected_output", ""),
                quality_criteria=criteria,
                timeout_seconds=case_data.get("timeout_seconds", 60),
                metadata=case_data.get("metadata", {}),
            )
            cases.append(case)

        return EvalTestSuite(
            suite_id=data.get("suite_id", str(uuid4())),
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            cases=cases,
            metadata=data.get("metadata", {}),
        )

    @staticmethod
    def load_from_json_string(json_str: str) -> EvalTestSuite:
        """从 JSON 字符串加载评测套件。

        Args:
            json_str: JSON 字符串

        Returns:
            评测套件对象
        """
        data = json.loads(json_str)
        return TestSuiteLoader._parse_suite(data, "<json_string>")


# =============================================================================
# 10. 评测管道主类
# =============================================================================


class EvalPipeline:
    """评测管道 — 编排完整的评测流程。

    流程:
    1. 加载测试套件
    2. 执行评测用例（通过外部 Executor）
    3. 评估工具调用准确率
    4. 评估输出质量
    5. 回归检测
    6. 生成报告

    Usage::

        # 创建自定义执行器
        class MyExecutor:
            async def execute(self, case):
                # 调用 LLM / Agent 获取结果
                return actual_output, actual_tool_calls

        pipeline = EvalPipeline(executor=MyExecutor())
        report = await pipeline.run_from_file("test_suite.json")
        print(report.summary())
    """

    def __init__(
        self,
        executor: Any,
        baseline_dir: str = "./data/baselines",
        accuracy_threshold: float = 0.05,
        quality_threshold: float = 0.1,
    ) -> None:
        """初始化评测管道。

        Args:
            executor: 评测执行器（需实现 execute(case) -> (output, tool_calls) 方法）
            baseline_dir: 基线数据目录
            accuracy_threshold: 准确率回归阈值
            quality_threshold: 质量回归阈值
        """
        self._executor = executor
        self._tool_evaluator = ToolCallEvaluator()
        self._quality_evaluator = OutputQualityEvaluator()
        self._regression_detector = RegressionDetector(
            accuracy_threshold=accuracy_threshold,
            quality_threshold=quality_threshold,
        )
        self._baseline_manager = BaselineManager(baseline_dir)
        self._suite_loader = TestSuiteLoader()

    @property
    def baseline_manager(self) -> BaselineManager:
        """基线管理器。"""
        return self._baseline_manager

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run_from_file(
        self,
        file_path: str | Path,
        update_baseline: bool = True,
    ) -> EvalReport:
        """从文件加载套件并运行评测。

        Args:
            file_path: 测试套件 JSON 文件路径
            update_baseline: 是否在评测完成后更新基线

        Returns:
            评测报告
        """
        suite = self._suite_loader.load_from_file(file_path)
        return await self.run(suite, update_baseline=update_baseline)

    async def run_from_dir(
        self,
        dir_path: str | Path,
        update_baseline: bool = True,
    ) -> list[EvalReport]:
        """从目录加载所有套件并运行评测。

        Args:
            dir_path: 测试套件目录
            update_baseline: 是否更新基线

        Returns:
            评测报告列表
        """
        suites = self._suite_loader.load_from_dir(dir_path)
        reports: list[EvalReport] = []
        for suite in suites:
            report = await self.run(suite, update_baseline=update_baseline)
            reports.append(report)
        return reports

    async def run(
        self,
        suite: EvalTestSuite,
        update_baseline: bool = True,
    ) -> EvalReport:
        """运行评测套件。

        Args:
            suite: 评测套件
            update_baseline: 是否更新基线

        Returns:
            评测报告
        """
        report = EvalReport(
            suite_name=suite.name,
            suite_version=suite.version,
            total_cases=len(suite.cases),
            started_at=datetime.now(),
        )

        logger.info(
            f"Starting evaluation: '{suite.name}' v{suite.version} "
            f"({len(suite.cases)} cases)"
        )

        # 执行每个用例
        for case in suite.cases:
            case_result = await self._evaluate_case(case)
            report.case_results.append(case_result)

            # 更新统计
            if case_result.status == EvalStatus.PASSED:
                report.passed += 1
            elif case_result.status == EvalStatus.FAILED:
                report.failed += 1
            elif case_result.status == EvalStatus.ERROR:
                report.errored += 1
            elif case_result.status == EvalStatus.SKIPPED:
                report.skipped += 1

        # 计算汇总指标
        self._compute_summary(report)

        # 回归检测
        baseline = self._baseline_manager.load(suite.name)
        if baseline is not None:
            regressions = self._regression_detector.detect(
                report.case_results, baseline
            )
            report.regressions = regressions
            report.has_regression = len(regressions) > 0
            if regressions:
                logger.warning(
                    f"Detected {len(regressions)} regression(s) in suite '{suite.name}'"
                )
                for reg in regressions:
                    logger.warning(
                        f"  [{reg['severity']}] {reg['case_name']}: {reg['description']}"
                    )

        report.completed_at = datetime.now()
        report.duration_seconds = (
            report.completed_at - report.started_at
        ).total_seconds()

        # 更新基线
        if update_baseline:
            self._baseline_manager.update_from_report(report)

        logger.info(
            f"Evaluation completed: '{suite.name}' - "
            f"passed={report.passed}, failed={report.failed}, "
            f"errored={report.errored}, "
            f"avg_tool_acc={report.avg_tool_accuracy:.3f}, "
            f"avg_quality={report.avg_quality_score:.3f}, "
            f"regressions={len(report.regressions)}, "
            f"duration={report.duration_seconds:.1f}s"
        )

        return report

    async def run_batch(
        self,
        suites: list[EvalTestSuite],
        update_baseline: bool = True,
        max_concurrent: int = 1,
    ) -> list[EvalReport]:
        """批量运行多个评测套件。

        Args:
            suites: 评测套件列表
            update_baseline: 是否更新基线
            max_concurrent: 最大并发数

        Returns:
            评测报告列表
        """
        if max_concurrent <= 1:
            reports: list[EvalReport] = []
            for suite in suites:
                report = await self.run(suite, update_baseline=update_baseline)
                reports.append(report)
            return reports

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _run_one(suite: EvalTestSuite) -> EvalReport:
            async with semaphore:
                return await self.run(suite, update_baseline=update_baseline)

        return await asyncio.gather(*[_run_one(s) for s in suites])

    # ------------------------------------------------------------------
    # 单条用例评测
    # ------------------------------------------------------------------

    async def _evaluate_case(self, case: EvalTestCase) -> EvalCaseResult:
        """评测单条用例。

        Args:
            case: 评测用例

        Returns:
            用例评测结果
        """
        result = EvalCaseResult(
            case_id=case.case_id,
            case_name=case.name,
            task_type=case.task_type,
            status=EvalStatus.RUNNING,
        )

        start_time = time.monotonic()

        try:
            # 执行用例（带超时）
            actual_output, actual_tool_calls = await asyncio.wait_for(
                self._executor.execute(case),
                timeout=case.timeout_seconds,
            )

            result.actual_output = actual_output
            result.actual_tool_calls = actual_tool_calls
            result.duration_ms = (time.monotonic() - start_time) * 1000

            # 根据任务类型执行评估
            if case.task_type == EvalTaskType.TOOL_CALL_ACCURACY:
                self._evaluate_tool_calls(case, result)
            elif case.task_type == EvalTaskType.OUTPUT_QUALITY:
                self._evaluate_output_quality(case, result)
            elif case.task_type == EvalTaskType.CUSTOM:
                # 自定义：同时执行两种评估
                if case.expected_tool_calls:
                    self._evaluate_tool_calls(case, result)
                if case.expected_output or case.quality_criteria:
                    self._evaluate_output_quality(case, result)

            # 综合判定
            result.status = self._determine_status(result, case)

        except asyncio.TimeoutError:
            result.status = EvalStatus.ERROR
            result.error_message = f"Timeout after {case.timeout_seconds}s"
            result.duration_ms = (time.monotonic() - start_time) * 1000
            logger.error(f"Case '{case.name}' timed out")

        except Exception as exc:
            result.status = EvalStatus.ERROR
            result.error_message = f"{type(exc).__name__}: {exc}"
            result.duration_ms = (time.monotonic() - start_time) * 1000
            logger.error(f"Case '{case.name}' error: {exc}")

        return result

    def _evaluate_tool_calls(
        self, case: EvalTestCase, result: EvalCaseResult
    ) -> None:
        """评估工具调用准确率。"""
        if not case.expected_tool_calls:
            return

        tool_result = self._tool_evaluator.evaluate(
            case.expected_tool_calls, result.actual_tool_calls
        )
        result.tool_call_result = tool_result

        # 记录问题
        if tool_result.missing_tools:
            result.issues.append({
                "type": "missing_tools",
                "severity": SeverityLevel.ERROR_LEVEL.value,
                "description": f"缺失工具调用: {', '.join(tool_result.missing_tools)}",
            })
        if tool_result.unexpected_tools:
            result.issues.append({
                "type": "unexpected_tools",
                "severity": SeverityLevel.WARNING.value,
                "description": f"意外工具调用: {', '.join(tool_result.unexpected_tools)}",
            })

    def _evaluate_output_quality(
        self, case: EvalTestCase, result: EvalCaseResult
    ) -> None:
        """评估输出质量。"""
        quality_result = self._quality_evaluator.evaluate(
            result.actual_output,
            case.expected_output,
            case.quality_criteria,
        )
        result.quality_result = quality_result

        # 记录未通过的标准
        for cs in quality_result.criterion_scores:
            if not cs.passed:
                result.issues.append({
                    "type": "quality_criterion_failed",
                    "severity": SeverityLevel.WARNING.value,
                    "criterion": cs.criterion_name,
                    "description": f"质量标准 '{cs.criterion_name}' 未通过: {cs.reason}",
                })

    @staticmethod
    def _determine_status(
        result: EvalCaseResult, case: EvalTestCase
    ) -> EvalStatus:
        """根据评估结果判定最终状态。"""
        has_tool_eval = result.tool_call_result is not None
        has_quality_eval = result.quality_result is not None

        if not has_tool_eval and not has_quality_eval:
            # 无评估 -> 默认通过
            return EvalStatus.PASSED

        all_passed = True

        if has_tool_eval:
            tool_res = result.tool_call_result
            assert tool_res is not None
            if tool_res.accuracy < 0.5:
                all_passed = False

        if has_quality_eval:
            qual_res = result.quality_result
            assert qual_res is not None
            if not qual_res.passed:
                all_passed = False

        return EvalStatus.PASSED if all_passed else EvalStatus.FAILED

    @staticmethod
    def _compute_summary(report: EvalReport) -> None:
        """计算报告汇总指标。"""
        tool_accuracies: list[float] = []
        tool_f1s: list[float] = []
        quality_scores: list[float] = []

        for cr in report.case_results:
            if cr.tool_call_result:
                tool_accuracies.append(cr.tool_call_result.accuracy)
                tool_f1s.append(cr.tool_call_result.f1_score)
            if cr.quality_result:
                quality_scores.append(cr.quality_result.overall_score)

        report.avg_tool_accuracy = (
            sum(tool_accuracies) / len(tool_accuracies) if tool_accuracies else 0.0
        )
        report.avg_tool_f1 = (
            sum(tool_f1s) / len(tool_f1s) if tool_f1s else 0.0
        )
        report.avg_quality_score = (
            sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        )

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def compare_reports(
        self,
        report_a: EvalReport,
        report_b: EvalReport,
    ) -> dict[str, Any]:
        """对比两份评测报告。

        Args:
            report_a: 基准报告
            report_b: 对比报告

        Returns:
            对比结果
        """
        comparison: dict[str, Any] = {
            "baseline": {
                "suite_name": report_a.suite_name,
                "pass_rate": report_a.pass_rate,
                "avg_tool_accuracy": report_a.avg_tool_accuracy,
                "avg_quality_score": report_a.avg_quality_score,
            },
            "current": {
                "suite_name": report_b.suite_name,
                "pass_rate": report_b.pass_rate,
                "avg_tool_accuracy": report_b.avg_tool_accuracy,
                "avg_quality_score": report_b.avg_quality_score,
            },
            "delta": {
                "pass_rate": report_b.pass_rate - report_a.pass_rate,
                "avg_tool_accuracy": report_b.avg_tool_accuracy - report_a.avg_tool_accuracy,
                "avg_quality_score": report_b.avg_quality_score - report_a.avg_quality_score,
            },
            "improved": [],
            "degraded": [],
        }

        # 逐用例对比
        a_map = {cr.case_id: cr for cr in report_a.case_results}
        for cr_b in report_b.case_results:
            cr_a = a_map.get(cr_b.case_id)
            if cr_a is None:
                continue

            if cr_a.status == EvalStatus.FAILED and cr_b.status == EvalStatus.PASSED:
                comparison["improved"].append(cr_b.case_name)
            elif cr_a.status == EvalStatus.PASSED and cr_b.status in (
                EvalStatus.FAILED, EvalStatus.ERROR
            ):
                comparison["degraded"].append(cr_b.case_name)

        return comparison

    @staticmethod
    def export_report_json(report: EvalReport, file_path: str | Path) -> Path:
        """导出评测报告为 JSON 文件。

        Args:
            report: 评测报告
            file_path: 输出文件路径

        Returns:
            输出文件路径
        """
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"Exported report to {path}")
        return path

    @staticmethod
    def export_report_markdown(report: EvalReport, file_path: str | Path) -> Path:
        """导出评测报告为 Markdown 文件。

        Args:
            report: 评测报告
            file_path: 输出文件路径

        Returns:
            输出文件路径
        """
        lines = [
            f"# Evaluation Report: {report.suite_name}",
            "",
            f"**Version:** {report.suite_version}  ",
            f"**Date:** {report.started_at.isoformat() if report.started_at else 'N/A'}  ",
            f"**Duration:** {report.duration_seconds:.1f}s  ",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total Cases | {report.total_cases} |",
            f"| Passed | {report.passed} |",
            f"| Failed | {report.failed} |",
            f"| Errored | {report.errored} |",
            f"| Pass Rate | {report.pass_rate:.1%} |",
            f"| Avg Tool Accuracy | {report.avg_tool_accuracy:.3f} |",
            f"| Avg Tool F1 | {report.avg_tool_f1:.3f} |",
            f"| Avg Quality Score | {report.avg_quality_score:.3f} |",
            "",
        ]

        # 用例详情
        lines.append("## Case Results")
        lines.append("")
        lines.append("| Case | Status | Tool Acc | Quality | Duration |")
        lines.append("|------|--------|----------|---------|----------|")
        for cr in report.case_results:
            tool_acc = f"{cr.tool_call_result.accuracy:.3f}" if cr.tool_call_result else "-"
            quality = f"{cr.quality_result.overall_score:.3f}" if cr.quality_result else "-"
            lines.append(
                f"| {cr.case_name} | {cr.status.value} | {tool_acc} | {quality} | {cr.duration_ms:.0f}ms |"
            )

        # 回归信息
        if report.regressions:
            lines.append("")
            lines.append("## Regressions")
            lines.append("")
            for reg in report.regressions:
                lines.append(
                    f"- **[{reg.get('severity', 'warning').upper()}]** "
                    f"{reg.get('case_name', 'unknown')}: {reg.get('description', '')}"
                )

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Exported markdown report to {path}")
        return path
