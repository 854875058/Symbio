/* ============================================
   Symbio UI — 外部 Agent（Codex / Claude CLI）：会话编排 + 实时双向会话
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ External Agents Page ============
async function loadExternalAgents() {
  await Promise.all([
    loadExternalAgentProviders(),
    loadExternalAgentSessions(),
    loadExternalAgentTranscripts(),
    loadExternalAgentAudit(),
    loadLiveSessions(),
  ]);
}

async function loadExternalAgentProviders() {
  try {
    const res = await fetch(`${API}/external-agents/providers`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.providers = data.providers || [];
    renderExternalAgentProviders();
    return data;
  } catch (e) {
    toast('error', 'External agent providers failed', e.message);
    return null;
  }
}

function renderExternalAgentProviders() {
  const providers = state.externalAgents.providers || [];
  if (dom.externalAgentProviderBadges) {
    dom.externalAgentProviderBadges.innerHTML = providers.map(provider => `
      <span class="sandbox-badge ${provider.installed ? 'external-agent-installed' : 'external-agent-missing'}" title="${esc(provider.path || provider.notes || '')}">
        ${esc(provider.provider_id)}:${provider.installed ? 'ready' : 'missing'}
      </span>
    `).join('');
  }
  if (dom.externalAgentProvider && providers.length) {
    const current = dom.externalAgentProvider.value;
    dom.externalAgentProvider.innerHTML = providers.map(provider => `
      <option value="${esc(provider.provider_id)}">${esc(provider.display_name)} ${provider.installed ? '' : '(missing)'}</option>
    `).join('');
    if (providers.some(provider => provider.provider_id === current)) {
      dom.externalAgentProvider.value = current;
    }
  }
}

async function loadExternalAgentSessions() {
  if (dom.externalAgentSessions) showLoading(dom.externalAgentSessions, 'Loading sessions...');
  try {
    const res = await fetch(`${API}/external-agents/sessions`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.sessions = data.sessions || [];
    if (!state.externalAgents.activeSessionId && state.externalAgents.sessions[0]) {
      state.externalAgents.activeSessionId = state.externalAgents.sessions[0].session_id;
    }
    renderExternalAgentSessions();
    return data;
  } catch (e) {
    toast('error', 'External agent sessions failed', e.message);
    if (dom.externalAgentSessions) dom.externalAgentSessions.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
    return null;
  }
}

function renderExternalAgentSessions() {
  if (!dom.externalAgentSessions) return;
  const sessions = state.externalAgents.sessions || [];
  if (!sessions.length) {
    dom.externalAgentSessions.innerHTML = `<div class="empty-state-lg"><p>No sessions registered.</p></div>`;
    return;
  }
  dom.externalAgentSessions.innerHTML = sessions.map(session => `
    <button class="external-agent-session ${session.session_id === state.externalAgents.activeSessionId ? 'active' : ''}" data-session-id="${esc(session.session_id)}">
      <span class="external-agent-session-main">${esc(session.label || session.provider)}</span>
      <span>${esc(session.provider)}</span>
      <span>${esc(session.external_session_id || 'new handle')}</span>
      <span>${esc(session.sandbox_mode || session.permission_mode || 'default')}</span>
    </button>
  `).join('');
}

function externalAgentSessionPayload() {
  return {
    provider: dom.externalAgentProvider?.value || 'codex',
    label: dom.externalAgentLabel?.value || '',
    workspace: dom.externalAgentWorkspace?.value || '.',
    external_session_id: dom.externalAgentSessionId?.value || '',
    model: dom.externalAgentModel?.value || '',
    sandbox_mode: dom.externalAgentSandboxMode?.value || 'workspace-write',
    approval_policy: dom.externalAgentApprovalPolicy?.value || 'on-request',
  };
}

async function createExternalAgentSession() {
  try {
    const payload = externalAgentSessionPayload();
    const res = await fetch(`${API}/external-agents/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.activeSessionId = data.session.session_id;
    await loadExternalAgentSessions();
    toast('success', 'External session registered', data.session.label || data.session.provider);
    return data.session;
  } catch (e) {
    toast('error', 'Register external session failed', e.message);
    return null;
  }
}

function activeExternalAgentSession() {
  return (state.externalAgents.sessions || []).find(
    session => session.session_id === state.externalAgents.activeSessionId,
  ) || state.externalAgents.sessions[0];
}

async function runExternalAgent(dryRun = false) {
  if (dom.externalAgentResult) {
    dom.externalAgentResult.innerHTML = `<div class="empty-state-lg"><p>${dryRun ? 'Building command preview...' : 'Running external agent...'}</p></div>`;
  }
  try {
    let session = activeExternalAgentSession();
    if (!session) session = await createExternalAgentSession();
    if (!session) throw new Error('No external session available');
    const prompt = dom.externalAgentPrompt?.value || '';
    if (!prompt.trim()) throw new Error('Prompt is required');
    const res = await fetch(`${API}/external-agents/sessions/${encodeURIComponent(session.session_id)}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt,
        dry_run: dryRun,
        model: dom.externalAgentModel?.value || undefined,
        sandbox_mode: dom.externalAgentSandboxMode?.value || undefined,
        approval_policy: dom.externalAgentApprovalPolicy?.value || undefined,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.lastResult = data.result;
    renderExternalAgentResult(data.result);
    await Promise.all([loadExternalAgentSessions(), loadExternalAgentAudit()]);
    toast(data.result.success ? 'success' : 'error', dryRun ? 'Command preview ready' : 'External agent finished', data.result.error || `exit=${data.result.exit_code}`);
  } catch (e) {
    toast('error', 'External agent run failed', e.message);
    if (dom.externalAgentResult) dom.externalAgentResult.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
  }
}

function renderExternalAgentResult(result) {
  if (!dom.externalAgentResult) return;
  const command = (result.command || []).join(' ');
  const statusClass = result.success ? 'success' : 'failed';
  dom.externalAgentResult.innerHTML = `
    <div class="sandbox-result-head">
      <span class="sandbox-result-status ${statusClass}">${result.dry_run ? 'preview' : (result.success ? 'success' : 'failed')}</span>
      <span>${esc(result.provider || '')}</span>
      <span>exit=${esc(result.exit_code)}</span>
      <span>${Math.round(result.duration_ms || 0)}ms</span>
    </div>
    ${result.error ? `<div class="sandbox-error">${esc(result.error)}</div>` : ''}
    <div class="sandbox-output-label">command</div>
    <pre class="sandbox-output">${esc(command)}</pre>
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
  `;
}

async function loadExternalAgentTranscripts() {
  if (dom.externalAgentTranscripts) showLoading(dom.externalAgentTranscripts, '正在扫描本机对话...');
  try {
    const res = await fetch(`${API}/external-agents/transcripts`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.transcripts = data.transcripts || [];
    renderExternalAgentTranscripts();
    return data;
  } catch (e) {
    toast('error', '扫描外部对话失败', e.message);
    if (dom.externalAgentTranscripts) dom.externalAgentTranscripts.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
    return null;
  }
}

function renderExternalAgentTranscripts() {
  if (!dom.externalAgentTranscripts) return;
  const transcripts = state.externalAgents.transcripts || [];
  if (!transcripts.length) {
    dom.externalAgentTranscripts.innerHTML = `<div class="empty-state-lg"><p>没有发现可导入的 Codex / Claude Code 对话。</p></div>`;
    return;
  }
  dom.externalAgentTranscripts.innerHTML = transcripts.map((transcript, index) => {
    const updated = transcript.updated_at ? new Date(transcript.updated_at).toLocaleString() : '未知时间';
    return `
      <article class="external-transcript-card">
        <div class="external-transcript-main">
          <div class="external-transcript-title" title="${esc(transcript.title || transcript.path)}">${esc(transcript.title || '未命名对话')}</div>
          <div class="external-transcript-meta">
            <span>${esc(transcript.provider)}</span>
            <span>${esc(transcript.external_session_id || '无外部 ID')}</span>
            <span>${esc(String(transcript.message_count || 0))} 条消息</span>
            <span>${esc(formatFileSize(transcript.file_size || 0))}</span>
            <span>${esc(updated)}</span>
          </div>
          <div class="external-transcript-path" title="${esc(transcript.path || '')}">${esc(transcript.path || '')}</div>
        </div>
        <button class="btn-outline external-transcript-import" type="button" data-transcript-index="${index}">导入</button>
      </article>
    `;
  }).join('');
}

async function importExternalAgentTranscript(index) {
  const transcript = (state.externalAgents.transcripts || [])[Number(index)];
  if (!transcript) {
    toast('error', '导入失败', '未找到选中的外部对话');
    return null;
  }
  try {
    const res = await fetch(`${API}/external-agents/transcripts/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: transcript.provider,
        path: transcript.path,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    await loadSessions();
    state.currentSession = data.session.id;
    renderSessions();
    await loadSessionMessages(data.session.id);
    await switchPage('chat');
    toast('success', '外部对话已导入', `${data.imported_messages || 0} 条消息`);
    return data;
  } catch (e) {
    toast('error', '导入外部对话失败', e.message);
    return null;
  }
}

async function loadExternalAgentAudit() {
  if (dom.externalAgentAudit) showLoading(dom.externalAgentAudit, 'Loading audit...');
  try {
    const res = await fetch(`${API}/external-agents/audit`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.externalAgents.audit = data.records || [];
    renderExternalAgentAudit(data.records || []);
    return data;
  } catch (e) {
    toast('error', 'External agent audit failed', e.message);
    if (dom.externalAgentAudit) dom.externalAgentAudit.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
    return null;
  }
}

function renderExternalAgentAudit(records) {
  if (!dom.externalAgentAudit) return;
  if (!records.length) {
    dom.externalAgentAudit.innerHTML = `<div class="empty-state-lg"><p>No external agent audit records.</p></div>`;
    return;
  }
  dom.externalAgentAudit.innerHTML = records.map(record => `
    <article class="sandbox-audit-card">
      <div class="sandbox-audit-head">
        <span class="sandbox-audit-command" title="${esc((record.command || []).join(' '))}">${esc((record.command || []).join(' '))}</span>
        <span class="sandbox-result-status ${record.success ? 'success' : 'failed'}">${record.dry_run ? 'preview' : `exit ${record.exit_code}`}</span>
      </div>
      <div class="sandbox-audit-meta">
        <span>${esc(record.provider)}</span>
        <span>${new Date(record.created_at).toLocaleTimeString()}</span>
        <span>${Math.round(record.duration_ms || 0)}ms</span>
      </div>
      ${record.error ? `<div class="sandbox-audit-reason">${esc(record.error)}</div>` : ''}
    </article>
  `).join('');
}

dom.externalAgentSessions?.addEventListener('click', (event) => {
  const card = event.target.closest('.external-agent-session');
  if (!card) return;
  state.externalAgents.activeSessionId = card.dataset.sessionId || '';
  renderExternalAgentSessions();
});

dom.externalAgentTranscripts?.addEventListener('click', (event) => {
  const button = event.target.closest('.external-transcript-import');
  if (!button) return;
  importExternalAgentTranscript(button.dataset.transcriptIndex);
});

$('#btn-refresh-external-agents')?.addEventListener('click', loadExternalAgents);
$('#btn-create-external-agent')?.addEventListener('click', createExternalAgentSession);
$('#btn-run-external-agent-dry')?.addEventListener('click', () => runExternalAgent(true));
$('#btn-run-external-agent')?.addEventListener('click', () => runExternalAgent(false));

// ============ External Agent: 实时双向会话 + 互相调用接力（批次17） ============
state.externalAgents.live = { sessions: [], activeId: '', messagesById: {}, pollTimer: null };

async function loadLiveSessions() {
  const list = $('#live-session-list');
  try {
    const res = await fetch(`${API}/external-agents/live`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const live = state.externalAgents.live;
    live.sessions = data.sessions || [];
    if (!live.activeId && live.sessions[0]) live.activeId = live.sessions[0].session_id;
    renderLiveSessions();
    if (live.activeId) startLivePolling();
  } catch (e) {
    if (list) list.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
  }
}

function renderLiveSessions() {
  const live = state.externalAgents.live;
  const list = $('#live-session-list');
  const sessions = live.sessions || [];
  if (list) {
    list.innerHTML = sessions.length ? sessions.map(s => `
      <button class="external-live-session ${s.session_id === live.activeId ? 'active' : ''}" data-live-id="${esc(s.session_id)}">
        <span class="external-live-session-main">${esc(s.label || s.external_session_id)}</span>
        <span class="external-live-session-sub">${esc(s.provider)} · ${esc(s.external_session_id)}</span>
      </button>`).join('') : `<div class="empty-state-lg"><p>还没有接管任何会话。</p></div>`;
  }
  const active = sessions.find(s => s.session_id === live.activeId);
  const hint = $('#live-active-hint');
  if (hint) hint.textContent = active ? `${active.provider} · ${active.external_session_id}` : '未选择会话';
  renderLiveStream();
}

function renderLiveStream() {
  const stream = $('#live-stream');
  if (!stream) return;
  const live = state.externalAgents.live;
  if (!live.activeId) {
    stream.innerHTML = `<div class="empty-state-lg"><p>接管会话后，这里实时显示双向消息。</p></div>`;
    return;
  }
  const msgs = live.messagesById[live.activeId] || [];
  if (!msgs.length) {
    stream.innerHTML = `<div class="empty-state-lg"><p>等待消息…（发送一句，或在终端里继续该会话）</p></div>`;
    return;
  }
  stream.innerHTML = msgs.map(m => `
    <div class="external-live-msg role-${esc(m.role)}">
      <span class="external-live-msg-role">${esc(m.role)}</span>
      <div class="external-live-msg-body">${esc(m.content)}</div>
    </div>`).join('');
  stream.scrollTop = stream.scrollHeight;
}

async function attachLiveSession() {
  const provider = $('#live-provider')?.value || 'claude-code';
  const externalId = ($('#live-session-id')?.value || '').trim();
  const path = ($('#live-transcript-path')?.value || '').trim();
  const fromStart = $('#live-from-start')?.checked ?? true;
  if (!externalId && !path) { toast('error', '接管失败', '请填外部会话 ID 或 transcript 路径'); return; }
  try {
    const res = await fetch(`${API}/external-agents/live/attach`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, external_session_id: externalId, transcript_path: path, from_start: fromStart }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const live = state.externalAgents.live;
    live.activeId = data.session.session_id;
    live.messagesById[live.activeId] = [];
    await loadLiveSessions();
    await pollLiveSession();
    startLivePolling();
    toast('success', '已接管会话', `${data.session.provider} · ${data.session.external_session_id}`);
  } catch (e) { toast('error', '接管会话失败', e.message); }
}

async function pollLiveSession() {
  const live = state.externalAgents.live;
  const activeId = live.activeId;
  if (!activeId) return;
  const page = document.getElementById('page-external-agents');
  if (page && !page.classList.contains('active')) return; // 仅在该页面可见时轮询
  try {
    const res = await fetch(`${API}/external-agents/live/${encodeURIComponent(activeId)}/poll`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const incoming = data.messages || [];
    if (incoming.length) {
      const bucket = live.messagesById[activeId] || (live.messagesById[activeId] = []);
      bucket.push(...incoming);
      renderLiveStream();
    }
  } catch (e) { /* 轮询期间静默 */ }
}

function startLivePolling() {
  stopLivePolling();
  state.externalAgents.live.pollTimer = setInterval(pollLiveSession, 3000);
}
function stopLivePolling() {
  const live = state.externalAgents.live;
  if (live.pollTimer) { clearInterval(live.pollTimer); live.pollTimer = null; }
}

async function sendLiveMessage() {
  const live = state.externalAgents.live;
  const activeId = live.activeId;
  if (!activeId) { toast('error', '发送失败', '请先接管一个会话'); return; }
  const input = $('#live-input');
  const prompt = (input?.value || '').trim();
  if (!prompt) return;
  const hint = $('#live-active-hint');
  const prevHint = hint ? hint.textContent : '';
  if (hint) hint.textContent = '发送中，等待回应…';
  if (input) input.value = '';
  try {
    const res = await fetch(`${API}/external-agents/live/${encodeURIComponent(activeId)}/send`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const newMsgs = (data.result && data.result.new_messages) || [];
    if (newMsgs.length) {
      const bucket = live.messagesById[activeId] || (live.messagesById[activeId] = []);
      bucket.push(...newMsgs);
      renderLiveStream();
    }
    toast(data.success ? 'success' : 'error', '已发送',
      (data.result && data.result.error) ? data.result.error : `exit=${data.result ? data.result.exit_code : '?'}`);
  } catch (e) {
    toast('error', '发送失败', e.message);
  } finally {
    if (hint) hint.textContent = prevHint;
  }
}

async function detachLiveSession() {
  const live = state.externalAgents.live;
  const activeId = live.activeId;
  if (!activeId) return;
  try {
    const res = await fetch(`${API}/external-agents/live/${encodeURIComponent(activeId)}`, { method: 'DELETE' });
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
    delete live.messagesById[activeId];
    live.activeId = '';
    stopLivePolling();
    await loadLiveSessions();
    toast('success', '已停止接管', '');
  } catch (e) { toast('error', '停止接管失败', e.message); }
}

async function runRelay() {
  const turnsEl = $('#relay-turns');
  const btn = $('#btn-run-relay');
  const payload = {
    seed_prompt: ($('#relay-seed')?.value || '').trim(),
    provider_a: $('#relay-provider-a')?.value || 'codex',
    provider_b: $('#relay-provider-b')?.value || 'claude-code',
    rounds: Math.max(1, Math.min(12, Number($('#relay-rounds')?.value) || 3)),
    role_a: ($('#relay-role-a')?.value || '').trim(),
    role_b: ($('#relay-role-b')?.value || '').trim(),
    dry_run: $('#relay-dry-run')?.checked || false,
  };
  if (!payload.seed_prompt) { toast('error', '接力失败', '请填写初始任务'); return; }
  if (turnsEl) turnsEl.innerHTML = `<div class="empty-state-lg"><p>接力进行中…</p></div>`;
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`${API}/external-agents/relay`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderRelay(data.result);
    toast(data.success ? 'success' : 'error', '接力完成', `${(data.result.turns || []).length} 轮`);
  } catch (e) {
    toast('error', '接力失败', e.message);
    if (turnsEl) turnsEl.innerHTML = `<div class="empty-state-lg"><p>${esc(e.message)}</p></div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderRelay(result) {
  const el = $('#relay-turns');
  if (!el) return;
  const turns = (result && result.turns) || [];
  if (!turns.length) { el.innerHTML = `<div class="empty-state-lg"><p>没有产生任何轮次。</p></div>`; return; }
  el.innerHTML = turns.map(t => `
    <article class="external-relay-turn ${t.success ? '' : 'failed'}">
      <div class="external-relay-turn-head">
        <span class="external-relay-turn-idx">#${t.index + 1}</span>
        <span class="external-relay-turn-provider">${esc(t.provider)}</span>
        <span class="sandbox-result-status ${t.success ? 'success' : 'failed'}">${t.success ? `exit ${t.exit_code}` : 'failed'}</span>
      </div>
      ${t.error ? `<div class="sandbox-error">${esc(t.error)}</div>` : ''}
      <div class="external-relay-turn-body">${esc(t.output || '(无输出)')}</div>
    </article>`).join('');
}

$('#btn-refresh-live-sessions')?.addEventListener('click', loadLiveSessions);
$('#btn-attach-live')?.addEventListener('click', attachLiveSession);
$('#btn-send-live')?.addEventListener('click', sendLiveMessage);
$('#btn-detach-live')?.addEventListener('click', detachLiveSession);
$('#btn-run-relay')?.addEventListener('click', runRelay);
$('#live-input')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendLiveMessage(); }
});
$('#live-session-list')?.addEventListener('click', (e) => {
  const b = e.target.closest('.external-live-session');
  if (!b) return;
  state.externalAgents.live.activeId = b.dataset.liveId || '';
  renderLiveSessions();
  pollLiveSession();
});
