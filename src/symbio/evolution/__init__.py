"""Evolution engine — 数据飞轮与自进化能力。

- DatasetExporter: 从执行轨迹自动生产高质量微调数据集
- ExportFormat: 支持 ShareGPT / Alpaca / OpenAI 格式
- PIIDetector: PII 脱敏引擎
- FeedbackCollector: 显式/隐式反馈收集与统计
- PatternAnalyzer: 失败复盘、根因记录与成功路径识别
- PromptOps: Prompt 版本控制、A/B 测试、灰度发布
- SelfOptimizer: Prompt 效果追踪、自动优化、进化日志
- EvalPipeline: 评测管道，支持多维度评估与回归检测
"""

from symbio.evolution.dataset_exporter import (
    DataCleaner,
    DatasetExporter,
    ExportConfig,
    ExportFormat,
    ExportReport,
    ExportTracker,
    FormatConverter,
    PIIDetector,
    PIIMaskConfig,
    QualityFilter,
    QualityFilterConfig,
)
from symbio.evolution.feedback import (
    ExplicitFeedback,
    FeedbackCollector,
    FeedbackQuery,
    FeedbackStats,
    FeedbackType,
    ImplicitActionType,
    ImplicitFeedback,
)
from symbio.evolution.analyzer import (
    AnalysisResult,
    FailureAnalysis,
    FailureCategory,
    FailureSeverity,
    PatternAnalyzer,
    RootCause,
    SuccessPath,
)
from symbio.evolution.promptops import (
    ABTest,
    ABTestResult,
    ABTestStatus,
    ABTestVariant,
    CanaryRelease,
    CanaryStage,
    PromptOps,
    PromptVersion,
    VersionQuery,
    VersionStatus,
)
from symbio.evolution.self_optimizer import (
    EvolutionLogEntry,
    MetricType,
    OptimizationConfig,
    OptimizationStrategy,
    OptimizationSuggestion,
    PerformanceRecord,
    PerformanceSummary,
    SelfOptimizer,
)
from symbio.evolution.eval_pipeline import (
    ActualToolCall,
    BaselineManager,
    BaselineMetric,
    BaselineStore,
    EvalCaseResult,
    EvalPipeline,
    EvalReport,
    EvalStatus,
    EvalTaskType,
    EvalTestCase,
    EvalTestSuite,
    ExpectedToolCall,
    OutputQualityEvaluator,
    OutputQualityResult,
    QualityCriterion,
    QualityScoreDetail,
    RegressionDetector,
    SeverityLevel,
    TestSuiteLoader,
    ToolCallAccuracyResult,
    ToolCallEvaluator,
)

__all__ = [
    # dataset_exporter
    "DatasetExporter",
    "ExportConfig",
    "ExportFormat",
    "ExportReport",
    "ExportTracker",
    "PIIDetector",
    "PIIMaskConfig",
    "QualityFilter",
    "QualityFilterConfig",
    "DataCleaner",
    "FormatConverter",
    # feedback
    "ExplicitFeedback",
    "FeedbackCollector",
    "FeedbackQuery",
    "FeedbackStats",
    "FeedbackType",
    "ImplicitActionType",
    "ImplicitFeedback",
    # analyzer
    "AnalysisResult",
    "FailureAnalysis",
    "FailureCategory",
    "FailureSeverity",
    "PatternAnalyzer",
    "RootCause",
    "SuccessPath",
    # promptops
    "ABTest",
    "ABTestResult",
    "ABTestStatus",
    "ABTestVariant",
    "CanaryRelease",
    "CanaryStage",
    "PromptOps",
    "PromptVersion",
    "VersionQuery",
    "VersionStatus",
    # self_optimizer
    "EvolutionLogEntry",
    "MetricType",
    "OptimizationConfig",
    "OptimizationStrategy",
    "OptimizationSuggestion",
    "PerformanceRecord",
    "PerformanceSummary",
    "SelfOptimizer",
    # eval_pipeline
    "ActualToolCall",
    "BaselineManager",
    "BaselineMetric",
    "BaselineStore",
    "EvalCaseResult",
    "EvalPipeline",
    "EvalReport",
    "EvalStatus",
    "EvalTaskType",
    "EvalTestCase",
    "EvalTestSuite",
    "ExpectedToolCall",
    "OutputQualityEvaluator",
    "OutputQualityResult",
    "QualityCriterion",
    "QualityScoreDetail",
    "RegressionDetector",
    "SeverityLevel",
    "TestSuiteLoader",
    "ToolCallAccuracyResult",
    "ToolCallEvaluator",
]
