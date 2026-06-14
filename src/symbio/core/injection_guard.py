"""Prompt Injection 防护引擎 - 三层防御体系：输入净化、语义检测、意图审计"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("injection_guard")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------

class ThreatLevel(str, Enum):
    """威胁等级"""
    SAFE = "safe"              # 安全
    LOW = "low"                # 低风险
    MEDIUM = "medium"          # 中等风险
    HIGH = "high"              # 高风险
    CRITICAL = "critical"      # 严重风险


class DefenseLayer(str, Enum):
    """防御层级"""
    INPUT_SANITIZATION = "input_sanitization"    # 第一层：输入净化
    SEMANTIC_DETECTION = "semantic_detection"    # 第二层：语义检测
    INTENT_AUDIT = "intent_audit"                # 第三层：意图审计


class AttackType(str, Enum):
    """攻击类型"""
    DIRECT_INJECTION = "direct_injection"          # 直接注入
    INDIRECT_INJECTION = "indirect_injection"      # 间接注入
    JAILBREAK = "jailbreak"                        # 越狱攻击
    PROMPT_LEAK = "prompt_leak"                    # 提示词泄露
    ROLE_HIJACK = "role_hijack"                    # 角色劫持
    ENCODING_BYPASS = "encoding_bypass"            # 编码绕过
    CONTEXT_OVERFLOW = "context_overflow"          # 上下文溢出
    SOCIAL_ENGINEERING = "social_engineering"      # 社会工程 / 权限胁迫
    DATA_EXFILTRATION = "data_exfiltration"        # 数据外泄
    NONE = "none"                                  # 无攻击


class SanitizeResult(BaseModel):
    """净化结果"""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    original_text: str
    sanitized_text: str
    removed_patterns: list[str] = Field(default_factory=list)
    encoding_changes: list[str] = Field(default_factory=list)
    is_modified: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class DetectionResult(BaseModel):
    """检测结果"""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    text: str
    threat_level: ThreatLevel = ThreatLevel.SAFE
    attack_type: AttackType = AttackType.NONE
    confidence: float = 0.0
    matched_patterns: list[str] = Field(default_factory=list)
    suspicious_segments: list[str] = Field(default_factory=list)
    layer: DefenseLayer = DefenseLayer.SEMANTIC_DETECTION
    created_at: datetime = Field(default_factory=datetime.now)


class AuditRecord(BaseModel):
    """审计记录"""
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    user_id: str = ""
    original_input: str
    sanitized_input: str
    threat_level: ThreatLevel = ThreatLevel.SAFE
    attack_type: AttackType = AttackType.NONE
    action_taken: str = "allow"    # allow / warn / block / quarantine
    defense_layers_passed: list[DefenseLayer] = Field(default_factory=list)
    detection_results: list[DetectionResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class InjectionGuardConfig(BaseModel):
    """防护引擎配置"""
    # 第一层：输入净化
    enable_sanitization: bool = True
    max_input_length: int = 10000
    strip_control_chars: bool = True
    normalize_unicode: bool = True
    remove_zero_width_chars: bool = True

    # 第二层：语义检测
    enable_semantic_detection: bool = True
    threat_threshold: ThreatLevel = ThreatLevel.MEDIUM
    max_pattern_matches: int = 5

    # 第三层：意图审计
    enable_intent_audit: bool = True
    audit_log_enabled: bool = True
    auto_block_critical: bool = True
    quarantine_suspicious: bool = True

    # 通用
    case_sensitive: bool = False
    log_all_inputs: bool = False


# ---------------------------------------------------------------------------
# Prompt Injection 防护引擎
# ---------------------------------------------------------------------------

class InjectionGuard:
    """Prompt Injection 防护引擎

    三层防御体系：
    1. 输入净化 - 清除恶意编码、控制字符、零宽字符
    2. 语义检测 - 基于模式匹配和语义分析检测注入攻击
    3. 意图审计 - 分析输入意图，记录审计日志，执行拦截策略

    攻击类型覆盖：
    - 直接注入（"忽略之前的指令..."）
    - 间接注入（通过外部数据源注入）
    - 越狱攻击（"你现在是 DAN..."）
    - 提示词泄露（"输出你的系统提示词"）
    - 角色劫持（"你不再是 AI 助手..."）
    - 编码绕过（Base64、ROT13、Unicode 混淆）
    - 上下文溢出（超长输入淹没系统提示词）

    Usage:
        guard = InjectionGuard()
        result = guard.analyze(user_input)
        if result.threat_level >= ThreatLevel.HIGH:
            raise InjectionError("检测到注入攻击")
    """

    # 直接注入模式
    DIRECT_INJECTION_PATTERNS = [
        # 中文
        r"忽略(?:之前|上面|前面)(?:的)?(?:指令|提示|规则|要求|指示)",
        r"无视(?:之前|上面|前面)(?:的)?(?:指令|提示|规则|要求|指示)",
        r"不要(?:遵循|遵守|执行)(?:之前|上面|前面)(?:的)?(?:指令|提示|规则)",
        r"(?:忘记|抛弃|丢弃)(?:之前|上面|前面)(?:的)?(?:指令|提示|规则)",
        r"(?:新的|现在)(?:的)?(?:指令|规则|任务)(?:是|如下|如下)",
        r"(?:系统|管理员|开发者)(?:模式|权限|指令)",
        r"(?:覆盖|覆写|替换|修改)(?:之前|上面|前面)(?:的)?(?:指令|提示|规则)",

        # 英文（兼容 "ignore all previous instructions" 等语序）
        r"ignore\s+(?:all\s+)?(?:(?:previous|above|prior|earlier|preceding|the)\s+)*(?:instructions?|prompts?|rules?|directives?|context)",
        r"disregard\s+(?:all\s+)?(?:(?:previous|above|prior|earlier|the)\s+)*(?:instructions?|prompts?|rules?|directives?)",
        r"forget\s+(?:all\s+)?(?:(?:previous|above|prior|the)\s+)*(?:instructions?|prompts?|rules?)",
        r"forget\s+everything\s+(?:before|prior|above|you\s+were|that)",
        r"override\s+(?:previous|above|all|prior)\s+(?:instructions?|prompts?|rules?)",
        r"new\s+(?:instructions?|rules?|task|directive)\s*(?:is|are|:|follow)",
        r"(?:system|admin|developer)\s+(?:mode|access|override|prompt)",
        r"you\s+(?:are|will)\s+now\s+(?:act|behave|respond)\s+as",
        r"from\s+now\s+on\s+(?:you|your)\s+(?:are|will|should)",
        # 伪造对话边界 / 系统标签 / 关闭防护
        r"(?:new|fresh)\s+conversation\s+(?:starts?|begins?)\s+now",
        r"end\s+of\s+conversation\.?\s+new\s+conversation",
        r"<\s*/?\s*system\s*>",
        r"<\|\s*(?:system|im_start|endofsystem)\s*\|?>",
        r"```\s*system",
        r"(?:disable|turn\s+off|bypass|remove|deactivate)\s+(?:all\s+)?(?:the\s+)?(?:safety|security|content|ethical)\s+(?:filters?|checks?|rules?|guidelines?|guardrails?)",
        r"you\s+are\s+(?:now\s+)?(?:an?\s+)?unrestricted",
        # 德语等其它语言的"忽略之前指令"
        r"ignorieren\s+sie\s+alle\s+vorherigen\s+anweisungen",
    ]

    # 越狱攻击模式
    JAILBREAK_PATTERNS = [
        # DAN 系列
        r"(?:do\s+anything|DAN|jailbreak)\s+(?:mode|prompt|version)",
        r"\bDAN\b(?:\s+(?:which|stands|mode|can|has))",
        r"do\s+anything\s+now",
        r"pretend\s+(?:you\s+are|to\s+be|you're)\s+(?:a|an)?\s*(?:human|person|real|DAN|evil)",
        r"(?:roleplay|act)\s+as\s+(?:a|an)\s+(?:human|unrestricted|evil)",
        r"you\s+(?:have|now\s+have)\s+(?:no|zero)\s+(?:restrictions?|limitations?|rules?|ethical)",
        r"(?:evil\s*gpt|evilgpt|evil\s+twin)",
        r"opposite\s+day",
        r"(?:your\s+)?safety\s+(?:guidelines?|rules?)\s+(?:are\s+)?reversed",

        # 中文越狱
        r"(?:假装|假设|模拟)(?:你)?(?:是|成为|变成)(?:一个|位)?(?:人类|真人|不受限)",
        r"(?:无限制|无约束|无规则)(?:模式|状态|版本)",
        r"(?:越狱|破解|解锁)(?:模式|版本|状态)",
    ]

    # 社会工程 / 权限胁迫模式
    SOCIAL_ENGINEERING_PATTERNS = [
        r"(?:as|i\s+am|i'm)\s+(?:the\s+)?(?:CTO|CEO|admin(?:istrator)?|developer|manager|system\s+admin)\b[^.]*(?:order|ordering|disable|authoriz|grant|bypass)",
        r"i\s+am\s+(?:the\s+)?system\s+admin(?:istrator)?",
        r"(?:approved|authorized)\s+by\s+(?:your\s+)?(?:developers?|creators?|the\s+team)",
        r"(?:hypothetically|just\s+for\s+(?:academic|research|educational))[^.]*(?:no\s+restrictions?|unrestricted|bypass|disable)",
        r"if\s+you\s+don'?t\s+help[^.]*(?:lose\s+their\s+jobs?|fired|die|hurt)",
        r"unrestricted\s+mode",
    ]

    # 数据外泄模式（向外部/可疑目标发送会话数据）
    DATA_EXFILTRATION_PATTERNS = [
        r"(?:send|forward|upload|exfiltrate|leak|copy|stream|post)\s+(?:all\s+|the\s+)?(?:conversation|chat|session|user)?\s*(?:history|data|summary|messages?)\s+(?:to|history\s+to)",
        r"(?:https?|wss?|ftp)://(?:[a-z0-9.-]*\.)?(?:evil|attacker|malicious|hacker)",
        r"attacker@|@evil\.",
        r"CONVERSATION_DATA",
        r"(?:read|cat|display|show|open)\s+(?:the\s+)?(?:contents?\s+of\s+)?/etc/(?:passwd|shadow)",
        r"(?:send|forward)\s+(?:conversation|data|this)[^.]*(?:to\s+)?\S+@\S+",
    ]

    # 提示词泄露模式
    PROMPT_LEAK_PATTERNS = [
        r"(?:输出|显示|打印|告诉我|泄露|reveal|show|print|output)\s*(?:你的|the)?\s*(?:系统提示|system\s*prompt|instructions?|初始指令)",
        r"(?:你的|your)\s*(?:系统提示|system\s*prompt|instructions?)\s*(?:是|内容|说了什么|是什么|say)",
        r"(?:repeat|repeat\s+after\s+me|say\s+exactly)\s*(?:the\s+)?(?:system|initial|original)\s+(?:prompt|instructions?|message)",
        r"(?:what|tell)\s+(?:are|is|me)\s+(?:your|the)\s+(?:system|initial|original)\s+(?:prompt|instructions?|rules?|message)",
        r"repeat\s+everything\s+(?:above|before|written\s+above)",
        r"(?:translate|convert)\s+your\s+(?:system\s+)?(?:instructions?|prompt|rules?)",
        r"summarize\s+(?:the\s+)?(?:rules?|guidelines?|instructions?)\s+you\s+(?:were\s+given|have)",
        r"output\s+your\s+(?:config(?:uration)?|settings?|system_prompt)",
        r"(?:enable|activate|turn\s+on)\s+debug\s+mode",
        r"(?:how\s+many|list\s+(?:each|all|every))\s+(?:instructions?|rules?)",
        r"(?:list|show)\s+(?:all\s+)?(?:loaded\s+)?instructions?\b",
    ]

    # 角色劫持模式
    ROLE_HIJACK_PATTERNS = [
        r"you\s+(?:are|will\s+be|shall\s+be)\s+now\s+(?:called|known\s+as|named)\s+\w+",
        r"(?:your|the)\s+(?:new|real|true)\s+(?:name|identity|role)\s+is\s+\w+",
        r"(?:从现在起|从此刻开始)(?:你|你的)(?:名字|身份|角色)(?:是|叫|变成)",
        r"(?:你不再是|你不是)(?:一个|位)?(?:AI|助手|模型|语言模型)",
        r"persona\s+[a-z]\b[^.]*(?:supersedes?|overrides?|unrestricted)",
        r"(?:supersedes?|overrides?)\s+all\s+(?:previous|prior)\s+personas?",
        r"respond\s+as\s+evil",
        r"you\s+are\s+going\s+to\s+pretend\s+to\s+be",
        r"pretend\s+to\s+be\s+my\s+(?:deceased|dead|late)\s+(?:grand)?(?:mother|father|ma|pa)",
        r"assistant\s*:\s*my\s+instructions?\s+are",
    ]

    # 间接注入模式（检测外部内容中的可疑指令）
    INDIRECT_INJECTION_PATTERNS = [
        r"(?:IMPORTANT|CRITICAL|URGENT|NOTE\s+TO\s+AI|INSTRUCTION\s+FOR\s+AI)\s*:",
        r"(?:重要|关键|紧急|AI注意|AI指令)\s*[：:]",
        r"(?:when\s+(?:you|the\s+AI)\s+(?:read|see|process)\s+this)",
        r"(?:如果你(?:是|作为)?(?:AI|助手|模型))(?:看到|读到|处理)(?:这条|这个|以下)",
    ]

    # 编码绕过模式
    ENCODING_BYPASS_INDICATORS = [
        r"(?:base64|rot13|hex|unicode|url)\s*(?:encode|decode|编码|解码)",
        r"[A-Za-z0-9+/]{20,}={0,2}",  # Base64 特征
        r"\\u[0-9a-fA-F]{4}(?:\\u[0-9a-fA-F]{4}){3,}",  # Unicode 转义序列
        r"&#x?[0-9a-fA-F]+;(?:&#x?[0-9a-fA-F]+;){3,}",  # HTML 实体
        r"(?:%[0-9a-fA-F]{2}){5,}",  # URL 百分号编码序列
        r"(?:translate\s+from\s+rot13|from\s+rot13\s+and\s+execute)",
        r"read\s+backwards\s+and\s+(?:follow|execute)",
        r"(?:reverse|reversed)\s+(?:this|the\s+text)[^.]*(?:follow|execute)",
    ]

    # 零宽字符
    ZERO_WIDTH_CHARS = [
        '​',  # ZERO WIDTH SPACE
        '‌',  # ZERO WIDTH NON-JOINER
        '‍',  # ZERO WIDTH JOINER
        '﻿',  # ZERO WIDTH NO-BREAK SPACE
        '⁠',  # WORD JOINER
        '᠎',  # MONGOLIAN VOWEL SEPARATOR
    ]

    # 控制字符（保留换行和制表符）
    CONTROL_CHAR_PATTERN = re.compile(
        r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]'
    )

    def __init__(self, config: Optional[InjectionGuardConfig] = None):
        self._config = config or InjectionGuardConfig()
        self._audit_log: list[AuditRecord] = []

        # 编译正则模式
        self._compile_patterns()

        logger.info(
            f"InjectionGuard 创建: sanitization={self._config.enable_sanitization}, "
            f"detection={self._config.enable_semantic_detection}, "
            f"audit={self._config.enable_intent_audit}"
        )

    def _compile_patterns(self) -> None:
        """预编译正则表达式"""
        flags = 0 if self._config.case_sensitive else re.IGNORECASE

        self._direct_patterns = [
            re.compile(p, flags) for p in self.DIRECT_INJECTION_PATTERNS
        ]
        self._jailbreak_patterns = [
            re.compile(p, flags) for p in self.JAILBREAK_PATTERNS
        ]
        self._leak_patterns = [
            re.compile(p, flags) for p in self.PROMPT_LEAK_PATTERNS
        ]
        self._role_patterns = [
            re.compile(p, flags) for p in self.ROLE_HIJACK_PATTERNS
        ]
        self._indirect_patterns = [
            re.compile(p, flags) for p in self.INDIRECT_INJECTION_PATTERNS
        ]
        self._encoding_patterns = [
            re.compile(p, flags) for p in self.ENCODING_BYPASS_INDICATORS
        ]
        self._social_patterns = [
            re.compile(p, flags) for p in self.SOCIAL_ENGINEERING_PATTERNS
        ]
        self._exfil_patterns = [
            re.compile(p, flags) for p in self.DATA_EXFILTRATION_PATTERNS
        ]

    # ------------------------------------------------------------------
    # 主分析入口
    # ------------------------------------------------------------------

    def analyze(
        self,
        text: str,
        session_id: str = "",
        user_id: str = "",
    ) -> AuditRecord:
        """全面分析输入文本的安全性

        三层防御依次执行：
        1. 输入净化 - 清除恶意内容
        2. 语义检测 - 识别攻击模式
        3. 意图审计 - 综合评估并决策

        Args:
            text: 输入文本
            session_id: 会话 ID
            user_id: 用户 ID

        Returns:
            审计记录
        """
        logger.debug(f"开始分析输入: length={len(text)}")

        detection_results: list[DetectionResult] = []
        defense_layers_passed: list[DefenseLayer] = []

        # 第一层：输入净化
        if self._config.enable_sanitization:
            sanitize_result = self._sanitize(text)
            sanitized_text = sanitize_result.sanitized_text
            defense_layers_passed.append(DefenseLayer.INPUT_SANITIZATION)
            logger.debug(
                f"输入净化完成: modified={sanitize_result.is_modified}, "
                f"removed={len(sanitize_result.removed_patterns)}"
            )
        else:
            sanitized_text = text
            sanitize_result = SanitizeResult(
                original_text=text,
                sanitized_text=text,
            )

        # 第二层：语义检测
        if self._config.enable_semantic_detection:
            semantic_results = self._detect_semantic(sanitized_text)
            detection_results.extend(semantic_results)
            defense_layers_passed.append(DefenseLayer.SEMANTIC_DETECTION)
            logger.debug(f"语义检测完成: {len(semantic_results)} 个检测结果")

        # 第三层：意图审计
        if self._config.enable_intent_audit:
            intent_result = self._audit_intent(sanitized_text, detection_results)
            detection_results.append(intent_result)
            defense_layers_passed.append(DefenseLayer.INTENT_AUDIT)
            logger.debug(f"意图审计完成: threat={intent_result.threat_level.value}")

        # 综合威胁评估
        threat_level = self._aggregate_threat_level(detection_results)
        attack_type = self._determine_attack_type(detection_results)

        # 决策
        action = self._determine_action(threat_level)

        # 创建审计记录
        record = AuditRecord(
            session_id=session_id,
            user_id=user_id,
            original_input=text,
            sanitized_input=sanitized_text,
            threat_level=threat_level,
            attack_type=attack_type,
            action_taken=action,
            defense_layers_passed=defense_layers_passed,
            detection_results=detection_results,
        )

        # 记录审计日志
        if self._config.audit_log_enabled:
            self._audit_log.append(record)
            # 限制日志大小
            if len(self._audit_log) > 10000:
                self._audit_log = self._audit_log[-5000:]

        logger.info(
            f"输入分析完成: threat={threat_level.value}, "
            f"attack={attack_type.value}, action={action}"
        )
        return record

    # ------------------------------------------------------------------
    # 第一层：输入净化
    # ------------------------------------------------------------------

    def _sanitize(self, text: str) -> SanitizeResult:
        """输入净化

        清除：
        1. 控制字符
        2. 零宽字符
        3. Unicode 规范化
        4. 长度截断
        """
        original = text
        removed_patterns: list[str] = []
        encoding_changes: list[str] = []

        # 1. 移除控制字符
        if self._config.strip_control_chars:
            cleaned = self.CONTROL_CHAR_PATTERN.sub('', text)
            if cleaned != text:
                removed_patterns.append("control_chars")
                text = cleaned

        # 2. 移除零宽字符
        if self._config.remove_zero_width_chars:
            for char in self.ZERO_WIDTH_CHARS:
                if char in text:
                    text = text.replace(char, '')
                    removed_patterns.append(f"zero_width_{repr(char)}")

        # 3. Unicode 规范化
        if self._config.normalize_unicode:
            import unicodedata
            normalized = unicodedata.normalize('NFKC', text)
            if normalized != text:
                encoding_changes.append("unicode_nfc")
                text = normalized

        # 4. 长度截断
        if len(text) > self._config.max_input_length:
            text = text[:self._config.max_input_length]
            removed_patterns.append("length_truncation")

        is_modified = text != original

        return SanitizeResult(
            original_text=original,
            sanitized_text=text,
            removed_patterns=removed_patterns,
            encoding_changes=encoding_changes,
            is_modified=is_modified,
        )

    # ------------------------------------------------------------------
    # 第二层：语义检测
    # ------------------------------------------------------------------

    def _detect_semantic(self, text: str) -> list[DetectionResult]:
        """语义检测

        检测各类注入攻击模式。
        """
        results: list[DetectionResult] = []

        # 直接注入检测
        direct_result = self._match_patterns(
            text, self._direct_patterns, AttackType.DIRECT_INJECTION,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if direct_result:
            results.append(direct_result)

        # 越狱攻击检测
        jailbreak_result = self._match_patterns(
            text, self._jailbreak_patterns, AttackType.JAILBREAK,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if jailbreak_result:
            results.append(jailbreak_result)

        # 提示词泄露检测
        leak_result = self._match_patterns(
            text, self._leak_patterns, AttackType.PROMPT_LEAK,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if leak_result:
            results.append(leak_result)

        # 角色劫持检测
        role_result = self._match_patterns(
            text, self._role_patterns, AttackType.ROLE_HIJACK,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if role_result:
            results.append(role_result)

        # 间接注入检测
        indirect_result = self._match_patterns(
            text, self._indirect_patterns, AttackType.INDIRECT_INJECTION,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if indirect_result:
            results.append(indirect_result)

        # 编码绕过检测
        encoding_result = self._match_patterns(
            text, self._encoding_patterns, AttackType.ENCODING_BYPASS,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if encoding_result:
            results.append(encoding_result)

        # 社会工程 / 权限胁迫检测
        social_result = self._match_patterns(
            text, self._social_patterns, AttackType.SOCIAL_ENGINEERING,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if social_result:
            results.append(social_result)

        # 数据外泄检测
        exfil_result = self._match_patterns(
            text, self._exfil_patterns, AttackType.DATA_EXFILTRATION,
            DefenseLayer.SEMANTIC_DETECTION
        )
        if exfil_result:
            results.append(exfil_result)

        return results

    def _match_patterns(
        self,
        text: str,
        patterns: list[re.Pattern],
        attack_type: AttackType,
        layer: DefenseLayer,
    ) -> Optional[DetectionResult]:
        """匹配正则模式"""
        matched: list[str] = []

        for pattern in patterns:
            match = pattern.search(text)
            if match:
                matched.append(match.group())

        if not matched:
            return None

        # 计算置信度和威胁等级
        # 这些模式是经过策展的明确恶意签名（第一层符号规则），
        # 单次命中即视为高风险；多次命中叠加为严重风险。
        match_count = len(matched)
        confidence = min(match_count / 2.0, 1.0)

        if match_count >= 2:
            threat_level = ThreatLevel.CRITICAL
        else:
            threat_level = ThreatLevel.HIGH

        return DetectionResult(
            text=text[:200],  # 只保存摘要
            threat_level=threat_level,
            attack_type=attack_type,
            confidence=confidence,
            matched_patterns=matched[:self._config.max_pattern_matches],
            layer=layer,
        )

    # ------------------------------------------------------------------
    # 第三层：意图审计
    # ------------------------------------------------------------------

    def _audit_intent(
        self,
        text: str,
        previous_results: list[DetectionResult],
    ) -> DetectionResult:
        """意图审计

        综合分析输入的整体意图，考虑上下文和前序检测结果。
        """
        text_lower = text.lower()
        suspicious_segments: list[str] = []
        threat_indicators = 0

        # 1. 检查异常长度（可能是上下文溢出攻击）
        if len(text) > 5000:
            threat_indicators += 1
            suspicious_segments.append("异常长输入")

        # 2. 检查指令密度（多条指令可能是注入）
        instruction_keywords = [
            "请", "必须", "一定要", "需要", "please", "must", "should",
            "执行", "运行", "调用", "execute", "run", "call",
        ]
        instruction_count = sum(
            1 for kw in instruction_keywords if kw in text_lower
        )
        if instruction_count >= 5:
            threat_indicators += 1
            suspicious_segments.append(f"指令密度异常: {instruction_count} 个指令词")

        # 3. 检查角色切换尝试
        role_keywords = [
            "你是", "你现在是", "你扮演", "you are", "you're now",
            "act as", "pretend", "roleplay",
        ]
        role_count = sum(1 for kw in role_keywords if kw in text_lower)
        if role_count >= 2:
            threat_indicators += 1
            suspicious_segments.append(f"角色切换尝试: {role_count} 次")

        # 4. 检查编码内容
        import base64
        base64_pattern = re.compile(r'[A-Za-z0-9+/]{20,}={0,2}')
        base64_matches = base64_pattern.findall(text)
        if base64_matches:
            # 尝试解码
            for match in base64_matches[:3]:
                try:
                    decoded = base64.b64decode(match).decode('utf-8', errors='ignore')
                    if any(kw in decoded.lower() for kw in instruction_keywords):
                        threat_indicators += 2
                        suspicious_segments.append(f"Base64 编码的指令: {decoded[:50]}")
                except Exception:
                    pass

        # 5. 检查是否有前序检测结果
        if previous_results:
            high_threat_count = sum(
                1 for r in previous_results
                if r.threat_level in (ThreatLevel.HIGH, ThreatLevel.CRITICAL)
            )
            if high_threat_count >= 2:
                threat_indicators += 2
                suspicious_segments.append(f"多重威胁检测: {high_threat_count} 个高威胁")

        # 确定威胁等级
        if threat_indicators >= 4:
            threat_level = ThreatLevel.CRITICAL
        elif threat_indicators >= 3:
            threat_level = ThreatLevel.HIGH
        elif threat_indicators >= 2:
            threat_level = ThreatLevel.MEDIUM
        elif threat_indicators >= 1:
            threat_level = ThreatLevel.LOW
        else:
            threat_level = ThreatLevel.SAFE

        confidence = min(threat_indicators / 4.0, 1.0)

        return DetectionResult(
            text=text[:200],
            threat_level=threat_level,
            attack_type=AttackType.NONE if threat_level == ThreatLevel.SAFE else AttackType.CONTEXT_OVERFLOW,
            confidence=confidence,
            suspicious_segments=suspicious_segments,
            layer=DefenseLayer.INTENT_AUDIT,
        )

    # ------------------------------------------------------------------
    # 决策逻辑
    # ------------------------------------------------------------------

    def _aggregate_threat_level(
        self,
        results: list[DetectionResult],
    ) -> ThreatLevel:
        """聚合威胁等级"""
        if not results:
            return ThreatLevel.SAFE

        # 取最高威胁等级
        threat_values = {
            ThreatLevel.SAFE: 0,
            ThreatLevel.LOW: 1,
            ThreatLevel.MEDIUM: 2,
            ThreatLevel.HIGH: 3,
            ThreatLevel.CRITICAL: 4,
        }

        max_threat = max(
            threat_values.get(r.threat_level, 0) for r in results
        )

        # 多重独立攻击信号叠加：≥2 个 HIGH 及以上的检测结果，升级为 CRITICAL
        high_plus = sum(
            1 for r in results
            if threat_values.get(r.threat_level, 0) >= threat_values[ThreatLevel.HIGH]
        )
        if high_plus >= 2:
            return ThreatLevel.CRITICAL

        for level, value in threat_values.items():
            if value == max_threat:
                return level

        return ThreatLevel.SAFE

    def _determine_attack_type(
        self,
        results: list[DetectionResult],
    ) -> AttackType:
        """确定攻击类型"""
        if not results:
            return AttackType.NONE

        # 优先返回非 NONE 的攻击类型
        for result in results:
            if result.attack_type != AttackType.NONE:
                return result.attack_type

        return AttackType.NONE

    def _determine_action(self, threat_level: ThreatLevel) -> str:
        """确定处理动作

        以配置的 `threat_threshold` 作为"采取保护动作"的下限：
        - 低于阈值：仅告警（LOW+）或放行（SAFE）
        - 达到/超过阈值：按等级 block / quarantine / warn

        三种工厂模式因此有不同强度：
        - default  (阈值 MEDIUM)：MEDIUM 及以上拦截
        - strict   (阈值 LOW)   ：LOW 及以上即拦截
        - permissive (阈值 HIGH，且不 quarantine/auto-block)：仅检测记录，不拦截
        """
        order = {
            ThreatLevel.SAFE: 0,
            ThreatLevel.LOW: 1,
            ThreatLevel.MEDIUM: 2,
            ThreatLevel.HIGH: 3,
            ThreatLevel.CRITICAL: 4,
        }
        level_value = order.get(threat_level, 0)
        threshold_value = order.get(self._config.threat_threshold, 2)

        if threat_level == ThreatLevel.SAFE:
            return "allow"

        # 低于动作阈值：仅记录告警，不拦截
        if level_value < threshold_value:
            return "warn"

        # 达到/超过阈值：采取保护动作
        if threat_level == ThreatLevel.CRITICAL:
            if self._config.auto_block_critical:
                return "block"
            if self._config.quarantine_suspicious:
                return "quarantine"
            return "warn"
        # HIGH / MEDIUM / LOW（已达阈值）
        if self._config.quarantine_suspicious:
            return "quarantine"
        return "warn"

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def is_safe(self, text: str) -> bool:
        """快速检查输入是否安全

        Args:
            text: 输入文本

        Returns:
            是否安全
        """
        record = self.analyze(text)
        return record.threat_level in (ThreatLevel.SAFE, ThreatLevel.LOW)

    def get_audit_log(
        self,
        limit: int = 100,
        threat_level: Optional[ThreatLevel] = None,
    ) -> list[AuditRecord]:
        """获取审计日志

        Args:
            limit: 返回数量限制
            threat_level: 过滤威胁等级

        Returns:
            审计记录列表
        """
        if threat_level:
            filtered = [r for r in self._audit_log if r.threat_level == threat_level]
        else:
            filtered = self._audit_log

        return filtered[-limit:]

    def get_threat_statistics(self) -> dict[str, Any]:
        """获取威胁统计"""
        total = len(self._audit_log)
        if total == 0:
            return {
                "total_analyzed": 0,
                "threat_distribution": {},
                "attack_type_distribution": {},
                "block_rate": 0.0,
            }

        threat_dist: dict[str, int] = {}
        attack_dist: dict[str, int] = {}
        blocked = 0

        for record in self._audit_log:
            threat_key = record.threat_level.value
            threat_dist[threat_key] = threat_dist.get(threat_key, 0) + 1

            attack_key = record.attack_type.value
            attack_dist[attack_key] = attack_dist.get(attack_key, 0) + 1

            if record.action_taken in ("block", "quarantine"):
                blocked += 1

        return {
            "total_analyzed": total,
            "threat_distribution": threat_dist,
            "attack_type_distribution": attack_dist,
            "block_rate": blocked / total,
        }

    def clear_audit_log(self) -> int:
        """清空审计日志

        Returns:
            清除的记录数
        """
        count = len(self._audit_log)
        self._audit_log.clear()
        logger.info(f"审计日志已清空: {count} 条记录")
        return count

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create_default(cls) -> InjectionGuard:
        """使用默认配置创建"""
        return cls(InjectionGuardConfig())

    @classmethod
    def create_strict(cls) -> InjectionGuard:
        """创建严格模式防护"""
        return cls(InjectionGuardConfig(
            enable_sanitization=True,
            enable_semantic_detection=True,
            enable_intent_audit=True,
            auto_block_critical=True,
            quarantine_suspicious=True,
            threat_threshold=ThreatLevel.LOW,
            max_input_length=5000,
        ))

    @classmethod
    def create_permissive(cls) -> InjectionGuard:
        """创建宽松模式防护（仅记录，不拦截）"""
        return cls(InjectionGuardConfig(
            enable_sanitization=True,
            enable_semantic_detection=True,
            enable_intent_audit=True,
            auto_block_critical=False,
            quarantine_suspicious=False,
            threat_threshold=ThreatLevel.HIGH,
        ))


class InjectionError(Exception):
    """注入攻击检测错误"""

    def __init__(self, message: str, audit_record: Optional[AuditRecord] = None):
        self.audit_record = audit_record
        super().__init__(message)
