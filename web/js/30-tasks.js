/* ============================================
   Symbio UI — 任务页：任务卡片、工作流策略/证据面板、执行详情
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Tasks Page ============
async function loadTasks() {
  showLoading(dom.tasksGrid, '加载任务...');
  try {
    const statusParam = state.taskFilter !== 'all' ? `?status=${state.taskFilter}` : '';
    const res = await fetch(`${API}/tasks${statusParam}`);
    const data = await res.json();
    state.tasks = data.tasks || [];
    renderTasks();
  } catch (e) {
    toast('error', '加载任务失败', e.message);
    dom.tasksGrid.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法加载任务列表</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadTasks()">重试</button>
        </div>
      </div>`;
  }
}

function taskFilterLabel(filter) {
  return { running: '运行中', completed: '已完成', failed: '失败', all: '全部' }[filter] || filter;
}

function renderTasks() {
  // Render filter tabs
  const filtersHtml = `
    <div class="filter-tabs">
      <button class="filter-tab ${state.taskFilter === 'all' ? 'active' : ''}" data-filter="all">全部 (${state.tasks.length})</button>
      <button class="filter-tab ${state.taskFilter === 'running' ? 'active' : ''}" data-filter="running">运行中</button>
      <button class="filter-tab ${state.taskFilter === 'completed' ? 'active' : ''}" data-filter="completed">已完成</button>
      <button class="filter-tab ${state.taskFilter === 'failed' ? 'active' : ''}" data-filter="failed">失败</button>
    </div>
  `;

  if (state.tasks.length === 0) {
    // 任务不是在这一页创建的——是 Agent 拆解对话时自动产生的。
    // 空状态必须说明这一点，否则用户会在这页找「新建任务」按钮而找不到。
    const isAll = state.taskFilter === 'all';
    dom.tasksGrid.innerHTML = `
      ${filtersHtml}
      ${isAll ? `
      <div class="empty-block">
        <p class="empty-block-title">还没有任何任务</p>
        <p class="empty-block-hint">任务不在这里手动创建，而是 Agent 拆解你的需求时自动生成的：去「对话」页提一个需要多步完成的目标，它会把目标拆成有依赖关系的子任务，执行过程和每一步的结果都会回到这一页。</p>
        <div class="empty-block-actions">
          <button class="btn-primary" type="button" onclick="switchPage('chat')">去对话页提一个目标</button>
        </div>
      </div>` : `
      <div class="empty-block">
        <p class="empty-block-title">没有${esc(taskFilterLabel(state.taskFilter))}的任务</p>
        <p class="empty-block-hint">切到「全部」可以看到所有任务及其当前状态。</p>
      </div>`}
    `;
    attachFilterListeners();
    return;
  }

  dom.tasksGrid.innerHTML = `
    ${filtersHtml}
    ${state.tasks.map(t => `
      <div class="task-card" data-id="${t.id}">
        <div class="task-card-header">
          <div class="task-card-title">${esc(t.name)}</div>
          <span class="task-status task-status-${t.status}">${statusLabel(t.status)}</span>
        </div>
        <div class="task-card-desc">${esc(t.description || '')}</div>
        <div class="task-card-meta">
          <span class="task-agent">${esc(t.agent)}</span>
          <span class="task-time">${formatTime(t.created_at)}</span>
        </div>
        ${t.steps ? `
          <div class="task-steps">
            ${t.steps.map(s => `
              <div class="task-step">
                <span class="task-step-icon step-${s.status}">${stepIcon(s.status)}</span>
                <span class="task-step-name">${esc(s.name)}</span>
                ${s.duration ? `<span class="task-step-dur">${s.duration}</span>` : ''}
              </div>
            `).join('')}
          </div>
        ` : ''}
        ${t.result ? `<div class="task-result">${esc(t.result)}</div>` : ''}
        <div class="task-evidence-stack">
          ${renderPlannerReviewerControls(t, 'compact')}
          ${renderWorkflowPolicyPanel(t, 'compact')}
          ${renderVerificationEvidencePanel(t, 'compact')}
          ${renderApprovalContextPanel(t, 'compact')}
        </div>
      </div>
    `).join('')}
  `;

  attachFilterListeners();
  attachReviewControlsInteractions(dom.tasksGrid);

  // Task detail click
  dom.tasksGrid.querySelectorAll('.task-card').forEach(card => {
    card.addEventListener('click', () => showTaskDetail(card.dataset.id));
  });
}

function attachFilterListeners() {
  dom.tasksGrid.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      state.taskFilter = tab.dataset.filter;
      loadTasks();
    });
  });
}

function statusLabel(status) {
  const map = { running: '运行中', completed: '已完成', failed: '失败', pending: '等待中' };
  return map[status] || status;
}

function stepIcon(status) {
  const map = { completed: '&#10003;', running: '&#9679;', failed: '&#10007;', pending: '&#9675;' };
  return map[status] || '?';
}

function statusTone(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'completed') return 'completed';
  if (normalized === 'cancelled') return 'cancelled';
  if (['failed', 'failed_policy', 'needs_verification', 'blocked', 'blocking', 'rejected'].includes(normalized)) return 'failed';
  if (['running', 'verifying', 'replanning'].includes(normalized)) return 'running';
  return 'pending';
}

function compactId(value, head = 8, tail = 6) {
  const text = String(value || '');
  if (!text || text.length <= head + tail + 1) return text;
  return `${text.slice(0, head)}...${text.slice(-tail)}`;
}

function asObj(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function firstValue(...values) {
  return values.find(v => v !== undefined && v !== null && v !== '');
}

function stringifyEvidence(value) {
  if (value === undefined || value === null || value === '') return '';
  if (Array.isArray(value)) {
    return value.map(stringifyEvidence).filter(Boolean).join('\n');
  }
  if (typeof value === 'object') {
    const label = firstValue(value.command, value.name, value.title, value.type, value.path, value.id, 'evidence');
    const status = firstValue(value.status, value.result, value.outcome, '');
    const detail = firstValue(value.summary, value.output, value.message, value.detail, value.reason, value.error, '');
    return [label, status, detail].filter(Boolean).join(' - ');
  }
  return String(value);
}

function collectWorkflowPolicy(item) {
  const meta = asObj(item.metadata);
  return asObj(firstValue(
    item.workflow_policy,
    item.workflowPolicy,
    item.policy,
    meta.workflow_policy,
    meta.workflowPolicy,
    meta.policy,
  ));
}

function renderWorkflowPolicyPanel(item, mode = 'card') {
  const policy = collectWorkflowPolicy(item);
  const checklist = Array.isArray(policy.checklist) ? policy.checklist.filter(Boolean) : [];
  const flags = [
    ['先出方案', policy.require_plan],
    ['测试先行', policy.require_tdd],
    ['先定根因', policy.require_root_cause_before_fix],
    ['完成前须验证', policy.require_verification_before_completion],
    ['需评审规格', policy.require_spec_review],
    ['歧义须澄清', policy.require_clarification_on_ambiguity],
  ].filter(([, value]) => value === true);

  if (!Object.keys(policy).length) {
    return `
      <div class="evidence-panel evidence-muted">
        <div class="evidence-panel-title">执行准则</div>
        <div class="evidence-empty">这条任务没有记录执行准则——说明它是按默认方式跑的，没有额外约束。</div>
      </div>
    `;
  }

  return `
    <div class="evidence-panel">
      <div class="evidence-panel-title">执行准则</div>
      ${flags.length ? `<div class="evidence-chips">${flags.map(([label]) => `<span class="evidence-chip">${esc(label)}</span>`).join('')}</div>` : '<div class="evidence-empty">记录了准则字段，但没有一条被启用。</div>'}
      ${checklist.length ? `
        <ul class="evidence-list ${mode === 'compact' ? 'evidence-list-compact' : ''}">
          ${checklist.slice(0, mode === 'compact' ? 3 : 8).map(item => `<li>${esc(item)}</li>`).join('')}
        </ul>
      ` : ''}
    </div>
  `;
}

function collectVerificationEvidence(item) {
  const meta = asObj(item.metadata);
  const candidates = firstValue(
    item.verification_evidence,
    item.verificationEvidence,
    item.verification,
    item.evidence,
    meta.verification_evidence,
    meta.verificationEvidence,
    meta.verification,
    meta.evidence,
  );
  const direct = Array.isArray(candidates) ? candidates : (candidates ? [candidates] : []);
  const stepEvidence = (item.steps || [])
    .filter(step => /test|verify|verification|check|lint|audit|scan|测试|验证|检查|审查/i.test(`${step.name || ''} ${step.status || ''}`))
    .map(step => ({
      name: step.name,
      status: step.status,
      duration: step.duration,
    }));
  return [...direct, ...stepEvidence].map(stringifyEvidence).filter(Boolean);
}

function renderVerificationEvidencePanel(item, mode = 'card') {
  const evidence = collectVerificationEvidence(item);
  if (!evidence.length) {
    return `
      <div class="evidence-panel evidence-muted">
        <div class="evidence-panel-title">验证证据</div>
        <div class="evidence-empty">还没有验证证据。也就是说这条任务声称的结果没有留下可核对的凭据（测试输出、命令回显），只能当作「它自己说完成了」。</div>
      </div>
    `;
  }

  return `
    <div class="evidence-panel">
      <div class="evidence-panel-title">验证证据</div>
      <ul class="evidence-list ${mode === 'compact' ? 'evidence-list-compact' : ''}">
        ${evidence.slice(0, mode === 'compact' ? 3 : 10).map(item => `<li>${esc(item)}</li>`).join('')}
      </ul>
    </div>
  `;
}

function collectApprovalContext(item) {
  const meta = asObj(item.metadata);
  const hitl = asObj(firstValue(item.hitl, item.approval, meta.hitl, meta.approval));
  const approvals = firstValue(item.approvals, hitl.approvals, meta.approvals, []);
  const alternatives = firstValue(item.alternatives, hitl.alternatives, meta.alternatives, []);
  return {
    requestId: firstValue(item.hitl_request_id, item.request_id, hitl.request_id, hitl.id, meta.hitl_request_id, meta.approval_request_id),
    code: firstValue(item.code, hitl.code, meta.approval_code),
    risk: firstValue(item.risk, item.risk_level, hitl.risk, hitl.risk_level, meta.risk_level),
    action: firstValue(item.action, hitl.action, meta.action),
    impact: firstValue(item.impact_scope, hitl.impact_scope, meta.impact_scope),
    reason: firstValue(item.reason, item.blocked_reason, hitl.reason, hitl.blocked_reason, meta.blocked_reason),
    blocked: firstValue(item.blocked_context, item.blocked, hitl.blocked_context, meta.blocked_context),
    status: firstValue(item.status, hitl.status),
    requiredApprovers: firstValue(item.required_approvers, hitl.required_approvers, meta.required_approvers),
    approvals: Array.isArray(approvals) ? approvals : [],
    alternatives: Array.isArray(alternatives) ? alternatives : [],
  };
}

function renderApprovalContextPanel(item, mode = 'card') {
  const ctx = collectApprovalContext(item);
  const rows = [
    ['风险等级', ctx.risk],
    ['动作', ctx.action],
    ['影响范围', ctx.impact],
    ['原因', ctx.reason],
    ['阻塞上下文', stringifyEvidence(ctx.blocked)],
    ['请求 ID', ctx.requestId],
    ['代码', ctx.code],
    ['需谁批准', ctx.requiredApprovers],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');

  const approvalLines = ctx.approvals.map(a => {
    const decision = firstValue(a.decision, a.status, '审批');
    const approver = firstValue(a.approver_id, a.approver, a.user, '未知审批人');
    const comment = firstValue(a.comment, '');
    return `${decision} — ${approver}${comment ? `：${comment}` : ''}`;
  });

  if (!rows.length && !approvalLines.length && !ctx.alternatives.length) {
    return `
      <div class="evidence-panel evidence-muted">
        <div class="evidence-panel-title">审批与阻塞信息</div>
        <div class="evidence-empty">这条任务没有走过审批，也没有被阻塞——全程自主完成。</div>
      </div>
    `;
  }

  return `
    <div class="evidence-panel">
      <div class="evidence-panel-title">审批与阻塞信息</div>
      ${rows.length ? `
        <div class="evidence-kv">
          ${rows.slice(0, mode === 'compact' ? 5 : 10).map(([label, value]) => `
            <div class="evidence-kv-row">
              <span>${esc(label)}</span>
              <strong>${esc(String(value))}</strong>
            </div>
          `).join('')}
        </div>
      ` : ''}
      ${ctx.alternatives.length ? `
        <ul class="evidence-list ${mode === 'compact' ? 'evidence-list-compact' : ''}">
          ${ctx.alternatives.slice(0, mode === 'compact' ? 2 : 6).map(item => `<li>备选方案：${esc(item)}</li>`).join('')}
        </ul>
      ` : ''}
      ${approvalLines.length ? `
        <ul class="evidence-list ${mode === 'compact' ? 'evidence-list-compact' : ''}">
          ${approvalLines.slice(0, mode === 'compact' ? 2 : 8).map(item => `<li>${esc(item)}</li>`).join('')}
        </ul>
      ` : ''}
    </div>
  `;
}

function normalizeReviewSection(value) {
  if (value === undefined || value === null || value === '') return '';
  if (Array.isArray(value)) return value.map(stringifyEvidence).filter(Boolean).join('\n');
  return typeof value === 'object' ? safeExecutionJson(value) : String(value);
}

function normalizeReviewFindings(value) {
  if (value === undefined || value === null || value === '') return [];
  const list = Array.isArray(value) ? value : [value];
  return list.map((finding, index) => {
    if (finding && typeof finding === 'object') {
      const severity = firstValue(finding.severity, finding.level, finding.status, finding.type, 'blocking');
      const title = firstValue(finding.title, finding.summary, finding.reason, finding.message, finding.code, `Finding ${index + 1}`);
      const detail = firstValue(finding.detail, finding.description, finding.evidence, finding.context, finding.path, finding.node_id, '');
      return { severity: String(severity), title: String(title), detail: normalizeReviewSection(detail) };
    }
    return { severity: 'blocking', title: String(finding), detail: '' };
  }).filter(finding => finding.title || finding.detail);
}

function collectPlannerReviewer(item) {
  const meta = asObj(item.metadata);
  const reviewer = asObj(firstValue(
    item.planner_reviewer,
    item.plannerReviewer,
    item.planner_review,
    item.review,
    meta.planner_reviewer,
    meta.plannerReviewer,
    meta.planner_review,
    meta.review,
  ));
  const result = asObj(firstValue(reviewer.result, reviewer.output, reviewer.data, reviewer.review));
  const sectionsSource = asObj(firstValue(reviewer.sections, result.sections));
  const blocking = firstValue(
    reviewer.blocking_findings,
    reviewer.blockingFindings,
    reviewer.blockers,
    result.blocking_findings,
    result.blockers,
    sectionsSource.blocking_findings,
    [],
  );
  const findings = normalizeReviewFindings(blocking);
  const extraFindings = normalizeReviewFindings(firstValue(reviewer.findings, result.findings, []))
    .filter(finding => !findings.some(blocker => blocker.title === finding.title && blocker.detail === finding.detail));

  return {
    hasData: Object.keys(reviewer).length > 0 || Object.keys(result).length > 0,
    status: firstValue(reviewer.status, reviewer.outcome, result.status, result.outcome, item.review_status, ''),
    summary: firstValue(reviewer.summary, result.summary, reviewer.message, result.message, ''),
    reviewer: firstValue(reviewer.reviewer, reviewer.agent, result.reviewer, result.agent, 'planner_reviewer'),
    updatedAt: firstValue(reviewer.updated_at, reviewer.created_at, result.updated_at, result.created_at, ''),
    findings,
    extraFindings,
    sections: [
      ['plan', '方案', firstValue(reviewer.plan, result.plan, sectionsSource.plan, sectionsSource.planning, '')],
      ['spec_review', '规格评审', firstValue(reviewer.spec_review, reviewer.specReview, result.spec_review, result.specReview, sectionsSource.spec_review, '')],
      ['quality_review', '质量评审', firstValue(reviewer.quality_review, reviewer.qualityReview, result.quality_review, result.qualityReview, sectionsSource.quality_review, '')],
    ].map(([key, label, value]) => ({ key, label, body: normalizeReviewSection(value) })).filter(section => section.body),
  };
}

function renderPlannerReviewerControls(item, mode = 'card') {
  const review = collectPlannerReviewer(item);
  if (!review.hasData) {
    return `
      <div class="review-panel review-muted">
        <div class="review-panel-title">方案自审</div>
        <div class="evidence-empty">这条任务没有走方案自审。自审是让 Agent 在动手前先审自己的计划，没有记录说明它是直接执行的。</div>
      </div>
    `;
  }

  const tone = statusTone(review.status || (review.findings.length ? 'failed' : 'completed'));
  const compact = mode === 'compact';
  const sections = review.sections.slice(0, compact ? 2 : review.sections.length);
  return `
    <div class="review-panel" data-review-panel>
      <div class="review-panel-header">
        <div>
          <div class="review-panel-title">方案自审</div>
          <div class="review-panel-subtitle">${esc(review.reviewer)}${review.updatedAt ? ` / ${esc(formatTime(review.updatedAt))}` : ''}</div>
        </div>
        <span class="task-status task-status-${tone}">${esc(statusLabel(review.status || tone))}</span>
      </div>
      <div class="review-summary">
        <span class="review-chip">${review.findings.length} 项阻塞</span>
        <span class="review-chip">${review.extraFindings.length} 项发现</span>
        <span class="review-chip">${review.sections.length} 个章节</span>
      </div>
      ${review.summary ? `<div class="review-status-summary">${esc(String(review.summary))}</div>` : ''}
      ${review.findings.length ? `
        <div class="review-quick-actions">
          <button type="button" class="review-action-btn" data-review-jump="blocking">跳到阻塞原因</button>
          <button type="button" class="review-action-btn" data-review-expand="all">全部展开</button>
          <button type="button" class="review-action-btn" data-review-expand="none">全部收起</button>
        </div>
        <div class="review-findings" data-review-section="blocking">
          ${review.findings.slice(0, compact ? 2 : 12).map((finding, index) => `
            <div class="review-finding" data-review-finding>
              <div class="review-finding-title">
                <span class="task-status task-status-${statusTone(finding.severity)}">${esc(statusLabel(finding.severity))}</span>
                <strong>${esc(finding.title)}</strong>
              </div>
              ${finding.detail ? `<div class="review-finding-detail">${esc(finding.detail)}</div>` : ''}
              ${index === 0 ? '<span class="review-anchor-label">主要阻塞原因</span>' : ''}
            </div>
          `).join('')}
          ${review.findings.length > (compact ? 2 : 12) ? `<div class="evidence-empty">共 ${review.findings.length} 项阻塞，这里只显示前 ${compact ? 2 : 12} 项。</div>` : ''}
        </div>
      ` : '<div class="review-status-summary">自审没有发现阻塞项——方案可以照此执行。</div>'}
      ${sections.length ? `
        <div class="review-section-list">
          ${sections.map(section => `
            <details class="review-section" data-review-section="${esc(section.key)}">
              <summary>${esc(section.label)}</summary>
              <pre>${esc(section.body)}</pre>
            </details>
          `).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function extractExecutionId(item) {
  const meta = asObj(item?.metadata);
  const data = asObj(item?.data);
  const execution = asObj(item?.execution);
  const dagRuntime = asObj(meta.dag_runtime || meta.dagRuntime);
  return firstValue(
    item?.execution_id,
    item?.executionId,
    data.execution_id,
    data.executionId,
    execution.execution_id,
    dagRuntime.execution_id,
    dagRuntime.executionId,
    meta.execution_id,
    meta.executionId,
  );
}

async function loadExecutionEvidence(executionId) {
  if (!executionId) return null;
  if (state.executionCache[executionId]) {
    return state.executionCache[executionId];
  }

  const urls = [
    `${API}/executions/${executionId}`,
    `${API}/executions/${executionId}/events`,
    `${API}/executions/${executionId}/artifacts`,
  ];

  try {
    const responses = await Promise.all(urls.map(url => fetch(url)));
    if (responses.some(res => !res.ok)) {
      throw new Error(`Execution API returned ${responses.map(res => res.status).join('/')}`);
    }

    const [detail, events, artifacts] = await Promise.all(responses.map(res => res.json()));
    const bundle = { detail, events, artifacts };
    state.executionCache[executionId] = bundle;
    return bundle;
  } catch (e) {
    console.warn('Failed to load execution evidence:', executionId, e.message);
    return {
      error: e.message,
      detail: { execution: { execution_id: executionId } },
      events: { events: [], total: 0 },
      artifacts: { artifacts: [], total: 0 },
    };
  }
}

function normalizeExecutionValue(value, fallback = 'unknown') {
  const text = String(firstValue(value, fallback)).trim();
  return text || fallback;
}

function getEventStatus(event) {
  const payload = asObj(event?.payload);
  return normalizeExecutionValue(firstValue(event?.status, payload.status, payload.outcome, payload.result, 'event'));
}

function safeExecutionJson(value) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return stringifyEvidence(value);
  }
}

function summarizeExecutionStatus(items, statusGetter) {
  return items.reduce((summary, item) => {
    const tone = statusTone(statusGetter(item));
    summary[tone] = (summary[tone] || 0) + 1;
    return summary;
  }, {});
}

function renderExecutionStatusSummary(items, statusGetter, emptyLabel) {
  const summary = summarizeExecutionStatus(items, statusGetter);
  const chips = ['completed', 'running', 'failed', 'cancelled', 'pending']
    .filter(tone => summary[tone])
    .map(tone => `<span class="execution-chip execution-status-chip task-status-${tone}">${esc(statusLabel(tone))}: ${summary[tone]}</span>`);
  return chips.length ? chips.join('') : `<span class="execution-chip">${esc(emptyLabel)}</span>`;
}

function renderExecutionNodeStatusBreakdown(nodes) {
  if (!nodes.length) return '';
  const groups = new Map();
  nodes.forEach(node => {
    const tone = statusTone(node.status);
    if (!groups.has(tone)) groups.set(tone, []);
    groups.get(tone).push(node);
  });

  return `
    <div class="execution-node-breakdown">
      ${['failed', 'running', 'pending', 'cancelled', 'completed'].filter(tone => groups.has(tone)).map(tone => `
        <div class="execution-node-breakdown-group">
          <span class="task-status task-status-${tone}">${esc(statusLabel(tone))}</span>
          <div class="execution-node-breakdown-items">
            ${groups.get(tone).slice(0, 6).map(node => `
              <button type="button" class="execution-node-pill" data-execution-node-jump="${esc(node.node_id || node.name || 'unassigned')}">${esc(compactId(node.name || node.node_id || 'node', 16, 6))}</button>
            `).join('')}
            ${groups.get(tone).length > 6 ? `<span class="execution-node-more">+${groups.get(tone).length - 6}</span>` : ''}
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderExecutionDetails(label, body, options = {}) {
  if (!body) return '';
  const detailClass = options.compact ? ' execution-details-compact' : '';
  return `
    <details class="execution-details${detailClass}">
      <summary>${esc(label)}</summary>
      <pre>${esc(body)}</pre>
    </details>
  `;
}

function renderExecutionNodeDetails(node) {
  const detail = {
    node_id: node.node_id,
    executor: node.executor,
    status: node.status,
    depends_on: node.depends_on || node.dependencies,
    inputs: node.inputs,
    outputs: node.outputs,
    payload: node.payload,
    error: node.error,
  };
  const compactDetail = Object.fromEntries(Object.entries(detail).filter(([, value]) => value !== undefined && value !== null && value !== ''));
  return renderExecutionDetails('详情', Object.keys(compactDetail).length ? safeExecutionJson(compactDetail) : '');
}

function getArtifactPreviewValue(artifact) {
  return firstValue(
    artifact.content,
    artifact.preview,
    artifact.summary,
    artifact.text,
    artifact.message,
    artifact.metadata,
    '',
  );
}

function getArtifactPath(artifact) {
  return firstValue(artifact.path_ref, artifact.path, artifact.uri, artifact.url, artifact.file, '');
}

function getArtifactType(artifact) {
  return firstValue(artifact.artifact_type, artifact.type, artifact.content_type, artifact.mime_type, 'artifact');
}

function buildArtifactPreview(artifact) {
  const content = getArtifactPreviewValue(artifact);
  const contentType = String(firstValue(artifact.content_type, artifact.mime_type, '')).toLowerCase();
  const size = firstValue(artifact.size_bytes, artifact.bytes, artifact.size, '');
  const path = getArtifactPath(artifact);
  const isBinary = /image|audio|video|application\/octet-stream|zip|gzip|tar|pdf/.test(contentType);

  if ((content === '' || content === undefined || content === null) && path) {
    return {
      body: `Stored artifact reference:\n${path}`,
      truncated: false,
      reason: '',
    };
  }

  if (isBinary && typeof content === 'string' && content.length > 300) {
    return {
      body: [
        '二进制产物不在页面里展开，只显示元信息。',
        path ? `路径：${path}` : '',
        contentType ? `类型：${contentType}` : '',
        size ? `大小：${size}` : '',
      ].filter(Boolean).join('\n'),
      truncated: false,
      reason: 'binary',
    };
  }

  let preview = safeExecutionJson(content);
  const maxPreviewLength = 2400;
  const truncated = preview.length > maxPreviewLength;
  if (truncated) preview = `${preview.slice(0, maxPreviewLength)}\n... truncated ...`;
  return { body: preview, truncated, reason: truncated ? 'length' : '' };
}

function renderArtifactPreview(artifact) {
  const content = getArtifactPreviewValue(artifact);
  const path = getArtifactPath(artifact);
  const type = getArtifactType(artifact);
  const nodeId = normalizeExecutionValue(firstValue(artifact.node_id, artifact.nodeId, artifact.source_node_id, asObj(artifact.metadata).node_id, ''), '');
  const preview = buildArtifactPreview(artifact);
  const artifactMeta = [
    firstValue(artifact.content_type, artifact.mime_type, ''),
    firstValue(artifact.size_bytes, artifact.bytes, artifact.size, ''),
    preview.truncated ? 'truncated' : '',
    preview.reason === 'binary' ? 'binary' : '',
  ].filter(Boolean);

  return `
    <div class="execution-row execution-artifact-row" ${nodeId ? `data-artifact-node-id="${esc(nodeId)}"` : ''}>
      <div class="execution-row-main">
        <div class="execution-row-title">${esc(type)}</div>
        ${artifactMeta.length ? `<div class="execution-artifact-meta">${artifactMeta.map(item => `<span>${esc(String(item))}</span>`).join('')}</div>` : ''}
        <div class="execution-row-subtitle">${esc(firstValue(path, stringifyEvidence(content), '（无产物详情）'))}</div>
        ${preview.body ? renderExecutionDetails('预览', preview.body, { compact: true }) : '<div class="execution-empty execution-empty-inline">这个产物没有可预览的内容。</div>'}
      </div>
      <span class="execution-row-time">${esc(formatTime(artifact.created_at) || '')}</span>
    </div>
  `;
}

function getEventNodeId(event) {
  return normalizeExecutionValue(event.node_id, 'unassigned');
}

function getEventGroupLabel(event, groupMode) {
  const nodeId = getEventNodeId(event);
  const status = getEventStatus(event);
  const tone = statusTone(status);
  if (groupMode === 'node') {
    return {
      key: nodeId,
      title: nodeId === 'unassigned' ? '未归属节点' : compactId(nodeId, 14, 8),
      subtitle: '节点',
      tone,
    };
  }
  if (groupMode === 'status') {
    return {
      key: tone,
      title: statusLabel(tone),
      subtitle: '状态',
      tone,
    };
  }
  return {
    key: `${nodeId}::${tone}`,
    title: nodeId === 'unassigned' ? '未归属节点' : compactId(nodeId, 12, 8),
    subtitle: statusLabel(status),
    tone,
  };
}

function renderExecutionEventGroups(events, groupMode = 'node-status') {
  if (!events.length) return '<div class="execution-empty">这次执行没有留下时间线事件。</div>';

  const latestEvents = events.slice(-80).reverse();
  const groups = new Map();
  latestEvents.forEach(event => {
    const nodeId = getEventNodeId(event);
    const status = getEventStatus(event);
    const tone = statusTone(status);
    const group = getEventGroupLabel(event, groupMode);
    if (!groups.has(group.key)) {
      groups.set(group.key, { ...group, nodeId, status, events: [] });
    }
    groups.get(group.key).events.push(event);
  });

  return `
    <div class="execution-event-list" data-execution-event-list data-execution-group-mode="${esc(groupMode)}">
      ${Array.from(groups.values()).map(group => `
        <div class="execution-event-group" data-group-key="${esc(group.key)}">
          <div class="execution-event-group-title">
            <span>${esc(group.title)}</span>
            <span class="task-status task-status-${group.tone}">${esc(group.subtitle)}</span>
            <span class="execution-row-time" data-execution-group-count>${group.events.length} events</span>
          </div>
          ${group.events.map(event => {
            const payload = safeExecutionJson(event.payload);
            const nodeId = getEventNodeId(event);
            const tone = statusTone(getEventStatus(event));
            return `
              <div class="execution-row execution-event-row" data-node-id="${esc(nodeId)}" data-status="${esc(tone)}">
                <div class="execution-row-main">
                  <div class="execution-row-title">${esc(firstValue(event.event_type, event.type, '事件'))}</div>
                  <div class="execution-row-subtitle">${esc(firstValue(stringifyEvidence(event.payload), nodeId, '（无载荷详情）'))}</div>
                  ${renderExecutionDetails('载荷', payload, { compact: true })}
                </div>
                <span class="execution-row-time">${esc(formatTime(event.timestamp) || '')}</span>
              </div>
            `;
          }).join('')}
        </div>
      `).join('')}
      ${events.length > latestEvents.length ? `<div class="execution-empty">Showing latest ${latestEvents.length} of ${events.length} events.</div>` : ''}
    </div>
  `;
}

function renderExecutionPanel(task, executionBundle = null) {
  const executionId = extractExecutionId(task) || executionBundle?.detail?.execution?.execution_id;
  if (!executionId) {
    return `
      <div class="execution-panel execution-panel-muted">
        <div class="evidence-panel-title">执行图（DAG）</div>
        <div class="execution-empty">这条任务还没有关联执行记录。执行图只在任务真正被编排成 DAG 跑起来之后才生成——简单的一问一答不会产生。</div>
      </div>
    `;
  }

  const execution = asObj(executionBundle?.detail?.execution);
  const nodes = Array.isArray(executionBundle?.detail?.nodes) ? executionBundle.detail.nodes : [];
  const graphVersions = Array.isArray(executionBundle?.detail?.graph_versions) ? executionBundle.detail.graph_versions : [];
  const events = Array.isArray(executionBundle?.events?.events) ? executionBundle.events.events : [];
  const artifacts = Array.isArray(executionBundle?.artifacts?.artifacts) ? executionBundle.artifacts.artifacts : [];
  const latestGraph = graphVersions.length ? graphVersions[graphVersions.length - 1] : null;
  const status = execution.status || 'planned';
  const eventNodeIds = Array.from(new Set(events.map(event => normalizeExecutionValue(event.node_id, 'unassigned')))).sort();
  const eventStatuses = Array.from(new Set(events.map(event => statusTone(getEventStatus(event))))).sort();
  const artifactNodeIds = Array.from(new Set(artifacts
    .map(artifact => normalizeExecutionValue(firstValue(artifact.node_id, artifact.nodeId, artifact.source_node_id, asObj(artifact.metadata).node_id, ''), ''))
    .filter(Boolean))).sort();
  const rows = [
    ['任务 ID', execution.task_id || task.id],
    ['计划版本', execution.plan_version],
    ['重规划次数', execution.replan_generation],
    ['创建时间', formatTime(execution.created_at)],
    ['完成时间', formatTime(execution.completed_at)],
    ['图版本', latestGraph ? `v${latestGraph.graph_version}` : 'v1'],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '');

  return `
    <div class="execution-panel">
      <div class="execution-panel-header">
        <div class="execution-panel-heading">
          <div class="execution-panel-title">执行图（DAG）</div>
          <div class="execution-panel-id">${esc(executionId)}</div>
        </div>
        <span class="task-status task-status-${statusTone(status)}">${esc(statusLabel(status))}</span>
      </div>

      <div class="execution-chip-row">
        <span class="execution-chip">${nodes.length} 个节点</span>
        <span class="execution-chip">${events.length} 条事件</span>
        <span class="execution-chip">${artifacts.length} 个产物</span>
        <span class="execution-chip">${graphVersions.length || 1} 个图版本</span>
        ${executionBundle?.error ? `<span class="execution-chip execution-chip-warning">${esc(executionBundle.error)}</span>` : ''}
      </div>

      <div class="execution-view-tabs" role="tablist" aria-label="执行视图切换">
        <button type="button" class="execution-view-tab active" data-execution-view-tab="graph">依赖图</button>
        <button type="button" class="execution-view-tab" data-execution-view-tab="timeline">时间线</button>
        <button type="button" class="execution-view-tab" data-execution-view-tab="artifacts">产物</button>
      </div>

      <div class="execution-summary-grid">
        <div class="execution-summary-card">
          <div class="execution-meta-label">节点状态</div>
          <div class="execution-chip-row">${renderExecutionStatusSummary(nodes, node => node.status, '没有节点状态')}</div>
          ${renderExecutionNodeStatusBreakdown(nodes)}
        </div>
        <div class="execution-summary-card">
          <div class="execution-meta-label">事件状态</div>
          <div class="execution-chip-row">${renderExecutionStatusSummary(events, getEventStatus, '没有事件状态')}</div>
        </div>
      </div>

      ${rows.length ? `
        <div class="execution-meta-grid">
          ${rows.map(([label, value]) => `
            <div class="execution-meta-item">
              <span class="execution-meta-label">${esc(label)}</span>
              <strong class="execution-meta-value">${esc(String(value))}</strong>
            </div>
          `).join('')}
        </div>
      ` : ''}

      <div class="execution-stack">
        <div class="execution-section" data-execution-view="graph">
          <div class="execution-section-header">
            <div class="execution-section-title">依赖图节点</div>
            <span class="execution-section-count">${nodes.length}</span>
          </div>
          ${nodes.length ? `
            <div class="execution-node-list">
              ${nodes.map(node => `
                <div class="execution-row execution-node-row" data-execution-node-row="${esc(node.node_id || node.name || 'unassigned')}">
                  <div class="execution-row-main">
                    <div class="execution-row-title">${esc(node.name || node.node_id)}</div>
                    <div class="execution-row-subtitle">${esc([node.executor, compactId(node.node_id)].filter(Boolean).join(' - '))}</div>
                    ${renderExecutionNodeDetails(node)}
                  </div>
                  <div class="execution-row-actions">
                    <span class="task-status task-status-${statusTone(node.status)}">${esc(statusLabel(node.status))}</span>
                    <button type="button" class="execution-filter-btn execution-row-btn" data-execution-node-jump="${esc(node.node_id || node.name || 'unassigned')}">看事件</button>
                  </div>
                </div>
              `).join('')}
            </div>
          ` : '<div class="execution-empty">这次执行没有记录任何节点。</div>'}
        </div>

        <div class="execution-section" data-execution-view="timeline" hidden>
          <div class="execution-section-header">
            <div class="execution-section-title">时间线</div>
            <span class="execution-section-count" data-execution-visible-count>${events.length}</span>
          </div>
          ${events.length ? `
            <div class="execution-filter-bar">
              <label>
                <span>搜索</span>
                <input class="execution-filter-select" data-execution-filter="text" placeholder="事件名或载荷内容">
              </label>
              <label>
                <span>节点</span>
                <select class="execution-filter-select" data-execution-filter="node">
                  <option value="all">全部节点</option>
                  ${eventNodeIds.map(nodeId => `<option value="${esc(nodeId)}">${esc(nodeId === 'unassigned' ? '未归属' : compactId(nodeId, 18, 8))}</option>`).join('')}
                </select>
              </label>
              <label>
                <span>状态</span>
                <select class="execution-filter-select" data-execution-filter="status">
                  <option value="all">全部状态</option>
                  ${eventStatuses.map(eventStatus => `<option value="${esc(eventStatus)}">${esc(statusLabel(eventStatus))}</option>`).join('')}
                </select>
              </label>
              <label>
                <span>分组方式</span>
                <select class="execution-filter-select" data-execution-filter="group">
                  <option value="node-status">节点 + 状态</option>
                  <option value="node">按节点</option>
                  <option value="status">按状态</option>
                </select>
              </label>
              <div class="execution-filter-actions">
                <button type="button" class="execution-filter-btn" data-execution-details="open">展开载荷</button>
                <button type="button" class="execution-filter-btn" data-execution-details="close">收起</button>
                <button type="button" class="execution-filter-btn" data-execution-reset>重置筛选</button>
              </div>
            </div>
            <div class="execution-filter-result" data-execution-filter-result></div>
          ` : ''}
          ${renderExecutionEventGroups(events)}
        </div>

        <div class="execution-section" data-execution-view="artifacts" hidden>
          <div class="execution-section-header">
            <div class="execution-section-title">产物</div>
            <span class="execution-section-count">${artifacts.length}</span>
          </div>
          ${artifactNodeIds.length ? `
            <div class="execution-filter-bar execution-artifact-filter-bar">
              <label>
                <span>节点</span>
                <select class="execution-filter-select" data-artifact-filter="node">
                  <option value="all">全部节点</option>
                  ${artifactNodeIds.map(nodeId => `<option value="${esc(nodeId)}">${esc(compactId(nodeId, 18, 8))}</option>`).join('')}
                </select>
              </label>
              <div class="execution-filter-result" data-artifact-filter-result></div>
            </div>
          ` : ''}
          ${artifacts.length ? `
            <div class="execution-artifact-list">
              ${artifacts.slice(-20).reverse().map(renderArtifactPreview).join('')}
              ${artifacts.length > 20 ? `<div class="execution-empty">共 ${artifacts.length} 个产物，这里只显示最近 20 个。</div>` : ''}
            </div>
          ` : '<div class="execution-empty">这次执行没有产出文件。产物指执行过程中真正写下来的东西（代码、报告、数据），没有说明这次任务只是读取或推理。</div>'}
        </div>
      </div>
    </div>
  `;
}

function attachExecutionPanelInteractions(root) {
  root.querySelectorAll('.execution-panel').forEach(panel => {
    const nodeFilter = panel.querySelector('[data-execution-filter="node"]');
    const statusFilter = panel.querySelector('[data-execution-filter="status"]');
    const groupFilter = panel.querySelector('[data-execution-filter="group"]');
    const textFilter = panel.querySelector('[data-execution-filter="text"]');
    const eventList = panel.querySelector('[data-execution-event-list]');
    const result = panel.querySelector('[data-execution-filter-result]');
    const activateView = (view) => {
      panel.querySelectorAll('[data-execution-view]').forEach(section => {
        section.hidden = section.dataset.executionView !== view;
      });
      panel.querySelectorAll('[data-execution-view-tab]').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.executionViewTab === view);
      });
    };

    panel.querySelectorAll('[data-execution-view-tab]').forEach(tab => {
      tab.addEventListener('click', () => activateView(tab.dataset.executionViewTab || 'graph'));
    });

    const update = () => {
      if (!eventList) return;
      const rows = Array.from(panel.querySelectorAll('.execution-event-row'));
      const groups = Array.from(panel.querySelectorAll('.execution-event-group'));
      const selectedNode = nodeFilter?.value || 'all';
      const selectedStatus = statusFilter?.value || 'all';
      const query = String(textFilter?.value || '').trim().toLowerCase();
      let visibleRows = 0;

      rows.forEach(row => {
        const nodeOk = selectedNode === 'all' || row.dataset.nodeId === selectedNode;
        const statusOk = selectedStatus === 'all' || row.dataset.status === selectedStatus;
        const textOk = !query || row.textContent.toLowerCase().includes(query);
        const isVisible = nodeOk && statusOk && textOk;
        row.hidden = !isVisible;
        if (isVisible) visibleRows += 1;
      });

      groups.forEach(group => {
        const groupRows = Array.from(group.querySelectorAll('.execution-event-row'));
        const groupVisibleRows = groupRows.filter(row => !row.hidden);
        group.hidden = !groupVisibleRows.length;
        const groupCount = group.querySelector('[data-execution-group-count]');
        if (groupCount) groupCount.textContent = `${groupVisibleRows.length} / ${groupRows.length} events`;
      });

      const counter = panel.querySelector('[data-execution-visible-count]');
      if (counter) counter.textContent = String(visibleRows);
      if (result) {
        const parts = [];
        if (query) parts.push(`包含「${query}」`);
        if (selectedNode !== 'all') parts.push(`节点 ${selectedNode === 'unassigned' ? '未归属' : compactId(selectedNode, 18, 8)}`);
        if (selectedStatus !== 'all') parts.push(statusLabel(selectedStatus));
        result.textContent = parts.length ? `符合 ${parts.join(' / ')} 的事件 ${visibleRows} 条` : `共显示 ${visibleRows} 条时间线事件`;
      }
    };

    const regroupEvents = () => {
      if (!eventList) return;
      const groupMode = groupFilter.value || 'node-status';
      const rows = Array.from(eventList.querySelectorAll('.execution-event-row')).map(row => ({
        nodeId: row.dataset.nodeId || 'unassigned',
        status: row.dataset.status || 'pending',
        html: row.outerHTML,
      }));
      const groups = new Map();
      rows.forEach(row => {
        let key = `${row.nodeId}::${row.status}`;
        let title = row.nodeId === 'unassigned' ? '未归属节点' : compactId(row.nodeId, 12, 8);
        let subtitle = statusLabel(row.status);
        if (groupMode === 'node') {
          key = row.nodeId;
          title = row.nodeId === 'unassigned' ? '未归属节点' : compactId(row.nodeId, 14, 8);
          subtitle = '节点';
        } else if (groupMode === 'status') {
          key = row.status;
          title = statusLabel(row.status);
          subtitle = '状态';
        }
        if (!groups.has(key)) groups.set(key, { key, title, subtitle, tone: row.status, rows: [] });
        groups.get(key).rows.push(row.html);
      });
      eventList.dataset.executionGroupMode = groupMode;
      eventList.innerHTML = Array.from(groups.values()).map(group => `
        <div class="execution-event-group" data-group-key="${esc(group.key)}">
          <div class="execution-event-group-title">
            <span>${esc(group.title)}</span>
            <span class="task-status task-status-${group.tone}">${esc(group.subtitle)}</span>
            <span class="execution-row-time" data-execution-group-count>${group.rows.length} events</span>
          </div>
          ${group.rows.join('')}
        </div>
      `).join('');
      update();
    };
    groupFilter?.addEventListener('change', regroupEvents);

    panel.querySelectorAll('[data-execution-details]').forEach(button => {
      button.addEventListener('click', () => {
        const open = button.dataset.executionDetails === 'open';
        panel.querySelectorAll('.execution-event-row details').forEach(detail => {
          if (!detail.closest('.execution-event-row')?.hidden) detail.open = open;
        });
      });
    });

    panel.querySelectorAll('[data-execution-node-jump]').forEach(button => {
      button.addEventListener('click', () => {
        const nodeId = button.dataset.executionNodeJump || 'unassigned';
        activateView('timeline');
        if (nodeFilter) nodeFilter.value = Array.from(nodeFilter.options).some(option => option.value === nodeId) ? nodeId : 'all';
        update();
        const target = panel.querySelector(`.execution-event-row[data-node-id="${escapeSelectorValue(nodeFilter?.value || nodeId)}"]:not([hidden])`);
        if (target) {
          target.classList.add('execution-row-highlight');
          target.scrollIntoView({ block: 'center', behavior: 'smooth' });
          setTimeout(() => target.classList.remove('execution-row-highlight'), 1800);
        } else {
          panel.querySelector('[data-execution-view="timeline"]')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
      });
    });

    panel.querySelector('[data-execution-reset]')?.addEventListener('click', () => {
      if (nodeFilter) nodeFilter.value = 'all';
      if (statusFilter) statusFilter.value = 'all';
      if (groupFilter) groupFilter.value = 'node-status';
      if (textFilter) textFilter.value = '';
      if (groupFilter) {
        regroupEvents();
      } else {
        update();
      }
    });

    const artifactFilter = panel.querySelector('[data-artifact-filter="node"]');
    const artifactResult = panel.querySelector('[data-artifact-filter-result]');
    const updateArtifacts = () => {
      const selectedNode = artifactFilter?.value || 'all';
      const rows = Array.from(panel.querySelectorAll('.execution-artifact-row'));
      let visibleRows = 0;
      rows.forEach(row => {
        const nodeId = row.dataset.artifactNodeId || '';
        const isVisible = selectedNode === 'all' || nodeId === selectedNode;
        row.hidden = !isVisible;
        if (isVisible) visibleRows += 1;
      });
      if (artifactResult) artifactResult.textContent = selectedNode === 'all' ? `${visibleRows} artifacts shown` : `${visibleRows} artifacts for ${compactId(selectedNode, 18, 8)}`;
    };

    [nodeFilter, statusFilter].forEach(control => control?.addEventListener('change', update));
    textFilter?.addEventListener('input', update);
    artifactFilter?.addEventListener('change', updateArtifacts);
    updateArtifacts();
    update();
  });
}

function attachReviewControlsInteractions(root) {
  root.querySelectorAll('[data-review-panel]').forEach(panel => {
    panel.addEventListener('click', (event) => {
      if (event.target.closest('button, summary, details')) event.stopPropagation();
    });

    panel.querySelectorAll('[data-review-expand]').forEach(button => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const open = button.dataset.reviewExpand === 'all';
        panel.querySelectorAll('.review-section').forEach(section => { section.open = open; });
      });
    });

    panel.querySelectorAll('[data-review-jump]').forEach(button => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const section = panel.querySelector(`[data-review-section="${escapeSelectorValue(button.dataset.reviewJump)}"]`);
        const target = section?.querySelector('[data-review-finding]') || section;
        if (!target) return;
        target.classList.add('review-finding-highlight');
        target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setTimeout(() => target.classList.remove('review-finding-highlight'), 1800);
      });
    });
  });
}

async function showTaskDetail(taskId) {
  const fallbackTask = state.tasks.find(t => t.id === taskId);
  let task = fallbackTask;
  try {
    const res = await fetch(`${API}/tasks/${taskId}`);
    if (res.ok) {
      const data = await res.json();
      task = data.task || fallbackTask;
    }
  } catch (e) {
    console.warn('Failed to load task detail, using list payload:', e.message);
  }
  if (!task) return;
  const executionBundle = await loadExecutionEvidence(extractExecutionId(task));

  // Remove existing detail modal
  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <div class="modal-header">
        <h3>${esc(task.name)}</h3>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="task-status task-status-${task.status}">${statusLabel(task.status)}</span>
          <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>
      <div class="modal-body">
        <div class="detail-section">
          <div class="detail-label">描述</div>
          <div class="detail-value">${esc(task.description || '无')}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">执行者</div>
          <div class="detail-value">${esc(task.agent)}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">创建时间</div>
          <div class="detail-value">${formatTime(task.created_at)}</div>
        </div>
        ${task.completed_at ? `
          <div class="detail-section">
            <div class="detail-label">完成时间</div>
            <div class="detail-value">${formatTime(task.completed_at)}</div>
          </div>
        ` : ''}
        ${task.steps ? `
          <div class="detail-section">
            <div class="detail-label">执行步骤</div>
            <div class="detail-steps">
              ${task.steps.map(s => `
                <div class="detail-step">
                  <span class="task-step-icon step-${s.status}">${stepIcon(s.status)}</span>
                  <span>${esc(s.name)}</span>
                  ${s.duration ? `<span class="task-step-dur">${s.duration}</span>` : ''}
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}
        ${task.result ? `
          <div class="detail-section">
            <div class="detail-label">结果</div>
            <div class="detail-value detail-result">${esc(task.result)}</div>
          </div>
        ` : ''}
        <div class="detail-section">
          <div class="detail-label">执行图（DAG）</div>
          ${renderExecutionPanel(task, executionBundle)}
        </div>
        <div class="detail-section">
          <div class="detail-label">准则与证据</div>
          <div class="detail-evidence-grid">
            ${renderPlannerReviewerControls(task)}
            ${renderWorkflowPolicyPanel(task)}
            ${renderVerificationEvidencePanel(task)}
            ${renderApprovalContextPanel(task)}
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-close-btn-bottom">关闭</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  attachExecutionPanelInteractions(overlay);
  attachReviewControlsInteractions(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-close-btn-bottom').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Refresh button
$('#btn-refresh-tasks')?.addEventListener('click', loadTasks);

// Task filter select (from page header)
$('#task-filter')?.addEventListener('change', (e) => {
  state.taskFilter = e.target.value;
  loadTasks();
});

// Test all models button
$('#btn-test-all-models')?.addEventListener('click', async () => {
  if (state.models.length === 0) {
    toast('info', '无模型', '尚未添加任何模型');
    return;
  }
  toast('info', '测试中...', `正在测试 ${state.models.length} 个模型`);
  let successCount = 0;
  for (const m of state.models) {
    try {
      const res = await fetch(`${API}/models/${m.id}/test`, { method: 'POST' });
      const data = await res.json();
      const resultEl = document.getElementById(`test-result-${m.id}`);
      if (resultEl) {
        resultEl.innerHTML = `<div class="test-result ${data.success ? 'test-ok' : 'test-fail'}">${esc(data.message)}</div>`;
      }
      if (data.success) successCount++;
    } catch (e) {
      const resultEl = document.getElementById(`test-result-${m.id}`);
      if (resultEl) {
        resultEl.innerHTML = `<div class="test-result test-fail">请求失败: ${esc(e.message)}</div>`;
      }
    }
  }
  toast(successCount === state.models.length ? 'success' : 'error',
    '测试完成', `${successCount}/${state.models.length} 个模型连接正常`);
});
