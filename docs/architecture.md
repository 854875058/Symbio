# Symbio 架构设计

## 整体架构

Symbio 采用分层架构，核心思想是"调度中枢 + 多 Agent 协作"。

### 四层架构

```
┌─────────────────────────────────────────────┐
│              接入层 (Interface)               │
│   CLI  |  Web UI  |  Desktop App  |  IM Bot  │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│           调度中枢 (Orchestrator)             │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ 任务理解  │  │ 复杂度   │  │ 模型路由  │   │
│  │  & 规划   │→│ 评估器   │→│  & 派发   │   │
│  └──────────┘  └──────────┘  └──────────┘   │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│            执行层 (Agent Layer)              │
│                                              │
│  ┌───────┐  ┌───────┐  ┌───────┐            │
│  │ Agent │  │ Agent │  │ Agent │  ...        │
│  │  (A)  │  │  (B)  │  │  (C)  │            │
│  └───┬───┘  └───┬───┘  └───┬───┘            │
│      │          │          │                 │
│  SubAgent   SubAgent   SubAgent              │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│             基础层 (Foundation)               │
│                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐      │
│  │ 工具层  │  │ 记忆系统 │  │ 配置中心 │      │
│  │ Tools   │  │ Memory  │  │ Config  │      │
│  └─────────┘  └─────────┘  └─────────┘      │
└─────────────────────────────────────────────┘
```

## 核心组件

### 1. 接入层 (Interface Layer)

统一消息入口，将不同来源的消息标准化为内部格式。

**消息格式：**
```python
class Message:
    source: str          # cli / qq / wechat / web
    user_id: str         # 用户标识
    content: str         # 消息内容
    session_id: str      # 会话ID
    timestamp: datetime
    metadata: dict       # 附加信息
```

### 2. 调度中枢 (Orchestrator)

核心调度引擎，负责：

1. **任务理解** — 解析用户意图
2. **复杂度评估** — 判断任务难度等级
3. **模型路由** — 选择合适的模型
4. **Agent 派发** — 分配任务给 Agent

**复杂度评估模型：**
```python
class TaskComplexity(Enum):
    LOW = "low"          # 简单问答、格式转换 → Haiku
    MEDIUM = "medium"    # 代码生成、文档撰写 → Sonnet
    HIGH = "high"        # 架构设计、复杂推理 → Opus
```

### 3. Agent 层

**Agent 基类：**
```python
class BaseAgent(ABC):
    name: str
    capabilities: list[str]                    # 能力声明
    state_schema: type[pydantic.BaseModel]     # 强制的全局状态契约

    @abstractmethod
    async def execute_node(self, node_context: DAGNode) -> NodeObservation:
        """接收图节点上下文，返回包含测试结果与状态变更的观测值"""
        pass

    async def delegate(self, subtask: Task, target_agent: str = None) -> DAGNode:
        """派发子任务，返回新的 DAG 节点"""
        pass

    async def report_observation(self, observation: NodeObservation) -> None:
        """向 DAG 引擎汇报观测结果，触发可能的拓扑重构"""
        pass
```

**SubAgent 机制：**
- 主 Agent 通过 DAG 引擎派发子任务，生成新的 DAG 节点
- SubAgent 执行完成后向 DAG 引擎汇报观测结果
- DAG 引擎根据观测结果决定是否触发动态拓扑重构
- 支持并行执行多个 SubAgent，通过状态机协调

### 4. 工具层 (Tool Layer)

工具注册与执行框架：

```python
class ToolRegistry:
    def register(self, name: str, tool: BaseTool)
    def execute(self, name: str, params: dict) -> Result
    def list_tools(self) -> list[str]
```

内置工具：
- `cc` — Claude Code 调用
- `shell` — Shell 命令执行
- `file` — 文件读写
- `git` — Git 操作
- `browser` — 网页访问

### 5. 记忆系统 (Memory System)

基于 LanceDB 的向量记忆：

```python
class MemoryManager:
    # 短期记忆（当前会话）
    def add_short_term(self, key: str, value: any)
    def get_short_term(self, key: str) -> any

    # 长期记忆（持久化）
    def store(self, content: str, metadata: dict)
    def recall(self, query: str, top_k: int) -> list[Memory]
    def forget(self, memory_id: str)
```

### 6. 进化引擎 (Evolution Engine)

从反馈中学习，持续优化行为：

```python
class EvolutionEngine:
    def collect_feedback(self, task_id: str, feedback: Feedback)
    def analyze_patterns(self) -> list[Insight]
    def update_strategy(self, insight: Insight) -> None
```

## 数据流

```
用户消息 → 接入层 → 调度中枢
                      │
                      ├→ 复杂度评估
                      ├→ 模型选择
                      └→ Agent 派发
                           │
                           ├→ 主 Agent 执行
                           │     ├→ 直接处理
                           │     └→ 派发 SubAgent
                           │           ├→ 调用工具
                           │           └→ 检索记忆
                           │
                           └→ 结果汇总 → 返回用户
```

## 扩展机制

### 插件系统
```python
class Plugin:
    name: str
    version: str

    def on_message(self, message: Message) -> Optional[Message]
    def on_task_complete(self, task: Task, result: Result) -> None
    def register_tools(self, registry: ToolRegistry) -> None
```

### Agent 注册
```python
# 注册自定义 Agent
@register_agent("code_reviewer")
class CodeReviewerAgent(BaseAgent):
    capabilities = ["code_review", "security_check"]
    model = "sonnet"
```

---

## 安全架构

### 分层防御体系

```
┌─────────────────────────────────────────────────────────────┐
│                    Layer 1: 输入净化层                        │
│  正则匹配 + 敏感词库 + 0ms 硬截断                             │
├─────────────────────────────────────────────────────────────┤
│                    Layer 2: 语义检测层                        │
│  轻量语义分类器 + Prompt Injection 检测                       │
├─────────────────────────────────────────────────────────────┤
│                    Layer 3: 意图审计层                        │
│  行为偏离检测 + 输出审计 + 异常熔断                            │
└─────────────────────────────────────────────────────────────┘
```

### 权限分级模型

```python
class PermissionLevel(Enum):
    READ_ONLY = "read_only"      # 安全：只读操作
    WRITE = "write"              # 敏感：写操作
    EXECUTE = "execute"          # 高危：执行操作，强制绑定 HITL
    ADMIN = "admin"              # 管理：系统配置操作
```

---

## 可观测性架构

### OpenTelemetry 集成

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent 执行链路                             │
│  Orchestrator → Agent → SubAgent → Tool                     │
│       │           │         │         │                     │
│       ▼           ▼         ▼         ▼                     │
│    Span 1      Span 2    Span 3    Span 4                   │
│                                                             │
│                    Trace (完整链路)                           │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    可观测性后端                               │
│  Jaeger (链路追踪) + Prometheus (指标) + Grafana (可视化)     │
└─────────────────────────────────────────────────────────────┘
```

### 指标采集

| 指标类型 | 采集内容 |
|----------|----------|
| Counter | Token 消耗、API 调用次数、任务完成数 |
| Histogram | 响应时间、任务执行时间、工具调用时间 |
| Gauge | 活跃 Agent 数、队列长度、内存使用量 |

---

## HITL 架构

### 审批流程

```
高危操作检测
    │
    ▼
冻结 DAG 节点状态
    │
    ▼
序列化检查点
    │
    ▼
发送审批请求 (Webhook)
    ├─→ 飞书审批卡片
    ├─→ 微信消息
    ├─→ Web UI 通知
    └─→ 邮件通知
    │
    ▼
等待审批结果
    ├─→ 同意 → 恢复执行
    ├─→ 拒绝 → 取消任务
    └─→ 超时 → 自动拒绝/降级
```

---

## 项目隔离架构

### 多租户数据模型

```
Global Config
    │
    ├── Project A
    │   ├── Memory Store (LanceDB Table)
    │   ├── Ontology Graph
    │   ├── Config Override
    │   └── Resource Quota
    │
    ├── Project B
    │   ├── Memory Store (LanceDB Table)
    │   ├── Ontology Graph
    │   ├── Config Override
    │   └── Resource Quota
    │
    └── Project C
        └── ...
```

### 配置继承链

```
Global Config (默认值)
    │
    ▼
Project Config (项目覆盖)
    │
    ▼
Task Config (任务级覆盖)
```

---

## 数据飞轮架构

### 轨迹捕获流程

```
Agent 执行
    │
    ▼
捕获 Thought → Action → Observation → Result
    │
    ▼
存储到轨迹数据库 (aiosqlite)
    │
    ▼
后台异步处理
    ├─→ 质量评估
    ├─→ 去重过滤
    ├─→ PII 脱敏
    └─→ 格式转换 (ShareGPT/Alpaca)
    │
    ▼
导出高质量微调数据集
```

---

## 边缘计算架构

### 分层部署模型

```
┌─────────────────────────────────────────────────────────────┐
│                    云端 (Cloud)                              │
│  完整 Agent 运行时 + 大模型 API + 记忆存储                    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    边缘 (Edge)                               │
│  轻量级运行时 + 本地模型 + 状态缓存                           │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    设备 (Device)                             │
│  嵌入式 SDK + 端侧推理 + 离线模式                            │
└─────────────────────────────────────────────────────────────┘
```
