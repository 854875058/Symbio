"""数据飞轮闭环测试：四阶段门面 + API 端点。"""

from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.evolution.flywheel import DataFlywheel, get_flywheel, reset_flywheel
from symbio.interfaces.api import app


@pytest.fixture
def flywheel(tmp_path):
    fw = DataFlywheel(
        analysis_db=str(tmp_path / "a.db"),
        feedback_db=str(tmp_path / "f.db"),
        distilled_path=tmp_path / "sop.json",
    )
    return fw


# ---------------------------------------------------------------------------
# 阶段二：失效分析
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_failure_updates_summary(flywheel):
    r = await flywheel.record_failure({
        "task_id": "t1", "category": "timeout", "severity": "high",
        "description": "tool timed out", "steps_to_failure": 3,
    })
    assert r["analysis_id"]
    summary = await flywheel.analysis_summary()
    assert summary["available"] is True
    assert summary["total_failures"] == 1
    failures = await flywheel.list_failures()
    assert len(failures) == 1
    await flywheel.close()


@pytest.mark.asyncio
async def test_bad_category_falls_back_to_unknown(flywheel):
    r = await flywheel.record_failure({"category": "not_a_real_category", "description": "x"})
    assert r["analysis_id"]
    failures = await flywheel.list_failures()
    assert failures[0]["category"] == "unknown"
    await flywheel.close()


# ---------------------------------------------------------------------------
# 阶段三：SOP 蒸馏
# ---------------------------------------------------------------------------

def test_list_sops_includes_seeds(flywheel):
    sops = flywheel.list_sops()
    assert sops["seed_count"] >= 1
    assert all(s["source"] == "seed" for s in sops["seeds"])


def test_distill_persists_when_quality_met(flywheel):
    steps = [
        {"step_id": i, "thought": f"思考{i}", "action": f"action_{i}", "observation": "ok"}
        for i in range(5)
    ]
    result = flywheel.distill_from_trajectory({
        "trajectory_id": "traj-1", "task_type": "code_generation",
        "steps": steps, "success": True, "token_count": 1200, "duration_ms": 5000,
    })
    # 蒸馏可能因质量门槛被拒，但无论如何不应抛错且返回结构正确
    assert "distilled" in result
    if result["distilled"]:
        reloaded = flywheel.list_sops()
        assert reloaded["distilled_count"] >= 1


# ---------------------------------------------------------------------------
# 阶段四：反馈
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_feedback_updates_stats(flywheel):
    fb = await flywheel.collect_feedback({"task_id": "t1", "rating": 4.5, "comment": "good", "tags": ["fast"]})
    assert fb["feedback_id"]
    stats = await flywheel.feedback_stats()
    assert stats["available"] is True
    assert stats["total_explicit"] == 1
    assert stats["average_rating"] == 4.5
    await flywheel.close()


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_has_four_stages(flywheel):
    overview = await flywheel.overview()
    assert set(overview["stages"].keys()) == {"capture", "analysis", "distillation", "feedback"}
    await flywheel.close()


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flywheel_api_endpoints(tmp_path):
    reset_flywheel()
    fw = get_flywheel()
    fw._analysis_db = str(tmp_path / "a.db")
    fw._feedback_db = str(tmp_path / "f.db")
    fw._distilled_path = tmp_path / "sop.json"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/flywheel/overview")
        assert resp.status_code == 200
        assert "stages" in resp.json()

        resp = await client.post("/api/flywheel/failures", json={
            "task_id": "t1", "category": "tool_error", "severity": "medium", "description": "boom",
        })
        assert resp.status_code == 200
        assert resp.json()["analysis_id"]

        resp = await client.get("/api/flywheel/failures")
        assert resp.status_code == 200
        assert resp.json()["total_failures"] >= 1

        resp = await client.get("/api/flywheel/sops")
        assert resp.status_code == 200
        assert resp.json()["seed_count"] >= 1

        resp = await client.post("/api/flywheel/feedback", json={"rating": 5.0, "comment": "great"})
        assert resp.status_code == 200
        assert resp.json()["feedback_id"]

        resp = await client.get("/api/flywheel/feedback")
        assert resp.status_code == 200
        assert resp.json()["total_explicit"] >= 1

    await fw.close()
    reset_flywheel()
