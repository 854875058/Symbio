<div align="center">

# 🧬 Symbio（共生）

### AI Infra 级多 Agent 协同框架

**从"多 Agent 调度工具"升维为"具备自我进化能力、能在复杂/隔离环境中稳定运行的 AI 基础设施"**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Stars](https://img.shields.io/github/stars/854875058/Symbio?style=social)](https://github.com/854875058/Symbio)

[文档](docs/) · [功能清单](docs/features.md) · [架构设计](docs/architecture.md) · [模块白皮书](docs/module-design-whitepaper.md) · [UI 设计](docs/ui-design.md)

</div>

---

## 为什么选择 Symbio？

| 痛点 | Symbio 解决方案 |
|------|-----------------|
| Agent 框架只是 LLM 套壳 | 动态 DAG 拓扑引擎，运行时自适应重构 |
| 记忆系统只是向量检索 | T-Box/A-Box 本体化记忆，零 Token 图推理 |
| Agent 过早宣布完成 | 强制 Tool Calling 结束 + 测试验证闭环 |
| 通信成本爆炸 | 状态驱动通信，Token 成本降低 80%+ |
| 单机无法扩展 | Ray-Native 分布式 Actor，横向推平万卡集群 |
| 安全靠信任 | 神经符号混合网关 + 绝对沙箱隔离 |
| 数据白白浪费 | 数据飞轮，运行即沉淀高质量微调语料 |

---

## 核心特性 (33 个杀手级亮点)

### 🧠 核心引擎
- **动态 DAG 拓扑** — 运行时拓扑演化，"兵无常势，水无常形"
- **智能路由矩阵** — 前端可配置模型池，Pareto 前沿投机路由
- **上下文智能剪枝** — 语义级压缩，Prompt Cache 深度对齐
- **防过早完成** — 强制 Tool Calling + 测试验证闭环

### 👥 多 Agent 协同
- **SubAgent 动态派发** — Ray-Native 分布式 Actor 运行时
- **共识辩论机制** — 黑格尔式正反合多维辩论系统
- **状态驱动通信** — 全局状态对象，零对话传递

### 💾 认知记忆
- **本体化记忆** — T-Box/A-Box 分离的神经符号认知图谱
- **语义缓存** — 相似请求复用结果，Token 成本砍到零
- **项目级隔离** — 每个项目独立的"记忆宇宙"

### 🛠️ 工具与安全
- **MCP 原生支持** — 标准化工具挂载，即插即用
- **绝对沙箱** — 容器化/微型虚拟机物理隔离
- **Prompt Injection 防护** — 三层防御体系，0ms 硬截断

### 🚀 进化与智能
- **数据飞轮** — 轨迹捕获 → 微调数据集自动导出
- **Agent 自我进化** — Prompt 效果追踪 + 自动优化
- **评测管道** — 自动化回归检测，防止系统退化

### 🌐 接入与协议
- **HITL 人类介入** — IM 异步审批，手机一键授权
- **A2A 协议** — 与外部 Agent 互通
- **Computer Use** — 屏幕截图 → 视觉理解 → GUI 操控
- **多模态原生** — 图片/文档/音频统一处理

### 📊 可观测性
- **OpenTelemetry 链路追踪** — 全链路 Trace 可视化
- **Token 消耗热力图** — 实时成本监控
- **记忆快照回放** — 断点恢复，复现调试

### 🔒 企业级特性
- **隐私计算** — 联邦学习 + 差分隐私
- **边缘计算** — 云-边-端分层部署
- **版本兼容** — 无感知平滑升级
- **PromptOps** — Prompt 版本管理 + A/B 测试

---

## 快速开始

```bash
# 安装
pip install symbio

# 初始化项目
symbio init

# 启动服务
symbio start
```

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    接入层 (Interface)                        │
│     CLI  ·  Web UI  ·  Desktop  ·  IM (QQ/微信/飞书)        │
├─────────────────────────────────────────────────────────────┤
│                    调度中枢 (Orchestrator)                    │
│     动态 DAG  ·  智能路由  ·  复杂度评估  ·  安全网关          │
├─────────────────────────────────────────────────────────────┤
│                    执行层 (Agent Layer)                      │
│     主 Agent  ·  SubAgent  ·  共识辩论  ·  仿真测试           │
├─────────────────────────────────────────────────────────────┤
│                    基础层 (Foundation)                        │
│     工具层  ·  记忆系统  ·  进化引擎  ·  配置中心              │
└─────────────────────────────────────────────────────────────┘
```

---

## 文档体系

| 文档 | 说明 |
|------|------|
| [功能清单](docs/features.md) | 33 个杀手级亮点详细定义 |
| [架构设计](docs/architecture.md) | 四层架构 + 安全/可观测性/HITL 架构 |
| [模块设计白皮书](docs/module-design-whitepaper.md) | 17 个模块的超前设计哲学 |
| [UI 设计方案](docs/ui-design.md) | 28 个页面 + 组件系统 + 交互设计 |
| [开发路线图](docs/roadmap.md) | 10 Phase 完整开发计划 |
| [模块规划](docs/modules.md) | 模块目录树 + 代码骨架 |
| [技术栈选型](docs/tech-stack.md) | 技术选型 + 依赖管理 |
| [参考项目](docs/references.md) | 竞品对标 + 可借鉴项目 |

---

## 技术栈

| 层级 | 选型 |
|------|------|
| 核心 | Python 3.10+ · asyncio · uvloop |
| Agent | 自研动态 DAG · Ray (可选) |
| 记忆 | LanceDB · NetworkX · 本体推理 |
| 工具 | MCP · Claude Code · Shell · Git |
| 前端 | Next.js 15 · shadcn/ui · Zustand |
| 可观测 | OpenTelemetry · Jaeger · Grafana |
| 存储 | aiosqlite · LanceDB · Redis (可选) |
| 部署 | Docker · K8s (可选) · Tauri |

---

## 开发路线图

| 阶段 | 优先级 | 核心交付物 |
|------|--------|------------|
| Phase 1 核心骨架 | **P0** | 动态 DAG + 三大防御网关 + CLI |
| Phase 2 多 Agent | **P0** | SubAgent 派发 + 共识辩论 |
| Phase 3 记忆系统 | **P1** | LanceDB + 本体推理图谱 |
| Phase 4 工具层 | **P1** | MCP + Claude Code + 沙箱 |
| Phase 5 接入层 | **P2** | IM + HITL + WebUI |
| Phase 6 进化引擎 | **P2** | 数据飞轮 + 评测管道 |
| Phase 7 安全合规 | **P2** | Injection 防护 + 语义缓存 + 多模态 |
| Phase 8 高级功能 | **P3** | 前沿协议 + 隐私计算 + 边缘计算 |

---

## 贡献

我们欢迎任何形式的贡献！请阅读 [贡献指南](CONTRIBUTING.md)。

---

## 许可证

MIT License - 自由使用，自由修改，自由分发。

---

<div align="center">

**Symbio — 让 AI Agent 不再是套壳玩具**

*大处着眼，小处着手。Think Big, Start Small.*

</div>
