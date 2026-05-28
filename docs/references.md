# Symbio 参考项目

## Agent 框架

| 项目 | 链接 | 参考点 |
|------|------|--------|
| Claude Code (Harness) | github.com/anthropics/claude-code | Agent 调度、工具执行、权限管理 |
| LangGraph | github.com/langchain-ai/langgraph | 图调度、状态管理 |
| AutoGen | github.com/microsoft/autogen | 多 Agent 协作模式 |
| CrewAI | github.com/joaomdmoura/crewai | Agent 角色定义、任务委派 |
| MetaGPT | github.com/geekan/MetaGPT | 多角色协作、SOP 驱动 |

## 记忆系统

| 项目 | 链接 | 参考点 |
|------|------|--------|
| Mem0 | github.com/mem0ai/mem0 | 记忆管理、用户偏好学习 |
| LanceDB | github.com/lancedb/lancedb | 向量数据库使用 |
| LangChain Memory | langchain.com/docs/modules/memory | 记忆模式设计 |

## IM 接入

| 项目 | 链接 | 参考点 |
|------|------|--------|
| Lagrange.OneBot | github.com/LagrangeDev/Lagrange.Core | QQ 协议实现 |
| Wechaty | github.com/wechaty/wechaty | 微信接入 |
| NoneBot2 | github.com/nonebot/nonebot2 | 机器人框架、消息路由 |
| Koishi | github.com/koishijs/koishi | 插件化机器人框架 |

## CLI / TUI

| 项目 | 链接 | 参考点 |
|------|------|--------|
| Typer | github.com/tiangolo/typer | CLI 框架 |
| Rich | github.com/Textualize/rich | 终端美化 |
| Textual | github.com/Textualize/textual | TUI 框架 |

## Web UI

| 项目 | 链接 | 参考点 |
|------|------|--------|
| Open WebUI | github.com/open-webui/open-webui | LLM Web 界面 |
| ChatBox | github.com/Bin-Huang/chatbox | 桌面端 LLM 客户端 |

## 进化与学习

| 项目 | 链接 | 参考点 |
|------|------|--------|
| DSPy | github.com/stanfordnlp/dspy | 自动优化 prompt |
| TextGrad | github.com/zou-group/textgrad | 梯度式优化 |

---

## 可复用的代码/模块

### 从 Claude Code (Harness) 可借鉴
- Agent 调度循环 (agentic loop)
- 工具注册与执行机制
- 权限管理模式
- 上下文压缩策略

### 从 NoneBot2 可借鉴
- 消息事件模型
- 插件加载机制
- 多平台适配器模式

### 从 Mem0 可借鉴
- 记忆提取与更新逻辑
- 用户画像构建
- 记忆检索策略

---

## 学习资源

- [Anthropic Claude Docs](https://docs.anthropic.com)
- [LanceDB 文档](https://lancedb.github.io/lancedb/)
- [OneBot 协议](https://github.com/botuniverse/onebot-11)
