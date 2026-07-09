"""可在 Ray worker 进程内 import 的测试用 Agent。

Ray worker 是独立进程，只能执行「可 import」的 Agent——driver 里临时定义的类
在 worker 里不存在。这些 stub 放在真实模块里，`_bootstrap_registry` import builtin
之外，测试通过显式 import 本模块触发注册，从而在 worker 内也可用。

纯计算 / 回显，不联网、不花钱，用于验证分布式执行链路本身。
"""

from __future__ import annotations

import os

from symbio.agents.base import BaseAgent
from symbio.agents.registry import register_agent
from symbio.utils.types import Result, Task


@register_agent("dist_echo")
class DistEchoAgent(BaseAgent):
    """回显 Agent：把输入原样返回，并带上执行进程 PID（证明跨进程）。"""

    name = "dist_echo"
    description = "分布式测试回显 Agent"

    async def execute(self, task: Task) -> Result:
        pid = os.getpid()
        text = task.intent.raw_text
        return Result(
            task_id=task.task_id,
            success=True,
            content=f"echo:{text}|pid:{pid}",
        )


@register_agent("dist_boom")
class DistBoomAgent(BaseAgent):
    """故意抛异常的 Agent：验证 worker 异常被隔离并回传为失败结果。"""

    name = "dist_boom"
    description = "分布式测试异常 Agent"

    async def execute(self, task: Task) -> Result:
        raise RuntimeError("boom from worker")
