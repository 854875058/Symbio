"""内存管理器 - 内存监控、泄漏检测与垃圾回收策略"""

from __future__ import annotations

import gc
import sys
import threading
import time
import tracemalloc
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("utils.memory_manager")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class GCStrategy(str, Enum):
    """垃圾回收策略"""
    AUTO = "auto"             # 自动 (Python 默认)
    AGGRESSIVE = "aggressive" # 激进回收
    CONSERVATIVE = "conservative"  # 保守回收
    MANUAL = "manual"         # 手动回收
    ADAPTIVE = "adaptive"     # 自适应


class MemoryPressure(str, Enum):
    """内存压力等级"""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class MemorySnapshot(BaseModel):
    """内存快照"""
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.now)
    rss_mb: float = 0.0           # 物理内存 (MB)
    vms_mb: float = 0.0           # 虚拟内存 (MB)
    heap_mb: float = 0.0          # Python 堆内存 (MB)
    gc_counts: dict[int, int] = Field(default_factory=dict)  # 各代 GC 计数
    object_counts: dict[str, int] = Field(default_factory=dict)  # 各类对象计数
    tracemalloc_top: list[dict[str, Any]] = Field(default_factory=list)
    pressure: MemoryPressure = MemoryPressure.LOW


class MemoryAlert(BaseModel):
    """内存告警"""
    alert_id: str = Field(default_factory=lambda: str(uuid4()))
    level: MemoryPressure
    message: str
    current_mb: float = 0.0
    threshold_mb: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)
    action_taken: str = ""


class MemoryStats(BaseModel):
    """内存统计信息"""
    current_rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    current_heap_mb: float = 0.0
    peak_heap_mb: float = 0.0
    gc_collections: dict[int, int] = Field(default_factory=dict)
    total_snapshots: int = 0
    total_alerts: int = 0
    monitoring_duration_sec: float = 0.0
    pressure_distribution: dict[str, int] = Field(default_factory=dict)


class ObjectTracker(BaseModel):
    """对象跟踪记录"""
    type_name: str
    count: int = 0
    total_size_bytes: int = 0
    growth_rate: float = 0.0  # 每秒增长


# ---------------------------------------------------------------------------
# 内存监控器
# ---------------------------------------------------------------------------

class MemoryMonitor:
    """内存监控器 - 跟踪进程内存使用"""

    def __init__(self, enable_tracemalloc: bool = False):
        self._enable_tracemalloc = enable_tracemalloc
        self._peak_rss: float = 0.0
        self._peak_heap: float = 0.0
        self._snapshots: list[MemorySnapshot] = []
        self._alerts: list[MemoryAlert] = []
        self._start_time: float = time.monotonic()
        self._pressure_counts: dict[str, int] = defaultdict(int)

        if enable_tracemalloc:
            tracemalloc.start(25)

    def take_snapshot(self) -> MemorySnapshot:
        """获取内存快照"""
        snapshot = MemorySnapshot()
        gc.collect()  # 确保数据准确

        # 获取进程内存
        try:
            import psutil
            process = psutil.Process()
            mem_info = process.memory_info()
            snapshot.rss_mb = mem_info.rss / (1024 * 1024)
            snapshot.vms_mb = mem_info.vms / (1024 * 1024)
        except ImportError:
            # psutil 不可用时使用 tracemalloc
            if self._enable_tracemalloc:
                current, peak = tracemalloc.get_traced_memory()
                snapshot.heap_mb = current / (1024 * 1024)
            snapshot.rss_mb = snapshot.heap_mb

        # Python 堆内存
        if self._enable_tracemalloc:
            current, _ = tracemalloc.get_traced_memory()
            snapshot.heap_mb = current / (1024 * 1024)
            snapshot.tracemalloc_top = self._get_tracemalloc_top(10)

        # GC 统计
        gc_stats = gc.get_stats()
        for i, stat in enumerate(gc_stats):
            snapshot.gc_counts[i] = stat.get("collections", 0)

        # 对象计数
        snapshot.object_counts = self._count_objects()

        # 内存压力评估
        snapshot.pressure = self._evaluate_pressure(snapshot.rss_mb)
        self._pressure_counts[snapshot.pressure.value] += 1

        # 更新峰值
        self._peak_rss = max(self._peak_rss, snapshot.rss_mb)
        self._peak_heap = max(self._peak_heap, snapshot.heap_mb)

        self._snapshots.append(snapshot)
        return snapshot

    def _evaluate_pressure(self, rss_mb: float) -> MemoryPressure:
        """评估内存压力"""
        if rss_mb < 100:
            return MemoryPressure.LOW
        elif rss_mb < 500:
            return MemoryPressure.MODERATE
        elif rss_mb < 1000:
            return MemoryPressure.HIGH
        else:
            return MemoryPressure.CRITICAL

    def _count_objects(self) -> dict[str, int]:
        """统计各类型对象数量"""
        type_counts: dict[str, int] = defaultdict(int)
        for obj in gc.get_objects():
            type_name = type(obj).__name__
            type_counts[type_name] += 1
        # 只返回前 20 个最多的类型
        sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        return dict(sorted_types)

    def _get_tracemalloc_top(self, limit: int) -> list[dict[str, Any]]:
        """获取 tracemalloc 内存分配排行"""
        if not self._enable_tracemalloc:
            return []

        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")[:limit]
        return [
            {
                "file": str(stat.traceback),
                "size_kb": stat.size / 1024,
                "count": stat.count,
            }
            for stat in top_stats
        ]

    def get_current_rss(self) -> float:
        """获取当前 RSS (MB)"""
        try:
            import psutil
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except ImportError:
            if self._enable_tracemalloc:
                current, _ = tracemalloc.get_traced_memory()
                return current / (1024 * 1024)
            return 0.0

    @property
    def peak_rss(self) -> float:
        return self._peak_rss

    @property
    def peak_heap(self) -> float:
        return self._peak_heap

    def get_snapshots(self, limit: int = 100) -> list[MemorySnapshot]:
        """获取历史快照"""
        return self._snapshots[-limit:]

    def get_alerts(self) -> list[MemoryAlert]:
        """获取告警记录"""
        return list(self._alerts)


# ---------------------------------------------------------------------------
# 垃圾回收管理器
# ---------------------------------------------------------------------------

class GCManager:
    """垃圾回收管理器 - 自定义 GC 策略"""

    def __init__(self, strategy: GCStrategy = GCStrategy.AUTO):
        self._strategy = strategy
        self._original_thresholds = gc.get_threshold()
        self._collection_counts: dict[int, int] = {0: 0, 1: 0, 2: 0}
        self._apply_strategy(strategy)

    def _apply_strategy(self, strategy: GCStrategy) -> None:
        """应用 GC 策略"""
        if strategy == GCStrategy.AUTO:
            gc.set_threshold(*self._original_thresholds)
        elif strategy == GCStrategy.AGGRESSIVE:
            # 更频繁的回收
            gc.set_threshold(100, 5, 2)
        elif strategy == GCStrategy.CONSERVATIVE:
            # 减少回收频率
            gc.set_threshold(1000, 20, 10)
        elif strategy == GCStrategy.MANUAL:
            # 禁用自动回收
            gc.disable()
        elif strategy == GCStrategy.ADAPTIVE:
            # 自适应策略: 根据内存压力动态调整
            gc.set_threshold(500, 10, 5)

        logger.info(f"GC 策略已设置: {strategy.value}")

    @property
    def strategy(self) -> GCStrategy:
        return self._strategy

    def set_strategy(self, strategy: GCStrategy) -> None:
        """切换 GC 策略"""
        if self._strategy == GCStrategy.MANUAL:
            gc.enable()
        self._strategy = strategy
        self._apply_strategy(strategy)

    def collect(self, generation: int = 2) -> dict[int, int]:
        """手动触发垃圾回收

        Args:
            generation: 回收代 (0, 1, 2)

        Returns:
            各代回收的对象数
        """
        collected: dict[int, int] = {}
        for gen in range(generation + 1):
            count = gc.collect(gen)
            collected[gen] = count
            self._collection_counts[gen] = self._collection_counts.get(gen, 0) + 1

        logger.debug(f"GC 执行: generation={generation}, collected={collected}")
        return collected

    def adapt_to_pressure(self, pressure: MemoryPressure) -> None:
        """根据内存压力自适应调整 GC

        Args:
            pressure: 内存压力等级
        """
        if self._strategy != GCStrategy.ADAPTIVE:
            return

        if pressure == MemoryPressure.LOW:
            gc.set_threshold(1000, 20, 10)
        elif pressure == MemoryPressure.MODERATE:
            gc.set_threshold(500, 10, 5)
        elif pressure == MemoryPressure.HIGH:
            gc.set_threshold(200, 5, 3)
            gc.collect()
        elif pressure == MemoryPressure.CRITICAL:
            gc.set_threshold(100, 3, 1)
            gc.collect()

        logger.info(f"GC 自适应调整: pressure={pressure.value}, thresholds={gc.get_threshold()}")

    def get_stats(self) -> dict[str, Any]:
        """获取 GC 统计"""
        return {
            "strategy": self._strategy.value,
            "thresholds": gc.get_threshold(),
            "is_enabled": gc.isenabled(),
            "collections": dict(self._collection_counts),
            "gc_stats": gc.get_stats(),
        }

    def restore_defaults(self) -> None:
        """恢复默认设置"""
        gc.enable()
        gc.set_threshold(*self._original_thresholds)
        self._strategy = GCStrategy.AUTO


# ---------------------------------------------------------------------------
# 对象泄漏检测器
# ---------------------------------------------------------------------------

class LeakDetector:
    """对象泄漏检测器"""

    def __init__(self):
        self._baseline: dict[str, int] = {}
        self._tracking: bool = False

    def set_baseline(self) -> None:
        """设置基线 (记录当前对象计数)"""
        gc.collect()
        self._baseline = self._count_objects()
        self._tracking = True
        logger.info(f"泄漏检测基线已设置: {len(self._baseline)} 种类型")

    def check_leaks(self) -> list[ObjectTracker]:
        """检查泄漏 (与基线对比)

        Returns:
            可能泄漏的对象类型列表
        """
        if not self._tracking:
            logger.warning("未设置基线, 请先调用 set_baseline()")
            return []

        gc.collect()
        current = self._count_objects()
        leaks: list[ObjectTracker] = []

        for type_name, count in current.items():
            baseline_count = self._baseline.get(type_name, 0)
            growth = count - baseline_count
            if growth > 100:  # 增长超过 100 个对象视为可疑
                leaks.append(
                    ObjectTracker(
                        type_name=type_name,
                        count=count,
                        growth_rate=float(growth),
                    )
                )

        # 检查新出现的类型
        for type_name in current:
            if type_name not in self._baseline and current[type_name] > 50:
                leaks.append(
                    ObjectTracker(
                        type_name=type_name,
                        count=current[type_name],
                        growth_rate=float(current[type_name]),
                    )
                )

        leaks.sort(key=lambda t: t.growth_rate, reverse=True)
        return leaks

    def _count_objects(self) -> dict[str, int]:
        """统计对象数量"""
        type_counts: dict[str, int] = defaultdict(int)
        for obj in gc.get_objects():
            type_name = type(obj).__name__
            type_counts[type_name] += 1
        return dict(type_counts)


# ---------------------------------------------------------------------------
# 内存管理器
# ---------------------------------------------------------------------------

class MemoryManager:
    """内存管理器

    集成内存监控、垃圾回收策略和泄漏检测功能。

    用法:
        manager = MemoryManager(gc_strategy=GCStrategy.ADAPTIVE)
        manager.start_monitoring(interval_sec=10)
        snapshot = manager.take_snapshot()
        leaks = manager.check_leaks()
    """

    def __init__(
        self,
        gc_strategy: GCStrategy = GCStrategy.AUTO,
        enable_tracemalloc: bool = False,
        alert_threshold_mb: float = 500.0,
    ):
        self._monitor = MemoryMonitor(enable_tracemalloc=enable_tracemalloc)
        self._gc_manager = GCManager(gc_strategy)
        self._leak_detector = LeakDetector()
        self._alert_threshold_mb = alert_threshold_mb
        self._monitoring_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._alert_callbacks: list[Callable[[MemoryAlert], None]] = []
        self._start_time: float = time.monotonic()

    def start_monitoring(self, interval_sec: float = 30.0) -> None:
        """启动后台监控

        Args:
            interval_sec: 监控间隔 (秒)
        """
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            logger.warning("监控已在运行")
            return

        self._stop_event.clear()
        self._monitoring_thread = threading.Thread(
            target=self._monitoring_loop,
            args=(interval_sec,),
            daemon=True,
        )
        self._monitoring_thread.start()
        logger.info(f"内存监控已启动: interval={interval_sec}s")

    def stop_monitoring(self) -> None:
        """停止后台监控"""
        self._stop_event.set()
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=5)
        logger.info("内存监控已停止")

    def _monitoring_loop(self, interval_sec: float) -> None:
        """监控循环"""
        while not self._stop_event.is_set():
            try:
                snapshot = self.take_snapshot()

                # 检查告警
                if snapshot.rss_mb > self._alert_threshold_mb:
                    alert = MemoryAlert(
                        level=snapshot.pressure,
                        message=f"内存使用超过阈值: {snapshot.rss_mb:.1f}MB > {self._alert_threshold_mb}MB",
                        current_mb=snapshot.rss_mb,
                        threshold_mb=self._alert_threshold_mb,
                    )

                    # 自动 GC
                    if snapshot.pressure in (MemoryPressure.HIGH, MemoryPressure.CRITICAL):
                        collected = self._gc_manager.collect()
                        alert.action_taken = f"自动 GC: {collected}"

                    self._monitor._alerts.append(alert)
                    for callback in self._alert_callbacks:
                        try:
                            callback(alert)
                        except Exception as exc:
                            logger.error(f"告警回调异常: {exc}")

                # 自适应 GC
                self._gc_manager.adapt_to_pressure(snapshot.pressure)

            except Exception as exc:
                logger.error(f"内存监控异常: {exc}")

            self._stop_event.wait(interval_sec)

    def take_snapshot(self) -> MemorySnapshot:
        """获取内存快照"""
        return self._monitor.take_snapshot()

    def check_leaks(self) -> list[ObjectTracker]:
        """检查内存泄漏"""
        return self._leak_detector.check_leaks()

    def set_leak_baseline(self) -> None:
        """设置泄漏检测基线"""
        self._leak_detector.set_baseline()

    def force_gc(self, generation: int = 2) -> dict[int, int]:
        """强制垃圾回收"""
        return self._gc_manager.collect(generation)

    def set_gc_strategy(self, strategy: GCStrategy) -> None:
        """设置 GC 策略"""
        self._gc_manager.set_strategy(strategy)

    def on_alert(self, callback: Callable[[MemoryAlert], None]) -> None:
        """注册告警回调"""
        self._alert_callbacks.append(callback)

    def get_stats(self) -> MemoryStats:
        """获取内存统计"""
        snapshots = self._monitor.get_snapshots()
        alerts = self._monitor.get_alerts()

        return MemoryStats(
            current_rss_mb=self._monitor.get_current_rss(),
            peak_rss_mb=self._monitor.peak_rss,
            current_heap_mb=snapshots[-1].heap_mb if snapshots else 0.0,
            peak_heap_mb=self._monitor.peak_heap,
            gc_collections=self._gc_manager.get_stats().get("collections", {}),
            total_snapshots=len(snapshots),
            total_alerts=len(alerts),
            monitoring_duration_sec=time.monotonic() - self._start_time,
            pressure_distribution=dict(self._monitor._pressure_counts),
        )

    def get_gc_stats(self) -> dict[str, Any]:
        """获取 GC 统计"""
        return self._gc_manager.get_stats()

    def get_report(self) -> str:
        """生成内存报告"""
        stats = self.get_stats()
        gc_stats = self.get_gc_stats()

        lines = [
            "=" * 60,
            "内存管理报告",
            "=" * 60,
            f"当前 RSS: {stats.current_rss_mb:.1f} MB",
            f"峰值 RSS: {stats.peak_rss_mb:.1f} MB",
            f"当前堆内存: {stats.current_heap_mb:.1f} MB",
            f"峰值堆内存: {stats.peak_heap_mb:.1f} MB",
            f"监控时长: {stats.monitoring_duration_sec:.1f} 秒",
            f"快照数量: {stats.total_snapshots}",
            f"告警数量: {stats.total_alerts}",
            "",
            "GC 配置:",
            f"  策略: {gc_stats['strategy']}",
            f"  阈值: {gc_stats['thresholds']}",
            f"  自动回收: {'启用' if gc_stats['is_enabled'] else '禁用'}",
            "",
            "内存压力分布:",
        ]

        for level, count in stats.pressure_distribution.items():
            lines.append(f"  {level}: {count} 次")

        lines.append("=" * 60)
        return "\n".join(lines)
