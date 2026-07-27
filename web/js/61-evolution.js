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
  if (!jobs.length) { box.innerHTML = `<div class="cost-table-empty">还没有训练作业</div>`; return; }
  const statusLabel = { pending:'排队', preparing:'准备', training:'训练中', evaluating:'评估', completed:'完成', failed:'失败', cancelled:'已取消' };
  box.innerHTML = jobs.map(j => {
    const pct = Math.round((j.progress_ratio || 0) * 100);
    const loss = j.final_loss != null ? Number(j.final_loss).toFixed(4) : '—';
    const params = j.trainable_params ? `${formatNumber(j.trainable_params)}/${formatNumber(j.total_params)} 参数` : '';
    const backend = j.backend === 'stub' ? '<span class="ft-badge-stub">stub</span>' : (j.backend === 'lora' ? '<span class="ft-badge-lora">真LoRA</span>' : '');
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
      container.innerHTML = '<div class="cost-table-empty">暂无失败记录 — 点击右上角"记录一条样例失败"体验闭环</div>';
      return;
    }
    const sevColor = { low: 'var(--teal)', medium: 'var(--amber)', high: 'var(--accent-text)', critical: 'var(--red)' };
    let html = '';
    if (causes.length) {
      html += '<div class="flywheel-subhead">根因 (Root Cause)</div>';
      html += causes.slice(0, 5).map(c => `<div class="flywheel-cause"><span class="flywheel-cat">${esc(c.category || '')}</span><span class="flywheel-cause-text">${esc(c.cause_summary || '')}</span><span class="flywheel-occ">×${c.occurrence_count || 1}</span></div>`).join('');
    }
    html += '<div class="flywheel-subhead">最近失败</div>';
    html += failures.slice(0, 8).map(f => {
      const color = sevColor[f.severity] || 'var(--text-secondary)';
      return `<div class="flywheel-failure"><span class="security-badge" style="background:${color}22;color:${color}">${esc(f.severity || '')}</span><span class="flywheel-cat">${esc(f.category || '')}</span><span class="flywheel-failure-desc" title="${esc(f.description || '')}">${esc(f.description || f.error_message || '—')}</span></div>`;
    }).join('');
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="cost-table-empty">加载失败分析出错: ${esc(e.message)}</div>`;
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
    if (!all.length) { container.innerHTML = '<div class="cost-table-empty">暂无 SOP</div>'; return; }
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
    container.innerHTML = `<div class="cost-table-empty">加载 SOP 出错: ${esc(e.message)}</div>`;
  }
}

$('#btn-record-failure-demo')?.addEventListener('click', async () => {
  const cat = FAILURE_CATEGORIES[Math.floor(Math.random() * 4)];
  const demos = {
    timeout: { description: '工具调用超过 30s 未返回，任务被强制中断', severity: 'high' },
    logic_error: { description: 'Agent 误判任务已完成，跳过了验证步骤', severity: 'medium' },
    tool_error: { description: '调用文件写入工具时权限不足，返回 EACCES', severity: 'medium' },
    external_api: { description: '外部 API 返回 429，重试 3 次后仍失败', severity: 'high' },
  };
  const d = demos[cat] || demos.timeout;
  try {
    const res = await fetch(`${API}/flywheel/failures`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: `demo-${Date.now()}`, category: cat, severity: d.severity, description: d.description, steps_to_failure: 2 + Math.floor(Math.random() * 5) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', '已记录失败样例', '失效分析阶段已更新');
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
    dom.exportPreview.textContent = preview ? 'Loading preview...' : 'Writing JSONL...';
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
      toast('success', 'Dataset exported', data.output_path || 'JSONL written');
    }
    return data;
  } catch (e) {
    toast('error', 'Export failed', e.message);
    if (dom.exportPreview) dom.exportPreview.textContent = e.message;
    return null;
  }
}

function renderConversationExport(data) {
  if (dom.exportMeta) {
    dom.exportMeta.innerHTML = `
      <span>Format: <strong>${esc(data.format || '')}</strong></span>
      <span>Samples: <strong>${formatNumber(data.sample_count || 0)}</strong></span>
      <span>Written: <strong>${data.written ? 'yes' : 'no'}</strong></span>
      ${data.output_path ? `<span>Path: <strong>${esc(data.output_path)}</strong></span>` : ''}
    `;
  }
  if (dom.exportPreview) {
    const samples = data.samples || [];
    dom.exportPreview.textContent = samples.length
      ? JSON.stringify(samples, null, 2)
      : 'No exportable sessions yet.';
  }
}

async function loadEvaluationSuites() {
  if (dom.evalSuiteGrid) {
    showLoading(dom.evalSuiteGrid, 'Loading suites...');
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
    toast('error', 'Failed to load suites', e.message);
    if (dom.evalSuiteGrid) dom.evalSuiteGrid.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
    return null;
  }
}

function renderEvaluationSuites(data) {
  const suites = data.suites || [];
  const errors = data.errors || [];
  if (!dom.evalSuiteGrid) return;
  if (!suites.length && !errors.length) {
    dom.evalSuiteGrid.innerHTML = `<div class="empty-state-lg"><p>No evaluation suites found.</p></div>`;
    return;
  }
  dom.evalSuiteGrid.innerHTML = [
    ...suites.map(suite => `
      <article class="evolution-suite-card">
        <div class="evolution-suite-title">${esc(suite.name)}</div>
        <div class="evolution-suite-desc">${esc(suite.description || 'No description')}</div>
        <div class="evolution-suite-meta">
          <span>v${esc(suite.version || '1.0.0')}</span>
          <span>${formatNumber(suite.case_count || 0)} cases</span>
        </div>
        <div class="evolution-suite-path">${esc(suite.path || '')}</div>
      </article>
    `),
    ...errors.map(error => `
      <article class="evolution-suite-card error">
        <div class="evolution-suite-title">Parse error</div>
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
