# Symbio 模块详细规划

## 模块总览

```
symbio/
├── core/                  # 核心模块
│   ├── orchestrator.py    # 调度中枢
│   ├── router.py          # 模型路由器
│   ├── evaluator.py       # 复杂度评估器
│   └── task_queue.py      # 任务队列
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
