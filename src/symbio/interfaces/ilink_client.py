"""原生 iLink Bot API 客户端（个人微信 clawbot）。

参考微信官方 iLink Bot 协议（ilinkai.weixin.qq.com）：无需公网回调，扫码登录后
通过 HTTP 长轮询收消息、sendmessage 发消息。这样 Symbio 内置即可接入个人微信，
不依赖任何外部 bridge。

流程：
1. get_qr()           → 拉登录二维码（qrcode id + 可渲染的二维码内容）
2. poll_qr_status()   → 轮询 scaned/confirmed/expired，确认后拿到 token + account_id
3. get_updates()      → 长轮询拉取入站消息（携带 sync_buf 增量游标）
4. send_message()     → 回复消息
"""

from __future__ import annotations

import secrets
import struct
import uuid
from typing import Any, Optional

from symbio.utils.logger import get_logger

logger = get_logger("ilink_client")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0  # 131584

EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


def _random_uin() -> str:
    return str(struct.unpack(">I", secrets.token_bytes(4))[0])


def _login_headers() -> dict[str, str]:
    return {
        "X-WECHAT-UIN": _random_uin(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }


def _bot_headers(token: str) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_uin(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def extract_text(item_list: list[dict[str, Any]]) -> str:
    """从 item_list 提取文本（文本优先，其次语音转写）。"""
    for item in item_list or []:
        if item.get("type") == ITEM_TEXT:
            return str((item.get("text_item") or {}).get("text") or "")
    for item in item_list or []:
        if item.get("type") == ITEM_VOICE:
            return str((item.get("voice_item") or {}).get("text") or "")
    return ""


class ILinkClient:
    """iLink Bot API 异步客户端。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "", account_id: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.account_id = account_id

    # -- 扫码登录 ----------------------------------------------------------

    async def get_qr(self) -> Optional[dict[str, str]]:
        """拉取登录二维码。返回 {qrcode, qr_content} 或 None。"""
        import httpx
        url = f"{self.base_url}/{EP_GET_BOT_QR}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={"bot_type": "3"}, headers=_login_headers())
            data = resp.json()
        except Exception as e:
            logger.error(f"get_bot_qrcode 失败: {e}")
            return None
        qrcode = data.get("qrcode")
        if not qrcode:
            logger.error(f"未拿到二维码: {data}")
            return None
        return {
            "qrcode": str(qrcode),
            "qr_content": str(data.get("qrcode_img_content") or ""),
        }

    async def poll_qr_status(self, qrcode: str) -> dict[str, Any]:
        """查询一次扫码状态。返回 {status, ...}，confirmed 时含 token/account_id。"""
        import httpx
        url = f"{self.base_url}/{EP_GET_QR_STATUS}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={"qrcode": qrcode}, headers=_login_headers())
            data = resp.json()
        except Exception as e:
            logger.warning(f"get_qrcode_status 失败: {e}")
            return {"status": "error", "error": str(e)}
        status = str(data.get("status") or "")
        if status == "confirmed":
            return {
                "status": "confirmed",
                "account_id": str(data.get("account_id") or data.get("ilink_bot_id") or ""),
                "token": str(data.get("token") or data.get("bot_token") or ""),
                "base_url": str(data.get("base_url") or data.get("baseurl") or self.base_url),
                "user_id": str(data.get("user_id") or data.get("ilink_user_id") or ""),
            }
        return {"status": status or "pending"}

    # -- 消息收发 ----------------------------------------------------------

    async def get_updates(self, sync_buf: str = "", timeout_ms: int = LONG_POLL_TIMEOUT_MS) -> dict[str, Any]:
        """长轮询拉取入站消息。"""
        import httpx
        url = f"{self.base_url}/{EP_GET_UPDATES}"
        payload = {"get_updates_buf": sync_buf, "longpolling_timeout_ms": timeout_ms}
        async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 5) as client:
            resp = await client.post(url, json=payload, headers=_bot_headers(self.token))
        return resp.json()

    async def send_message(self, to_user: str, text: str, context_token: str = "") -> dict[str, Any]:
        """发送一条文本消息。"""
        import httpx
        url = f"{self.base_url}/{EP_SEND_MESSAGE}"
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": uuid.uuid4().hex,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        async with httpx.AsyncClient(timeout=API_TIMEOUT_MS / 1000 + 5) as client:
            resp = await client.post(url, json={"msg": msg}, headers=_bot_headers(self.token))
        return resp.json()
