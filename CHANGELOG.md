# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-06-16

### Changed
- README 重构为视觉化项目主页：新增 Hero 截图与 2×2 截图画廊（成本中心 / 安全防火墙 / 数据飞轮 / Computer Use / 本体图谱），核心能力改为收益导向呈现，去掉冗长的"相关代码"文件清单
- 图片改用绝对 raw URL，使 PyPI 详情页也能正常渲染截图
- README_zh.md 同步加入 Hero 与截图画廊

### Added
- `tools/capture_screenshots.py`：基于 Playwright 的 Web UI 截图脚本（可复现）

## [0.2.0] - 2026-06-15

### Added

#### Token 成本五层优化接入运行时
- 新增 `src/symbio/core/chat_pipeline.py`：把语义缓存、上下文剪枝、成本监控接进 `/api/chat` 与 `/ws/chat`
- 语义缓存命中后零 Token 返回（HTTP 直接返回 / WS 分块回放），上下文指纹保证复用等价性，无 embedding key 时优雅降级
- 上下文超预算时按 FULL 策略剪枝并修复 user/assistant 交替结构
- 新增 `GET /api/costs/{summary,cache,budget,dashboard}` 与月度预算设置
- Dashboard 新增"成本中心"面板：24h 用量、缓存命中率、节省 Token、预算进度条、按模型用量明细

#### Prompt Injection 三层防火墙接入对话入口
- 修复检测引擎：经典注入语 "ignore all previous instructions" 因语序漏匹配的 bug；单签名命中升 HIGH；让死配置 `threat_threshold` 真正作为动作阈值生效；多重高危信号聚合升 CRITICAL
- 扩充 8 类攻击签名（直接/间接注入、越狱、泄露、角色劫持、社工胁迫、数据外泄、编码绕过）；攻击样本自检拦截率 0% → 65%，对编程话题零误伤
- 新增 `src/symbio/security/chat_guard.py`，接进 `/api/chat`、`/ws/chat`；新增 `GET/POST /api/security/{stats,audit,scan,selftest}`
- Web UI 新增"安全"页面：三层防御概览、威胁分布、在线扫描、红队自检、审计轨迹

#### 数据飞轮四阶段闭环
- 新增 `src/symbio/evolution/flywheel.py`：串起 捕获 → 失效分析 → SOP 蒸馏 → 反哺优化
- 新增 `GET/POST /api/flywheel/{overview,failures,sops,sops/distill,feedback}`
- Web UI 飞轮页升级：四阶段闭环可视化、失效分析与根因、SOP 卡片、现场记录失败驱动闭环

#### HITL 审批超时策略闭环
- 网关新增 `escalate()`：转交管理员、延长截止时间并重新计时、记录升级轨迹，超出上限自动落到拒绝
- `_timeout_handler` 按 `timeout_policy` 分流：自动拒绝 / 自动通过 / 转交管理员
- 新增 `GET/POST /api/hitl/timeout/policy` 读写默认策略并持久化到 `symbio.yaml`
- Web UI 审批页新增超时策略配置（含"转交管理员"、转交目标、最大升级次数）

#### Computer Use 最小闭环
- 新增 `src/symbio/tools/computer_use.py`：浏览器会话控制、动作集（navigate/screenshot/click/type/scroll/extract_text）、启发式动作规划、审计轨迹与回放；Playwright 不可用时降级为 dry-run record-only
- 新增 `POST /api/computer-use/sessions`、`/act`、`/plan`、`/replay`、`DELETE`
- Web UI 新增 Computer Use 页面：会话管理、目标驱动规划执行、手动动作、审计时间线、回放

#### Web UI 暖色浅底主题（Claude Code / Hermes 风格）
- 默认主题改为浅色，重做 light 调色板为暖米白底 + 赤陶强调色 + 暖灰文字
- 引入可主题化品牌渐变变量，全站统一；新增 `prefers-reduced-motion` 支持
- 中英混排页面全部中文化；新增输入框回车快捷键

#### A2A 协议（Agent-to-Agent）
- 新增 `src/symbio/interfaces/a2a.py`：完整的 A2A 数据模型（AgentCard、A2ATask、A2ASession）和 A2ASessionManager
- `GET /.well-known/agent.json` 对外发布本机 AgentCard
- `POST /api/a2a/tasks` 接收入站 A2A 任务并自动接入 LLM 处理
- `GET|POST /api/a2a/sessions` 管理出站多轮会话，自动探测远程 AgentCard
- `GET /api/a2a/probe` 探测任意远程 Agent 的 AgentCard
- Web UI 新增 A2A 页面（本机名片、出站会话、入站任务列表）

#### HITL 通知渠道扩展
- 新增钉钉自定义机器人（`dingtalk`）：ActionCard 富卡片 + Markdown
- 新增 Telegram Bot（`telegram`）：支持群组和私聊
- 新增 WxPusher（`wxpusher`）：**个人微信**推送，扫码绑定
- 新增 PushPlus（`pushplus`）：**个人微信**推送，token 配置
- 新增 Server酱（`serverchan`）：**个人微信/企业微信**推送
- 新增 Slack Webhook（`slack`）：Block Kit 富卡片
- 全部渠道均支持同意/拒绝按钮跳转回调

#### HITL 渠道管理 API + 超时策略
- `POST /api/hitl/channels` 添加渠道（持久化到 `data/hitl_channels.json`）
- `GET /api/hitl/channels/list` 列出所有渠道（含 yaml 和手动配置）
- `POST /api/hitl/channels/test` 发送测试通知
- `DELETE /api/hitl/channels/{id}` 删除渠道
- `GET /api/hitl/timeout/check` 批量检查并处理超时审批（auto_reject / auto_approve）
- Web UI 审批页新增可视化渠道管理面板和超时策略控制

#### MCP 工具网关 UI
- `GET|POST|DELETE /api/mcp/servers` MCP 服务器增删查
- `POST /api/mcp/servers/{id}/tools` 连接并探测工具列表
- Web UI 新增 MCP 页面（server 管理、工具探测）

#### OTel 可观测部署包
- `docker-compose.observability.yml`：一键拉起 Jaeger + Prometheus + Grafana + OTel Collector
- `config/otel/collector.yaml`：OTLP 接收器 → Jaeger + Prometheus 导出
- `config/prometheus/prometheus.yml`：Symbio 指标采集配置
- `config/grafana/provisioning/`：Prometheus 和 Jaeger 数据源自动注入

#### UI 重构
- 顶部 13-tab 导航 → 左侧分组侧边栏（核心/智能体/工具/知识/分析/配置），支持折叠
- 新增 Topbar 显示当前页标题、连接状态、Token 消耗
- 仪表盘 Token 趋势从 div 柱子升级为 Chart.js 交互式柱状图
- 字体换用 Inter，设计语言更专业

### Changed
- `capabilities.py`：新增 `token_cost_optimization`、`prompt_injection_defense`（均 implemented）；`computer_use_loop` 从 `missing` 升级为 `partial`；新增诚实保留的 `federated_privacy`（missing）；`a2a_protocol` 从 `missing` 升级为 `partial`，`mcp_gateway` 和 `observability_otel` 新增证据文件
- Web UI 从 14 个页面扩展到 16 个（新增"安全"与"Computer Use"）

### Fixed
- HITL 渠道通知中 `_feishu_sign` 函数调用方式修正
- Web UI 断线重连逻辑优化，Topbar 连接状态实时同步

## [Unreleased]

### Added
- 初始项目架构设计
- 33 个杀手级亮点特性定义
- 10 Phase 开发路线图
- 完整的模块设计白皮书
- UI 设计方案（22 个页面）
- 核心骨架代码（配置、日志、事件总线）

### Changed
- 无

### Deprecated
- 无

### Removed
- 无

### Fixed
- 无

### Security
- 无

## [0.1.0] - 2026-05-28

### Added
- 项目初始化
- 核心架构设计
- 文档体系建设

---

## 版本说明

### 版本格式

- **主版本号 (MAJOR)**: 不兼容的 API 变更
- **次版本号 (MINOR)**: 向下兼容的功能性新增
- **修订号 (PATCH)**: 向下兼容的问题修正

### 变更类型

- **Added**: 新功能
- **Changed**: 对现有功能的变更
- **Deprecated**: 已经不建议使用，即将移除的功能
- **Removed**: 已移除的功能
- **Fixed**: 任何 Bug 的修复
- **Security**: 安全相关的变更
