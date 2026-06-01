"""DAG 分层路由评估 - 70/15/10/5% 路由策略"""

from __future__ import annotations

from collections import defaultdict
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("layered_router")


# ---------------------------------------------------------------------------
# 枚举与数据模型
# ---------------------------------------------------------------------------

class ClassificationResult(str, Enum):
    """DAG 节点执行结果分类"""
    SUCCESS = "success"                      # 70% - 无需 LLM，直接继续
    TRANSIENT_ERROR = "transient_error"      # 15% - 规则引擎简单重试
    UNKNOWN_ERROR = "unknown_error"          # 10% - 轻量模型诊断
    STRUCTURAL_ERROR = "structural_error"    # 5%  - 强模型拓扑重构


class RoutingDecision(BaseModel):
    """路由决策"""
    classification: ClassificationResult
    reason: str = ""
    suggested_action: str = ""
    model_tier: Optional[str] = None        # "haiku" / "sonnet" / "opus" / None


class RetryStrategy(BaseModel):
    """重试策略"""
    max_retries: int = 3
    backoff_base: float = 1.0               # 基础退避秒数
    backoff_max: float = 30.0               # 最大退避秒数
    jitter: bool = True                     # 是否添加随机抖动


class CircuitBreakerStatus(BaseModel):
    """熔断器状态"""
    tripped: bool = False
    reason: str = ""
    step_count: int = 0
    token_usage: int = 0
    node_failure_count: int = 0


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """三重熔断器 - 防止 DAG 执行失控

    1. 最大步数限制（默认 50）
    2. Token 预算限制（默认 100K）
    3. 重复失败检测（同一节点相同错误 3 次 → 升级为结构性错误）
    """

    def __init__(
        self,
        max_steps: int = 50,
        token_budget: int = 100_000,
        repeat_failure_threshold: int = 3,
    ):
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.repeat_failure_threshold = repeat_failure_threshold
        # (node_id, error_signature) -> failure_count
        self._failure_tracker: dict[tuple[str, str], int] = defaultdict(int)

    def record_failure(self, node_id: str, error_signature: str) -> None:
        """记录节点失败"""
        key = (node_id, error_signature)
        self._failure_tracker[key] += 1

    def get_failure_count(self, node_id: str, error_signature: str) -> int:
        """查询特定失败次数"""
        return self._failure_tracker.get((node_id, error_signature), 0)

    def is_repeat_failure(self, node_id: str, error_signature: str) -> bool:
        """判断是否达到重复失败阈值"""
        return self.get_failure_count(node_id, error_signature) >= self.repeat_failure_threshold

    def check(
        self,
        step_count: int,
        token_usage: int,
        node_failures: Optional[dict[str, int]] = None,
    ) -> CircuitBreakerStatus:
        """检查熔断条件

        Args:
            step_count: 当前已执行步数
            token_usage: 当前已消耗 token 数
            node_failures: 各节点失败次数（可选，用于外部聚合检查）

        Returns:
            CircuitBreakerStatus，tripped=True 表示应终止执行
        """
        # 步数限制
        if step_count >= self.max_steps:
            return CircuitBreakerStatus(
                tripped=True,
                reason=f"步数达到上限 ({step_count}/{self.max_steps})",
                step_count=step_count,
                token_usage=token_usage,
            )

        # Token 预算限制
        if token_usage >= self.token_budget:
            return CircuitBreakerStatus(
                tripped=True,
                reason=f"Token 预算耗尽 ({token_usage}/{self.token_budget})",
                step_count=step_count,
                token_usage=token_usage,
            )

        # 外部节点失败聚合检查
        max_failures = max(node_failures.values()) if node_failures else 0
        if max_failures >= self.repeat_failure_threshold:
            return CircuitBreakerStatus(
                tripped=True,
                reason=f"节点重复失败达到阈值 ({max_failures}/{self.repeat_failure_threshold})",
                step_count=step_count,
                token_usage=token_usage,
                node_failure_count=max_failures,
            )

        return CircuitBreakerStatus(
            tripped=False,
            step_count=step_count,
            token_usage=token_usage,
            node_failure_count=max_failures,
        )

    def reset(self) -> None:
        """重置熔断器状态"""
        self._failure_tracker.clear()


# ---------------------------------------------------------------------------
# 已知错误模式表
# ---------------------------------------------------------------------------

# 瞬态错误关键词 → 匹配即归类为 TRANSIENT_ERROR
_TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "503",
    "connection reset",
    "connection refused",
    "temporary",
    "try again",
    "econnreset",
    "socket hang up",
)

# 不可恢复错误关键词 → 匹配即归类为 STRUCTURAL_ERROR
_STRUCTURAL_ERROR_PATTERNS: tuple[str, ...] = (
    "schema mismatch",
    "missing dependency",
    "circular dependency",
    "topology",
    "invalid dag",
    "deadlock",
    "incompatible",
    "type mismatch",
    "assertion failed",
)


def _error_signature(error: Any) -> str:
    """提取错误签名用于重复失败检测"""
    if error is None:
        return ""
    text = str(error).lower().strip()
    # 取前 120 字符作为签名，避免超长错误信息干扰
    return text[:120]


# ---------------------------------------------------------------------------
# 分层路由器
# ---------------------------------------------------------------------------

class LayeredRouter:
    """DAG 分层路由器

    实现 70/15/10/5% 路由策略：
    - SUCCESS (70%): 无需 LLM，直接继续下一节点
    - TRANSIENT_ERROR (15%): 规则引擎简单重试
    - UNKNOWN_ERROR (10%): 轻量模型 (Haiku/8B) 诊断
    - STRUCTURAL_ERROR (5%): 强模型 (Sonnet/Opus) 拓扑重构
    """

    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        transient_patterns: Optional[tuple[str, ...]] = None,
        structural_patterns: Optional[tuple[str, ...]] = None,
    ):
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._transient_patterns = transient_patterns or _TRANSIENT_ERROR_PATTERNS
        self._structural_patterns = structural_patterns or _STRUCTURAL_ERROR_PATTERNS

        # 各分类对应的默认重试策略
        self._retry_strategies: dict[ClassificationResult, RetryStrategy] = {
            ClassificationResult.SUCCESS: RetryStrategy(max_retries=0),
            ClassificationResult.TRANSIENT_ERROR: RetryStrategy(max_retries=3, backoff_base=1.0, backoff_max=10.0),
            ClassificationResult.UNKNOWN_ERROR: RetryStrategy(max_retries=2, backoff_base=2.0, backoff_max=20.0),
            ClassificationResult.STRUCTURAL_ERROR: RetryStrategy(max_retries=1, backoff_base=5.0, backoff_max=30.0),
        }

        logger.info("LayeredRouter 初始化完成")

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def classify_result(
        self,
        node_id: str,
        result: Any = None,
        error: Any = None,
    ) -> RoutingDecision:
        """对 DAG 节点执行结果进行分类

        Args:
            node_id: 节点 ID
            result: 节点执行结果（成功时有值）
            error: 节点执行错误（失败时有值）

        Returns:
            RoutingDecision
        """
        # 无错误 → 成功
        if error is None:
            logger.debug(f"节点 {node_id}: SUCCESS")
            return RoutingDecision(
                classification=ClassificationResult.SUCCESS,
                reason="节点执行成功，无需 LLM 干预",
                suggested_action="continue",
                model_tier=None,
            )

        sig = _error_signature(error)

        # 记录失败（用于重复失败检测）
        self.circuit_breaker.record_failure(node_id, sig)

        # 检查是否重复失败 → 升级为结构性错误
        if self.circuit_breaker.is_repeat_failure(node_id, sig):
            logger.warning(f"节点 {node_id}: STRUCTURAL_ERROR (重复失败升级)")
            return RoutingDecision(
                classification=ClassificationResult.STRUCTURAL_ERROR,
                reason=f"节点 {node_id} 同一错误重复 {self.circuit_breaker.repeat_failure_threshold} 次，升级为结构性错误",
                suggested_action="restructure_dag",
                model_tier="opus",
            )

        error_lower = sig

        # 匹配结构性错误模式
        for pattern in self._structural_patterns:
            if pattern in error_lower:
                logger.info(f"节点 {node_id}: STRUCTURAL_ERROR (匹配模式: {pattern})")
                return RoutingDecision(
                    classification=ClassificationResult.STRUCTURAL_ERROR,
                    reason=f"错误匹配结构性模式: {pattern}",
                    suggested_action="restructure_dag",
                    model_tier="opus",
                )

        # 匹配瞬态错误模式
        for pattern in self._transient_patterns:
            if pattern in error_lower:
                logger.info(f"节点 {node_id}: TRANSIENT_ERROR (匹配模式: {pattern})")
                return RoutingDecision(
                    classification=ClassificationResult.TRANSIENT_ERROR,
                    reason=f"错误匹配瞬态模式: {pattern}",
                    suggested_action="retry",
                    model_tier=None,
                )

        # 未匹配任何模式 → 未知错误，需要轻量模型诊断
        logger.info(f"节点 {node_id}: UNKNOWN_ERROR")
        return RoutingDecision(
            classification=ClassificationResult.UNKNOWN_ERROR,
            reason="错误未匹配已知模式，需要轻量模型诊断",
            suggested_action="diagnose_with_llm",
            model_tier="haiku",
        )

    def get_retry_strategy(self, error_type: ClassificationResult) -> RetryStrategy:
        """获取指定错误类型的重试策略

        Args:
            error_type: 错误分类

        Returns:
            RetryStrategy
        """
        return self._retry_strategies.get(
            error_type,
            RetryStrategy(max_retries=1),
        )

    def should_use_llm(self, classification: ClassificationResult) -> tuple[bool, str]:
        """判断是否需要调用 LLM 以及使用哪个模型层级

        Args:
            classification: 错误分类

        Returns:
            (use_llm, model_tier) 元组
            - model_tier: "haiku" / "sonnet" / "opus" / ""
        """
        if classification == ClassificationResult.SUCCESS:
            return (False, "")
        if classification == ClassificationResult.TRANSIENT_ERROR:
            return (False, "")
        if classification == ClassificationResult.UNKNOWN_ERROR:
            return (True, "haiku")
        if classification == ClassificationResult.STRUCTURAL_ERROR:
            return (True, "opus")
        return (False, "")

    # ------------------------------------------------------------------
    # 集成方法：一次性完成分类 + 熔断检查
    # ------------------------------------------------------------------

    def evaluate(
        self,
        node_id: str,
        result: Any = None,
        error: Any = None,
        step_count: int = 0,
        token_usage: int = 0,
        node_failures: Optional[dict[str, int]] = None,
    ) -> tuple[RoutingDecision, CircuitBreakerStatus]:
        """完整评估：分类 + 熔断检查

        Args:
            node_id: 节点 ID
            result: 节点执行结果
            error: 节点执行错误
            step_count: 当前步数
            token_usage: 当前 token 消耗
            node_failures: 各节点失败次数

        Returns:
            (RoutingDecision, CircuitBreakerStatus) 元组
        """
        decision = self.classify_result(node_id, result, error)
        cb_status = self.circuit_breaker.check(step_count, token_usage, node_failures)

        if cb_status.tripped:
            logger.warning(f"熔断器触发: {cb_status.reason}")

        return decision, cb_status
