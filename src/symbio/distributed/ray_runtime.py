"""真 Ray Actor 分布式执行运行时。

SubAgent 的默认并发是单进程 asyncio.gather——CPU 密集或需要真多进程隔离时，
本模块用真实的 Ray Actor 池把子任务分发到独立 worker 进程执行，产出可序列化的
Result，再由主进程收集、补发事件、聚合。Ray 未安装或起不来时，`ray_available()`
返回 False，由 SubAgentManager 回退到 asyncio（行为与从前一致）。

关键设计——**不序列化 Agent 实例**：
    Symbio 的 Agent（如 GeneralAgent）无参构造、LLM client 在 execute 内部按
    settings 创建，实例上不持有 socket 等不可 pickle 的东西。因此 worker 侧只接收
    `(agent_name, task_dict)`，在 worker 进程内用全局 registry 重建 agent 再执行，
    避免跨进程序列化 client 的老大难问题。Task/Result 都是 pydantic 模型，
    用 model_dump()/model_validate() 干净地过进程边界。
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Optional

from symbio.utils.logger import get_logger

logger = get_logger("ray_runtime")


class RayRuntimeError(RuntimeError):
    """Ray 运行时相关错误（未安装 / 初始化失败 / 提交失败）。"""


def ray_available() -> bool:
    """探测 Ray 是否可 import（不强制 init，避免探测即起集群的副作用）。"""
    return importlib.util.find_spec("ray") is not None


# ---------------------------------------------------------------------------
# worker 侧：重建 registry + 执行单个子任务（在独立 Ray worker 进程内运行）
# ---------------------------------------------------------------------------


def _bootstrap_registry(extra_modules: Optional[list] = None):
    """在 worker 进程内确保 builtin Agent 已注册并返回全局 registry。

    Ray worker 是全新进程，模块级的全局 registry 初始为空；import builtin 包会
    触发 @register_agent 装饰器把 GeneralAgent 等注册进全局 registry。
    """
    import importlib

    import symbio.agents.builtin  # noqa: F401  —— import 即完成注册（装饰器副作用）
    from symbio.agents.registry import get_registry

    # 额外的 Agent 包（用户自定义 Agent / 测试 Agent）：import 触发其注册装饰器
    for mod in extra_modules or ():
        try:
            importlib.import_module(mod)
        except Exception as exc:  # 单个模块失败不影响其它 Agent 可用
            logger.warning(f"worker 内 import Agent 模块失败 {mod}: {exc}")

    return get_registry()


def _run_subtask_in_worker(
    agent_name: str, task_dict: dict, extra_modules: Optional[list] = None
) -> dict:
    """worker 进程内执行入口：重建 agent → 执行 task → 返回可序列化结果。

    返回 dict 而非 Result 对象，跨进程边界更稳妥；同时带上 worker PID，
    让上层能证明"任务真的在独立进程上跑过"（区别于 asyncio 单进程伪并发）。
    """
    import asyncio

    from symbio.utils.types import Result, Task

    pid = os.getpid()
    try:
        registry = _bootstrap_registry(extra_modules)
        agent = registry.get(agent_name)
        if agent is None:
            return {
                "ok": False,
                "pid": pid,
                "error": f"worker 内未找到 Agent: {agent_name}",
            }

        task = Task.model_validate(task_dict)
        # worker 进程内没有事件循环，用 asyncio.run 驱动 async execute
        result: Result = asyncio.run(agent.execute(task))
        return {"ok": True, "pid": pid, "result": result.model_dump(mode="json")}
    except Exception as exc:  # 隔离 worker 异常，回传给主进程决定如何处理
        return {"ok": False, "pid": pid, "error": str(exc)}


# ---------------------------------------------------------------------------
# 主进程侧：RayExecutor 门面（start / submit / gather / cancel / shutdown）
# ---------------------------------------------------------------------------


class RayExecutor:
    """真 Ray Actor 池执行器。

    生命周期：
        ex = RayExecutor()
        ex.start()                       # 真 ray.init（本地或连指定集群）
        ref = ex.submit("general", task) # 提交到 Actor 池，立即返回 ObjectRef
        outs = ex.gather([ref])          # 阻塞收集结果（dict 列表）
        ex.cancel(ref)                   # 取消未完成任务
        ex.shutdown()                    # 关闭池 + ray.shutdown

    不可用（未装 Ray / init 失败）时 start() 抛 RayRuntimeError，由上层回退。
    """

    def __init__(
        self,
        address: Optional[str] = None,
        num_workers: int = 4,
        bootstrap_modules: Optional[list] = None,
    ) -> None:
        # address=None → 本地起集群；"auto"/"ray://..." → 连已有集群
        self._address = address
        self._num_workers = max(1, num_workers)
        # worker 进程需额外 import 的 Agent 模块（用户自定义 / 测试 Agent）
        self._bootstrap_modules = list(bootstrap_modules or [])
        self._ray = None
        self._actor_cls = None
        self._actors: list = []
        self._rr = 0  # round-robin 派发游标
        self._started = False
        self._owns_ray = False  # 是否由本执行器 init 的 ray（决定 shutdown 时是否收尾）

    @staticmethod
    def _ensure_localhost_no_proxy() -> None:
        """把 localhost/127.0.0.1 加进 no_proxy，避免全局代理劫持 Ray 本地 gRPC。"""
        hosts = {"localhost", "127.0.0.1", "::1"}
        for var in ("no_proxy", "NO_PROXY"):
            current = os.environ.get(var, "")
            existing = {h.strip() for h in current.split(",") if h.strip()}
            merged = existing | hosts
            os.environ[var] = ",".join(sorted(merged))

    def available(self) -> bool:
        return ray_available()

    def is_started(self) -> bool:
        return self._started

    def start(self) -> None:
        """真 ray.init + 建 Actor 池。已启动则幂等返回。"""
        if self._started:
            return
        if not ray_available():
            raise RayRuntimeError("Ray 未安装，无法启动分布式执行；安装: pip install ray")
        try:
            # 本机常配全局 HTTP(S) 代理；Ray 的 GCS/worker 走本地 gRPC，必须让
            # 127.0.0.1/localhost 绕过代理，否则 init 会卡在"连不上 GCS"直到超时。
            self._ensure_localhost_no_proxy()

            import ray

            self._ray = ray
            if not ray.is_initialized():
                if self._address:
                    ray.init(address=self._address, ignore_reinit_error=True)
                else:
                    # 本地集群：不跑 dashboard，静音日志，避免污染测试输出
                    ray.init(
                        ignore_reinit_error=True,
                        include_dashboard=False,
                        logging_level="ERROR",
                        configure_logging=False,
                    )
                self._owns_ray = True

            # 定义并实例化 Actor 池。Actor 是常驻 worker 进程，反复复用。
            @ray.remote
            class _SubtaskActor:
                def run(self, agent_name: str, task_dict: dict, extra_modules) -> dict:
                    return _run_subtask_in_worker(agent_name, task_dict, extra_modules)

                def ping(self) -> int:
                    return os.getpid()

            self._actor_cls = _SubtaskActor
            self._actors = [_SubtaskActor.remote() for _ in range(self._num_workers)]
            self._started = True
            logger.info(
                f"RayExecutor 启动: workers={self._num_workers}, address={self._address or 'local'}"
            )
        except RayRuntimeError:
            raise
        except Exception as exc:
            raise RayRuntimeError(f"Ray 初始化失败: {exc}") from exc

    def submit(self, agent_name: str, task: Any):
        """提交一个子任务到 Actor 池，返回 Ray ObjectRef（非阻塞）。

        task 接受 Task 对象或已 dump 的 dict；round-robin 派发到各 Actor。
        """
        if not self._started:
            raise RayRuntimeError("RayExecutor 未启动，先调用 start()")
        task_dict = task if isinstance(task, dict) else task.model_dump(mode="json")
        actor = self._actors[self._rr % len(self._actors)]
        self._rr += 1
        return actor.run.remote(agent_name, task_dict, self._bootstrap_modules)

    def gather(self, refs: list) -> list[dict]:
        """阻塞收集所有 ObjectRef 的结果，保持提交顺序。"""
        if not self._started:
            raise RayRuntimeError("RayExecutor 未启动，先调用 start()")
        return list(self._ray.get(refs))

    def cancel(self, ref) -> None:
        """取消一个未完成的任务（尽力而为）。"""
        if self._started and self._ray is not None:
            try:
                self._ray.cancel(ref, force=True)
            except Exception as exc:
                logger.debug(f"cancel 失败（可能已完成）: {exc}")

    def worker_pids(self) -> list[int]:
        """返回各 Actor 的进程 PID（用于验证真多进程）。"""
        if not self._started:
            return []
        return list(self._ray.get([a.ping.remote() for a in self._actors]))

    def shutdown(self) -> None:
        """关闭 Actor 池；若 ray 是本执行器起的，一并 ray.shutdown。"""
        if not self._started:
            return
        for actor in self._actors:
            try:
                self._ray.kill(actor)
            except Exception:
                pass
        self._actors = []
        if self._owns_ray and self._ray is not None:
            try:
                self._ray.shutdown()
            except Exception as exc:
                logger.debug(f"ray.shutdown 异常: {exc}")
        self._started = False
        logger.info("RayExecutor 已关闭")
