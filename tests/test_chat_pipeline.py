"""聊天成本优化管线测试：语义缓存降级、上下文剪枝、成本记录、预算、API 端点。"""

from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.chat_pipeline import ChatPipeline, get_chat_pipeline, reset_chat_pipeline
from symbio.core.cost_monitor import BudgetManager, CostTracker
from symbio.interfaces.api import app


@pytest.fixture(autouse=True)
def _fresh_pipeline():
    reset_chat_pipeline()
    yield
    reset_chat_pipeline()


# ---------------------------------------------------------------------------
# 上下文剪枝
# ---------------------------------------------------------------------------


def test_prune_history_keeps_short_history_unchanged():
    pipeline = ChatPipeline()
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你？"},
        {"role": "user", "content": "介绍一下 Symbio"},
    ]
    pruned, info = pipeline.prune_history(messages, max_tokens=8000)
    assert pruned == messages
    assert info is None


def test_prune_history_shrinks_long_history():
    pipeline = ChatPipeline()
    filler = "这是一段很长的填充内容。" * 200  # 每条约 600+ tokens
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"问题{i}: {filler}"})
        messages.append({"role": "assistant", "content": f"回答{i}: {filler}"})
    messages.append({"role": "user", "content": "当前的提问"})

    pruned, info = pipeline.prune_history(messages, max_tokens=2000)

    assert info is not None
    assert info["removed_count"] > 0
    assert len(pruned) < len(messages)
    # 当前提问必须保留在最后
    assert pruned[-1]["content"].endswith("当前的提问")
    # 首条必须是 user，且无相邻同角色消息
    assert pruned[0]["role"] == "user"
    for prev, cur in zip(pruned, pruned[1:]):
        assert prev["role"] != cur["role"]


def test_prune_history_empty_input():
    pipeline = ChatPipeline()
    pruned, info = pipeline.prune_history([], max_tokens=100)
    assert pruned == []
    assert info is None


def test_normalize_alternation_merges_and_leads_with_user():
    merged = ChatPipeline._normalize_alternation(
        [
            {"role": "assistant", "content": "孤儿回答"},
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"},
        ]
    )
    assert merged[0]["role"] == "user"
    assert merged[0]["content"] == "a\n\nb"
    assert merged[1]["content"] == "c"


# ---------------------------------------------------------------------------
# 语义缓存（无 embedding key 时整层优雅关闭）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_available_by_default_with_local_fallback():
    # 即使没有 OpenAI key，本地 embedding 降级让语义缓存默认可用
    pipeline = ChatPipeline()
    assert pipeline.cache_available() is True


@pytest.mark.asyncio
async def test_cache_disabled_when_semantic_cache_off():
    from symbio.config.settings import get_settings

    settings = get_settings()
    prev = settings.cost.semantic_cache_enabled
    settings.cost.semantic_cache_enabled = False
    try:
        pipeline = ChatPipeline()
        assert pipeline.cache_available() is False
        assert await pipeline.lookup_cache("任意问题", model="m", context_hash="h") is None
    finally:
        settings.cost.semantic_cache_enabled = prev


@pytest.mark.asyncio
async def test_local_cache_roundtrip(tmp_path, monkeypatch):
    # 用临时 lancedb 路径，验证本地降级下 store -> lookup 命中
    from symbio.config.settings import get_settings

    if get_settings().model.openai_api_key:
        pytest.skip("环境配置了真实 embedding key（走远程后端）")
    monkeypatch.setenv("SYMBIO_MEMORY_LANCEDB_PATH", str(tmp_path / "lancedb"))
    get_settings.cache_clear()
    try:
        pipeline = ChatPipeline()
        assert pipeline.cache_available() is True
        await pipeline.store_cache("帮我写个快速排序算法", "QUICKSORT", model="m", context_hash="h")
        hit = await pipeline.lookup_cache("帮我写个快速排序算法", model="m", context_hash="h")
        assert hit is not None
        assert hit["content"] == "QUICKSORT"
        miss = await pipeline.lookup_cache("今天天气如何", model="m", context_hash="h")
        assert miss is None
    finally:
        get_settings.cache_clear()


def test_context_hash_distinguishes_history():
    h1 = ChatPipeline.context_hash([{"role": "user", "content": "a"}])
    h2 = ChatPipeline.context_hash([{"role": "user", "content": "b"}])
    h3 = ChatPipeline.context_hash([{"role": "user", "content": "a"}])
    assert h1 != h2
    assert h1 == h3


# ---------------------------------------------------------------------------
# 成本记录与预算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_and_summary(tmp_path):
    pipeline = ChatPipeline()
    pipeline._cost_tracker = CostTracker(tmp_path / "cost.db")

    await pipeline.record_usage(
        session_id="sess-1",
        model="claude-sonnet-4-20250514",
        input_tokens=120,
        output_tokens=80,
    )
    await pipeline.record_usage(
        session_id="sess-1",
        model="claude-sonnet-4-20250514",
        input_tokens=60,
        output_tokens=40,
    )

    summary = await pipeline.cost_summary(period_hours=1)
    assert summary["available"] is True
    assert summary["total_tokens"] == 300
    assert summary["total_requests"] == 2
    assert summary["top_model"] == "claude-sonnet-4-20250514"

    await pipeline._cost_tracker.close()


@pytest.mark.asyncio
async def test_budget_set_and_check(tmp_path):
    pipeline = ChatPipeline()
    tracker = CostTracker(tmp_path / "cost.db")
    pipeline._cost_tracker = tracker
    pipeline._budget_manager = BudgetManager(tracker, tmp_path / "budget.db")

    await pipeline.record_usage(
        session_id="default",
        model="claude-sonnet-4-20250514",
        input_tokens=900,
        output_tokens=100,
    )
    status = await pipeline.set_budget("default", 2000)
    assert status["available"] is True
    assert status["monthly_limit_tokens"] == 2000
    assert status["consumed_tokens"] == 1000
    assert status["percentage_used"] == pytest.approx(0.5)
    assert status["is_exceeded"] is False

    await pipeline._budget_manager.close()
    await tracker.close()


@pytest.mark.asyncio
async def test_budget_unset_is_unlimited(tmp_path):
    pipeline = ChatPipeline()
    tracker = CostTracker(tmp_path / "cost.db")
    pipeline._cost_tracker = tracker
    pipeline._budget_manager = BudgetManager(tracker, tmp_path / "budget.db")

    status = await pipeline.budget_status("nobudget")
    assert status["available"] is True
    assert status["monthly_limit_tokens"] == 0
    assert status["remaining_tokens"] == -1
    assert status["is_exceeded"] is False

    await pipeline._budget_manager.close()
    await tracker.close()


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_costs_api_endpoints(tmp_path):
    pipeline = get_chat_pipeline()
    tracker = CostTracker(tmp_path / "cost.db")
    pipeline._cost_tracker = tracker
    pipeline._budget_manager = BudgetManager(tracker, tmp_path / "budget.db")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/costs/summary")
        assert resp.status_code == 200
        assert resp.json()["available"] is True

        resp = await client.get("/api/costs/cache")
        assert resp.status_code == 200
        assert "enabled" in resp.json()

        resp = await client.post(
            "/api/costs/budget",
            json={"project_id": "default", "monthly_limit_tokens": 100000},
        )
        assert resp.status_code == 200
        assert resp.json()["monthly_limit_tokens"] == 100000

        resp = await client.get("/api/costs/budget?project_id=default")
        assert resp.status_code == 200
        assert resp.json()["monthly_limit_tokens"] == 100000

        resp = await client.post(
            "/api/costs/budget",
            json={"project_id": "default", "monthly_limit_tokens": -5},
        )
        assert resp.status_code == 400

        resp = await client.get("/api/costs/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"summary", "cache", "budget"}

    await pipeline._budget_manager.close()
    await tracker.close()
