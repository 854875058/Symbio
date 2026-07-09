"""数据飞轮微调 API 测试：提交作业 / 查状态 / 列数据集。

用 SYMBIO_FT_STUB=1 强制走训练桩（秒级完成），只验证 API 契约与异步作业流转，
不在测试里跑真训练（真训练由 test_lora_trainer.py 覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app


@pytest.fixture(autouse=True)
def _force_stub(monkeypatch):
    # 强制训练走 stub，避免测试触发真模型下载/训练
    monkeypatch.setenv("SYMBIO_FT_STUB", "1")
    # 每个测试用干净的 tuner，避免作业互相污染
    if hasattr(app.state, "fine_tuner"):
        delattr(app.state, "fine_tuner")
    yield
    if hasattr(app.state, "fine_tuner"):
        delattr(app.state, "fine_tuner")


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_dataset(tmp_path) -> str:
    ds = tmp_path / "train.jsonl"
    with ds.open("w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({"conversations": [
                {"role": "user", "content": f"问题{i}"},
                {"role": "assistant", "content": "回答"},
            ]}, ensure_ascii=False) + "\n")
    return str(ds)


@pytest.mark.asyncio
async def test_list_datasets_returns_structure(client):
    resp = await client.get("/api/flywheel/datasets")
    assert resp.status_code == 200
    assert "datasets" in resp.json()
    assert isinstance(resp.json()["datasets"], list)


@pytest.mark.asyncio
async def test_submit_finetune_returns_job_id_immediately(client, tmp_path):
    dataset = _make_dataset(tmp_path)
    resp = await client.post("/api/flywheel/finetune", json={
        "dataset_path": dataset, "model_name": "sshleifer/tiny-gpt2", "epochs": 1,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"]
    assert body["status"] in ("pending", "training", "completed")


@pytest.mark.asyncio
async def test_missing_dataset_returns_404(client):
    resp = await client.post("/api/flywheel/finetune", json={
        "dataset_path": "definitely-not-here.jsonl",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_finetune_job_completes_and_is_queryable(client, tmp_path):
    dataset = _make_dataset(tmp_path)
    submit = await client.post("/api/flywheel/finetune", json={
        "dataset_path": dataset, "model_name": "sshleifer/tiny-gpt2", "epochs": 2,
    })
    job_id = submit.json()["job_id"]

    # 轮询直到作业结束（stub 很快，但后台线程需要极短时间）
    status = None
    for _ in range(50):
        resp = await client.get(f"/api/flywheel/finetune/{job_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)

    assert status == "completed"
    detail = (await client.get(f"/api/flywheel/finetune/{job_id}")).json()
    assert detail["backend"] == "stub"          # 强制 stub 生效
    assert len(detail["metrics"]) > 0            # 有指标曲线
    assert detail["progress_ratio"] >= 0.0

    # 作业出现在列表里
    listing = await client.get("/api/flywheel/finetune")
    assert any(j["job_id"] == job_id for j in listing.json()["jobs"])


@pytest.mark.asyncio
async def test_get_unknown_job_returns_404(client):
    resp = await client.get("/api/flywheel/finetune/nonexistent-job-id")
    assert resp.status_code == 404
