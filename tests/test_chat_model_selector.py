from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def test_chat_page_exposes_model_selector_and_payload_binding():
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="chat-model-select"' in html
    assert "chatModelSelect:" in js
    assert "loadChatModelOptions" in js
    assert "selectedChatModel()" in js
    assert "model: selectedChatModel()" in js
