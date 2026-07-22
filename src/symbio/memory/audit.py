"""记忆审计系统 - 检测 PII、注入攻击、过期和低质量记忆

集成 DataSanitizer（PII 检测）和 InjectionGuard（注入检测），
对 MemoryItem 进行全面审计，支持批量报告生成和自动清理。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.core.injection_guard import InjectionGuard, ThreatLevel
from symbio.memory.manager import MemoryItem, MemoryStatus
from symbio.security.sanitizer import DataSanitizer, PIIMatch
from symbio.utils.logger import get_logger

logger = get_logger("memory.audit")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class AuditFlag(str, Enum):
    """审计标记类型"""

    PII_DETECTED = "pii_detected"  # 检测到个人信息
    INJECTION_DETECTED = "injection_detected"  # 检测到注入攻击模式
    STALE = "stale"  # 记忆过期（超过30天未访问）
    LOW_IMPORTANCE = "low_importance"  # 重要性过低（< 0.2）
    DUPLICATE = "duplicate"  # 重复记忆
    EXPIRED = "expired"  # 短期记忆已过期


class Severity(str, Enum):
    """问题严重程度"""

    INFO = "info"  # 信息性提示
    WARNING = "warning"  # 警告
    CRITICAL = "critical"  # 严重，需要立即处理


class AuditItem(BaseModel):
    """单条审计发现"""

    item_id: str = Field(default_factory=lambda: str(uuid4()))
    flag: AuditFlag
    severity: Severity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AuditResult(BaseModel):
    """单条记忆的审计结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    memory_id: str
    content_preview: str = ""  # 内容摘要（前100字符）
    is_clean: bool = True  # 是否通过审计
    flags: list[AuditItem] = Field(default_factory=list)
    pii_matches: list[PIIMatch] = Field(default_factory=list)
    threat_level: str = "safe"  # 注入检测威胁等级
    timestamp: datetime = Field(default_factory=datetime.now)

    @property
    def has_critical(self) -> bool:
        """是否存在严重问题"""
        return any(f.severity == Severity.CRITICAL for f in self.flags)

    @property
    def flag_summary(self) -> dict[str, int]:
        """按类型统计标记数量"""
        summary: dict[str, int] = {}
        for flag in self.flags:
            key = flag.flag.value
            summary[key] = summary.get(key, 0) + 1
        return summary


class AuditReport(BaseModel):
    """批量审计聚合报告"""

    report_id: str = Field(default_factory=lambda: str(uuid4()))
    total_audited: int = 0
    total_clean: int = 0
    total_flagged: int = 0
    total_critical: int = 0
    flags_by_type: dict[str, int] = Field(default_factory=dict)
    severity_distribution: dict[str, int] = Field(default_factory=dict)
    top_issues: list[str] = Field(default_factory=list)  # 出现频率最高的问题
    results: list[AuditResult] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.now)

    @property
    def clean_rate(self) -> float:
        """通过率"""
        if self.total_audited == 0:
            return 1.0
        return self.total_clean / self.total_audited

    @property
    def critical_rate(self) -> float:
        """严重问题率"""
        if self.total_audited == 0:
            return 0.0
        return self.total_critical / self.total_audited


class CleanupAction(str, Enum):
    """清理动作"""

    KEEP = "keep"  # 保留
    ARCHIVE = "archive"  # 归档
    REMOVE = "remove"  # 移除
    SANITIZE = "sanitize"  # 脱敏后保留


class CleanupDetail(BaseModel):
    """单条清理详情"""

    memory_id: str
    action: CleanupAction
    reason: str
    original_content_preview: str = ""


class CleanupResult(BaseModel):
    """清理结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    total_processed: int = 0
    kept: int = 0
    archived: int = 0
    removed: int = 0
    sanitized: int = 0
    details: list[CleanupDetail] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 记忆审计器
# ---------------------------------------------------------------------------


class MemoryAuditor:
    """记忆审计器

    对 MemoryItem 进行全面安全和质量审计：
    1. PII 检测 - 通过 DataSanitizer 检测个人信息
    2. 注入检测 - 通过 InjectionGuard 检测注入攻击模式
    3. 过期检测 - 检查记忆是否超过30天未访问
    4. 低质量检测 - 检查重要性评分是否过低
    5. 重复检测 - 基于内容哈希检测重复记忆

    Usage:
        auditor = MemoryAuditor()
        result = auditor.audit_memory(memory_item)
        report = auditor.generate_report(memory_list)
        cleanup = auditor.cleanup(report, memory_list)
    """

    def __init__(
        self,
        sanitizer: Optional[DataSanitizer] = None,
        injection_guard: Optional[InjectionGuard] = None,
        staleness_days: int = 30,
        low_importance_threshold: float = 0.2,
    ):
        """初始化审计器

        Args:
            sanitizer: PII 检测器，默认使用 DataSanitizer
            injection_guard: 注入检测器，默认使用 InjectionGuard
            staleness_days: 过期天数阈值，默认30天
            low_importance_threshold: 低重要性阈值，默认0.2
        """
        self._sanitizer = sanitizer or DataSanitizer()
        self._injection_guard = injection_guard or InjectionGuard.create_default()
        self._staleness_days = staleness_days
        self._low_importance_threshold = low_importance_threshold
        self._content_hashes: dict[str, str] = {}  # content_hash -> memory_id

        logger.info(
            f"MemoryAuditor 创建: staleness_days={staleness_days}, "
            f"low_importance_threshold={low_importance_threshold}"
        )

    def audit_memory(
        self,
        memory_item: MemoryItem,
        *,
        check_pii: bool = True,
        check_injection: bool = True,
        check_staleness: bool = True,
        check_importance: bool = True,
        check_duplicate: bool = True,
    ) -> AuditResult:
        """审计单条记忆

        Args:
            memory_item: 待审计的记忆条目
            check_pii: 是否检查 PII
            check_injection: 是否检查注入
            check_staleness: 是否检查过期
            check_importance: 是否检查重要性
            check_duplicate: 是否检查重复

        Returns:
            审计结果
        """
        result = AuditResult(
            memory_id=memory_item.memory_id,
            content_preview=memory_item.content[:100],
        )
        flags: list[AuditItem] = []
        pii_matches: list[PIIMatch] = []
        threat_level = "safe"

        # 1. PII 检测
        if check_pii:
            pii_flags, matches = self._check_pii(memory_item)
            flags.extend(pii_flags)
            pii_matches = matches

        # 2. 注入检测
        if check_injection:
            injection_flags, detected_level = self._check_injection(memory_item)
            flags.extend(injection_flags)
            threat_level = detected_level

        # 3. 过期检测
        if check_staleness:
            staleness_flags = self._check_staleness(memory_item)
            flags.extend(staleness_flags)

        # 4. 低重要性检测
        if check_importance:
            importance_flags = self._check_importance(memory_item)
            flags.extend(importance_flags)

        # 5. 重复检测
        if check_duplicate:
            duplicate_flags = self._check_duplicate(memory_item)
            flags.extend(duplicate_flags)

        # 6. 短期记忆过期检测
        if memory_item.is_expired():
            flags.append(
                AuditItem(
                    flag=AuditFlag.EXPIRED,
                    severity=Severity.WARNING,
                    message=f"短期记忆已过期: expires_at={memory_item.expires_at}",
                )
            )

        result.flags = flags
        result.pii_matches = pii_matches
        result.threat_level = threat_level
        result.is_clean = len(flags) == 0

        if result.is_clean:
            logger.debug(f"记忆审计通过: {memory_item.memory_id}")
        else:
            flag_types = [f.flag.value for f in flags]
            logger.info(f"记忆审计发现问题: {memory_item.memory_id}, flags={flag_types}")

        return result

    def generate_report(self, memories: list[MemoryItem]) -> AuditReport:
        """生成批量审计报告

        Args:
            memories: 待审计的记忆列表

        Returns:
            聚合审计报告
        """
        # 先重置重复检测哈希表
        self._content_hashes.clear()

        results: list[AuditResult] = []
        for memory in memories:
            result = self.audit_memory(memory)
            results.append(result)

        # 统计聚合
        total = len(results)
        clean = sum(1 for r in results if r.is_clean)
        flagged = total - clean
        critical = sum(1 for r in results if r.has_critical)

        # 按标记类型统计
        flags_by_type: dict[str, int] = {}
        for result in results:
            for flag in result.flags:
                key = flag.flag.value
                flags_by_type[key] = flags_by_type.get(key, 0) + 1

        # 按严重程度统计
        severity_dist: dict[str, int] = {}
        for result in results:
            for flag in result.flags:
                key = flag.severity.value
                severity_dist[key] = severity_dist.get(key, 0) + 1

        # 找出出现频率最高的问题（前5个）
        sorted_issues = sorted(flags_by_type.items(), key=lambda x: x[1], reverse=True)
        top_issues = [f"{k} ({v}次)" for k, v in sorted_issues[:5]]

        report = AuditReport(
            total_audited=total,
            total_clean=clean,
            total_flagged=flagged,
            total_critical=critical,
            flags_by_type=flags_by_type,
            severity_distribution=severity_dist,
            top_issues=top_issues,
            results=results,
        )

        logger.info(
            f"审计报告生成: total={total}, clean={clean}, "
            f"flagged={flagged}, critical={critical}, "
            f"clean_rate={report.clean_rate:.1%}"
        )

        return report

    def cleanup(
        self,
        audit_report: AuditReport,
        memories: list[MemoryItem],
        *,
        remove_critical: bool = True,
        archive_stale: bool = True,
        sanitize_pii: bool = False,
    ) -> CleanupResult:
        """根据审计结果清理记忆

        策略：
        - 严重问题（注入检测到高威胁）-> 移除
        - PII 检测到 -> 脱敏或移除
        - 过期记忆 -> 归档
        - 低重要性 -> 归档
        - 其他 -> 保留

        Args:
            audit_report: 审计报告
            memories: 原始记忆列表
            remove_critical: 是否移除严重问题记忆
            archive_stale: 是否归档过期记忆
            sanitize_pii: 是否对 PII 记忆做脱敏（否则移除）

        Returns:
            清理结果
        """
        # 构建 memory_id -> AuditResult 映射
        result_map: dict[str, AuditResult] = {}
        for result in audit_report.results:
            result_map[result.memory_id] = result

        # 构建 memory_id -> MemoryItem 映射
        memory_map: dict[str, MemoryItem] = {}
        for memory in memories:
            memory_map[memory.memory_id] = memory

        cleanup_result = CleanupResult(total_processed=len(memories))

        for memory in memories:
            audit_result = result_map.get(memory.memory_id)

            # 无审计结果或通过审计 -> 保留
            if audit_result is None or audit_result.is_clean:
                cleanup_result.kept += 1
                cleanup_result.details.append(
                    CleanupDetail(
                        memory_id=memory.memory_id,
                        action=CleanupAction.KEEP,
                        reason="通过审计",
                        original_content_preview=memory.content[:50],
                    )
                )
                continue

            action = CleanupAction.KEEP
            reason = ""

            # 检查严重问题（注入攻击）
            if remove_critical and audit_result.has_critical:
                # 检查是否有注入检测
                injection_flags = [
                    f for f in audit_result.flags if f.flag == AuditFlag.INJECTION_DETECTED
                ]
                if injection_flags:
                    action = CleanupAction.REMOVE
                    reason = f"检测到注入攻击: threat_level={audit_result.threat_level}"

            # 检查 PII
            if action == CleanupAction.KEEP and audit_result.pii_matches:
                if sanitize_pii:
                    action = CleanupAction.SANITIZE
                    pii_types = list(set(m.pii_type.value for m in audit_result.pii_matches))
                    reason = f"检测到 PII，执行脱敏: types={pii_types}"
                else:
                    action = CleanupAction.REMOVE
                    pii_types = list(set(m.pii_type.value for m in audit_result.pii_matches))
                    reason = f"检测到 PII: types={pii_types}"

            # 检查过期
            if action == CleanupAction.KEEP and archive_stale:
                stale_flags = [
                    f for f in audit_result.flags if f.flag in (AuditFlag.STALE, AuditFlag.EXPIRED)
                ]
                if stale_flags:
                    action = CleanupAction.ARCHIVE
                    reason = "记忆过期或长期未访问"

            # 检查低重要性
            if action == CleanupAction.KEEP:
                low_imp_flags = [
                    f for f in audit_result.flags if f.flag == AuditFlag.LOW_IMPORTANCE
                ]
                if low_imp_flags:
                    action = CleanupAction.ARCHIVE
                    reason = f"重要性过低: {memory.importance}"

            # 执行清理动作
            if action == CleanupAction.REMOVE:
                memory.status = MemoryStatus.FORGOTTEN
                cleanup_result.removed += 1
            elif action == CleanupAction.ARCHIVE:
                memory.status = MemoryStatus.ARCHIVED
                cleanup_result.archived += 1
            elif action == CleanupAction.SANITIZE:
                sanitized = self._sanitizer.sanitize_text(memory.content)
                memory.content = sanitized.sanitized_text
                cleanup_result.sanitized += 1
            else:
                cleanup_result.kept += 1

            cleanup_result.details.append(
                CleanupDetail(
                    memory_id=memory.memory_id,
                    action=action,
                    reason=reason,
                    original_content_preview=memory.content[:50],
                )
            )

        logger.info(
            f"记忆清理完成: total={cleanup_result.total_processed}, "
            f"kept={cleanup_result.kept}, archived={cleanup_result.archived}, "
            f"removed={cleanup_result.removed}, sanitized={cleanup_result.sanitized}"
        )

        return cleanup_result

    # ------------------------------------------------------------------
    # 内部检测方法
    # ------------------------------------------------------------------

    def _check_pii(self, memory: MemoryItem) -> tuple[list[AuditItem], list[PIIMatch]]:
        """检测 PII 内容"""
        flags: list[AuditItem] = []
        matches = self._sanitizer.detector.detect(memory.content)

        if matches:
            pii_types = list(set(m.pii_type.value for m in matches))
            flags.append(
                AuditItem(
                    flag=AuditFlag.PII_DETECTED,
                    severity=Severity.CRITICAL,
                    message=f"检测到个人信息: types={pii_types}, count={len(matches)}",
                    details={
                        "pii_types": pii_types,
                        "match_count": len(matches),
                    },
                )
            )

        return flags, matches

    def _check_injection(self, memory: MemoryItem) -> tuple[list[AuditItem], str]:
        """检测注入攻击模式"""
        flags: list[AuditItem] = []
        record = self._injection_guard.analyze(memory.content)
        threat_level = record.threat_level.value

        if record.threat_level in (ThreatLevel.HIGH, ThreatLevel.CRITICAL):
            flags.append(
                AuditItem(
                    flag=AuditFlag.INJECTION_DETECTED,
                    severity=Severity.CRITICAL,
                    message=(
                        f"检测到注入攻击模式: "
                        f"threat={threat_level}, "
                        f"attack={record.attack_type.value}"
                    ),
                    details={
                        "threat_level": threat_level,
                        "attack_type": record.attack_type.value,
                        "action_taken": record.action_taken,
                    },
                )
            )
        elif record.threat_level == ThreatLevel.MEDIUM:
            flags.append(
                AuditItem(
                    flag=AuditFlag.INJECTION_DETECTED,
                    severity=Severity.WARNING,
                    message=(
                        f"检测到可疑注入模式: "
                        f"threat={threat_level}, "
                        f"attack={record.attack_type.value}"
                    ),
                    details={
                        "threat_level": threat_level,
                        "attack_type": record.attack_type.value,
                    },
                )
            )

        return flags, threat_level

    def _check_staleness(self, memory: MemoryItem) -> list[AuditItem]:
        """检测记忆过期（超过阈值天数未访问）"""
        flags: list[AuditItem] = []

        if memory.last_accessed is None:
            return flags

        now = datetime.now()
        days_since_access = (now - memory.last_accessed).days

        if days_since_access > self._staleness_days:
            flags.append(
                AuditItem(
                    flag=AuditFlag.STALE,
                    severity=Severity.WARNING,
                    message=(
                        f"记忆超过 {self._staleness_days} 天未访问: "
                        f"last_accessed={memory.last_accessed.isoformat()}, "
                        f"days_since={days_since_access}"
                    ),
                    details={
                        "last_accessed": memory.last_accessed.isoformat(),
                        "days_since_access": days_since_access,
                        "staleness_threshold": self._staleness_days,
                    },
                )
            )

        return flags

    def _check_importance(self, memory: MemoryItem) -> list[AuditItem]:
        """检测低重要性"""
        flags: list[AuditItem] = []

        if memory.importance < self._low_importance_threshold:
            flags.append(
                AuditItem(
                    flag=AuditFlag.LOW_IMPORTANCE,
                    severity=Severity.INFO,
                    message=(
                        f"记忆重要性过低: "
                        f"importance={memory.importance}, "
                        f"threshold={self._low_importance_threshold}"
                    ),
                    details={
                        "importance": memory.importance,
                        "threshold": self._low_importance_threshold,
                    },
                )
            )

        return flags

    def _check_duplicate(self, memory: MemoryItem) -> list[AuditItem]:
        """检测重复记忆（基于内容哈希）"""
        flags: list[AuditItem] = []

        content_hash = hashlib.sha256(memory.content.strip().encode("utf-8")).hexdigest()

        if content_hash in self._content_hashes:
            existing_id = self._content_hashes[content_hash]
            flags.append(
                AuditItem(
                    flag=AuditFlag.DUPLICATE,
                    severity=Severity.WARNING,
                    message=(
                        f"检测到重复记忆: current={memory.memory_id}, duplicate_of={existing_id}"
                    ),
                    details={
                        "content_hash": content_hash,
                        "duplicate_of": existing_id,
                    },
                )
            )
        else:
            self._content_hashes[content_hash] = memory.memory_id

        return flags

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> MemoryAuditor:
        """使用默认配置创建"""
        return cls()

    @classmethod
    def create_strict(cls) -> MemoryAuditor:
        """创建严格审计模式（更低阈值，更短过期时间）"""
        return cls(
            staleness_days=14,
            low_importance_threshold=0.3,
        )

    @classmethod
    def create_lenient(cls) -> MemoryAuditor:
        """创建宽松审计模式（更高阈值，更长过期时间）"""
        return cls(
            staleness_days=90,
            low_importance_threshold=0.1,
        )


# ---------------------------------------------------------------------------
# Module-level re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "AuditFlag",
    "AuditItem",
    "AuditReport",
    "AuditResult",
    "CleanupAction",
    "CleanupDetail",
    "CleanupResult",
    "MemoryAuditor",
    "Severity",
]
