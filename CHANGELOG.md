# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-12

### Added

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
- `capabilities.py`：`a2a_protocol` 状态从 `missing` 升级为 `partial`，`mcp_gateway` 和 `observability_otel` 新增证据文件

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
