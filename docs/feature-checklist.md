# Symbio 功能实现账本

> 对照 README、README_zh、模块设计白皮书和公众号文章承诺维护。
> `[x]` 表示已有可运行实现并有测试或人工验证证据；`[~]` 表示已有框架但还没形成完整产品闭环；`[ ]` 表示仍待实现。

## 当前汇总

> 汇总以运行时账本 `GET /api/capabilities`（`src/symbio/capabilities.py`）为准，下表与其保持一致。

| 状态 | 数量 | 说明 |
| --- | ---: | --- |
| `[x]` 已实现 | 18 | DAG-first、规划/审查、HITL IM 审批、本体图谱、模型路由、外部 Agent 接管、成本优化、注入防火墙、Skills 市场、MCP 网关、OTel 可观测、数据飞轮、A2A 协议、多模态视觉、微信机器人、外部后端 Agent、Ray Actor 分布式、Computer Use 视觉闭环（VLM 截图→坐标动作） |
| `[~]` 部分实现 | 2 | 沙箱/K8s（K8s pod 仍为桩）、联邦学习+差分隐私（FedAvg+DP 单机多客户端已验证，跨机安全传输待补） |
| `[ ]` 未实现 | 0 | —（账本所有承诺已全部落地或部分落地） |

运行时账本接口：`GET /api/capabilities`。

## P0 核心承诺

### 动态 DAG / 编排

- [x] DAGEngine 支持节点、依赖、并发执行、动态拓扑变更、快照恢复 - `src/symbio/core/dag_engine.py`
- [x] Orchestrator 默认路径已委托给 DAG-first runtime - `src/symbio/core/orchestrator.py`, `src/symbio/core/dag_orchestrator.py`
- [x] Decomposition 编译为持久化 execution graph 节点和边 - `src/symbio/core/execution_planner.py`
- [x] Observation-driven replan 已接入运行时，支持 retry、local-patch、global-replan 事件记录 - `src/symbio/core/replanner.py`, `src/symbio/core/dag_runtime.py`
- [x] 执行详情、事件、artifact、图版本接口已暴露 - `/api/executions/{id}`, `/api/tasks/{id}/dag`
- [~] 断点恢复已有 graph version 和 SQLite 状态恢复能力，但不能宣称固定 `10ms` SLA - `src/symbio/core/execution_state_store.py`

### 工作流纪律

- [x] 规划优先、审查门禁、完成前验证策略已接入 Orchestrator - `src/symbio/core/planner_reviewer.py`, `src/symbio/core/workflow_policy.py`
- [x] `submit_task`、Checklist、Testing Agent 形成防过早完成闭环底座 - `src/symbio/tools/submit_task.py`, `src/symbio/agents/checklist.py`, `src/symbio/agents/testing_agent.py`
- [x] Web UI 已展示 workflow policy、verification evidence、approval context、planner/reviewer controls - `web/app.js`, `web/style.css`

### HITL / 人类审批

- [x] 异步审批网关、风险等级、Token 签名、回调接口 - `src/symbio/core/hitl_gateway.py`, `src/symbio/interfaces/api.py`
- [x] Orchestrator 高风险任务可挂起并在审批后恢复 - `src/symbio/core/orchestrator.py`
- [x] Web/IM 审批卡片、文本命令回调、短码审批、outbound notification 审计 payload 已实现 - `src/symbio/core/hitl_notifier.py`, `tests/test_hitl_notifier.py`
- [x] QQ OneBot/Lagrange、企业微信 webhook、飞书签名机器人发送已接入；Wechaty bridge 保留为兼容目标
- [~] 审批超时策略仍未形成产品闭环：自动拒绝、降级执行、转交管理员待补

### 模型配置 / 路由

- [x] Web UI 可配置模型池和 LLM 配置 - `web/app.js`, `/api/models`, `/api/config`
- [x] 模型连接测试会优先使用当前 `symbio.yaml` 配置凭证，避免读到旧库记录 - `src/symbio/interfaces/api.py`
- [x] 模型列表和创建接口不再向前端泄露 `api_key`，仅返回 `has_api_key` - `src/symbio/interfaces/api.py`, `tests/test_integration.py`
- [~] 路由决策解释还没有系统写入 execution artifact

## P1 重要增强

### 记忆 / 本体图谱

- [x] 记忆压缩、版本化、冲突处理、噪声过滤底座 - `src/symbio/memory/*`
- [x] 本体化记忆和零 Token 图推理底座 - `src/symbio/memory/ontology.py`
- [x] 独立本体图谱接口和 UI 页面已实现 - `/api/ontology`, `web/index.html`, `web/app.js`, `web/style.css`
- [x] 空本体首次展示可从已有 memories 自动 bootstrap - `src/symbio/interfaces/api.py`
- [~] 多模态记忆模块存在，但生产级解析质量和索引链路仍需集成验证

### Skills

- [x] Skills 列表、搜索、导入、启停、删除、自动检测、目录导入接口已实现 - `/api/skills/*`
- [x] Skill 详情页支持文件树、README、manifest、prompt/test 文件查看和编辑 - `web/app.js`
- [x] 默认数据确定性和 trigger keywords 补齐已验证 - `tests/test_integration.py`
- [x] Skills Marketplace API/UI 支持浏览、搜索、已安装状态和本地安装记录 - `/api/skills/marketplace`, `web/app.js`, `tests/test_marketplace_api.py`
- [~] 远程/私有市场、依赖安装流水线、运行沙箱和版本发布流程仍需产品化

### MCP / 外部工具

- [x] MCP stdio JSON-RPC 客户端和工具桥 - `src/symbio/tools/mcp.py`
- [~] MCP 配置发现已支持 dict/JSON/YAML 和 `mcpServers` schema - `tests/test_mcp_config.py`
- [~] 长连接池、resources/prompts 协议、完整认证、市场分发和 UI 挂载仍未完成
- [ ] A2A 协议适配未实现

### Browser / Computer Use

- [x] BrowserTool fetch 能力 - `src/symbio/tools/registry.py`
- [x] BrowserTool screenshot 在 Playwright 可用时截图，缺依赖时返回明确错误 - `src/symbio/tools/registry.py`
- [~] Computer Use 最小闭环已实现：浏览器会话控制、动作集（navigate/screenshot/click/type/scroll/extract_text）、启发式动作规划、审计轨迹与回放；Playwright 不可用时降级为 dry-run record-only 模式 - `src/symbio/tools/computer_use.py`, `/api/computer-use/*`, `web/app.js`, `tests/test_computer_use.py`
- [ ] 待补：截图视觉理解（VLM 定位坐标）、LLM 规划器接管、多标签/多会话生命周期硬化

### 安全 / 沙箱 / 资源

- [x] Guardrail、RateLimiter、ResourceManager 底座存在 - `src/symbio/core/guardrail.py`, `src/symbio/core/rate_limiter.py`, `src/symbio/core/resource_manager.py`
- [x] 工具权限分级和高危操作审批元数据 - `src/symbio/tools/registry.py`
- [x] 本地 SandboxExecutor - `src/symbio/tools/sandbox.py`
- [x] Codex-like sandbox policy 已接入本地代码执行：`read-only`/`workspace-write`/`danger-full-access`、`on-request`/`on-failure`/`never`/`always` approval policy、工作区边界、网络命令拦截、审计记录和 Web UI - `src/symbio/tools/sandbox.py`, `/api/sandbox/execute`, `web/app.js`, `tests/test_sandbox_runtime.py`
- [x] K8s/Docker 安全资源 YAML 生成器 - `src/symbio/tools/k8s_sandbox.py`
- [~] 生产级“动态拉起 Pod 执行并销毁”的 K8s executor 未完成
- [~] 安全攻击样本库存在，但规模和 CI 接入还达不到 README 宣传口径 - `src/symbio/security/attack_samples.py`

### 可观测

- [x] Tracer、Span、Token heatmap、memory snapshot、metric record 底座 - `src/symbio/core/tracer.py`
- [x] 前端 DAG/Trace 交互式可视化已支持 Graph/Timeline/Artifacts 切换、节点定位、筛选、payload 展开和 artifact 过滤 - `web/app.js`
- [~] OpenTelemetry 是可选依赖 fallback，默认 OTLP/Jaeger/Grafana/Prometheus 部署和看板仍需补齐

## P2 平台化 / 自我进化

### 数据飞轮

- [x] SOP 蒸馏、异步轨迹捕获、DatasetExporter - `src/symbio/evolution/sop_distiller.py`, `src/symbio/evolution/dataset_exporter.py`
- [x] CLI export 支持 ShareGPT/Alpaca/OpenAI/raw JSONL - `src/symbio/cli.py`
- [x] Conversation export API 支持 ShareGPT/Alpaca/OpenAI/raw 预览和 JSONL 写出 - `/api/export/conversations`
- [x] Evaluation suite discovery API 和 Web UI 可视化已接入 - `/api/evaluation/suites`, `web/app.js`, `data/eval_suites/smoke.json`
- [~] FineTuner 目前是训练作业管理 + 本地/Ray 桩，不是真实训练循环 - `src/symbio/evolution/fine_tuner.py`
- [~] Eval pipeline 已可见，但执行报告、回归对比和失败根因分析闭环仍需补

### 分布式 / 边缘 / 企业能力

- [~] Ray-Native SubAgent 目前本地 asyncio 为主，Ray Actor 投递仍待产品化 - `src/symbio/agents/subagent.py`
- [~] Edge/mobile/IoT 管理模块存在，但仍是平台适配底座，不是完整产品入口 - `src/symbio/interfaces/edge/*`
- [~] 联邦学习 + 差分隐私：联邦 LoRA（客户端本地训 adapter、数据不出本地）+ FedAvg 加权聚合 + 差分隐私（L2 裁剪+高斯噪声）已落地，单机多客户端端到端验证；跨机安全传输、抗梯度泄露、拜占庭鲁棒聚合待补 - `src/symbio/evolution/federated.py`

## 本批实现记录

- [x] 2026-06-02: Skills API 默认数据确定性，自动检测/导入补齐 trigger keywords
- [x] 2026-06-03: MCP stdio 工具桥
- [x] 2026-06-03: BrowserTool screenshot
- [x] 2026-06-03: `/api/tasks/{id}/dag`
- [x] 2026-06-03: Workflow policy checkpoint persistence and `submit_task` evidence enforcement
- [x] 2026-06-03: Planner/reviewer gate wired into Orchestrator before HITL/DAG execution
- [x] 2026-06-04: HITL QQ OneBot/Lagrange、企业微信 webhook、飞书签名机器人 connector
- [x] 2026-06-08: README 架构图更新为 `assets/readme.png`
- [x] 2026-06-08: 模型配置连接测试改为读取当前配置凭证
- [x] 2026-06-08: 模型 API 响应隐藏 `api_key`，改为 `has_api_key`
- [x] 2026-06-08: 独立本体图谱 API/UI 页面
- [x] 2026-06-08: 新增 `/api/capabilities` 运行时能力账本，并用测试覆盖
- [x] 2026-06-08: Web UI 新增“能力账本”页面，可查看承诺状态、证据文件、文档来源和下一步动作
- [x] 2026-06-08: HITL UI 历史筛选修复，approved/rejected/all 会读取 `/api/hitl` 而不是 pending-only 数据
- [x] 2026-06-08: 新增 `/api/observability/summary`，并在 Dashboard 展示 tracer/span/metric/token heatmap 摘要
- [x] 2026-06-08: 新增数据飞轮 UI，展示 Dataset Export 预览/写出和 Evaluation Suites 解析结果
- [x] 2026-06-08: 新增 Skills Marketplace API/UI，支持市场搜索、卡片展示、安装按钮和已安装状态刷新
- [x] 2026-06-09: 新增 Sandbox Execution API/UI，支持工作区写入边界、审批策略、命令执行结果和审计轨迹

## 下一批建议按这个顺序补

1. Eval/Dataset 页面：把已存在的数据飞轮能力暴露到 UI，形成可点击产品闭环。
2. MCP 工具挂载页面：展示 MCP 配置、连接测试、工具列表和启停。
3. OTel 部署包：补 `docker-compose.observability.yml`、Jaeger/Grafana 默认配置和健康检查。
4. A2A 最小协议适配：schema、handshake、message bridge、测试。
5. Computer Use 最小闭环：浏览器 session、截图、动作计划接口、审计回放。
