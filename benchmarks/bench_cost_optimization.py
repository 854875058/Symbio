"""成本优化真实收益基准 — 把"声称"变成"实测数字"。

Symbio README 声称"五层成本优化""语义缓存""分层路由"能省 token，但一直没有
可复现的量化证据。本脚本用固定合成负载跑三个成本优化组件，输出实测指标：

  1. 语义缓存命中率 + 估算 token 节省（改写查询能否命中）
  2. 分层路由 LLM 调用避免率（多少节点结果无需上强模型）
  3. 上下文剪枝压缩率（长上下文裁到目标后的 token 比）

用法：
    python benchmarks/bench_cost_optimization.py              # 人读表格
    python benchmarks/bench_cost_optimization.py --json        # 机读 JSON

设计原则：
  - 负载固定、可复现（同一份查询/节点/消息），跑多少次结果一致
  - 不联网、不花钱：语义缓存走本地 embedding 降级，路由/剪枝是纯规则
  - 指标是"这套机制在这份负载下的表现"，不是营销数字；换负载会变，
    这正是重点——给出可审计的方法而非空口断言
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

# 允许直接 python benchmarks/xxx.py 运行（把 src 加进路径）
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))


# ---------------------------------------------------------------------------
# 1) 语义缓存命中率
# ---------------------------------------------------------------------------
# 负载：10 组"原问 + 改写"。先把原问答案写入缓存，再用改写问去查，
# 看语义缓存能否跨表述命中。命中率越高 = 越多 LLM 调用被省掉。
_CACHE_PAIRS = [
    ("帮我写个快速排序", "用 Python 实现快排算法"),
    ("怎么读取一个 JSON 文件", "如何用代码解析 JSON 文件"),
    ("解释一下什么是闭包", "闭包是什么意思"),
    ("如何反转一个链表", "链表反转怎么写"),
    ("介绍下 HTTP 和 HTTPS 区别", "HTTP 与 HTTPS 有什么不同"),
    ("写一个二分查找", "二分搜索算法怎么实现"),
    ("怎么去重一个列表", "如何给数组去掉重复元素"),
    ("解释 Python 的 GIL", "什么是全局解释器锁"),
    ("如何捕获异常", "怎么用 try except 处理错误"),
    ("斐波那契数列怎么算", "写一个求 fibonacci 的函数"),
]


async def bench_semantic_cache() -> dict:
    from symbio.core.semantic_cache import SemanticCacheConfig, SemanticCacheEngine

    tmp = tempfile.mkdtemp(prefix="bench_cache_")
    eng = SemanticCacheEngine(SemanticCacheConfig(
        lancedb_path=str(Path(tmp) / "lancedb"),
        local_embedding_fallback=True,
        similarity_threshold=0.75,  # 本地 embedding 语义分偏低，用略宽阈值
    ))
    await eng.initialize()

    # 写入原问答案（response 用一段有长度的文本，便于估算 token 节省）
    answer = "这里是一段示例回答内容，" * 20
    for original, _ in _CACHE_PAIRS:
        await eng.put(query=original, response=answer, model="bench")

    t0 = time.perf_counter()
    for _, paraphrase in _CACHE_PAIRS:
        await eng.get(paraphrase)
    elapsed = time.perf_counter() - t0

    stats = eng.get_stats()
    await eng.close()

    return {
        "backend": eng._embedding_backend,
        "dim": eng._effective_dim,
        "paraphrase_queries": len(_CACHE_PAIRS),
        "hits": stats.cache_hits,
        "misses": stats.cache_misses,
        "hit_rate": round(stats.hit_rate, 4),
        "estimated_token_saved": stats.estimated_token_saved,
        "avg_query_ms": round(elapsed / len(_CACHE_PAIRS) * 1000, 2),
    }


# ---------------------------------------------------------------------------
# 2) 分层路由 LLM 调用避免率
# ---------------------------------------------------------------------------
# 负载：100 个 DAG 节点结果，按典型分布构造——成功 / 瞬态错误（可规则重试）
# 无需 LLM，未知错误上 haiku，结构性错误上 opus。避免率 = 无需 LLM 的比例。
# 这直接对应 README 里"70/15/10/5 分层路由"的省钱逻辑。
def bench_layered_router() -> dict:
    from symbio.core.layered_router import ClassificationResult, LayeredRouter

    router = LayeredRouter()

    # 构造 100 个结果：70 成功、15 瞬态、10 未知、5 结构性
    workload = []
    workload += [("ok", None)] * 70
    workload += [("", TimeoutError("connection timeout")) for _ in range(15)]
    workload += [("", RuntimeError("some unexpected weird failure")) for _ in range(10)]
    workload += [("", ValueError("schema mismatch: incompatible type")) for _ in range(5)]

    counts = {c: 0 for c in ClassificationResult}
    llm_calls = 0
    tier_calls = {"haiku": 0, "opus": 0}

    for i, (result, error) in enumerate(workload):
        decision = router.classify_result(f"node_{i}", result=result or None, error=error)
        counts[decision.classification] += 1
        use_llm, tier = router.should_use_llm(decision.classification)
        if use_llm:
            llm_calls += 1
            if tier in tier_calls:
                tier_calls[tier] += 1

    total = len(workload)
    return {
        "total_nodes": total,
        "success": counts[ClassificationResult.SUCCESS],
        "transient_error": counts[ClassificationResult.TRANSIENT_ERROR],
        "unknown_error": counts[ClassificationResult.UNKNOWN_ERROR],
        "structural_error": counts[ClassificationResult.STRUCTURAL_ERROR],
        "llm_calls": llm_calls,
        "llm_avoided": total - llm_calls,
        "llm_avoidance_rate": round((total - llm_calls) / total, 4),
        "haiku_calls": tier_calls["haiku"],
        "opus_calls": tier_calls["opus"],
    }


# ---------------------------------------------------------------------------
# 3) 上下文剪枝压缩率
# ---------------------------------------------------------------------------
# 负载：一段 60 条消息的长对话（含冗长工具输出），目标裁到 2000 token。
# 压缩率 = 裁后 token / 裁前 token，越低省得越多。
def bench_context_pruner() -> dict:
    from symbio.core.context_pruner import (
        ContextMessage,
        ContextPruner,
        MessageRole,
        PrunerConfig,
        PruneStrategy,
    )

    messages = []
    for i in range(60):
        if i % 3 == 0:
            role, tokens, content = MessageRole.USER, 40, f"用户第 {i} 轮提问，描述一个需求。"
        elif i % 3 == 1:
            role, tokens, content = MessageRole.ASSISTANT, 60, f"助手第 {i} 轮回答，给出方案。"
        else:
            role, tokens, content = MessageRole.TOOL, 300, f"工具第 {i} 轮输出，一大段冗长日志。" * 10
        messages.append(ContextMessage(role=role, content=content, token_count=tokens))

    pruner = ContextPruner(PrunerConfig())
    target = 2000
    result = pruner.prune(messages, strategy=PruneStrategy.FULL, target_token_count=target)

    return {
        "original_messages": len(messages),
        "original_tokens": result.original_token_count,
        "target_tokens": target,
        "pruned_tokens": result.pruned_token_count,
        "compression_ratio": round(result.compression_ratio, 4),
        "tokens_saved": result.original_token_count - result.pruned_token_count,
        "removed_messages": len(result.removed_messages),
        "compressed_messages": len(result.compressed_messages),
    }


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------
async def run_all() -> dict:
    return {
        "semantic_cache": await bench_semantic_cache(),
        "layered_router": bench_layered_router(),
        "context_pruner": bench_context_pruner(),
    }


def _print_human(report: dict) -> None:
    sc = report["semantic_cache"]
    lr = report["layered_router"]
    cp = report["context_pruner"]

    print("=" * 68)
    print("Symbio 成本优化基准（固定合成负载，可复现）")
    print("=" * 68)

    print("\n[1] 语义缓存 — 改写查询命中率")
    print(f"    embedding 后端 : {sc['backend']} (dim={sc['dim']})")
    print(f"    改写查询数     : {sc['paraphrase_queries']}")
    print(f"    命中 / 未命中  : {sc['hits']} / {sc['misses']}")
    print(f"    命中率         : {sc['hit_rate']:.1%}")
    print(f"    估算省 token   : {sc['estimated_token_saved']}")
    print(f"    平均查询耗时   : {sc['avg_query_ms']} ms")

    print("\n[2] 分层路由 — LLM 调用避免率")
    print(f"    节点总数       : {lr['total_nodes']}")
    print(f"    成功/瞬态/未知/结构 : "
          f"{lr['success']}/{lr['transient_error']}/{lr['unknown_error']}/{lr['structural_error']}")
    print(f"    需调 LLM       : {lr['llm_calls']} (haiku {lr['haiku_calls']} / opus {lr['opus_calls']})")
    print(f"    LLM 避免率     : {lr['llm_avoidance_rate']:.1%}")

    print("\n[3] 上下文剪枝 — 压缩率")
    print(f"    原始消息/token : {cp['original_messages']} 条 / {cp['original_tokens']} token")
    print(f"    目标 token     : {cp['target_tokens']}")
    print(f"    裁后 token     : {cp['pruned_tokens']}")
    print(f"    压缩率         : {cp['compression_ratio']:.1%} (越低省越多)")
    print(f"    省下 token     : {cp['tokens_saved']}")
    print(f"    移除/压缩消息  : {cp['removed_messages']} / {cp['compressed_messages']}")
    print("=" * 68)


def main() -> None:
    parser = argparse.ArgumentParser(description="Symbio 成本优化真实收益基准")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非人读表格")
    args = parser.parse_args()

    report = asyncio.run(run_all())

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)


if __name__ == "__main__":
    main()
