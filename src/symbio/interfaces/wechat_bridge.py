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
import json
from pathlib import Path
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

    def __init__(self, *, session_path: str | Path = Path("data") / "wechat_session.json") -> None:
        self._session_path = Path(session_path)
        self._login: dict[str, Any] = {
            "status": "logged_out",
            "qr": "",  # 登录二维码内容（URL/字符串，前端可渲染成二维码）
            "qr_image": "",  # 二维码图片 data URL（bridge 直接给图时用）
            "user": "",  # 绑定成功后的微信账号名
            "updated_at": "",
        }
        # 内置 iLink 模式运行态
        self._client: Any = None  # ILinkClient
        self._login_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._qrcode_id: str = ""  # 当前二维码 id（轮询登录状态用）
        self._sync_buf: str = ""  # getupdates 增量游标
        # 消息处理回调：由 api 层注入，签名 async (from_user, content, is_group) -> reply
        self._handler: Optional[Callable[[str, str, bool], Awaitable[str]]] = None
        # 实时消息流环形缓冲（收/发），供 UI 展示是否真正收发通了
        self._messages: list[dict[str, Any]] = []
        self._messages_max = 60
        # iLink 出站发送的重试次数（网络异常时退避重试）
        self._send_retries = 3

    def set_message_handler(self, handler: Callable[[str, str, bool], Awaitable[str]]) -> None:
        """注入入站消息处理回调（内置 iLink 模式收到消息后调用）。"""
        self._handler = handler

    def record_message(self, direction: str, user: str, text: str, kind: str = "") -> None:
        """记录一条收/发消息到实时消息流（direction: in / out）。"""
        from datetime import datetime, timezone

        self._messages.append(
            {
                "direction": direction,
                "user": user,
                "text": (text or "")[:300],
                "kind": kind,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(self._messages) > self._messages_max:
            self._messages = self._messages[-self._messages_max :]

    def recent_messages(self, limit: int = 40) -> list[dict[str, Any]]:
        return list(reversed(self._messages[-limit:]))

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
                self._save_session()
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
            if new_buf and new_buf != self._sync_buf:
                self._sync_buf = new_buf
                self._save_session()  # 持久化游标，避免重启后重复处理旧消息
            for msg in data.get("msgs") or []:
                sender = str(msg.get("from_user_id") or "").strip()
                if not sender or sender == client.account_id:
                    continue
                text = extract_text(msg.get("item_list") or [])
                if not text:
                    continue
                ctx_token = str(msg.get("context_token") or "")
                self.record_message("in", sender, text)
                try:
                    reply = await self._dispatch(sender, text)
                    if reply:
                        await client.send_message(sender, reply, context_token=ctx_token)
                        self.record_message("out", sender, reply)
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
        self._clear_session()
        return self.update_login("logged_out")

    # -- 登录态持久化（重启免重扫码）-------------------------------------

    def _save_session(self) -> None:
        """把当前 iLink 登录凭据落盘，供进程重启后恢复。"""
        if self._client is None or not getattr(self._client, "token", ""):
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            self._session_path.write_text(
                json.dumps(
                    {
                        "token": self._client.token,
                        "account_id": getattr(self._client, "account_id", ""),
                        "base_url": getattr(self._client, "base_url", ""),
                        "user": self._login.get("user", ""),
                        "sync_buf": self._sync_buf,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:  # pragma: no cover - 磁盘异常
            logger.warning(f"保存微信会话失败: {e}")

    def _clear_session(self) -> None:
        try:
            self._session_path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass

    async def try_restore_session(self) -> bool:
        """进程启动时尝试用落盘 token 恢复登录，免重扫码。

        token 若已过期，recv loop 首次 getupdates 会收到 -14 并自动回落到 failed，
        用户再扫码即可。因此恢复是 best-effort，不保证 token 仍有效。
        """
        if self.is_logged_in or not self._session_path.exists():
            return False
        try:
            data = json.loads(self._session_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        token = str(data.get("token") or "")
        if not token:
            return False
        from symbio.interfaces.ilink_client import ILinkClient

        cfg = getattr(get_settings(), "wechat", None)
        base_url = str(data.get("base_url") or "") or (
            getattr(cfg, "ilink_base_url", "") or "https://ilinkai.weixin.qq.com"
        )
        self._client = ILinkClient(base_url=base_url)
        self._client.token = token
        self._client.account_id = str(data.get("account_id") or "")
        self._sync_buf = str(data.get("sync_buf") or "")
        self.update_login(
            "logged_in", user=str(data.get("user") or data.get("account_id") or "微信账号")
        )
        self._start_recv_loop()
        logger.info("微信登录态已从本地恢复，免重扫码")
        return True

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

    async def send(
        self, to_user: str, content: str, *, is_group: bool = False, context_token: str = ""
    ) -> dict[str, Any]:
        """发送一条微信消息。

        优先级：内置 iLink 已登录 → 直接调 iLink sendmessage；
        否则有外部 send_endpoint → POST 回外部 bridge；都没有 → prepared。
        """
        payload = {"to_user": to_user, "content": content, "is_group": is_group}

        # 1) 内置 iLink 模式（已扫码登录）；网络异常自动重试（默认 3 次、退避）
        if self.is_logged_in and self._client is not None and getattr(self._client, "token", ""):
            last_err = ""
            for attempt in range(self._send_retries):
                try:
                    resp = await self._client.send_message(
                        to_user, content, context_token=context_token
                    )
                    ok = not (resp.get("ret") or resp.get("errcode"))
                    return {
                        "delivery_status": "sent" if ok else "error",
                        "via": "ilink",
                        "payload": payload,
                        "response": resp,
                        "attempts": attempt + 1,
                    }
                except Exception as e:
                    last_err = str(e)
                    logger.warning(f"iLink 发送失败(第{attempt + 1}/{self._send_retries}次): {e}")
                    if attempt + 1 < self._send_retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
            return {
                "delivery_status": "error",
                "via": "ilink",
                "payload": payload,
                "error": last_err,
                "attempts": self._send_retries,
            }

        # 2) 外部 bridge 模式
        cfg = getattr(get_settings(), "wechat", None)
        endpoint = getattr(cfg, "send_endpoint", "") if cfg else ""
        if not endpoint:
            return {
                "delivery_status": "prepared",
                "payload": payload,
                "reason": "未登录 iLink 且未配置 send_endpoint，回复随 HTTP 响应返回",
            }
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
