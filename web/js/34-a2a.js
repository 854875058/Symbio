/* ============================================
   Symbio UI — A2A 页：Agent Card 探测、跨 Agent 会话
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ A2A Page ============
async function loadA2A() {
  await Promise.all([
    loadA2AOwnCard(),
    loadA2ASessions(),
    loadA2AInboundTasks(),
  ]);
}

async function loadA2AOwnCard() {
  const el = document.getElementById('a2a-own-card');
  if (!el) return;
  try {
    const res = await fetch(`${window.location.origin}/.well-known/agent.json`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const card = await res.json();
    state.a2a.ownCard = card;
    renderA2ACard(el, card);
  } catch (e) {
    el.innerHTML = `<div class="empty-state-lg"><p>加载失败: ${esc(e.message)}</p></div>`;
  }
}

function renderA2ACard(container, card) {
  if (!card) { container.innerHTML = '<div class="empty-state-lg"><p>无数据</p></div>'; return; }
  const caps = card.capabilities || {};
  const skills = card.skills || [];
  container.innerHTML = `
    <div class="a2a-card-grid">
      <div class="a2a-card-field"><label>名称</label><span>${esc(card.name || '—')}</span></div>
      <div class="a2a-card-field"><label>版本</label><span>${esc(card.version || '—')}</span></div>
      <div class="a2a-card-field"><label>URL</label><span style="font-family:var(--font-mono);font-size:var(--fs-xs);color:var(--accent)">${esc(card.url || '—')}</span></div>
      <div class="a2a-card-field"><label>流式响应</label><span>${caps.streaming ? '✓ 支持' : '✗ 不支持'}</span></div>
      <div class="a2a-card-field"><label>推送通知</label><span>${caps.pushNotifications ? '✓ 支持' : '✗ 不支持'}</span></div>
      <div class="a2a-card-field"><label>状态历史</label><span>${caps.stateTransitionHistory ? '✓ 支持' : '✗ 不支持'}</span></div>
      <div class="a2a-card-field" style="grid-column:1/-1">
        <label>描述</label>
        <span style="line-height:1.5">${esc(card.description || '—')}</span>
      </div>
    </div>
    ${skills.length ? `
    <div style="margin-top:14px">
      <div class="a2a-card-field"><label>可用 Skills</label></div>
      <div class="a2a-skills-list">
        ${skills.map(s => `<span class="a2a-skill-chip" title="${esc(s.description || '')}">${esc(s.name)}</span>`).join('')}
      </div>
    </div>` : ''}
  `;
}

async function loadA2ASessions(limit = 30) {
  const el = document.getElementById('a2a-sessions-list');
  if (!el) return;
  try {
    const res = await fetch(`${API}/a2a/sessions?limit=${limit}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.a2a.sessions = data.sessions || [];
    renderA2ASessions(el, data.sessions || []);
  } catch (e) {
    el.innerHTML = `<div class="empty-state-lg"><p>加载失败: ${esc(e.message)}</p></div>`;
  }
}

function renderA2ASessions(container, sessions) {
  if (!sessions.length) {
    container.innerHTML = '<div class="empty-state-lg"><p>暂无出站会话</p></div>';
    return;
  }
  container.innerHTML = sessions.map(s => `
    <div class="a2a-session-item" id="a2a-session-${esc(s.id)}">
      <div class="a2a-session-meta">
        <div class="a2a-session-name">${esc(s.remote_name || 'remote-agent')}</div>
        <div class="a2a-session-url">${esc(s.remote_url)}</div>
        <div class="a2a-session-time">${formatTime(s.last_active)} · ${s.messages?.length || 0} 条消息</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
        <span class="a2a-state-badge a2a-state-${esc(s.state || 'submitted')}">${a2aStateLabel(s.state)}</span>
        <div class="a2a-send-row" style="margin:0">
          <input class="text-input a2a-send-input" id="a2a-send-msg-${esc(s.id)}" placeholder="继续发送消息...">
          <button class="btn-outline" onclick="sendA2AMessage('${esc(s.id)}')">发送</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function loadA2AInboundTasks(limit = 30) {
  const el = document.getElementById('a2a-inbound-tasks');
  if (!el) return;
  try {
    const res = await fetch(`${API}/a2a/tasks?origin=inbound&limit=${limit}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.a2a.inboundTasks = data.tasks || [];
    renderA2AInboundTasks(el, data.tasks || []);
  } catch (e) {
    el.innerHTML = `<div class="empty-state-lg"><p>加载失败: ${esc(e.message)}</p></div>`;
  }
}

function renderA2AInboundTasks(container, tasks) {
  if (!tasks.length) {
    container.innerHTML = '<div class="empty-state-lg"><p>暂无入站任务</p></div>';
    return;
  }
  container.innerHTML = tasks.map(t => {
    const prompt = t.message?.parts?.[0]?.text || t.message?.text_content || '(no content)';
    const resultText = t.result?.message?.parts?.[0]?.text || t.result?.message?.text_content || '';
    return `
    <div class="a2a-task-item">
      <div class="a2a-task-meta">
        <div class="a2a-task-prompt">${esc(prompt)}</div>
        ${resultText ? `<div style="margin-top:4px;font-size:var(--fs-xs);color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">↳ ${esc(resultText.substring(0,100))}${resultText.length > 100 ? '…' : ''}</div>` : ''}
        <div class="a2a-task-time">${formatTime(t.created_at)} · ${esc(t.id.substring(0,20))}…</div>
      </div>
      <span class="a2a-state-badge a2a-state-${esc(t.state || 'submitted')}">${a2aStateLabel(t.state)}</span>
    </div>
    `;
  }).join('');
}

function a2aStateLabel(state) {
  const map = {
    submitted: '待处理', working: '处理中', completed: '完成',
    failed: '失败', cancelled: '已取消', input_required: '等待输入',
  };
  return map[state] || state || '—';
}

// Probe remote agent
document.getElementById('btn-a2a-probe')?.addEventListener('click', async () => {
  const url = document.getElementById('a2a-probe-url')?.value?.trim();
  const resultEl = document.getElementById('a2a-probe-result');
  if (!url || !resultEl) return;
  resultEl.innerHTML = '<div class="empty-state-lg"><p>探测中...</p></div>';
  try {
    const res = await fetch(`${API}/a2a/probe?url=${encodeURIComponent(url)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderA2ACard(resultEl, data);
    toast('success', '探测成功', data.name || url);
  } catch (e) {
    resultEl.innerHTML = `<div class="empty-state-lg"><p>探测失败: ${esc(e.message)}</p></div>`;
    toast('error', '探测失败', e.message);
  }
});

// Load own card
document.getElementById('btn-load-own-card')?.addEventListener('click', loadA2AOwnCard);

// Create outbound session
document.getElementById('btn-a2a-create-session')?.addEventListener('click', async () => {
  const remoteUrl = document.getElementById('a2a-session-url')?.value?.trim();
  const remoteName = document.getElementById('a2a-session-name')?.value?.trim() || 'remote-agent';
  const initMsg = document.getElementById('a2a-session-init-msg')?.value?.trim();
  const resultEl = document.getElementById('a2a-session-result');
  if (!remoteUrl) { toast('error', '请输入远程 Agent URL', ''); return; }
  if (resultEl) resultEl.innerHTML = '<div class="empty-state-lg"><p>建立连接中...</p></div>';
  try {
    const res = await fetch(`${API}/a2a/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remote_url: remoteUrl, remote_name: remoteName, initial_message: initMsg || null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', '会话已建立', `连接到 ${data.session?.remote_name || remoteUrl}`);
    if (resultEl) {
      const remCard = data.remote_card;
      resultEl.innerHTML = `
        <div class="a2a-card-display" style="margin-top:0">
          <div style="font-size:var(--fs-sm);color:var(--green);margin-bottom:8px">✓ 会话创建成功 · ID: ${esc((data.session?.id || '').substring(0,24))}…</div>
          ${remCard ? `<div style="font-size:var(--fs-sm);color:var(--text-secondary)">远程 Agent: <strong>${esc(remCard.name || remoteName)}</strong> ${esc(remCard.version || '')}</div>` : ''}
          ${data.send_error ? `<div style="font-size:var(--fs-sm);color:var(--red);margin-top:6px">初始消息发送失败: ${esc(data.send_error)}</div>` : ''}
        </div>`;
    }
    await loadA2ASessions();
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<div class="empty-state-lg"><p>失败: ${esc(e.message)}</p></div>`;
    toast('error', '建立会话失败', e.message);
  }
});

async function sendA2AMessage(sessionId) {
  const input = document.getElementById(`a2a-send-msg-${sessionId}`);
  const msg = input?.value?.trim();
  if (!msg) return;
  input.value = '';
  try {
    const res = await fetch(`${API}/a2a/sessions/${encodeURIComponent(sessionId)}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', '消息已发送', '等待远程 Agent 响应');
    await loadA2ASessions();
  } catch (e) {
    toast('error', '发送失败', e.message);
  }
}

document.getElementById('btn-refresh-a2a')?.addEventListener('click', loadA2A);
