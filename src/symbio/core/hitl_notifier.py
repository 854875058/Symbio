"""External notification bridge for HITL approvals.

The notifier intentionally speaks simple HTTP bridge protocols instead of
binding the core runtime to a specific bot SDK. QQ/OneBot, WeChaty, Feishu,
and custom bots can all forward approval messages through this layer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.core.hitl_gateway import ApprovalRequest
from symbio.utils.logger import get_logger

logger = get_logger("hitl_notifier")


class HITLNotificationTarget(BaseModel):
    """A configured external approval notification target."""

    platform: str
    endpoint: str = ""
    chat_id: str = ""
    chat_type: str = "group"
    access_token: str = ""
    secret: str = ""
    enabled: bool = True


class HITLNotificationResult(BaseModel):
    """Delivery result for one notification target."""

    platform: str
    success: bool
    delivery_status: str = "not_sent"
    status_code: int = 0
    error: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class IMApprovalCommand(BaseModel):
    """Parsed approval command from an IM message."""

    action: str
    request_id: str
    comment: str = ""


def approval_short_code(request_id: str, length: int = 8) -> str:
    """Return a short human-facing approval code."""
    compact = "".join(ch for ch in request_id if ch.isalnum()).upper()
    return compact[:length]


class HITLNotifier:
    """Send HITL approval cards to IM/webhook channels."""

    def __init__(
        self,
        targets: Optional[list[HITLNotificationTarget]] = None,
        callback_base_url: str = "",
        timeout: float = 5.0,
        client_factory: Optional[Callable[..., httpx.AsyncClient]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.targets = [
            target if isinstance(target, HITLNotificationTarget) else HITLNotificationTarget(**target)
            for target in (targets or [])
        ]
        self.callback_base_url = callback_base_url.rstrip("/")
        self.timeout = timeout
        self._client_factory = client_factory or httpx.AsyncClient
        self._clock = clock

    @classmethod
    def from_settings(cls) -> HITLNotifier:
        settings = get_settings()
        hitl = settings.hitl
        targets: list[HITLNotificationTarget] = []

        for raw in getattr(hitl, "notify_targets", []) or []:
            if isinstance(raw, dict):
                targets.append(HITLNotificationTarget(**raw))

        if not targets and hitl.notify_platform:
            platforms = [p.strip() for p in hitl.notify_platform.split(",") if p.strip()]
            for platform in platforms:
                targets.append(HITLNotificationTarget(
                    platform=platform,
                    endpoint=getattr(hitl, "notify_endpoint", ""),
                    chat_id=hitl.notify_chat_id,
                    chat_type=getattr(hitl, "notify_chat_type", "group"),
                    access_token=getattr(hitl, "notify_access_token", ""),
                    secret=getattr(hitl, "notify_secret", ""),
                ))

        return cls(
            targets=targets,
            callback_base_url=getattr(hitl, "callback_base_url", ""),
            timeout=float(getattr(hitl, "notify_timeout", 5.0)),
        )

    def enabled_targets(self) -> list[HITLNotificationTarget]:
        return [target for target in self.targets if target.enabled and target.endpoint]

    def audit_targets(self) -> list[HITLNotificationTarget]:
        return [target for target in self.targets if target.enabled]

    def callback_url(self) -> str:
        if not self.callback_base_url:
            return "/api/hitl/im-callback"
        return f"{self.callback_base_url}/api/hitl/im-callback"

    async def prepare_notifications(self, request: ApprovalRequest) -> list[dict[str, Any]]:
        """Build outbound notification payloads for audit and connector delivery."""
        return [self.build_outbound_payload(target, request) for target in self.audit_targets()]

    def build_outbound_payload(
        self,
        target: HITLNotificationTarget,
        request: ApprovalRequest,
    ) -> dict[str, Any]:
        code = approval_short_code(request.request_id)
        platform = target.platform.lower()
        recipient_key = "channel" if target.chat_type == "group" else "recipient"
        payload = {
            "platform": target.platform,
            "recipient": target.chat_id,
            "channel": target.chat_id if target.chat_type == "group" else "",
            "chat_type": target.chat_type,
            "request_id": request.request_id,
            "short_code": code,
            "approve_command": f"approve {code}",
            "reject_command": f"reject {code} reason",
            "api_path": "/api/hitl/im-callback",
            "callback_url": self.callback_url(),
            "message": self.render_message(request),
            "created_at": datetime.now().isoformat(),
            "delivery_status": "pending" if target.endpoint else "prepared",
        }
        payload[recipient_key] = target.chat_id
        payload["connector_payload"] = self._payload_for(platform, target, request, payload["message"])
        return payload

    def render_message(self, request: ApprovalRequest) -> str:
        code = approval_short_code(request.request_id)
        lines = [
            "[Symbio HITL] Approval required",
            f"Code: {code}",
            f"Task: {request.task_id}",
            f"Risk: {request.risk_level.value}",
            f"Action: {request.action or '-'}",
            f"Impact: {request.impact_scope or '-'}",
            f"Reason: {request.reason or '-'}",
            "",
            f"Approve: 同意 {code}",
            f"Reject: 拒绝 {code} reason",
        ]
        if self.callback_base_url:
            lines.extend([
                "",
                f"Detail: {self.callback_base_url}/api/hitl/{request.request_id}",
            ])
        return "\n".join(lines)

        lines = [
            "[Symbio HITL] 待审批",
            f"ID: {request.request_id}",
            f"任务: {request.task_id}",
            f"风险: {request.risk_level.value}",
            f"动作: {request.action or '-'}",
            f"影响: {request.impact_scope or '-'}",
            f"原因: {request.reason or '-'}",
            "",
            f"通过: 同意 {request.request_id}",
            f"拒绝: 拒绝 {request.request_id} 原因",
        ]
        if self.callback_base_url:
            lines.extend([
                "",
                f"详情: {self.callback_base_url}/api/hitl/{request.request_id}",
            ])
        return "\n".join(lines)

    async def notify(self, request: ApprovalRequest) -> list[HITLNotificationResult]:
        results: list[HITLNotificationResult] = []
        for target in self.audit_targets():
            outbound_payload = self.build_outbound_payload(target, request)
            if not target.endpoint:
                results.append(HITLNotificationResult(
                    platform=target.platform,
                    success=False,
                    delivery_status="prepared",
                    payload=outbound_payload,
                ))
                continue
            results.append(await self._send_to_target(
                target,
                request,
                outbound_payload["message"],
                outbound_payload,
            ))
        return results

    async def _send_to_target(
        self,
        target: HITLNotificationTarget,
        request: ApprovalRequest,
        message: str,
        outbound_payload: Optional[dict[str, Any]] = None,
    ) -> HITLNotificationResult:
        platform = target.platform.lower()
        headers = self._headers(target)
        payload = self._payload_for(platform, target, request, message)
        url = self._url_for(platform, target)

        try:
            async with self._client_factory(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
            response_payload = self._response_json(response)
            success = self._response_success(platform, response.status_code, response_payload)
            delivery_status = "sent" if success else "failed"
            audit_payload = self._delivery_audit_payload(
                outbound_payload or self.build_outbound_payload(target, request),
                delivery_status=delivery_status,
                status_code=response.status_code,
                error="" if success else self._response_error(response, response_payload),
            )
            return HITLNotificationResult(
                platform=target.platform,
                success=success,
                delivery_status=delivery_status,
                status_code=response.status_code,
                error="" if success else self._response_error(response, response_payload),
                payload=audit_payload,
            )
        except Exception as exc:
            logger.warning(f"HITL notification failed: platform={target.platform}, error={exc}")
            return HITLNotificationResult(
                platform=target.platform,
                success=False,
                delivery_status="failed",
                error=str(exc),
                payload=self._delivery_audit_payload(
                    outbound_payload or self.build_outbound_payload(target, request),
                    delivery_status="failed",
                    error=str(exc),
                ),
            )

    def _headers(self, target: HITLNotificationTarget) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        platform = target.platform.lower()
        if target.access_token and platform not in {"feishu", "lark", "wechat", "weixin", "wx", "wecom", "work_wechat", "enterprise_wechat"}:
            headers["Authorization"] = f"Bearer {target.access_token}"
        return headers

    def _url_for(self, platform: str, target: HITLNotificationTarget) -> str:
        endpoint = target.endpoint.rstrip("/")
        if platform in {"qq", "onebot", "lagrange"}:
            if endpoint.endswith("/send_group_msg") or endpoint.endswith("/send_private_msg"):
                return endpoint
            action = "send_private_msg" if target.chat_type == "private" else "send_group_msg"
            return f"{endpoint}/{action}"
        return target.endpoint

    def _payload_for(
        self,
        platform: str,
        target: HITLNotificationTarget,
        request: ApprovalRequest,
        message: str,
    ) -> dict[str, Any]:
        if platform in {"qq", "onebot", "lagrange"}:
            key = "user_id" if target.chat_type == "private" else "group_id"
            return {key: target.chat_id, "message": message}
        if platform in {"wechaty"}:
            return {"to": target.chat_id, "text": message, "type": "text"}
        if platform in {"wechat", "weixin", "wx", "wecom", "work_wechat", "enterprise_wechat"}:
            return {"msgtype": "text", "text": {"content": message}}
        if platform in {"feishu", "lark"}:
            payload = {"msg_type": "text", "content": {"text": message}}
            if target.secret:
                timestamp = str(int(self._clock()))
                payload["timestamp"] = timestamp
                payload["sign"] = self._feishu_sign(timestamp, target.secret)
            return payload
        return {
            "platform": target.platform,
            "chat_id": target.chat_id,
            "message": message,
            "request": request.model_dump(mode="json"),
        }

    def _feishu_sign(self, timestamp: str, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _response_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _response_success(
        self,
        platform: str,
        status_code: int,
        response_payload: dict[str, Any],
    ) -> bool:
        if not 200 <= status_code < 300:
            return False
        platform = platform.lower()
        if platform in {"feishu", "lark"}:
            return response_payload.get("code", 0) == 0
        if platform in {"wechat", "weixin", "wx", "wecom", "work_wechat", "enterprise_wechat"}:
            return response_payload.get("errcode", 0) == 0
        if platform in {"qq", "onebot", "lagrange"}:
            retcode = response_payload.get("retcode", 0)
            status = str(response_payload.get("status", "ok")).lower()
            return retcode == 0 and status in {"ok", "async", "success"}
        return True

    def _response_error(self, response: httpx.Response, response_payload: dict[str, Any]) -> str:
        if response_payload:
            return str(response_payload)[:500]
        return response.text[:500]

    def _delivery_audit_payload(
        self,
        payload: dict[str, Any],
        delivery_status: str,
        status_code: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        audited = dict(payload)
        audited["delivery_status"] = delivery_status
        audited["status_code"] = status_code
        audited["error"] = error
        return audited


_COMMAND_RE = re.compile(
    r"^\s*(?P<action>同意|通过|批准|approve|yes|ok|拒绝|驳回|reject|no)\s+"
    r"(?P<request_id>[0-9a-fA-F-]{8,})"
    r"(?:\s+(?P<comment>.*))?\s*$",
    re.IGNORECASE,
)


def parse_im_approval_command(text: str) -> Optional[IMApprovalCommand]:
    short_match = re.match(
        r"^\s*(?P<action>\u540c\u610f|\u901a\u8fc7|\u6279\u51c6|approve|yes|ok|"
        r"\u62d2\u7edd|\u9a73\u56de|reject|no)\s+"
        r"(?P<request_id>[0-9a-zA-Z-]{4,36})"
        r"(?:\s+(?P<comment>.*))?\s*$",
        text or "",
        re.IGNORECASE,
    )
    if short_match:
        raw_action = short_match.group("action").lower()
        action = "reject" if raw_action in {"\u62d2\u7edd", "\u9a73\u56de", "reject", "no"} else "approve"
        return IMApprovalCommand(
            action=action,
            request_id=short_match.group("request_id"),
            comment=(short_match.group("comment") or "").strip(),
        )

    match = _COMMAND_RE.match(text or "")
    if not match:
        return None

    raw_action = match.group("action").lower()
    action = "reject" if raw_action in {"拒绝", "驳回", "reject", "no"} else "approve"
    return IMApprovalCommand(
        action=action,
        request_id=match.group("request_id"),
        comment=(match.group("comment") or "").strip(),
    )
