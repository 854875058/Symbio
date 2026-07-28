/* ============================================
   Symbio UI — Sandbox 页：命令沙箱策略与审计
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Sandbox Page ============
async function loadSandbox() {
  await Promise.all([loadSandboxPolicy(), loadSandboxAudit()]);
}

async function loadSandboxPolicy() {
  try {
    const res = await fetch(`${API}/sandbox/policy`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.sandbox.policy = data;
    renderSandboxPolicy(data);
    if (dom.sandboxWorkingDir && !dom.sandboxWorkingDir.value) {
      dom.sandboxWorkingDir.placeholder = data.workspace_roots?.[0] || '默认工作区根目录';
    }
    return data;
  } catch (e) {
    toast('error', '加载沙箱策略失败', e.message);
    return null;
  }
}

function renderSandboxPolicy(policy) {
  if (!dom.sandboxPolicy) return;
  const roots = policy.workspace_roots || [];
  dom.sandboxPolicy.innerHTML = `
    <span class="sandbox-badge">${esc(policy.access_mode || '')}</span>
    <span class="sandbox-badge">${esc(policy.approval_policy || '')}</span>
    <span class="sandbox-badge">${policy.allow_network ? '联网：开' : '联网：关'}</span>
    ${roots[0] ? `<span class="sandbox-badge sandbox-root" title="${esc(roots[0])}">${esc(roots[0])}</span>` : ''}
  `;
}

function sandboxPayload(forceApproved = false) {
  return {
    command: dom.sandboxCommand?.value || '',
    permission_level: dom.sandboxPermission?.value || 'read_only',
    access_mode: dom.sandboxAccessMode?.value || 'workspace-write',
    approval_policy: dom.sandboxApprovalPolicy?.value || 'on-request',
    approved: forceApproved || !!dom.sandboxApproved?.checked,
    shell: !!dom.sandboxShell?.checked,
    timeout: Number(dom.sandboxTimeout?.value || 30),
    working_dir: dom.sandboxWorkingDir?.value || undefined,
  };
}

async function runSandbox(forceApproved = false) {
  const payload = sandboxPayload(forceApproved);

  // 危险程度必须和确认强度成正比。这两条是本页真正不可逆的入口：
  // 1) danger-full-access 等于取消沙箱隔离，命令能碰工作区之外的任何东西；
  // 2) forceApproved（「批准后执行」）直接绕过人工审批闸门。
  // 二者以前都是点一下就跑，而删一个 Skill 反倒有确认——用户由此学到
  // 「没弹窗 = 安全」，恰恰在最危险处失效。
  if (payload.access_mode === 'danger-full-access'
    && !confirmDanger('以完全放开权限执行？', '当前访问模式是 danger-full-access：命令不再被限制在工作目录内，可以读写本机任意文件、访问网络、修改系统配置。相当于没有沙箱。确认这条命令你完全清楚它会做什么。')) {
    return;
  }
  if (forceApproved
    && !confirmDanger('跳过审批直接执行？', '「批准后执行」会带着已批准标记提交，绕过本应由你逐条确认的人工审批环节。这条命令会立即执行，不再询问。')) {
    return;
  }

  if (dom.sandboxResult) {
    showLoading(dom.sandboxResult, '正在沙箱中执行...');
  }
  try {
    if (!payload.command.trim()) throw new Error('请填写要执行的命令');
    const res = await fetch(`${API}/sandbox/execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.sandbox.lastResult = data;
    renderSandboxResult(data);
    await loadSandboxAudit();
    if (data.approval_required) {
      toast('info', '需要审批', '这条命令触发了审批策略。勾选「已批准」或点「批准后执行」才会真正运行。');
    } else if (data.success) {
      toast('success', '执行完成', `退出码 ${data.result.exit_code}`);
    } else {
      toast('error', '被拦截或执行失败', data.result.error_message || `退出码 ${data.result.exit_code}`);
    }
  } catch (e) {
    toast('error', '执行失败', e.message);
    if (dom.sandboxResult) {
      dom.sandboxResult.innerHTML = `
        <div class="empty-block is-inline is-error">
          <p class="empty-block-title">执行失败</p>
          <p class="empty-block-hint">${esc(e.message)}</p>
        </div>`;
    }
  }
}

function renderSandboxResult(data) {
  if (!dom.sandboxResult) return;
  const result = data.result || {};
  const meta = result.metadata || {};
  const statusClass = data.approval_required ? 'approval' : (data.success ? 'success' : 'failed');
  dom.sandboxResult.innerHTML = `
    <div class="sandbox-result-head">
      <span class="sandbox-result-status ${statusClass}">${data.approval_required ? '待审批' : (data.success ? '成功' : '被拦截 / 失败')}</span>
      <span>退出码 ${esc(result.exit_code)}</span>
      <span>${Math.round(result.duration_ms || 0)}ms</span>
      <span>${esc(result.permission_level || '')}</span>
    </div>
    ${result.error_message ? `<div class="sandbox-error">${esc(result.error_message)}</div>` : ''}
    <div class="sandbox-output-grid">
      <div>
        <div class="sandbox-output-label">stdout</div>
        <pre class="sandbox-output">${esc(result.stdout || '')}</pre>
      </div>
      <div>
        <div class="sandbox-output-label">stderr</div>
        <pre class="sandbox-output">${esc(result.stderr || '')}</pre>
      </div>
    </div>
    <div class="sandbox-meta">
      <span>访问模式：${esc(meta.policy?.access_mode || '—')}</span>
      <span>审批策略：${esc(meta.policy?.approval_policy || '—')}</span>
      <span>已批准：${meta.approved ? '是' : '否'}</span>
      <span>需审批：${meta.approval_required ? '是' : '否'}</span>
      <span>${esc(result.working_dir || '')}</span>
    </div>
  `;
}

async function loadSandboxAudit() {
  if (dom.sandboxAuditList) {
    showLoading(dom.sandboxAuditList, '正在加载审计记录...');
  }
  try {
    const res = await fetch(`${API}/sandbox/audit`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.sandbox.audit = data.records || [];
    renderSandboxAudit(data.records || []);
    return data;
  } catch (e) {
    toast('error', '加载审计记录失败', e.message);
    if (dom.sandboxAuditList) {
      dom.sandboxAuditList.innerHTML = `
        <div class="empty-block is-inline is-error">
          <p class="empty-block-title">无法加载审计记录</p>
          <p class="empty-block-hint">${esc(e.message)}</p>
          <div class="empty-block-actions">
            <button class="btn-outline" type="button" onclick="loadSandboxAudit()">重试</button>
          </div>
        </div>`;
    }
    return null;
  }
}

function renderSandboxAudit(records) {
  if (!dom.sandboxAuditList) return;
  if (!records.length) {
    dom.sandboxAuditList.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">暂无执行记录</p>
        <p class="empty-block-hint">每一条在沙箱里跑过的命令都会留痕：命令原文、权限级别、是否被拦截、退出码。这是事后追查「Agent 到底动了什么」的唯一依据。</p>
      </div>`;
    return;
  }
  dom.sandboxAuditList.innerHTML = records.map(record => `
    <article class="sandbox-audit-card">
      <div class="sandbox-audit-head">
        <span class="sandbox-audit-command" title="${esc(record.command)}">${esc(record.command)}</span>
        <span class="sandbox-result-status ${record.approval_required ? 'approval' : (record.exit_code === 0 ? 'success' : 'failed')}">${record.approval_required ? 'approval' : `exit ${record.exit_code}`}</span>
      </div>
      <div class="sandbox-audit-meta">
        <span>${esc(record.permission_level)}</span>
        <span>${esc(record.access_mode)}</span>
        <span>${record.approved ? 'approved' : 'not approved'}</span>
        <span>${new Date(record.created_at).toLocaleTimeString()}</span>
      </div>
      ${record.reason ? `<div class="sandbox-audit-reason">${esc(record.reason)}</div>` : ''}
    </article>
  `).join('');
}

$('#btn-refresh-sandbox')?.addEventListener('click', loadSandbox);
$('#btn-run-sandbox')?.addEventListener('click', () => runSandbox(false));
$('#btn-run-sandbox-approved')?.addEventListener('click', () => runSandbox(true));
