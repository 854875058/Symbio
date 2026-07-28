# Product

<!-- impeccable:product-schema 1 -->

> **本文档的证据来源与推断标注**
> 用户授权跳过访谈直接从仓库证据推断（原话："你接下来的任何事情不用问我，自己做决定"）。
> 因此下文每一条都标注了来源：`【证据】` = 有代码/测试/文档背书；`【推断】` = 我从证据合理外推，未经用户确认。
> 未来任何人可以推翻 `【推断】` 条目而不必视为改需求。

## Platform

web

## Users

**主要用户：项目作者本人（单人开发者 / AI Infra 方向）。**【证据】

- 仓库是个人探索项目（路径 `E:\工作\个人探索\Symbio`），git 用户 `Symbio Dev`，单人提交历史。
- 部署形态是 `pip install symbio && symbio serve`，默认只绑 `127.0.0.1`，无多用户体系、无账号、无权限分级——鉴权是单一共享 Bearer token，能力账本 `api_authentication` 的 `next_step` 明确写着"Add per-user accounts, scoped tokens, and rotation instead of one shared token"。这是单人自用形态的直接证据。
- 使用场景：在自己机器上跑起服务，浏览器开 `http://localhost:9090/ui`，边开发 Symbio 本身边用它干活。

**次要用户：GitHub 上评估这个项目的其他开发者。**【推断】

- 项目已发布到 PyPI，README 有三语版本（中/英/日）、徽章、截图墙。这些是给外部读者看的，说明作者在意外部评估。
- 但这类用户的主要接触面是 README 而非 Web UI；UI 上他们最可能做的事是"装完跑起来、点一圈看看是不是真的"。**这一条对设计的含义很重要：全新安装、零数据状态下的 UI 就是这个项目的第一印象**，而当前 17 个页面在零数据时大面积空白。

**明确不是用户：** 团队协作场景、生产环境运维、非技术用户。【推断，依据是无多用户/无 RBAC/无审计导出/文案全部预设读者懂 DAG、LoRA、MCP 这类术语】

## Product Purpose

**把 Agent 能力拆成可观测、可审批、可恢复、可验证的基础设施模块，并且让每一条能力主张都能在运行时被验证。**【证据：README 开篇 + `src/symbio/capabilities.py`】

产品要解决的不是"让模型调用工具"，而是它后面那层工程问题（README「为什么是 Symbio」逐条列出）：任务半途失败怎么恢复、危险操作谁来审批、多 Agent 协作怎么不把 Token 打爆、记忆能不能带结构、Prompt Injection 第一道防线在哪、运行轨迹能不能反哺训练数据。

**成功的样子**【推断】：
1. 作者自己日常真的在用它，而不是写完就放着。
2. 外部开发者装上后能在几分钟内看懂"这个项目到底实现到什么程度"——而不是被 17 个空页面挡在门外。
3. 能力账本里 `implemented` 的条目，用户能在 UI 上亲手验证到。

## Positioning

**运行时自带能力账本，是这个项目最难被邻居产品照抄的机制。**【证据：`src/symbio/capabilities.py`、`GET /api/capabilities`、`tests/test_capabilities.py:47` 会断言每条 evidence 路径真实存在】

原则写在 README 里："已经落地的写成能力，部分落地的写清缺口，没实现的只放路线图"。当前 21 条能力中 19 条 `implemented`、2 条 `partial`（`sandbox_cluster`、`federated_privacy`），每条都带 evidence 文件路径和 `next_step`。测试会在 evidence 路径失效时报错——这意味着账本不可能悄悄腐化。

这条定位对 UI 设计有直接约束【推断】：**能力账本页面不是一个普通的信息展示页，它是产品定位的具象化**，理应是全站最有分量的页面之一，而不是导航里排在末尾的一个次要条目。

## Operating Context

**运行环境**【证据】
- 本机单进程 FastAPI，`symbio serve --port 9090`，UI 在 `/ui`。
- 前端是纯静态资产（`web/` 目录），无构建工具、无 npm、无框架。经典 `<script>` 按序加载 20 个文件，共享全局作用域。三方库（Chart.js / xterm.js / qrcode）全部本地打包，零 CDN 依赖，可离线/内网隔离运行。
- 浏览器是唯一 GUI；另有 `symbio` CLI 覆盖部分操作（chat / task list / memory store / export）。

**典型工作流**【证据来自 README CLI 示例与 API 路由】
- 对话驱动：在 chat 页发起任务 → 任务落成 DAG → 高危节点触发 HITL 审批 → 审批卡片推到微信 → 通过后继续执行。
- 接管驱动：把本机已有的 Claude Code / Codex 会话登记进来，在工作台里多开平铺，甚至在网页里开真 PTY 终端跑 TUI。
- 反哺驱动：运行轨迹被捕获 → 失效分析 → SOP 蒸馏 → 导出数据集 → LoRA 微调。

**关键使用特征**【推断】
- 大量长时运行任务，用户需要"边跑边看"，所以实时性（WebSocket 推流、轮询状态）比页面美观更重要。
- 单人使用意味着**没有交接成本，但也没有他人纠错**——UI 上任何一个静默失败都可能被长期忽视。这支持"宁可吵闹也不要静默"的反馈设计。

## Capabilities and Constraints

**能力真相的唯一来源是 `src/symbio/capabilities.py`，不是 `docs/feature-checklist.md`。**【证据：项目记忆里已记录此事；文档层曾与代码脱节】

21 条能力，按模块：
- orchestration / workflow：动态 DAG 运行时、Planner-Reviewer-Verification 策略
- hitl：多渠道人工审批（Web / webhook / QQ / WeCom / Feishu / 文本命令 / 微信卡片自动推送）
- memory：本体记忆图谱（零 Token 符号推理）、多模态视觉记忆
- models / cost：模型池与路由、Token 成本分层优化（语义缓存 + 上下文剪枝 + 成本监控 + 预算）
- security：Prompt Injection 三层防火墙、可选全局 Bearer 鉴权
- skills / tools：Skills 市场（含 GitHub 导入）、MCP stdio 网关、沙箱（`partial`——本地沙箱 + Docker 隔离已有，集群未实现）
- external_agents / agents：外部 Agent 接管与实时同步、A2A 协议、单 Agent 委托 Claude Code/Codex 后端
- interfaces：个人微信扫码机器人
- observability：OpenTelemetry trace/metrics/token 热力图
- evolution：数据飞轮四阶段 + 真 LoRA SFT 训练后端
- distributed：Ray Actor 池
- browser：Computer Use 视觉闭环
- platform：联邦隐私学习（`partial`——缺跨机权重传输、梯度泄漏防御、拜占庭鲁棒聚合）

**技术约束（对设计是硬边界）**【证据】
- 前端不引入构建工具、npm、框架。任何设计方案必须能用原生 HTML/CSS/JS 实现。
- 经典脚本共享全局作用域，加载顺序有意义（基础层在前，`init` 最后）。
- 三方库必须本地打包，不能引 CDN。Web 字体是渐进增强，缺失时回退系统字体。
- `web/` 下资源由 `StaticFiles` 直接提供，`?v=N` 查询参数是唯一的缓存失效手段。
- 测试 `tests/test_capabilities.py` 会校验 evidence 路径存在——重命名前端文件必须同步改能力账本。

**术语（面向懂行读者，不做通俗化）**【证据：全站文案现状】
DAG、HITL、SOP、LoRA / SFT / adapter、MCP、A2A、AgentCard、PTY、Prompt Injection、语义缓存、上下文剪枝、本体 / 概念 / 实体 / 关系、数据飞轮。

**明确未决**
- 是否要做多用户/团队形态：未决，当前所有证据都指向单人自用。
- 国际化：README 有三语，但 UI 文案全中文硬编码、无 i18n 层。是否要做 UI 多语言未决。【推断】

## Brand Commitments

- **名称**：Symbio（共生）。Logo 在 `assets/symbio-logo.png`。【证据】
- **语言**：中文优先。代码注释、commit message、UI 文案全中文；技术标识符保留原文不翻译。【证据：全仓库一致，且项目记忆已记录用户要求全程中文】
- **视觉方向（用户此前明确选定，是绑定约束）**：冷静专业型——单一橙棕强调色、1px 实线边框、不用发光/光球/渐变文字、阴影只给浮层用。【证据：用户在上一轮改造中通过选项明确选择】
- **主题策略（用户明确选定）**：深浅双主题都要校准，默认跟随 `prefers-color-scheme`，顶栏按钮写 localStorage。【证据：同上】
- **文档语气**：不吹。README 的自我约束"已实现/部分实现/规划中"要一致贯彻到 UI——**UI 不得把 `partial` 能力显示成已完成**。【推断，但与能力账本机制强一致】

## Evidence on Hand

**真实存在、可直接用于设计**
- `assets/symbio-logo.png` — 产品 logo。
- `assets/screenshots/` — 6 张真实 UI 截图（ui-chat / ui-dashboard / ui-security / ui-flywheel / ui-computer-use / ui-wechat），README 截图墙在用。
- `benchmarks/` — 可复现的性能基准：分层路由 LLM 避免率 85%、上下文剪枝压缩到 25%、语义缓存改写命中率 30%、Prompt Injection 样本库拦截率约 65%。**这些数字是真的，有基准脚本背书，UI 上可以引用。**
- `GET /api/capabilities` — 运行时能力账本，21 条带状态与证据路径。
- 测试套件 659 passed / 23 skipped。

**不存在，未来工作不得编造**
- 无真实用户数量、无客户案例、无第三方测评、无 star 数承诺、无 SLA、无定价。
- 无团队成员信息（单人项目）。
- 全新安装后**没有任何业务数据**：无对话、无任务、无记忆、无轨迹、无成本记录。这是零数据状态设计的事实前提，不能靠假数据掩盖。

## Product Principles

1. **可验证优先于可宣称。** 每条能力主张都要能在运行时被用户亲手验证到；`partial` 就显示为 `partial`，缺口写清楚。这是产品的立身之本，UI 不得稀释它。
2. **零数据状态是第一印象，不是边缘情况。** 单人自用 + 外部评估者的双重身份意味着"刚装好、什么都没有"是最高频的首次体验。空状态必须解释这里是什么、为什么空、下一步点哪里。
3. **宁可吵闹也不要静默。** 单人使用没有他人纠错，静默失败会被长期忽视。每个操作都要有明确反馈，每个错误都要说清发生了什么、该怎么办。
4. **信息密度服务于操作效率，不是堆砌。** 用户是在长时运行的任务之间来回切换、边跑边看，扫读性和状态可辨识度高于视觉表现力。
5. **术语不迁就，措辞要人话。** 读者懂 DAG 和 LoRA，不需要科普；但错误信息和引导文案必须是人话，不是把后端异常原样抛出来。

## Accessibility & Inclusion

- **已建立的基线（上一轮改造实测校准，属于必须维持的约束）**【证据】：全站正文级文字对比度 ≥ 4.5:1（按最不利底色 `--bg-tertiary` 校准，非页面底色）；最小字号 12.2px（`--fs-xs`）；全局 `:focus-visible` 焦点环，不允许裸写 `outline:none`；所有图标控件有可访问名称；模态框有 `role="dialog"` / `aria-modal` / `aria-labelledby` + 焦点保存恢复 + Tab 捕获；导航有 `aria-current`；侧栏支持方向键/Home/End；Toast `role="status"`；加载态 `role="status" aria-busy`；跳过导航链接。
- **未确立的**：用户未提出具体无障碍标准（如必须达到 WCAG 2.1 AA 认证）。上述基线是工程自觉，不是外部合规要求。【推断】
- 键盘优先是既定原则（`Ctrl/⌘+K` 命令面板、`Esc` 关闭、回车提交），源自"操作效率至上"。【证据：DESIGN.md「Do's and Don'ts」+ 已实现的快捷键】
