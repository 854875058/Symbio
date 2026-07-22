"""Memory Security Gateway - 记忆安全网关

整合 PII 检测和注入攻击防护，为记忆系统提供统一的安全检查。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.core.injection_guard import InjectionGuard, ThreatLevel
from symbio.security.sanitizer import DataSanitizer, PIIDetector, PIIMatch
from symbio.utils.logger import get_logger

logger = get_logger("security.gateway")


class SecurityAction(str, Enum):
    """安全处置动作"""

    ALLOW = "allow"
    BLOCK = "block"
    SANITIZE = "sanitize"
    QUARANTINE = "quarantine"


class SecurityCheckResult(BaseModel):
    """安全检查结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    original_content: str = ""
    sanitized_content: str = ""
    is_safe: bool = True
    action: SecurityAction = SecurityAction.ALLOW
    pii_matches: list[PIIMatch] = Field(default_factory=list)
    injection_threat: ThreatLevel = ThreatLevel.SAFE
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)


class SecurityStats(BaseModel):
    """安全统计"""

    total_checked: int = 0
    total_allowed: int = 0
    total_blocked: int = 0
    total_sanitized: int = 0
    pii_detections: int = 0
    injection_detections: int = 0


class MemorySecurityGateway:
    """记忆安全网关

    职责：
    1. PII 检测与脱敏
    2. 注入攻击检测与拦截
    3. 综合安全评估与决策

    Usage:
        gateway = MemorySecurityGateway()
        result = gateway.check("用户手机号是 13812345678")
        if not result.is_safe:
            print(f"Blocked: {result.reasons}")
    """

    def __init__(
        self,
        pii_detector: Optional[PIIDetector] = None,
        sanitizer: Optional[DataSanitizer] = None,
        injection_guard: Optional[InjectionGuard] = None,
        block_on_injection: bool = True,
        auto_sanitize_pii: bool = True,
    ):
        self._pii_detector = pii_detector or PIIDetector()
        self._sanitizer = sanitizer or DataSanitizer()
        self._injection_guard = injection_guard or InjectionGuard()
        self._block_on_injection = block_on_injection
        self._auto_sanitize_pii = auto_sanitize_pii
        self._stats = SecurityStats()
        logger.info("MemorySecurityGateway 创建")

    def check(self, content: str) -> SecurityCheckResult:
        """执行安全检查

        Args:
            content: 待检查的内容

        Returns:
            安全检查结果
        """
        self._stats.total_checked += 1
        reasons: list[str] = []
        is_safe = True
        action = SecurityAction.ALLOW
        sanitized_content = content

        # 1. PII 检测
        pii_matches = self._pii_detector.detect(content)
        if pii_matches:
            self._stats.pii_detections += 1
            reasons.append(f"PII detected: {len(pii_matches)} matches")
            if self._auto_sanitize_pii:
                sanitize_result = self._sanitizer.sanitize_text(content)
                sanitized_content = sanitize_result.sanitized_text
                action = SecurityAction.SANITIZE
                reasons.append("PII auto-sanitized")

        # 2. 注入攻击检测
        injection_record = self._injection_guard.analyze(content)
        if injection_record.threat_level not in (ThreatLevel.SAFE, ThreatLevel.LOW):
            self._stats.injection_detections += 1
            is_safe = False
            reasons.append(
                f"Injection threat: {injection_record.threat_level.value}, "
                f"type: {injection_record.attack_type.value}"
            )
            if self._block_on_injection:
                action = SecurityAction.BLOCK
            else:
                action = SecurityAction.QUARANTINE

        # 更新统计
        if action == SecurityAction.ALLOW:
            self._stats.total_allowed += 1
        elif action == SecurityAction.BLOCK:
            self._stats.total_blocked += 1
        elif action == SecurityAction.SANITIZE:
            self._stats.total_sanitized += 1

        return SecurityCheckResult(
            original_content=content,
            sanitized_content=sanitized_content,
            is_safe=is_safe,
            action=action,
            pii_matches=pii_matches,
            injection_threat=injection_record.threat_level,
            reasons=reasons,
        )

    def get_stats(self) -> SecurityStats:
        """获取统计"""
        return self._stats.model_copy()

    def reset_stats(self) -> None:
        """重置统计"""
        self._stats = SecurityStats()
