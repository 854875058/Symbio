# Symbio（共生）

> 多 Agent 协同 AI 助手，支持任务派发、记忆系统、IM 接入、自动进化。

## 核心特性

- **多 Agent 架构** — 主 Agent + SubAgent 分层派发
- **智能路由** — 按任务复杂度分配模型（Haiku / Sonnet / Opus）
- **记忆系统** — LanceDB 驱动，短期 + 长期记忆持久化
- **IM 接入** — QQ、微信、飞书等消息平台
- **工具集成** — Claude Code、Shell、Git、文件操作
- **自动进化** — 从用户反馈中学习，持续优化行为

## 架构概览

```
接入层 (CLI / Web UI / Desktop / IM)
        │
   调度中枢 (Orchestrator)
        │
  ┌─────┼─────┐
  ▼     ▼     ▼
Agent Agent Agent
  │     │     │
  ▼     ▼     ▼
SubAgent (子任务派发)
        │
   工具层 (CC / Shell / Git / File / API)
        │
   记忆系统 (LanceDB)
```

## 技术栈

- **核心**: Python 3.10+
- **前端**: Next.js / Tauri
- **记忆**: LanceDB
- **IM**: Lagrange.OneBot (QQ) / wechaty (微信)
- **Agent 框架**: 自研

## 快速开始

> 开发中...

## 文档

- [功能清单](docs/features.md)
- [架构设计](docs/architecture.md)
- [模块设计白皮书](docs/module-design-whitepaper.md)
- [开发路线图](docs/roadmap.md)
- [模块规划](docs/modules.md)
- [技术栈选型](docs/tech-stack.md)
- [参考项目](docs/references.md)

## 开源协议

MIT License
