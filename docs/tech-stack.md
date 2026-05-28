# Symbio 技术栈选型

## 核心运行时

| 组件 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.10+ | AI 生态成熟，异步支持好 |
| 异步框架 | asyncio + uvloop | 高性能异步 I/O |
| 配置管理 | pydantic-settings | 类型安全，支持 .env |
| 日志 | loguru | 简洁易用，功能丰富 |
| CLI | typer / click | 成熟的 CLI 框架 |

## Agent 框架

| 方案 | 选型 | 说明 |
|------|------|------|
| 方案 A | 自研 | 完全可控，按需定制 |
| 方案 B | LangGraph | 成熟的图调度，但较重 |
| 方案 C | AutoGen | 微软出品，多 Agent 支持好 |

**推荐：** 自研核心 + 参考 LangGraph 的图调度设计

## LLM 接入

| 组件 | 选型 | 理由 |
|------|------|------|
| SDK | anthropic-python-sdk | 官方 SDK，支持最新特性 |
| 模型 | Claude 系列 | Haiku / Sonnet / Opus |
| 备选 | litellm | 多模型兼容层 |

## 记忆系统

| 组件 | 选型 | 理由 |
|------|------|------|
| 向量数据库 | LanceDB | 嵌入式，无需额外服务，性能好 |
| 嵌入模型 | text-embedding-3-small | OpenAI 嵌入，性价比高 |
| 备选嵌入 | bge-large-zh | 中文优化，可本地部署 |

**LanceDB 优势：**
- 嵌入式，无需 Docker / 独立服务
- 基于 Apache Arrow，查询快
- 支持向量 + 标量混合查询
- Python 原生支持

## 工具层

| 工具 | 实现方式 |
|------|----------|
| Claude Code | subprocess 调用 CLI |
| Shell | subprocess / asyncio.create_subprocess |
| 文件 | pathlib + aiofiles |
| Git | gitpython / subprocess |
| HTTP | httpx (异步) |
| 浏览器 | playwright / selenium |

## 接入层

### CLI
| 组件 | 选型 |
|------|------|
| 框架 | typer |
| 交互 | prompt_toolkit |
| 表格 | rich |

### Web UI
| 组件 | 选型 |
|------|------|
| 前端框架 | Next.js 14+ |
| UI 组件 | shadcn/ui |
| 状态管理 | zustand |
| 通信 | WebSocket |

### Desktop App
| 组件 | 选型 |
|------|------|
| 框架 | Tauri 2.0 |
| 前端 | 复用 Web UI |
| 理由 | 轻量，原生性能 |

### IM 接入
| 平台 | 方案 | 说明 |
|------|------|------|
| QQ | Lagrange.OneBot | 基于 NTQQ，稳定 |
| 微信 | wechaty | 多协议支持 |
| 飞书 | 官方 SDK | 企业场景 |

## 进化引擎

| 组件 | 选型 | 说明 |
|------|------|------|
| 反馈存储 | SQLite | 轻量，结构化 |
| 分析 | pandas + numpy | 数据分析 |
| 策略优化 | 自研规则引擎 | 可控性强 |

## 测试

| 组件 | 选型 |
|------|------|
| 测试框架 | pytest |
| 异步测试 | pytest-asyncio |
| Mock | pytest-mock + unittest.mock |
| 覆盖率 | coverage |

## 部署

| 方案 | 说明 |
|------|------|
| Docker | 容器化部署 |
| systemd | 服务化管理 |
| pm2 | 进程守护（Node 侧） |

## 依赖管理

| 工具 | 说明 |
|------|------|
| pyproject.toml | 项目元数据 |
| uv | 快速包管理器 |
| requirements.txt | 兼容传统方式 |

---

## 最小启动依赖

```toml
[project]
dependencies = [
    "anthropic>=0.40.0",
    "lancedb>=0.15.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "loguru>=0.7.0",
    "typer>=0.12.0",
    "httpx>=0.27.0",
    "rich>=13.0.0",
]
```
