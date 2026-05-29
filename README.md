<div align="center">

<img src="assets/symbio-logo.png" width="120" height="120" alt="Symbio">

# SYMBIO

### AI Infra 级多 Agent 协同框架

**不是套壳玩具，是真正的 AI 基础设施**

English · [中文](README_zh.md) · [日本語](README_ja.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Stars](https://img.shields.io/github/stars/854875058/Symbio?style=social)](https://github.com/854875058/Symbio)

</div>

---

## 为什么 Symbio 不一样

> 市面上的 Agent 框架只是 LLM 套壳。Symbio 是从底层重构的 AI 基础设施。

| 痛点 | 其他框架 | Symbio |
|------|----------|--------|
| Agent 过早宣布完成 | 依赖 EOS 自然停止 | **强制 Tool Calling + 测试验证闭环** |
| 通信成本爆炸 | Agent 互相对话传递 | **状态驱动零对话通信，Token -80%** |
| 记忆只是向量检索 | Top-K 相似度搜索 | **T-Box/A-Box 本体推理 + 零 Token 图推理** |
| 静态执行路径 | 预定义 Chain/Graph | **动态 DAG 运行时拓扑重构** |
| 黑盒无法调试 | print 打日志 | **OpenTelemetry 全链路追踪** |
| 安全靠信任 | 无防护或 Regex | **三层防御体系 + 绝对沙箱** |
| 不会进化 | Prompt 写死 | **数据飞轮 + 自动 Prompt 优化** |

---

## 核心设计哲学

### 1. 动态 DAG 拓扑中枢

> 兵无常势，水无常形

传统框架用固定的 Agent 链。Symbio 的 Orchestrator 将任务编译为动态有向无环图（Dynamic DAG），执行过程中如果某个节点返回预期外的结果，触发**拓扑重构**——动态增删、修剪或重组图节点。

**核心壁垒：** 彻底解决"一处报错，全盘崩溃"的痛点。

### 2. 强制 Tool Calling + 测试验证闭环

> 从根源解决"代理过早宣布完成"

```
传统：Agent 自己判断完成 → 依赖 EOS token → 经常提前停机
Symbio：Agent 必须调用 submit_task() → Testing Agent 执行 pytest → 用工程化结果取代主观判断
```

### 3. 状态驱动零对话通信

> 消除传话游戏，Token 成本降低 80%+

```
┌─────────────────────────────────────────┐
│       全局状态对象 (JSON Checklist)       │
│       Single Source of Truth             │
└──────────────────┬──────────────────────┘
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
   Initializer   Coder      Tester
   Agent         Agent      Agent
       │           │           │
       └───────────┴───────────┘
            状态读写，非对话传递
```

### 4. 本体化认知记忆

> 向量检索只能找相似片段，本体理解领域概念层次

- **T-Box（世界观约束）** — 不可动摇的本体元模型
- **A-Box（事实抽取）** — LLM 严密抽取标准化实体和关系
- **零 Token 图推理** — NetworkX 拓扑推理，传递性/继承性/逆属性

### 5. 神经符号安全防火墙

> 三层防御，0ms 硬截断

```
Layer 1: 正则匹配 + 敏感词库 → 0ms 硬截断
Layer 2: 轻量语义分类器 → 检测隐晦攻击
Layer 3: 意图偏离检测 → 监控 Agent 行为
```

### 6. 数据飞轮

> 运行即沉淀，越跑越值钱

```
Agent 执行 → 捕获轨迹 → 质量过滤 → PII 脱敏 → 导出微调数据集
     ↑                                                    │
     └──────────── 自动 Prompt 优化 ←──────────────────────┘
```

### 7. 事件溯源断点续传

> 借鉴数据库 WAL 日志，10ms 恢复现场

Agent 的每一次 Thought、Action、Observation 都是不可变事件。系统以秒级频率异步打成快照。断电、Pod 挂掉后，重启即重放整个 DAG 拓扑。

### 8. 语义缓存 + Prompt Cache 深度对齐

> 高频请求 Token 成本砍到零，响应速度提升 10 倍

"帮我写个快排" 和 "实现快速排序算法" 语义相同，直接复用缓存。cache_aligner 强制规范前缀排列顺序，确保 90%+ 静态前缀持续命中 Cache。

### 9. 前端可配置智能路由

> 用户掌控模型选择，而非被框架绑架

用户在 Web UI 自由配置模型池和任务-模型绑定策略。evaluator 从"语义复杂度、上下文长度、工具调用深度、金融成本"四维评估，router 优先查用户配置，找不到才自动选择。

### 10. 神经符号安全网关 + 背压流控

> 0ms 危险动作硬截断 + 自适应背压传播

- **guardrail** — 静态符号规则 + 语义分类器双引擎，0ms 硬截断
- **rate_limiter** — 令牌桶算法，429 时发送背压信号，动态降低 DAG 并发步长
- **resource_manager** — Token 预算 + 步数限制 + 超时，杜绝 OOM 和死循环

### 11. Ray-Native 分布式 Actor

> 横向推平万卡/万核集群

SubAgent 继承或被装饰为 Ray Actor。主 Agent 派发子任务时，整个子 Agent 作为有界 Actor 远程投递到集群空闲节点。本地开发时退化为轻量 asyncio.Task。

### 12. 黑格尔式共识辩论

> 用算力换确定性，消灭幻觉

高危任务自动触发三方辩论：
- **Proposer** — 提出方案
- **Critic** — 从安全、边界、最坏打算全面痛击
- **Refiner** — 综合辩论日志沉淀完美方案

内置数学终止条件，防止 Token 死循环。

### 13. MCP 原生协议网关

> 不造轮子，兼容最前沿工具生态

紧跟 Anthropic 开源标准，实现高并发 MCP 客户端/服务端代理。社区的数据库 MCP、GitHub MCP、私有工具，只要符合 JSON-RPC over stdio，一键挂载。

### 14. 瞬时弹性沙箱

> 高危操作物理隔离，执行完即销毁

- **本地** — 异步子进程 + resource_manager 内核配额
- **集群** — K8s API 秒级拉起无状态 Pod，NetworkPolicy 切断内网，执行完物理抹除

### 15. 异步人类介入总线

> 手机一键审批，不阻塞系统

Guardrail 判定需要人类确认时，冻结 DAG 节点状态，通过 Webhook 推送审批卡片到飞书/微信/WebUI。Orchestrator 腾出资源处理其他任务。人类点击"同意"后，任意空闲节点瞬间恢复执行。

### 16. 多租户记忆宇宙

> 每个项目是独立的记忆世界

- 记忆隔离 — 独立 LanceDB 表、本体图谱、向量空间
- 配置三级继承 — 全局 → 项目 → 任务
- 数据物理隔离 — 支持导入/导出/备份/恢复
- 资源配额 — 独立 Token 预算、存储配额

### 17. Agent 自我进化

> Prompt 效果追踪 + 自动优化 + A/B 测试

记录每次 Prompt 的成功率、Token 消耗、用户满意度。基于历史数据自动调整措辞。在线 A/B 测试对比不同版本效果。进化日志可审计可回滚。

### 18. 隐私计算

> 数据不出域，满足 GDPR/等保

- 联邦学习 — 多方协作训练，数据不离开本地
- 差分隐私 — 数学上保证隐私预算
- 数据脱敏 — 自动检测 PII、密钥、凭证
- 审计日志 — 所有数据访问可追溯

### 19. 边缘原生运行时

> Agent 无处不在，从云端到边缘到移动端

- 轻量级运行时 — 树莓派、Jetson 等资源受限设备
- 移动端 SDK — iOS/Android 原生，支持端侧推理
- 离线优先 — 断网自动切本地模型，联网后同步
- IoT 管理 — 通过 Agent 协调设备集群

### 20. 主动式内存管理

> 长时间运行不崩溃，内存不泄漏

- 实时监控 — 超过阈值自动告警
- 自动 GC — 清理过期会话历史、缓存、临时文件
- 泄漏检测 — 自动检测并报告潜在泄漏
- 资源限制 — 每个 Agent 的内存/CPU/磁盘上限

---

## 架构全景

```
┌─────────────────────────────────────────────────────────────┐
│                      🌐 接入层                               │
│      CLI  ·  Web UI  ·  Desktop  ·  IM (QQ/微信/飞书)       │
├─────────────────────────────────────────────────────────────┤
│                      🧠 调度中枢                             │
│  动态 DAG · 智能路由 · 复杂度评估 · 安全网关 · 资源管控       │
├─────────────────────────────────────────────────────────────┤
│                      👥 执行层                               │
│  主 Agent · SubAgent · 共识辩论 · 仿真测试 · 评测管道        │
├─────────────────────────────────────────────────────────────┤
│                      💾 基础层                               │
│  工具层 · 记忆系统 · 进化引擎 · 配置中心 · 安全模块          │
├─────────────────────────────────────────────────────────────┤
│                      📊 可观测层                             │
│  OpenTelemetry · Token 热力图 · 记忆快照回放 · DAG Trace     │
└─────────────────────────────────────────────────────────────┘
```

---

## 33 个杀手级亮点

<details>
<summary><b>🧠 核心引擎 (7)</b></summary>

- **动态 DAG** — 运行时拓扑重构，"兵无常势水无常形"
- **智能路由** — 前端可配置模型池，Pareto 前沿投机路由
- **上下文剪枝** — 语义级压缩，裁剪无用中间体
- **Prompt Cache 对齐** — 90%+ 前缀命中，斩断 50%+ 成本
- **防过早完成** — 强制 Tool Calling + 测试验证闭环
- **语义缓存** — 相似请求复用，Token 成本砍到零
- **预算熔断** — max_cost_usd / max_steps / 超时三重保护

</details>

<details>
<summary><b>👥 多 Agent (4)</b></summary>

- **SubAgent 派发** — Ray-Native 分布式 Actor 运行时
- **共识辩论** — 黑格尔式正反合：Proposer + Critic + Refiner
- **状态驱动通信** — 全局状态对象，零对话传递
- **仿真测试** — 场景模拟 + 边界行为发现 + 回归测试

</details>

<details>
<summary><b>💾 记忆系统 (3)</b></summary>

- **本体化记忆** — T-Box/A-Box 分离，零 Token 图推理
- **项目级隔离** — 每个项目独立的"记忆宇宙"
- **记忆衰减** — 基于时间和访问频率的智能遗忘

</details>

<details>
<summary><b>🛠️ 工具与安全 (5)</b></summary>

- **MCP 原生支持** — 标准化工具挂载，即插即用
- **绝对沙箱** — 容器化物理隔离
- **Prompt Injection 防护** — 三层防御体系
- **并发流控** — 令牌桶算法 + 背压信号
- **资源管控** — Token 预算 + 步数限制 + 超时控制

</details>

<details>
<summary><b>🚀 进化与智能 (5)</b></summary>

- **数据飞轮** — 轨迹捕获 → 微调数据集导出
- **Agent 自我进化** — Prompt 效果追踪 + 自动优化
- **PromptOps** — 版本管理 + A/B 测试 + 灰度发布
- **评测管道** — 自动化回归检测
- **根因分析** — 失败任务自动复盘

</details>

<details>
<summary><b>🌐 接入与协议 (5)</b></summary>

- **HITL 人类介入** — IM 异步审批，手机一键授权
- **A2A 协议** — 与外部 Agent 互通
- **Computer Use** — 屏幕截图 → 视觉理解 → GUI 操控
- **多模态原生** — 图片/文档/音频统一处理
- **OpenTelemetry** — 全链路 Trace 可视化

</details>

<details>
<summary><b>🔒 企业级 (4)</b></summary>

- **隐私计算** — 联邦学习 + 差分隐私
- **边缘计算** — 云-边-端分层部署
- **版本兼容** — 无感知平滑升级
- **内存管理** — 主动式 GC + 泄漏检测

</details>

---

## 快速开始

```bash
# 安装
pip install symbio

# 初始化
symbio init

# 启动
symbio
```

Web UI: `http://localhost:9090/ui`

---

## 技术栈

| 层级 | 选型 |
|------|------|
| 核心 | Python 3.10+ · asyncio · Pydantic v2 |
| Agent | 自研动态 DAG · Ray (可选) |
| 记忆 | LanceDB · NetworkX · 本体推理 |
| 工具 | MCP · Claude Code · Shell · Git |
| 前端 | Next.js · shadcn/ui · WebSocket |
| 可观测 | OpenTelemetry · Jaeger · Grafana |
| 存储 | aiosqlite · LanceDB · Redis (可选) |

---

## 文档

| 文档 | 说明 |
|------|------|
| [功能清单](docs/features.md) | 33 个杀手级亮点详细定义 |
| [架构设计](docs/architecture.md) | 四层架构 + 安全/可观测性 |
| [模块设计白皮书](docs/module-design-whitepaper.md) | 17 个模块的设计哲学 |
| [UI 设计方案](docs/ui-design.md) | 28 个页面 + 组件系统 |
| [开发路线图](docs/roadmap.md) | 10 Phase 开发计划 |
| [模块规划](docs/modules.md) | 模块目录树 + 代码骨架 |
| [技术栈选型](docs/tech-stack.md) | 技术选型 + 依赖管理 |

---

## 设计原则

1. **解耦优先** — 模块间通过抽象接口通信
2. **本地优先** — 默认轻量级单机，集群作为扩展
3. **防御优先** — 安全、流控、熔断是第一道门槛
4. **智能优先** — LLM 做决策，符号做约束，向量做检索
5. **进化优先** — 运行即沉淀，越用越聪明
6. **体验优先** — 5 分钟上手，渐进式复杂度

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=854875058/Symbio&type=Date)](https://star-history.com/#854875058/Symbio&Date)

---

<div align="center">

**Symbio — 不是套壳玩具，是真正的 AI 基础设施**

*大处着眼，小处着手。Think Big, Start Small.*

</div>
