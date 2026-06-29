"""批次D5：多模态视觉做实——图片描述从占位改为调 Claude vision 真生成。

这些测试通过注入 vision_describe 函数，在不发网络请求的前提下覆盖：
- 注入描述函数 -> 文本表示含真实描述（而非占位）
- 描述函数收到正确的 base64 + media_type
- 不支持的格式（BMP）-> 不调用视觉模型，降级占位
- 描述函数抛异常 -> 优雅降级占位（不打断处理）
- enable_vision=False -> 一律占位，不调用描述函数
- 超过大小上限 -> 跳过视觉模型，降级占位
"""

from __future__ import annotations

import base64
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.memory.multimodal import (
    ContentModality,
    MultiModalMemory,
    _VISION_MAX_BYTES,
)

PLACEHOLDER = "[图片描述占位]"


def _write_png(path: Path, width: int = 2, height: int = 2, pad: int = 40) -> None:
    """写一个魔数+IHDR 合法的最小 PNG（内容不要求可解码，仅用于格式/尺寸检测）。"""
    data = (
        b"\x89PNG\r\n\x1a\n"          # 魔数 (8)
        + b"\x00\x00\x00\x0dIHDR"      # IHDR chunk 长度+类型 (8)
        + struct.pack(">II", width, height)  # 宽高 (offset 16:24)
        + b"\x08\x06\x00\x00\x00"      # 位深/颜色类型等
        + b"\x00" * pad
    )
    path.write_bytes(data)


def _write_jpeg(path: Path) -> None:
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 60)


def _write_bmp(path: Path) -> None:
    path.write_bytes(b"BM" + b"\x00" * 62)


def test_injected_describer_produces_real_description(tmp_path):
    img = tmp_path / "cat.png"
    _write_png(img)

    mm = MultiModalMemory(vision_describe=lambda b64, mime: "一只橘猫坐在窗台上")
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert result.is_valid
    assert "[图片描述] 一只橘猫坐在窗台上" in result.text_representation
    assert PLACEHOLDER not in result.text_representation
    assert result.metadata["vision_description"] == "一只橘猫坐在窗台上"
    assert result.metadata["has_vision_description"] is True


def test_describer_receives_base64_and_media_type(tmp_path):
    img = tmp_path / "pic.png"
    _write_png(img)
    raw = img.read_bytes()

    seen: dict[str, str] = {}

    def describe(b64: str, mime: str) -> str:
        seen["b64"] = b64
        seen["mime"] = mime
        return "描述"

    mm = MultiModalMemory(vision_describe=describe)
    mm.process_content(str(img), ContentModality.IMAGE)

    assert seen["mime"] == "image/png"
    assert base64.standard_b64decode(seen["b64"]) == raw


def test_jpeg_maps_to_jpeg_media_type(tmp_path):
    img = tmp_path / "photo.jpg"
    _write_jpeg(img)

    seen: dict[str, str] = {}
    mm = MultiModalMemory(
        vision_describe=lambda b64, mime: seen.setdefault("mime", mime) or "ok"
    )
    mm.process_content(str(img), ContentModality.IMAGE)

    assert seen["mime"] == "image/jpeg"


def test_unsupported_format_skips_vision(tmp_path):
    img = tmp_path / "old.bmp"
    _write_bmp(img)

    calls: list = []

    def describe(b64: str, mime: str) -> str:
        calls.append((b64, mime))
        return "不应被调用"

    mm = MultiModalMemory(vision_describe=describe)
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert calls == []  # BMP 不在 Claude vision 支持列表，描述函数不应被调用
    assert PLACEHOLDER in result.text_representation
    assert result.metadata["has_vision_description"] is False


def test_describer_exception_falls_back_to_placeholder(tmp_path):
    img = tmp_path / "boom.png"
    _write_png(img)

    def describe(b64: str, mime: str):
        raise RuntimeError("模型超时")

    mm = MultiModalMemory(vision_describe=describe)
    result = mm.process_content(str(img), ContentModality.IMAGE)

    # 描述函数抛错不应打断处理，结果降级为占位
    assert result.is_valid
    assert PLACEHOLDER in result.text_representation
    assert result.metadata["has_vision_description"] is False


def test_describer_returns_none_falls_back(tmp_path):
    img = tmp_path / "none.png"
    _write_png(img)

    mm = MultiModalMemory(vision_describe=lambda b64, mime: None)
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert PLACEHOLDER in result.text_representation
    assert result.metadata["has_vision_description"] is False


def test_enable_vision_false_skips_describer(tmp_path):
    img = tmp_path / "off.png"
    _write_png(img)

    calls: list = []
    mm = MultiModalMemory(
        vision_describe=lambda b64, mime: calls.append(1) or "x",
        enable_vision=False,
    )
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert calls == []
    assert PLACEHOLDER in result.text_representation
    assert result.metadata["has_vision_description"] is False


def test_oversize_image_skipped(tmp_path):
    img = tmp_path / "huge.png"
    # 合法 PNG 头 + 填充到超过上限；格式检测只看前 64 字节，故仍识别为 PNG
    _write_png(img, pad=40)
    with open(img, "ab") as f:
        f.write(b"\x00" * (_VISION_MAX_BYTES + 1))

    calls: list = []
    mm = MultiModalMemory(vision_describe=lambda b64, mime: calls.append(1) or "x")
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert calls == []  # 超大图片跳过视觉模型
    assert PLACEHOLDER in result.text_representation


def test_default_describe_without_api_key_returns_none(tmp_path, monkeypatch):
    """默认（非注入）路径：无 API key 时 _default_vision_describe 返回 None，降级占位。"""
    img = tmp_path / "default.png"
    _write_png(img)

    from symbio.config import settings as settings_mod

    real = settings_mod.get_settings()

    class _Stub:
        model = type("M", (), {
            "anthropic_api_key": "",
            "anthropic_base_url": "https://api.anthropic.com",
            "model_medium": "claude-sonnet-4-6",
        })()

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Stub())
    # multimodal 内部是 `from symbio.config.settings import get_settings`（函数内导入），
    # 故 patch 模块属性即可生效。
    assert real is not None

    mm = MultiModalMemory()  # 不注入，走真实默认路径
    result = mm.process_content(str(img), ContentModality.IMAGE)

    assert PLACEHOLDER in result.text_representation
    assert result.metadata["has_vision_description"] is False
