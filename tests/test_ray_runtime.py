"""真 Ray Actor 分布式执行运行时测试。

真起本地 Ray 集群，验证：
  1. 任务在**多个不同 PID 的 worker 进程**上执行（真多进程，非 asyncio 伪并发）
  2. worker 内按 name 重建 registry 并执行 Agent，结果正确收集
  3. worker 异常被隔离、回传为失败结果
  4. cancel / shutdown / available 语义
  5. SubAgentManager 注入 RayExecutor 后端到端跑通
  6. 未注入 executor 时保持 asyncio 行为（回退）

Ray init 较慢（数秒~数十秒），整体标 slow，默认跳过；--run-slow 全量跑。
Ray 未安装则整文件跳过。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.slow

from symbio.distributed import ray_available

if not ray_available():
    pytest.skip("Ray 未安装，跳过分布式测试", allow_module_level=True)

from symbio.distributed import RayExecutor
from symbio.utils.types import Intent, Task

# worker 进程需 import 的测试 Agent 模块（dist_echo / dist_boom 在此注册）
_BOOTSTRAP = ["symbio.distributed._test_agents"]


@pytest.fixture(scope="module")
def executor():
    ex = RayExecutor(num_workers=3, bootstrap_modules=_BOOTSTRAP)
    ex.start()
    yield ex
    ex.shutdown()


def test_available_and_started(executor):
    assert executor.available() is True
    assert executor.is_started() is True


def test_workers_run_in_distinct_processes(executor):
    """核心证据：任务真的在多个独立进程上执行，且都不是主进程。"""
    tasks = [Task(intent=Intent(raw_text=f"t{i}")) for i in range(9)]
    refs = [executor.submit("dist_echo", t) for t in tasks]
    outs = executor.gather(refs)

    assert all(o["ok"] for o in outs), outs
    worker_pids = {o["pid"] for o in outs}
    # 3 个 worker，9 个任务 round-robin 分发 → 应命中多个不同 PID
    assert len(worker_pids) >= 2, f"只在 {worker_pids} 上执行，未体现多进程"
    assert os.getpid() not in worker_pids, "任务竟在主进程执行，不是真分布式"


def test_result_content_from_worker(executor):
    task = Task(intent=Intent(raw_text="hello"))
    out = executor.gather([executor.submit("dist_echo", task)])[0]
    assert out["ok"] is True
    content = out["result"]["content"]
    assert content.startswith("echo:hello|pid:")
    assert out["result"]["success"] is True


def test_worker_exception_isolated(executor):
    """Agent 在 worker 内抛异常 → 回传为失败结果，不炸主进程。"""
    task = Task(intent=Intent(raw_text="x"))
    out = executor.gather([executor.submit("dist_boom", task)])[0]
    assert out["ok"] is False
    assert "boom from worker" in out["error"]


def test_unknown_agent_returns_error(executor):
    task = Task(intent=Intent(raw_text="x"))
    out = executor.gather([executor.submit("no_such_agent", task)])[0]
    assert out["ok"] is False
    assert "未找到 Agent" in out["error"]


def test_cancel_does_not_raise(executor):
    task = Task(intent=Intent(raw_text="x"))
    ref = executor.submit("dist_echo", task)
    # 取消是尽力而为，不应抛异常（可能已完成）
    executor.cancel(ref)


def test_submit_before_start_raises():
    from symbio.distributed import RayRuntimeError

    ex = RayExecutor(num_workers=1)
    task = Task(intent=Intent(raw_text="x"))
    with pytest.raises(RayRuntimeError):
        ex.submit("dist_echo", task)


# ---------------------------------------------------------------------------
# SubAgentManager 端到端：注入 RayExecutor 后子任务跨进程执行
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subagent_manager_executes_group_on_ray(executor):
    from symbio.agents.registry import get_registry
    from symbio.agents.subagent import SubAgentManager
    from symbio.core.decomposer import SubTask
    from symbio.core.event_bus import EventBus
    from symbio.core.rate_limiter import RateLimiter
    from symbio.utils.types import Task as CoreTask

    import symbio.distributed._test_agents  # noqa: F401  —— 主进程也注册，便于 _resolve_agent

    manager = SubAgentManager(
        get_registry(), EventBus(), RateLimiter(), executor=executor
    )
    subtasks = [
        SubTask(subtask_id=f"s{i}", name=f"sub{i}", description=f"echo{i}",
                action="chat", suggested_agent="dist_echo")
        for i in range(4)
    ]
    parent = CoreTask(intent=Intent(raw_text="parent"))
    agg = await manager.execute_subtasks(
        subtasks, parent, execution_order=[[s.subtask_id for s in subtasks]]
    )

    assert agg.total_subtasks == 4
    assert agg.completed_subtasks == 4
    assert agg.success is True
    # 结果内容来自 worker（带 pid），证明真的过了 Ray 路径
    joined = " ".join(r.content for r in agg.results)
    assert "echo:" in joined
    assert "pid:" in joined


@pytest.mark.asyncio
async def test_subagent_manager_falls_back_without_executor():
    """未注入 executor → 走 asyncio 路径，用主进程内的 Agent 正常执行。"""
    from symbio.agents.registry import get_registry
    from symbio.agents.subagent import SubAgentManager
    from symbio.core.decomposer import SubTask
    from symbio.core.event_bus import EventBus
    from symbio.core.rate_limiter import RateLimiter
    from symbio.utils.types import Task as CoreTask

    import symbio.distributed._test_agents  # noqa: F401

    manager = SubAgentManager(get_registry(), EventBus(), RateLimiter())  # executor=None
    subtasks = [
        SubTask(subtask_id="s0", name="sub0", description="echo",
                action="chat", suggested_agent="dist_echo")
    ]
    parent = CoreTask(intent=Intent(raw_text="parent"))
    agg = await manager.execute_subtasks(subtasks, parent, execution_order=[["s0"]])
    assert agg.completed_subtasks == 1
    assert agg.success is True
