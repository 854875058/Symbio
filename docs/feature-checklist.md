# Symbio 功能实现清单

> 对照公众号文章承诺，逐项落实。每完成一项更新状态。

---

## P0 — 核心卖点（必须优先实现）

### 文章 1：多Agent系统为什么总是过早宣布完成

- [x] `submit_task` 强制 Tool Calling 结束机制 — `tools/submit_task.py`
- [x] JSON Checklist 数据模型（files/test/completion_criteria） — `agents/checklist.py`
- [x] Initializer Agent 任务开始时自动生成 Checklist — `agents/initializer.py`
- [x] `submit_task` 验证逻辑（文件存在/非空/checklist 完成/测试通过） — ChecklistValidator
- [x] Testing Agent 闭环验证（pytest/npm test → 失败回退 → 重试） — `agents/testing_agent.py`
- [x] 状态驱动通信（全局状态对象 Single Source of Truth） — StateManager
- [x] 每轮任务完成后清空 Agent 会话历史 — Orchestrator.clear_agent_session()

### 文章 2：Agent间通信的致命陷阱

- [x] StateManager 类（asyncio.Lock + read/update/CAS 操作） — `core/state_manager.py`
- [x] 全局状态对象标准化结构（task_id/status/phase/checklist/files/tests/errors/metadata） — GlobalState
- [x] Orchestrator.generate_instruction（从 pending checklist 生成指令） — InstructionGenerator
- [x] Orchestrator.get_minimal_context（最小上下文裁剪） — InstructionGenerator
- [x] 状态持久化到 SQLite — aiosqlite state_snapshots 表
- [x] Agent 间零对话通信集成（将 StateManager 接入 Orchestrator 主流程） — Orchestrator 已集成

### 文章 7：HITL-Agent卡住了怎么办

- [x] 异步审批网关（冻结→通知→继续其他任务→回调→恢复） — `core/hitl_gateway.py`
- [x] 审批请求六要素（做什么/影响范围/为什么/替代方案/操作按钮/超时） — ApprovalRequest
- [x] 分级审批（低/中/高/极高 → 自动通过/1人/2人/3人） — RiskLevel
- [x] Webhook 回调端点（JWT 签名验证） — generate/verify_approval_token
- [x] HITL 与 DAG 集成（节点标记 + 非阻塞） — Orchestrator._check_hitl_required + resume_after_approval
- [x] 防审批疲劳（合并同类/智能升级/置信度加权） — FatiguePreventer

---

## P1 — 重要增强

### 文章 4：记忆系统为什么向量数据库不够用

- [x] 记忆压缩流水线（聚类→模式→规则→T-Box注入→冷存储归档） — `memory/compression.py`
- [x] 版本化记忆（更新保留历史，可追溯/可回滚/可审计） — `memory/versioning.py`
- [x] 四层冲突解决（时间戳/可信度加权/因果推理/用户确认） — `memory/versioning.py`
- [x] 三级噪音过滤（规则0ms→分类器10ms→LLM 200ms） — `memory/filters.py`
- [x] 记忆写入安全网关（经过安全检查后才写入） — `memory/filters.py`
- [x] 五层遗忘策略 L3 冲突覆盖 + L5 项目清理 — `memory/filters.py`

### 文章 5：安全防护PromptInjection三层防火墙

- [x] 信任区域划分（不可信/半可信/可信） — `security/trust_zones.py`
- [x] 记忆写入安全网关（符号规则+语义分类+来源验证） — `memory/filters.py`
- [x] 安全测试流水线（1000+ 攻击样本自动化测试） — `security/trust_zones.py`

### 文章 8：Token成本优化从PromptCache到语义缓存

- [x] 工具懒加载（项目级静态隔离 + DAG节点级动态加载） — `tools/lazy_loader.py`
- [x] Prompt Cache 保活 Ping（4分钟心跳） — `core/cost_monitor.py`
- [x] 成本监控仪表盘（Token消耗/缓存命中率/路由分布） — `core/cost_monitor.py`
- [x] 项目级成本预算管理（月度预算 + 80%自动降级） — `core/cost_monitor.py`

### 文章 6：数据飞轮让Agent越用越聪明

- [x] SOP 蒸馏质量阈值（成功率100%/Token<1.5x/步数<1.5x/重试≤1） — `evolution/sop_distiller.py`
- [x] 异步非阻塞轨迹捕获（内存队列+批量写入+背压控制） — `evolution/sop_distiller.py`
- [x] 冷启动种子 SOP 库 — `evolution/sop_distiller.py`

---

## P2 — 锦上添花

- [ ] 离线微调 Ray Train 集成（需 5 万条轨迹）
- [x] 多模态记忆（图片/PDF/代码） — `memory/multimodal.py`
- [ ] K8s Ephemeral Sandbox
- [x] DAG 分层路由评估（70%/15%/10%/5%） — `core/layered_router.py`
- [x] 并发状态合并（MapReduce + 乐观锁） — StateManager CAS 操作已实现
- [x] 冷启动代码仓库扫描 — `memory/cold_start.py`
- [x] Testing Agent 失败重试闭环 — `agents/testing_agent.py` TestDrivenLoop
- [x] 工具懒加载接入 Orchestrator — orchestrator.py 集成
- [x] 安全攻击样本库（50+） — `security/attack_samples.py`
- [x] 三道保险丝（步数/预算/重复） — `core/layered_router.py` CircuitBreaker

---

## 完成记录

| 日期 | 完成项 | Commit |
|------|--------|--------|
| 2026-06-01 | Phase 1-3 基础架构 + 记忆系统 | (多个) |
| 2026-06-01 | submit_task + Checklist 机制 | 4d8a96c |
| 2026-06-01 | StateManager + 零对话通信 | affc805 |
| 2026-06-01 | HITL 异步审批网关 | c031390 |
| 2026-06-01 | P0 集成（Initializer+Testing+StateManager+HITL接入） | a93576d, edbf1a0 |
| 2026-06-01 | P1+P2 全部实现（8 个新模块） | d91d3f1 |
| 2026-06-01 | P2 补全：分层路由+冷启动+重试+多模态+攻击样本 | (多个) |
