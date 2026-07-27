from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def test_chat_page_exposes_model_selector_and_payload_binding():
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    # 前端脚本按页面拆分在 web/js/ 下，合并后再断言，避免依赖具体文件划分。
    js = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((PROJECT_ROOT / "web" / "js").glob("*.js"))
    )

    assert 'id="chat-model-select"' in html
    assert "chatModelSelect:" in js
    assert "loadChatModelOptions" in js
    assert "selectedChatModel()" in js
    assert "model: selectedChatModel()" in js
