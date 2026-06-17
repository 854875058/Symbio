"""个人微信双向机器人 bridge（provider-agnostic）。

设计为不绑定任何具体微信实现：外部 bridge（Wechaty / padlocal / 自建 Go-bot 等）
把收到的微信消息以统一 schema POST 到 /api/wechat/inbound；Symbio 处理后既可在
HTTP 响应里直接带回复（同步 bridge 直接转发即可），也可通过配置的 send_endpoint
主动把回复 POST 回 bridge（异步 bridge）。

消息路由：
- 先尝试解析为 HITL 审批命令（同意/拒绝 REQ-CODE）→ 路由到审批网关
- 否则当作普通对话 → 走完整对话管线（防火墙 + 语义缓存 + LLM + 持久化）

所有出站发送失败都降级为 "prepared"（与 HITL notifier 一致），不抛异常打断流程。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger

logger = get_logger("wechat_bridge")


class WeChatInbound(BaseModel):
    """归一化的入站微信消息（由外部 bridge 提供）。"""
    from_user: str = Field(..., description="发送者标识（微信 wxid / 备注名）")
    content: str = Field(default="", description="消息文本")
    msg_id: str = ""
    is_group: bool = False
    group_id: str = ""
    token: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


LOGIN_STATES = {"logged_out", "waiting_scan", "scanned", "logged_in", "failed"}


class WeChatBridge:
    """微信出站发送 + 路由分类 + 扫码登录态（进程级单例，通过 get_wechat_bridge 获取）。

    扫码绑定流程（Wechaty 式个人微信）：真正生成登录二维码、维持登录会话的是外部
    bridge（Wechaty / wechatpadpro 等）。Symbio 不实现微信协议，只做"透出与编排"：
    bridge 把二维码和登录状态 push 到 /api/wechat/login/event，Symbio 存下来供
    Web UI 显示，用户扫码后 bridge 再 push logged_in。
    """

    def __init__(self) -> None:
        self._login: dict[str, Any] = {
            "status": "logged_out",
            "qr": "",          # 登录二维码内容（URL/字符串，前端可渲染成二维码）
            "qr_image": "",    # 二维码图片 data URL（bridge 直接给图时用）
            "user": "",        # 绑定成功后的微信账号名
            "updated_at": "",
        }

    # -- 扫码登录态 ---------------------------------------------------------

    def update_login(
        self,
        status: str,
        *,
        qr: Optional[str] = None,
        qr_image: Optional[str] = None,
        user: Optional[str] = None,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        if status in LOGIN_STATES:
            self._login["status"] = status
        if qr is not None:
            self._login["qr"] = qr
        if qr_image is not None:
            self._login["qr_image"] = qr_image
        if user is not None:
            self._login["user"] = user
        if status == "logged_out":
            self._login["qr"] = ""
            self._login["qr_image"] = ""
            self._login["user"] = ""
        self._login["updated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"微信登录态更新: status={self._login['status']} user={self._login['user']}")
        return self.login_state()

    def login_state(self) -> dict[str, Any]:
        return dict(self._login)

    def classify(self, content: str) -> tuple[str, Any]:
        """把入站文本分类为 'approval' 或 'chat'。

        Returns (kind, parsed)：approval 时 parsed 是 IMApprovalCommand，否则 None。
        """
        try:
            from symbio.core.hitl_notifier import parse_im_approval_command
            cmd = parse_im_approval_command(content or "")
            if cmd is not None:
                return "approval", cmd
        except Exception as e:
            logger.debug(f"审批命令解析跳过: {e}")
        return "chat", None

    async def send(self, to_user: str, content: str, *, is_group: bool = False) -> dict[str, Any]:
        """把回复 POST 回外部 bridge 的 send_endpoint。

        未配置 send_endpoint 时返回 prepared（同步 bridge 用 HTTP 响应里的 reply 即可）。
        """
        cfg = getattr(get_settings(), "wechat", None)
        endpoint = getattr(cfg, "send_endpoint", "") if cfg else ""
        payload = {
            "to_user": to_user,
            "content": content,
            "is_group": is_group,
        }
        if not endpoint:
            return {"delivery_status": "prepared", "payload": payload,
                    "reason": "未配置 send_endpoint，回复随 HTTP 响应返回"}
        try:
            import httpx
            headers = {"Content-Type": "application/json"}
            token = getattr(cfg, "send_token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=getattr(cfg, "timeout", 10.0)) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
            return {"delivery_status": "sent", "status_code": resp.status_code, "payload": payload}
        except Exception as e:
            logger.warning(f"微信出站发送失败（降级 prepared）: {e}")
            return {"delivery_status": "prepared", "payload": payload, "error": str(e)}


_bridge: Optional[WeChatBridge] = None


def get_wechat_bridge() -> WeChatBridge:
    global _bridge
    if _bridge is None:
        _bridge = WeChatBridge()
    return _bridge


def reset_wechat_bridge() -> None:
    global _bridge
    _bridge = None
