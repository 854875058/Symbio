# Symbio 模块详细规划

## 模块总览

```
symbio/
├── core/                  # 核心模块
│   ├── orchestrator.py    # 调度中枢
│   ├── router.py          # 模型路由器
│   ├── evaluator.py       # 复杂度评估器
│   ├── task_queue.py      # 任务队列
│   ├── guardrail.py       # 预算熔断与安全拦截
│   ├── rate_limiter.py    # 并发流控 (令牌桶/漏桶)
│   ├── checkpoint.py      # 状态持久化与断点续传
│   ├── dag_engine.py      # 动态 DAG 引擎
│   ├── context_pruner.py  # 上下文智能剪枝
│   ├── cache_aligner.py   # Prompt Cache 对齐器
│   ├── resource_manager.py # 资源管控 (Token 预算、步数、超时)
│   ├── tracer.py          # 可观测性 (OTel 链路追踪)
│   ├── semantic_cache.py  # 语义缓存
│   ├── injection_guard.py # Prompt Injection 防护
│   └── event_bus.py       # 事件总线 (Agent 间通信)
├── agents/                # Agent 模块
│   ├── base.py            # Agent 基类
│   ├── registry.py        # Agent 注册中心
│   ├── builtin/           # 内置预制 Agent
│   │   ├── ui_designer.py     # UI 设计 Agent
│   │   ├── code_reviewer.py   # 代码审查 Agent
│   │   ├── data_analyst.py    # 数据分析 Agent
│   │   └── doc_writer.py      # 文档撰写 Agent
│   ├── subagent.py        # SubAgent 管理
│   ├── debate.py          # 多代理共识辩论
│   └── simulator.py       # Agent 仿真测试
├── memory/                # 记忆模块
│   ├── manager.py         # 记忆管理器
│   ├── short_term.py      # 短期记忆
│   ├── long_term.py       # 长期记忆（LanceDB）
│   ├── retriever.py       # 记忆检索
│   └── ontology.py        # 本体引擎 (Ontology-Enhanced Memory)
├── tools/                 # 工具模块
│   ├── registry.py        # 工具注册中心
│   ├── mcp.py             # MCP 协议网关
│   ├── cc.py              # Claude Code
│   ├── shell.py           # Shell 命令
│   ├── file.py            # 文件操作
│   ├── git.py             # Git 操作
│   ├── http.py            # HTTP 请求
│   ├── sandbox.py         # 沙箱执行器
│   └── multimodal.py      # 多模态处理 (图片/文档/音频)
├── interfaces/            # 接入层
│   ├── cli.py             # CLI 入口
│   ├── web/               # Web UI
│   ├── desktop/           # Desktop App
│   └── im/                # IM 接入
│       ├── qq.py
│       ├── wechat.py
│       ├── feishu.py      # 飞书接入
│       ├── dingtalk.py    # 钉钉接入
│       └── router.py      # 消息路由
│   ├── hitl.py            # HITL 人类介入审批总线
│   ├── api.py             # FastAPI 服务端接口
│   ├── edge/              # 边缘计算
│   │   ├── runtime.py     # 轻量级运行时
│   │   ├── mobile_sdk.py  # 移动端 SDK
│   │   └── iot_manager.py # IoT 设备管理
├── evolution/             # 进化引擎
│   ├── feedback.py        # 反馈收集
│   ├── analyzer.py        # 模式分析
│   ├── optimizer.py       # 策略优化
│   ├── promptops.py       # Prompt 版本管理与 A/B 测试
│   └── eval_pipeline.py   # 自动化评测管道
├── skills/                # Skills 仓库
│   ├── registry.py        # Skills 注册中心
│   ├── builtin/           # 内置 Skills
│   ├── marketplace.py     # Skills 市场
│   └── schema.py          # Skill 标准格式定义
├── evolution/             # 进化引擎
│   ├── feedback.py        # 反馈收集
│   ├── analyzer.py        # 模式分析
│   ├── optimizer.py       # 策略优化
│   ├── promptops.py       # Prompt 版本管理与 A/B 测试
│   ├── eval_pipeline.py   # 自动化评测管道
│   └── self_optimizer.py  # Agent 自我进化引擎
├── config/                # 配置
│   ├── settings.py        # 全局配置
│   ├── models.py          # 模型配置
│   ├── project.py         # 项目级配置隔离
│   └── migration.py       # 版本迁移工具
├── security/              # 安全模块
│   ├── privacy.py         # 隐私计算（联邦学习、差分隐私）
│   ├── sanitizer.py       # 数据脱敏引擎
│   └── audit.py           # 审计日志
├── docs/                  # 文档生成
│   ├── generator.py       # 自动文档生成器
│   └── examples/          # 示例项目
└── utils/                 # 工具函数
    ├── logger.py
    ├── helpers.py
    ├── types.py
    └── memory_manager.py  # 内存管理与垃圾回收
```

---

## 核心模块详解

### 1. 调度中枢 (Orchestrator)

**职责：** 接收任务，评估复杂度，选择模型，派发给 Agent

```python
class Orchestrator:
    def __init__(self, config: Config):
        self.router = ModelRouter(config)
        self.evaluator = ComplexityEvaluator()
        self.agent_registry = AgentRegistry()
        self.task_queue = TaskQueue()

    async def process(self, message: Message) -> Result:
        # 1. 理解任务意图
        intent = await self.parse_intent(message)

        # 2. 评估复杂度
        complexity = await self.evaluator.evaluate(intent)

        # 3. 选择模型
        model = self.router.select(complexity)

        # 4. 选择 Agent
        agent = self.agent_registry.find_best(intent)

        # 5. 执行任务
        result = await agent.execute(Task(
            intent=intent,
            model=model,
            context=message.context
        ))

        return result
```

### 2. 模型路由器 (ModelRouter)

**职责：** 根据任务复杂度选择合适的模型，支持前端可配置

**设计哲学：** 模型路由不硬编码，用户在 Web UI 中自由配置模型池和任务-模型绑定策略。

```python
class ModelConfig(BaseModel):
    """单个模型配置"""
    model_id: str                    # 模型标识 (如 "claude-sonnet-4-20250514")
    provider: str                    # 供应商 (anthropic/openai/local)
    display_name: str                # 显示名称
    api_key: str = ""                # API Key (加密存储)
    base_url: str = ""               # 自定义 Base URL
    max_tokens: int = 4096           # 最大输出 Token
    cost_per_1k_input: float = 0.0   # 输入成本 ($/1K tokens)
    cost_per_1k_output: float = 0.0  # 输出成本 ($/1K tokens)
    is_local: bool = False           # 是否本地模型
    enabled: bool = True             # 是否启用

class TaskModelBinding(BaseModel):
    """任务-模型绑定策略"""
    task_type: str                   # 任务类型 (code/chat/research/planning)
    complexity_level: Complexity     # 复杂度等级
    preferred_model_id: str          # 首选模型 ID
    fallback_model_id: str = ""      # 备选模型 ID

class ModelRouter:
    """可配置的模型路由器"""

    def __init__(self, config: RouterConfig):
        self.model_pool: dict[str, ModelConfig] = {}  # 模型池 (从配置加载)
        self.bindings: list[TaskModelBinding] = []     # 绑定策略 (从配置加载)
        self.load_from_config(config)

    def load_from_config(self, config: RouterConfig) -> None:
        """从配置文件/数据库加载模型池和绑定策略"""
        self.model_pool = {m.model_id: m for m in config.models}
        self.bindings = config.bindings

    def select(self, task_type: str, complexity: Complexity) -> str:
        """根据任务类型和复杂度选择模型"""
        # 1. 查找用户配置的绑定
        binding = self._find_binding(task_type, complexity)
        if binding and binding.preferred_model_id in self.model_pool:
            model = self.model_pool[binding.preferred_model_id]
            if model.enabled:
                return model.model_id

        # 2. 降级到备选模型
        if binding and binding.fallback_model_id in self.model_pool:
            return binding.fallback_model_id

        # 3. 最终降级：按复杂度自动选择
        return self._auto_select(complexity)

    def _auto_select(self, complexity: Complexity) -> str:
        """自动选择：优先本地模型，再按成本排序"""
        candidates = [m for m in self.model_pool.values() if m.enabled]
        if not candidates:
            raise NoModelAvailableError()

        # 按复杂度筛选合适的模型
        if complexity == Complexity.LOW:
            # 优先本地模型
            local = [m for m in candidates if m.is_local]
            if local:
                return local[0].model_id

        # 按成本排序
        candidates.sort(key=lambda m: m.cost_per_1k_input)
        return candidates[0].model_id

    async def update_model_pool(self, models: list[ModelConfig]) -> None:
        """热更新模型池（前端调用）"""
        self.model_pool = {m.model_id: m for m in models}
        # 持久化到配置文件
        await self._persist_config()

    async def update_bindings(self, bindings: list[TaskModelBinding]) -> None:
        """热更新绑定策略（前端调用）"""
        self.bindings = bindings
        await self._persist_config()
```

### 3. 复杂度评估器 (ComplexityEvaluator)

**职责：** 评估任务复杂度

评估维度：
- **token 预估** — 预期输入/输出长度
- **推理深度** — 是否需要多步推理
- **工具依赖** — 是否需要调用外部工具
- **上下文依赖** — 是否需要历史记忆

```python
class ComplexityEvaluator:
    async def evaluate(self, intent: Intent) -> Complexity:
        score = 0

        # 文本长度评分
        score += self._score_length(intent.text)

        # 关键词评分
        score += self._score_keywords(intent.text)

        # 工具需求评分
        score += self._score_tool_dependency(intent)

        # 上下文评分
        score += self._score_context(intent)

        return self._score_to_complexity(score)
```

### 4. Agent 基类 (BaseAgent)

```python
class BaseAgent(ABC):
    name: str
    description: str
    capabilities: list[str]
    default_model: str

    def __init__(self, config: Config):
        self.config = config
        self.memory = MemoryManager()
        self.tools = ToolRegistry()

    @abstractmethod
    async def execute(self, task: Task) -> Result:
        """执行任务"""
        pass

    async def delegate(self, subtask: Task) -> Result:
        """派发子任务给 SubAgent"""
        subagent = SubAgent(parent=self, task=subtask)
        return await subagent.execute()

    async def recall(self, query: str) -> list[Memory]:
        """检索相关记忆"""
        return await self.memory.recall(query)

    async def remember(self, content: str, metadata: dict):
        """存储记忆"""
        await self.memory.store(content, metadata)
```

### 5. SubAgent 管理

```python
class SubAgent(BaseAgent):
    def __init__(self, parent: BaseAgent, task: Task):
        self.parent = parent
        self.task = task
        # 继承父 Agent 的记忆和工具
        self.memory = parent.memory
        self.tools = parent.tools

    async def execute(self, task: Task = None) -> Result:
        task = task or self.task
        result = await self._run(task)
        # 向父 Agent 汇报
        await self.parent.on_subagent_complete(self, result)
        return result

    async def _run(self, task: Task) -> Result:
        # 调用 LLM 执行任务
        response = await self.llm.chat(
            model=task.model,
            messages=self._build_messages(task),
            tools=self.tools.get_schemas()
        )
        return Result.from_response(response)
```

### 6. 记忆管理器 (MemoryManager)

```python
class MemoryManager:
    def __init__(self, config: MemoryConfig):
        self.short_term = ShortTermMemory(max_tokens=config.window_size)
        self.long_term = LongTermMemory(db_path=config.lancedb_path)

    async def store(self, content: str, metadata: dict = None):
        """存储到长期记忆"""
        embedding = await self.embed(content)
        await self.long_term.insert(
            content=content,
            embedding=embedding,
            metadata=metadata or {}
        )

    async def recall(self, query: str, top_k: int = 5) -> list[Memory]:
        """语义检索"""
        embedding = await self.embed(query)
        return await self.long_term.search(embedding, top_k)

    async def get_context(self, session_id: str) -> str:
        """获取会话上下文"""
        return self.short_term.get_context(session_id)
```

### 7. 工具注册中心 (ToolRegistry)

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    async def execute(self, name: str, params: dict) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            raise ToolNotFoundError(name)
        return await tool.execute(params)

    def get_schemas(self) -> list[dict]:
        """获取所有工具的 schema，用于 LLM function calling"""
        return [tool.to_schema() for tool in self._tools.values()]
```

### 8. IM 消息路由

```python
class IMRouter:
    def __init__(self):
        self.adapters: dict[str, IMAdapter] = {}
        self.session_manager = SessionManager()

    def register_adapter(self, platform: str, adapter: IMAdapter):
        self.adapters[platform] = adapter

    async def receive(self, platform: str, raw_message: dict) -> Message:
        """将平台消息转为统一格式"""
        adapter = self.adapters[platform]
        return adapter.parse(raw_message)

    async def reply(self, platform: str, session_id: str, content: str):
        """发送回复到指定平台"""
        adapter = self.adapters[platform]
        await adapter.send(session_id, content)
```

---

## 内置 Agent 列表

| Agent | 能力 | 默认模型 |
|-------|------|----------|
| `GeneralAgent` | 通用对话、问答 | Sonnet |
| `CodeAgent` | 代码生成、修改、调试 | Sonnet |
| `FileAgent` | 文件读写、搜索、整理 | Haiku |
| `GitAgent` | Git 操作、PR 管理 | Haiku |
| `ResearchAgent` | 信息检索、分析总结 | Opus |
| `PlannerAgent` | 任务规划、分解 | Opus |

---

## 防御性模块详解

### 9. 预算熔断与安全拦截 (Guardrail)

**职责：** 任务级预算控制、步数限制、安全拦截

```python
class Guardrail:
    def __init__(self, config: GuardrailConfig):
        self.max_cost_usd = config.max_cost_usd
        self.max_steps = config.max_steps
        self.blocked_commands = config.blocked_commands

    async def check_budget(self, task_chain: TaskChain) -> bool:
        """检查任务链是否超预算"""
        total_cost = task_chain.get_total_cost()
        if total_cost >= self.max_cost_usd:
            raise BudgetExceededError(f"Cost {total_cost} USD exceeded limit {self.max_cost_usd}")
        return True

    async def check_steps(self, task: Task) -> bool:
        """检查任务步数是否超限"""
        if task.step_count >= self.max_steps:
            raise StepLimitExceededError(f"Steps {task.step_count} exceeded limit {self.max_steps}")
        return True

    async def check_command(self, command: str) -> bool:
        """检查命令是否安全"""
        for blocked in self.blocked_commands:
            if blocked in command:
                raise BlockedCommandError(f"Command contains blocked pattern: {blocked}")
        return True

    async def check_tool_permission(self, tool: str, action: str) -> PermissionLevel:
        """检查工具权限等级"""
        permission = self.get_tool_permission(tool)
        if permission == PermissionLevel.HIGH_RISK:
            await self.request_hitl_approval(tool, action)
        return permission
```

### 10. 并发流控器 (RateLimiter)

**职责：** LLM API 请求流控，防止触发 RPM/TPM 限制

```python
class RateLimiter:
    """基于令牌桶算法的异步流控器"""

    def __init__(self, config: RateLimitConfig):
        self.buckets: dict[str, TokenBucket] = {}
        self.retry_config = RetryConfig(
            max_retries=3,
            base_delay=1.0,
            max_delay=60.0,
            jitter=True
        )

    async def acquire(self, model: str, tokens: int = 1) -> None:
        """获取执行许可，必要时等待"""
        bucket = self._get_bucket(model)
        await bucket.acquire(tokens)

    async def release(self, model: str, tokens: int = 1) -> None:
        """释放令牌"""
        bucket = self._get_bucket(model)
        bucket.release(tokens)

    async def handle_rate_limit(self, error: RateLimitError, model: str) -> None:
        """处理 429 错误，指数退避重试"""
        for attempt in range(self.retry_config.max_retries):
            delay = self._calculate_backoff(attempt)
            logger.warning(f"Rate limited on {model}, retrying in {delay}s (attempt {attempt+1})")
            await asyncio.sleep(delay)
            if await self._try_acquire(model):
                return
        raise RateLimitExhaustedError(f"Rate limit retries exhausted for {model}")
```

### 11. 资源管控器 (ResourceManager)

**职责：** Token 预算、步数限制、超时控制，防止系统自杀或烧光额度

```python
class ResourceTicket:
    """资源支票：任务启动时签发的总预算"""

    def __init__(self, config: ResourceConfig):
        self.max_cost_usd: float = config.max_cost_usd       # 最大 Token 费用 (美元)
        self.max_steps: int = config.max_steps                 # 最大执行步数
        self.max_tokens: int = config.max_tokens               # 最大 Token 总量
        self.timeout_seconds: int = config.timeout_seconds     # 单步超时

        # 实时消耗追踪
        self.consumed_cost: float = 0.0
        self.consumed_steps: int = 0
        self.consumed_tokens: int = 0
        self.created_at: datetime = datetime.now()

    def deduct(self, usage: TokenUsage) -> None:
        """扣减资源额度"""
        self.consumed_cost += usage.cost_usd
        self.consumed_tokens += usage.total_tokens
        self.consumed_steps += 1

    def is_exhausted(self) -> bool:
        """检查是否耗尽"""
        return (
            self.consumed_cost >= self.max_cost_usd or
            self.consumed_steps >= self.max_steps or
            self.consumed_tokens >= self.max_tokens
        )

    def remaining(self) -> dict:
        """返回剩余配额"""
        return {
            "cost_usd": max(0, self.max_cost_usd - self.consumed_cost),
            "steps": max(0, self.max_steps - self.consumed_steps),
            "tokens": max(0, self.max_tokens - self.consumed_tokens),
        }

    def is_expired(self) -> bool:
        """检查是否超时"""
        elapsed = (datetime.now() - self.created_at).total_seconds()
        return elapsed > self.timeout_seconds


class ResourceManager:
    """资源管控网关：Token 预算 + 步数熔断 + 超时控制"""

    def __init__(self, config: ResourceConfig):
        self.default_config = config
        self.active_tickets: dict[str, ResourceTicket] = {}  # task_id -> ticket

    def issue_ticket(self, task_id: str, overrides: dict = None) -> ResourceTicket:
        """为任务签发资源支票"""
        config = self.default_config.model_copy(update=overrides or {})
        ticket = ResourceTicket(config)
        self.active_tickets[task_id] = ticket
        logger.info(f"Resource ticket issued for {task_id}: {ticket.remaining()}")
        return ticket

    async def check_and_deduct(self, task_id: str, usage: TokenUsage) -> bool:
        """检查资源并扣减额度，返回是否允许继续"""
        ticket = self.active_tickets.get(task_id)
        if not ticket:
            return True  # 无票证任务不限制

        # 扣减
        ticket.deduct(usage)

        # 检查是否耗尽
        if ticket.is_exhausted():
            remaining = ticket.remaining()
            logger.warning(f"Resource exhausted for {task_id}: {remaining}")
            raise ResourceExhaustedError(
                task_id=task_id,
                reason="Budget exhausted",
                consumed={"cost": ticket.consumed_cost, "steps": ticket.consumed_steps},
                remaining=remaining
            )

        if ticket.is_expired():
            raise ResourceExhaustedError(
                task_id=task_id,
                reason="Timeout exceeded",
                consumed={"cost": ticket.consumed_cost, "steps": ticket.consumed_steps}
            )

        return True

    def release_ticket(self, task_id: str) -> None:
        """任务完成，释放资源支票"""
        self.active_tickets.pop(task_id, None)

    def get_status(self, task_id: str) -> dict:
        """获取任务资源消耗状态"""
        ticket = self.active_tickets.get(task_id)
        if not ticket:
            return {"status": "no_ticket"}
        return {
            "status": "active",
            "consumed": {
                "cost_usd": ticket.consumed_cost,
                "steps": ticket.consumed_steps,
                "tokens": ticket.consumed_tokens,
            },
            "remaining": ticket.remaining(),
            "expired": ticket.is_expired(),
        }
```

### 12. 状态检查点 (Checkpoint)

**职责：** 任务状态持久化与断点续传

```python
class CheckpointManager:
    """任务状态检查点管理"""

    def __init__(self, db_path: str = "./data/checkpoints.db"):
        self.db = sqlite3.connect(db_path)
        self._init_schema()

    async def save_checkpoint(self, task_dag: TaskDAG) -> str:
        """保存当前任务 DAG 状态"""
        checkpoint_id = str(uuid4())
        snapshot = {
            "checkpoint_id": checkpoint_id,
            "task_dag": task_dag.to_dict(),
            "short_term_memory": task_dag.agent.memory.short_term.export(),
            "execution_log": task_dag.get_execution_log(),
            "timestamp": datetime.now().isoformat()
        }
        self.db.execute(
            "INSERT INTO checkpoints (id, data) VALUES (?, ?)",
            (checkpoint_id, json.dumps(snapshot))
        )
        self.db.commit()
        return checkpoint_id

    async def load_checkpoint(self, checkpoint_id: str) -> TaskDAG:
        """从检查点恢复任务状态"""
        row = self.db.execute(
            "SELECT data FROM checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if not row:
            raise CheckpointNotFoundError(checkpoint_id)

        snapshot = json.loads(row[0])
        task_dag = TaskDAG.from_dict(snapshot["task_dag"])
        task_dag.agent.memory.short_term.import_(snapshot["short_term_memory"])
        return task_dag

    async def list_checkpoints(self, task_id: str = None) -> list[dict]:
        """列出可用检查点"""
        if task_id:
            rows = self.db.execute(
                "SELECT id, timestamp FROM checkpoints WHERE task_id = ? ORDER BY timestamp DESC",
                (task_id,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, timestamp FROM checkpoints ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        return [{"id": r[0], "timestamp": r[1]} for r in rows]
```

### 12. 评测管道 (EvalPipeline)

**职责：** 自动化评测，防止系统退化

```python
class EvalPipeline:
    """Agent 评测管道"""

    def __init__(self, config: EvalConfig):
        self.test_cases: list[TestCase] = []
        self.results: list[EvalResult] = []

    def load_test_suite(self, path: str) -> None:
        """加载测试集"""
        with open(path) as f:
            suite = json.load(f)
        for case in suite["test_cases"]:
            self.test_cases.append(TestCase(
                input=case["input"],
                expected_tools=case.get("expected_tools", []),
                expected_output=case.get("expected_output", ""),
                max_tokens=case.get("max_tokens", 1000)
            ))

    async def run(self, agent: BaseAgent) -> EvalReport:
        """运行评测"""
        results = []
        for case in self.test_cases:
            result = await self._eval_single(agent, case)
            results.append(result)

        return EvalReport(
            total=len(results),
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed),
            tool_accuracy=self._calc_tool_accuracy(results),
            details=results
        )

    async def _eval_single(self, agent: BaseAgent, case: TestCase) -> EvalResult:
        """评测单个用例"""
        task = Task(intent=Intent(raw_text=case.input))
        result = await agent.execute(task)

        tool_match = set(result.tools_used) == set(case.expected_tools)
        output_match = self._fuzzy_match(result.content, case.expected_output)

        return EvalResult(
            passed=tool_match and output_match,
            tool_match=tool_match,
            output_match=output_match,
            actual_tools=result.tools_used,
            actual_output=result.content,
            token_usage=result.token_usage
        )
```

---

## 骨灰级高阶模块详解

### 13. 动态 DAG 引擎 (DAGEngine)

**职责：** 运行时动态重构任务拓扑，实现"兵无常势，水无常形"

```python
class DAGNode:
    """DAG 节点"""
    node_id: str
    agent: BaseAgent
    task: Task
    dependencies: list[str]  # 依赖的节点 ID
    status: NodeStatus
    result: Optional[Result]

class DAGEngine:
    """动态 DAG 执行引擎"""

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator
        self.nodes: dict[str, DAGNode] = {}
        self.execution_order: list[str] = []

    async def execute(self, initial_dag: DAG) -> Result:
        """执行 DAG，支持运行时动态修改"""
        self.nodes = initial_dag.nodes

        while not self._is_complete():
            # 获取可执行节点（依赖已完成）
            ready_nodes = self._get_ready_nodes()

            # 并行执行
            tasks = [self._execute_node(node) for node in ready_nodes]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果，可能触发动态重构
            for node, result in zip(ready_nodes, results):
                node.result = result
                node.status = NodeStatus.COMPLETED

                # 关键：根据中间结果决定是否重构后续拓扑
                should_replan = await self._evaluate_replan_need(node, result)
                if should_replan:
                    await self._replan_dag(node, result)

        return self._aggregate_results()

    async def _replan_dag(self, completed_node: DAGNode, result: Result) -> None:
        """根据中间结果动态重构 DAG"""
        # 调用 Orchestrator 重新规划后续步骤
        new_subgraph = await self.orchestrator.replan(
            completed_node=completed_node,
            observation=result,
            current_dag=self.nodes
        )

        # 合并新子图到现有 DAG
        self._merge_subgraph(new_subgraph)

        # 检查是否可以合并并行链路
        self._optimize_parallel_paths()

    def _optimize_parallel_paths(self) -> None:
        """优化：合并可并行的节点，拆分过重的节点"""
        # 识别独立子树并行化
        # 识别串行瓶颈并拆分
        pass
```

### 14. 上下文智能剪枝器 (ContextPruner)

**职责：** 语义级上下文压缩，而非粗暴滑动窗口

```python
class ContextPruner:
    """基于语义的上下文智能剪枝"""

    def __init__(self, config: PrunerConfig):
        self.summary_model = config.summary_model  # 用于提取关键点的小模型
        self.cache_block_size = config.cache_block_size

    async def prune(self, history: list[Message], max_tokens: int) -> list[Message]:
        """智能剪枝历史消息"""
        if self._count_tokens(history) <= max_tokens:
            return history

        # 第一步：提取每条消息的"决策关键点"
        key_points = await self._extract_key_points(history)

        # 第二步：识别"状态增量"（哪些消息改变了系统状态）
        state_changes = self._identify_state_changes(history)

        # 第三步：裁剪无用中间体（原始 HTML、大段日志等）
        pruned = self._remove_noise(history, key_points, state_changes)

        # 第四步：如果仍然超限，合并相似消息
        if self._count_tokens(pruned) > max_tokens:
            pruned = await self._merge_similar(pruned)

        return pruned

    async def _extract_key_points(self, history: list[Message]) -> list[str]:
        """用小模型提取每条消息的决策关键点"""
        # 对于工具输出，提取：成功/失败、关键数值、错误原因
        # 对于对话，提取：用户意图、决策、约束条件
        pass

    def _remove_noise(self, history: list[Message], key_points: list[str], state_changes: list[int]) -> list[Message]:
        """移除噪声，保留信号"""
        pruned = []
        for i, msg in enumerate(history):
            if i in state_changes:
                pruned.append(msg)  # 状态变更消息必须保留
            elif self._is_noise(msg):
                # 替换为关键点摘要
                pruned.append(Message(content=f"[Summary: {key_points[i]}]"))
            else:
                pruned.append(msg)
        return pruned
```

### 15. Prompt Cache 对齐器 (CacheAligner)

**职责：** 按模型缓存规则优化记忆布局，最大化缓存命中

```python
class CacheAligner:
    """Prompt Cache 深度对齐"""

    def __init__(self, model: str):
        self.model = model
        self.cache_prefix_rules = self._load_cache_rules(model)

    def align_memory_blocks(self, messages: list[Message]) -> list[Message]:
        """重新排列消息以最大化缓存命中"""
        # Claude 缓存规则：前缀匹配，越长的静态前缀命中率越高

        # 1. 分离静态内容（系统提示、工具定义）和动态内容（对话）
        static_blocks, dynamic_blocks = self._split_blocks(messages)

        # 2. 将静态内容放在最前面，形成稳定的缓存前缀
        aligned = static_blocks + dynamic_blocks

        # 3. 确保动态内容中的"准静态"部分（如长文档）也参与缓存
        aligned = self._promote_quasi_static(aligned)

        return aligned

    def estimate_cache_savings(self, messages: list[Message]) -> dict:
        """估算缓存带来的成本节省"""
        total_tokens = self._count_tokens(messages)
        cacheable_tokens = self._count_cacheable_tokens(messages)
        hit_rate = cacheable_tokens / total_tokens

        return {
            "total_tokens": total_tokens,
            "cacheable_tokens": cacheable_tokens,
            "estimated_hit_rate": hit_rate,
            "estimated_cost_reduction": hit_rate * 0.5,  # 缓存命中通常节省 50% 成本
            "estimated_ttft_improvement": f"{hit_rate * 70:.0f}%"  # TTFT 改善
        }
```

### 16. 多代理共识辩论 (MultiAgentDebate)

**职责：** 高精度任务的多轮辩论与交叉验证

```python
class DebateRole(str, Enum):
    PROPOSER = "proposer"   # 创造者：提出方案
    CRITIC = "critic"       # 批判者：找问题
    REFINER = "refiner"     # 修正者：综合改进

class MultiAgentDebate:
    """多代理共识辩论引擎"""

    def __init__(self, config: DebateConfig):
        self.max_rounds = config.max_rounds  # 最大辩论轮次
        self.consensus_threshold = config.consensus_threshold  # 共识阈值

    async def debate(self, task: Task, context: str) -> DebateResult:
        """启动三方辩论"""
        # 克隆 3 个不同角色的 SubAgent
        proposer = self._create_agent(DebateRole.PROPOSER, task)
        critic = self._create_agent(DebateRole.CRITIC, task)
        refiner = self._create_agent(DebateRole.REFINER, task)

        history = []
        current_proposal = None

        for round_num in range(self.max_rounds):
            # Proposer 提出方案
            if round_num == 0:
                current_proposal = await proposer.execute(task)
            else:
                current_proposal = await proposer.refine(history)

            # Critic 批判
            critique = await critic.analyze(current_proposal)

            # 检查是否达成共识
            if self._is_consensus(critique):
                return DebateResult(
                    final_output=current_proposal,
                    rounds=round_num + 1,
                    consensus=True,
                    history=history
                )

            # Refiner 修正
            refined = await refiner.synthesize(current_proposal, critique)

            history.append({
                "round": round_num,
                "proposal": current_proposal,
                "critique": critique,
                "refinement": refined
            })

            current_proposal = refined

        # 超过最大轮次，投票决定
        return DebateResult(
            final_output=self._vote(history),
            rounds=self.max_rounds,
            consensus=False,
            history=history
        )

    def _create_agent(self, role: DebateRole, task: Task) -> BaseAgent:
        """根据角色创建辩论 Agent"""
        prompts = {
            DebateRole.PROPOSER: "You are a creative problem solver. Propose solutions.",
            DebateRole.CRITIC: "You are a rigorous critic. Find flaws and risks.",
            DebateRole.REFINER: "You are a synthesizer. Combine the best ideas and fix issues."
        }
        return SubAgent(
            task=task,
            system_prompt=prompts[role],
            model="sonnet"  # 辩论用中等模型，节省成本
        )
```

### 17. 本体引擎 (OntologyEngine)

**职责：** 向量+本体(Ontology)双驱动记忆，赋予 Agent 领域语义理解与推理能力

```python
class OntologyClass:
    """本体类定义"""
    class_id: str
    name: str                          # 类名 (如: Module, User, Task)
    description: str                   # 类描述
    parent_classes: list[str]          # 父类 (继承层次)
    properties: list[OntologyProperty] # 类属性定义
    constraints: list[Constraint]      # 约束条件

class OntologyProperty:
    """本体属性定义"""
    property_id: str
    name: str                          # 属性名 (如: depends_on, prefers, created_by)
    domain: str                        # 定义域 (适用的类)
    range_type: str                    # 值域 (属性值类型)
    is_functional: bool                # 是否函数性 (单值)
    inverse_of: Optional[str]          # 逆属性 (如: depends_on 的逆是 depended_by)

class OntologyInstance:
    """本体实例 (具体个体)"""
    instance_id: str
    class_id: str                      # 所属类
    name: str                          # 实例名
    property_values: dict[str, Any]    # 属性值
    embedding: list[float]             # 向量嵌入

class OntologyEngine:
    """Vector + Ontology 双驱动记忆引擎"""

    def __init__(self, config: OntologyConfig):
        self.vector_store = LanceDB(config.lancedb_path)
        self.ontology_store = OntologyStore(config.ontology_db_path)
        self.reasoner = OntologyReasoner()
        self.instance_extractor = InstanceExtractor(config.llm)

    async def define_class(self, cls: OntologyClass) -> None:
        """定义本体类"""
        await self.ontology_store.upsert_class(cls)

    async def ingest(self, content: str, metadata: dict) -> None:
        """摄入新知识：向量化 + 实例抽取"""
        # 1. 向量化存储
        embedding = await self.embed(content)
        await self.vector_store.insert(content, embedding, metadata)

        # 2. 实例抽取 (识别内容中的具体个体及其属性)
        instances = await self.instance_extractor.extract(content, self.ontology_store)

        # 3. 写入本体实例库
        for instance in instances:
            await self.ontology_store.upsert_instance(instance)

    async def hybrid_retrieve(self, query: str, top_k: int = 5) -> RetrievalResult:
        """混合检索：向量相似 + 本体推理"""
        # 1. 向量检索
        query_embedding = await self.embed(query)
        vector_results = await self.vector_store.search(query_embedding, top_k)

        # 2. 识别查询涉及的本体类和实例
        mentioned = await self.instance_extractor.identify(query, self.ontology_store)

        # 3. 本体推理扩展
        ontology_context = []
        for instance in mentioned:
            # 获取实例的直接关系
            direct_relations = await self.ontology_store.get_instance_relations(instance.instance_id)
            ontology_context.extend(direct_relations)

            # 推理：利用本体规则推导隐含关系
            inferred = await self.reasoner.infer(instance, self.ontology_store)
            ontology_context.extend(inferred)

        # 4. 继承层次查询
        for cls_id in set(i.class_id for i in mentioned):
            parent_instances = await self.ontology_store.get_instances_by_class(cls_id, include_subclasses=True)
            ontology_context.extend(parent_instances)

        # 5. 融合排序
        combined = self._merge_and_rank(vector_results, ontology_context)

        return RetrievalResult(
            vector_results=vector_results,
            ontology_context=ontology_context,
            combined=combined,
            mentioned_instances=mentioned
        )

class OntologyReasoner:
    """本体推理引擎"""

    async def infer(self, instance: OntologyInstance, store: OntologyStore) -> list:
        """基于本体规则推导隐含知识"""
        inferred = []

        # 规则1：传递性推理 (如: A depends_on B, B depends_on C → A depends_on C)
        transitive_relations = await self._infer_transitive(instance, store)
        inferred.extend(transitive_relations)

        # 规则2：继承推理 (如: User_X is_a PremiumUser, PremiumUser has_feature PrioritySupport → User_X has_feature PrioritySupport)
        inherited = await self._infer_inheritance(instance, store)
        inferred.extend(inherited)

        # 规则3：逆属性推理 (如: A created_by B → B created A)
        inverse = await self._infer_inverse(instance, store)
        inferred.extend(inverse)

        return inferred
```
