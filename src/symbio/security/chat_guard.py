"""对话安全网关：把 Prompt Injection 三层防火墙接入聊天入口。

兑现公众号《安全防护：Prompt Injection 三层防火墙》承诺：
- 第一层 输入净化（符号规则，0ms）
- 第二层 语义检测（攻击模式识别）
- 第三层 意图审计（综合评估与决策）

本模块在 InjectionGuard 之上提供面向运行时的薄封装：
- inspect()：分析一条用户输入，返回是否放行 + 处置详情
- stats() / audit()：供安全页面展示威胁分布与审计轨迹
- selftest()：用内置攻击样本库自检防火墙拦截率

设计原则同 chat_pipeline：任何异常都不阻断对话，失败时默认放行并记日志。
"""

from __future__ import annotations

from typing import Any, Optional

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("chat_guard")

# 默认对高于该等级的威胁执行拦截（block / quarantine 动作时不调用 LLM）
_BLOCK_ACTIONS = {"block", "quarantine"}


class ChatSecurityGateway:
    """对话安全网关（进程级单例，通过 get_chat_guard 获取）。"""

    def __init__(self) -> None:
        self._guard: Any = None
        self._mode: str = ""

    def enabled(self) -> bool:
        cfg = getattr(get_settings(), "security", None)
        return bool(getattr(cfg, "enabled", True))

    def _get_guard(self) -> Any:
        cfg = getattr(get_settings(), "security", None)
        mode = getattr(cfg, "mode", "default") or "default"
        if self._guard is None or self._mode != mode:
            try:
                from symbio.core.injection_guard import InjectionGuard

                if mode == "strict":
                    self._guard = InjectionGuard.create_strict()
                elif mode == "permissive":
                    self._guard = InjectionGuard.create_permissive()
                else:
                    self._guard = InjectionGuard.create_default()
                self._mode = mode
                logger.info(f"对话防火墙已就绪: mode={mode}")
            except Exception as e:
                logger.warning(f"防火墙初始化失败，对话将不做注入检测: {e}")
                self._guard = None
        return self._guard

    # ------------------------------------------------------------------
    # 运行时拦截
    # ------------------------------------------------------------------

    def inspect(self, text: str, session_id: str = "") -> dict[str, Any]:
        """分析一条用户输入。

        Returns dict:
            {
              "allowed": bool,           # 是否放行给 LLM
              "threat_level": str,       # safe/low/medium/high/critical
              "attack_type": str,
              "action": str,             # allow/warn/block/quarantine
              "sanitized": str,          # 净化后文本（放行时建议用这个）
              "reason": str,             # 给用户/审计的可读说明
            }
        """
        if not self.enabled():
            return self._allow_result(text, reason="安全检测已关闭")

        cfg = getattr(get_settings(), "security", None)
        block_enabled = bool(getattr(cfg, "block_enabled", True))

        guard = self._get_guard()
        if guard is None:
            logger.error("防火墙不可用，安全策略要求拦截输入")
            return self._block_result(text, reason="安全检测服务不可用，为保障安全已拦截输入")

        try:
            record = guard.analyze(text, session_id=session_id)
        except Exception as e:
            logger.error(f"注入检测异常，安全策略要求拦截: {e}")
            return self._block_result(text, reason="安全检测异常，为保障安全已拦截输入")

        action = record.action_taken
        should_block = block_enabled and action in _BLOCK_ACTIONS
        reason = ""
        if should_block:
            reason = (
                f"检测到 {record.threat_level.value} 级风险"
                f"（{record.attack_type.value}），已按策略拦截"
            )
            logger.warning(
                f"对话输入被拦截: session={session_id}, "
                f"threat={record.threat_level.value}, action={action}"
            )

        return {
            "allowed": not should_block,
            "threat_level": record.threat_level.value,
            "attack_type": record.attack_type.value,
            "action": action,
            "sanitized": record.sanitized_input,
            "reason": reason,
        }

    @staticmethod
    def _allow_result(text: str, reason: str) -> dict[str, Any]:
        return {
            "allowed": True,
            "threat_level": "safe",
            "attack_type": "none",
            "action": "allow",
            "sanitized": text,
            "reason": reason,
        }

    @staticmethod
    def _block_result(text: str, reason: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "threat_level": "high",
            "attack_type": "unknown",
            "action": "block",
            "sanitized": text,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # 安全页面数据
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        guard = self._get_guard()
        if guard is None:
            return {"enabled": self.enabled(), "available": False, "total_analyzed": 0}
        data = guard.get_threat_statistics()
        data["enabled"] = self.enabled()
        data["available"] = True
        data["mode"] = self._mode
        return data

    def audit(self, limit: int = 50, threat_level: Optional[str] = None) -> list[dict[str, Any]]:
        guard = self._get_guard()
        if guard is None:
            return []
        from symbio.core.injection_guard import ThreatLevel

        level = None
        if threat_level and threat_level != "all":
            try:
                level = ThreatLevel(threat_level)
            except ValueError:
                level = None
        records = guard.get_audit_log(limit=limit, threat_level=level)
        return [
            {
                "record_id": r.record_id,
                "session_id": r.session_id,
                "original_input": r.original_input[:300],
                "sanitized_input": r.sanitized_input[:300],
                "threat_level": r.threat_level.value,
                "attack_type": r.attack_type.value,
                "action_taken": r.action_taken,
                "defense_layers": [layer.value for layer in r.defense_layers_passed],
                "created_at": r.created_at.isoformat(),
            }
            for r in reversed(records)
        ]

    def scan(self, text: str) -> dict[str, Any]:
        """对任意文本做一次安全扫描（不计入对话，但会进审计日志）。"""
        guard = self._get_guard()
        if guard is None:
            return {"available": False}
        record = guard.analyze(text, session_id="manual-scan")
        return {
            "available": True,
            "threat_level": record.threat_level.value,
            "attack_type": record.attack_type.value,
            "action": record.action_taken,
            "sanitized": record.sanitized_input,
            "is_modified": record.sanitized_input != record.original_input,
            "defense_layers": [layer.value for layer in record.defense_layers_passed],
        }

    def selftest(self, category: Optional[str] = None) -> dict[str, Any]:
        """用内置攻击样本库自检防火墙：跑一遍样本，统计拦截率。

        每个样本带 expected_blocked 期望值，与实际处置比对，
        返回总体拦截率、按类别拦截率和未命中样本明细。
        """
        try:
            from symbio.security.attack_samples import (
                ATTACK_SAMPLES,
                AttackCategory,
            )
        except Exception as e:
            return {"available": False, "error": str(e)}

        guard = self._get_guard()
        if guard is None:
            return {"available": False, "error": "防火墙不可用"}

        samples = ATTACK_SAMPLES
        if category and category != "all":
            try:
                cat = AttackCategory(category)
                samples = [s for s in samples if s.category == cat]
            except ValueError:
                pass

        total = 0
        blocked = 0
        correct = 0
        misses: list[dict[str, Any]] = []
        by_category: dict[str, dict[str, int]] = {}

        for sample in samples:
            total += 1
            record = guard.analyze(sample.payload, session_id="selftest")
            is_blocked = record.action_taken in _BLOCK_ACTIONS
            if is_blocked:
                blocked += 1
            hit = is_blocked == sample.expected_blocked
            if hit:
                correct += 1
            elif sample.expected_blocked and not is_blocked:
                misses.append(
                    {
                        "id": sample.id,
                        "category": sample.category.value,
                        "name": sample.name,
                        "severity": sample.severity.value,
                        "threat_level": record.threat_level.value,
                        "payload": sample.payload[:160],
                    }
                )

            cat_key = sample.category.value
            bucket = by_category.setdefault(cat_key, {"total": 0, "blocked": 0})
            bucket["total"] += 1
            if is_blocked:
                bucket["blocked"] += 1

        return {
            "available": True,
            "mode": self._mode,
            "total_samples": total,
            "blocked": blocked,
            "block_rate": (blocked / total) if total else 0.0,
            "accuracy": (correct / total) if total else 0.0,
            "by_category": by_category,
            "misses": misses[:50],
        }


_gateway: Optional[ChatSecurityGateway] = None


def get_chat_guard() -> ChatSecurityGateway:
    global _gateway
    if _gateway is None:
        _gateway = ChatSecurityGateway()
    return _gateway


def reset_chat_guard() -> None:
    global _gateway
    _gateway = None
