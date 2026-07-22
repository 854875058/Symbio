"""可观测性追踪器 - OpenTelemetry 全链路追踪、指标暴露、Token 热力图、记忆快照回放。

集成 OpenTelemetry，使每个 Agent/工具调用都是一个 Span，整条 DAG 是一个 Trace。
支持对接 Jaeger / Grafana / Prometheus，提供企业级监控能力。
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

# OpenTelemetry imports (optional dependency)
_OTEL_AVAILABLE = False
try:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SpanExporter,
        SpanExportResult,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        MetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
    from opentelemetry.trace import StatusCode, Status, SpanKind
    from opentelemetry.context import Context
    from opentelemetry.propagate import extract, inject
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    _OTEL_AVAILABLE = True
except ImportError:
    # Provide fallback stubs when opentelemetry is not installed
    class _StubEnum:
        """Fallback for OTel enums."""

        INTERNAL = "INTERNAL"
        CLIENT = "CLIENT"
        SERVER = "SERVER"
        PRODUCER = "PRODUCER"
        CONSUMER = "CONSUMER"
        UNSET = "UNSET"
        OK = "OK"
        ERROR = "ERROR"

    class SpanKind(_StubEnum):
        """Stub SpanKind when opentelemetry is not available."""

        pass

    class StatusCode(_StubEnum):
        """Stub StatusCode when opentelemetry is not available."""

        pass

    class Status:
        """Stub Status when opentelemetry is not available."""

        def __init__(self, status_code=None, description=""):
            self.status_code = status_code
            self.description = description

    # Stub classes for type annotations
    TracerProvider = None
    MeterProvider = None
    SpanExporter = None
    SpanExportResult = None
    BatchSpanProcessor = None
    ConsoleSpanExporter = None
    ConsoleMetricExporter = None
    MetricExporter = None
    PeriodicExportingMetricReader = None
    Resource = None
    SERVICE_NAME = "service.name"
    SERVICE_VERSION = "service.version"
    Context = None
    OTLPSpanExporter = None
    OTLPMetricExporter = None

    class _StubMetrics:
        """Stub for opentelemetry.metrics module."""

        Counter = None
        Histogram = None
        ObservableGauge = None
        Meter = None

        def set_meter_provider(self, *a, **kw):
            pass

        def get_meter(self, *a, **kw):
            return None

        class Observation:
            """Stub for metrics.Observation."""

            def __init__(self, value=0.0):
                self.value = value

    class _StubTrace:
        """Stub for opentelemetry.trace module."""

        Tracer = None

        def set_tracer_provider(self, *a, **kw):
            pass

        def get_tracer(self, *a, **kw):
            return None

    metrics = _StubMetrics()
    trace = _StubTrace()

    def inject(carrier):
        pass

    def extract(carrier):
        return None


logger = get_logger("tracer")


# ---------------------------------------------------------------------------
# Context variables for trace propagation within async tasks
# ---------------------------------------------------------------------------
_current_trace_id: ContextVar[Optional[str]] = ContextVar("_current_trace_id", default=None)
_current_span_id: ContextVar[Optional[str]] = ContextVar("_current_span_id", default=None)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExporterType(str, Enum):
    """Span / Metric 导出器类型。"""

    CONSOLE = "console"
    OTLP_GRPC = "otlp_grpc"
    NONE = "none"


class MetricType(str, Enum):
    """指标类型。"""

    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


# ---------------------------------------------------------------------------
# Pydantic Data Models
# ---------------------------------------------------------------------------


class TraceConfig(BaseModel):
    """追踪系统配置。"""

    service_name: str = Field(default="symbio", description="服务名称")
    service_version: str = Field(default="0.1.0", description="服务版本")
    exporter_type: ExporterType = Field(default=ExporterType.CONSOLE, description="Span 导出器类型")
    otlp_endpoint: str = Field(default="http://localhost:4317", description="OTLP gRPC 端点")
    otlp_insecure: bool = Field(default=True, description="OTLP 是否使用非安全连接")
    metric_exporter_type: ExporterType = Field(
        default=ExporterType.CONSOLE, description="Metric 导出器类型"
    )
    metric_export_interval_ms: int = Field(default=30000, description="指标导出间隔（毫秒）")
    batch_export_max_queue_size: int = Field(default=2048, description="批导出最大队列长度")
    batch_export_max_export_batch_size: int = Field(
        default=512, description="批导出每批最大 Span 数"
    )
    batch_export_schedule_delay_ms: int = Field(default=5000, description="批导出调度延迟（毫秒）")
    console_exporter_pretty: bool = Field(default=False, description="控制台导出器是否美化输出")
    enabled: bool = Field(default=True, description="是否启用追踪")


class SpanData(BaseModel):
    """Span 数据快照，可用于序列化和回放。"""

    span_id: str = Field(description="Span ID")
    trace_id: str = Field(description="Trace ID")
    parent_span_id: Optional[str] = Field(default=None, description="父 Span ID")
    name: str = Field(description="Span 名称")
    kind: str = Field(default="INTERNAL", description="Span 类型")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Span 属性")
    start_time: datetime = Field(description="开始时间")
    end_time: Optional[datetime] = Field(default=None, description="结束时间")
    duration_ms: Optional[float] = Field(default=None, description="持续时间（毫秒）")
    status_code: str = Field(default="UNSET", description="状态码")
    status_message: str = Field(default="", description="状态消息")
    events: list[SpanEvent] = Field(default_factory=list, description="Span 事件")
    token_usage: Optional[TokenUsageSnapshot] = Field(default=None, description="Token 消耗信息")


class SpanEvent(BaseModel):
    """Span 事件。"""

    name: str = Field(description="事件名称")
    timestamp: datetime = Field(description="事件时间戳")
    attributes: dict[str, Any] = Field(default_factory=dict, description="事件属性")


class TokenUsageSnapshot(BaseModel):
    """Token 消耗快照。"""

    input_tokens: int = Field(default=0, description="输入 Token 数")
    output_tokens: int = Field(default=0, description="输出 Token 数")
    total_tokens: int = Field(default=0, description="总 Token 数")
    model: str = Field(default="", description="模型名称")
    cost_usd: float = Field(default=0.0, description="费用（美元）")


class TokenHeatmapEntry(BaseModel):
    """Token 消耗热力图数据条目。"""

    component_name: str = Field(description="组件名称（Agent 或工具名）")
    component_type: str = Field(description="组件类型：agent / tool")
    trace_id: str = Field(description="所属 Trace ID")
    span_id: str = Field(description="所属 Span ID")
    input_tokens: int = Field(default=0, description="输入 Token 数")
    output_tokens: int = Field(default=0, description="输出 Token 数")
    total_tokens: int = Field(default=0, description="总 Token 数")
    model: str = Field(default="", description="模型名称")
    cost_usd: float = Field(default=0.0, description="费用（美元）")
    timestamp: datetime = Field(default_factory=datetime.now, description="记录时间")


class TokenHeatmapSummary(BaseModel):
    """Token 消耗热力图汇总。"""

    total_input_tokens: int = Field(default=0, description="总输入 Token 数")
    total_output_tokens: int = Field(default=0, description="总输出 Token 数")
    total_tokens: int = Field(default=0, description="总 Token 数")
    total_cost_usd: float = Field(default=0.0, description="总费用（美元）")
    by_component: dict[str, TokenUsageSnapshot] = Field(
        default_factory=dict, description="按组件汇总"
    )
    entries: list[TokenHeatmapEntry] = Field(default_factory=list, description="详细条目")


class MemorySnapshot(BaseModel):
    """记忆快照 - 在某个 Trace 节点保存的完整记忆状态，用于回放恢复。"""

    snapshot_id: str = Field(default_factory=lambda: str(uuid4()), description="快照 ID")
    trace_id: str = Field(description="关联的 Trace ID")
    span_id: str = Field(description="关联的 Span ID")
    task_id: Optional[str] = Field(default=None, description="关联的任务 ID")
    agent_name: str = Field(default="", description="Agent 名称")
    created_at: datetime = Field(default_factory=datetime.now, description="快照创建时间")
    short_term_memory: list[dict[str, Any]] = Field(
        default_factory=list, description="短期记忆（对话窗口内的消息）"
    )
    long_term_memory: list[dict[str, Any]] = Field(
        default_factory=list, description="长期记忆（向量检索结果）"
    )
    working_memory: dict[str, Any] = Field(
        default_factory=dict, description="工作记忆（当前任务上下文）"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="额外元数据")


class MetricRecord(BaseModel):
    """指标记录。"""

    name: str = Field(description="指标名称")
    metric_type: MetricType = Field(description="指标类型")
    value: float = Field(description="指标值")
    unit: str = Field(default="", description="单位")
    attributes: dict[str, str] = Field(default_factory=dict, description="指标属性")
    timestamp: datetime = Field(default_factory=datetime.now, description="记录时间")


# Fix forward references
SpanData.model_rebuild()


# ---------------------------------------------------------------------------
# Async batch exporter for MemorySnapshots and TokenHeatmapEntries
# ---------------------------------------------------------------------------


class _AsyncBatchCollector:
    """异步批量收集器 - 将 Token 热力图条目和记忆快照异步写入持久化存储。"""

    def __init__(
        self,
        max_batch_size: int = 100,
        flush_interval_seconds: float = 10.0,
        output_dir: Optional[Path] = None,
    ):
        self._max_batch_size = max_batch_size
        self._flush_interval = flush_interval_seconds
        self._output_dir = output_dir

        self._token_entries: list[TokenHeatmapEntry] = []
        self._memory_snapshots: list[MemorySnapshot] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """启动后台刷新任务。"""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("AsyncBatchCollector 启动")

    async def stop(self) -> None:
        """停止后台刷新任务并执行最终刷新。"""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._do_flush()
        logger.info("AsyncBatchCollector 停止")

    async def add_token_entry(self, entry: TokenHeatmapEntry) -> None:
        """添加 Token 热力图条目。"""
        async with self._lock:
            self._token_entries.append(entry)
            if len(self._token_entries) >= self._max_batch_size:
                await self._flush_locked()

    async def add_memory_snapshot(self, snapshot: MemorySnapshot) -> None:
        """添加记忆快照。"""
        async with self._lock:
            self._memory_snapshots.append(snapshot)
            if len(self._memory_snapshots) >= self._max_batch_size:
                await self._flush_locked()

    async def _flush_loop(self) -> None:
        """后台定时刷新循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                async with self._lock:
                    await self._flush_locked()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AsyncBatchCollector 刷新异常: {e}")

    async def _flush_locked(self) -> None:
        """在持锁状态下刷新缓冲区（调用者需已持有 _lock）。"""
        if not self._token_entries and not self._memory_snapshots:
            return
        await self._do_flush()

    async def _do_flush(self) -> None:
        """执行实际的持久化写入。"""
        token_batch: list[TokenHeatmapEntry] = []
        snapshot_batch: list[MemorySnapshot] = []

        async with self._lock:
            if self._token_entries:
                token_batch = self._token_entries[:]
                self._token_entries.clear()
            if self._memory_snapshots:
                snapshot_batch = self._memory_snapshots[:]
                self._memory_snapshots.clear()

        if not token_batch and not snapshot_batch:
            return

        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)

            if token_batch:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = self._output_dir / f"token_heatmap_{ts}.json"
                data = [e.model_dump(mode="json") for e in token_batch]
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.debug(f"写入 {len(token_batch)} 条 Token 热力图条目到 {path}")

            if snapshot_batch:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = self._output_dir / f"memory_snapshot_{ts}.json"
                data = [s.model_dump(mode="json") for s in snapshot_batch]
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.debug(f"写入 {len(snapshot_batch)} 条记忆快照到 {path}")
        else:
            # 无输出目录时仅记录日志
            if token_batch:
                logger.info(f"缓冲 {len(token_batch)} 条 Token 热力图条目（无持久化目录）")
            if snapshot_batch:
                logger.info(f"缓冲 {len(snapshot_batch)} 条记忆快照（无持久化目录）")


# ---------------------------------------------------------------------------
# Custom Span Exporter that captures SpanData for internal use
# ---------------------------------------------------------------------------


class _SpanDataCaptureExporter(SpanExporter if SpanExporter is not None else object):
    """捕获 SpanData 的自定义导出器，用于内部 Span 数据收集。"""

    def __init__(self) -> None:
        self._captured: list[SpanData] = []
        self._lock = asyncio.Lock()

    async def capture(self, span_data: SpanData) -> None:
        """异步捕获一条 SpanData。"""
        async with self._lock:
            self._captured.append(span_data)

    def export(self, spans: Sequence[Any]) -> Any:
        """由 OTel SDK 调用的同步导出方法。"""
        for sdk_span in spans:
            try:
                span_data = self._sdk_span_to_data(sdk_span)
                # 使用 asyncio.create_task 而非阻塞调用
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.capture(span_data))
                except RuntimeError:
                    # 没有运行中的事件循环，同步追加
                    self._captured.append(span_data)
            except Exception as e:
                logger.warning(f"捕获 Span 数据失败: {e}")
        return SpanExportResult.SUCCESS if SpanExportResult is not None else 0

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def get_captured(self) -> list[SpanData]:
        """获取已捕获的 SpanData 列表（线程安全副本）。"""
        return list(self._captured)

    def clear(self) -> None:
        """清空已捕获的数据。"""
        self._captured.clear()

    @staticmethod
    def _sdk_span_to_data(sdk_span: Any) -> SpanData:
        """将 OTel SDK Span 转换为 SpanData 模型。"""
        ctx = sdk_span.get_span_context()
        parent = sdk_span.parent

        start_ns = sdk_span.start_time or 0
        end_ns = sdk_span.end_time or 0
        duration_ms = (end_ns - start_ns) / 1e6 if end_ns > 0 else None

        start_dt = datetime.fromtimestamp(start_ns / 1e9) if start_ns else datetime.now()
        end_dt = datetime.fromtimestamp(end_ns / 1e9) if end_ns else None

        events: list[SpanEvent] = []
        for ev in sdk_span.events or []:
            events.append(
                SpanEvent(
                    name=ev.name,
                    timestamp=datetime.fromtimestamp(ev.timestamp / 1e9),
                    attributes=dict(ev.attributes) if ev.attributes else {},
                )
            )

        status = sdk_span.status
        status_code = status.status_code.name if status else "UNSET"
        status_msg = status.description if status and status.description else ""

        attributes: dict[str, Any] = {}
        if sdk_span.attributes:
            for k, v in sdk_span.attributes.items():
                attributes[k] = v

        token_usage = None
        if "token.input_tokens" in attributes:
            token_usage = TokenUsageSnapshot(
                input_tokens=attributes.get("token.input_tokens", 0),
                output_tokens=attributes.get("token.output_tokens", 0),
                total_tokens=attributes.get("token.total_tokens", 0),
                model=attributes.get("token.model", ""),
                cost_usd=attributes.get("token.cost_usd", 0.0),
            )

        return SpanData(
            span_id=format(ctx.span_id, "016x"),
            trace_id=format(ctx.trace_id, "032x"),
            parent_span_id=format(parent.span_id, "016x") if parent else None,
            name=sdk_span.name,
            kind=sdk_span.kind.name if sdk_span.kind else "INTERNAL",
            attributes=attributes,
            start_time=start_dt,
            end_time=end_dt,
            duration_ms=duration_ms,
            status_code=status_code,
            status_message=status_msg,
            events=events,
            token_usage=token_usage,
        )


# ---------------------------------------------------------------------------
# Core Tracer
# ---------------------------------------------------------------------------


class Tracer:
    """可观测性追踪器。

    集成 OpenTelemetry，提供：
    - 全链路 Trace/Span 管理
    - 自动 Agent/工具 Span 采集
    - Counter / Histogram / Gauge 指标暴露
    - Token 消耗热力图数据
    - 记忆快照回放

    Usage::

        tracer = Tracer(TraceConfig(service_name="my-app"))
        await tracer.start()

        async with tracer.span("my-operation") as span:
            span.set_attribute("key", "value")
            tracer.record_tokens("my-agent", "agent", 100, 50, model="gpt-4")

        await tracer.stop()
    """

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config or TraceConfig()
        self._started = False

        # OTel providers
        self._tracer_provider: Optional[TracerProvider] = None
        self._meter_provider: Optional[MeterProvider] = None
        self._otel_tracer: Optional[trace.Tracer] = None
        self._otel_meter: Optional[metrics.Meter] = None

        # Internal span capture
        self._span_capture_exporter: Optional[_SpanDataCaptureExporter] = None

        # Metrics instruments
        self._counters: dict[str, metrics.Counter] = {}
        self._histograms: dict[str, metrics.Histogram] = {}
        self._gauges: dict[str, metrics.ObservableGauge] = {}

        # Gauge value stores (for ObservableGauge callbacks)
        self._gauge_values: dict[str, float] = {}
        self._gauge_attrs: dict[str, dict[str, str]] = {}

        # Token heatmap
        self._token_heatmap: list[TokenHeatmapEntry] = []
        self._token_heatmap_lock = asyncio.Lock()

        # Memory snapshots
        self._memory_snapshots: dict[str, MemorySnapshot] = {}
        self._memory_snapshots_lock = asyncio.Lock()

        # Async batch collector
        self._batch_collector = _AsyncBatchCollector(
            output_dir=Path("./data/tracer") if self._config.enabled else None,
        )

        # Aggregated metric records
        self._metric_records: list[MetricRecord] = []
        self._metric_records_lock = asyncio.Lock()

        logger.info(
            f"Tracer 初始化: service={self._config.service_name}, exporter={self._config.exporter_type.value}"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动追踪系统。"""
        if self._started:
            logger.warning("Tracer 已经启动")
            return

        if not _OTEL_AVAILABLE:
            logger.warning(
                "opentelemetry 未安装，追踪系统将以降级模式运行。"
                "请安装: pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
            )
            self._started = True
            return

        if not self._config.enabled:
            logger.info("Tracer 已禁用，跳过初始化")
            self._started = True
            return

        resource = Resource.create(
            {
                SERVICE_NAME: self._config.service_name,
                SERVICE_VERSION: self._config.service_version,
                "telemetry.sdk.language": "python",
                "telemetry.sdk.name": "opentelemetry",
            }
        )

        # --- Tracer Provider ---
        self._tracer_provider = TracerProvider(resource=resource)
        self._span_capture_exporter = _SpanDataCaptureExporter()

        # Add span processors
        span_exporter = self._build_span_exporter()
        if span_exporter:
            self._tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    span_exporter,
                    max_queue_size=self._config.batch_export_max_queue_size,
                    max_export_batch_size=self._config.batch_export_max_export_batch_size,
                    schedule_delay_millis=self._config.batch_export_schedule_delay_ms,
                )
            )

        # Always add capture processor
        self._tracer_provider.add_span_processor(
            BatchSpanProcessor(
                self._span_capture_exporter,
                max_queue_size=self._config.batch_export_max_queue_size,
                max_export_batch_size=self._config.batch_export_max_export_batch_size,
                schedule_delay_millis=self._config.batch_export_schedule_delay_ms,
            )
        )

        trace.set_tracer_provider(self._tracer_provider)
        self._otel_tracer = self._tracer_provider.get_tracer(
            self._config.service_name,
            self._config.service_version,
        )

        # --- Meter Provider ---
        metric_exporter = self._build_metric_exporter()
        readers: list[PeriodicExportingMetricReader] = []
        if metric_exporter:
            readers.append(
                PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=self._config.metric_export_interval_ms,
                )
            )
        self._meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(self._meter_provider)
        self._otel_meter = self._meter_provider.get_meter(
            self._config.service_name,
            self._config.service_version,
        )

        # Pre-create well-known metrics
        self._init_default_metrics()

        # Start async batch collector
        await self._batch_collector.start()

        self._started = True
        logger.info(
            f"Tracer 启动完成: service={self._config.service_name}, "
            f"span_exporter={self._config.exporter_type.value}, "
            f"metric_exporter={self._config.metric_exporter_type.value}"
        )

    async def stop(self) -> None:
        """停止追踪系统并刷新所有缓冲数据。"""
        if not self._started:
            return

        # Stop batch collector first
        await self._batch_collector.stop()

        # Shutdown OTel providers
        if self._tracer_provider:
            self._tracer_provider.shutdown()
        if self._meter_provider:
            self._meter_provider.shutdown()

        self._started = False
        logger.info("Tracer 已停止")

    # ------------------------------------------------------------------
    # Exporter builders
    # ------------------------------------------------------------------

    def _build_span_exporter(self) -> Optional[SpanExporter]:
        """根据配置构建 Span 导出器。"""
        if self._config.exporter_type == ExporterType.CONSOLE:
            return ConsoleSpanExporter(
                formatter=self._config.console_exporter_pretty and str or None,
            )
        elif self._config.exporter_type == ExporterType.OTLP_GRPC:
            return OTLPSpanExporter(
                endpoint=self._config.otlp_endpoint,
                insecure=self._config.otlp_insecure,
            )
        elif self._config.exporter_type == ExporterType.NONE:
            return None
        else:
            logger.warning(f"未知的 Span 导出器类型: {self._config.exporter_type}")
            return None

    def _build_metric_exporter(self) -> Optional[MetricExporter]:
        """根据配置构建 Metric 导出器。"""
        if self._config.metric_exporter_type == ExporterType.CONSOLE:
            return ConsoleMetricExporter()
        elif self._config.metric_exporter_type == ExporterType.OTLP_GRPC:
            return OTLPMetricExporter(
                endpoint=self._config.otlp_endpoint,
                insecure=self._config.otlp_insecure,
            )
        elif self._config.metric_exporter_type == ExporterType.NONE:
            return None
        else:
            logger.warning(f"未知的 Metric 导出器类型: {self._config.metric_exporter_type}")
            return None

    # ------------------------------------------------------------------
    # Default metrics
    # ------------------------------------------------------------------

    def _init_default_metrics(self) -> None:
        """初始化默认指标。"""
        if not self._otel_meter:
            return

        # Agent 执行计数
        self._counters["agent_executions_total"] = self._otel_meter.create_counter(
            name="symbio.agent.executions.total",
            description="Agent 执行总次数",
            unit="1",
        )

        # 工具调用计数
        self._counters["tool_calls_total"] = self._otel_meter.create_counter(
            name="symbio.tool.calls.total",
            description="工具调用总次数",
            unit="1",
        )

        # 任务完成计数
        self._counters["tasks_completed_total"] = self._otel_meter.create_counter(
            name="symbio.tasks.completed.total",
            description="任务完成总次数",
            unit="1",
        )

        # 任务失败计数
        self._counters["tasks_failed_total"] = self._otel_meter.create_counter(
            name="symbio.tasks.failed.total",
            description="任务失败总次数",
            unit="1",
        )

        # Agent 执行耗时
        self._histograms["agent_duration_ms"] = self._otel_meter.create_histogram(
            name="symbio.agent.duration_ms",
            description="Agent 执行耗时（毫秒）",
            unit="ms",
        )

        # 工具调用耗时
        self._histograms["tool_duration_ms"] = self._otel_meter.create_histogram(
            name="symbio.tool.duration_ms",
            description="工具调用耗时（毫秒）",
            unit="ms",
        )

        # Token 消耗直方图
        self._histograms["token_usage"] = self._otel_meter.create_histogram(
            name="symbio.token.usage",
            description="Token 消耗量",
            unit="tokens",
        )

        # 任务执行耗时
        self._histograms["task_duration_ms"] = self._otel_meter.create_histogram(
            name="symbio.task.duration_ms",
            description="任务执行耗时（毫秒）",
            unit="ms",
        )

        # 活跃 Trace 数量（Gauge）
        self._gauge_values["active_traces"] = 0.0
        self._otel_meter.create_observable_gauge(
            name="symbio.traces.active",
            description="当前活跃的 Trace 数量",
            unit="1",
            callbacks=[self._gauge_callback_active_traces],
        )

        # 活跃 Span 数量（Gauge）
        self._gauge_values["active_spans"] = 0.0
        self._otel_meter.create_observable_gauge(
            name="symbio.spans.active",
            description="当前活跃的 Span 数量",
            unit="1",
            callbacks=[self._gauge_callback_active_spans],
        )

        # Token 消耗总计（Gauge）
        self._gauge_values["total_tokens_consumed"] = 0.0
        self._otel_meter.create_observable_gauge(
            name="symbio.tokens.total_consumed",
            description="累计 Token 消耗总量",
            unit="tokens",
            callbacks=[self._gauge_callback_total_tokens],
        )

        logger.debug("默认指标初始化完成")

    def _gauge_callback_active_traces(self, options: Any) -> Sequence[metrics.Observation]:
        """活跃 Trace 数 Gauge 回调。"""
        return [metrics.Observation(self._gauge_values.get("active_traces", 0.0))]

    def _gauge_callback_active_spans(self, options: Any) -> Sequence[metrics.Observation]:
        """活跃 Span 数 Gauge 回调。"""
        return [metrics.Observation(self._gauge_values.get("active_spans", 0.0))]

    def _gauge_callback_total_tokens(self, options: Any) -> Sequence[metrics.Observation]:
        """Token 消耗总量 Gauge 回调。"""
        return [metrics.Observation(self._gauge_values.get("total_tokens_consumed", 0.0))]

    # ------------------------------------------------------------------
    # Span management
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[dict[str, Any]] = None,
        links: Optional[list[Any]] = None,
    ):
        """异步 Span 上下文管理器。

        自动管理 Span 的创建、属性设置、异常记录和结束。

        Args:
            name: Span 名称
            kind: Span 类型（INTERNAL, CLIENT, SERVER, PRODUCER, CONSUMER）
            attributes: 初始属性
            links: 关联的 Span 链接

        Yields:
            OTel Span 对象，可在上下文中操作

        Usage::

            async with tracer.span("my-operation", attributes={"key": "value"}) as span:
                span.add_event("step-started")
                # ... do work ...
                span.set_attribute("result", "ok")
        """
        if not self._started or not self._otel_tracer:
            # 未启动时提供 no-op span
            yield _NoOpSpan()
            return

        if not self._config.enabled:
            yield _NoOpSpan()
            return

        # Update active spans gauge
        self._gauge_values["active_spans"] = self._gauge_values.get("active_spans", 0.0) + 1.0

        span = self._otel_tracer.start_span(
            name=name,
            kind=kind,
            attributes=attributes or {},
            links=links or [],
        )

        # Propagate context variables
        ctx = span.get_span_context()
        trace_token = _current_trace_id.set(format(ctx.trace_id, "032x"))
        span_token = _current_span_id.set(format(ctx.span_id, "016x"))

        start_time = time.monotonic()
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            span.set_attribute("error.message", str(exc))
            raise
        finally:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            span.set_attribute("duration_ms", elapsed_ms)
            span.end()
            self._gauge_values["active_spans"] = max(
                0.0, self._gauge_values.get("active_spans", 0.0) - 1.0
            )
            _current_trace_id.reset(trace_token)
            _current_span_id.reset(span_token)

    @contextmanager
    def sync_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[dict[str, Any]] = None,
    ):
        """同步 Span 上下文管理器（用于非异步场景）。

        Args:
            name: Span 名称
            kind: Span 类型
            attributes: 初始属性

        Yields:
            OTel Span 对象
        """
        if not self._started or not self._otel_tracer or not self._config.enabled:
            yield _NoOpSpan()
            return

        self._gauge_values["active_spans"] = self._gauge_values.get("active_spans", 0.0) + 1.0

        span = self._otel_tracer.start_span(
            name=name,
            kind=kind,
            attributes=attributes or {},
        )

        ctx = span.get_span_context()
        trace_token = _current_trace_id.set(format(ctx.trace_id, "032x"))
        span_token = _current_span_id.set(format(ctx.span_id, "016x"))

        start_time = time.monotonic()
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            span.set_attribute("duration_ms", elapsed_ms)
            span.end()
            self._gauge_values["active_spans"] = max(
                0.0, self._gauge_values.get("active_spans", 0.0) - 1.0
            )
            _current_trace_id.reset(trace_token)
            _current_span_id.reset(span_token)

    def get_current_trace_id(self) -> Optional[str]:
        """获取当前上下文的 Trace ID。"""
        return _current_trace_id.get()

    def get_current_span_id(self) -> Optional[str]:
        """获取当前上下文的 Span ID。"""
        return _current_span_id.get()

    # ------------------------------------------------------------------
    # Metrics API
    # ------------------------------------------------------------------

    def increment_counter(
        self,
        name: str,
        value: float = 1.0,
        attributes: Optional[dict[str, str]] = None,
    ) -> None:
        """递增 Counter 指标。

        Args:
            name: 指标名称（如 "agent_executions_total"）
            value: 递增值
            attributes: 指标属性标签
        """
        counter = self._counters.get(name)
        if counter:
            counter.add(value, attributes=attributes or {})
        else:
            logger.warning(f"Counter '{name}' 不存在，尝试动态创建")
            if self._otel_meter:
                counter = self._otel_meter.create_counter(
                    name=f"symbio.custom.{name}",
                    description=f"自定义计数器: {name}",
                    unit="1",
                )
                self._counters[name] = counter
                counter.add(value, attributes=attributes or {})

        # Record for internal aggregation
        self._record_metric(name, MetricType.COUNTER, value, attributes)

    def record_histogram(
        self,
        name: str,
        value: float,
        attributes: Optional[dict[str, str]] = None,
    ) -> None:
        """记录 Histogram 指标。

        Args:
            name: 指标名称（如 "agent_duration_ms"）
            value: 记录值
            attributes: 指标属性标签
        """
        histogram = self._histograms.get(name)
        if histogram:
            histogram.record(value, attributes=attributes or {})
        else:
            logger.warning(f"Histogram '{name}' 不存在，尝试动态创建")
            if self._otel_meter:
                histogram = self._otel_meter.create_histogram(
                    name=f"symbio.custom.{name}",
                    description=f"自定义直方图: {name}",
                    unit="",
                )
                self._histograms[name] = histogram
                histogram.record(value, attributes=attributes or {})

        self._record_metric(name, MetricType.HISTOGRAM, value, attributes)

    def set_gauge(
        self,
        name: str,
        value: float,
        attributes: Optional[dict[str, str]] = None,
    ) -> None:
        """设置 Gauge 指标值。

        Args:
            name: 指标名称（如 "active_traces"）
            value: 当前值
            attributes: 指标属性标签
        """
        self._gauge_values[name] = value
        if attributes:
            self._gauge_attrs[name] = attributes

        self._record_metric(name, MetricType.GAUGE, value, attributes)

    def _record_metric(
        self,
        name: str,
        metric_type: MetricType,
        value: float,
        attributes: Optional[dict[str, str]],
    ) -> None:
        """内部方法：异步记录指标到缓冲区。"""
        record = MetricRecord(
            name=name,
            metric_type=metric_type,
            value=value,
            attributes=attributes or {},
        )
        # Fire-and-forget async record
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._append_metric_record(record))
        except RuntimeError:
            self._metric_records.append(record)

    async def _append_metric_record(self, record: MetricRecord) -> None:
        """异步追加指标记录。"""
        async with self._metric_records_lock:
            self._metric_records.append(record)

    def get_metric_records(self) -> list[MetricRecord]:
        """获取所有已记录的指标快照。"""
        return list(self._metric_records)

    # ------------------------------------------------------------------
    # Token Heatmap
    # ------------------------------------------------------------------

    async def record_tokens(
        self,
        component_name: str,
        component_type: str,
        input_tokens: int,
        output_tokens: int,
        model: str = "",
        cost_usd: float = 0.0,
    ) -> TokenHeatmapEntry:
        """记录 Token 消耗（用于热力图数据）。

        Args:
            component_name: 组件名称（Agent 或工具名）
            component_type: 组件类型（agent / tool）
            input_tokens: 输入 Token 数
            output_tokens: 输出 Token 数
            model: 模型名称
            cost_usd: 费用（美元）

        Returns:
            创建的 TokenHeatmapEntry
        """
        total = input_tokens + output_tokens
        trace_id = self.get_current_trace_id() or "no-trace"
        span_id = self.get_current_span_id() or "no-span"

        entry = TokenHeatmapEntry(
            component_name=component_name,
            component_type=component_type,
            trace_id=trace_id,
            span_id=span_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            model=model,
            cost_usd=cost_usd,
        )

        async with self._token_heatmap_lock:
            self._token_heatmap.append(entry)

        # Update gauge
        self._gauge_values["total_tokens_consumed"] = (
            self._gauge_values.get("total_tokens_consumed", 0.0) + total
        )

        # Record as histogram
        self.record_histogram(
            "token_usage",
            float(total),
            attributes={
                "component": component_name,
                "component_type": component_type,
                "model": model,
            },
        )

        # Persist via batch collector
        await self._batch_collector.add_token_entry(entry)

        logger.debug(
            f"Token 消耗: {component_name} ({component_type}) - "
            f"input={input_tokens}, output={output_tokens}, total={total}, model={model}"
        )
        return entry

    async def get_token_heatmap(
        self,
        trace_id: Optional[str] = None,
    ) -> TokenHeatmapSummary:
        """获取 Token 消耗热力图数据。

        Args:
            trace_id: 可选的 Trace ID 过滤

        Returns:
            TokenHeatmapSummary 汇总数据
        """
        async with self._token_heatmap_lock:
            entries = self._token_heatmap[:]

        if trace_id:
            entries = [e for e in entries if e.trace_id == trace_id]

        # Aggregate by component
        by_component: dict[str, TokenUsageSnapshot] = {}
        total_input = 0
        total_output = 0
        total_all = 0
        total_cost = 0.0

        for entry in entries:
            total_input += entry.input_tokens
            total_output += entry.output_tokens
            total_all += entry.total_tokens
            total_cost += entry.cost_usd

            key = f"{entry.component_type}:{entry.component_name}"
            if key not in by_component:
                by_component[key] = TokenUsageSnapshot(model=entry.model)
            snap = by_component[key]
            snap.input_tokens += entry.input_tokens
            snap.output_tokens += entry.output_tokens
            snap.total_tokens += entry.total_tokens
            snap.cost_usd += entry.cost_usd

        return TokenHeatmapSummary(
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_all,
            total_cost_usd=total_cost,
            by_component=by_component,
            entries=entries,
        )

    # ------------------------------------------------------------------
    # Memory Snapshots
    # ------------------------------------------------------------------

    async def save_memory_snapshot(
        self,
        agent_name: str,
        task_id: Optional[str] = None,
        short_term_memory: Optional[list[dict[str, Any]]] = None,
        long_term_memory: Optional[list[dict[str, Any]]] = None,
        working_memory: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> MemorySnapshot:
        """保存记忆快照。

        在任务关键节点保存完整的记忆状态，用于失败回放和状态恢复。

        Args:
            agent_name: Agent 名称
            task_id: 关联的任务 ID
            short_term_memory: 短期记忆（对话窗口内的消息列表）
            long_term_memory: 长期记忆（向量检索结果列表）
            working_memory: 工作记忆（当前任务上下文）
            metadata: 额外元数据

        Returns:
            创建的 MemorySnapshot
        """
        trace_id = self.get_current_trace_id() or "no-trace"
        span_id = self.get_current_span_id() or "no-span"

        snapshot = MemorySnapshot(
            trace_id=trace_id,
            span_id=span_id,
            task_id=task_id,
            agent_name=agent_name,
            short_term_memory=short_term_memory or [],
            long_term_memory=long_term_memory or [],
            working_memory=working_memory or {},
            metadata=metadata or {},
        )

        async with self._memory_snapshots_lock:
            self._memory_snapshots[snapshot.snapshot_id] = snapshot

        # Persist via batch collector
        await self._batch_collector.add_memory_snapshot(snapshot)

        logger.info(
            f"记忆快照已保存: id={snapshot.snapshot_id}, agent={agent_name}, "
            f"trace={trace_id}, span={span_id}, "
            f"stm={len(snapshot.short_term_memory)}, ltm={len(snapshot.long_term_memory)}"
        )
        return snapshot

    async def get_memory_snapshot(self, snapshot_id: str) -> Optional[MemorySnapshot]:
        """根据 ID 获取记忆快照。

        Args:
            snapshot_id: 快照 ID

        Returns:
            MemorySnapshot 或 None
        """
        async with self._memory_snapshots_lock:
            return self._memory_snapshots.get(snapshot_id)

    async def list_memory_snapshots(
        self,
        trace_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        limit: int = 100,
    ) -> list[MemorySnapshot]:
        """列出记忆快照，支持过滤。

        Args:
            trace_id: 按 Trace ID 过滤
            task_id: 按任务 ID 过滤
            agent_name: 按 Agent 名称过滤
            limit: 最大返回数量

        Returns:
            MemorySnapshot 列表
        """
        async with self._memory_snapshots_lock:
            snapshots = list(self._memory_snapshots.values())

        if trace_id:
            snapshots = [s for s in snapshots if s.trace_id == trace_id]
        if task_id:
            snapshots = [s for s in snapshots if s.task_id == task_id]
        if agent_name:
            snapshots = [s for s in snapshots if s.agent_name == agent_name]

        # Sort by creation time descending
        snapshots.sort(key=lambda s: s.created_at, reverse=True)
        return snapshots[:limit]

    async def restore_memory_from_snapshot(
        self,
        snapshot_id: str,
    ) -> Optional[dict[str, Any]]:
        """从记忆快照恢复状态。

        将快照中的短期记忆、长期记忆和工作记忆恢复为可直接使用的字典格式。

        Args:
            snapshot_id: 快照 ID

        Returns:
            包含恢复状态的字典，如果快照不存在则返回 None

            返回格式::

                {
                    "short_term_memory": [...],
                    "long_term_memory": [...],
                    "working_memory": {...},
                    "metadata": {...},
                    "snapshot_info": {...}
                }
        """
        snapshot = await self.get_memory_snapshot(snapshot_id)
        if not snapshot:
            logger.warning(f"记忆快照不存在: {snapshot_id}")
            return None

        restored = {
            "short_term_memory": snapshot.short_term_memory,
            "long_term_memory": snapshot.long_term_memory,
            "working_memory": snapshot.working_memory,
            "metadata": snapshot.metadata,
            "snapshot_info": {
                "snapshot_id": snapshot.snapshot_id,
                "trace_id": snapshot.trace_id,
                "span_id": snapshot.span_id,
                "task_id": snapshot.task_id,
                "agent_name": snapshot.agent_name,
                "created_at": snapshot.created_at.isoformat(),
            },
        }

        logger.info(f"记忆快照已恢复: id={snapshot_id}, agent={snapshot.agent_name}")
        return restored

    # ------------------------------------------------------------------
    # Trace context propagation (for cross-service / cross-process)
    # ------------------------------------------------------------------

    def inject_context(self, carrier: dict[str, str]) -> None:
        """将当前 Trace 上下文注入到 carrier（如 HTTP header）中。

        Args:
            carrier: 载体字典，通常为 HTTP headers
        """
        inject(carrier)
        logger.debug(f"Trace 上下文已注入: {carrier}")

    def extract_context(self, carrier: dict[str, str]) -> Context:
        """从 carrier 中提取 Trace 上下文。

        Args:
            carrier: 载体字典，通常为 HTTP headers

        Returns:
            OTel Context 对象
        """
        ctx = extract(carrier)
        logger.debug(f"Trace 上下文已提取: {carrier}")
        return ctx

    # ------------------------------------------------------------------
    # Captured Span data access
    # ------------------------------------------------------------------

    async def get_captured_spans(self) -> list[SpanData]:
        """获取所有已捕获的 SpanData。

        Returns:
            SpanData 列表
        """
        if self._span_capture_exporter:
            return self._span_capture_exporter.get_captured()
        return []

    async def get_trace_spans(self, trace_id: str) -> list[SpanData]:
        """获取指定 Trace 的所有 Span。

        Args:
            trace_id: Trace ID

        Returns:
            该 Trace 下的 SpanData 列表，按开始时间排序
        """
        all_spans = await self.get_captured_spans()
        trace_spans = [s for s in all_spans if s.trace_id == trace_id]
        trace_spans.sort(key=lambda s: s.start_time)
        return trace_spans

    async def build_trace_tree(self, trace_id: str) -> dict[str, Any]:
        """构建 Trace 的树形结构。

        将扁平的 Span 列表转换为树形层级结构，便于可视化 DAG。

        Args:
            trace_id: Trace ID

        Returns:
            树形结构字典::

                {
                    "trace_id": "...",
                    "root_spans": [
                        {
                            "span": SpanData,
                            "children": [...]
                        }
                    ]
                }
        """
        spans = await self.get_trace_spans(trace_id)
        if not spans:
            return {"trace_id": trace_id, "root_spans": []}

        # Build parent-child mapping
        span_map: dict[str, SpanData] = {s.span_id: s for s in spans}
        children_map: dict[Optional[str], list[str]] = defaultdict(list)

        for s in spans:
            children_map[s.parent_span_id].append(s.span_id)

        def _build_node(span_id: str) -> dict[str, Any]:
            span = span_map[span_id]
            child_ids = children_map.get(span_id, [])
            return {
                "span": span,
                "children": [_build_node(cid) for cid in child_ids],
            }

        # Root spans have no parent or parent not in this trace
        root_ids = [
            s.span_id for s in spans if s.parent_span_id is None or s.parent_span_id not in span_map
        ]

        return {
            "trace_id": trace_id,
            "root_spans": [_build_node(rid) for rid in root_ids],
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> TraceConfig:
        """当前配置。"""
        return self._config

    @property
    def is_started(self) -> bool:
        """是否已启动。"""
        return self._started

    @property
    def otel_tracer(self) -> Optional[trace.Tracer]:
        """底层 OTel Tracer 实例。"""
        return self._otel_tracer

    @property
    def otel_meter(self) -> Optional[metrics.Meter]:
        """底层 OTel Meter 实例。"""
        return self._otel_meter


# ---------------------------------------------------------------------------
# No-op Span (when tracer is disabled or not started)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """空操作 Span，在追踪禁用时使用。"""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[dict[str, Any]] = None) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass

    def end(self) -> None:
        pass

    def get_span_context(self) -> Any:
        return None


# ---------------------------------------------------------------------------
# Decorators for automatic span creation
# ---------------------------------------------------------------------------


def trace_agent(
    tracer: Tracer,
    agent_name: Optional[str] = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Callable:
    """Agent 执行自动追踪装饰器。

    被装饰的异步方法会自动创建 Span，记录执行耗时、状态和异常。

    Args:
        tracer: Tracer 实例
        agent_name: Agent 名称（默认从方法的 self.name 获取）
        kind: Span 类型

    Usage::

        class MyAgent(BaseAgent):
            @trace_agent(tracer=my_tracer)
            async def execute(self, task: Task) -> Result:
                ...

        # 装饰器会自动生成名为 "agent.{agent_name}" 的 Span
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve agent name
            name = agent_name
            if not name and args and hasattr(args[0], "name"):
                name = args[0].name
            if not name:
                name = func.__qualname__

            span_name = f"agent.{name}"

            # Extract task info if available
            task_id = ""
            task = kwargs.get("task") or (args[1] if len(args) > 1 else None)
            if task and hasattr(task, "task_id"):
                task_id = task.task_id

            attributes = {
                "agent.name": name,
                "agent.method": func.__name__,
                "task.id": task_id,
            }

            async with tracer.span(span_name, kind=kind, attributes=attributes) as span:
                tracer.increment_counter(
                    "agent_executions_total",
                    attributes={"agent": name},
                )

                start = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    elapsed = (time.monotonic() - start) * 1000

                    span.set_attribute("agent.success", True)
                    tracer.record_histogram(
                        "agent_duration_ms",
                        elapsed,
                        attributes={"agent": name, "status": "success"},
                    )

                    # Record token usage if available in result
                    if result and hasattr(result, "token_usage") and result.token_usage:
                        tu = result.token_usage
                        await tracer.record_tokens(
                            component_name=name,
                            component_type="agent",
                            input_tokens=tu.input_tokens,
                            output_tokens=tu.output_tokens,
                            model=getattr(tu, "model", ""),
                            cost_usd=getattr(tu, "cost_usd", 0.0),
                        )
                        span.set_attribute("token.input_tokens", tu.input_tokens)
                        span.set_attribute("token.output_tokens", tu.output_tokens)
                        span.set_attribute("token.total_tokens", tu.total_tokens)

                    return result

                except Exception:
                    elapsed = (time.monotonic() - start) * 1000
                    span.set_attribute("agent.success", False)
                    tracer.increment_counter(
                        "tasks_failed_total",
                        attributes={"agent": name},
                    )
                    tracer.record_histogram(
                        "agent_duration_ms",
                        elapsed,
                        attributes={"agent": name, "status": "error"},
                    )
                    raise

        return wrapper

    return decorator


def trace_task(
    tracer: Tracer,
    task_name: Optional[str] = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Callable:
    """任务级自动追踪装饰器。

    被装饰的异步函数会自动创建 Span，记录任务 ID、执行耗时和结果。
    适合包裹 Orchestrator 的任务执行入口。

    Args:
        tracer: Tracer 实例
        task_name: 任务名称（默认从函数名获取）
        kind: Span 类型

    Usage::

        @trace_task(tracer=my_tracer, task_name="data-analysis")
        async def run_analysis(task_id: str, query: str) -> Result:
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = task_name or func.__name__
            span_name = f"task.{name}"

            # 尝试提取 task_id
            task_id = kwargs.get("task_id", "")
            if not task_id and args:
                for arg in args:
                    if isinstance(arg, str) and len(arg) > 8:
                        task_id = arg
                        break

            attributes = {
                "task.name": name,
                "task.id": str(task_id),
                "task.method": func.__name__,
            }

            async with tracer.span(span_name, kind=kind, attributes=attributes) as span:
                tracer.increment_counter(
                    "tasks_completed_total",
                    attributes={"task": name},
                )

                start = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    elapsed = (time.monotonic() - start) * 1000

                    span.set_attribute("task.success", True)
                    tracer.record_histogram(
                        "task_duration_ms",
                        elapsed,
                        attributes={"task": name, "status": "success"},
                    )

                    # 记录 token 消耗（如果结果中包含）
                    if result and hasattr(result, "token_usage") and result.token_usage:
                        tu = result.token_usage
                        await tracer.record_tokens(
                            component_name=name,
                            component_type="task",
                            input_tokens=getattr(tu, "input_tokens", 0),
                            output_tokens=getattr(tu, "output_tokens", 0),
                            model=getattr(tu, "model", ""),
                            cost_usd=getattr(tu, "cost_usd", 0.0),
                        )

                    return result

                except Exception:
                    elapsed = (time.monotonic() - start) * 1000
                    span.set_attribute("task.success", False)
                    tracer.increment_counter(
                        "tasks_failed_total",
                        attributes={"task": name},
                    )
                    tracer.record_histogram(
                        "task_duration_ms",
                        elapsed,
                        attributes={"task": name, "status": "error"},
                    )
                    raise

        return wrapper

    return decorator


def trace_tool(
    tracer: Tracer,
    tool_name: Optional[str] = None,
    kind: SpanKind = SpanKind.CLIENT,
) -> Callable:
    """工具调用自动追踪装饰器。

    被装饰的异步方法会自动创建 Span，记录工具名称、参数摘要、执行耗时和结果。

    Args:
        tracer: Tracer 实例
        tool_name: 工具名称（默认从函数名获取）
        kind: Span 类型

    Usage::

        @trace_tool(tracer=my_tracer, tool_name="web_search")
        async def search(query: str) -> str:
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = tool_name or func.__name__
            span_name = f"tool.{name}"

            # Build parameter summary (truncate long values)
            params_summary: dict[str, str] = {}
            for k, v in kwargs.items():
                val_str = str(v)
                params_summary[k] = val_str[:200] + "..." if len(val_str) > 200 else val_str

            attributes = {
                "tool.name": name,
                "tool.method": func.__name__,
            }

            async with tracer.span(span_name, kind=kind, attributes=attributes) as span:
                tracer.increment_counter(
                    "tool_calls_total",
                    attributes={"tool": name},
                )

                # Add params as event (not attributes, to avoid high cardinality)
                span.add_event(
                    "tool.params", {"params": json.dumps(params_summary, ensure_ascii=False)[:1000]}
                )

                start = time.monotonic()
                try:
                    result = await func(*args, **kwargs)
                    elapsed = (time.monotonic() - start) * 1000

                    span.set_attribute("tool.success", True)
                    tracer.record_histogram(
                        "tool_duration_ms",
                        elapsed,
                        attributes={"tool": name, "status": "success"},
                    )

                    # Record result summary
                    result_str = str(result)[:500] if result else ""
                    span.add_event("tool.result", {"result_preview": result_str})

                    return result

                except Exception:
                    elapsed = (time.monotonic() - start) * 1000
                    span.set_attribute("tool.success", False)
                    tracer.record_histogram(
                        "tool_duration_ms",
                        elapsed,
                        attributes={"tool": name, "status": "error"},
                    )
                    raise

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Module-level singleton and convenience functions
# ---------------------------------------------------------------------------

_global_tracer: Optional[Tracer] = None


def get_tracer() -> Optional[Tracer]:
    """获取全局 Tracer 实例。"""
    return _global_tracer


def init_tracer(config: Optional[TraceConfig] = None) -> Tracer:
    """初始化全局 Tracer 实例。

    Args:
        config: 追踪配置

    Returns:
        Tracer 实例
    """
    global _global_tracer
    if _global_tracer is not None:
        logger.warning("全局 Tracer 已存在，将被替换")
    _global_tracer = Tracer(config)
    logger.info("全局 Tracer 已初始化")
    return _global_tracer


async def shutdown_tracer() -> None:
    """关闭全局 Tracer。"""
    global _global_tracer
    if _global_tracer:
        await _global_tracer.stop()
        _global_tracer = None
        logger.info("全局 Tracer 已关闭")


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    # Enums
    "ExporterType",
    "MetricType",
    # Data models
    "TraceConfig",
    "SpanData",
    "SpanEvent",
    "TokenUsageSnapshot",
    "TokenHeatmapEntry",
    "TokenHeatmapSummary",
    "MemorySnapshot",
    "MetricRecord",
    # Core
    "Tracer",
    # Decorators
    "trace_agent",
    "trace_task",
    "trace_tool",
    # Singleton
    "get_tracer",
    "init_tracer",
    "shutdown_tracer",
]
