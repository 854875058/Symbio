"""成本优化基准脚本的冒烟测试。

只验证基准能跑通、指标字段齐全且落在合理区间——防止基准脚本随代码演进腐烂。
不断言具体数值（换负载/换 embedding 会变），只断言"结构对、方向对"。
语义缓存那段要加载 ST 模型，标记 slow；路由/剪枝是纯规则，无条件快跑。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# 动态加载 benchmarks/bench_cost_optimization.py（不是包，用 spec 加载）
_BENCH_PATH = Path(__file__).parent.parent / "benchmarks" / "bench_cost_optimization.py"
_spec = importlib.util.spec_from_file_location("bench_cost_optimization", _BENCH_PATH)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_layered_router_bench_shape_and_bounds():
    r = bench.bench_layered_router()
    assert r["total_nodes"] == 100
    # 分类计数之和等于总数
    assert (r["success"] + r["transient_error"]
            + r["unknown_error"] + r["structural_error"]) == 100
    # 成功 + 瞬态无需 LLM；未知 + 结构才调 LLM
    assert r["llm_calls"] == r["unknown_error"] + r["structural_error"]
    assert r["llm_avoided"] == r["success"] + r["transient_error"]
    assert 0.0 <= r["llm_avoidance_rate"] <= 1.0
    # 结构性错误应走 opus，未知走 haiku
    assert r["opus_calls"] == r["structural_error"]
    assert r["haiku_calls"] == r["unknown_error"]


def test_context_pruner_bench_shape_and_bounds():
    r = bench.bench_context_pruner()
    assert r["original_tokens"] > r["target_tokens"]
    # 裁后不超过目标，压缩率在 (0, 1]
    assert r["pruned_tokens"] <= r["target_tokens"]
    assert 0.0 < r["compression_ratio"] <= 1.0
    assert r["tokens_saved"] == r["original_tokens"] - r["pruned_tokens"]
    assert r["tokens_saved"] > 0


@pytest.mark.slow
@pytest.mark.asyncio
async def test_semantic_cache_bench_runs():
    r = await bench.bench_semantic_cache()
    assert r["paraphrase_queries"] == 10
    assert r["hits"] + r["misses"] == 10
    assert 0.0 <= r["hit_rate"] <= 1.0
    # 至少写入的原问自身语义应可命中，命中数非负、token 节省非负
    assert r["hits"] >= 0
    assert r["estimated_token_saved"] >= 0
