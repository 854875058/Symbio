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
│   └── checkpoint.py      # 状态持久化与断点续传
├── agents/                # Agent 模块
│   ├── base.py            # Agent 基类
│   ├── registry.py        # Agent 注册中心
│   ├── builtin/           # 内置 Agent
│   └── subagent.py        # SubAgent 管理
├── memory/                # 记忆模块
│   ├── manager.py         # 记忆管理器
│   ├── short_term.py      # 短期记忆
│   ├── long_term.py       # 长期记忆（LanceDB）
│   └── retriever.py       # 记忆检索
├── tools/                 # 工具模块
│   ├── registry.py        # 工具注册中心
│   ├── cc.py              # Claude Code
│   ├── shell.py           # Shell 命令
│   ├── file.py            # 文件操作
│   ├── git.py             # Git 操作
│   └── http.py            # HTTP 请求
├── interfaces/            # 接入层
│   ├── cli.py             # CLI 入口
│   ├── web/               # Web UI
│   ├── desktop/           # Desktop App
│   └── im/                # IM 接入
│       ├── qq.py
│       ├── wechat.py
│       └── router.py      # 消息路由
├── evolution/             # 进化引擎
│   ├── feedback.py        # 反馈收集
│   ├── analyzer.py        # 模式分析
│   └── optimizer.py       # 策略优化
├── config/                # 配置
│   ├── settings.py        # 全局配置
│   └── models.py          # 模型配置
└── utils/                 # 工具函数
    ├── logger.py
    ├── helpers.py
    └── types.py
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

**职责：** 根据任务复杂度选择合适的模型

```python
class ModelRouter:
    MODELS = {
        Complexity.LOW: "claude-haiku-4-5",
        Complexity.MEDIUM: "claude-sonnet-4-6",
        Complexity.HIGH: "claude-opus-4-7",
    }

    def select(self, complexity: Complexity) -> str:
        return self.MODELS[complexity]

    def select_with_override(self, complexity: Complexity, user_preference: str = None) -> str:
        if user_preference:
            return user_preference
        return self.MODELS[complexity]
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

### 11. 状态检查点 (Checkpoint)

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
