"""测试验证 Agent - 执行测试验证任务结果

核心职责：
1. 从任务 Checklist 中提取测试命令
2. 沙箱化执行测试命令，收集 stdout/stderr
3. 验证产出文件存在且非空
4. 返回结构化的验证结果

设计目标：
- 作为防过早宣布完成的第三层保障
- 用工程化验证取代 Agent 主观判断
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.agents.base import BaseAgent
from symbio.agents.checklist import ChecklistItemStatus, TaskChecklist
from symbio.utils.logger import get_logger
from symbio.utils.types import AgentState, Intent, Result, Task

logger = get_logger("testing_agent")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


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

    async def run_tests(
        self, test_command: str, working_dir: str = "."
    ) -> TestRunResult:
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
                logger.warning(
                    f"测试失败: {test_command} (exit_code={exit_code}, {elapsed:.0f}ms)"
                )

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
