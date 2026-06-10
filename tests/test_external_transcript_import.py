import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app
from symbio.interfaces.database import Database
from symbio.tools.external_transcripts import (
    discover_external_transcripts,
    parse_external_transcript,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )


def test_discovers_and_parses_codex_transcript(tmp_path):
    codex_file = tmp_path / ".codex" / "sessions" / "2026" / "06" / "09" / "rollout-abc.jsonl"
    _write_jsonl(
        codex_file,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-06-09T08:00:00Z",
                "payload": {"id": "codex-thread-1", "cwd": str(tmp_path)},
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:01:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "import my current codex chat"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:02:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will import it."}],
                },
            },
            {"type": "event_msg", "payload": {"type": "tool_call"}},
        ],
    )

    summaries = discover_external_transcripts(codex_root=tmp_path / ".codex", claude_root=tmp_path / ".claude")
    transcript = parse_external_transcript(summaries[0].path, provider=summaries[0].provider)

    assert summaries[0].provider == "codex"
    assert summaries[0].external_session_id == "codex-thread-1"
    assert summaries[0].message_count == 2
    assert summaries[0].title == "import my current codex chat"
    assert [message.role for message in transcript.messages] == ["user", "assistant"]
    assert transcript.messages[1].content == "I will import it."


def test_codex_parser_ignores_internal_context_records(tmp_path):
    codex_file = tmp_path / ".codex" / "sessions" / "2026" / "06" / "09" / "rollout-noise.jsonl"
    _write_jsonl(
        codex_file,
        [
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:00:00Z",
                "payload": {"type": "message", "role": "user", "content": "<environment_context>\n  <cwd>x</cwd>\n</environment_context>"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:00:01Z",
                "payload": {"type": "message", "role": "user", "content": "# AGENTS.md instructions for demo"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:01:00Z",
                "payload": {"type": "message", "role": "user", "content": "real user request"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T08:02:00Z",
                "payload": {"type": "message", "role": "assistant", "content": "real assistant reply"},
            },
        ],
    )

    transcript = parse_external_transcript(codex_file, provider="codex")

    assert transcript.title == "real user request"
    assert [message.content for message in transcript.messages] == ["real user request", "real assistant reply"]


def test_discovers_and_parses_claude_code_transcript(tmp_path):
    claude_file = tmp_path / ".claude" / "projects" / "demo" / "claude-thread-1.jsonl"
    _write_jsonl(
        claude_file,
        [
            {"type": "summary", "summary": "old summary should not import"},
            {
                "type": "user",
                "timestamp": "2026-06-09T09:00:00Z",
                "message": {"role": "user", "content": "bring in claude history"},
                "sessionId": "claude-thread-1",
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-09T09:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Claude history is readable."},
                        {"type": "tool_use", "name": "Read"},
                    ],
                },
                "sessionId": "claude-thread-1",
            },
        ],
    )

    summaries = discover_external_transcripts(codex_root=tmp_path / ".codex", claude_root=tmp_path / ".claude")
    transcript = parse_external_transcript(summaries[0].path, provider=summaries[0].provider)

    assert summaries[0].provider == "claude-code"
    assert summaries[0].external_session_id == "claude-thread-1"
    assert summaries[0].message_count == 2
    assert transcript.messages[0].content == "bring in claude history"
    assert transcript.messages[1].content == "Claude history is readable."


@pytest.mark.asyncio
async def test_external_transcript_api_imports_messages_into_symbio_chat(tmp_path):
    codex_file = tmp_path / ".codex" / "sessions" / "2026" / "06" / "09" / "rollout-api.jsonl"
    _write_jsonl(
        codex_file,
        [
            {"type": "session_meta", "payload": {"id": "api-codex-thread"}},
            {
                "type": "response_item",
                "timestamp": "2026-06-09T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": "open the old codex task"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-06-09T10:01:00Z",
                "payload": {"type": "message", "role": "assistant", "content": "old task loaded"},
            },
        ],
    )
    db = Database(str(tmp_path / "symbio.db"))
    await db.connect()

    async def mock_get_db(db_path=None):
        return db

    previous_roots = getattr(app.state, "external_transcript_roots", None)
    app.state.external_transcript_roots = {
        "codex": str(tmp_path / ".codex"),
        "claude-code": str(tmp_path / ".claude"),
    }
    try:
        with patch("symbio.interfaces.api.get_db", mock_get_db):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                listed = await client.get("/api/external-agents/transcripts")
                imported = await client.post(
                    "/api/external-agents/transcripts/import",
                    json={"provider": "codex", "path": str(codex_file)},
                )
                session_id = imported.json()["session"]["id"]
                messages = await client.get(f"/api/sessions/{session_id}/messages")

        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert imported.status_code == 200
        assert imported.json()["imported_messages"] == 2
        assert messages.status_code == 200
        assert [message["role"] for message in messages.json()["messages"]] == ["user", "assistant"]
        assert messages.json()["messages"][0]["content"] == "open the old codex task"
    finally:
        await db.close()
        if previous_roots is not None:
            app.state.external_transcript_roots = previous_roots
        elif hasattr(app.state, "external_transcript_roots"):
            delattr(app.state, "external_transcript_roots")
