# Symbio 技术栈选型

## 核心运行时

| 组件 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.10+ | AI 生态成熟，长时运行异步支持完美 |
| 异步框架 | asyncio + uvloop | 替换原生 EventLoop，提供极致的高并发异步 I/O 性能 |
| 配置管理 | pydantic-settings | 类型安全，原生支持 `.env` 环境变量无缝加载 |
| 日志系统 | loguru | 配置极其简单，天生对异步友好，支持高效的轮转与格式化 |
| CLI 框架 | typer / click | 生态极其成熟，构建交互式命令行工具的首选 |

## Agent 核心调度机制

| 方案 | 选型 | 说明 |
|------|------|------|
| 核心架构 | **自研核心 + 动态 DAG 拓扑机制** | 放弃机械的静态固化图，完全掌控中间层状态，实现运行时自适应重规划 |

## 大模型接入层 (LLM Gateway)

| 组件 | 选型 | 理由 |
|------|------|------|
| 官方 SDK | anthropic-python-sdk | 深度对齐 Claude 官方最新特性（如 Prompt Caching 等） |
| 统一兼容层 | litellm | 极佳的多模型供应商抽象层，用于私有化环境向本地大模型平滑降级 |

## 双驱动记忆系统 (Dual-Engine Memory)

| 组件 | 选型 | 理由 |
|------|------|------|
| **向量数据库** | **LanceDB** | 嵌入式架构，无需 Docker 或独立服务，基于 Apache Arrow，标量+向量混合查询极快 |
| **拓扑图引擎** | **NetworkX** | 轻量级本地图结构计算库，用于支持本体系统（Ontology）的拓扑关系存储与纯符号逻辑推理 |
| 默认嵌入模型 | text-embedding-3-small | 极具性价比的高质量向量化选择 |
| 离线降级嵌入 | bge-large-zh-v1.5 | 中文极度优化，支持无网环境本地 CPU/GPU 离线加载部署 |

## 工具与沙箱执行层

| 工具 | 实现方式 | 说明 |
|------|----------|------|
| MCP 协议支持 | 原生通信解析 | 兼容标准 Model Context Protocol，零改动接入社区工具 |
| Claude Code | subprocess / asyncio | 异步流式封装调用 Anthropic Harness 终端 |
| 物理 Shell 执行 | asyncio.create_subprocess | 全异步子进程长连接，带有自研超时与令牌桶流控约束 |
| 异步文件读写 | pathlib + aiofiles | 避免传统 `open()` 导致异步事件循环整体挂起阻塞 |
| 代码 AST 解析 | python `ast` 模块 | 纯本地无网络依赖分析，精准提取模块/函数依赖本体 |
| 浏览器环境 | playwright | 异步 Headless 浏览器支持，用于高级自动化检索 |

## 接入层与 HITL (Human-in-the-Loop)

### 交互控制台 (CLI)
| 组件 | 选型 | 说明 |
|------|------|------|
| 交互强化 | prompt_toolkit | 提供自动补全、历史搜索和多行精致编辑能力 |
| 终端美化 | rich | 渲染高级状态表格、Trace 拓扑与高颜值执行面板 |

### 统一服务端 & 跨端
| 组件 | 选型 | 说明 |
|------|------|------|
| 后端 API | FastAPI | 基于 ASGI 的纯异步 Web 框架，完美融合 Pydantic 校验 |
| 双向通信 | WebSocket | 支撑前端时空轨迹图、Token 热力图的毫秒级流式状态推送 |
| 前端 UI | Next.js 14+ + shadcn/ui | 全栈响应式控制台，利用 Zustand 进行轻量级状态流控 |
| 桌面外壳 | Tauri 2.0 | Rust 驱动，直接复用 Web UI 前端，内存占用极小，性能极其强悍 |

### IM 动态审批总线
| 平台 | 方案 | 核心价值 |
|------|------|------|
| QQ 机器人 | Lagrange.OneBot | 基于 NTQQ 协议，稳定，用于推送高危动作审批卡片 |
| 微信机器人 | wechaty | 多协议覆盖，打通移动端 HITL 审批链路 |

## 进化引擎与快照基建

| 组件 | 选型 | 说明 |
|------|------|------|
| 物理存储层 | SQLite | 纯本地嵌入式无服务数据库，单文件易迁移 |
| 异步驱动层 | **aiosqlite** | 必须通过 aiosqlite 进行异步封装，防止快照写入和日志分析阻塞核心引擎 |
| 数据分析 | pandas + numpy | 用于离线分析失败模式（Root Cause），优化高频成功 SOP |

## 全方位测试

| 组件 | 选型 | 说明 |
|------|------|------|
| 测试框架 | pytest | Python 行业级标准测试利器 |
| 异步扩展 | pytest-asyncio | 原生支持 `async def` 测试用例的高效调度 |
| 隔离 Mock | pytest-mock | 用于对大模型 API、沙箱工具执行进行确定性模拟测试 |
| 代码覆盖率 | coverage | 确保核心 Infra 的测试覆盖率维持在生产线以上 |

## 依赖管理与项目元数据

| 工具 | 说明 |
|------|------|
| **pyproject.toml** | 遵循 PEP 621 标准，统一管理系统所有元数据、Entry Points 以及打包契约 |
| **uv** | Astral 出品的下一代超高速 Python 包管理与运行时工具，替代传统的 pip/poetry |

---

## 完美对齐的最小启动依赖 (pyproject.toml dependencies)

```toml
[project]
name = "symbio"
version = "0.1.0"
description = "AI Infra-level Multi-Agent Orchestration Framework with Self-Evolution Capabilities."
requires-python = ">=3.10"
dependencies = [
    "anthropic>=0.40.0",
    "lancedb>=0.15.0",
    "networkx>=3.0",          # 强力支撑本体引擎
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "loguru>=0.7.0",
    "typer>=0.12.0",
    "httpx>=0.27.0",
    "rich>=13.0.0",
    "aiofiles>=23.0.0",       # 确保文件层全异步
    "aiosqlite>=0.20.0",       # 确保快照与飞书/微信数据持久化全异步
    "prompt-toolkit>=3.0.0",
    "pyyaml>=6.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "pytest-mock>=3.14.0",
    "coverage>=7.0.0",
    "ruff>=0.5.0",
]
im = [
    "nonebot2>=2.3.0",        # QQ/微信机器人框架
]
cluster = [
    "ray>=2.55.0",            # 分布式 Agent 运行时 (远期)
    "kubernetes>=30.0.0",     # K8s 沙箱调度 (远期)
]
```
