# Symbio 主运行链路闭环改造设计

> 日期：2026-07-22
> 状态：设计完成，待实施
> 范围：6 个断点全部解决

---

## 背景

Symbio 框架基础模块已实现（599 测试中 575 通过），但主运行链路存在 6 个关键断点：配置加载、模型路由、预算拦截、验证闭环、辩论/SubAgent 接入、OTel 部署。各模块接口存在但未串联，导致功能无法形成完整闭环。

## 总体方案：混合策略

- **配置和 OTel**：独立补丁，不涉及主流程
- **执行链路 4 个问题**：引入 ExecutionPipeline，把模型路由、预算、执行策略、验证串成管道

---

## 1. 配置加载修复

### 问题

`get_settings()` 使用 `@lru_cache`，默认创建 `Settings()` 从环境变量读取，只有 `config_file` 有值时才从 YAML 加载。但 `config_file` 本身从环境变量 `SYMBIO_CONFIG_FILE` 读取——鸡生蛋死循环。

### 方案

**自动探测 `symbio.yaml`**：

```python
@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.config_file:
        return Settings.from_yaml(settings.config_file)
    for candidate in [Path("symbio.yaml"), Path.home() / ".symbio" / "config.yaml"]:
        if candidate.exists():
            return Settings.from_yaml(candidate)
    return settings
```

**新增 `reload_settings()` 便利函数**：

```python
def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
```

**`/api/config` 修复**：写入后调用 `reload_settings()` 并同步更新运行中的 ModelRouter 和 HITLNotifier。

**CLI `--config` 修复**：启动时设置 `SYMBIO_CONFIG_FILE` 环境变量。

### 修改文件

- `src/symbio/config/settings.py`
- `src/symbio/interfaces/api.py`
- `src/symbio/cli.py`

### 验证

- 修改 `symbio.yaml` 后 GET `/api/config` 确认返回新值
- 2 个失败的 HITL 配置测试通过

---

## 2. 执行管道设计

### 问题

Orchestrator.run() 是线性流程，各环节之间没有统一拦截机制。模型选择后不传递、预算事后记录、验证自动生成假记录、辩论/SubAgent 从未被调用。

### 方案

引入 ExecutionPipeline 作为胶水层，不重写现有模块。

**PipelineContext 数据结构**：

```python
@dataclass
class PipelineContext:
    task: Task
    model_id: str | None = None          # ModelResolutionStage 填入
    budget_ticket: ResourceTicket | None  # BudgetGateStage 填入
    execution_mode: str = "dag"           # "dag" | "debate" | "subagent"
    root_span: Any | None = None          # OTel span
    result: Result | None = None          # 最终结果
```

**管道实现**：

```python
class ExecutionPipeline:
    def __init__(self, orchestrator):
        self.stages = [
            ModelResolutionStage(orchestrator.router),
            BudgetGateStage(orchestrator.guardrail, orchestrator.budget_manager),
            ExecutionStrategyStage(orchestrator),
            VerificationStage(orchestrator.testing_agent),
        ]

    async def execute(self, task: Task) -> Result:
        ctx = PipelineContext(task=task)
        for stage in self.stages:
            result = await stage.process(ctx)
            if result.should_short_circuit:
                return result.final_result
        return ctx.result
```

### 管道阶段

| 阶段 | 职责 |
|------|------|
| ModelResolutionStage | 解析最终模型 ID（优先 model_override） |
| BudgetGateStage | 事前预算检查，超预算降级或阻断 |
| ExecutionStrategyStage | 选择执行策略（DAG/辩论/SubAgent） |
| VerificationStage | 真实验证（TestingAgent 或证据检查） |

### 修改文件

- 新增 `src/symbio/core/pipeline.py`（约 200 行）
- `src/symbio/core/orchestrator.py`：run() 末尾改为调用 Pipeline

### 验证

- 每个 Stage 独立单元测试
- 完整 Pipeline 集成测试
- 现有 575 个测试不受影响

---

## 3. 模型路由打通

### 问题

- Web 端 model_override 被忽略
- /api/models 添加的模型不加入运行时 Router
- 聊天接口强制要求 Anthropic Key

### 方案

**ModelResolutionStage**：优先使用 `task.metadata["model_override"]`，否则按复杂度自动选择。

**ModelRouter 增强**：

```python
def is_available(self, model_id: str) -> bool:
    return model_id in self._model_pool

def register_model(self, model_info: ModelInfo) -> None:
    self._model_pool[model_info.model_id] = model_info
```

**`/api/models` 接入运行时**：写数据库后同步调用 `router.register_model()`。

**移除强制 Anthropic Key 检查**：改为检查是否有任意可用的 LLM provider。

### 修改文件

- `src/symbio/core/router.py`
- `src/symbio/interfaces/api.py`
- `src/symbio/core/pipeline.py`

### 验证

- OpenAI-only 配置下聊天走通
- /api/models 添加后 router.is_available() 返回 True
- Web 端选择模型后 Orchestrator 使用该模型

---

## 4. 预算拦截机制

### 问题

Guardrail 只签发票据不检查，chat_pipeline 事后记录不拦截，月度预算检查在 LLM 调用完成后才触发。

### 方案

**BudgetGateStage**：事前检查月度预算，超限直接拦截；签发资源票据。

**执行过程中扣减**：dag_runtime._process_node 完成后调用 check_and_deduct，超预算标记节点失败。

**预算降级策略**：默认关闭，通过 `settings.guardrail.auto_downgrade = True` 启用。启用后预算使用超 80% 时自动降级到低成本模型；关闭时仅记录警告日志。

### 修改文件

- `src/symbio/core/pipeline.py`
- `src/symbio/core/dag_runtime.py`

### 验证

- 低预算任务被拦截
- 预算 80% 阈值触发模型降级
- HITL timeout policy 测试通过

---

## 5. 验证闭环重构

### 问题

dag_runtime 自动生成 passed=True、method="agent_self_report" 的假验证记录。

### 方案

**VerificationStage**：收集 NEEDS_VERIFICATION 节点，调用 TestingAgent 或证据检查。

**验证策略矩阵**：

| 节点类型 | 验证策略 |
|----------|----------|
| 代码生成 | TestingAgent 执行测试 |
| 数据分析 | 证据检查（输出非空） |
| 文件操作 | 文件存在性检查 |
| 通用任务 | LLM 自评 + 证据 |

**移除自动生成假验证**：dag_runtime 中删除 agent_self_report 自动生成逻辑。节点执行成功后保持 COMPLETED 状态不变，但在 artifact 中标记 `verification_status: "pending"`。VerificationStage 遍历所有 `verification_required=True` 且 `verification_status="pending"` 的节点进行真实验证，验证完成后更新为 `"passed"` 或 `"failed"`。

### 修改文件

- `src/symbio/core/pipeline.py`
- `src/symbio/core/dag_runtime.py`

### 验证

- 代码生成任务确认 TestingAgent 被调用
- 失败任务正确标记验证失败
- 验证状态测试通过

---

## 6. 辩论/SubAgent 接入

### 问题

_execute_with_debate 和 _execute_with_subagents 已实现但从未被调用。SubAgentManager 调用异步 find_best() 缺少 await。

### 方案

**ExecutionStrategyStage**：读取 `needs_debate` 和 `decomposition` 元数据，分发到对应执行方法。

**修复 SubAgentManager**：`find_best()` 前加 `await`。

**执行策略选择**：

| 条件 | 执行策略 |
|------|----------|
| needs_debate=True | _execute_with_debate |
| decomposition.subtasks > 1 | _execute_with_subagents |
| 默认 | _execute_via_dag |

### 修改文件

- `src/symbio/core/pipeline.py`
- `src/symbio/agents/subagent.py`
- `src/symbio/agents/planner.py`（可选）

### 验证

- 高复杂度任务触发辩论
- 多子任务触发 SubAgent 并行
- SubAgent 异步调用测试通过

---

## 7. OTel 补全

### 问题

docker-compose 引用不存在的 collector.yaml，API 启动时没有初始化 tracer。

### 方案

**创建 `config/otel/collector.yaml`**：OTLP 接收器 + Prometheus 导出器。

**API 启动初始化**：`@app.on_event("startup")` 中根据配置初始化 tracer。

**Settings 增加 OTELConfig**：`enabled`、`endpoint`、`sample_rate` 字段。

### 修改文件

- 新增 `config/otel/collector.yaml`
- `src/symbio/config/settings.py`
- `src/symbio/interfaces/api.py`
- `docker-compose.observability.yml`

### 验证

- docker compose up 确认 Collector 启动
- API 启动后 tracer 初始化日志
- 任务执行后 traces 出现在 Collector

---

## 改造优先级和顺序

| 顺序 | 模块 | 依赖 | 预估工作量 |
|------|------|------|------------|
| 1 | 配置加载修复 | 无 | 小 |
| 2 | 执行管道骨架 | 配置修复 | 中 |
| 3 | 模型路由打通 | 管道骨架 | 小 |
| 4 | 预算拦截 | 管道骨架 | 小 |
| 5 | 验证闭环 | 管道骨架 | 中 |
| 6 | 辩论/SubAgent | 管道骨架 + SubAgent 修复 | 小 |
| 7 | OTel 补全 | 配置修复 | 小 |

## 预期结果

- 4 个失败测试全部通过
- 主运行链路从"模块存在"变为"完整闭环"
- 配置保存后立即生效
- 模型选择、预算拦截、验证、辩论/SubAgent 全部接入主链路
- OTel 可一键部署
