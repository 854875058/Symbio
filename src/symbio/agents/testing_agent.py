"""测试验证 Agent - 执行测试验证任务结果

核心职责：
1. 从任务 Checklist 中提取测试命令
2. 沙箱化执行测试命令，收集 stdout/stderr
3. 验证产出文件存在且非空
4. 返回结构化的验证结果
5. 提供 TestDrivenLoop 实现测试失败重试机制

设计目标：
- 作为防过早宣布完成的第三层保障
- 用工程化验证取代 Agent 主观判断
- 通过重试循环自动修复常见失败场景
"""

from __future__ import annotations

import asyncio
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from pydantic import BaseModel, Field

from symbio.agents.base import BaseAgent
from symbio.agents.checklist import ChecklistItem, ChecklistItemStatus, TaskChecklist
from symbio.utils.logger import get_logger
from symbio.utils.types import Result, Task

logger = get_logger("testing_agent")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class FailureClassification(str, Enum):
    """失败分类"""

    SYNTAX_ERROR = "syntax_error"
    RUNTIME_ERROR = "runtime_error"
    TEST_FAILURE = "test_failure"
    TIMEOUT = "timeout"


class FailureAnalysis(BaseModel):
    """失败分析结果"""

    classification: FailureClassification
    error_message: str = ""
    line_number: Optional[int] = None
    fix_suggestion: str = ""


class RetryRecord(BaseModel):
    """单次重试记录"""

    attempt: int
    started_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    finished_at: str = ""
    passed: bool = False
    failure_analysis: Optional[FailureAnalysis] = None
    test_result: Optional[dict[str, Any]] = None


class RetryHistory(BaseModel):
    """重试历史"""

    item_name: str
    max_retries: int
    records: list[RetryRecord] = Field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        """已尝试次数"""
        return len(self.records)

    @property
    def last_failure(self) -> Optional[FailureAnalysis]:
        """最近一次失败分析"""
        for rec in reversed(self.records):
            if rec.failure_analysis:
                return rec.failure_analysis
        return None

    @property
    def all_failure_classifications(self) -> list[FailureClassification]:
        """所有失败分类"""
        return [rec.failure_analysis.classification for rec in self.records if rec.failure_analysis]


class TestRunResult(BaseModel):
    """单条测试命令的执行结果"""

    command: str
    passed: bool = False
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_ms: float = 0.0


class FileVerificationResult(BaseModel):
    """文件验证结果"""

    all_exist: bool = True
    all_non_empty: bool = True
    files: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 测试驱动重试循环
# ---------------------------------------------------------------------------

# 用于匹配语法错误中的行号
_SYNTAX_ERROR_PATTERN = re.compile(
    r"(?:line|行)\s*(\d+)",
    re.IGNORECASE,
)
_RUNTIME_ERROR_PATTERN = re.compile(
    r"(?:line|行)\s*(\d+)",
    re.IGNORECASE,
)


class TestDrivenLoop:
    """测试驱动重试循环

    执行流程：执行任务 -> 运行测试 -> 失败则分析原因 -> 重新执行 -> 重复
    每次重试后更新 checklist 条目状态，达到最大重试次数后标记为 FAILED。

    使用方式：
        loop = TestDrivenLoop(testing_agent)
        result = await loop.run_until_pass(task, checklist, item, task_executor)
    """

    def __init__(self, testing_agent: "TestingAgent") -> None:
        self._agent = testing_agent

    async def run_until_pass(
        self,
        task: Task,
        checklist: TaskChecklist,
        item: ChecklistItem,
        task_executor: Callable[[FailureAnalysis | None], Coroutine[Any, Any, bool]],
        max_retries: int = 3,
    ) -> tuple[bool, RetryHistory]:
        """执行任务直到测试通过或达到最大重试次数

        Args:
            task: 任务对象
            checklist: 任务清单（用于更新条目状态）
            item: 当前清单条目
            task_executor: 异步执行函数，接受可选的 FailureAnalysis 参数
                           返回 True 表示执行成功（不代表测试通过），False 表示执行异常
            max_retries: 最大重试次数（不含首次执行）

        Returns:
            (success, retry_history) 元组
        """
        history = RetryHistory(item_name=item.name, max_retries=max_retries)
        total_attempts = 1 + max_retries  # 首次 + 重试

        for attempt in range(1, total_attempts + 1):
            logger.info(f"TestDrivenLoop: 条目 '{item.name}' 第 {attempt}/{total_attempts} 次尝试")

            record = RetryRecord(attempt=attempt)
            failure_hint: FailureAnalysis | None = history.last_failure

            # 更新清单状态：第一次为 IN_PROGRESS，后续重试保持 IN_PROGRESS
            item.status = ChecklistItemStatus.IN_PROGRESS
            item.result = f"第 {attempt} 次尝试中..."

            try:
                # 1. 执行任务（重试时传入上次失败分析）
                exec_ok = await task_executor(failure_hint)

                if not exec_ok:
                    record.failure_analysis = FailureAnalysis(
                        classification=FailureClassification.RUNTIME_ERROR,
                        error_message="任务执行函数返回失败",
                        fix_suggestion="检查任务执行逻辑是否正确",
                    )
                    record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    history.records.append(record)
                    logger.warning(
                        f"TestDrivenLoop: 条目 '{item.name}' 第 {attempt} 次执行函数失败"
                    )

                    if attempt == total_attempts:
                        break
                    continue

                # 2. 运行测试
                if not item.test:
                    # 无测试命令，仅验证文件
                    file_result = await self._agent.verify_files(item.files)
                    if file_result.all_exist and file_result.all_non_empty:
                        record.passed = True
                        record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                        history.records.append(record)
                        logger.info(f"TestDrivenLoop: 条目 '{item.name}' 文件验证通过 (无测试命令)")
                        break
                    else:
                        missing = [f["path"] for f in file_result.files if not f["exists"]]
                        empty = [
                            f["path"] for f in file_result.files if f["exists"] and f["size"] == 0
                        ]
                        err_parts: list[str] = []
                        if missing:
                            err_parts.append(f"文件不存在: {', '.join(missing)}")
                        if empty:
                            err_parts.append(f"文件为空: {', '.join(empty)}")
                        record.failure_analysis = FailureAnalysis(
                            classification=FailureClassification.TEST_FAILURE,
                            error_message="; ".join(err_parts),
                            fix_suggestion="确保所有预期文件已创建且非空",
                        )
                else:
                    test_result = await self._agent.run_tests(item.test)
                    record.test_result = test_result.model_dump()

                    if test_result.passed:
                        # 测试通过，再验证文件
                        file_result = await self._agent.verify_files(item.files)
                        if file_result.all_exist and file_result.all_non_empty:
                            record.passed = True
                            record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                            history.records.append(record)
                            logger.info(
                                f"TestDrivenLoop: 条目 '{item.name}' 第 {attempt} 次测试通过"
                            )
                            break
                        else:
                            record.failure_analysis = FailureAnalysis(
                                classification=FailureClassification.TEST_FAILURE,
                                error_message="测试通过但文件验证失败",
                                fix_suggestion="确保所有预期文件已创建且非空",
                            )
                    else:
                        record.failure_analysis = self._analyze_failure(test_result)

            except asyncio.TimeoutError:
                record.failure_analysis = FailureAnalysis(
                    classification=FailureClassification.TIMEOUT,
                    error_message=f"执行超时 (尝试 {attempt})",
                    fix_suggestion="检查是否存在死循环或阻塞操作，考虑增加超时时间",
                )
            except Exception as exc:
                record.failure_analysis = FailureAnalysis(
                    classification=FailureClassification.RUNTIME_ERROR,
                    error_message=str(exc),
                    fix_suggestion="检查异常类型和堆栈信息，修复代码中的错误",
                )

            record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            history.records.append(record)

            if record.passed:
                # 已在循环内 break，此处为安全兜底
                break

            # 未通过，记录诊断信息
            analysis = record.failure_analysis
            if analysis:
                logger.warning(
                    f"TestDrivenLoop: 条目 '{item.name}' 第 {attempt} 次失败 "
                    f"[{analysis.classification.value}] {analysis.error_message[:120]}"
                )

            # 最后一次尝试失败后不再继续
            if attempt == total_attempts:
                break

        # --- 循环结束，更新清单状态 ---
        final_record = history.records[-1] if history.records else None
        if final_record and final_record.passed:
            item.status = ChecklistItemStatus.COMPLETED
            item.result = f"测试通过 (第 {final_record.attempt} 次尝试)"
            logger.info(
                f"TestDrivenLoop: 条目 '{item.name}' 最终通过 (共 {history.attempt_count} 次尝试)"
            )
            return True, history
        else:
            item.status = ChecklistItemStatus.FAILED
            diagnostics = self._build_diagnostics(history)
            item.result = f"达到最大重试次数 ({max_retries})，验证未通过。{diagnostics}"
            logger.error(
                f"TestDrivenLoop: 条目 '{item.name}' 最终失败 "
                f"(共 {history.attempt_count} 次尝试): {diagnostics}"
            )
            return False, history

    def _analyze_failure(self, test_result: TestRunResult) -> FailureAnalysis:
        """分析测试失败原因

        根据 stderr/stdout 内容和 exit_code 对失败进行分类，
        提取错误消息和行号，生成修复建议。

        Args:
            test_result: 测试执行结果

        Returns:
            FailureAnalysis 实例
        """
        error_text = (test_result.stderr + "\n" + test_result.stdout).strip()
        exit_code = test_result.exit_code

        # 1. 检测超时
        if "TIMEOUT" in error_text.upper() or exit_code == -1:
            # exit_code=-1 且包含 TIMEOUT 关键字
            if "TIMEOUT" in error_text.upper():
                return FailureAnalysis(
                    classification=FailureClassification.TIMEOUT,
                    error_message=self._extract_error_message(error_text),
                    fix_suggestion="检查是否存在死循环或长时间阻塞操作",
                )

        # 2. 检测语法错误
        syntax_indicators = [
            "SyntaxError",
            "SyntaxWarning",
            "IndentationError",
            "TabError",
            "unexpected EOF",
            "invalid syntax",
            "语法错误",
        ]
        for indicator in syntax_indicators:
            if indicator in error_text:
                line_num = self._extract_line_number(error_text)
                return FailureAnalysis(
                    classification=FailureClassification.SYNTAX_ERROR,
                    error_message=self._extract_error_message(error_text),
                    line_number=line_num,
                    fix_suggestion=f"检查第 {line_num} 行附近的语法"
                    if line_num
                    else "检查代码语法",
                )

        # 3. 检测运行时错误
        runtime_indicators = [
            "Traceback",
            "Error:",
            "Exception:",
            "TypeError",
            "ValueError",
            "AttributeError",
            "NameError",
            "KeyError",
            "IndexError",
            "ImportError",
            "ModuleNotFoundError",
            "FileNotFoundError",
            "RuntimeError",
            "ZeroDivisionError",
            "AssertionError",
        ]
        for indicator in runtime_indicators:
            if indicator in error_text:
                line_num = self._extract_line_number(error_text)
                return FailureAnalysis(
                    classification=FailureClassification.RUNTIME_ERROR,
                    error_message=self._extract_error_message(error_text),
                    line_number=line_num,
                    fix_suggestion=(
                        f"检查第 {line_num} 行: {indicator}"
                        if line_num
                        else f"修复 {indicator} 类型的错误"
                    ),
                )

        # 4. 默认归类为测试失败
        return FailureAnalysis(
            classification=FailureClassification.TEST_FAILURE,
            error_message=self._extract_error_message(error_text),
            fix_suggestion="检查测试断言和预期输出是否匹配",
        )

    @staticmethod
    def _extract_line_number(error_text: str) -> Optional[int]:
        """从错误文本中提取行号"""
        match = _SYNTAX_ERROR_PATTERN.search(error_text)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                pass
        return None

    @staticmethod
    def _extract_error_message(error_text: str, max_length: int = 500) -> str:
        """提取错误消息（取最后一行非空行，截断至 max_length）"""
        lines = [line.strip() for line in error_text.splitlines() if line.strip()]
        if not lines:
            return "(无错误输出)"
        # 取最后一行作为错误摘要
        message = lines[-1]
        if len(message) > max_length:
            message = message[:max_length] + "..."
        return message

    @staticmethod
    def _build_diagnostics(history: RetryHistory) -> str:
        """构建诊断摘要"""
        parts: list[str] = []
        for rec in history.records:
            analysis = rec.failure_analysis
            if analysis:
                parts.append(
                    f"第{rec.attempt}次: [{analysis.classification.value}] "
                    f"{analysis.error_message[:100]}"
                )
        if not parts:
            return "无详细诊断信息"
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# 测试验证 Agent
# ---------------------------------------------------------------------------


class TestingAgent(BaseAgent):
    """测试验证 Agent - 执行测试验证任务是否真正完成

    使用方式：
        agent = TestingAgent()
        result = await agent.execute(task)
    """

    name = "testing"
    description = "执行测试验证任务结果"
    version = "1.0.0"

    def __init__(self, default_timeout: int = 120) -> None:
        super().__init__()
        self._default_timeout = default_timeout

    async def execute(self, task: Task) -> Result:
        """执行测试验证

        流程：
        1. 从 task.metadata 中提取 checklist
        2. 对每个已完成条目的 test 字段，执行测试命令
        3. 验证所有产出文件存在且非空
        4. 汇总结果返回

        Args:
            task: 任务对象，需在 metadata 中包含 "checklist" 键

        Returns:
            包含验证详情的 Result
        """
        self.start(task)
        start_time = time.monotonic()

        checklist_data = task.metadata.get("checklist")
        if checklist_data is None:
            self.fail("任务 metadata 中未找到 checklist")
            return Result(
                task_id=task.task_id,
                success=False,
                content="验证失败：任务 metadata 中未找到 checklist",
            )

        # 反序列化 checklist
        if isinstance(checklist_data, dict):
            checklist = TaskChecklist(**checklist_data)
        elif isinstance(checklist_data, TaskChecklist):
            checklist = checklist_data
        else:
            self.fail("checklist 类型无法识别")
            return Result(
                task_id=task.task_id,
                success=False,
                content="验证失败：checklist 类型无法识别",
            )

        test_results: list[TestRunResult] = []
        all_files: list[str] = []
        issues: list[str] = []

        # 1. 对已完成条目执行测试命令
        for item in checklist.completed_items:
            all_files.extend(item.files)

            if not item.test:
                continue

            logger.info(f"执行测试命令: {item.test} (条目: {item.name})")
            run_result = await self.run_tests(item.test)
            test_results.append(run_result)

            if not run_result.passed:
                issues.append(
                    f"条目 '{item.name}' 测试失败 (exit_code={run_result.exit_code}): "
                    f"{run_result.stderr[:200] if run_result.stderr else run_result.stdout[:200]}"
                )

        # 2. 验证所有产出文件
        file_result = await self.verify_files(all_files)

        if not file_result.all_exist:
            missing = [f["path"] for f in file_result.files if not f["exists"]]
            issues.append(f"以下文件不存在: {', '.join(missing)}")

        if not file_result.all_non_empty:
            empty = [f["path"] for f in file_result.files if f["exists"] and f["size"] == 0]
            issues.append(f"以下文件为空: {', '.join(empty)}")

        # 3. 汇总
        all_tests_passed = all(r.passed for r in test_results) if test_results else True
        success = all_tests_passed and file_result.all_exist and file_result.all_non_empty

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # 构建摘要
        summary_parts: list[str] = []
        if test_results:
            passed_count = sum(1 for r in test_results if r.passed)
            summary_parts.append(f"测试: {passed_count}/{len(test_results)} 通过")
        if all_files:
            existing = sum(1 for f in file_result.files if f["exists"] and f["size"] > 0)
            summary_parts.append(f"文件: {existing}/{len(all_files)} 有效")

        if success:
            summary = "验证通过" + (f" ({'; '.join(summary_parts)})" if summary_parts else "")
        else:
            summary = "验证未通过: " + "; ".join(issues)

        if success:
            self.complete(Result(task_id=task.task_id, success=True))
        else:
            self.fail(summary)

        logger.info(f"测试验证完成: task={task.task_id}, success={success}, {summary}")

        return Result(
            task_id=task.task_id,
            success=success,
            content=summary,
            data={
                "test_results": [r.model_dump() for r in test_results],
                "file_verification": file_result.model_dump(),
                "issues": issues,
            },
            duration_ms=duration_ms,
        )

    async def run_tests(self, test_command: str, working_dir: str = ".") -> TestRunResult:
        """运行测试命令

        使用 asyncio.create_subprocess_exec 沙箱化执行，
        与 test_runner.py 保持一致的子进程管理模式。

        Args:
            test_command: 要执行的测试命令（如 "python -m pytest tests/"）
            working_dir: 工作目录

        Returns:
            TestRunResult 实例
        """
        # 将命令字符串拆分为参数列表
        cmd_parts = test_command.split()
        if not cmd_parts:
            return TestRunResult(command=test_command, passed=False, stderr="空命令")

        work_dir = Path(working_dir).resolve()
        if not work_dir.exists():
            return TestRunResult(
                command=test_command,
                passed=False,
                stderr=f"工作目录不存在: {work_dir}",
            )

        start_time = time.monotonic()
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
            )

            try:
                raw_stdout, raw_stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._default_timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"测试命令超时 ({self._default_timeout}s): {test_command}")
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

                elapsed = (time.monotonic() - start_time) * 1000
                return TestRunResult(
                    command=test_command,
                    passed=False,
                    stderr=f"TIMEOUT: 命令执行超过 {self._default_timeout}s",
                    exit_code=-1,
                    duration_ms=elapsed,
                )

            stdout_text = raw_stdout.decode("utf-8", errors="replace").strip()
            stderr_text = raw_stderr.decode("utf-8", errors="replace").strip()
            exit_code = process.returncode or 0
            elapsed = (time.monotonic() - start_time) * 1000

            passed = exit_code == 0

            if passed:
                logger.info(f"测试通过: {test_command} ({elapsed:.0f}ms)")
            else:
                logger.warning(f"测试失败: {test_command} (exit_code={exit_code}, {elapsed:.0f}ms)")

            return TestRunResult(
                command=test_command,
                passed=passed,
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                duration_ms=elapsed,
            )

        except FileNotFoundError:
            elapsed = (time.monotonic() - start_time) * 1000
            return TestRunResult(
                command=test_command,
                passed=False,
                stderr=f"命令未找到: {cmd_parts[0]}",
                exit_code=-1,
                duration_ms=elapsed,
            )

        except PermissionError:
            elapsed = (time.monotonic() - start_time) * 1000
            return TestRunResult(
                command=test_command,
                passed=False,
                stderr=f"权限不足: {test_command}",
                exit_code=-1,
                duration_ms=elapsed,
            )

        except Exception as exc:
            elapsed = (time.monotonic() - start_time) * 1000
            return TestRunResult(
                command=test_command,
                passed=False,
                stderr=f"执行异常: {exc}",
                exit_code=-1,
                duration_ms=elapsed,
            )

    async def verify_files(self, file_paths: list[str]) -> FileVerificationResult:
        """验证文件存在且非空

        Args:
            file_paths: 待验证的文件路径列表

        Returns:
            FileVerificationResult 实例
        """
        if not file_paths:
            return FileVerificationResult(all_exist=True, all_non_empty=True, files=[])

        files_info: list[dict[str, Any]] = []
        all_exist = True
        all_non_empty = True

        for fp in file_paths:
            p = Path(fp)
            exists = p.exists()
            size = p.stat().st_size if exists else 0

            files_info.append(
                {
                    "path": fp,
                    "exists": exists,
                    "size": size,
                }
            )

            if not exists:
                all_exist = False
            if exists and size == 0:
                all_non_empty = False

        return FileVerificationResult(
            all_exist=all_exist,
            all_non_empty=all_non_empty,
            files=files_info,
        )
