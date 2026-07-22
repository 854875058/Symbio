"""仿真测试沙箱 - 场景模拟、边界行为发现与回归测试"""

from __future__ import annotations

import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("agents.simulator")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class ScenarioStatus(str, Enum):
    """场景状态"""

    CREATED = "created"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class AssertionType(str, Enum):
    """断言类型"""

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    IN_RANGE = "in_range"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    NO_EXCEPTION = "no_exception"
    CUSTOM = "custom"


class Assertion(BaseModel):
    """测试断言"""

    assertion_id: str = Field(default_factory=lambda: str(uuid4()))
    type: AssertionType
    description: str = ""
    expected: Any = None
    actual: Any = None
    passed: bool = False
    message: str = ""


class ScenarioStep(BaseModel):
    """场景步骤"""

    step_id: int
    name: str
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    assertions: list[Assertion] = Field(default_factory=list)
    duration_ms: float = 0.0
    status: ScenarioStatus = ScenarioStatus.CREATED
    error: Optional[str] = None


class ScenarioResult(BaseModel):
    """场景执行结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    scenario_id: str
    scenario_name: str
    status: ScenarioStatus = ScenarioStatus.CREATED
    total_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0
    total_assertions: int = 0
    passed_assertions: int = 0
    failed_assertions: int = 0
    duration_ms: float = 0.0
    step_results: list[ScenarioStep] = Field(default_factory=list)
    boundary_findings: list[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class Scenario(BaseModel):
    """测试场景定义"""

    scenario_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    steps: list[ScenarioStep] = Field(default_factory=list)
    setup_actions: list[str] = Field(default_factory=list)
    teardown_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegressionReport(BaseModel):
    """回归测试报告"""

    report_id: str = Field(default_factory=lambda: str(uuid4()))
    total_scenarios: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    results: list[ScenarioResult] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 边界行为探测器
# ---------------------------------------------------------------------------


class BoundaryProber:
    """边界行为探测器 - 自动发现输入边界和异常行为"""

    def __init__(self):
        self._findings: list[str] = []

    def probe_null_inputs(self, action: Callable[..., Any], param_names: list[str]) -> list[str]:
        """探测空值输入"""
        findings: list[str] = []
        for name in param_names:
            try:
                kwargs = {name: None}
                action(**kwargs)
                findings.append(f"参数 {name} 接受 None 值未抛出异常")
            except (TypeError, ValueError) as exc:
                findings.append(f"参数 {name} 为 None 时抛出: {type(exc).__name__}: {exc}")
            except Exception as exc:
                findings.append(f"参数 {name} 为 None 时产生意外异常: {type(exc).__name__}: {exc}")
        self._findings.extend(findings)
        return findings

    def probe_boundary_values(
        self, action: Callable[..., Any], param_ranges: dict[str, list[Any]]
    ) -> list[str]:
        """探测边界值"""
        findings: list[str] = []
        for name, values in param_ranges.items():
            for value in values:
                try:
                    kwargs = {name: value}
                    result = action(**kwargs)
                    findings.append(f"参数 {name}={value} 正常返回: {result}")
                except Exception as exc:
                    findings.append(f"参数 {name}={value} 异常: {type(exc).__name__}: {exc}")
        self._findings.extend(findings)
        return findings

    def probe_concurrent_access(
        self, action: Callable[..., Any], iterations: int = 100
    ) -> list[str]:
        """探测并发安全性 (简化版)"""
        import threading

        findings: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def run_action() -> None:
            try:
                action()
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run_action) for _ in range(iterations)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        if errors:
            findings.append(f"并发执行 {iterations} 次发现 {len(errors)} 个错误")
            for err in errors[:5]:
                findings.append(f"  - {type(err).__name__}: {err}")
        else:
            findings.append(f"并发执行 {iterations} 次无异常")

        self._findings.extend(findings)
        return findings

    def get_findings(self) -> list[str]:
        """获取所有探测发现"""
        return list(self._findings)

    def clear(self) -> None:
        """清空发现"""
        self._findings.clear()


# ---------------------------------------------------------------------------
# 断言引擎
# ---------------------------------------------------------------------------


class AssertionEngine:
    """断言执行引擎"""

    @staticmethod
    def evaluate(assertion: Assertion) -> Assertion:
        """执行断言并更新结果"""
        try:
            if assertion.type == AssertionType.EQUALS:
                assertion.passed = assertion.actual == assertion.expected
                assertion.message = (
                    "值相等"
                    if assertion.passed
                    else f"期望 {assertion.expected}, 实际 {assertion.actual}"
                )
            elif assertion.type == AssertionType.NOT_EQUALS:
                assertion.passed = assertion.actual != assertion.expected
                assertion.message = (
                    "值不相等" if assertion.passed else f"值不应等于 {assertion.expected}"
                )
            elif assertion.type == AssertionType.CONTAINS:
                assertion.passed = assertion.expected in assertion.actual
                assertion.message = (
                    "包含目标值"
                    if assertion.passed
                    else f"{assertion.actual} 不包含 {assertion.expected}"
                )
            elif assertion.type == AssertionType.GREATER_THAN:
                assertion.passed = assertion.actual > assertion.expected
                assertion.message = (
                    "大于阈值"
                    if assertion.passed
                    else f"{assertion.actual} 不大于 {assertion.expected}"
                )
            elif assertion.type == AssertionType.LESS_THAN:
                assertion.passed = assertion.actual < assertion.expected
                assertion.message = (
                    "小于阈值"
                    if assertion.passed
                    else f"{assertion.actual} 不小于 {assertion.expected}"
                )
            elif assertion.type == AssertionType.IN_RANGE:
                low, high = assertion.expected
                assertion.passed = low <= assertion.actual <= high
                assertion.message = (
                    f"在范围 [{low}, {high}] 内"
                    if assertion.passed
                    else f"{assertion.actual} 不在范围 [{low}, {high}] 内"
                )
            elif assertion.type == AssertionType.IS_TRUE:
                assertion.passed = bool(assertion.actual) is True
                assertion.message = "为真" if assertion.passed else "不为真"
            elif assertion.type == AssertionType.IS_FALSE:
                assertion.passed = bool(assertion.actual) is False
                assertion.message = "为假" if assertion.passed else "不为假"
            elif assertion.type == AssertionType.NO_EXCEPTION:
                assertion.passed = assertion.actual is None
                assertion.message = (
                    "无异常" if assertion.passed else f"产生异常: {assertion.actual}"
                )
            elif assertion.type == AssertionType.CUSTOM:
                if callable(assertion.expected):
                    assertion.passed = bool(assertion.expected(assertion.actual))
                    assertion.message = "自定义断言通过" if assertion.passed else "自定义断言失败"
                else:
                    assertion.passed = False
                    assertion.message = "自定义断言需要 callable 作为 expected"
            else:
                assertion.passed = False
                assertion.message = f"未知断言类型: {assertion.type}"
        except Exception as exc:
            assertion.passed = False
            assertion.message = f"断言执行异常: {type(exc).__name__}: {exc}"

        return assertion


# ---------------------------------------------------------------------------
# 仿真沙箱
# ---------------------------------------------------------------------------


class SimulationSandbox:
    """仿真测试沙箱

    支持场景模拟、边界行为发现和回归测试。

    用法:
        sandbox = SimulationSandbox()
        scenario = Scenario(name="用户登录测试", steps=[...])
        result = await sandbox.run_scenario(scenario)
    """

    def __init__(self):
        self._scenarios: dict[str, Scenario] = {}
        self._results: dict[str, ScenarioResult] = {}
        self._action_registry: dict[str, Callable[..., Any]] = {}
        self._boundary_prober = BoundaryProber()
        self._assertion_engine = AssertionEngine()

    def register_action(self, name: str, action: Callable[..., Any]) -> None:
        """注册可执行的动作

        Args:
            name: 动作名称
            action: 动作执行函数
        """
        self._action_registry[name] = action
        logger.debug(f"注册动作: {name}")

    def create_scenario(
        self,
        name: str,
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
    ) -> Scenario:
        """快速创建测试场景

        Args:
            name: 场景名称
            description: 场景描述
            steps: 步骤定义列表
            tags: 标签列表

        Returns:
            创建的场景对象
        """
        scenario_steps: list[ScenarioStep] = []
        if steps:
            for idx, step_def in enumerate(steps):
                scenario_steps.append(
                    ScenarioStep(
                        step_id=idx + 1,
                        name=step_def.get("name", f"步骤 {idx + 1}"),
                        action=step_def.get("action", ""),
                        parameters=step_def.get("parameters", {}),
                        expected_outcome=step_def.get("expected_outcome", ""),
                        assertions=[
                            Assertion(
                                type=AssertionType(a.get("type", "equals")),
                                description=a.get("description", ""),
                                expected=a.get("expected"),
                            )
                            for a in step_def.get("assertions", [])
                        ],
                    )
                )

        scenario = Scenario(
            name=name,
            description=description,
            steps=scenario_steps,
            tags=tags or [],
        )
        self._scenarios[scenario.scenario_id] = scenario
        logger.info(f"创建场景: {scenario.scenario_id} - {name}")
        return scenario

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        """执行单个测试场景

        Args:
            scenario: 测试场景

        Returns:
            场景执行结果
        """
        result = ScenarioResult(
            scenario_id=scenario.scenario_id,
            scenario_name=scenario.name,
            total_steps=len(scenario.steps),
            started_at=datetime.now(),
        )
        result.status = ScenarioStatus.RUNNING

        logger.info(f"开始执行场景: {scenario.name} ({len(scenario.steps)} 步)")
        start_time = time.monotonic()

        for step in scenario.steps:
            step_result = await self._execute_step(step)
            result.step_results.append(step_result)

            if step_result.status == ScenarioStatus.PASSED:
                result.passed_steps += 1
            elif step_result.status == ScenarioStatus.FAILED:
                result.failed_steps += 1

            result.total_assertions += len(step_result.assertions)
            result.passed_assertions += sum(1 for a in step_result.assertions if a.passed)
            result.failed_assertions += sum(1 for a in step_result.assertions if not a.passed)

        result.duration_ms = (time.monotonic() - start_time) * 1000
        result.completed_at = datetime.now()

        # 收集边界发现
        result.boundary_findings = self._boundary_prober.get_findings()
        self._boundary_prober.clear()

        # 判定总体状态
        if result.failed_steps > 0 or result.failed_assertions > 0:
            result.status = ScenarioStatus.FAILED
        else:
            result.status = ScenarioStatus.PASSED

        self._results[result.result_id] = result
        logger.info(
            f"场景执行完成: {scenario.name}, "
            f"status={result.status.value}, "
            f"passed={result.passed_steps}/{result.total_steps}, "
            f"duration={result.duration_ms:.1f}ms"
        )
        return result

    async def _execute_step(self, step: ScenarioStep) -> ScenarioStep:
        """执行单个步骤"""
        step.status = ScenarioStatus.RUNNING
        start_time = time.monotonic()

        try:
            action = self._action_registry.get(step.action)
            if action is None:
                step.error = f"未注册的动作: {step.action}"
                step.status = ScenarioStatus.ERROR
                return step

            # 执行动作
            actual_result = action(**step.parameters)

            # 执行断言
            for assertion in step.assertions:
                if assertion.type == AssertionType.NO_EXCEPTION:
                    assertion.actual = None
                else:
                    assertion.actual = actual_result
                self._assertion_engine.evaluate(assertion)

            # 判定步骤状态
            if all(a.passed for a in step.assertions):
                step.status = ScenarioStatus.PASSED
            else:
                step.status = ScenarioStatus.FAILED

        except Exception as exc:
            step.error = f"{type(exc).__name__}: {exc}"
            step.status = ScenarioStatus.FAILED
            logger.error(f"步骤 {step.name} 执行异常: {step.error}")

            # 检查是否有 NO_EXCEPTION 断言
            for assertion in step.assertions:
                if assertion.type == AssertionType.NO_EXCEPTION:
                    assertion.actual = str(exc)
                    self._assertion_engine.evaluate(assertion)

        step.duration_ms = (time.monotonic() - start_time) * 1000
        return step

    async def run_regression(
        self,
        scenario_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> RegressionReport:
        """运行回归测试

        Args:
            scenario_ids: 指定场景 ID 列表, None 运行全部
            tags: 按标签过滤场景

        Returns:
            回归测试报告
        """
        report = RegressionReport()
        start_time = time.monotonic()

        # 筛选场景
        scenarios_to_run: list[Scenario] = []
        if scenario_ids:
            for sid in scenario_ids:
                if sid in self._scenarios:
                    scenarios_to_run.append(self._scenarios[sid])
        elif tags:
            for scenario in self._scenarios.values():
                if any(tag in scenario.tags for tag in tags):
                    scenarios_to_run.append(scenario)
        else:
            scenarios_to_run = list(self._scenarios.values())

        report.total_scenarios = len(scenarios_to_run)
        logger.info(f"开始回归测试: {report.total_scenarios} 个场景")

        for scenario in scenarios_to_run:
            result = await self.run_scenario(scenario)
            report.results.append(result)

            if result.status == ScenarioStatus.PASSED:
                report.passed += 1
            elif result.status == ScenarioStatus.FAILED:
                report.failed += 1
                report.regressions.append(f"场景 '{scenario.name}' 失败")
            elif result.status == ScenarioStatus.SKIPPED:
                report.skipped += 1
            else:
                report.errors += 1

        report.duration_ms = (time.monotonic() - start_time) * 1000

        logger.info(
            f"回归测试完成: total={report.total_scenarios}, "
            f"passed={report.passed}, failed={report.failed}, "
            f"errors={report.errors}, duration={report.duration_ms:.1f}ms"
        )
        return report

    def get_scenario(self, scenario_id: str) -> Scenario | None:
        """获取场景定义"""
        return self._scenarios.get(scenario_id)

    def get_result(self, result_id: str) -> ScenarioResult | None:
        """获取执行结果"""
        return self._results.get(result_id)

    def list_scenarios(self, tags: list[str] | None = None) -> list[Scenario]:
        """列出场景"""
        scenarios = list(self._scenarios.values())
        if tags:
            scenarios = [s for s in scenarios if any(t in s.tags for t in tags)]
        return scenarios

    def get_boundary_prober(self) -> BoundaryProber:
        """获取边界探测器"""
        return self._boundary_prober
