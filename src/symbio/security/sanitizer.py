"""数据脱敏模块 - PII 检测和脱敏处理"""

from __future__ import annotations

import os
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("security.sanitizer")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class PIIType(str, Enum):
    """PII 数据类型"""

    PHONE = "phone"
    EMAIL = "email"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    NAME = "name"
    ADDRESS = "address"
    IP_ADDRESS = "ip_address"
    PASSPORT = "passport"
    LICENSE_PLATE = "license_plate"
    SSN = "ssn"
    CUSTOM = "custom"


class SanitizeStrategy(str, Enum):
    """脱敏策略"""

    MASK = "mask"  # 掩码替换 (如 138****1234)
    HASH = "hash"  # 哈希替换
    REPLACE = "replace"  # 固定替换
    REDACT = "redact"  # 完全移除
    TOKENIZE = "tokenize"  # 令牌化
    ENCRYPT = "encrypt"  # 加密


class SanitizeRule(BaseModel):
    """脱敏规则"""

    rule_id: str = Field(default_factory=lambda: str(uuid4()))
    pii_type: PIIType
    strategy: SanitizeStrategy
    pattern: str = ""
    replacement: str = "***"
    mask_char: str = "*"
    mask_left: int = 3
    mask_right: int = 4
    enabled: bool = True
    description: str = ""


class PIIMatch(BaseModel):
    """PII 检测结果"""

    match_id: str = Field(default_factory=lambda: str(uuid4()))
    pii_type: PIIType
    value: str
    start: int
    end: int
    confidence: float = Field(ge=0.0, le=1.0)
    context: str = ""


class SanitizeResult(BaseModel):
    """脱敏结果"""

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    original_length: int = 0
    sanitized_text: str = ""
    matches_found: int = 0
    matches: list[PIIMatch] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)


class SanitizeReport(BaseModel):
    """脱敏报告"""

    report_id: str = Field(default_factory=lambda: str(uuid4()))
    total_texts: int = 0
    total_matches: int = 0
    matches_by_type: dict[str, int] = Field(default_factory=dict)
    results: list[SanitizeResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# PII 检测器
# ---------------------------------------------------------------------------


class PIIDetector:
    """PII 检测器 - 通过正则表达式检测各类个人信息"""

    # 内置检测模式
    BUILTIN_PATTERNS: dict[PIIType, str] = {
        PIIType.PHONE: r"(?<!\d)1[3-9]\d{9}(?!\d)",
        PIIType.EMAIL: r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        PIIType.ID_CARD: r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
        PIIType.BANK_CARD: r"(?<!\d)[1-9]\d{15,18}(?!\d)",
        PIIType.IP_ADDRESS: r"(?<!\d)(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)",
        PIIType.PASSPORT: r"(?<![A-Za-z0-9])[A-Za-z]\d{8}(?![A-Za-z0-9])",
        PIIType.LICENSE_PLATE: r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]",
        PIIType.SSN: r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    }

    def __init__(self, custom_patterns: dict[PIIType, str] | None = None):
        self._patterns: dict[PIIType, re.Pattern[str]] = {}
        # 编译内置模式
        for pii_type, pattern in self.BUILTIN_PATTERNS.items():
            self._patterns[pii_type] = re.compile(pattern)
        # 编译自定义模式
        if custom_patterns:
            for pii_type, pattern in custom_patterns.items():
                self._patterns[pii_type] = re.compile(pattern)

    def detect(self, text: str, pii_types: list[PIIType] | None = None) -> list[PIIMatch]:
        """检测文本中的 PII

        Args:
            text: 输入文本
            pii_types: 指定检测的 PII 类型, None 则检测所有类型

        Returns:
            检测到的 PII 列表
        """
        matches: list[PIIMatch] = []
        types_to_check = pii_types or list(self._patterns.keys())

        for pii_type in types_to_check:
            pattern = self._patterns.get(pii_type)
            if not pattern:
                continue

            for m in pattern.finditer(text):
                context_start = max(0, m.start() - 10)
                context_end = min(len(text), m.end() + 10)
                matches.append(
                    PIIMatch(
                        pii_type=pii_type,
                        value=m.group(),
                        start=m.start(),
                        end=m.end(),
                        confidence=self._compute_confidence(pii_type, m.group()),
                        context=text[context_start:context_end],
                    )
                )

        # 按位置排序
        matches.sort(key=lambda x: x.start)
        return matches

    def _compute_confidence(self, pii_type: PIIType, value: str) -> float:
        """计算检测置信度"""
        if pii_type == PIIType.PHONE:
            return 0.95 if len(value) == 11 else 0.7
        elif pii_type == PIIType.EMAIL:
            return 0.9
        elif pii_type == PIIType.ID_CARD:
            return 0.95 if len(value) == 18 else 0.7
        elif pii_type == PIIType.BANK_CARD:
            return 0.85 if 16 <= len(value) <= 19 else 0.6
        elif pii_type == PIIType.IP_ADDRESS:
            return 0.85
        else:
            return 0.7

    def add_pattern(self, pii_type: PIIType, pattern: str) -> None:
        """添加自定义检测模式"""
        self._patterns[pii_type] = re.compile(pattern)
        logger.info(f"添加 PII 检测模式: {pii_type.value}")


# ---------------------------------------------------------------------------
# 脱敏执行器
# ---------------------------------------------------------------------------


class SanitizeExecutor:
    """脱敏执行器 - 按规则对 PII 进行脱敏处理"""

    def __init__(
        self,
        rules: list[SanitizeRule] | None = None,
        encrypt_key: str | bytes | None = None,
        key_path: str | Path | None = None,
    ):
        self._rules: dict[PIIType, SanitizeRule] = {}
        self._encrypt_key = encrypt_key
        self._key_path = key_path
        self._cipher = None  # 懒加载 Fernet
        if rules:
            for rule in rules:
                self._rules[rule.pii_type] = rule
        else:
            self._init_default_rules()

    # -- 真加密（ENCRYPT 策略，可逆）---------------------------------------

    def _resolve_encrypt_key(self) -> bytes:
        """解析 Fernet 密钥：显式传入 > 环境变量 > 落盘文件（不存在则生成并持久化）。"""
        if self._encrypt_key:
            key = self._encrypt_key
            return key.encode() if isinstance(key, str) else key
        env_key = os.environ.get("SYMBIO_SANITIZE_KEY")
        if env_key:
            return env_key.encode()
        path = Path(self._key_path) if self._key_path else Path("data") / "sanitizer.key"
        if path.exists():
            return path.read_bytes().strip()
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        return key

    def _get_cipher(self):
        if self._cipher is None:
            from cryptography.fernet import Fernet

            self._cipher = Fernet(self._resolve_encrypt_key())
        return self._cipher

    def decrypt(self, token: str) -> str:
        """还原 ENCRYPT 策略加密的值；非密文/解密失败时原样返回。"""
        if not isinstance(token, str) or not token.startswith("ENC:"):
            return token
        try:
            return self._get_cipher().decrypt(token[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return token

    def _init_default_rules(self) -> None:
        """初始化默认脱敏规则"""
        defaults = [
            SanitizeRule(
                pii_type=PIIType.PHONE,
                strategy=SanitizeStrategy.MASK,
                mask_left=3,
                mask_right=4,
                description="手机号脱敏: 保留前3后4",
            ),
            SanitizeRule(
                pii_type=PIIType.EMAIL,
                strategy=SanitizeStrategy.MASK,
                mask_left=2,
                mask_right=0,
                description="邮箱脱敏: 保留前2字符",
            ),
            SanitizeRule(
                pii_type=PIIType.ID_CARD,
                strategy=SanitizeStrategy.MASK,
                mask_left=4,
                mask_right=4,
                description="身份证脱敏: 保留前4后4",
            ),
            SanitizeRule(
                pii_type=PIIType.BANK_CARD,
                strategy=SanitizeStrategy.MASK,
                mask_left=4,
                mask_right=4,
                description="银行卡脱敏: 保留前4后4",
            ),
            SanitizeRule(
                pii_type=PIIType.IP_ADDRESS,
                strategy=SanitizeStrategy.REPLACE,
                replacement="[IP_REDACTED]",
                description="IP 地址替换",
            ),
            SanitizeRule(
                pii_type=PIIType.PASSPORT,
                strategy=SanitizeStrategy.MASK,
                mask_left=1,
                mask_right=2,
                description="护照脱敏: 保留首字母和后2位",
            ),
            SanitizeRule(
                pii_type=PIIType.LICENSE_PLATE,
                strategy=SanitizeStrategy.MASK,
                mask_left=2,
                mask_right=2,
                description="车牌脱敏: 保留前2后2",
            ),
            SanitizeRule(
                pii_type=PIIType.SSN,
                strategy=SanitizeStrategy.MASK,
                mask_left=0,
                mask_right=4,
                description="SSN 脱敏: 保留后4位",
            ),
        ]
        for rule in defaults:
            self._rules[rule.pii_type] = rule

    def sanitize_value(self, value: str, rule: SanitizeRule) -> str:
        """对单个值执行脱敏

        Args:
            value: 原始值
            rule: 脱敏规则

        Returns:
            脱敏后的值
        """
        if rule.strategy == SanitizeStrategy.MASK:
            return self._mask(value, rule)
        elif rule.strategy == SanitizeStrategy.HASH:
            return self._hash(value)
        elif rule.strategy == SanitizeStrategy.REPLACE:
            return rule.replacement
        elif rule.strategy == SanitizeStrategy.REDACT:
            return ""
        elif rule.strategy == SanitizeStrategy.TOKENIZE:
            return self._tokenize(value)
        elif rule.strategy == SanitizeStrategy.ENCRYPT:
            return self._encrypt(value)
        else:
            return rule.replacement

    def _mask(self, value: str, rule: SanitizeRule) -> str:
        """掩码处理"""
        if not value:
            return value

        left_count = min(rule.mask_left, len(value))
        right_count = min(rule.mask_right, len(value) - left_count)

        if left_count + right_count >= len(value):
            return rule.mask_char * len(value)

        left = value[:left_count]
        right = value[-right_count:] if right_count > 0 else ""
        middle = rule.mask_char * (len(value) - left_count - right_count)
        return left + middle + right

    def _hash(self, value: str) -> str:
        """哈希替换"""
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _tokenize(self, value: str) -> str:
        """令牌化"""
        return f"TOK_{uuid4().hex[:8]}"

    def _encrypt(self, value: str) -> str:
        """真加密（Fernet 对称加密，可经 decrypt 还原）；crypto 不可用时回退占位。"""
        try:
            token = self._get_cipher().encrypt((value or "").encode("utf-8")).decode("ascii")
            return f"ENC:{token}"
        except Exception as exc:  # pragma: no cover - 仅在 cryptography 缺失时
            logger.warning(f"加密失败，回退占位: {exc}")
            return f"ENC_{uuid4().hex[:12]}"

    def add_rule(self, rule: SanitizeRule) -> None:
        """添加脱敏规则"""
        self._rules[rule.pii_type] = rule
        logger.info(f"添加脱敏规则: {rule.pii_type.value} -> {rule.strategy.value}")

    def get_rule(self, pii_type: PIIType) -> SanitizeRule | None:
        """获取脱敏规则"""
        return self._rules.get(pii_type)


# ---------------------------------------------------------------------------
# 数据脱敏器
# ---------------------------------------------------------------------------


class DataSanitizer:
    """数据脱敏器

    集成 PII 检测和脱敏处理, 支持文本、字典和列表数据。

    用法:
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize_text("我的手机号是13812345678")
        # result.sanitized_text == "我的手机号是138****5678"
    """

    def __init__(
        self,
        detector: PIIDetector | None = None,
        executor: SanitizeExecutor | None = None,
    ):
        self.detector = detector or PIIDetector()
        self.executor = executor or SanitizeExecutor()
        self._results: list[SanitizeResult] = []

    def sanitize_text(
        self,
        text: str,
        pii_types: list[PIIType] | None = None,
    ) -> SanitizeResult:
        """对文本执行脱敏

        Args:
            text: 输入文本
            pii_types: 指定处理的 PII 类型

        Returns:
            脱敏结果
        """
        result = SanitizeResult(original_length=len(text))

        # 检测 PII
        matches = self.detector.detect(text, pii_types)
        result.matches = matches
        result.matches_found = len(matches)

        if not matches:
            result.sanitized_text = text
            self._results.append(result)
            return result

        # 从后向前替换, 避免偏移量变化
        sanitized = text
        for match in reversed(matches):
            rule = self.executor.get_rule(match.pii_type)
            if rule and rule.enabled:
                replacement = self.executor.sanitize_value(match.value, rule)
                sanitized = sanitized[: match.start] + replacement + sanitized[match.end :]
                result.rules_applied.append(rule.rule_id)

        result.sanitized_text = sanitized
        self._results.append(result)

        logger.info(
            f"文本脱敏完成: 发现 {len(matches)} 处 PII, 应用 {len(result.rules_applied)} 条规则"
        )
        return result

    def sanitize_dict(
        self,
        data: dict[str, Any],
        sensitive_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """对字典数据执行脱敏

        Args:
            data: 输入字典
            sensitive_keys: 敏感字段名列表 (对这些字段的值做全文脱敏)

        Returns:
            脱敏后的字典
        """
        result: dict[str, Any] = {}
        sensitive_keys = sensitive_keys or []

        for key, value in data.items():
            if isinstance(value, str):
                if key in sensitive_keys:
                    sanitized = self.sanitize_text(value)
                    result[key] = sanitized.sanitized_text
                else:
                    result[key] = value
            elif isinstance(value, dict):
                result[key] = self.sanitize_dict(value, sensitive_keys)
            elif isinstance(value, list):
                result[key] = self.sanitize_list(value, sensitive_keys)
            else:
                result[key] = value

        return result

    def sanitize_list(
        self,
        data: list[Any],
        sensitive_keys: list[str] | None = None,
    ) -> list[Any]:
        """对列表数据执行脱敏"""
        result: list[Any] = []
        for item in data:
            if isinstance(item, str):
                sanitized = self.sanitize_text(item)
                result.append(sanitized.sanitized_text)
            elif isinstance(item, dict):
                result.append(self.sanitize_dict(item, sensitive_keys))
            elif isinstance(item, list):
                result.append(self.sanitize_list(item, sensitive_keys))
            else:
                result.append(item)
        return result

    def generate_report(self) -> SanitizeReport:
        """生成脱敏报告"""
        report = SanitizeReport(
            total_texts=len(self._results),
            total_matches=sum(r.matches_found for r in self._results),
        )

        # 按类型统计
        type_counts: dict[str, int] = {}
        for result in self._results:
            for match in result.matches:
                type_name = match.pii_type.value
                type_counts[type_name] = type_counts.get(type_name, 0) + 1
        report.matches_by_type = type_counts
        report.results = list(self._results)

        return report

    def clear_results(self) -> None:
        """清空结果"""
        self._results.clear()
