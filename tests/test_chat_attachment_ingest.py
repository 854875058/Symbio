"""批次D7：聊天附件自动摄取（图片/PDF）。

验证 _ingest_chat_attachments 把图片/PDF 摄取进记忆并落历史消息，未知类型跳过，
无 memory_manager 时安全返回空；以及 /api/chat 带 attachments 时端到端自动摄取
（无 API key 也照样摄取，附件不依赖 LLM 调用成功）。全部 hermetic，不触网。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.memory.manager import MemoryManager, MemoryManagerConfig
from symbio.memory.multimodal import MultiModalMemory


def _write_png(path: Path, width: int = 2, height: int = 2) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 40
    )


def _write_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")


def _manager(tmp_path: Path, describer) -> MemoryManager:
    mgr = MemoryManager(MemoryManagerConfig(lancedb_path=str(tmp_path / "db")))
    mgr._initialized = True
    mgr._table = None

    async def _no_embed(text: str):
        return []

    mgr._get_embedding = _no_embed  # type: ignore[assignment]
    mgr.set_multimodal_processor(MultiModalMemory(vision_describe=describer))
    return mgr


class _FakeDB:
    def __init__(self):
        self.messages: list = []
        self.sessions: list = []

    async def get_session(self, sid):
        return None

    async def create_session(self, sid, title=""):
        self.sessions.append((sid, title))

    async def create_message(self, mid, sid, role, content, ts, tokens):
        self.messages.append({"role": role, "content": content})

    async def update_session(self, sid, **kw):
        return None

    async def list_messages_by_session(self, sid):
        return self.messages


@pytest.mark.asyncio
async def test_helper_ingests_image_and_records_history(tmp_path):
    from symbio.interfaces.api import _ingest_chat_attachments

    img = tmp_path / "shot.png"
    _write_png(img)
    mgr = _manager(tmp_path, lambda b64, mime: "一张界面截图")
    db = _FakeDB()

    notes = await _ingest_chat_attachments(mgr, db, "sess-1", [str(img)])

    assert len(notes) == 1
    assert notes[0]["modality"] == "image"
    assert notes[0]["has_vision_description"] is True
    assert notes[0]["description"] == "一张界面截图"
    # 进了语义记忆
    assert notes[0]["memory_id"] in mgr._long_term
    # 落了一条 user 历史消息，含描述
    assert any(m["role"] == "user" and "一张界面截图" in m["content"] for m in db.messages)


@pytest.mark.asyncio
async def test_helper_ingests_pdf(tmp_path):
    from symbio.interfaces.api import _ingest_chat_attachments

    pdf = tmp_path / "doc.pdf"
    _write_pdf(pdf)
    mgr = _manager(tmp_path, lambda b64, mime: "不应被调用")
    db = _FakeDB()

    notes = await _ingest_chat_attachments(mgr, db, "s", [str(pdf)])

    assert len(notes) == 1
    assert notes[0]["modality"] == "pdf"


@pytest.mark.asyncio
async def test_helper_skips_unknown_extension(tmp_path):
    from symbio.interfaces.api import _ingest_chat_attachments

    txt = tmp_path / "note.txt"
    txt.write_text("hello")
    mgr = _manager(tmp_path, lambda b64, mime: "x")
    db = _FakeDB()

    notes = await _ingest_chat_attachments(mgr, db, "s", [str(txt)])

    assert notes == []
    assert db.messages == []


@pytest.mark.asyncio
async def test_helper_no_manager_returns_empty(tmp_path):
    from symbio.interfaces.api import _ingest_chat_attachments

    img = tmp_path / "x.png"
    _write_png(img)
    notes = await _ingest_chat_attachments(None, _FakeDB(), "s", [str(img)])
    assert notes == []


@pytest.mark.asyncio
async def test_helper_missing_file_skipped(tmp_path):
    from symbio.interfaces.api import _ingest_chat_attachments

    mgr = _manager(tmp_path, lambda b64, mime: "x")
    db = _FakeDB()
    notes = await _ingest_chat_attachments(mgr, db, "s", [str(tmp_path / "ghost.png")])
    assert notes == []


@pytest.mark.asyncio
async def test_chat_api_ingests_attachment_even_without_api_key(tmp_path, monkeypatch):
    from symbio.interfaces import api as api_mod
    from symbio.config.settings import Settings

    img = tmp_path / "chat.png"
    _write_png(img)

    fake_db = _FakeDB()

    async def _fake_get_db():
        return fake_db

    async def _fake_settings():
        s = Settings()
        s.model.anthropic_api_key = ""  # 强制走"无 key"早返回
        return s

    monkeypatch.setattr(api_mod, "get_db", _fake_get_db)
    monkeypatch.setattr(api_mod, "_load_llm_settings", _fake_settings)

    mgr = _manager(tmp_path, lambda b64, mime: "聊天里的图")
    previous = getattr(api_mod.app.state, "memory_manager", None)
    api_mod.app.state.memory_manager = mgr
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "看看这张图", "session_id": "s7", "attachments": [str(img)]},
            )

        assert resp.status_code == 200
        data = resp.json()
        # 无 key 对话失败，但附件已摄取并在响应中可见
        assert data["success"] is False
        assert data["attachments_ingested"]
        assert data["attachments_ingested"][0]["has_vision_description"] is True
        assert data["attachments_ingested"][0]["description"] == "聊天里的图"
        # 摄取进了语义记忆
        assert len(mgr._long_term) == 1
        # 附件描述作为 user 历史消息落库
        assert any("聊天里的图" in m["content"] for m in fake_db.messages)
    finally:
        api_mod.app.state.memory_manager = previous
