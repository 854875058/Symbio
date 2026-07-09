# Symbio 基准（benchmarks）

把 README 里"五层成本优化"这类**声称**变成**可复现的实测数字**。

## 成本优化基准

```bash
python benchmarks/bench_cost_optimization.py          # 人读表格
python benchmarks/bench_cost_optimization.py --json   # 机读 JSON
```

固定合成负载，不联网、不花钱（语义缓存走本地 embedding，路由/剪枝纯规则），
同一份负载跑多少次结果一致。测三个成本优化组件：

| 组件 | 指标 | 含义 |
|------|------|------|
| 语义缓存 | 改写查询命中率 | 换个说法问同一件事，能否命中已有答案（省一次 LLM 调用） |
| 分层路由 | LLM 调用避免率 | 多少 DAG 节点结果无需上 LLM（成功/瞬态错误规则处理） |
| 上下文剪枝 | 压缩率 | 长上下文裁到目标 token 后的占比（越低省越多） |

### 参考结果（2026-07-09，本机 all-MiniLM-L6-v2）

```
[1] 语义缓存    命中率 30.0%（10 组难改写查询，阈值 0.75，本地 ST embedding）
[2] 分层路由    LLM 避免率 85.0%（100 节点，70 成功 + 15 瞬态无需 LLM）
[3] 上下文剪枝  压缩率 25.0%（8000 → 2000 token，移除 20 条冗余消息）
```

> 这些数字是"这套机制在这份负载下的表现"，不是营销值——换负载会变，
> 这正是重点：给出可审计的方法而非空口断言。语义缓存命中率偏低反映本地
> embedding 对跨表述改写的语义匹配能力有限；接入 OpenAI embedding 会显著提升。

冒烟测试见 `tests/test_bench_cost_optimization.py`（防脚本随代码腐烂）。
