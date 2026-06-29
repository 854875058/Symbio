"""批次D6：视觉描述接入记忆摄取管线。

验证 MemoryManager.add_multimodal_memory 把图片/代码处理成可检索记忆，
图片走（可注入的）视觉描述，文件签名缓存避免重复描述；以及 /api/memory/store
的 modality=image 端到端走多模态路径。全部 hermetic：注入假描述器 + 假 DB，
覆写 _get_embedding，跳过 LanceDB，不触网。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.memory.manager import MemoryManager, MemoryManagerConfig, MemoryType
from symbio.memory.multimodal import MultiModalMemory


def _write_png(path: Path, width: int = 2, height: int = 2) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 40
    )


def _manager(tmp_path: Path, describer) -> MemoryManager:
    cfg = MemoryManagerConfig(lancedb_path=str(tmp_path / "db"))
    mgr = MemoryManager(cfg)
    # 跳过 LanceDB / embedding 网络
    mgr._initialized = True
    mgr._table = None

    async def _no_embed(text: str):
        return []

    mgr._get_embedding = _no_embed  # type: ignore[assignment]
    mgr.set_multimodal_processor(MultiModalMemory(vision_describe=describer))
    return mgr


@pytest.mark.asyncio
async def test_image_ingest_stores_vision_description(tmp_path):
    img = tmp_path / "cat.png"
    _write_png(img)

    mgr = _manager(tmp_path, lambda b64, mime: "一只橘猫坐在窗台")
    item = await mgr.add_multimodal_memory(str(img), "image")

    assert item is not None
    assert "[图片描述] 一只橘猫坐在窗台" in item.content
    assert item.metadata["modality"] == "image"
    assert item.metadata["vision_description"] == "一只橘猫坐在窗台"
    assert item.metadata["has_vision_description"] is True
    # 真入库：可在长期记忆中找到
    assert item.memory_id in mgr._long_term


@pytest.mark.asyncio
async def test_image_describe_cached_per_file(tmp_path):
    img = tmp_path / "c.png"
    _write_png(img)

    calls: list = []

    def describe(b64: str, mime: str) -> str:
        calls.append(1)
        return "描述"

    mgr = _manager(tmp_path, describe)
    await mgr.add_multimodal_memory(str(img), "image")
    await mgr.add_multimodal_memory(str(img), "image")

    # 第二次命中文件签名缓存，不再调用视觉模型（避免重复计费）
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_image_not_stored(tmp_path):
    mgr = _manager(tmp_path, lambda b64, mime: "x")
    item = await mgr.add_multimodal_memory(str(tmp_path / "nope.png"), "image")

    assert item is None
    assert len(mgr._long_term) == 0


@pytest.mark.asyncio
async def test_code_ingest_stores_structure_without_network(tmp_path):
    mgr = _manager(tmp_path, lambda b64, mime: "不应被调用")
    item = await mgr.add_multimodal_memory(
        "def foo(x):\n    return x", "code", language="python"
    )

    assert item is not None
    assert "函数: foo" in item.content
    assert item.metadata["modality"] == "code"


@pytest.mark.asyncio
async def test_unknown_modality_falls_back_to_text(tmp_path):
    mgr = _manager(tmp_path, lambda b64, mime: "x")
    item = await mgr.add_multimodal_memory("hello world", "weird")

    assert item is not None
    assert item.content == "hello world"
    assert item.metadata["modality"] == "text"


@pytest.mark.asyncio
async def test_api_store_image_routes_through_vision(tmp_path, monkeypatch):
    from symbio.interfaces import api as api_mod

    img = tmp_path / "api.png"
    _write_png(img)

    # 假 DB，避免污染真实 SQLite
    class _FakeDB:
        def __init__(self):
            self.rows: list = []

        async def create_memory(self, **kw):
            self.rows.append(kw)

    fake_db = _FakeDB()

    async def _fake_get_db():
        return fake_db

    monkeypatch.setattr(api_mod, "get_db", _fake_get_db)

    mgr = _manager(tmp_path, lambda b64, mime: "一张测试图")
    previous = getattr(api_mod.app.state, "memory_manager", None)
    api_mod.app.state.memory_manager = mgr
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/memory/store",
                json={"content": str(img), "modality": "image", "tags": ["pic"]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["modality"] == "image"
        assert data["has_vision_description"] is True
        assert "[图片描述] 一张测试图" in data["text_representation"]
        # SQLite 侧落的是处理后的文本表示，两侧一致
        assert fake_db.rows and "[图片描述] 一张测试图" in fake_db.rows[0]["content"]
    finally:
        api_mod.app.state.memory_manager = previous


@pytest.mark.asyncio
async def test_api_store_bad_image_path_returns_422(tmp_path, monkeypatch):
    from symbio.interfaces import api as api_mod

    class _FakeDB:
        async def create_memory(self, **kw):
            return None

    async def _fake_get_db():
        return _FakeDB()

    monkeypatch.setattr(api_mod, "get_db", _fake_get_db)

    mgr = _manager(tmp_path, lambda b64, mime: "x")
    previous = getattr(api_mod.app.state, "memory_manager", None)
    api_mod.app.state.memory_manager = mgr
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/memory/store",
                json={"content": str(tmp_path / "ghost.png"), "modality": "image"},
            )
        assert resp.status_code == 422
    finally:
        api_mod.app.state.memory_manager = previous
