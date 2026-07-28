---
name: Symbio
description: AI Infra 级多 Agent 协同框架的操作台——可核对的台账，不是仪表盘玩具
colors:
  accent-ember: "#e07a5a"
  accent-ember-hover: "#ea8b6b"
  accent-ember-text: "#ef9276"
  accent-ember-light: "#b0512f"
  accent-ember-light-hover: "#9c4326"
  accent-ember-light-text: "#a8492a"
  on-accent-dark: "#1f1409"
  on-accent-light: "#ffffff"
  void-dark: "#16150f"
  surface-dark: "#1a1a17"
  surface-raised-dark: "#21201c"
  surface-sunken-dark: "#2a2824"
  chrome-dark: "#131310"
  ink-dark: "#edeae3"
  ink-muted-dark: "#b0a99c"
  ink-faint-dark: "#9c9588"
  ink-decor-dark: "#6b665c"
  void-light: "#faf9f5"
  surface-light: "#ffffff"
  surface-raised-light: "#f4f2ec"
  surface-sunken-light: "#ebe7dd"
  chrome-light: "#f6f4ee"
  ink-light: "#2a2622"
  ink-muted-light: "#5c564d"
  ink-faint-light: "#6b645a"
  ink-decor-light: "#a09889"
  signal-pass: "#6cbf87"
  signal-pass-light: "#376e47"
  signal-fail: "#e8756b"
  signal-fail-light: "#a83c32"
  signal-hold: "#d9a441"
  signal-hold-light: "#82591c"
  signal-info: "#5cb5ad"
  signal-info-light: "#256b65"
  signal-trace: "#b294c9"
  signal-trace-light: "#6d4682"
typography:
  headline:
    fontFamily: "Segoe UI Variable Text, Segoe UI, -apple-system, BlinkMacSystemFont, Microsoft YaHei, PingFang SC, Hiragino Sans GB, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Segoe UI Variable Text, Segoe UI, -apple-system, BlinkMacSystemFont, Microsoft YaHei, PingFang SC, Hiragino Sans GB, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
  body:
    fontFamily: "Segoe UI Variable Text, Segoe UI, -apple-system, BlinkMacSystemFont, Microsoft YaHei, PingFang SC, Hiragino Sans GB, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  label:
    fontFamily: "Segoe UI Variable Text, Segoe UI, -apple-system, BlinkMacSystemFont, Microsoft YaHei, PingFang SC, Hiragino Sans GB, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.05em"
  data:
    fontFamily: "Cascadia Mono, Cascadia Code, Consolas, SF Mono, Menlo, monospace"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "0.03em"
rounded:
  # xs 专给"细条"用：进度条填充、迷你占比条、2–6px 高的元素。
  # 这类元素上 6px 圆角会把两端啃掉一大截，看起来像胶囊而不是条。
  xs: "3px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"   # 只给模态框：它是浮在页面之上的独立面板，比卡片更圆才不像"卡片放大版"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent-ember}"
    textColor: "{colors.on-accent-dark}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    typography: "{typography.body}"
  button-primary-hover:
    backgroundColor: "{colors.accent-ember-hover}"
    textColor: "{colors.on-accent-dark}"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted-dark}"
    rounded: "{rounded.md}"
    padding: "6px 14px"
  button-outline-hover:
    textColor: "{colors.accent-ember-text}"
  card:
    backgroundColor: "{colors.surface-raised-dark}"
    textColor: "{colors.ink-dark}"
    rounded: "{rounded.lg}"
    padding: "16px"
  card-hover:
    backgroundColor: "{colors.surface-sunken-dark}"
  input:
    backgroundColor: "{colors.surface-sunken-dark}"
    textColor: "{colors.ink-dark}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  status-chip:
    rounded: "{rounded.full}"
    padding: "2px 8px"
    typography: "{typography.data}"
---

# Design System: Symbio

> 本文件记录**既有**视觉系统（`document` 模式扫描 `web/style.css` 5449 行 + `web/index.html` + `web/js/*.js` 得出），不是替换方案。
> frontmatter 是规范层；正文解释如何应用。
> 定性描述（北极星、色彩性格、组件哲学）按用户授权由我拟定，未经用户逐条确认——但视觉方向本身（冷静专业型、单一橙棕强调色、1px 实线边框、无发光渐变、双主题跟随系统）是用户明确选定的绑定约束，不可改动。

## Overview

**Creative North Star: "运维台账"（The Operator's Ledger）**

Symbio 的界面是一本可以核对的台账，不是一块给人看的仪表盘。这个隐喻不是修辞——产品的立身机制就是运行时能力账本：21 条能力主张，每条都带证据文件路径和缺口说明，测试会在证据失效时报错。界面继承同一种诚实：每个数字有出处，每个状态有依据，`partial` 就显示成 `partial`。台账的美感来自准确和整齐，不来自装饰。

密度偏高，但不是把信息挤在一起，而是让一屏之内能读到足够多的可核对事实。用户在长时运行的任务之间来回巡检——这决定了扫读性和状态可辨识度优先于视觉表现力。层级靠背景阶梯和 1px 实线边框表达，不靠发光、投影堆叠或渐变。强调色只有一个，稀有即有力。

明确拒绝的（用户确认的反向参考）：赛博朋克霓虹、发光边框、光球背景、渐变文字、多强调色并置、深色卡片上叠半透明玻璃拟态。这些不只是审美选择——渐变文字压低对比度且无法校准，半透明层让对比度不可预测，都与"实测达标"的工程要求冲突。

**Key Characteristics:**
- 暖中性底色，不带一丝紫蓝倾向（`#16150f` 而非 `#0f1117`）
- 单一橙棕强调色贯穿深浅双主题
- 1px 实线边框三档权重，无投影层级
- 阴影只有两级，且只给浮层用
- 全部正文级文字对比度实测 ≥ 4.5:1，按最不利底色校准
- 等宽字体承载所有"可核对的量"

## Colors

暖中性灰阶配单一橙棕，像旧纸和陶土——温度来自色相偏暖，不来自饱和度。

### Primary

- **Ember（余烬橙棕）** `#e07a5a` 深色 / `#b0512f` 浅色：唯一强调色。用于主按钮填充、当前导航项、运行中状态、聚焦环、logo 标记。深浅两主题各有独立值——不是同一个色号换透明度，而是分别按对比度校准过：浅色主题的填充色压深到 `#b0512f` 才能让白字达到 5.17:1（原 `#c96442` 只有 3.9:1）。
- **Ember Text（文字专用）** `#ef9276` 深色 / `#a8492a` 浅色：强调色作**文字**时必须换用这一档。深色主题下 `--accent` 当文字只有 6.2:1，小字号时视觉偏弱，提亮到 7.5:1；浅色主题下反向压深到 4.66:1（按 `--bg-tertiary` 底色算）。这一对是这套系统里最容易被误用的 token。
- **On-Accent（强调底上的字）** `#1f1409` 深色 / `#ffffff` 浅色：只出现在 Ember 填充之上。

### Neutral

**深色主题**
- **Void（幽底）** `#16150f`：页面最底层，比任何卡面都深。
- **Surface（面）** `#1a1a17` → **Raised（卡面）** `#21201c` → **Sunken（凹面）** `#2a2824`：三级背景阶梯，层级的主要表达手段。输入框和 hover 态用凹面。
- **Chrome（框架）** `#131310`：侧栏专用，比页面底色更深，让导航从内容里退后。
- **Ink** `#edeae3`（约 14:1）→ **Ink Muted** `#b0a99c`（约 7:1）→ **Ink Faint** `#9c9588`（4.95:1 on 凹面 / 6.2:1 on 页底）：三级文字，全部达标。
- **Ink Decor** `#6b665c`（约 3.1:1）：**只允许用于纯装饰**，不承载任何信息。

**浅色主题**
- **Void** `#faf9f5` / **Surface** `#ffffff` / **Raised** `#f4f2ec` / **Sunken** `#ebe7dd` / **Chrome** `#f6f4ee`：暖米白纸感，不用纯灰。
- **Ink** `#2a2622`（13.4:1）→ **Muted** `#5c564d`（6.3:1）→ **Faint** `#6b645a`（4.7:1）→ **Decor** `#a09889`（约 2.6:1，仅装饰）。

### Tertiary（语义信号色）

五个信号色，同一饱和度基准，深色下统一提亮、浅色下统一压深。每个都配一个 `-dim`（14% 深色 / 10-12% 浅色）作填充底：
- **Pass** `#6cbf87` / `#376e47`：完成、通过、健康。
- **Fail** `#e8756b` / `#a83c32`：失败、拦截、危险操作。
- **Hold** `#d9a441` / `#82591c`：取消、等待、降级、`partial` 状态。
- **Info** `#5cb5ad` / `#256b65`：中性提示、次要指标。
- **Trace** `#b294c9` / `#6d4682`：轨迹、trace、图谱关系。

### Named Rules

**一色定音（The One Voice Rule）。** 全站只有一个强调色。任何"再加一个主色区分模块"的提议都是错的——模块用位置和标题区分，不用颜色。Ember 在任意一屏上的覆盖面不超过 10%，稀有是它有力的原因。

**文字换档（The Text-Tier Rule）。** 强调色作填充用 `--accent`，作文字必须用 `--accent-text`。这两个值不可互换，混用会直接掉到对比度线下。

**装饰灰不载信息（The Decor-Never-Informs Rule）。** `--text-muted` 约 3:1，只能用于分隔符、水印这类纯装饰。任何用户需要读到的内容——哪怕是"最不重要的元信息"——最低用 `--text-tertiary`。

**按最不利底色校准（The Worst-Surface Rule）。** 对比度一律按 `--bg-tertiary` 计算，不按页面底色。同一段文字会出现在页底、卡面、凹面三层背景上，只按页底算会在深一档的卡面上掉到 4.2:1。

## Typography

**Body / Display Font:** 系统原生 UI 字体——Segoe UI Variable Text（Win11 正文面）→ Segoe UI → -apple-system → Microsoft YaHei / PingFang SC / Hiragino Sans GB。
不加载 Web 字体：这一页所有三方资源都本地打包以支持内网/离线，再挂一张 CDN 字体表自相矛盾；而且 Inter 这类面已是 AI 生成界面的默认长相，没有辨识度。
**Data / Mono Font:** Cascadia Mono（回退 Cascadia Code → Consolas → SF Mono → Menlo），同样是系统自带。

**Character:** 中性、工程感、无个性——这是刻意的。台账的字体不该有观点，它的工作是让数字对齐、让状态可辨。中文回退链完整到 Windows/macOS/Linux 三平台，Web 字体是渐进增强：`media="print" onload="this.media='all'"` 异步加载，离线或内网环境直接落到系统字体，不阻塞首屏。

根字号是绝对值 `15px`（不是 `1rem`——写 `var(--fs-md)` 会自引用并退回浏览器默认 16px，这个坑踩过）。

### Hierarchy

- **Headline**（600, 1.75rem = 26.25px, 1.3）：页面主标题，每页仅一处。
- **Title**（600, 1.125rem = 16.9px, 1.4）：区块标题、卡片标题、模态框标题。
- **Body**（400, 1rem = 15px, 1.6）：正文与主要内容。
- **Label**（600, 0.8125rem = 12.2px, letter-spacing 0.05em, 大写）：表单标签、区块小标题。`text-transform: uppercase` 只对拉丁文有效，中文标签靠字重和字距区分。
- **Data**（500, 0.8125rem, letter-spacing 0.03em, 等宽）：ID、时间戳、状态码、Token 数、成本、版本号。

字号阶梯只有六档（12.2 / 13.1 / 15 / 16.9 / 20.6 / 26.25px），封在 `--fs-xs` 到 `--fs-2xl`。

### Named Rules

**12.2px 地板（The 12.2 Floor）。** `--fs-xs` = 0.8125rem 是绝对下限。此前散落的 `0.6rem` / `0.65rem` 在 15px 根字号下渲染成 8.7px，实际不可读。任何小于 `--fs-xs` 的字号都是 bug。

**可核对的量走等宽（The Ledger-Mono Rule）。** 凡是用户可能要比对、复制、核对的值——ID、时间、数量、成本、哈希、状态码——都用等宽体。散文用正文面。这条规则让"哪些是数据"在视觉上不需要解释。

## Layout

**外壳**：CSS Grid 两列——侧栏（展开 224px / 收起 58px）+ 主区。主区内部纵向三段：顶栏、页面内容区（唯一滚动容器）、状态栏。`body { overflow: hidden }`，全站不做整页滚动，滚动发生在内容区内部。这是操作台而非文档站的直接体现。

**页面内部**：统一 `page-header`（标题 + 可选操作条，`padding: 16px 24px 12px`，底部 1px 分隔）+ 内容区（`padding: 24px`）。卡片网格用 `repeat(auto-fill, minmax(300px, 1fr))`，`gap: 16px`，`align-content: start`。

**节奏**：4 / 8 / 16 / 24 / 32px。组件内部间距 4-8px，组件之间 16px，区块之间 24px。

**响应式**：侧栏在窄屏自动收起为图标条（此时分组标题隐藏并强制展开所有分组，因为图标条上没地方放分组标题）。命令面板触发器在 ≤860px 只留图标。

### Named Rules

**内容区自己滚（The Inner-Scroll Rule）。** 页面外壳固定，滚动条属于内容区。任何让整页滚动的布局都会把顶栏和状态栏推出视口，破坏"边跑边看"的巡检场景。

## Elevation & Depth

**这是一套色调分层系统，不是投影系统。** 深度靠三级背景阶梯（`surface` → `raised` → `sunken`）加 1px 边框表达。平面上的元素静止时完全没有阴影。

### Shadow Vocabulary

- **Ambient**（`0 1px 3px rgba(0,0,0,0.32)` 深 / `0 1px 3px rgba(80,60,40,0.08)` 浅）：轻浮层，如下拉、tooltip。
- **Overlay**（`0 8px 28px rgba(0,0,0,0.44)` 深 / `0 8px 28px rgba(80,60,40,0.12)` 浅）：模态框、命令面板、跳过导航链接。

### Named Rules

**阴影只给离开平面的东西（The Off-Plane-Only Rule）。** 卡片、面板、输入框、按钮——凡是留在页面平面上的，一律无阴影。只有真正浮在内容之上的（模态框、命令面板、浮动提示）才有。hover 态改变的是边框色和背景色，不是阴影。

**卡面不透明（The Opaque-Surface Rule）。** `--bg-glass` 这个名字是历史遗留，它的值现在是实色（`#21201c` / `#ffffff`）。半透明卡面让对比度不可预测、无法校准，已被移除。不要因为变量名叫 glass 就给它加 `backdrop-filter`。

## Shapes

圆角五档：6 / 8 / 12 / 16px + 全圆。小控件（按钮、输入框、选择器）用 6-8px，卡片和面板用 12px，模态框和大容器用 16px，状态胶囊和头像用全圆。

边框是这套系统的主力：`--border`（10% 深 / 13% 浅）日常勾边，`--border-hover`（18% / 24%）hover 提示，`--border-accent`（Ember 实色）表示选中或聚焦。全部 1px 实线——没有虚线、没有 2px 以上、没有渐变边框。

### Named Rules

**不用侧边色条（The No-Side-Tab Rule）。** 卡片一侧加 2-3px 彩色竖条是 AI 生成界面最容易辨认的特征。状态用胶囊标签或图标表达，不用色条。当前代码里还有 6 处遗留（`web/style.css` 1373-1375、3792、4880、5332），属于待清理项。

## Components

### Buttons

**性格：** 克制、明确、不装饰。

- **Shape:** 8px 圆角（`{rounded.md}`）
- **Primary:** Ember 实色填充 + `on-accent` 深字，`padding: 8px 16px`，1px 同色边框（让它在深浅主题下都有确定的轮廓）。hover 换 `accent-hover`，160ms。
- **Outline:** 透明底 + `--border` 边框 + `ink-muted` 文字，`padding: 6px 14px`。hover 时边框和文字同时转 `accent-text`——这是唯一允许"整体转强调色"的组件。
- **Icon:** 方形，仅图标，`--text-secondary`；hover 转 `--bg-hover` + `ink`。危险变体 hover 转 `signal-fail`。
- **Focus:** 全局 `:focus-visible` 2px Ember 实线 + 2px offset。组件内允许 `outline: none` 的**唯一**条件是配了 `box-shadow: 0 0 0 3px var(--accent-dim)` 自绘焦点环。

### Cards

**性格：** 台账的一行，不是展示位。

实色卡面 + 1px 边框 + 12px 圆角 + 16px 内距。hover 时边框转 `border-hover`、背景降到 `sunken`——没有位移、没有缩放、没有阴影。标题用 Title 档并 `text-overflow: ellipsis` 单行截断（`min-width: 0` 必须写，否则 flex 子项不会收缩）。

### Status Chips

**性格：** 一眼可辨的状态，不需要图例。

全圆胶囊，等宽字体，`padding: 2px 8px`，600 字重。配色是 `-dim` 底 + 同名实色字：completed→Pass，failed→Fail，cancelled→Hold，running→Ember（带 2s 透明度脉冲），pending→中性灰。

### Inputs

**性格：** 凹陷、安静、聚焦时才发声。

`sunken` 底 + 1px 边框 + 8px 圆角 + `8px 12px` 内距。聚焦时边框转 `accent-text` 并加 3px `accent-dim` 光环。placeholder 用 `ink-faint`。标签是 Label 档（12.2px / 600 / 大写 / 0.05em）。

**已知缺陷：** `select` 的下拉箭头是内联 SVG data-URI，描边色写死为 `#8888a0`（冷灰蓝）——既不跟随主题，也违背"暖中性、不带紫蓝倾向"的底色原则。见 `web/style.css:2486`，待修。

### Navigation

**性格：** 退后一步的框架，不争夺注意力。

侧栏用 `chrome` 底色（比页面更深/更浅），6 个可折叠分组，18 个页面项。当前项用 `bg-active`（Ember 14% / 10%）+ `accent-text` 文字 + `aria-current="page"`。分组折叠状态存 localStorage，但**当前页所在分组强制展开**——否则会出现"高亮项被折叠隐藏"的矛盾状态。收起态只剩 58px 图标条。

### Modal / Command Palette

**性格：** 短暂、聚焦、可键盘完成。

16px 圆角 + Overlay 级阴影 + 半透明遮罩。全部经由 `MutationObserver` 统一接管无障碍：自动补 `role="dialog"` / `aria-modal` / `aria-labelledby`，记住触发元素并把焦点移进弹窗，关闭时归还焦点，打开期间 Tab 圈在弹窗内。命令面板（`Ctrl/⌘+K`）是 combobox + listbox 结构，子序列模糊匹配，命中字符用 `<mark>` 高亮。

## Do's and Don'ts

**Do**
- 用背景阶梯和 1px 边框做层级。
- 可核对的量一律等宽体。
- 空状态解释"这是什么 / 为什么空 / 下一步点哪"——零数据是这个产品最高频的首次体验，不是边缘情况。
- 每个操作都给明确反馈；单人使用没有他人纠错，静默失败会被长期忽视。
- 对比度按 `--bg-tertiary` 校准后再提交。
- `partial` 能力就显示成 `partial`。UI 不得把缺口渲染成完成。

**Don't**
- 不加第二个强调色。
- 不给平面元素加阴影。
- 不用侧边彩色条表示状态。
- 不用渐变文字（压低对比度且无法校准）。
- 不给 `--bg-glass` 加 `backdrop-filter`——名字是历史遗留，值已是实色。
- 不写小于 12.2px 的字号。
- 不裸写 `outline: none`。
- 不用 `--text-muted` 承载任何用户需要读的信息。
- 不引 CDN、不引构建工具、不引前端框架——这是硬约束，任何设计方案必须能用原生 HTML/CSS/JS 实现。这条也管字体：不挂 Google Fonts，用系统原生 UI 字体（省一次请求，也省掉字体换入时的跳字）。
- 不用弹性/回弹缓动。真实物体平滑减速，用 `--ease-out`。

## 三条刻意的例外

检测器会报这三类，它们是有意为之，不要"顺手修掉"：

1. **`transition: width` / `height` 共 9 处。** 通常该避免（会触发重排），但这些地方动的宽度本身就是要表达的量：进度条填充、占比条、柱图长度、侧栏折叠、手风琴展开。改成 `transform: scaleX()` 会把两端圆角一起拉变形，读数也不再对得上刻度。数量固定在 9 处，新增前先想清楚动的是不是"值"。
2. **根元素 `font-size: 15px`。** 必须是绝对值——写 `var(--fs-md)`（=1rem）会自引用，退回浏览器默认 16px，整套字号阶梯会一起漂。
3. **`.form-hint-warn::before` 的 10px。** 那是 14px 圆圈里的一个 `!` 符号，是图标不是文字。12.2px 地板管的是"要读的字"。
