import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces import database as db_module
from symbio.interfaces.api import app
from symbio.interfaces.database import Database


@pytest_asyncio.fixture
async def evolution_db(tmp_path):
    db = Database(str(tmp_path / "symbio.db"))
    await db.connect()
    await db.create_session("export-session", title="Export Session")
    await db.create_message(
        "msg-export-user",
        "export-session",
        "user",
        "Write a deployment checklist",
        "2026-06-08T10:00:00",
        12,
    )
    await db.create_message(
        "msg-export-assistant",
        "export-session",
        "assistant",
        "1. Build\n2. Test\n3. Deploy",
        "2026-06-08T10:00:01",
        18,
    )
    previous = db_module._db_instance
    db_module._db_instance = db
    try:
        yield db
    finally:
        await db.close()
        db_module._db_instance = previous


@pytest_asyncio.fixture
async def evolution_client(evolution_db):
    async def mock_get_db(db_path=None):
        return evolution_db

    with patch("symbio.interfaces.api.get_db", mock_get_db):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_export_conversations_preview_returns_openai_samples(evolution_client):
    resp = await evolution_client.post(
        "/api/export/conversations",
        json={"format": "openai", "session_id": "export-session", "preview": True},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["format"] == "openai"
    assert data["sample_count"] == 1
    assert data["written"] is False
    assert data["samples"][0]["id"] == "export-session"
    assert data["samples"][0]["messages"][0]["role"] == "user"
    assert data["samples"][0]["messages"][1]["content"].startswith("1. Build")


@pytest.mark.asyncio
async def test_export_conversations_writes_jsonl_file(evolution_client, tmp_path):
    output_path = tmp_path / "exports" / "dataset.jsonl"

    resp = await evolution_client.post(
        "/api/export/conversations",
        json={
            "format": "alpaca",
            "session_id": "export-session",
            "preview": False,
            "output_path": str(output_path),
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["written"] is True
    assert data["output_path"] == str(output_path)
    assert data["sample_count"] == 1
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    sample = json.loads(lines[0])
    assert sample["instruction"] == "Write a deployment checklist"
    assert sample["output"].startswith("1. Build")


@pytest.mark.asyncio
async def test_evaluation_suites_lists_local_json_suites(evolution_client, tmp_path):
    suite_dir = tmp_path / "eval_suites"
    suite_dir.mkdir()
    (suite_dir / "smoke.json").write_text(
        json.dumps(
            {
                "name": "Smoke Suite",
                "description": "Basic tool-call checks",
                "version": "1.2.0",
                "cases": [
                    {
                        "case_id": "case-1",
                        "name": "Use search",
                        "input_text": "search docs",
                        "expected_tool_calls": [{"tool_name": "browser.fetch"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    resp = await evolution_client.get(
        "/api/evaluation/suites",
        params={"path": str(suite_dir)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["suites"][0]["name"] == "Smoke Suite"
    assert data["suites"][0]["case_count"] == 1
    assert data["suites"][0]["version"] == "1.2.0"
