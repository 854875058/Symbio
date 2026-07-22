"""审计日志模块 - 操作日志记录与查询"""

from __future__ import annotations

import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("security.audit")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class AuditLevel(str, Enum):
    """审计级别"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditAction(str, Enum):
    """审计动作类型"""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    ACCESS = "access"
    EXPORT = "export"
    IMPORT = "import"
    CONFIG_CHANGE = "config_change"
    PERMISSION_CHANGE = "permission_change"
    CUSTOM = "custom"


class AuditOutcome(str, Enum):
    """操作结果"""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    ERROR = "error"


class AuditEntry(BaseModel):
    """审计日志条目"""

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.now)
    level: AuditLevel = AuditLevel.INFO
    action: AuditAction = AuditAction.CUSTOM
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    actor: str = ""  # 操作者
    target: str = ""  # 操作对象
    description: str = ""  # 操作描述
    details: dict[str, Any] = Field(default_factory=dict)
    source_ip: str = ""
    session_id: str = ""
    request_id: str = ""
    duration_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditQuery(BaseModel):
    """审计日志查询条件"""

    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    levels: list[AuditLevel] = Field(default_factory=list)
    actions: list[AuditAction] = Field(default_factory=list)
    outcomes: list[AuditOutcome] = Field(default_factory=list)
    actor: Optional[str] = None
    target: Optional[str] = None
    keyword: Optional[str] = None
    limit: int = Field(default=100, ge=1)
    offset: int = Field(default=0, ge=0)


class AuditQueryResult(BaseModel):
    """审计查询结果"""

    entries: list[AuditEntry] = Field(default_factory=list)
    total: int = 0
    has_more: bool = False


class AuditStatistics(BaseModel):
    """审计统计信息"""

    total_entries: int = 0
    entries_by_level: dict[str, int] = Field(default_factory=dict)
    entries_by_action: dict[str, int] = Field(default_factory=dict)
    entries_by_outcome: dict[str, int] = Field(default_factory=dict)
    entries_by_actor: dict[str, int] = Field(default_factory=dict)
    time_range: dict[str, Optional[str]] = Field(default_factory=dict)
    failure_rate: float = 0.0


# ---------------------------------------------------------------------------
# 审计日志写入器
# ---------------------------------------------------------------------------


class AuditWriter:
    """审计日志写入器 - 线程安全的日志持久化"""

    def __init__(
        self,
        log_dir: str | Path = "./audit_logs",
        max_entries_per_file: int = 10000,
        flush_interval: float = 5.0,
    ):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._max_entries_per_file = max_entries_per_file
        self._flush_interval = flush_interval

        self._buffer: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._current_file_index = 0
        self._entries_in_current_file = 0
        self._closed = False

        # 恢复文件索引
        existing_files = sorted(self._log_dir.glob("audit_*.jsonl"))
        if existing_files:
            self._current_file_index = len(existing_files)
            # 检查最后一个文件的行数
            last_file = existing_files[-1]
            with open(last_file, "r", encoding="utf-8") as f:
                self._entries_in_current_file = sum(1 for _ in f)

    def write(self, entry: AuditEntry) -> None:
        """写入审计条目

        Args:
            entry: 审计日志条目
        """
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= 100:
                self._flush()

    def _flush(self) -> None:
        """将缓冲区内容写入文件"""
        if not self._buffer:
            return

        entries_to_write = self._buffer[:]
        self._buffer.clear()

        for entry in entries_to_write:
            if self._entries_in_current_file >= self._max_entries_per_file:
                self._current_file_index += 1
                self._entries_in_current_file = 0

            file_path = self._log_dir / f"audit_{self._current_file_index:06d}.jsonl"
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
            self._entries_in_current_file += 1

    def flush(self) -> None:
        """手动刷新缓冲区"""
        with self._lock:
            self._flush()

    def close(self) -> None:
        """关闭写入器"""
        self._closed = True
        self.flush()

    def read_entries(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[AuditEntry]:
        """从文件读取审计条目"""
        self.flush()

        entries: list[AuditEntry] = []
        log_files = sorted(self._log_dir.glob("audit_*.jsonl"))

        for log_file in log_files:
            if len(entries) >= limit:
                break
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = AuditEntry.model_validate_json(line)
                        if start_time and entry.timestamp < start_time:
                            continue
                        if end_time and entry.timestamp > end_time:
                            continue
                        entries.append(entry)
                        if len(entries) >= limit:
                            break
                    except Exception as exc:
                        logger.warning(f"解析审计条目失败: {exc}")

        return entries


# ---------------------------------------------------------------------------
# 审计查询引擎
# ---------------------------------------------------------------------------


class AuditQueryEngine:
    """审计日志查询引擎"""

    def __init__(self, writer: AuditWriter):
        self._writer = writer

    def query(self, query: AuditQuery) -> AuditQueryResult:
        """查询审计日志

        Args:
            query: 查询条件

        Returns:
            查询结果
        """
        # 读取所有可能匹配的条目
        all_entries = self._writer.read_entries(
            start_time=query.start_time,
            end_time=query.end_time,
            limit=10000,
        )

        # 过滤
        filtered: list[AuditEntry] = []
        for entry in all_entries:
            if query.levels and entry.level not in query.levels:
                continue
            if query.actions and entry.action not in query.actions:
                continue
            if query.outcomes and entry.outcome not in query.outcomes:
                continue
            if query.actor and query.actor not in entry.actor:
                continue
            if query.target and query.target not in entry.target:
                continue
            if query.keyword:
                searchable = f"{entry.description} {entry.actor} {entry.target}"
                if query.keyword.lower() not in searchable.lower():
                    continue
            filtered.append(entry)

        total = len(filtered)
        # 排序: 按时间倒序
        filtered.sort(key=lambda e: e.timestamp, reverse=True)
        # 分页
        paginated = filtered[query.offset : query.offset + query.limit]

        return AuditQueryResult(
            entries=paginated,
            total=total,
            has_more=(query.offset + query.limit) < total,
        )


# ---------------------------------------------------------------------------
# 审计日志管理器
# ---------------------------------------------------------------------------


class AuditLogger:
    """审计日志管理器

    提供统一的操作日志记录、查询和统计功能。

    用法:
        audit = AuditLogger()
        audit.log(AuditAction.LOGIN, actor="user-1", outcome=AuditOutcome.SUCCESS)
        result = audit.query(AuditQuery(actor="user-1"))
    """

    def __init__(
        self,
        log_dir: str | Path = "./audit_logs",
        enable_console: bool = False,
    ):
        self._writer = AuditWriter(log_dir=log_dir)
        self._query_engine = AuditQueryEngine(self._writer)
        self._enable_console = enable_console
        self._entries_cache: list[AuditEntry] = []

    def log(
        self,
        action: AuditAction,
        actor: str = "",
        target: str = "",
        description: str = "",
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        level: AuditLevel = AuditLevel.INFO,
        details: dict[str, Any] | None = None,
        source_ip: str = "",
        session_id: str = "",
        request_id: str = "",
        duration_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录审计日志

        Args:
            action: 操作类型
            actor: 操作者
            target: 操作对象
            description: 操作描述
            outcome: 操作结果
            level: 审计级别
            details: 操作详情
            source_ip: 来源 IP
            session_id: 会话 ID
            request_id: 请求 ID
            duration_ms: 耗时 (毫秒)
            metadata: 附加元数据

        Returns:
            创建的审计条目
        """
        entry = AuditEntry(
            level=level,
            action=action,
            outcome=outcome,
            actor=actor,
            target=target,
            description=description,
            details=details or {},
            source_ip=source_ip,
            session_id=session_id,
            request_id=request_id,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

        self._writer.write(entry)
        self._entries_cache.append(entry)

        if self._enable_console:
            log_msg = (
                f"[AUDIT] {entry.level.value.upper()} | {entry.action.value} | "
                f"actor={entry.actor} target={entry.target} outcome={entry.outcome.value} | "
                f"{entry.description}"
            )
            if level in (AuditLevel.ERROR, AuditLevel.CRITICAL):
                logger.error(log_msg)
            elif level == AuditLevel.WARNING:
                logger.warning(log_msg)
            else:
                logger.info(log_msg)

        return entry

    def log_success(
        self,
        action: AuditAction,
        actor: str = "",
        target: str = "",
        description: str = "",
        **kwargs: Any,
    ) -> AuditEntry:
        """记录成功操作"""
        return self.log(
            action=action,
            actor=actor,
            target=target,
            description=description,
            outcome=AuditOutcome.SUCCESS,
            **kwargs,
        )

    def log_failure(
        self,
        action: AuditAction,
        actor: str = "",
        target: str = "",
        description: str = "",
        error: str = "",
        **kwargs: Any,
    ) -> AuditEntry:
        """记录失败操作"""
        details = kwargs.pop("details", {})
        if error:
            details["error"] = error
        return self.log(
            action=action,
            actor=actor,
            target=target,
            description=description,
            outcome=AuditOutcome.FAILURE,
            level=AuditLevel.WARNING,
            details=details,
            **kwargs,
        )

    def log_access(
        self,
        actor: str,
        target: str,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        **kwargs: Any,
    ) -> AuditEntry:
        """记录访问日志"""
        return self.log(
            action=AuditAction.ACCESS,
            actor=actor,
            target=target,
            description=f"{actor} 访问 {target}",
            outcome=outcome,
            **kwargs,
        )

    def log_config_change(
        self,
        actor: str,
        target: str,
        old_value: Any = None,
        new_value: Any = None,
        **kwargs: Any,
    ) -> AuditEntry:
        """记录配置变更"""
        return self.log(
            action=AuditAction.CONFIG_CHANGE,
            actor=actor,
            target=target,
            description=f"配置变更: {target}",
            details={"old_value": str(old_value), "new_value": str(new_value)},
            **kwargs,
        )

    def query(self, query: AuditQuery) -> AuditQueryResult:
        """查询审计日志"""
        return self._query_engine.query(query)

    def get_statistics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> AuditStatistics:
        """获取审计统计信息"""
        entries = self._writer.read_entries(
            start_time=start_time,
            end_time=end_time,
            limit=100000,
        )

        stats = AuditStatistics(total_entries=len(entries))

        if not entries:
            return stats

        # 按级别统计
        for entry in entries:
            level_key = entry.level.value
            stats.entries_by_level[level_key] = stats.entries_by_level.get(level_key, 0) + 1

            action_key = entry.action.value
            stats.entries_by_action[action_key] = stats.entries_by_action.get(action_key, 0) + 1

            outcome_key = entry.outcome.value
            stats.entries_by_outcome[outcome_key] = stats.entries_by_outcome.get(outcome_key, 0) + 1

            if entry.actor:
                stats.entries_by_actor[entry.actor] = stats.entries_by_actor.get(entry.actor, 0) + 1

        # 时间范围
        timestamps = [e.timestamp for e in entries]
        stats.time_range = {
            "earliest": min(timestamps).isoformat() if timestamps else None,
            "latest": max(timestamps).isoformat() if timestamps else None,
        }

        # 失败率
        failures = stats.entries_by_outcome.get("failure", 0) + stats.entries_by_outcome.get(
            "error", 0
        )
        stats.failure_rate = failures / len(entries) if entries else 0.0

        return stats

    def flush(self) -> None:
        """刷新缓冲区"""
        self._writer.flush()

    def close(self) -> None:
        """关闭审计日志"""
        self._writer.close()
