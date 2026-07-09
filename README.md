<div align="center">

<img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/symbio-logo.png" width="110" height="110" alt="Symbio">

# SYMBIO

**AI Infra 级多 Agent 协同框架**

把 Agent 能力拆成*可观测、可审批、可恢复、可验证*的基础设施模块 —— 调度、记忆、审批、安全、成本、沙箱、外部工具接管和数据飞轮，都在同一个运行时里协作。

[English](README_en.md) · **中文** · [日本語](README_ja.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/symbio.svg)](https://pypi.org/project/symbio/)
[![Downloads](https://static.pepy.tech/badge/symbio)](https://pepy.tech/project/symbio)

<img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-chat.png" width="100%" alt="Symbio Web UI">

</div>

---

## 为什么是 Symbio

很多 Agent 项目只解决"让模型调用工具"这一层。Symbio 关心的是更靠后、也更难的工程问题：

- 任务跑到一半失败了，状态怎么恢复？
- Agent 要删文件、跑代码、调外部系统时，**谁来审批？通知能推到微信/飞书/钉钉/Telegram 吗？**
- 多 Agent 协作时，怎么避免互相传话把 Token 成本打爆？
- 记忆能不能不只是向量检索，而是带结构和关系、能零 Token 推理的本体图谱？
- 用户输入里藏了 Prompt Injection，**第一道防线在哪？**
- 运行轨迹能不能反哺 SOP、评测集、微调数据和 Prompt 优化？

> Symbio 的原则：**已经落地的写成能力，部分落地的写清缺口，没实现的只放路线图** —— 运行时自带一份 `GET /api/capabilities` 能力账本，README 里的每个"已实现"都有代码和测试背书。

---

## 界面预览

Web UI 共 16 个页面，默认暖色浅底主题（Claude / Hermes 风格，可一键切换深色）。

<table>
<tr>
<td width="50%"><b>成本中心 · 仪表盘</b><br/>语义缓存命中率、24h 用量、月度预算<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-dashboard.png" alt="Dashboard"></td>
<td width="50%"><b>Prompt Injection 三层防火墙</b><br/>威胁分布、在线扫描、红队自检、审计轨迹<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-security.png" alt="Security"></td>
</tr>
<tr>
<td width="50%"><b>数据飞轮四阶段闭环</b><br/>捕获 → 失效分析 → SOP 蒸馏 → 反哺优化<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-flywheel.png" alt="Data Flywheel"></td>
<td width="50%"><b>Computer Use 最小闭环</b><br/>浏览器会话 · 动作规划 · 审计回放<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-computer-use.png" alt="Computer Use"></td>
</tr>
<tr>
<td width="50%"><b>个人微信双向机器人</b><br/>扫码绑定 · 审批/对话双向收发（provider-agnostic）<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-wechat.png" alt="WeChat Bridge"></td>
<td width="50%"><b>本体记忆图谱</b><br/>概念 / 实体 / 关系可视化，支持零 Token 符号推理<br/><img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/screenshots/ui-ontology.png" alt="Ontology Graph"></td>
</tr>
</table>

---

## 快速开始

```bash
pip install symbio
symbio init
symbio serve --port 9090
```

打开 Web UI：`http://localhost:9090/ui`

从源码开发：

```bash
git clone https://github.com/854875058/Symbio.git
cd Symbio
pip install -e ".[dev,server,ml,otel]"
symbio init && symbio serve --port 9090
```

CLI 示例：

```bash
symbio chat "帮我分析这个项目的测试缺口" --model claude-sonnet-4
symbio task list
symbio memory store --title "项目约束" --content "生产环境默认需要人工审批高危操作"
symbio export --format sharegpt --output data/exports/train.jsonl
```

---

## 核心能力

**调度与工作流**
- **动态 DAG 运行时** —— 任务不是静态链而是可持久化的执行图，支持节点状态、事件、产物、重规划与断点恢复。
- **Planner / Reviewer / Verification** —— 高风险任务先规划、再审查、再执行，workflow policy 约束 Agent 不跳过计划与验证。

**安全与成本**（兑现公众号系列文章承诺，已接入对话运行时）
- **Token 成本五层优化** —— 语义缓存（相似问题命中后零 Token 返回）、上下文剪枝、成本监控 + 月度预算与超额降级建议，全部接进 `/api/chat` 与 `/ws/chat`。
- **Prompt Injection 三层防火墙** —— 输入净化 → 语义检测（8 类攻击签名）→ 意图审计，高危输入在调用 LLM 前被拦截；攻击样本库自检拦截率约 65%，对 `os.system`、`while True` 等编程话题零误伤。
- **HITL 多渠道审批 + 超时升级** —— 微信/QQ/飞书/钉钉/Telegram/Slack 审批卡片与文本命令；超时可自动拒绝 / 自动通过 / **转交管理员**。

**记忆与进化**
- **本体记忆图谱** —— 概念、实体、关系、属性组织成可查询、可视化、可零 Token 推理的图谱。
- **数据飞轮四阶段闭环** —— 轨迹捕获 → 失效分析（根因归纳）→ SOP 蒸馏 → 反哺优化，全部暴露成可点击的 `/api/flywheel/*`。

**接入与工具**
- **Agent 接入 Claude Code / Codex** —— 单个 Symbio Agent 可把任务委托给本地 Claude Code / Codex CLI 执行（`ExternalBackedAgent`），编排、审批、沙箱、审计仍由 Symbio 掌控。
- **个人微信扫码绑定机器人** —— 内置 iLink Bot 直连，Web UI 点「开始扫码登录」即出二维码，扫码后双向收发；消息自动分流（审批指令→HITL / 其它→对话管线）。无需任何外部部署，也兼容 Wechaty 等外部 bridge。
- **外部 Agent 接管** —— 登记并控制本地 Codex / Claude Code 会话，导入 transcript，纳入统一审批 / 沙箱 / 审计控制面；工作台支持多开平铺、目录浏览选工作区，并可在网页里起**真交互终端**（PTY 跑 claude/codex/shell 的完整 TUI），或在终端内 resume 接管已有会话。
- **Codex 风格沙箱** —— `read-only` / `workspace-write` / `danger-full-access` 三种访问模式 + 审批策略 + 工作区边界 + 审计。
- **真 Docker 容器沙箱** —— 命令可在隔离容器内执行：默认断网（`--network none`）、只读根文件系统 + `/tmp` tmpfs、内存/CPU 限额、执行前引擎预检、超时按容器名兜底清理、宿主机环境与密钥绝不泄漏进容器。`GET /api/sandbox/docker/status` 查引擎状态，`POST /api/sandbox/docker/execute` 容器内执行。
- **Computer Use 最小闭环** —— 浏览器会话控制、动作集、启发式规划、审计回放；Playwright 不可用时降级 dry-run。
- **MCP 工具网关 / A2A 协议 / Skills 市场** —— 标准协议接入与本地工具生态；A2A 支持入站编排器执行、出站会话闭环、SSE 流式订阅、webhook 推送与 Bearer 鉴权。

---

## 能力账本

运行时自带能力账本，区分"已实现 / 部分实现 / 规划中"，每项都有证据文件与测试。运行 `curl http://localhost:9090/api/capabilities` 查看实时状态。

| 能力 | 状态 | 说明 |
|------|------|------|
| 动态 DAG 运行时 | ✅ 已实现 | 图状态持久化、执行事件、重规划 |
| Planner/Reviewer 策略 | ✅ 已实现 | 先规划、风险审查、验证闭环 |
| HITL + IM 多渠道审批 | ✅ 已实现 | 6+ 渠道 + 超时升级策略（拒绝/通过/转交管理员） |
| 本体记忆图谱 | ✅ 已实现 | ontology memory + API + Web UI |
| 模型池与模型路由 | ✅ 已实现 | 模型配置、路由策略、对话模型选择 |
| 外部 Agent 接管 | ✅ 已实现 | Codex / Claude Code 会话登记、运行、导入 transcript；工作台多开平铺 + 网页真交互终端（PTY 跑 claude/codex/shell 的 TUI）+ 终端内接管 resume |
| Agent 接入 Claude/Codex | ✅ 已实现 | 单 Agent 委托本地 Claude Code / Codex CLI 执行（ExternalBackedAgent） |
| 个人微信扫码绑定机器人 | ✅ 已实现 | 内置 iLink Bot 直连，扫码登录 + 双向收发 + 审批/对话分流 |
| **Token 成本五层优化** | ✅ 已实现 | 语义缓存 + 上下文剪枝 + 成本监控接入对话链路 + 成本仪表盘 |
| **Prompt Injection 三层防火墙** | ✅ 已实现 | 接入对话入口，攻击样本自检拦截率 65%，编程话题零误伤 |
| Skills 市场 | 🔧 部分实现 | 本地市场与安装记录已具备，远程生态待完善 |
| MCP 工具网关 | 🔧 部分实现 | stdio JSON-RPC 桥接已具备，协议面待补齐 |
| 沙箱与 K8s 路径 | 🔧 部分实现 | 本地工作区沙箱 + 真 Docker 容器隔离（断网/只读根/资源限额/引擎预检/孤儿清理）已具备；K8s pod 执行仍为桩，沙箱审计跨重启持久化待补 |
| OpenTelemetry 可观测 | 🔧 部分实现 | trace/token heatmap + OTel Compose 部署包 |
| 数据飞轮（四阶段闭环） | 🔧 部分实现 | 捕获/失效分析/SOP 蒸馏/反哺已打通，真实训练后端待补 |
| Ray Actor 运行时 | 🔧 部分实现 | 本地 fallback 已有，集群调度待产品化 |
| A2A 协议 | 🔧 部分实现 | 动态 AgentCard、入站走编排器管线、出站 poll 闭环、跨实例真 HTTP 往返、SSE 流式、webhook 推送、Bearer 鉴权；完整 OAuth/非文本 artifact 待补 |
| **Computer Use 最小闭环** | 🔧 部分实现 | 会话/动作/截图/规划/审计/回放，待接 VLM 视觉规划 |
| 多模态视觉记忆 | 🔧 部分实现 | Claude 视觉接入摄取管线（图片描述转可检索记忆）+ PDF/代码结构抽取；聊天图片自动摄取、描述缓存跨重启、OCR 待补 |
| 隐私计算 / 联邦学习 | 📋 规划中 | 当前仅在路线图 |

---

## 架构

<img src="https://raw.githubusercontent.com/854875058/Symbio/master/assets/symbio-architecture.png" width="100%" alt="Symbio 四层核心架构图">

| 层级 | 作用 | 主要模块 |
|------|------|----------|
| 接入层 | CLI、Web UI、外部会话、IM 审批、A2A 统一接入 | CLI、FastAPI、Web UI、IM Bot、External Agent、A2A |
| 调度中枢 | 任务理解、DAG 编排、模型路由、风险判断 | Orchestrator、DAG Runtime、Router、Planner/Reviewer |
| 执行层 | 多 Agent 执行、SubAgent 协作、工具调用 | BaseAgent、SubAgent、Debate、Execution Store |
| 基础层 | 工具、记忆、安全、成本、观测、进化 | Sandbox、Memory、Ontology、Guard、Cost、Telemetry、Evolution |

---

## API 概览

| 接口 | 用途 |
|------|------|
| `GET /api/capabilities` | 运行时能力账本 |
| `POST /api/chat` · `WS /ws/chat` | 对话（含语义缓存 / 剪枝 / 防火墙） |
| `GET /api/costs/dashboard` | 成本中心（用量 + 缓存命中率 + 预算） |
| `POST /api/security/{scan,selftest}` | 防火墙在线扫描 / 红队自检 |
| `GET/POST /api/flywheel/{overview,failures,sops,feedback}` | 数据飞轮四阶段 |
| `GET/POST /api/hitl/timeout/policy` | 审批超时升级策略 |
| `POST /api/computer-use/sessions` | Computer Use 浏览器会话 |
| `GET /api/tasks/{id}/dag` · `GET /api/executions/{id}` | 任务 DAG / 执行详情 |
| `GET /api/ontology` | 本体图谱 |
| `POST /api/sandbox/execute` · `GET /api/sandbox/audit` | 本地沙箱执行 / 审计 |
| `GET /api/sandbox/docker/status` · `POST /api/sandbox/docker/execute` | Docker 容器隔离执行 |
| `WS /ws/terminal` | 网页真交互终端（PTY 跑 claude/codex/shell，默认仅本机可连） |
| `GET /api/external-agents/transcripts` · `POST .../sessions` · `.../{id}/run` | 外部 Agent 会话登记 / 运行 / 导入 |
| `GET /api/fs/dirs` | 服务端目录浏览（工作台选工作区文件夹） |
| `GET /.well-known/agent.json` · `POST /api/a2a/tasks` | A2A AgentCard / 入站任务（可选 Bearer 鉴权） |
| `GET /api/a2a/tasks/{id}/stream` | A2A 任务 SSE 流式订阅 |
| `POST /api/a2a/sessions` · `.../{id}/send` · `.../{id}/poll` | A2A 出站会话（发送 + 拉回远端结果） |
| `POST /api/export/conversations` | 对话数据集导出 |

---

## 配置

初始化后生成 `symbio.yaml`。推荐通过 Web UI 或环境变量管理真实密钥，不要把生产 Key 提交进仓库。

```yaml
model:
  anthropic_api_key: ""
  anthropic_base_url: "https://api.anthropic.com"
  openai_api_key: ""          # 配置后启用语义缓存（embedding）
  model_low: "claude-3-5-haiku-latest"
  model_medium: "claude-sonnet-4-20250514"
  model_high: "claude-opus-4-20250514"

server: { host: "0.0.0.0", port: 9090 }

hitl:
  enabled: true
  approval_timeout: 300
  timeout_action: "reject"     # reject / approve / escalate

cost:
  semantic_cache_enabled: true
  context_max_tokens: 8000

security:
  enabled: true
  mode: "default"              # default / strict / permissive
```

---

## 技术栈

| 方向 | 技术 |
|------|------|
| 后端 | Python 3.10+、FastAPI、Typer、Pydantic v2、aiosqlite |
| Agent 调度 | asyncio、DAG Runtime、Planner/Reviewer、Execution Store |
| 记忆 | LanceDB、NetworkX、本体图谱、SQLite 持久化 |
| 安全 / 成本 | InjectionGuard、SemanticCache、ContextPruner、CostMonitor |
| 工具 | MCP、Sandbox、Playwright、External Agent adapters |
| 前端 | 原生 Web UI、HTML/CSS/JS、WebSocket、Chart.js |
| 观测 | OpenTelemetry、Jaeger、Prometheus、Grafana |

---

## 文档

| 文档 | 说明 |
|------|------|
| [功能检查表](docs/feature-checklist.md) | 能力实现状态和后续 TODO |
| [架构设计](docs/architecture.md) | 四层架构、安全、HITL、数据飞轮 |
| [模块白皮书](docs/module-design-whitepaper.md) | 模块边界和工程设计 |
| [外部 Agent 接管](docs/external-agent-control.md) | Codex / Claude Code 会话控制 |
| [路线图](docs/roadmap.md) | 分阶段开发规划 |

---

## 开发与验证

```bash
pip install -e ".[dev]"
pytest                 # 395 passed
```

---

## 当前状态

Symbio 仍处于 Alpha 阶段。核心调度、HITL（多渠道审批 + 超时升级）、记忆、外部 Agent 接管、沙箱（本地 + Docker 容器隔离）、Token 成本优化、Prompt Injection 防火墙、数据飞轮闭环和 Web UI 已形成可运行能力；Computer Use 已具备最小闭环；A2A 已支持编排器执行、跨实例往返、SSE 流式、推送与鉴权；企业级部署、安全隔离、远程 Skill 生态、完整 MCP 协议面、真实训练后端和隐私计算还在持续实现。

---

## License

MIT License. See [LICENSE](LICENSE).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=854875058/Symbio&type=Date)](https://star-history.com/#854875058/Symbio&Date)


<div align="center">

**Symbio: AI Infra for controllable, observable, evolvable agents.**

</div>
