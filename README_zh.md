<div align="center">

<img src="assets/symbio-logo.png" width="120" height="120" alt="Symbio">

# SYMBIO

### AI Infra 级多 Agent 协同框架

Symbio 不是一个单纯的 LLM 外壳，而是一套面向真实工程任务的 Agent 基础设施：调度、记忆、审批、沙箱、观测、外部工具接管和数据飞轮都在同一个运行时里协作。

[English](README_en.md) | **中文** | [日本語](README_ja.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/symbio.svg)](https://pypi.org/project/symbio/)

</div>

---

## 项目定位

现在很多 Agent 项目只解决"让模型调用工具"这一层问题。Symbio 关注的是更靠后的工程问题：

- 任务跑到一半失败了，状态怎么恢复？
- Agent 要删除文件、执行代码、调用外部系统时，谁来审批？**审批通知能推送到微信、QQ、飞书、钉钉、Telegram 吗？**
- 多个 Agent 协作时，怎么避免互相传话造成成本爆炸？
- 记忆能不能不只是向量检索，而是带结构、关系和可视化的本体图谱？
- Codex、Claude Code 这类现有工具已经在跑，Symbio 能不能接管它们的会话、审计它们的行为？
- 运行轨迹能不能反哺 SOP、评测集、微调数据和 Prompt 优化？
- **外部 Agent 系统能不能通过标准协议和 Symbio 互联互通？**

Symbio 的答案是：把 Agent 能力拆成可观测、可审批、可恢复、可验证的基础设施模块。

---

## 架构图

<img src="assets/symbio-architecture.png" width="100%" alt="Symbio 四层核心架构图">

Symbio 当前采用四层架构：

| 层级 | 作用 | 主要模块 |
|------|------|----------|
| 接入层 | CLI、Web UI、外部会话和 IM 审批统一接入 | CLI、FastAPI、Web UI、IM Bot、外部 Agent 控制、A2A 协议 |
| 调度中枢 | 任务理解、DAG 编排、模型路由、风险判断 | Orchestrator、DAG Runtime、Router、Planner/Reviewer |
| 执行层 | 多 Agent 执行、SubAgent 协作、工具调用 | BaseAgent、SubAgent、Debate、Execution Store |
| 基础层 | 工具、记忆、配置、安全、观测和进化 | Sandbox、Memory、Ontology、HITL、Telemetry、Evolution |

---

## 核心特性

### 1. 动态 DAG 调度

Symbio 不把任务固定成一条静态链，而是把任务拆成可持久化的执行图。执行过程中可以记录节点状态、事件、产物和重规划决策，为失败恢复、任务审计和 UI 可视化提供基础。

相关代码：`src/symbio/core/dag_runtime.py` · `dag_orchestrator.py` · `replanner.py` · `tests/test_dag_runtime.py`

### 2. Planner / Reviewer / Verification 工作流

高风险任务先规划，再审查，再执行。Symbio 内置 workflow policy，约束 Agent 不跳过计划、审批和验证步骤。

相关代码：`src/symbio/core/planner_reviewer.py` · `workflow_policy.py`

### 3. HITL 多渠道审批

HITL 不是简单弹窗，而是可持久化的审批网关。Symbio 支持以下所有渠道发送审批卡片并接收回调：

| 渠道 | 平台标识 | 说明 |
|------|----------|------|
| 企业微信群机器人 | `wecom` | 模板卡片，带同意/拒绝按钮 |
| 飞书群机器人 | `feishu` | 富卡片 + HMAC-SHA256 签名 |
| QQ（OneBot/Lagrange）| `qq` | 群消息 / 私聊 |
| 钉钉自定义机器人 | `dingtalk` | ActionCard，带跳转按钮 |
| Telegram Bot | `telegram` | Bot API，支持群组和私聊 |
| WxPusher | `wxpusher` | **个人微信**推送，扫码绑定 |
| PushPlus | `pushplus` | **个人微信**推送，token 一键配置 |
| Server酱 | `serverchan` | **个人微信/企业微信**推送 |
| Slack | `slack` | Block Kit 富卡片 |
| Wechaty bridge | `wechaty` | 个人微信 bridge（需自建） |

文本命令（任意渠道均支持）：

```text
同意 REQ-CODE
拒绝 REQ-CODE 风险太高
approve REQ-CODE
reject REQ-CODE too risky
```

审批通过后 Orchestrator 自动从挂起恢复；超时可配置自动拒绝或自动通过策略。

相关代码：`src/symbio/core/hitl_gateway.py` · `hitl_notifier.py` · `src/symbio/interfaces/api.py`

### 4. A2A 协议（Agent-to-Agent）

Symbio 实现了 Google A2A 规范的最小可用子集，让不同 Agent 系统之间可以互联：

- **AgentCard** 发布在 `GET /.well-known/agent.json`，对外声明能力
- **入站任务**：接收外部 Agent 通过 `POST /api/a2a/tasks` 投递的任务，自动接入 LLM 处理
- **出站会话**：主动连接远程 A2A Agent，支持多轮对话，自动发现 AgentCard
- **探测工具**：`GET /api/a2a/probe?url=...` 读取任意远程 Agent 的名片

相关代码：`src/symbio/interfaces/a2a.py` · `src/symbio/interfaces/api.py`

### 5. 本体记忆与图谱展示

Symbio 的记忆不只是一组向量片段。项目内置 ontology memory，把概念、实体、关系和属性组织成可查询、可展示的图谱。Web UI 可以展示本体节点和关系。

相关代码：`src/symbio/memory/ontology.py` · `src/symbio/memory/auto_populator.py`

### 6. 模型池与模型路由

支持在 CLI 和 Web UI 中配置模型池，并通过任务复杂度和用户配置决定路由。

相关代码：`src/symbio/core/router.py` · `src/symbio/config/settings.py`

### 7. Skills 市场与本地安装

独立的 Skill schema、注册表、市场索引和安装记录。支持本地浏览、搜索、导入和安装记录。

相关代码：`src/symbio/skills/schema.py` · `registry.py` · `marketplace.py`

### 8. MCP 工具网关

MCP stdio JSON-RPC 工具桥接：注册 MCP server、探测可用工具列表、在 Agent 执行时挂载。Web UI 提供 MCP 服务器管理页面。

相关代码：`src/symbio/tools/mcp.py` · `src/symbio/interfaces/api.py`

### 9. 外部 Agent 会话接管

可以登记并控制本地 Codex、Claude Code 等外部 Agent 会话，也支持导入外部 transcript。

相关代码：`src/symbio/tools/external_agents.py` · `external_transcripts.py`

### 10. Codex 风格沙箱

沙箱层支持访问模式、审批策略、权限等级和审计接口。支持 `read-only`/`workspace-write`/`danger-full-access` 三种访问模式。

相关代码：`src/symbio/tools/sandbox.py` · `k8s_sandbox.py`

### 11. 可观测性与 OTel 部署包

Symbio 有 trace、metric、token heatmap、执行事件和 artifact 的接口基础，同时提供一键拉起的 Docker Compose 可观测套件：

```bash
docker compose -f docker-compose.observability.yml up -d
# Jaeger UI:   http://localhost:16686
# Grafana:     http://localhost:3001  (admin / symbio123)
# Prometheus:  http://localhost:9091
```

相关代码：`src/symbio/core/tracer.py` · `docker-compose.observability.yml` · `config/otel/` · `config/prometheus/`

### 12. 数据飞轮与进化引擎

SOP 蒸馏、异步轨迹捕获、DatasetExporter，支持 ShareGPT/Alpaca/OpenAI/raw JSONL 导出。

相关代码：`src/symbio/evolution/sop_distiller.py` · `dataset_exporter.py` · `eval_pipeline.py`

---

## 能力账本

| 能力 | 状态 | 说明 |
|------|------|------|
| 动态 DAG 运行时 | ✅ 已实现 | 图状态持久化、执行事件、重规划 |
| Planner/Reviewer 策略 | ✅ 已实现 | 先规划、风险审查、验证闭环 |
| HITL + IM 多渠道审批 | ✅ 已实现 | 微信/QQ/飞书/钉钉/Telegram/Slack 全支持 |
| 本体记忆图谱 | ✅ 已实现 | ontology memory + API + Web UI |
| 模型池与模型路由 | ✅ 已实现 | 模型配置、路由策略、对话模型选择 |
| 外部 Agent 接管 | ✅ 已实现 | Codex / Claude Code 会话登记、运行 |
| A2A 协议 | 🔧 部分实现 | AgentCard、入站/出站任务、多轮会话 |
| Skills 市场 | 🔧 部分实现 | 本地市场与安装记录已具备 |
| MCP 工具网关 | 🔧 部分实现 | stdio JSON-RPC 桥接 + UI 管理页面 |
| 沙箱与 K8s 路径 | 🔧 部分实现 | 本地沙箱已具备 |
| OpenTelemetry 可观测 | 🔧 部分实现 | trace 基础 + OTel Compose 部署包 |
| 数据飞轮 | 🔧 部分实现 | SOP、导出、eval 基础已具备 |
| Ray Actor 运行时 | 🔧 部分实现 | 本地 fallback 已有，集群调度待产品化 |
| Computer Use 完整闭环 | 📋 规划中 | 截图理解、坐标规划、GUI 操作 |

运行时账本接口：

```bash
curl http://localhost:9090/api/capabilities
```

---

## 快速开始

### 从 PyPI 安装

```bash
pip install symbio
symbio init
symbio serve --port 9090
```

打开 Web UI：`http://localhost:9090/ui`

### 从源码开发

```bash
git clone https://github.com/854875058/Symbio.git
cd Symbio
pip install -e ".[dev,server,ml,otel]"
symbio init
symbio serve --port 9090
```

### CLI 示例

```bash
symbio chat "帮我分析这个项目的测试缺口" --model claude-sonnet-4
symbio task list
symbio memory store --title "项目约束" --content "生产环境默认需要人工审批高危操作"
symbio export --format sharegpt --output data/exports/train.jsonl
```

---

## Web UI

当前 Web UI 覆盖 14 个页面，采用左侧分组侧边栏布局：

| 分组 | 页面 | 说明 |
|------|------|------|
| 核心 | 对话 | 多会话、流式回答、模型选择、历史持久化 |
| 智能体 | 任务 | 任务列表、DAG 可视化、执行事件 |
| 智能体 | 审批 | HITL 审批列表、渠道配置、超时策略 |
| 智能体 | Agents | Codex/Claude Code 会话接管、transcript 导入 |
| 智能体 | A2A | AgentCard 展示、出站会话、入站任务 |
| 工具 | Skills | 本地 Skill 管理、市场浏览、文件编辑 |
| 工具 | Sandbox | 命令执行、权限策略、审批策略、审计 |
| 工具 | MCP | MCP server 管理、工具列表探测 |
| 知识 | 记忆 | 语义搜索、写入、统计 |
| 知识 | 本体图谱 | 实体关系可视化 |
| 分析 | 仪表盘 | Token 趋势图（Chart.js）、可观测摘要 |
| 分析 | 能力账本 | 承诺能力与实现状态对照 |
| 分析 | 数据飞轮 | Dataset 导出、Eval suite 解析 |
| 配置 | 模型 | 模型池配置、API Key 管理、连接测试 |

---

## 配置

初始化后生成 `symbio.yaml`。推荐通过 Web UI 或环境变量管理真实密钥。

最小配置：

```yaml
model:
  anthropic_api_key: ""
  anthropic_base_url: "https://api.anthropic.com"
  openai_api_key: ""
  model_low: "claude-3-5-haiku-latest"
  model_medium: "claude-sonnet-4-20250514"
  model_high: "claude-opus-4-20250514"

server:
  host: "0.0.0.0"
  port: 9090

hitl:
  enabled: true
  high_risk_auto_suspend: true
  approval_timeout: 300
  # 通知渠道示例（也可在 Web UI 中配置）
  notify_targets:
    - platform: feishu
      endpoint: "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_TOKEN"
      secret: "YOUR_SIGN_SECRET"
    - platform: dingtalk
      endpoint: "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
    - platform: wxpusher
      access_token: "YOUR_APP_TOKEN"
      chat_id: "UID_YOUR_USER_ID"
    - platform: telegram
      access_token: "YOUR_BOT_TOKEN"
      chat_id: "-100YOUR_CHAT_ID"
```

---

## 可观测性部署

```bash
# 启动 Jaeger + Prometheus + Grafana + OTel Collector
docker compose -f docker-compose.observability.yml up -d

# 在 symbio.yaml 中配置 OTLP 端点
otel:
  enabled: true
  endpoint: "http://localhost:4317"
  service_name: "symbio"
```

---

## 技术栈

| 方向 | 技术 |
|------|------|
| 后端 | Python 3.10+、FastAPI、Typer、Pydantic v2、aiosqlite |
| Agent 调度 | asyncio、DAG Runtime、Planner/Reviewer、Execution Store |
| 记忆 | LanceDB、NetworkX、本体图谱、SQLite 持久化 |
| 工具 | MCP、Sandbox、K8s sandbox path、External Agent adapters |
| 前端 | 原生 Web UI、HTML/CSS/JavaScript、WebSocket、Chart.js |
| 观测 | OpenTelemetry、Jaeger、Prometheus、Grafana |
| 扩展 | Ray、Kubernetes、Playwright、LiteLLM、OpenAI、Anthropic |

---

## 文档

| 文档 | 说明 |
|------|------|
| [功能检查表](docs/feature-checklist.md) | 能力实现状态和后续 TODO |
| [架构设计](docs/architecture.md) | 四层架构、安全、HITL、数据飞轮 |
| [模块白皮书](docs/module-design-whitepaper.md) | 模块边界和工程设计 |
| [外部 Agent 接管](docs/external-agent-control.md) | Codex / Claude Code 会话控制 |
| [Agent 工作流策略](docs/agent-workflow-policy.md) | 计划、审查、验证工作流 |
| [路线图](docs/roadmap.md) | 分阶段开发规划 |

---

## 开发与验证

```bash
pip install -e ".[dev]"
pytest

# 专项测试
pytest tests/test_capabilities.py
pytest tests/test_hitl_notifier.py
pytest tests/test_external_agents.py
pytest tests/test_dag_runtime.py
```

---

## 当前状态

Symbio 仍处于 Alpha 阶段。核心调度、HITL（含微信/QQ/飞书/钉钉/Telegram 全渠道）、A2A 协议、记忆、外部 Agent 接管、沙箱、MCP 工具网关和 Web UI 已经形成可运行骨架；Computer Use 完整闭环、Ray 集群调度和完整 MCP 协议面还在继续实现。

---

## License

MIT License. See [LICENSE](LICENSE).

---

<div align="center">

**Symbio: AI Infra for controllable, observable, evolvable agents.**

</div>
