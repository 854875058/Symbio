"""轻量级运行时 - 面向资源受限设备的精简运行环境"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("interfaces.edge.runtime")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class DeviceTier(str, Enum):
    """设备资源等级"""
    TIER_0 = "tier_0"   # 极低资源 (< 64MB RAM, 单核)
    TIER_1 = "tier_1"   # 低资源 (64-256MB RAM, 双核)
    TIER_2 = "tier_2"   # 中等资源 (256MB-1GB RAM, 四核)
    TIER_3 = "tier_3"   # 较高资源 (> 1GB RAM, 多核)


class RuntimeStatus(str, Enum):
    """运行时状态"""
    CREATED = "created"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class TaskPriority(str, Enum):
    """任务优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class ResourceUsage(BaseModel):
    """资源使用情况"""
    memory_used_mb: float = 0.0
    memory_limit_mb: float = 0.0
    cpu_percent: float = 0.0
    disk_used_mb: float = 0.0
    disk_limit_mb: float = 0.0
    active_tasks: int = 0
    queued_tasks: int = 0
    timestamp: datetime = Field(default_factory=datetime.now)


class RuntimeTask(BaseModel):
    """运行时任务"""
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    handler: str = ""  # 处理函数名
    priority: TaskPriority = TaskPriority.NORMAL
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = 30000
    retry_count: int = 0
    max_retries: int = 3
    status: RuntimeStatus = RuntimeStatus.CREATED
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RuntimeConfig(BaseModel):
    """运行时配置"""
    device_tier: DeviceTier = DeviceTier.TIER_1
    memory_limit_mb: float = 64.0
    disk_limit_mb: float = 256.0
    max_concurrent_tasks: int = 2
    task_queue_size: int = 50
    enable_model_offloading: bool = False
    model_cache_size_mb: float = 32.0
    enable_telemetry: bool = True
    telemetry_interval_sec: float = 30.0
    log_level: str = "INFO"


class RuntimeMetrics(BaseModel):
    """运行时指标"""
    uptime_seconds: float = 0.0
    total_tasks_executed: int = 0
    total_tasks_failed: int = 0
    avg_task_duration_ms: float = 0.0
    peak_memory_mb: float = 0.0
    current_memory_mb: float = 0.0
    restarts: int = 0


# ---------------------------------------------------------------------------
# 资源监控器
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """资源监控器 - 跟踪设备资源使用"""

    def __init__(self, config: RuntimeConfig):
        self._config = config
        self._peak_memory: float = 0.0
        self._task_durations: list[float] = []
        self._lock = threading.Lock()

    def get_usage(self) -> ResourceUsage:
        """获取当前资源使用情况"""
        memory_used = self._estimate_memory()

        with self._lock:
            self._peak_memory = max(self._peak_memory, memory_used)

        return ResourceUsage(
            memory_used_mb=memory_used,
            memory_limit_mb=self._config.memory_limit_mb,
            cpu_percent=self._estimate_cpu(),
            disk_used_mb=self._estimate_disk(),
            disk_limit_mb=self._config.disk_limit_mb,
        )

    def record_task_duration(self, duration_ms: float) -> None:
        """记录任务执行时长"""
        with self._lock:
            self._task_durations.append(duration_ms)
            # 保留最近 1000 条记录
            if len(self._task_durations) > 1000:
                self._task_durations = self._task_durations[-1000:]

    def get_metrics(self) -> RuntimeMetrics:
        """获取运行时指标"""
        with self._lock:
            avg_duration = (
                sum(self._task_durations) / len(self._task_durations)
                if self._task_durations
                else 0.0
            )

        return RuntimeMetrics(
            peak_memory_mb=self._peak_memory,
            current_memory_mb=self._estimate_memory(),
            avg_task_duration_ms=avg_duration,
        )

    def is_resource_available(self) -> bool:
        """检查是否有足够资源执行新任务"""
        usage = self.get_usage()
        return usage.memory_used_mb < self._config.memory_limit_mb * 0.9

    def _estimate_memory(self) -> float:
        """估算内存使用 (MB)"""
        try:
            import psutil
            process = psutil.Process(os.getpid())
            return process.memory_info().rss / (1024 * 1024)
        except ImportError:
            # psutil 不可用时使用粗略估算
            return sys.getsizeof({}) / (1024 * 1024) * 100

    def _estimate_cpu(self) -> float:
        """估算 CPU 使用百分比"""
        try:
            import psutil
            return psutil.cpu_percent(interval=0)
        except ImportError:
            return 0.0

    def _estimate_disk(self) -> float:
        """估算磁盘使用 (MB)"""
        return 0.0


# ---------------------------------------------------------------------------
# 任务调度器
# ---------------------------------------------------------------------------

class TaskScheduler:
    """轻量级任务调度器 - 适配资源受限环境"""

    def __init__(self, config: RuntimeConfig):
        self._config = config
        self._queue: list[RuntimeTask] = []
        self._running: dict[str, RuntimeTask] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._lock = threading.Lock()
        self._total_executed: int = 0
        self._total_failed: int = 0

    def register_handler(self, name: str, handler: Callable[..., Any]) -> None:
        """注册任务处理器"""
        self._handlers[name] = handler

    def submit(self, task: RuntimeTask) -> str:
        """提交任务

        Args:
            task: 运行时任务

        Returns:
            任务 ID

        Raises:
            RuntimeError: 队列已满
        """
        with self._lock:
            if len(self._queue) >= self._config.task_queue_size:
                raise RuntimeError(f"任务队列已满 ({self._config.task_queue_size})")
            self._queue.append(task)

        # 按优先级排序
        priority_order = {
            TaskPriority.CRITICAL: 0,
            TaskPriority.HIGH: 1,
            TaskPriority.NORMAL: 2,
            TaskPriority.LOW: 3,
        }
        with self._lock:
            self._queue.sort(key=lambda t: priority_order.get(t.priority, 2))

        logger.debug(f"任务已提交: {task.task_id}, priority={task.priority.value}")
        return task.task_id

    def execute_next(self, resource_monitor: ResourceMonitor) -> RuntimeTask | None:
        """执行队列中的下一个任务

        Args:
            resource_monitor: 资源监控器

        Returns:
            执行完成的任务, 或 None (队列为空或资源不足)
        """
        with self._lock:
            if not self._queue:
                return None

            if len(self._running) >= self._config.max_concurrent_tasks:
                return None

            task = self._queue.pop(0)
            self._running[task.task_id] = task

        # 检查资源
        if not resource_monitor.is_resource_available():
            with self._lock:
                del self._running[task.task_id]
                self._queue.insert(0, task)
            return None

        task.status = RuntimeStatus.RUNNING
        task.started_at = datetime.now()
        start_time = time.monotonic()

        try:
            handler = self._handlers.get(task.handler)
            if handler:
                result = handler(**task.payload)
                task.result = result if isinstance(result, dict) else {"output": result}
            else:
                task.result = {"output": f"模拟执行: {task.name}"}

            task.status = RuntimeStatus.STOPPED
            self._total_executed += 1

        except Exception as exc:
            task.error = f"{type(exc).__name__}: {exc}"
            task.status = RuntimeStatus.ERROR
            self._total_failed += 1

            # 重试
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = RuntimeStatus.CREATED
                with self._lock:
                    self._queue.insert(0, task)
                    del self._running[task.task_id]
                logger.warning(f"任务重试: {task.task_id}, 第 {task.retry_count} 次")
                return task

        duration_ms = (time.monotonic() - start_time) * 1000
        task.completed_at = datetime.now()
        resource_monitor.record_task_duration(duration_ms)

        with self._lock:
            self._running.pop(task.task_id, None)

        return task

    def get_queue_size(self) -> int:
        """获取队列大小"""
        return len(self._queue)

    def get_running_count(self) -> int:
        """获取正在运行的任务数"""
        return len(self._running)

    def clear_queue(self) -> int:
        """清空队列"""
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
        return count

    @property
    def total_executed(self) -> int:
        return self._total_executed

    @property
    def total_failed(self) -> int:
        return self._total_failed


# ---------------------------------------------------------------------------
# 轻量级运行时
# ---------------------------------------------------------------------------

class EdgeRuntime:
    """轻量级边缘运行时

    面向资源受限设备的精简运行环境, 支持任务调度、资源监控和模型卸载。

    用法:
        runtime = EdgeRuntime(config=RuntimeConfig(device_tier=DeviceTier.TIER_1))
        runtime.register_handler("greet", lambda name: f"Hello, {name}!")
        runtime.submit_task("greet", {"name": "world"})
        runtime.start()
    """

    def __init__(self, config: RuntimeConfig | None = None):
        self._config = config or RuntimeConfig()
        self._status = RuntimeStatus.CREATED
        self._resource_monitor = ResourceMonitor(self._config)
        self._task_scheduler = TaskScheduler(self._config)
        self._start_time: float = 0.0
        self._telemetry_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    def register_handler(self, name: str, handler: Callable[..., Any]) -> None:
        """注册任务处理器"""
        self._task_scheduler.register_handler(name, handler)
        logger.info(f"注册处理器: {name}")

    def submit_task(
        self,
        handler_name: str,
        payload: dict[str, Any] | None = None,
        name: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout_ms: int = 30000,
    ) -> str:
        """提交任务

        Args:
            handler_name: 处理器名称
            payload: 任务数据
            name: 任务名称
            priority: 优先级
            timeout_ms: 超时时间

        Returns:
            任务 ID
        """
        task = RuntimeTask(
            name=name or handler_name,
            handler=handler_name,
            priority=priority,
            payload=payload or {},
            timeout_ms=timeout_ms,
        )
        return self._task_scheduler.submit(task)

    def start(self) -> None:
        """启动运行时"""
        if self._status == RuntimeStatus.RUNNING:
            logger.warning("运行时已在运行")
            return

        self._status = RuntimeStatus.INITIALIZING
        self._start_time = time.monotonic()
        self._stop_event.clear()

        # 启动遥测线程
        if self._config.enable_telemetry:
            self._telemetry_thread = threading.Thread(
                target=self._telemetry_loop, daemon=True
            )
            self._telemetry_thread.start()

        self._status = RuntimeStatus.RUNNING
        logger.info(
            f"边缘运行时已启动: tier={self._config.device_tier.value}, "
            f"memory_limit={self._config.memory_limit_mb}MB"
        )

    def stop(self) -> None:
        """停止运行时"""
        self._status = RuntimeStatus.STOPPING
        self._stop_event.set()

        if self._telemetry_thread:
            self._telemetry_thread.join(timeout=5)

        self._status = RuntimeStatus.STOPPED
        logger.info("边缘运行时已停止")

    def pause(self) -> None:
        """暂停运行时"""
        if self._status == RuntimeStatus.RUNNING:
            self._status = RuntimeStatus.PAUSED
            logger.info("边缘运行时已暂停")

    def resume(self) -> None:
        """恢复运行时"""
        if self._status == RuntimeStatus.PAUSED:
            self._status = RuntimeStatus.RUNNING
            logger.info("边缘运行时已恢复")

    def process_pending_tasks(self) -> list[RuntimeTask]:
        """处理所有待执行任务"""
        results: list[RuntimeTask] = []
        while self._task_scheduler.get_queue_size() > 0:
            task = self._task_scheduler.execute_next(self._resource_monitor)
            if task:
                results.append(task)
            else:
                break
        return results

    def get_resource_usage(self) -> ResourceUsage:
        """获取资源使用情况"""
        usage = self._resource_monitor.get_usage()
        usage.active_tasks = self._task_scheduler.get_running_count()
        usage.queued_tasks = self._task_scheduler.get_queue_size()
        return usage

    def get_metrics(self) -> RuntimeMetrics:
        """获取运行时指标"""
        metrics = self._resource_monitor.get_metrics()
        metrics.uptime_seconds = time.monotonic() - self._start_time if self._start_time else 0
        metrics.total_tasks_executed = self._task_scheduler.total_executed
        metrics.total_tasks_failed = self._task_scheduler.total_failed
        metrics.restarts = 0
        return metrics

    def _telemetry_loop(self) -> None:
        """遥测循环"""
        while not self._stop_event.is_set():
            try:
                usage = self.get_resource_usage()
                if usage.memory_used_mb > self._config.memory_limit_mb * 0.85:
                    logger.warning(
                        f"内存使用接近限制: {usage.memory_used_mb:.1f}/{self._config.memory_limit_mb}MB"
                    )
            except Exception as exc:
                logger.error(f"遥测异常: {exc}")

            self._stop_event.wait(self._config.telemetry_interval_sec)

    @staticmethod
    def detect_device_tier() -> DeviceTier:
        """自动检测设备资源等级"""
        try:
            import psutil
            mem = psutil.virtual_memory()
            mem_mb = mem.total / (1024 * 1024)
            cpu_count = psutil.cpu_count() or 1

            if mem_mb < 64 or cpu_count < 2:
                return DeviceTier.TIER_0
            elif mem_mb < 256 or cpu_count < 4:
                return DeviceTier.TIER_1
            elif mem_mb < 1024:
                return DeviceTier.TIER_2
            else:
                return DeviceTier.TIER_3
        except ImportError:
            return DeviceTier.TIER_1  # 默认
