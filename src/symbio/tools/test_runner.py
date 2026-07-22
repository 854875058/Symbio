"""测试框架驱动器 — 挂载测试用例、沙箱化执行、解析结果并反馈 DAG 引擎。

核心职责：
1. 识别并挂载 pytest / npm test / jest 等主流测试框架
2. 沙箱化子进程执行，防止测试代码污染主进程
3. 超时控制，防止测试卡死
4. 标准化解析 stderr/stdout，输出 Pass/Fail/Error + 详细日志
5. 将执行结果封装为 NodeObservation，作为强约束反馈给 DAG 引擎

设计目标：
- Testing Agent 执行真实测试，用工程化结果取代模型主观判断
- Agent 必须调用 submit_task() 才能结束，绕过 EOS 提前停机
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

try:
    from symbio.core.dag_engine import NodeObservation
except ImportError:
    NodeObservation = None  # type: ignore[misc,assignment]

logger = get_logger("test_runner")


# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------


class TestFramework(str, Enum):
    """支持的测试框架。"""

    PYTEST = "pytest"
    NPM_TEST = "npm_test"
    JEST = "jest"
    AUTO = "auto"


class TestStatus(str, Enum):
    """单条测试用例的执行状态。"""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class TestCase(BaseModel):
    """单条测试用例定义。

    Attributes:
        case_id: 唯一标识。
        name: 用例名称。
        file_path: 用例所在的文件路径。
        framework: 测试框架类型。
        args: 额外的命令行参数。
        timeout: 该用例的超时秒数，None 表示使用全局默认值。
    """

    case_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    file_path: str = ""
    framework: TestFramework = TestFramework.AUTO
    args: list[str] = Field(default_factory=list)
    timeout: Optional[int] = None


class TestCaseResult(BaseModel):
    """单条测试用例的执行结果。

    Attributes:
        case_id: 对应的用例 ID。
        name: 用例名称。
        status: 执行状态。
        duration_ms: 执行耗时（毫秒）。
        stdout: 标准输出。
        stderr: 标准错误。
        error_message: 错误摘要。
        stack_trace: 完整的错误堆栈。
        metadata: 附加元数据。
    """

    case_id: str = ""
    name: str = ""
    status: TestStatus = TestStatus.ERROR
    duration_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    stack_trace: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TestSuite(BaseModel):
    """测试套件 — 一组测试用例的集合。

    Attributes:
        suite_id: 唯一标识。
        name: 套件名称。
        cases: 包含的测试用例列表。
        working_dir: 执行时的工作目录。
        env: 额外的环境变量。
        framework: 套件级别的框架覆盖。
    """

    suite_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    cases: list[TestCase] = Field(default_factory=list)
    working_dir: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    framework: TestFramework = TestFramework.AUTO


class TestResult(BaseModel):
    """测试套件的完整执行结果。

    Attributes:
        suite_id: 对应的套件 ID。
        suite_name: 套件名称。
        framework: 实际使用的测试框架。
        total: 总用例数。
        passed: 通过数。
        failed: 失败数。
        errored: 错误数。
        skipped: 跳过数。
        duration_ms: 总执行耗时（毫秒）。
        case_results: 每条用例的详细结果。
        raw_stdout: 框架原始标准输出。
        raw_stderr: 框架原始标准错误。
        exit_code: 子进程退出码。
        success: 整体是否通过（无 fail 且无 error）。
        metadata: 附加元数据。
    """

    suite_id: str = ""
    suite_name: str = ""
    framework: TestFramework = TestFramework.AUTO
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    case_results: list[TestCaseResult] = Field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int = -1
    success: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 框架适配器 — 解析各框架的 stdout/stderr
# ---------------------------------------------------------------------------


class FrameworkAdapter(ABC):
    """测试框架适配器抽象基类。"""

    @abstractmethod
    def build_command(
        self,
        suite: TestSuite,
        case: Optional[TestCase] = None,
    ) -> list[str]:
        """构建要传给 create_subprocess_exec 的命令列表。

        Args:
            suite: 测试套件。
            case: 单条用例（None 表示执行整个套件）。

        Returns:
            命令参数列表。
        """

    @abstractmethod
    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> list[TestCaseResult]:
        """解析框架输出，返回标准化的用例结果列表。

        Args:
            stdout: 子进程标准输出。
            stderr: 子进程标准错误。
            exit_code: 子进程退出码。

        Returns:
            解析后的 TestCaseResult 列表。
        """


class PytestAdapter(FrameworkAdapter):
    """pytest 适配器 — 使用 --tb=long -v 并解析输出。"""

    def build_command(
        self,
        suite: TestSuite,
        case: Optional[TestCase] = None,
    ) -> list[str]:
        cmd = ["python", "-m", "pytest", "--tb=long", "-v", "-q", "--no-header"]
        if case and case.file_path:
            cmd.append(case.file_path)
            if case.name:
                # pytest 允许用 :: 指定类和方法
                cmd[-1] = f"{case.file_path}::{case.name}"
        if case and case.args:
            cmd.extend(case.args)
        elif suite.cases and not case:
            # 整套件执行时，收集所有文件路径（去重）
            seen: set[str] = set()
            for c in suite.cases:
                if c.file_path and c.file_path not in seen:
                    seen.add(c.file_path)
                    cmd.append(c.file_path)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> list[TestCaseResult]:
        results: list[TestCaseResult] = []

        # pytest -v 输出格式: "file::class::method PASSED/FAILED/ERROR/SKIPPED"
        pattern = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)(?:\s+(\S+))?$",
            re.MULTILINE,
        )
        for match in pattern.finditer(stdout):
            test_id, status_str, _duration = match.group(1), match.group(2), match.group(3)
            status_map = {
                "PASSED": TestStatus.PASSED,
                "FAILED": TestStatus.FAILED,
                "ERROR": TestStatus.ERROR,
                "SKIPPED": TestStatus.SKIPPED,
            }
            status = status_map.get(status_str, TestStatus.ERROR)

            # 提取错误信息（pytest 在 FAILED/ERROR 后面紧跟 E 开头的行）
            error_msg = ""
            stack = ""
            if status in (TestStatus.FAILED, TestStatus.ERROR):
                error_msg, stack = self._extract_pytest_error(stdout, test_id)

            results.append(
                TestCaseResult(
                    name=test_id,
                    status=status,
                    error_message=error_msg,
                    stack_trace=stack,
                )
            )

        # 如果没有任何解析结果但退出码非 0，视为整体错误
        if not results and exit_code != 0:
            results.append(
                TestCaseResult(
                    name="pytest_execution",
                    status=TestStatus.ERROR,
                    error_message=self._first_error_line(stderr or stdout),
                    stack_trace=stderr,
                )
            )

        # 如果解析到但全是 passed 且退出码为 0，保持；否则根据退出码修正
        if results and exit_code != 0 and all(r.status == TestStatus.PASSED for r in results):
            results.append(
                TestCaseResult(
                    name="pytest_exit_abnormal",
                    status=TestStatus.ERROR,
                    error_message=f"pytest exited with code {exit_code}",
                    stack_trace=stderr,
                )
            )

        return results

    @staticmethod
    def _extract_pytest_error(stdout: str, test_id: str) -> tuple[str, str]:
        """从 pytest 输出中提取指定用例的错误摘要和堆栈。"""
        # 找到 test_id 所在行之后的 "E " 开头行
        lines = stdout.splitlines()
        capturing = False
        error_lines: list[str] = []
        for line in lines:
            if test_id in line and ("FAILED" in line or "ERROR" in line):
                capturing = True
                continue
            if capturing:
                # 遇到下一个测试用例则停止
                if re.match(r"^\S+::\S+\s+(PASSED|FAILED|ERROR|SKIPPED)", line):
                    break
                error_lines.append(line)

        error_msg = ""
        stack = "\n".join(error_lines).strip()
        for eline in error_lines:
            stripped = eline.strip()
            if stripped.startswith("E "):
                error_msg = stripped[2:]
                break
            if stripped.startswith("AssertionError:") or stripped.startswith("assert"):
                error_msg = stripped
                break
        if not error_msg and error_lines:
            # 取第一个非空行作为摘要
            for eline in error_lines:
                if eline.strip():
                    error_msg = eline.strip()
                    break

        return error_msg, stack

    @staticmethod
    def _first_error_line(text: str) -> str:
        """提取文本中第一个 ERROR/Error/error 行。"""
        for line in text.splitlines():
            if "error" in line.lower() or "exception" in line.lower():
                return line.strip()
        return text.strip()[:500] if text.strip() else "Unknown error"


class NpmTestAdapter(FrameworkAdapter):
    """npm test 适配器 — 解析 npm run test 的输出。"""

    def build_command(
        self,
        suite: TestSuite,
        case: Optional[TestCase] = None,
    ) -> list[str]:
        cmd = ["npm", "test", "--", "--verbose"]
        if case and case.args:
            cmd.extend(case.args)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> list[TestCaseResult]:
        results: list[TestCaseResult] = []

        # 尝试解析 JSON 输出（jest/mocha --json 模式）
        json_result = self._try_parse_json(stdout)
        if json_result is not None:
            return json_result

        # 回退：逐行模式匹配
        # 常见模式: "✓ test name" 或 "✗ test name" 或 "PASS"/"FAIL"
        pass_pattern = re.compile(r"[✓✔]\s+(.+?)(?:\s+\([\d.]+s\))?$", re.MULTILINE)
        fail_pattern = re.compile(r"[✗✘✖]\s+(.+?)(?:\s+\([\d.]+s\))?$", re.MULTILINE)

        for match in pass_pattern.finditer(stdout):
            results.append(
                TestCaseResult(
                    name=match.group(1).strip(),
                    status=TestStatus.PASSED,
                )
            )

        for match in fail_pattern.finditer(stdout):
            results.append(
                TestCaseResult(
                    name=match.group(1).strip(),
                    status=TestStatus.FAILED,
                    error_message=self._extract_npm_error(stdout, match.group(1).strip()),
                )
            )

        # 如果无解析结果但有错误
        if not results and exit_code != 0:
            results.append(
                TestCaseResult(
                    name="npm_test_execution",
                    status=TestStatus.ERROR,
                    error_message=stderr.strip()[:500]
                    if stderr.strip()
                    else f"exit code {exit_code}",
                    stack_trace=stderr,
                )
            )

        # 如果有解析结果但全是 passed，而退出码异常
        if results and exit_code != 0 and all(r.status == TestStatus.PASSED for r in results):
            results.append(
                TestCaseResult(
                    name="npm_test_exit_abnormal",
                    status=TestStatus.ERROR,
                    error_message=f"npm test exited with code {exit_code}",
                    stack_trace=stderr,
                )
            )

        return results

    @staticmethod
    def _try_parse_json(stdout: str) -> Optional[list[TestCaseResult]]:
        """尝试从输出中提取 JSON 格式的测试结果。"""
        # 查找 JSON 块（jest --json 输出被包裹在某些文本中）
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            results: list[TestCaseResult] = []
            test_results = data.get("testResults", [])
            for suite_result in test_results:
                for assertion in suite_result.get("assertionResults", []):
                    status_map = {
                        "passed": TestStatus.PASSED,
                        "failed": TestStatus.FAILED,
                        "pending": TestStatus.SKIPPED,
                    }
                    status = status_map.get(assertion.get("status", ""), TestStatus.ERROR)
                    error_msg = ""
                    stack = ""
                    if assertion.get("failureMessages"):
                        failure_text = assertion["failureMessages"][0]
                        error_msg = failure_text.splitlines()[0] if failure_text else ""
                        stack = failure_text

                    full_name = " > ".join(assertion.get("ancestorTitles", []))
                    case_name = assertion.get("title", "")
                    if full_name:
                        full_name = f"{full_name} > {case_name}"
                    else:
                        full_name = case_name

                    results.append(
                        TestCaseResult(
                            name=full_name,
                            status=status,
                            error_message=error_msg,
                            stack_trace=stack,
                        )
                    )
            if results:
                return results

        return None

    @staticmethod
    def _extract_npm_error(stdout: str, test_name: str) -> str:
        """从 npm test 输出中提取指定用例的错误信息。"""
        lines = stdout.splitlines()
        capturing = False
        error_lines: list[str] = []
        for line in lines:
            if test_name in line and ("✗" in line or "✘" in line or "✖" in line):
                capturing = True
                continue
            if capturing:
                # 遇到下一个测试或空行分隔符则停止
                if re.match(r"^\s*[✓✔✗✘✖]\s+", line) or (not line.strip() and len(error_lines) > 2):
                    break
                error_lines.append(line)

        return "\n".join(error_lines).strip()[:500]


class JestAdapter(FrameworkAdapter):
    """jest 适配器 — 直接调用 npx jest 并解析输出。"""

    def build_command(
        self,
        suite: TestSuite,
        case: Optional[TestCase] = None,
    ) -> list[str]:
        cmd = ["npx", "jest", "--verbose", "--no-coverage"]
        if case and case.file_path:
            cmd.append(case.file_path)
            if case.name:
                cmd.extend(["-t", case.name])
        if case and case.args:
            cmd.extend(case.args)
        elif suite.cases and not case:
            seen: set[str] = set()
            for c in suite.cases:
                if c.file_path and c.file_path not in seen:
                    seen.add(c.file_path)
                    cmd.append(c.file_path)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> list[TestCaseResult]:
        results: list[TestCaseResult] = []

        # jest --verbose 输出格式:
        #   ✓ test name (Xms)
        #   ✗ test name (Xms)
        pass_pattern = re.compile(r"^\s*[✓✔]\s+(.+?)\s*\((\d+)\s*ms\)", re.MULTILINE)
        fail_pattern = re.compile(r"^\s*[✗✘✖]\s+(.+?)\s*\((\d+)\s*ms\)", re.MULTILINE)

        for match in pass_pattern.finditer(stdout):
            results.append(
                TestCaseResult(
                    name=match.group(1).strip(),
                    status=TestStatus.PASSED,
                    duration_ms=float(match.group(2)),
                )
            )

        for match in fail_pattern.finditer(stdout):
            name = match.group(1).strip()
            error_msg, stack = self._extract_jest_error(stdout, name)
            results.append(
                TestCaseResult(
                    name=name,
                    status=TestStatus.FAILED,
                    duration_ms=float(match.group(2)),
                    error_message=error_msg,
                    stack_trace=stack,
                )
            )

        # 无解析结果且非零退出码
        if not results and exit_code != 0:
            results.append(
                TestCaseResult(
                    name="jest_execution",
                    status=TestStatus.ERROR,
                    error_message=self._first_error_line(stderr or stdout),
                    stack_trace=stderr,
                )
            )

        return results

    @staticmethod
    def _extract_jest_error(stdout: str, test_name: str) -> tuple[str, str]:
        """从 jest 输出中提取失败用例的错误信息。"""
        lines = stdout.splitlines()
        capturing = False
        error_lines: list[str] = []
        for line in lines:
            if test_name in line and ("✗" in line or "✘" in line or "✖" in line):
                capturing = True
                continue
            if capturing:
                if re.match(r"^\s*[✓✔✗✘✖]\s+", line):
                    break
                if re.match(r"^\s*(PASS|FAIL)\s+", line):
                    break
                error_lines.append(line)

        error_msg = ""
        stack = "\n".join(error_lines).strip()
        for eline in error_lines:
            stripped = eline.strip()
            if stripped.startswith("expect(") or "Expected:" in stripped or "Received:" in stripped:
                error_msg = stripped
                break
        if not error_msg and error_lines:
            for eline in error_lines:
                if eline.strip():
                    error_msg = eline.strip()
                    break

        return error_msg, stack

    @staticmethod
    def _first_error_line(text: str) -> str:
        for line in text.splitlines():
            if "error" in line.lower() or "fail" in line.lower():
                return line.strip()
        return text.strip()[:500] if text.strip() else "Unknown error"


# ---------------------------------------------------------------------------
# 框架适配器注册表
# ---------------------------------------------------------------------------

_ADAPTERS: dict[TestFramework, FrameworkAdapter] = {
    TestFramework.PYTEST: PytestAdapter(),
    TestFramework.NPM_TEST: NpmTestAdapter(),
    TestFramework.JEST: JestAdapter(),
}


def _detect_framework(working_dir: str | Path) -> TestFramework:
    """根据工作目录下的文件自动检测测试框架。

    检测优先级：
    1. pytest.ini / setup.cfg [tool:pytest] / pyproject.toml [tool.pytest] -> pytest
    2. jest.config.* / package.json 中有 jest -> jest
    3. package.json 中有 scripts.test -> npm_test
    4. 默认 pytest
    """
    wdir = Path(working_dir)

    # pytest 检测
    if (wdir / "pytest.ini").exists():
        return TestFramework.PYTEST
    if (wdir / "pyproject.toml").exists():
        content = (wdir / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
        if "pytest" in content:
            return TestFramework.PYTEST
    if (wdir / "setup.cfg").exists():
        content = (wdir / "setup.cfg").read_text(encoding="utf-8", errors="ignore")
        if "[tool:pytest]" in content:
            return TestFramework.PYTEST

    # jest 检测
    for jest_cfg in ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs"):
        if (wdir / jest_cfg).exists():
            return TestFramework.JEST
    pkg_json = wdir / "package.json"
    if pkg_json.exists():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            if "jest" in pkg_data:
                return TestFramework.JEST
            scripts = pkg_data.get("scripts", {})
            if "test" in scripts:
                return TestFramework.NPM_TEST
        except (json.JSONDecodeError, OSError):
            pass

    return TestFramework.PYTEST


# ---------------------------------------------------------------------------
# 沙箱环境构建器
# ---------------------------------------------------------------------------


def _build_sandbox_env(
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """构建沙箱化的环境变量。

    策略：
    - 继承当前进程的 PATH、HOME、SystemRoot 等必要变量
    - 设置 PYTHONDONTWRITEBYTECODE=1 防止生成 __pycache__
    - 设置 NODE_ENV=test 以标识测试环境
    - 可叠加额外的环境变量
    """
    import os

    # 只继承必要的环境变量，最小化信息泄漏
    safe_keys = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LANGUAGE",
        "SHELL",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "NODE_PATH",
    }
    env: dict[str, str] = {}
    for key in safe_keys:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # 沙箱标志
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NODE_ENV"] = "test"
    env["SYMBIO_SANDBOX"] = "1"

    # 叠加自定义环境变量
    if extra_env:
        env.update(extra_env)

    return env


# ---------------------------------------------------------------------------
# 测试执行器
# ---------------------------------------------------------------------------


class TestRunner:
    """测试框架驱动器 — 沙箱化执行测试并解析结果。

    使用方式:
        runner = TestRunner()
        suite = TestSuite(
            name="unit_tests",
            cases=[TestCase(file_path="tests/test_api.py")],
            working_dir="/path/to/project",
        )
        result = await runner.run(suite)

        # 作为 DAG 节点使用:
        node_observation = await runner.run_as_node(suite, context={})
    """

    DEFAULT_TIMEOUT = 300  # 默认 5 分钟超时
    MAX_OUTPUT_SIZE = 1024 * 1024  # 最大输出 1MB

    def __init__(self, default_timeout: int = DEFAULT_TIMEOUT) -> None:
        self._default_timeout = default_timeout
        logger.info(f"TestRunner initialized (default_timeout={default_timeout}s)")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run(self, suite: TestSuite) -> TestResult:
        """执行测试套件并返回标准化结果。

        流程：
        1. 自动检测或使用指定的测试框架
        2. 构建沙箱化环境变量
        3. 异步启动子进程执行测试命令
        4. 超时控制 — 超时则 kill 子进程
        5. 捕获 stdout/stderr
        6. 调用框架适配器解析结果
        7. 组装 TestResult 返回

        Args:
            suite: 测试套件定义。

        Returns:
            标准化的测试结果。
        """
        # 确定框架
        framework = suite.framework
        if framework == TestFramework.AUTO:
            framework = _detect_framework(suite.working_dir)
            logger.info(f"Auto-detected framework: {framework.value}")

        adapter = _ADAPTERS.get(framework)
        if adapter is None:
            logger.error(f"No adapter for framework: {framework.value}")
            return TestResult(
                suite_id=suite.suite_id,
                suite_name=suite.name,
                framework=framework,
                total=0,
                success=False,
                raw_stderr=f"No adapter registered for framework: {framework.value}",
                exit_code=-1,
            )

        # 沙箱化环境
        env = _build_sandbox_env(suite.env)

        # 确定工作目录（必须是绝对路径且存在）
        work_dir = Path(suite.working_dir).resolve()
        if not work_dir.exists():
            logger.error(f"Working directory does not exist: {work_dir}")
            return TestResult(
                suite_id=suite.suite_id,
                suite_name=suite.name,
                framework=framework,
                total=0,
                success=False,
                raw_stderr=f"Working directory does not exist: {work_dir}",
                exit_code=-1,
            )

        # 如果套件内没有用例，则执行整个目录
        if not suite.cases:
            logger.info("No specific cases provided; running entire test suite")
            cmd = adapter.build_command(suite, case=None)
            raw_stdout, raw_stderr, exit_code, duration_ms = await self._execute_subprocess(
                cmd, work_dir, env, self._default_timeout
            )
            case_results = adapter.parse_output(raw_stdout, raw_stderr, exit_code)
            return self._build_result(
                suite, framework, case_results, raw_stdout, raw_stderr, exit_code, duration_ms
            )

        # 逐用例执行（每个用例一个子进程，确保沙箱隔离）
        all_case_results: list[TestCaseResult] = []
        total_duration = 0.0
        combined_stdout_parts: list[str] = []
        combined_stderr_parts: list[str] = []
        last_exit_code = 0

        for case in suite.cases:
            timeout = case.timeout or self._default_timeout
            cmd = adapter.build_command(suite, case=case)
            logger.info(f"Running case '{case.name}' with command: {' '.join(cmd)}")

            raw_stdout, raw_stderr, exit_code, duration_ms = await self._execute_subprocess(
                cmd, work_dir, env, timeout
            )

            total_duration += duration_ms
            combined_stdout_parts.append(f"--- Case: {case.name} ---\n{raw_stdout}")
            combined_stderr_parts.append(f"--- Case: {case.name} ---\n{raw_stderr}")
            last_exit_code = exit_code

            case_results = adapter.parse_output(raw_stdout, raw_stderr, exit_code)

            # 将 case_id 回填到解析结果
            for cr in case_results:
                cr.case_id = case.case_id

            all_case_results.extend(case_results)

        return self._build_result(
            suite,
            framework,
            all_case_results,
            "\n".join(combined_stdout_parts),
            "\n".join(combined_stderr_parts),
            last_exit_code,
            total_duration,
        )

    async def run_as_node(
        self,
        suite: TestSuite,
        context: Optional[dict[str, Any]] = None,
    ) -> NodeObservation:
        """执行测试套件并封装为 DAG 引擎的 NodeObservation。

        此方法可直接作为 DAG 节点的 callable_ref 使用：
            node_callable = lambda ctx: runner.run_as_node(suite, ctx)

        逻辑：
        - 测试全部通过 -> expected=True, output=TestResult
        - 测试存在 fail/error -> expected=False, output=TestResult
          （触发 DAG 引擎拓扑重构，要求 Agent 修复后重新提交）

        Args:
            suite: 测试套件定义。
            context: DAG 执行上下文（可选）。

        Returns:
            NodeObservation 实例，可直接喂给 DAG 引擎。
        """
        result = await self.run(suite)

        metadata: dict[str, Any] = {
            "framework": result.framework.value,
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errored": result.errored,
            "skipped": result.skipped,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
        }

        # 收集失败/错误用例摘要
        failure_summary: list[str] = []
        for cr in result.case_results:
            if cr.status in (TestStatus.FAILED, TestStatus.ERROR):
                entry = f"[{cr.status.value}] {cr.name}"
                if cr.error_message:
                    entry += f": {cr.error_message}"
                failure_summary.append(entry)

        if failure_summary:
            metadata["failure_summary"] = failure_summary

        observation = NodeObservation(
            node_id=suite.suite_id,
            output=result.model_dump(),
            expected=result.success,
            metadata=metadata,
        )

        if result.success:
            logger.info(
                f"Test suite '{suite.name}' PASSED "
                f"({result.passed}/{result.total} in {result.duration_ms:.0f}ms)"
            )
        else:
            logger.warning(
                f"Test suite '{suite.name}' FAILED "
                f"(passed={result.passed}, failed={result.failed}, errored={result.errored}) — "
                f"triggering DAG reconfiguration"
            )

        return observation

    # ------------------------------------------------------------------
    # 便捷工厂方法
    # ------------------------------------------------------------------

    @staticmethod
    def create_suite(
        name: str,
        file_paths: list[str],
        working_dir: str = ".",
        framework: TestFramework = TestFramework.AUTO,
        env: Optional[dict[str, str]] = None,
    ) -> TestSuite:
        """快速创建测试套件。

        Args:
            name: 套件名称。
            file_paths: 测试文件路径列表。
            working_dir: 工作目录。
            framework: 测试框架。
            env: 额外环境变量。

        Returns:
            TestSuite 实例。
        """
        cases = [TestCase(file_path=fp, name=fp) for fp in file_paths]
        return TestSuite(
            name=name,
            cases=cases,
            working_dir=working_dir,
            framework=framework,
            env=env or {},
        )

    @staticmethod
    def create_dag_callable(
        suite: TestSuite,
        default_timeout: int = DEFAULT_TIMEOUT,
    ):
        """创建可直接挂载到 DAG 引擎的 callable。

        用法：
            from symbio.tools.test_runner import TestRunner, TestSuite

            suite = TestRunner.create_suite("my_tests", ["tests/test_*.py"], "/project")
            dag.add_node(
                name="run_tests",
                func=TestRunner.create_dag_callable(suite),
                dependencies=["code_generation_node_id"],
            )

        Args:
            suite: 测试套件。
            default_timeout: 默认超时。

        Returns:
            异步 callable，签名符合 DAG NodeCallable。
        """
        runner = TestRunner(default_timeout=default_timeout)

        async def _callable(context: dict[str, Any]) -> NodeObservation:
            return await runner.run_as_node(suite, context)

        return _callable

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _execute_subprocess(
        self,
        cmd: list[str],
        work_dir: Path,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[str, str, int, float]:
        """沙箱化执行子进程。

        使用 asyncio.create_subprocess_exec 启动独立子进程，
        与主进程完全隔离（不继承主进程的全局状态）。

        Args:
            cmd: 命令参数列表。
            work_dir: 工作目录。
            env: 环境变量。
            timeout: 超时秒数。

        Returns:
            (stdout, stderr, exit_code, duration_ms) 元组。
        """
        cmd_str = " ".join(cmd)
        logger.info(f"Executing subprocess: {cmd_str}")
        logger.debug(f"Working dir: {work_dir}, timeout: {timeout}s")

        start_time = datetime.now()
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=env,
            )

            logger.debug(f"Subprocess started (pid={process.pid})")

            # 带超时的等待
            try:
                raw_stdout_bytes, raw_stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"Subprocess timed out after {timeout}s (pid={process.pid})")
                # 超时 — 强制终止
                await self._kill_process(process)
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                return (
                    "",
                    f"TIMEOUT: Test execution exceeded {timeout}s limit",
                    -1,
                    elapsed,
                )

            # 截断过大的输出
            stdout_text = raw_stdout_bytes.decode("utf-8", errors="replace")[: self.MAX_OUTPUT_SIZE]
            stderr_text = raw_stderr_bytes.decode("utf-8", errors="replace")[: self.MAX_OUTPUT_SIZE]
            exit_code = process.returncode or 0
            elapsed = (datetime.now() - start_time).total_seconds() * 1000

            logger.info(
                f"Subprocess finished: pid={process.pid}, exit_code={exit_code}, elapsed={elapsed:.0f}ms"
            )

            if stdout_text.strip():
                logger.debug(f"stdout ({len(stdout_text)} chars): {stdout_text[:200]}...")
            if stderr_text.strip():
                logger.debug(f"stderr ({len(stderr_text)} chars): {stderr_text[:200]}...")

            return stdout_text, stderr_text, exit_code, elapsed

        except FileNotFoundError:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Command not found: {cmd[0]}"
            logger.error(error_msg)
            return ("", error_msg, -1, elapsed)

        except PermissionError:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Permission denied executing: {cmd_str}"
            logger.error(error_msg)
            return ("", error_msg, -1, elapsed)

        except OSError as exc:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"OS error executing subprocess: {exc}"
            logger.error(error_msg)
            return ("", error_msg, -1, elapsed)

        except Exception as exc:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Unexpected error executing subprocess: {exc}"
            logger.error(error_msg)
            return ("", error_msg, -1, elapsed)

        finally:
            # 确保进程资源被释放
            if process is not None and process.returncode is None:
                await self._kill_process(process)

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """安全终止子进程。

        先尝试 terminate（SIGTERM），给进程机会优雅退出；
        如果 3 秒后仍未退出，强制 kill（SIGKILL）。
        """
        try:
            logger.warning(f"Terminating subprocess (pid={process.pid})")
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
                logger.info(f"Subprocess terminated gracefully (pid={process.pid})")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Subprocess did not terminate gracefully, killing (pid={process.pid})"
                )
                process.kill()
                await process.wait()
        except ProcessLookupError:
            # 进程已经退出
            pass
        except Exception as exc:
            logger.error(f"Error killing subprocess (pid={process.pid}): {exc}")

    @staticmethod
    def _build_result(
        suite: TestSuite,
        framework: TestFramework,
        case_results: list[TestCaseResult],
        raw_stdout: str,
        raw_stderr: str,
        exit_code: int,
        duration_ms: float,
    ) -> TestResult:
        """组装标准化的 TestResult。"""
        passed = sum(1 for r in case_results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in case_results if r.status == TestStatus.FAILED)
        errored = sum(1 for r in case_results if r.status == TestStatus.ERROR)
        skipped = sum(1 for r in case_results if r.status == TestStatus.SKIPPED)
        total = len(case_results)
        success = failed == 0 and errored == 0 and total > 0

        return TestResult(
            suite_id=suite.suite_id,
            suite_name=suite.name,
            framework=framework,
            total=total,
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            duration_ms=duration_ms,
            case_results=case_results,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            success=success,
        )
