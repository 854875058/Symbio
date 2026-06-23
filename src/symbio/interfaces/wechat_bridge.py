"""个人微信双向机器人 bridge。

两种接入模式：
1. 内置 iLink Bot 客户端（推荐，即 clawbot）：点"扫码绑定"直接拉出微信官方 iLink
   二维码，扫码登录后由 Symbio 后台长轮询收发消息，无需任何外部部署。
2. 外部 bridge（兼容）：第三方网关（Wechaty 等）把消息 POST 到 /api/wechat/inbound，
   Symbio 处理后回推到 send_endpoint。

消息路由（两种模式共用）：
- 先解析为 HITL 审批命令（同意/拒绝 REQ-CODE）→ 路由到审批网关
- 否则当作普通对话 → 走完整对话管线（防火墙 + 语义缓存 + LLM + 持久化）
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

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
        # 内置 iLink 模式运行态
        self._client: Any = None              # ILinkClient
        self._login_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._qrcode_id: str = ""             # 当前二维码 id（轮询登录状态用）
        self._sync_buf: str = ""              # getupdates 增量游标
        # 消息处理回调：由 api 层注入，签名 async (from_user, content, is_group) -> reply
        self._handler: Optional[Callable[[str, str, bool], Awaitable[str]]] = None

    def set_message_handler(self, handler: Callable[[str, str, bool], Awaitable[str]]) -> None:
        """注入入站消息处理回调（内置 iLink 模式收到消息后调用）。"""
        self._handler = handler

    @property
    def is_logged_in(self) -> bool:
        return self._login.get("status") == "logged_in"

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

    # -- 内置 iLink 扫码登录（clawbot）-------------------------------------

    async def start_ilink_login(self) -> dict[str, Any]:
        """发起 iLink 扫码登录：拉二维码并在后台轮询登录状态。

        返回当前登录态（含二维码内容）。已登录则直接返回。
        """
        if self.is_logged_in:
            return self.login_state()
        from symbio.interfaces.ilink_client import ILinkClient

        cfg = getattr(get_settings(), "wechat", None)
        base_url = getattr(cfg, "ilink_base_url", "") or "https://ilinkai.weixin.qq.com"
        self._client = ILinkClient(base_url=base_url)
        qr = await self._client.get_qr()
        if not qr:
            self.update_login("failed")
            return self.login_state()
        self._qrcode_id = qr["qrcode"]
        self.update_login("waiting_scan", qr=qr["qr_content"])
        # 后台轮询登录状态，避免阻塞请求
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.create_task(self._poll_login_loop())
        return self.login_state()

    async def _poll_login_loop(self, max_seconds: int = 300) -> None:
        """后台轮询扫码状态，确认后启动收消息循环。"""
        client = self._client
        if client is None:
            return
        waited = 0
        while waited < max_seconds:
            await asyncio.sleep(2)
            waited += 2
            try:
                st = await client.poll_qr_status(self._qrcode_id)
            except Exception as e:  # pragma: no cover - 网络异常
                logger.warning(f"轮询扫码状态异常: {e}")
                continue
            status = st.get("status")
            if status == "scaned" and self._login.get("status") != "scanned":
                self.update_login("scanned")
            elif status == "confirmed":
                client.token = st.get("token", "")
                client.account_id = st.get("account_id", "")
                if st.get("base_url"):
                    client.base_url = st["base_url"].rstrip("/")
                self.update_login("logged_in", user=st.get("account_id") or "微信账号")
                self._start_recv_loop()
                return
            elif status == "expired":
                self.update_login("failed")
                return
        # 超时
        if not self.is_logged_in:
            self.update_login("failed")

    def _start_recv_loop(self) -> None:
        if self._recv_task is None or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        """登录后长轮询收消息，分流处理后回复。"""
        from symbio.interfaces.ilink_client import extract_text
        client = self._client
        if client is None:
            return
        logger.info("微信 iLink 收消息循环已启动")
        while self.is_logged_in and client.token:
            try:
                data = await client.get_updates(self._sync_buf)
            except Exception as e:  # pragma: no cover - 网络异常
                logger.warning(f"getupdates 异常: {e}")
                await asyncio.sleep(2)
                continue
            # iLink 错误码处理（-14=会话过期）
            ret = data.get("ret") or 0
            errcode = data.get("errcode") or 0
            if ret or errcode:
                logger.warning(f"getupdates 返回错误 ret={ret} errcode={errcode}")
                if ret == -14 or errcode == -14:
                    self.update_login("failed")
                    break
                await asyncio.sleep(2)
                continue
            # 更新增量游标（响应里回传同名字段 get_updates_buf）
            new_buf = str(data.get("get_updates_buf") or "")
            if new_buf:
                self._sync_buf = new_buf
            for msg in data.get("msgs") or []:
                sender = str(msg.get("from_user_id") or "").strip()
                if not sender or sender == client.account_id:
                    continue
                text = extract_text(msg.get("item_list") or [])
                if not text:
                    continue
                ctx_token = str(msg.get("context_token") or "")
                try:
                    reply = await self._dispatch(sender, text)
                    if reply:
                        await client.send_message(sender, reply, context_token=ctx_token)
                except Exception as e:  # pragma: no cover
                    logger.error(f"处理微信消息失败: {e}")
        logger.info("微信 iLink 收消息循环已退出")

    async def _dispatch(self, from_user: str, content: str, is_group: bool = False) -> str:
        """调用注入的 handler 处理消息；未注入时回退到本地分类提示。"""
        if self._handler is not None:
            return await self._handler(from_user, content, is_group)
        kind, _ = self.classify(content)
        return "（未配置消息处理器）" if kind == "chat" else "（审批命令已收到）"

    async def logout(self) -> dict[str, Any]:
        """登出并停止后台任务。"""
        for task in (self._login_task, self._recv_task):
            if task and not task.done():
                task.cancel()
        self._login_task = None
        self._recv_task = None
        self._client = None
        self._qrcode_id = ""
        self._sync_buf = ""
        return self.update_login("logged_out")

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

    async def send(self, to_user: str, content: str, *, is_group: bool = False,
                   context_token: str = "") -> dict[str, Any]:
        """发送一条微信消息。

        优先级：内置 iLink 已登录 → 直接调 iLink sendmessage；
        否则有外部 send_endpoint → POST 回外部 bridge；都没有 → prepared。
        """
        payload = {"to_user": to_user, "content": content, "is_group": is_group}

        # 1) 内置 iLink 模式（已扫码登录）
        if self.is_logged_in and self._client is not None and getattr(self._client, "token", ""):
            try:
                resp = await self._client.send_message(to_user, content, context_token=context_token)
                ok = not (resp.get("ret") or resp.get("errcode"))
                return {"delivery_status": "sent" if ok else "error", "via": "ilink",
                        "payload": payload, "response": resp}
            except Exception as e:
                logger.warning(f"iLink 发送失败: {e}")
                return {"delivery_status": "error", "via": "ilink", "payload": payload, "error": str(e)}

        # 2) 外部 bridge 模式
        cfg = getattr(get_settings(), "wechat", None)
        endpoint = getattr(cfg, "send_endpoint", "") if cfg else ""
        if not endpoint:
            return {"delivery_status": "prepared", "payload": payload,
                    "reason": "未登录 iLink 且未配置 send_endpoint，回复随 HTTP 响应返回"}
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
