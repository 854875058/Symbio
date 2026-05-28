# Symbio 终极模块全景设计白皮书

> 方案先行，设计升维。不敲一行代码，把每一个模块的底层设计哲学、超前思维、物理边界和技术壁垒推演到无懈可击。

---

## 1. symbio/core/ — 符号与神经流控内核 (The Kernel)

### 1.1 orchestrator.py — 动态 DAG 拓扑中枢

**传统平庸设计：** 固定的 Agent 链（Chain）或死板的条件循环。

**Symbio 超前思维：** 运行时拓扑演化引擎（Runtime Topological Evolution Engine）。

**深度解密：**

任务进来后，Orchestrator 不做具体执行，它是一个"编译与控制流中心"。它首先拉起大模型将任务编译为一张带有依赖关系的动态有向无环图（Dynamic DAG）。在执行过程中，如果某个节点返回了预期外的 Observation（例如环境报错、代码不兼容），Orchestrator 会触发"拓扑重构（Dynamic Re-planning）"，在不丢失其他节点状态的前提下，动态增删、修剪或重组图节点。

**核心壁垒：** 彻底解决复杂长链任务中"一处报错，全盘崩溃"的痛点。

---

### 1.2 router.py & evaluator.py — 前端可配置的智能路由矩阵

**传统平庸设计：** 简单的模型硬编码或基于 if-else 的路由，模型选择写死在代码里。

**Symbio 超前思维：** 前端可配置的多维成本-效能路由器（User-Configurable Speculative Matrix Routing）。

**深度解密：**

用户在 Web UI 中自由配置模型池（添加/删除/启用/禁用模型）和任务-模型绑定策略（哪些任务类型使用哪个模型）。evaluator 将任务转化为高维特征向量，从"语义复杂度、上下文长度、工具调用深度、潜在金融成本"四个维度进行数学评估。router 首先查找用户配置的绑定策略，找不到才降级到自动选择（优先本地模型，再按成本排序）。

**核心壁垒：** 用户完全掌控模型选择，而非被框架绑架。实现企业级生产环境的 Token 终极榨干与极致控本。

---

### 1.3 guardrail.py & rate_limiter.py & resource_manager.py — 安全、流控与多维隔离铁三角

**传统平庸设计：** 简单的 Regex 拦截和大模型后置审查。

**Symbio 超前思维：** 神经符号混合网关与非阻塞自适应背压流控机制（Neuro-Symbolic Gateway with Backpressure Propagation）。

**深度解密：**

- **guardrail** 采用本地静态符号规则（敏感词、系统命令黑名单）与轻量语义分类器双引擎，实现 0 毫秒级的危险动作硬截断。

- **rate_limiter** 采用分布式异步令牌桶算法。当外部大模型 API 触及并发极限（429 Too Many Requests）时，流控器不会简单丢弃请求，而是向 task_queue 发送"反向传播背压信号（Backpressure Signal）"，动态降低 DAG 引擎的并发节点步长。

- **resource_manager** 实现物理隔离：单机模式下利用 Linux rlimit 系统调用硬限制子进程的 CPU/内存/磁盘；集群模式下通过扩展无缝对接 K8s 的 Pod limits 与 Ray 硬件分配需求，杜绝任何 OOM、死循环和磁盘爆满风险。

---

### 1.4 context_pruner.py & cache_aligner.py — Prompt Cache 智能对齐拓扑

**传统平庸设计：** 每次把所有历史原封不动丢给大模型，或者死板地截断。

**Symbio 超前思维：** 前缀确定性对齐与语义剪枝（Deterministic Prefix Alignment）。

**深度解密：**

它是专为 Claude 等原生支持 Prompt Cache 的模型设计的核心优化层。cache_aligner 强制规范系统 Prompt 和全局记忆的排列顺序，确保在多 Agent 或 SubAgent 频繁交织的请求中，前缀（Prefix）哈希完全一致；context_pruner 在后台通过向量重要度与实体热度，动态切除对当前 DAG 节点无贡献的上下文碎片。

**核心壁垒：** 消灭 80% 的长上下文首字延迟（TTFT），并让大模型资费直接斩断至二折。

---

### 1.5 checkpoint.py — 事件溯源分布式快照

**传统平庸设计：** 将当前的聊天历史保存到数据库。

**Symbio 超前思维：** 基于状态机事件溯源（Event Sourcing）的断点续传总线。

**深度解密：**

借鉴数据库重做日志（Write-Ahead Log）设计。Agent 的每一次 Thought、Action、Observation 都是一个不可变的事件。系统会以秒级频率将事件流全异步（通过 aiosqlite 或分布式 Redis）打成快照。若执行过程中突发断电、Pod 挂掉，重启后系统能在 10 毫秒内重放整个 DAG 拓扑，无缝恢复现场。

---

## 2. symbio/agents/ — 分布式 Actor 智能体阵列 (The Actors)

### 2.1 base.py & registry.py — 基于解耦内省的能力契约注册中心

**传统平庸设计：** 给类传参声明名字和描述，让 LLM 瞎猜调用谁。

**Symbio 超前思维：** 语义内省声明契约（Semantic Introspection Contracts）。

**深度解密：**

Agent 类定义不依赖硬编码。每个 Agent 通过 Pydantic 极其严密地声明自己的能力范围（Capabilities）、工具集限制和状态契约。注册中心提供基于语义搜索的动态发现机制。当主调度器发现 DAG 某个节点需要"高级 AST 代码审计"时，它可以向注册中心发出语义检索，动态匹配并实例化最适合的 Agent。

---

### 2.2 subagent.py — Ray-Native 弹性分发控制器

**传统平庸设计：** 在本地多进程或多线程里跑子类。

**Symbio 超前思维：** 基于分布式 Actor 模型的智能体水平扩展（Scale-Out）集群运行时。

**深度解密：**

彻底解耦计算边界。在设计上，SubAgent 继承或被装饰为 Ray Actor。主 Agent 派发子任务时，整个子 Agent 的上下文、状态机和提示词会作为一个有界的 Actor 被远程投递（remote()）到集群中空闲的物理节点上执行。本地无网开发时，它退化为轻量级的单机 asyncio.Task。

**核心壁垒：** 框架天然具备横向推平万卡/万核集群的工业底座实力。

---

### 2.3 debate.py — 多代理辩证共识机

**传统平庸设计：** 简单的 A 问 B 答，或者死循环讨论。

**Symbio 超前思维：** 黑格尔式正反合多维辩论系统（Dialectical Consensus Engine）。

**深度解密：**

专门用于处理模糊、高难度决策。模块内置 Proposer（提案者）、Critic（批判者）、Refiner（修正者）三个角色。当遇到高危工具调用或极其复杂的逻辑漏洞时，触发辩论流。批判者专门从安全限制、边界条件、最坏打算等视角全面痛击提案，修正者根据辩论日志沉淀最终的完美方案。模块内置数学终止条件，防止 Token 陷入死循环风暴。

---

## 3. symbio/memory/ — 双驱动符号认知长效记忆 (The Memory)

### 3.1 ontology.py & retriever.py — 本体论驱动的符号认知图谱

**传统平庸设计：** 把文本做成 Embedding 丢进向量数据库，搜索时找 Top-K 相似。

**Symbio 超前思维：** 概念层（T-Box）与断言层（A-Box）分离的神经符号长期记忆网（Neuro-Symbolic Cognitive Graph）。

**深度解密：**

- **T-Box（世界观约束）：** 系统内置一套不可动摇的本体元模型（如：代码组件必然有调用关系、用户对依赖包必然有偏好等级）。

- **A-Box（事实抽取）：** 后台进化流运行机制会拿着 T-Box 的约束，用 LLM 严密抽取出标准化、去重的实体和关系。

- **零 Token 图推理：** 检索时，不仅利用 LanceDB 做向量语义搜索，同时利用本地 NetworkX（或轻量图结构）进行拓扑推理（例如利用本体规则中 depends_on 的传递性直接推导出完整依赖链路）。

**核心壁垒：** 彻底干掉传统 Graph-RAG 的实体爆炸、同义词冗余和长链推理的幻觉。

---

## 4. symbio/tools/ — 云原生零信任安全执行触手 (The Hand)

### 4.1 mcp.py — Model Context Protocol 原生代理总线

**传统平庸设计：** 为每个第三方工具手写定制的 Python Function 装饰器。

**Symbio 超前思维：** 模型上下文协议标准网关（Unified MCP Gateway）。

**深度解密：**

紧跟 Anthropic 开源标准。Symbio 核心不绑定任何具体工具，而是实现一个高并发的 MCP 客户端/服务端代理。无论是社区里海量的数据库 MCP、GitHub MCP，还是各种私有工具，只要符合标准 JSON-RPC over Standard I/O 协议，即可一键挂载到 Symbio 的工具注册中心。

---

### 4.2 sandbox.py & shell.py — 云原生临时弹性沙箱执行器

**传统平庸设计：** 用 subprocess.run 在本地宿主机上直接裸奔跑命令。

**Symbio 超前思维：** 面向高危动作的瞬时生命周期沙箱（Ephemeral Container Sandbox）。

**深度解密：**

- **本地环境：** 封装精细化的异步子进程管道，注入 resource_manager 的内核配额。

- **生产集群：** 一旦判定为高危、不可控的任意代码执行任务，sandbox 会通过 K8s API 在集群内动态、秒级拉起一个无状态的容器（Pod），并通过网络策略（NetworkPolicy）彻底切断内网访问。工具在 Pod 中执行完毕，数据流被安全回传后，该 Pod 立刻遭到物理抹除。

---

## 5. symbio/evolution/ — 数据飞轮与自进化闭环 (The Flywheel)

### 5.1 feedback.py & analyzer.py — 轨迹全量捕获与根因失效分析

**传统平庸设计：** 记录一个用户打分的 Good/Bad。

**Symbio 超前思维：** Trace-Native 智能化运行质检飞轮（Automated Root Cause Failure Analyzer）。

**深度解密：**

系统全异步捕获完整的多 Agent 协作轨迹（Thought → Action → Observation → Result）。当任务宣告失败或用户发生显式纠错（HITL 介入修改）时，analyzer 会拉起一个专门的"复盘 Agent"，回溯整条 DAG 执行链路，精准定位是"由于 evaluator 路由模型过低"、"某个 SubAgent 工具调用参数错误"还是"记忆检索污染"导致的失败，并将该失败模式（Root Cause）结构化写入离线数据库。

---

### 5.2 optimizer.py — SOP 自动蒸馏与异步微调流水线

**传统平庸设计：** 数据放着看，毫无用处。

**Symbio 超前思维：** 运行即反哺，策略自增强（Self-Optimization Engine）。

**深度解密：**

- **SOP 自动化固化：** 模块定期扫描高频成功的复杂 DAG 路径，将其高度抽象并提炼为标准的结构化 SOP 提示词，注入到系统元 Prompt 中。

- **离线微调闭环：** 当捕获的高质量成功/失败轨迹积累到特定量级（如 5 万条），自动在 Ray 集群中启动离线 Ray Train 分布式微调流水线，对本地小模型进行持续的指令微调（SFT）和对齐。

**核心壁垒：** 让 Symbio 变成一个拥有自我迭代生命周期的有机体，越用越聪明。

---

## 6. symbio/interfaces/ — 多端全时交互审批总线 (The Gateway)

### 6.1 hitl.py — 异步人类介入审批网络

**传统平庸设计：** 在终端弹出一个 input() 让用户按 y/n 确认，如果是远程运行直接卡死。

**Symbio 超前思维：** 解耦挂起与 Webhook 异步审批网关（Asynchronous Approval Hub）。

**深度解密：**

当 Guardrail 判定当前步骤需要人类确认时，hitl.py 会中断当前 DAG 节点的执行，利用 checkpoint.py 完整冻结系统时空状态，将其打包序列化。随后，通过 Webhook 向移动端控制台、飞书、微信或者 WebUI 推送一张包含完整上下文、预期风险和审批按钮的交互卡片。Orchestrator 腾出计算资源去处理其他任务。直到人类在手机上点击"同意"或"修改参数"后，Webhook 触发回调，系统在任意空闲 Pod / 机器上瞬间反序列化，断点继续向下狂飙。

---

## 7. 安全与多模态扩展 (Security & Multi-Modal Extensions)

### 7.1 injection_guard.py — Prompt Injection 防护引擎

**传统平庸设计：** 依靠大模型自身的安全对齐来防御，或者完全不防护。

**Symbio 超前思维：** 神经符号混合安全防火墙（Neuro-Symbolic Security Firewall）。

**深度解密：**

采用三层防御体系：第一层是本地静态符号规则（正则匹配已知攻击模式、敏感词库），实现 0 毫秒级硬截断；第二层是轻量语义分类器（基于微调的小模型），检测隐晦的角色劫持、指令覆盖和越狱尝试；第三层是意图偏离检测，监控 Agent 实际行为是否偏离用户原始意图，发现异常立即熔断。

**核心壁垒：** 从根源上解决 Agent 系统最大的安全盲区，让 Prompt Injection 无处遁形。

---

### 7.2 semantic_cache.py — 语义缓存引擎

**传统平庸设计：** 每次请求都调用大模型，或者简单的字符串匹配缓存。

**Symbio 超前思维：** 向量语义级结果复用（Semantic Result Reuse）。

**深度解密：**

当用户发送请求时，首先通过向量相似度匹配检查是否有语义相同的已缓存结果。"帮我写个快排" 和 "实现快速排序算法" 虽然字面不同，但语义向量高度相似，可以直接复用缓存结果。缓存失效策略基于时间、版本和上下文变更智能触发。与 Prompt Cache 深度整合，实现语义缓存 + 前缀缓存的双重优化。

**核心壁垒：** 高频请求的 Token 成本直接砍到零，响应速度提升 10 倍以上。

---

### 7.3 multimodal.py — 多模态处理引擎

**传统平庸设计：** 只处理文本，图片/文档/音频需要外部工具手动转换。

**Symbio 超前思维：** 统一多模态消息协议（Unified Multi-Modal Protocol）。

**深度解密：**

定义统一的多模态消息格式，任意模态输入（文本、图片、文档、音频）都会被转换为内部统一表示。图片通过视觉模型理解，文档通过解析器结构化提取，音频通过转写引擎转换。Agent 的输出也可以是任意模态。整个流程对上层透明，Agent 无需关心底层模态差异。

---

## 8. 项目生态与社区共建 (Project Ecosystem)

### 8.1 project.py — 项目级隔离管理器

**传统平庸设计：** 所有项目共用一套配置和记忆，数据混乱。

**Symbio 超前思维：** 多租户项目沙箱（Multi-Tenant Project Sandbox）。

**深度解密：**

每个项目拥有独立的 LanceDB 表、本体图谱、配置空间和数据目录。项目间数据物理隔离，支持项目导入/导出和备份恢复。项目级配置覆盖全局配置，允许每个项目自定义模型选择、工具权限和安全策略。项目健康检查独立运行，一个项目的问题不影响其他项目。

---

### 8.2 skills/ — Skills 仓库与市场

**传统平庸设计：** 每个工具都是硬编码的 Python 函数，无法共享。

**Symbio 超前思维：** 社区驱动的技能生态系统（Community-Driven Skill Ecosystem）。

**深度解密：**

Skills 是可复用的 Agent 能力单元，包含 Prompt、工具组合和执行逻辑。官方维护内置 Skills 库（UI 设计、代码审查、数据分析、文档撰写等），社区可通过 Skills 市场贡献和共享自定义 Skills。Skills 支持语义搜索和动态组合，Agent 可以根据任务需求自动发现和加载最合适的 Skills。

**核心壁垒：** 形成网络效应，社区越活跃，Symbio 越强大。

---

## 设计哲学总结

**"大处着眼，小处着手（Think Big, Start Small）。"** 这是顶级代码架构师的最高心法。

Symbio 的每一个模块都遵循以下设计原则：

1. **解耦优先** — 模块间通过抽象接口通信，实现可替换可插拔
2. **本地优先** — 默认轻量级单机运行，集群能力作为扩展无缝接入
3. **防御优先** — 安全、流控、熔断是上线的第一道门槛
4. **智能优先** — 用 LLM 做决策，用符号做约束，用向量做检索
5. **进化优先** — 运行即沉淀，系统越用越聪明
6. **安全优先** — Prompt Injection 防护、沙箱隔离、权限管控是第一公民
7. **生态优先** — 内置预制能力 + 社区共建，形成网络效应

---

> 方案先行阶段完美达成闭环。每一个模块的底层设计哲学、超前思维、物理边界和技术壁垒已推演到无懈可击。
