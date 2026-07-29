/* ============================================
   Symbio UI — 能力账本页
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。

   这一页是产品定位本身（见 PRODUCT.md）：把「声称」变成运行时可核对的账本。
   所以两条硬约束：
     1. partial 必须如实显示为 partial，不粉饰成完成；
     2. evidence 路径必须能当场点开看代码，否则「可验证」只是口号。
   ============================================ */

// ============ Capabilities Page ============
async function loadCapabilities() {
  showLoading(dom.capabilityGrid, '正在读取能力账本…');
  try {
    const res = await fetch(`${API}/capabilities`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.capabilities = await res.json();
    renderCapabilities();
  } catch (e) {
    toast('error', '能力账本读取失败', e.message);
    if (dom.capabilityGrid) {
      dom.capabilityGrid.innerHTML = `
        <div class="empty-block is-error">
          <p class="empty-block-title">能力账本读取失败</p>
          <p class="empty-block-hint"><code>${esc(e.message)}</code> · 账本数据由后端 <code>src/symbio/capabilities.py</code> 提供，请确认服务仍在运行。</p>
          <div class="empty-block-actions">
            <button class="btn-outline" type="button" onclick="loadCapabilities()">重新读取</button>
          </div>
        </div>`;
    }
  }
}

// 后端 claim / next_step 是英文（供 API 消费者读取），展示层按 id 补中文。
// 缺失时回落到英文原文，不隐藏内容。
// 翻译原则：保留原文的限定语（"尚未验证"/"仅单机"/"stub"），不许在中译里悄悄变自信。
const CAPABILITY_ZH = {
  dynamic_dag: {
    claim: '动态 DAG 运行时：图状态持久化，支持中途重新规划',
    next: '继续扩充真实场景下的图变更用例，并在 UI 上展示执行轨迹证据。',
  },
  planner_reviewer_policy: {
    claim: '先规划、危险操作前先复核、完成前先验证',
    next: '把这套策略的执行证据展示到所有长任务视图中。',
  },
  hitl_im_approval: {
    claim: '人工审批多通道：Web、Webhook、QQ、企业微信、飞书、文本指令，并自动推送审批卡片到已登录的个人微信（支持重推）',
    next: '补充各通道的投递诊断信息，以及多审批人的交互设计。',
  },
  ontology_memory_graph: {
    claim: '本体记忆图谱：以符号推理提供零 Token 的检索面',
    next: '在本体界面上补充编辑操作与关系来源追溯。',
  },
  model_routing_config: {
    claim: '可配置模型池与「任务→模型」路由',
    next: '把路由决策的理由写入执行产物，让选型可复盘。',
  },
  token_cost_optimization: {
    claim: '分层 Token 成本控制：语义缓存、上下文剪枝、成本追踪、预算，均已接入对话运行时',
    next: '在仪表盘补充 prompt cache TTL 保活指标与按会话的路由决策产物。',
  },
  prompt_injection_defense: {
    claim: '三层提示注入防火墙（净化 / 语义检测 / 意图审计），在对话入口强制生效',
    next: '增加语义 ML 分类层与跨轮上下文检测，突破单条消息签名匹配的局限。',
  },
  skills_marketplace: {
    claim: 'Skill 浏览与搜索、安装时真实落盘、可从 GitHub 仓库导入真实 Agent Skills',
    next: '补充包签名、沙箱化执行、版本兼容检查，以及私有/需鉴权的注册源。',
  },
  mcp_gateway: {
    claim: '原生 MCP stdio JSON-RPC 工具桥接与配置自动发现',
    next: '完成长连接池、resource/prompt 协议与鉴权方案。',
  },
  external_agent_control: {
    claim: '接管外部 Agent：附着、实时同步（tail + resume）、编排（中继 / 平铺工作台），并可驱动完整交互式 PTY 终端（claude-code / codex / shell，经 winpty 支持真实 TUI）',
    next: '终端 WebSocket 默认仅限本机；远程放行路径需补 Token 鉴权，非终端（-p）调用路径需补按轮次的流式输出与取消。',
  },
  agent_external_backend: {
    claim: '单个 Symbio Agent 可以跑在 Claude Code / Codex CLI 后端之上',
    next: '把外部后端的 Agent 接入 DAG 节点调度，并流式透出其输出。',
  },
  wechat_bridge: {
    claim: '个人微信扫码登录机器人（内置 iLink）：会话持久化、双向对话、HITL 审批推送与路由',
    next: '在 iLink 路径上补充图片/语音/文件处理与群聊策略。',
  },
  sandbox_cluster: {
    claim: '工作区受限的本地沙箱 + 真实 Docker 容器隔离（断网、只读根、内存/CPU 限制、引擎预检、孤儿容器清理、不泄漏宿主环境变量）；K8s Pod 路径仍是占位实现',
    next: '把 k8s_sandbox.py 的占位实现换成真实 Pod 执行器（创建/监控/销毁），让沙箱审计记录跨重启保留，并在 Web UI 暴露 Docker 容器执行。',
  },
  observability_otel: {
    claim: 'OpenTelemetry 追踪、指标、Token 热力图与调用链可视化',
    next: '把 OTLP exporter 接入生产配置，并在 Grafana 面板补充按节点的延迟 SLO。',
  },
  data_flywheel: {
    claim: '轨迹采集、SOP 蒸馏、数据集导出，以及真实 LoRA SFT 训练后端（transformers+peft，产出真实 adapter 权重与 loss；依赖或 GPU 缺失时回落为 stub），已接入后台作业 API 与 Web 提交/监控界面',
    next: '补充完整的评测运行报告，并支持更大的基座模型与量化（QLoRA）训练。',
  },
  ray_actor_runtime: {
    claim: '真实 Ray Actor 池执行跨进程 SubAgent：提交/收集/取消/关闭，Agent 按名字在 worker 内重建（不序列化客户端），通过配置开关接入 SubAgentManager；Ray 关闭或不可用时回落 asyncio。已在真实本地 Ray 集群验证（任务确实运行在不同 worker 进程），多机集群部署尚未验证',
    next: '验证真实多机 Ray 集群部署，并把轮询调度升级为按 Actor 负载感知的调度。',
  },
  a2a_protocol: {
    claim: 'Agent-to-Agent 协议：动态 AgentCard、入站任务走完整 Orchestrator 管线、出站会话以轮询拉回结果、已验证真实 HTTP 上的双进程跨实例往返、SSE 流式任务更新、Webhook 推送通知，以及可选 Bearer Token 鉴权',
    next: '把 Bearer 鉴权升级为完整 OAuth 流程，增加非文本 artifact 部件，并验证跨机器（而非仅跨进程）部署。',
  },
  computer_use_loop: {
    claim: 'Computer Use 循环 + VLM 视觉规划：当前截图像素交给 Claude 视觉模型，返回像素坐标级 GUI 动作（点击 x/y、输入）；三级回落（视觉 → 文本 LLM → 启发式）保证循环不断；完整的截图/动作/回放审计。真实 GUI 任务成功率取决于模型',
    next: '增加相对截图的坐标定位辅助（元素框），加固多标签页/会话生命周期，并在每个动作后增加自校验步骤。',
  },
  multimodal_vision: {
    claim: '多模态记忆：经 Claude 视觉实现真实图像理解并接入摄取管线（图像描述可被检索），自动摄取对话附带的图片/PDF，另有 PDF 与代码结构抽取',
    next: '让缓存的图像描述跨重启保留，并为文字密集图像增加 OCR。',
  },
  federated_privacy: {
    claim: '联邦 LoRA 学习 + FedAvg 聚合（客户端在本地数据上训练 adapter，数据不出本地，仅按样本数加权平均权重），叠加差分隐私（L2 裁剪 + 高斯噪声，DP-SGD 风格），带 (epsilon, delta) 预算记账与审计记录，预算耗尽后拒绝后续轮次。已在单机多客户端目录端到端验证；预算组合使用基础线性定理而非 RDP，跨机安全传输、梯度泄漏防御、拜占庭鲁棒聚合均未实现',
    next: '补充跨机安全权重传输、梯度泄漏防御、拜占庭鲁棒聚合，并把联邦轮次接入 API/UI。',
  },
  api_authentication: {
    claim: '可选的全局 Bearer Token 鉴权，覆盖所有 HTTP 路由与 WebSocket 端点。配置 Token 后，仅健康检查、A2A AgentCard 与 UI 静态资源保持公开；CLI 默认绑定 127.0.0.1，若绑定到所有网卡且未设 Token 会大声告警',
    next: '用按用户的独立账号、作用域令牌与轮换机制，替换当前单一共享令牌。',
  },
};

function capabilityClaimZh(item) {
  return CAPABILITY_ZH[item.id]?.claim || item.claim || item.id;
}
function capabilityNextZh(item) {
  return CAPABILITY_ZH[item.id]?.next || item.next_step || '';
}

function renderCapabilities() {
  const summary = state.capabilities.summary || {};
  const items = state.capabilities.items || [];
  if (dom.capabilitySummary) {
    dom.capabilitySummary.innerHTML = [
      capabilityStatCard('全部', summary.total || items.length || 0, 'all'),
      capabilityStatCard('已实现', summary.implemented || 0, 'implemented'),
      capabilityStatCard('部分实现', summary.partial || 0, 'partial'),
      capabilityStatCard('未实现', summary.missing || 0, 'missing'),
    ].join('');
  }

  // 快照时间：账本是"运行时"校验的，时间戳是它可信度的一部分
  const stampEl = document.getElementById('capability-generated-at');
  if (stampEl && state.capabilities.generated_at) {
    const d = new Date(state.capabilities.generated_at);
    stampEl.textContent = isNaN(d) ? '' : `账本快照时间 ${d.toLocaleString('zh-CN')}`;
  }

  const filtered = state.capabilityFilter === 'all'
    ? items
    : items.filter(item => item.status === state.capabilityFilter);

  if (!dom.capabilityGrid) return;
  if (filtered.length === 0) {
    const label = capabilityStatusLabel(state.capabilityFilter);
    dom.capabilityGrid.innerHTML = `
      <div class="empty-block">
        <p class="empty-block-title">没有「${esc(label)}」状态的能力</p>
        <p class="empty-block-hint">这是好消息：说明账本里没有该状态的条目。切换上方筛选可查看其他状态。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="setCapabilityFilter('all')">查看全部 ${esc(String(items.length))} 项</button>
        </div>
      </div>`;
    return;
  }

  dom.capabilityGrid.innerHTML = filtered.map(item => {
    const evidence = item.evidence || [];
    const docs = item.docs || [];
    return `
    <article class="capability-card capability-${esc(item.status)}">
      <div class="capability-card-head">
        <div>
          <div class="capability-module">${esc(item.module || '模块未标注')}</div>
          <h2>${esc(capabilityClaimZh(item))}</h2>
        </div>
        <span class="capability-status status-${esc(item.status)}">${esc(capabilityStatusLabel(item.status))}</span>
      </div>
      ${capabilityNextZh(item) ? `<div class="capability-next"><span class="capability-next-label">${item.status === 'implemented' ? '下一步' : '缺口'}</span>${esc(capabilityNextZh(item))}</div>` : ''}
      <div class="capability-meta-block">
        <div class="capability-meta-title">代码证据${evidence.length ? `（${evidence.length}）` : ''}</div>
        <div class="capability-chip-row">
          ${evidence.length
            ? evidence.map(path => `<button type="button" class="capability-chip is-link" onclick="viewSourceFile('${escJs(path)}')" title="点击查看 ${esc(path)}">${esc(path)}</button>`).join('')
            : '<span class="capability-chip muted">暂无代码证据</span>'}
        </div>
      </div>
      ${docs.length ? `
      <div class="capability-meta-block">
        <div class="capability-meta-title">相关文档（${docs.length}）</div>
        <div class="capability-chip-row">
          ${docs.map(path => `<button type="button" class="capability-chip doc is-link" onclick="viewSourceFile('${escJs(path)}')" title="点击查看 ${esc(path)}">${esc(path)}</button>`).join('')}
        </div>
      </div>` : ''}
    </article>
  `;
  }).join('');
}

function capabilityStatCard(label, value, status) {
  const isActive = state.capabilityFilter === status;
  return `
    <button class="capability-stat capability-stat-${status} ${isActive ? 'active' : ''}"
            data-status="${status}" aria-pressed="${isActive}"
            title="${status === 'all' ? '显示全部能力' : `只看「${label}」的能力`}">
      <span>${esc(label)}</span>
      <strong>${formatNumber(value)}</strong>
    </button>
  `;
}

function capabilityStatusLabel(status) {
  const map = {
    all: '全部',
    implemented: '已实现',
    partial: '部分实现',
    missing: '未实现',
  };
  return map[status] || status || '状态未知';
}

function setCapabilityFilter(status) {
  state.capabilityFilter = status || 'all';
  if (dom.capabilityFilter) dom.capabilityFilter.value = state.capabilityFilter;
  renderCapabilities();
}

// 让证据可核对：直接把仓库里的源码读出来看，而不是让用户手抄路径去编辑器翻。
async function viewSourceFile(path) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal source-modal" role="dialog" aria-modal="true" aria-label="查看源码 ${esc(path)}">
      <div class="modal-head">
        <h2 class="source-modal-path">${esc(path)}</h2>
        <button class="modal-close" type="button" aria-label="关闭">&times;</button>
      </div>
      <div class="modal-body source-modal-body">
        <div class="loading-state" role="status" aria-live="polite"><div class="loading-spinner" aria-hidden="true"></div><p>正在读取源码…</p></div>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.querySelector('.modal-close')?.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  const onKey = (e) => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); } };
  document.addEventListener('keydown', onKey);

  const body = overlay.querySelector('.source-modal-body');
  try {
    const res = await fetch(`${API}/source?path=${encodeURIComponent(path)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    body.innerHTML = `
      <div class="source-modal-meta">${formatNumber(data.lines || 0)} 行 · ${formatFileSize(data.size || 0)}</div>
      <pre class="source-modal-code"><code>${esc(data.content || '')}</code></pre>`;
  } catch (e) {
    body.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法读取该文件</p>
        <p class="empty-block-hint"><code>${esc(e.message)}</code></p>
      </div>`;
  }
}

dom.capabilityFilter?.addEventListener('change', (e) => {
  setCapabilityFilter(e.target.value);
});
dom.capabilitySummary?.addEventListener('click', (e) => {
  const card = e.target.closest('.capability-stat');
  if (!card) return;
  setCapabilityFilter(card.dataset.status || 'all');
});
$('#btn-refresh-capabilities')?.addEventListener('click', loadCapabilities);
