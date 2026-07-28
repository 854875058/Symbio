/* ============================================
   Symbio UI — 进化页：LoRA 微调、数据飞轮、评测集、对话导出
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Evolution Page ============
async function loadEvolution() {
  await Promise.all([
    loadFlywheel(),
    previewConversationExport(),
    loadEvaluationSuites(),
    loadFinetuneDatasets(),
    loadFinetuneJobs(),
  ]);
}

// ============ LoRA 微调训练（真训练后端）============
state.finetune = { pollTimer: null };

async function loadFinetuneDatasets() {
  const sel = $('#ft-dataset');
  if (!sel) return;
  try {
    const res = await fetch(`${API}/flywheel/datasets`);
    const data = await res.json();
    const items = data.datasets || [];
    sel.innerHTML = items.length
      ? items.map(d => `<option value="${esc(d.path)}">${esc(d.name)} · ${d.samples} 样本 · ${d.size_kb}KB</option>`).join('')
      : `<option value="">（先在上方"数据集导出"写出 JSONL）</option>`;
  } catch (e) {
    sel.innerHTML = `<option value="">加载数据集失败</option>`;
  }
}

async function startFinetune() {
  const dataset = $('#ft-dataset')?.value || '';
  if (!dataset) { toast('error', '无法开始', '请先选择一个数据集（在上方导出）'); return; }
  const payload = {
    dataset_path: dataset,
    model_name: ($('#ft-model')?.value || 'sshleifer/tiny-gpt2').trim(),
    epochs: parseInt($('#ft-epochs')?.value || '1', 10),
    lora_rank: parseInt($('#ft-rank')?.value || '8', 10),
  };
  // 训练是本页唯一会长时间占用机器、且没有取消接口的动作：一旦提交只能等它跑完。
  // 参数（模型、轮数、rank）直接决定耗时，提交前必须复述一遍让用户核对。
  if (!confirmDanger('开始 LoRA 微调训练？', `模型：${payload.model_name}\n数据集：${dataset}\n轮数：${payload.epochs} · LoRA rank：${payload.lora_rank}\n\n训练会在后台占用较多 CPU/GPU 与内存，视模型和数据量可能持续很久，目前没有中途取消的入口——提交后只能等它结束。若本机缺少 transformers / peft 依赖，会退化为 stub（模拟训练，不产生真实权重），作业卡片上会标明。`)) {
    return;
  }
  try {
    const res = await fetch(`${API}/flywheel/finetune`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', '训练已提交', `作业 ${data.job_id.slice(0,8)} 已在后台开始`);
    loadFinetuneJobs();
    startFinetunePolling();
  } catch (e) {
    toast('error', '提交训练失败', e.message);
  }
}

async function loadFinetuneJobs() {
  const box = $('#finetune-jobs');
  if (!box) return;
  try {
    const res = await fetch(`${API}/flywheel/finetune`);
    const data = await res.json();
    renderFinetuneJobs(data.jobs || []);
    // 有运行中的作业才继续轮询，全部结束就停
    const running = (data.jobs || []).some(j => ['pending','preparing','training','evaluating'].includes(j.status));
    if (running) startFinetunePolling(); else stopFinetunePolling();
  } catch (e) { /* 静默 */ }
}

function renderFinetuneJobs(jobs) {
  const box = $('#finetune-jobs');
  if (!box) return;
  if (!jobs.length) {
    box.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">还没有训练作业</p>
        <p class="empty-block-hint">微调是把积累的对话数据回灌成模型能力：先在上方导出数据集，再选中它开始训练。这是「越用越强」里唯一真正改动模型权重的环节。</p>
      </div>`;
    return;
  }
  const statusLabel = { pending:'排队', preparing:'准备', training:'训练中', evaluating:'评估', completed:'完成', failed:'失败', cancelled:'已取消' };
  box.innerHTML = jobs.map(j => {
    const pct = Math.round((j.progress_ratio || 0) * 100);
    const loss = j.final_loss != null ? Number(j.final_loss).toFixed(4) : '—';
    const params = j.trainable_params ? `${formatNumber(j.trainable_params)}/${formatNumber(j.total_params)} 参数` : '';
    // stub 必须自我解释：一次「完成」的模拟训练若被误当成真微调，
    // 用户会以为模型已经变强了。徽标带 title，卡片下方再补一行明文。
    const backend = j.backend === 'stub'
      ? '<span class="ft-badge-stub" title="模拟训练：本机缺少 transformers / peft 依赖，未产生真实权重">stub（模拟）</span>'
      : (j.backend === 'lora' ? '<span class="ft-badge-lora" title="真实 LoRA 训练，已产生适配器权重">真 LoRA</span>' : '');
    return `
    <div class="ft-job ft-job-${esc(j.status)}">
      <div class="ft-job-head">
        <span class="ft-job-model">${esc(j.model_name)} ${backend}</span>
        <span class="ft-job-status">${statusLabel[j.status] || j.status}</span>
      </div>
      <div class="ft-job-bar"><div class="ft-job-bar-fill" style="width:${pct}%"></div></div>
      <div class="ft-job-meta">
        <span>step ${j.metrics?.length || 0} · loss ${loss}</span>
        <span>${params}</span>
        ${j.error_message ? `<span class="ft-job-err">${esc(j.error_message)}</span>` : ''}
      </div>
    </div>`;
  }).join('');
}

function startFinetunePolling() {
  if (state.finetune.pollTimer) return;
  state.finetune.pollTimer = setInterval(() => {
    const page = document.getElementById('page-evolution');
    if (page && !page.classList.contains('active')) { stopFinetunePolling(); return; }
    loadFinetuneJobs();
  }, 2000);
}
function stopFinetunePolling() {
  if (state.finetune.pollTimer) { clearInterval(state.finetune.pollTimer); state.finetune.pollTimer = null; }
}

$('#btn-start-finetune')?.addEventListener('click', startFinetune);
$('#btn-refresh-finetune')?.addEventListener('click', () => { loadFinetuneDatasets(); loadFinetuneJobs(); });

// ============ Data Flywheel (4-stage closed loop) ============
const FAILURE_CATEGORIES = ['logic_error','timeout','resource','external_api','input_invalid','permission','model_error','tool_error','context_overflow','unknown'];

async function loadFlywheel() {
  await Promise.all([loadFlywheelOverview(), loadFlywheelFailures(), loadFlywheelSops()]);
}

async function loadFlywheelOverview() {
  try {
    const res = await fetch(`${API}/flywheel/overview`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const st = data.stages || {};
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const cap = st.capture || {};
    setText('fw-capture-metric', cap.available ? `${formatNumber(cap.written || 0)} 已捕获` : '就绪');
    const an = st.analysis || {};
    setText('fw-analysis-metric', `${an.total_failures || 0} 失败 / ${an.total_root_causes || 0} 根因`);
    const di = st.distillation || {};
    setText('fw-sop-metric', `${(di.seed_count || 0) + (di.distilled_count || 0)} SOP`);
    const fb = st.feedback || {};
    setText('fw-feedback-metric', fb.average_rating ? `评分 ${Number(fb.average_rating).toFixed(1)}` : '评分 —');
  } catch (e) {
    console.warn('加载飞轮总览失败:', e.message);
  }
}

async function loadFlywheelFailures() {
  const container = document.getElementById('flywheel-failures');
  if (!container) return;
  try {
    const res = await fetch(`${API}/flywheel/failures?limit=20`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const failures = data.failures || [];
    const causes = data.root_causes || [];
    if (!failures.length && !causes.length) {
      container.innerHTML = `
        <div class="empty-block is-inline">
          <p class="empty-block-title">暂无失败记录</p>
          <p class="empty-block-hint">这里收集 Agent 执行失败的样本，并归纳出反复出现的根因——它是「越用越强」的输入端：先知道栽在哪，才知道该改什么。任务跑失败后会自动进来，不需要你录入。</p>
        </div>`;
      return;
    }
    // 严重度既要有颜色，也要有文字标签——不能只靠颜色区分（色觉障碍读不出来）。
    // 底色用 color-mix 而不是 `var(--x)22`：把 alpha 十六进制拼在 var() 后面
    // 得到的是 `var(--teal)22` 这种非法值，整条声明被丢弃，徽标其实一直没有底色。
    const sevColor = { low: 'var(--teal)', medium: 'var(--amber)', high: 'var(--accent-text)', critical: 'var(--red)' };
    const sevLabel = { low: '轻微', medium: '中等', high: '严重', critical: '致命' };
    let html = '';
    if (causes.length) {
      html += '<div class="flywheel-subhead">根因 (Root Cause)</div>';
      html += causes.slice(0, 5).map(c => `<div class="flywheel-cause"><span class="flywheel-cat">${esc(c.category || '')}</span><span class="flywheel-cause-text">${esc(c.cause_summary || '')}</span><span class="flywheel-occ">×${c.occurrence_count || 1}</span></div>`).join('');
    }
    html += '<div class="flywheel-subhead">最近失败</div>';
    html += failures.slice(0, 8).map(f => {
      const color = sevColor[f.severity] || 'var(--text-secondary)';
      const label = sevLabel[f.severity] || f.severity || '未分级';
      return `<div class="flywheel-failure"><span class="security-badge" style="background:color-mix(in srgb, ${color} 14%, transparent);color:${color}">${esc(label)}</span><span class="flywheel-cat">${esc(f.category || '')}</span><span class="flywheel-failure-desc" title="${esc(f.description || '')}">${esc(f.description || f.error_message || '—')}</span></div>`;
    }).join('');
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `
      <div class="empty-block is-inline is-error">
        <p class="empty-block-title">无法加载失效分析</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadFlywheelFailures()">重试</button>
        </div>
      </div>`;
  }
}

async function loadFlywheelSops() {
  const container = document.getElementById('flywheel-sops');
  if (!container) return;
  try {
    const res = await fetch(`${API}/flywheel/sops`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const all = [...(data.seeds || []), ...(data.distilled || [])];
    if (!all.length) {
      container.innerHTML = `
        <div class="empty-block is-inline">
          <p class="empty-block-title">还没有可用的 SOP</p>
          <p class="empty-block-hint">SOP 是从成功的执行轨迹里蒸馏出来的固定步骤，下次遇到同类任务可以直接照着走，不用重新摸索。种子 SOP 需要人工写入，蒸馏 SOP 要先积累足够多的成功轨迹才会自动生成——所以刚部署时这里是空的属于正常。</p>
        </div>`;
      return;
    }
    container.innerHTML = all.map(s => {
      const isSeed = s.source === 'seed';
      const steps = Array.isArray(s.steps) ? s.steps.length : (s.steps || 0);
      return `<div class="flywheel-sop">
        <div class="flywheel-sop-head">
          <span class="flywheel-sop-name">${esc(s.name || s.task_type || 'SOP')}</span>
          <span class="flywheel-sop-tag ${isSeed ? 'seed' : 'distilled'}">${isSeed ? '种子' : '蒸馏'}</span>
        </div>
        <div class="flywheel-sop-desc">${esc((s.description || '').substring(0, 80))}</div>
        <div class="flywheel-sop-meta">${steps} 步 · 成功率 ${Math.round((s.success_rate || 0) * 100)}% · ${formatNumber(s.avg_tokens || 0)} tokens</div>
      </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = `
      <div class="empty-block is-inline is-error">
        <p class="empty-block-title">SOP 列表加载失败</p>
        <p class="empty-block-hint">${esc(e.message)} · 这不代表没有 SOP，只代表这次没读到。刷新页面重试；若持续失败，检查服务端 flywheel 模块是否可用。</p>
      </div>`;
  }
}

$('#btn-record-failure-demo')?.addEventListener('click', async () => {
  // 这个按钮写的是假数据，但落进的是真库。不确认就点，会把虚构的失败样本
  // 混进根因归纳里——之后页面上的「反复出现的根因」就不可信了，
  // 而且没有一键清理的入口。所以必须先说清它写的是什么、后果是什么。
  if (!confirmDanger('写入一条演示失败记录？', '这会向真实的失效分析库写入一条虚构的失败样本（内容随机，task_id 以 demo- 开头），仅用于预览本模块的展示效果。\n\n它会参与根因归纳，让统计和「反复出现的根因」掺入假数据，目前没有一键清理的入口。生产环境不要用。')) {
    return;
  }
  const cat = FAILURE_CATEGORIES[Math.floor(Math.random() * 4)];
  const demos = {
    timeout: { description: '【演示数据】工具调用超过 30s 未返回，任务被强制中断', severity: 'high' },
    logic_error: { description: '【演示数据】Agent 误判任务已完成，跳过了验证步骤', severity: 'medium' },
    tool_error: { description: '【演示数据】调用文件写入工具时权限不足，返回 EACCES', severity: 'medium' },
    external_api: { description: '【演示数据】外部 API 返回 429，重试 3 次后仍失败', severity: 'high' },
  };
  const d = demos[cat] || demos.timeout;
  try {
    const res = await fetch(`${API}/flywheel/failures`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: `demo-${Date.now()}`, category: cat, severity: d.severity, description: d.description, steps_to_failure: 2 + Math.floor(Math.random() * 5) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', '已写入演示数据', '这是虚构样本，描述里带「演示数据」标记');
    await loadFlywheelOverview();
    await loadFlywheelFailures();
  } catch (e) {
    toast('error', '记录失败', e.message);
  }
});

async function previewConversationExport() {
  return runConversationExport(true);
}

async function writeConversationExport() {
  return runConversationExport(false);
}

async function runConversationExport(preview = true) {
  if (dom.exportPreview) {
    dom.exportPreview.textContent = preview ? '正在生成预览...' : '正在写入 JSONL...';
  }
  try {
    const format = dom.exportFormat?.value || 'sharegpt';
    const res = await fetch(`${API}/export/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format, preview }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.evolution.export = data;
    renderConversationExport(data);
    if (!preview && data.written) {
      toast('success', '数据集已导出', data.output_path || 'JSONL 已写入');
    }
    return data;
  } catch (e) {
    toast('error', '导出失败', e.message);
    if (dom.exportPreview) dom.exportPreview.textContent = e.message;
    return null;
  }
}

function renderConversationExport(data) {
  if (dom.exportMeta) {
    dom.exportMeta.innerHTML = `
      <span>格式：<strong>${esc(data.format || '')}</strong></span>
      <span>样本数：<strong>${formatNumber(data.sample_count || 0)}</strong></span>
      <span>是否落盘：<strong>${data.written ? '已写入' : '仅预览'}</strong></span>
      ${data.output_path ? `<span>路径：<strong>${esc(data.output_path)}</strong></span>` : ''}
    `;
  }
  if (dom.exportPreview) {
    const samples = data.samples || [];
    dom.exportPreview.textContent = samples.length
      ? JSON.stringify(samples, null, 2)
      : '还没有可导出的对话。导出的是你和 Agent 的真实对话，攒够了才有微调价值——先去「对话」页多聊几轮。';
  }
}

async function loadEvaluationSuites() {
  if (dom.evalSuiteGrid) {
    showLoading(dom.evalSuiteGrid, '加载评测套件...');
  }
  try {
    const path = dom.evalSuitePath?.value || 'data/eval_suites';
    const res = await fetch(`${API}/evaluation/suites?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.evolution.suites = data.suites || [];
    renderEvaluationSuites(data);
    return data;
  } catch (e) {
    toast('error', '加载评测套件失败', e.message);
    if (dom.evalSuiteGrid) {
      dom.evalSuiteGrid.innerHTML = `
        <div class="empty-block is-inline is-error">
          <p class="empty-block-title">无法加载评测套件</p>
          <p class="empty-block-hint">${esc(e.message)}</p>
          <div class="empty-block-actions">
            <button class="btn-outline" type="button" onclick="loadEvaluationSuites()">重试</button>
          </div>
        </div>`;
    }
    return null;
  }
}

function renderEvaluationSuites(data) {
  const suites = data.suites || [];
  const errors = data.errors || [];
  if (!dom.evalSuiteGrid) return;
  if (!suites.length && !errors.length) {
    dom.evalSuiteGrid.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">还没有评测套件</p>
        <p class="empty-block-hint">评测套件是一组固定的题目和期望答案，用来量化「它有没有变强」。没有套件，能力变化只能靠感觉判断。在上方路径（默认 <code>data/eval_suites</code>）下放入 YAML 套件文件即可。</p>
      </div>`;
    return;
  }
  dom.evalSuiteGrid.innerHTML = [
    ...suites.map(suite => `
      <article class="evolution-suite-card">
        <div class="evolution-suite-title">${esc(suite.name)}</div>
        <div class="evolution-suite-desc">${esc(suite.description || '（无描述）')}</div>
        <div class="evolution-suite-meta">
          <span>v${esc(suite.version || '1.0.0')}</span>
          <span>${formatNumber(suite.case_count || 0)} 个用例</span>
        </div>
        <div class="evolution-suite-path">${esc(suite.path || '')}</div>
      </article>
    `),
    ...errors.map(error => `
      <article class="evolution-suite-card error">
        <div class="evolution-suite-title">解析失败</div>
        <div class="evolution-suite-desc">${esc(error.error || '')}</div>
        <div class="evolution-suite-path">${esc(error.path || '')}</div>
      </article>
    `),
  ].join('');
}

$('#btn-refresh-evolution')?.addEventListener('click', loadEvolution);
$('#btn-export-preview')?.addEventListener('click', previewConversationExport);
$('#btn-export-write')?.addEventListener('click', writeConversationExport);
$('#btn-load-eval-suites')?.addEventListener('click', loadEvaluationSuites);
dom.exportFormat?.addEventListener('change', previewConversationExport);
