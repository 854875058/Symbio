/* ============================================
   Symbio UI — HITL 页：人工审批队列 + 通知渠道与超时策略
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ HITL Page ============
async function loadHitl() {
  showLoading(dom.hitlGrid, '加载审批列表...');
  try {
    const filter = state.hitlFilter;
    const url = filter === 'pending' ? `${API}/hitl/pending` : `${API}/hitl`;
    const res = await fetch(url);
    const data = await res.json();
    state.hitlItems = data.requests || data.items || [];
    renderHitl();
  } catch (e) {
    toast('error', '加载审批列表失败', e.message);
    dom.hitlGrid.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法加载审批列表</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadHitl()">重试</button>
        </div>
      </div>`;
  }
}

function renderHitl() {
  const filtered = state.hitlFilter === 'all'
    ? state.hitlItems
    : state.hitlItems.filter(i => i.status === state.hitlFilter);

  if (filtered.length === 0) {
    // 「暂无待审批」在本产品里是好消息，不是故障——必须说清楚这是正常状态。
    // 而且审批总开关在「模型与配置」页，关掉后本页会永久空白；
    // 不在这里指出去哪开，用户只会以为页面坏了（见 PRODUCT.md 原则 2）。
    const isPending = state.hitlFilter === 'pending';
    dom.hitlGrid.innerHTML = isPending ? `
      <div class="empty-block">
        <p class="empty-block-title">暂无待审批事项</p>
        <p class="empty-block-hint">这是正常状态：Agent 目前没有触发需要你放行的高风险动作。当它要执行危险命令、动用敏感权限或超出预算时，请求会出现在这里，并按配置同步推送到你的 IM。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="switchPage('models')">检查审批开关与通知渠道</button>
        </div>
      </div>` : `
      <div class="empty-block">
        <p class="empty-block-title">没有符合当前筛选的记录</p>
        <p class="empty-block-hint">当前筛选条件是「${esc(hitlStatusLabel(state.hitlFilter))}」。换成「全部」可以看到历史上所有审批请求及其结果。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="document.getElementById('hitl-filter').value='all';document.getElementById('hitl-filter').dispatchEvent(new Event('change'))">查看全部</button>
        </div>
      </div>`;
    return;
  }

  dom.hitlGrid.innerHTML = filtered.map(item => `
    <div class="hitl-card" data-id="${item.id}">
      <div class="hitl-card-header">
        <div class="hitl-card-title">${esc(item.title || item.action || '审批请求')}</div>
        <span class="hitl-card-status hitl-status-${item.status}">${hitlStatusLabel(item.status)}</span>
      </div>
      <div class="hitl-card-desc">${esc(item.description || item.reason || '')}</div>
      <div class="hitl-card-meta">
        <span>${esc(item.agent || item.source || 'system')}</span>
        <span>${formatTime(item.created_at || item.timestamp)}</span>
        ${hitlNotifyBadge(item.notification_status)}
      </div>
      <div class="hitl-evidence-stack">
        ${renderPlannerReviewerControls(item)}
        ${renderApprovalContextPanel(item)}
        ${renderWorkflowPolicyPanel(item)}
        ${renderVerificationEvidencePanel(item)}
      </div>
      ${item.status === 'pending' ? `
        <div class="hitl-card-actions">
          <button class="btn-approve" data-id="${item.id}">通过</button>
          <button class="btn-reject" data-id="${item.id}">拒绝</button>
          <button class="btn-repush" data-id="${item.id}" title="把审批卡重新推送到已登录微信">重推微信</button>
        </div>
      ` : ''}
    </div>
  `).join('');
  attachReviewControlsInteractions(dom.hitlGrid);

  // Attach action listeners
  dom.hitlGrid.querySelectorAll('.btn-approve').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      approveHitl(btn.dataset.id);
    });
  });
  dom.hitlGrid.querySelectorAll('.btn-reject').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      rejectHitl(btn.dataset.id);
    });
  });
  dom.hitlGrid.querySelectorAll('.btn-repush').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      repushHitl(btn.dataset.id);
    });
  });
}

function hitlStatusLabel(status) {
  const map = { pending: '待审批', approved: '已通过', rejected: '已拒绝' };
  return map[status] || status;
}

function hitlNotifyBadge(status) {
  const map = {
    sent: ['已推送微信', 'notify-sent'],
    prepared: ['待推送', 'notify-prepared'],
    failed: ['推送失败', 'notify-failed'],
    not_configured: ['未配置推送', 'notify-none'],
  };
  const [label, cls] = map[status] || [null, null];
  if (!label) return '';
  return `<span class="hitl-notify-badge ${cls}">${label}</span>`;
}

async function repushHitl(id) {
  try {
    const res = await fetch(`${API}/hitl/${id}/repush-wechat`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      const ok = data.success;
      toast(ok ? 'success' : 'info', ok ? '已重推到微信' : '已尝试重推',
        (data.note && data.note.delivery_status) || '');
      loadHitl();
    } else {
      toast('error', '重推失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '重推失败', e.message);
  }
}

async function approveHitl(id) {
  try {
    const res = await fetch(`${API}/hitl/${id}/approve`, { method: 'POST' });
    if (res.ok) {
      toast('success', '已通过', '审批请求已通过');
      loadHitl();
    } else {
      const data = await res.json();
      toast('error', '操作失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '操作失败', e.message);
  }
}

async function rejectHitl(id) {
  try {
    const res = await fetch(`${API}/hitl/${id}/reject`, { method: 'POST' });
    if (res.ok) {
      toast('success', '已拒绝', '审批请求已拒绝');
      loadHitl();
    } else {
      const data = await res.json();
      toast('error', '操作失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '操作失败', e.message);
  }
}

// HITL filter
dom.hitlFilter?.addEventListener('change', (e) => {
  state.hitlFilter = e.target.value;
  loadHitl();
});
$('#btn-refresh-hitl')?.addEventListener('click', loadHitl);

// ============ HITL Channel Management ============
async function loadHitlChannels() {
  const list = document.getElementById('hitl-channels-list');
  if (!list) return;
  try {
    const res = await fetch(`${API}/hitl/channels/list`);
    const data = await res.json();
    renderHitlChannels(list, data.channels || []);
  } catch (e) {
    if (list) {
      list.innerHTML = `
        <div class="empty-block is-inline is-error">
          <p class="empty-block-title">无法加载通知渠道</p>
          <p class="empty-block-hint">${esc(e.message)}</p>
          <div class="empty-block-actions">
            <button class="btn-outline" type="button" onclick="loadHitlChannels()">重试</button>
          </div>
        </div>`;
    }
  }
}

function renderHitlChannels(container, channels) {
  if (!channels.length) {
    container.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">暂无通知渠道</p>
        <p class="empty-block-hint">没有渠道时，审批请求只会停留在本页面，你必须主动打开才能看到。配置一个渠道后，Agent 卡住等你放行时会主动推送到 IM——这是「无人值守」能成立的前提。支持企业微信 / 飞书 / 钉钉 / Telegram / QQ。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="document.getElementById('btn-add-channel')?.click()">添加渠道</button>
        </div>
      </div>`;
    return;
  }
  container.innerHTML = channels.map(ch => `
    <div class="hitl-channel-item">
      <div class="hitl-channel-meta">
        <div class="hitl-channel-name">${esc(ch.display_name || ch.platform)}</div>
        <div class="hitl-channel-endpoint">${esc(ch.endpoint || ch.chat_id || '—')}</div>
        <div style="font-size:var(--fs-xs);color:var(--text-tertiary);margin-top:2px">
          ${ch.has_access_token ? '🔑 Token' : ''} ${ch.has_secret ? '🔒 Secret' : ''}
        </div>
      </div>
      <span class="hitl-channel-badge ${ch.enabled ? '' : 'disabled'}">${ch.enabled ? '启用' : '停用'}</span>
      <div class="hitl-channel-actions">
        ${ch.deletable ? `<button class="btn-outline" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="deleteHitlChannel('${escJs(ch.id)}')">删除</button>` : ''}
      </div>
    </div>
  `).join('');
}

document.getElementById('btn-add-channel')?.addEventListener('click', () => {
  const form = document.getElementById('hitl-add-channel-form');
  if (form) form.style.display = form.style.display === 'none' ? 'block' : 'none';
});
document.getElementById('btn-cancel-channel')?.addEventListener('click', () => {
  const form = document.getElementById('hitl-add-channel-form');
  if (form) form.style.display = 'none';
});
document.getElementById('btn-test-channel')?.addEventListener('click', async () => {
  const resultEl = document.getElementById('ch-test-result');
  const body = gatherChannelForm();
  if (!body) return;
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-tertiary)">发送测试中...</span>';
  try {
    const res = await fetch(`${API}/hitl/channels/test`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await res.json();
    if (resultEl) {
      const ok = data.success;
      resultEl.innerHTML = `<span style="color:${ok ? 'var(--green)' : 'var(--red)'}">${ok ? '✓ 测试成功' : `✗ 失败: ${esc(data.error || data.delivery_status)}`}</span>`;
    }
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`;
  }
});
document.getElementById('btn-save-channel')?.addEventListener('click', async () => {
  const body = gatherChannelForm();
  if (!body) return;
  try {
    const res = await fetch(`${API}/hitl/channels`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', '渠道已保存', data.channel?.display_name || body.platform);
    document.getElementById('hitl-add-channel-form').style.display = 'none';
    await loadHitlChannels();
  } catch (e) {
    toast('error', '保存失败', e.message);
  }
});

function gatherChannelForm() {
  return {
    platform: document.getElementById('ch-platform')?.value || '',
    endpoint: document.getElementById('ch-endpoint')?.value?.trim() || '',
    access_token: document.getElementById('ch-access-token')?.value?.trim() || '',
    chat_id: document.getElementById('ch-chat-id')?.value?.trim() || '',
    secret: document.getElementById('ch-secret')?.value?.trim() || '',
    chat_type: document.getElementById('ch-chat-type')?.value || 'group',
    enabled: true,
  };
}

async function deleteHitlChannel(channelId) {
  if (!confirmDanger('删除这个通知渠道？', '删除后，需要你放行的审批请求将不再推送到该渠道。如果这是唯一的渠道，Agent 卡住等待时你不会收到任何通知，只能靠主动打开审批中心才能发现。')) return;
  try {
    const res = await fetch(`${API}/hitl/channels/${encodeURIComponent(channelId)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', '渠道已删除', '');
    await loadHitlChannels();
  } catch (e) {
    toast('error', '删除失败', e.message);
  }
}

document.getElementById('btn-refresh-channels')?.addEventListener('click', loadHitlChannels);

// 加载已保存的超时策略到表单
async function loadHitlTimeoutPolicy() {
  try {
    const res = await fetch(`${API}/hitl/timeout/policy`);
    if (!res.ok) return;
    const data = await res.json();
    const set = (id, v) => { const el = document.getElementById(id); if (el != null && v != null) el.value = v; };
    set('hitl-timeout-action', data.timeout_action);
    set('hitl-escalation-target', data.escalation_target);
    set('hitl-max-escalations', data.max_escalations);
    set('hitl-timeout-seconds', data.approval_timeout);
  } catch (e) { /* ignore */ }
}

// 保存超时策略
document.getElementById('btn-save-timeout-policy')?.addEventListener('click', async () => {
  const resultEl = document.getElementById('hitl-timeout-result');
  const body = {
    timeout_action: document.getElementById('hitl-timeout-action')?.value || 'reject',
    escalation_target: document.getElementById('hitl-escalation-target')?.value || '',
    max_escalations: parseInt(document.getElementById('hitl-max-escalations')?.value || '1'),
    approval_timeout: parseInt(document.getElementById('hitl-timeout-seconds')?.value || '300'),
  };
  try {
    const res = await fetch(`${API}/hitl/timeout/policy`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', '超时策略已保存', `默认动作：${body.timeout_action}`);
    if (resultEl) resultEl.innerHTML = '<span style="color:var(--green)">✓ 策略已写入 symbio.yaml</span>';
  } catch (e) {
    toast('error', '保存失败', e.message);
  }
});

// Timeout check（立即检查超时，留空 action 时用已保存的默认策略）
document.getElementById('btn-check-timeouts')?.addEventListener('click', async () => {
  const resultEl = document.getElementById('hitl-timeout-result');
  const seconds = parseInt(document.getElementById('hitl-timeout-seconds')?.value || '300');
  const action = document.getElementById('hitl-timeout-action')?.value || '';
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-tertiary)">检查中...</span>';
  try {
    const res = await fetch(`${API}/hitl/timeout/check?max_age_seconds=${seconds}&action=${action}`);
    const data = await res.json();
    if (resultEl) {
      const actionLabel = { reject: '自动拒绝', approve: '自动通过', escalate: '转交管理员' }[data.action] || data.action;
      resultEl.innerHTML = `<span style="color:var(--green)">✓ 检查 ${data.checked} 个，处理 ${data.handled} 个（${actionLabel}）</span>`;
    }
    if (data.handled > 0) await loadHitl();
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`;
  }
});
