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
      dom.sandboxWorkingDir.placeholder = data.workspace_roots?.[0] || 'Default workspace root';
    }
    return data;
  } catch (e) {
    toast('error', 'Sandbox policy failed', e.message);
    return null;
  }
}

function renderSandboxPolicy(policy) {
  if (!dom.sandboxPolicy) return;
  const roots = policy.workspace_roots || [];
  dom.sandboxPolicy.innerHTML = `
    <span class="sandbox-badge">${esc(policy.access_mode || '')}</span>
    <span class="sandbox-badge">${esc(policy.approval_policy || '')}</span>
    <span class="sandbox-badge">${policy.allow_network ? 'network:on' : 'network:off'}</span>
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
  if (dom.sandboxResult) {
    dom.sandboxResult.innerHTML = `<div class="empty-state-lg"><p>Running sandbox command...</p></div>`;
  }
  try {
    const payload = sandboxPayload(forceApproved);
    if (!payload.command.trim()) throw new Error('Command is required');
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
      toast('info', 'Approval required', 'Toggle approved or use Run approved to execute.');
    } else if (data.success) {
      toast('success', 'Sandbox command finished', `exit_code=${data.result.exit_code}`);
    } else {
      toast('error', 'Sandbox command blocked/failed', data.result.error_message || `exit_code=${data.result.exit_code}`);
    }
  } catch (e) {
    toast('error', 'Sandbox run failed', e.message);
    if (dom.sandboxResult) dom.sandboxResult.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
  }
}

function renderSandboxResult(data) {
  if (!dom.sandboxResult) return;
  const result = data.result || {};
  const meta = result.metadata || {};
  const statusClass = data.approval_required ? 'approval' : (data.success ? 'success' : 'failed');
  dom.sandboxResult.innerHTML = `
    <div class="sandbox-result-head">
      <span class="sandbox-result-status ${statusClass}">${data.approval_required ? 'approval required' : (data.success ? 'success' : 'blocked/failed')}</span>
      <span>exit=${esc(result.exit_code)}</span>
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
      <span>access: ${esc(meta.policy?.access_mode || '')}</span>
      <span>approval_policy: ${esc(meta.policy?.approval_policy || '')}</span>
      <span>approved: ${meta.approved ? 'yes' : 'no'}</span>
      <span>approval_required: ${meta.approval_required ? 'yes' : 'no'}</span>
      <span>${esc(result.working_dir || '')}</span>
    </div>
  `;
}

async function loadSandboxAudit() {
  if (dom.sandboxAuditList) {
    showLoading(dom.sandboxAuditList, 'Loading audit...');
  }
  try {
    const res = await fetch(`${API}/sandbox/audit`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.sandbox.audit = data.records || [];
    renderSandboxAudit(data.records || []);
    return data;
  } catch (e) {
    toast('error', 'Sandbox audit failed', e.message);
    if (dom.sandboxAuditList) dom.sandboxAuditList.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
    return null;
  }
}

function renderSandboxAudit(records) {
  if (!dom.sandboxAuditList) return;
  if (!records.length) {
    dom.sandboxAuditList.innerHTML = `<div class="empty-state-lg"><p>No sandbox audit records.</p></div>`;
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
