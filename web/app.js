/* ============================================
   Symbio — Neural Command Center
   ============================================ */

const API = 'http://localhost:9090/api';
const WS_URL = 'ws://localhost:9090/ws/chat';

// ============ State ============
const state = {
  page: 'chat',
  sessions: [{ id: 'default', title: '新对话', time: '刚刚' }],
  currentSession: 'default',
  messages: [],
  models: [],
  tasks: [],
  taskFilter: 'all',
  memories: [],
  tokens: { input: 0, output: 0, total: 0 },
  cost: 0,
  connected: false,
  ws: null,
  wsReconnectDelay: 1000,
  wsMaxReconnectDelay: 30000,
  wsReconnectTimer: null,
  streaming: false,
  streamContent: '',
};

// ============ DOM ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  navTabs: $$('.nav-tab'),
  pages: $$('.page'),
  sessionsList: $('#sessions-list'),
  welcome: $('#welcome-screen'),
  messages: $('#messages-container'),
  input: $('#message-input'),
  sendBtn: $('#send-btn'),
  newChat: $('#btn-new-chat'),
  chips: $$('.chip'),
  togglePanel: $('#toggle-panel'),
  panel: $('#context-panel'),
  toast: $('#toast-container'),
  modelsGrid: $('#models-grid'),
  tasksGrid: $('#tasks-grid'),
  memoryGrid: $('#memory-grid'),
  memorySearch: $('#memory-search'),
};

// ============ Navigation ============
function switchPage(name) {
  state.page = name;
  dom.navTabs.forEach(t => t.classList.toggle('active', t.dataset.page === name));
  dom.pages.forEach(p => p.classList.toggle('active', p.id === `page-${name}`));

  // Load data when switching to a page
  if (name === 'models') loadModels();
  if (name === 'tasks') loadTasks();
  if (name === 'memory') loadMemories();
}

dom.navTabs.forEach(tab => {
  tab.addEventListener('click', () => switchPage(tab.dataset.page));
});

// ============ Sessions ============
function renderSessions() {
  dom.sessionsList.innerHTML = state.sessions.map(s => `
    <div class="session-item ${s.id === state.currentSession ? 'active' : ''}" data-id="${s.id}">
      <div class="session-dot"></div>
      <div class="session-info">
        <div class="session-title">${esc(s.title)}</div>
        <div class="session-time">${s.time}</div>
      </div>
    </div>
  `).join('');

  dom.sessionsList.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', () => {
      state.currentSession = el.dataset.id;
      renderSessions();
    });
  });
}

dom.newChat?.addEventListener('click', () => {
  const id = `s-${Date.now()}`;
  state.sessions.unshift({ id, title: '新对话', time: '刚刚' });
  state.currentSession = id;
  state.messages = [];
  renderSessions();
  renderMessages();
});

// ============ Messages ============
function renderMessages() {
  if (state.messages.length === 0) {
    dom.welcome.style.display = 'flex';
    const msgs = dom.messages.querySelectorAll('.message');
    msgs.forEach(m => m.remove());
    return;
  }

  dom.welcome.style.display = 'none';

  const existing = dom.messages.querySelectorAll('.message');
  existing.forEach(m => m.remove());

  state.messages.forEach(msg => {
    const el = createMessageEl(msg);
    dom.messages.appendChild(el);
  });

  dom.messages.scrollTop = dom.messages.scrollHeight;
}

function createMessageEl(msg) {
  const div = document.createElement('div');
  div.className = `message ${msg.role}`;

  const avatar = msg.role === 'user' ? 'U' : 'S';
  const time = new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

  div.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="message-bubble">${formatContent(msg.content)}</div>
      <div class="message-meta">
        <span>${time}</span>
        ${msg.tokens ? `<span>${msg.tokens} tokens</span>` : ''}
      </div>
    </div>
  `;
  return div;
}

function createStreamingEl() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'streaming-msg';
  div.innerHTML = `
    <div class="message-avatar">S</div>
    <div class="message-content">
      <div class="message-bubble"><span class="cursor-blink"></span></div>
      <div class="message-meta"><span></span></div>
    </div>
  `;
  dom.messages.appendChild(div);
  return div;
}

function updateStreamingEl(text) {
  const el = document.getElementById('streaming-msg');
  if (!el) return;
  const bubble = el.querySelector('.message-bubble');
  bubble.innerHTML = formatContent(text) + '<span class="cursor-blink"></span>';
  dom.messages.scrollTop = dom.messages.scrollHeight;
}

function finalizeStreamingEl(fullContent, tokenUsage) {
  const el = document.getElementById('streaming-msg');
  if (!el) return;
  el.removeAttribute('id');
  const bubble = el.querySelector('.message-bubble');
  bubble.innerHTML = formatContent(fullContent);
  const meta = el.querySelector('.message-meta');
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  meta.innerHTML = `<span>${time}</span>${tokenUsage ? `<span>${tokenUsage.total} tokens</span>` : ''}`;
}

function formatContent(text) {
  if (!text) return '';
  let html = esc(text);
  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ============ WebSocket ============
function connectWebSocket() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  try {
    state.ws = new WebSocket(WS_URL);

    state.ws.onopen = () => {
      state.connected = true;
      state.wsReconnectDelay = 1000;
      updateConnectionStatus(true);
      console.log('[WS] 连接建立');
    };

    state.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        if (data.type === 'token') {
          state.streamContent += data.content;
          updateStreamingEl(state.streamContent);
        } else if (data.type === 'done') {
          state.streaming = false;
          const tokenUsage = data.token_usage || null;
          finalizeStreamingEl(state.streamContent, tokenUsage);

          // Record message
          state.messages.push({
            role: 'assistant',
            content: state.streamContent,
            timestamp: Date.now(),
            tokens: tokenUsage?.total || 0,
          });

          // Update token stats
          if (tokenUsage) {
            state.tokens.input += tokenUsage.input || 0;
            state.tokens.output += tokenUsage.output || 0;
            state.tokens.total += tokenUsage.total || 0;
            updateStatus();
          }

          state.streamContent = '';
          dom.sendBtn.disabled = !dom.input.value.trim();
        } else if (data.type === 'error') {
          state.streaming = false;
          state.streamContent = '';
          removeStreaming();
          toast('error', 'LLM 错误', data.content);
          dom.sendBtn.disabled = !dom.input.value.trim();
        }
      } catch (e) {
        console.error('[WS] 消息解析失败:', e);
      }
    };

    state.ws.onclose = () => {
      state.connected = false;
      updateConnectionStatus(false);
      console.log(`[WS] 连接断开，${state.wsReconnectDelay / 1000}s 后重连`);
      scheduleReconnect();
    };

    state.ws.onerror = (err) => {
      console.error('[WS] 错误:', err);
    };
  } catch (e) {
    console.error('[WS] 创建失败:', e);
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (state.wsReconnectTimer) return;
  state.wsReconnectTimer = setTimeout(() => {
    state.wsReconnectTimer = null;
    connectWebSocket();
    // Exponential backoff
    state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 2, state.wsMaxReconnectDelay);
  }, state.wsReconnectDelay);
}

function sendViaWebSocket(content) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    return false;
  }
  state.streaming = true;
  state.streamContent = '';
  createStreamingEl();

  state.ws.send(JSON.stringify({
    content: content,
    session_id: state.currentSession,
  }));
  return true;
}

function removeStreaming() {
  document.getElementById('streaming-msg')?.remove();
}

function updateConnectionStatus(online) {
  const dot = document.querySelector('.status-dot');
  if (dot) {
    dot.className = `status-dot ${online ? 'online' : 'offline'}`;
  }
  const label = document.querySelector('.status-left span:last-child');
  if (label) {
    label.textContent = online ? '已连接' : '未连接';
  }
}

// ============ Send Message ============
async function sendMessage() {
  const content = dom.input.value.trim();
  if (!content || state.streaming) return;

  // Add user message
  state.messages.push({
    role: 'user',
    content,
    timestamp: Date.now(),
  });
  renderMessages();

  dom.input.value = '';
  dom.input.style.height = 'auto';
  dom.sendBtn.disabled = true;

  // Hide welcome
  if (dom.welcome) dom.welcome.style.display = 'none';

  // Try WebSocket first (streaming)
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    sendViaWebSocket(content);
    return;
  }

  // Fallback to REST API
  showTyping();

  try {
    const res = await fetch(`${API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: content, session_id: state.currentSession }),
    });
    const data = await res.json();

    removeTyping();

    state.messages.push({
      role: 'assistant',
      content: data.content || '无响应',
      timestamp: Date.now(),
      tokens: data.token_usage?.total || 0,
    });

    if (data.token_usage) {
      state.tokens.input += data.token_usage.input || 0;
      state.tokens.output += data.token_usage.output || 0;
      state.tokens.total += data.token_usage.total || 0;
      updateStatus();
    }

    renderMessages();
  } catch (e) {
    removeTyping();
    state.messages.push({
      role: 'assistant',
      content: `错误: ${e.message}`,
      timestamp: Date.now(),
    });
    renderMessages();
    toast('error', '发送失败', e.message);
  }
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'message assistant';
  el.id = 'typing';
  el.innerHTML = `
    <div class="message-avatar">S</div>
    <div class="message-content">
      <div class="message-bubble">
        <div class="typing"><span></span><span></span><span></span></div>
      </div>
    </div>
  `;
  dom.messages.appendChild(el);
  dom.messages.scrollTop = dom.messages.scrollHeight;
}

function removeTyping() {
  document.getElementById('typing')?.remove();
}

// Input handling
dom.input?.addEventListener('input', () => {
  dom.input.style.height = 'auto';
  dom.input.style.height = Math.min(dom.input.scrollHeight, 180) + 'px';
  dom.sendBtn.disabled = !dom.input.value.trim() || state.streaming;
});

dom.input?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendMessage();
  }
});

dom.sendBtn?.addEventListener('click', sendMessage);

// Chips
dom.chips.forEach(chip => {
  chip.addEventListener('click', () => {
    dom.input.value = chip.dataset.prompt;
    dom.input.focus();
    dom.sendBtn.disabled = false;
  });
});

// Panel toggle
dom.togglePanel?.addEventListener('click', () => {
  dom.panel.classList.toggle('hidden');
});

// ============ Status ============
function updateStatus() {
  const el = document.querySelector('.status-center');
  if (el) {
    el.innerHTML = `
      <span>Token: <strong>${state.tokens.total}</strong></span>
      <span class="sep">·</span>
      <span>成本: <strong>$${state.cost.toFixed(2)}</strong></span>
    `;
  }
}

// ============ Health Check ============
async function checkHealth() {
  try {
    const res = await fetch(`${API}/health`);
    const data = await res.json();
    state.connected = data.status === 'ok';
  } catch {
    state.connected = false;
  }
  updateConnectionStatus(state.connected);
}

// ============ Models Page ============
async function loadModels() {
  try {
    const res = await fetch(`${API}/models`);
    const data = await res.json();
    state.models = data.models || [];
    renderModels();
  } catch (e) {
    toast('error', '加载模型失败', e.message);
  }
}

function renderModels() {
  if (state.models.length === 0) {
    dom.modelsGrid.innerHTML = `
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2">
          <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>
        </svg>
        <p>尚未添加任何模型</p>
      </div>
    `;
    return;
  }

  dom.modelsGrid.innerHTML = state.models.map(m => `
    <div class="model-card" data-id="${m.id}">
      <div class="model-card-header">
        <div class="model-card-info">
          <div class="model-card-name">${esc(m.display_name || m.model_id)}</div>
          <div class="model-card-provider">${esc(m.provider)} / ${esc(m.model_id)}</div>
        </div>
        <div class="model-card-actions">
          <button class="btn-icon model-test-btn" data-id="${m.id}" title="测试连接">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          </button>
          <button class="btn-icon btn-icon-danger model-delete-btn" data-id="${m.id}" title="删除">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
          </button>
        </div>
      </div>
      <div class="model-card-meta">
        <span class="badge ${m.enabled ? 'badge-green' : 'badge-gray'}">${m.enabled ? '启用' : '禁用'}</span>
        <span class="badge">${esc(m.base_url || '默认')}</span>
      </div>
      <div class="model-card-test-result" id="test-result-${m.id}"></div>
    </div>
  `).join('');

  // Attach event listeners
  dom.modelsGrid.querySelectorAll('.model-test-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      testModel(btn.dataset.id);
    });
  });

  dom.modelsGrid.querySelectorAll('.model-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteModel(btn.dataset.id);
    });
  });
}

async function testModel(modelId) {
  const resultEl = document.getElementById(`test-result-${modelId}`);
  if (resultEl) {
    resultEl.innerHTML = '<div class="testing">测试中...</div>';
  }

  try {
    const res = await fetch(`${API}/models/${modelId}/test`, { method: 'POST' });
    const data = await res.json();
    if (resultEl) {
      resultEl.innerHTML = `<div class="test-result ${data.success ? 'test-ok' : 'test-fail'}">${esc(data.message)}</div>`;
    }
    if (data.success) {
      toast('success', '连接测试', data.message);
    } else {
      toast('error', '连接测试', data.message);
    }
  } catch (e) {
    if (resultEl) {
      resultEl.innerHTML = `<div class="test-result test-fail">请求失败: ${esc(e.message)}</div>`;
    }
    toast('error', '测试失败', e.message);
  }
}

async function deleteModel(modelId) {
  const model = state.models.find(m => m.id === modelId);
  const name = model?.display_name || model?.model_id || modelId;
  if (!confirm(`确定要删除模型 "${name}" 吗？`)) return;

  try {
    const res = await fetch(`${API}/models/${modelId}`, { method: 'DELETE' });
    if (res.ok) {
      toast('success', '已删除', `模型 ${name} 已删除`);
      loadModels();
    } else {
      const data = await res.json();
      toast('error', '删除失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '删除失败', e.message);
  }
}

// Add Model Modal
const btnAddModel = $('#btn-add-model');
btnAddModel?.addEventListener('click', showAddModelModal);

function showAddModelModal() {
  // Remove existing modal
  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>添加模型</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>提供商</label>
          <select id="modal-provider">
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI 兼容</option>
            <option value="ollama">Ollama 本地</option>
          </select>
        </div>
        <div class="form-group">
          <label>模型 ID</label>
          <input type="text" id="modal-model-id" placeholder="例: claude-sonnet-4-20250514">
        </div>
        <div class="form-group">
          <label>显示名称</label>
          <input type="text" id="modal-display-name" placeholder="例: Claude Sonnet 4">
        </div>
        <div class="form-group">
          <label>API Key</label>
          <input type="password" id="modal-api-key" placeholder="sk-...">
        </div>
        <div class="form-group">
          <label>Base URL</label>
          <input type="text" id="modal-base-url" placeholder="https://api.anthropic.com" value="https://api.anthropic.com">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">保存</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  // Provider change updates base URL placeholder
  const providerEl = overlay.querySelector('#modal-provider');
  const baseUrlEl = overlay.querySelector('#modal-base-url');
  providerEl.addEventListener('change', () => {
    const urlMap = {
      anthropic: 'https://api.anthropic.com',
      openai: 'https://api.openai.com/v1',
      ollama: 'http://localhost:11434',
    };
    baseUrlEl.value = urlMap[providerEl.value] || '';
    baseUrlEl.placeholder = urlMap[providerEl.value] || '';
  });

  // Close
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  // Save
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const modelId = overlay.querySelector('#modal-model-id').value.trim();
    const provider = providerEl.value;
    const displayName = overlay.querySelector('#modal-display-name').value.trim();
    const apiKey = overlay.querySelector('#modal-api-key').value.trim();
    const baseUrl = baseUrlEl.value.trim();

    if (!modelId) {
      toast('error', '验证失败', '模型 ID 不能为空');
      return;
    }

    try {
      const res = await fetch(`${API}/models`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model_id: modelId,
          provider: provider,
          display_name: displayName,
          api_key: apiKey,
          base_url: baseUrl,
        }),
      });

      if (res.ok) {
        overlay.remove();
        toast('success', '已添加', `模型 ${displayName || modelId} 已添加`);
        loadModels();
      } else {
        const data = await res.json();
        toast('error', '添加失败', data.detail || '未知错误');
      }
    } catch (e) {
      toast('error', '添加失败', e.message);
    }
  });
}

// ============ Tasks Page ============
async function loadTasks() {
  try {
    const statusParam = state.taskFilter !== 'all' ? `?status=${state.taskFilter}` : '';
    const res = await fetch(`${API}/tasks${statusParam}`);
    const data = await res.json();
    state.tasks = data.tasks || [];
    renderTasks();
  } catch (e) {
    toast('error', '加载任务失败', e.message);
  }
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
    dom.tasksGrid.innerHTML = `
      ${filtersHtml}
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
        <p>暂无${state.taskFilter === 'all' ? '' : '匹配的'}任务</p>
      </div>
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
      </div>
    `).join('')}
  `;

  attachFilterListeners();

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

function formatTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoStr;
  }
}

async function showTaskDetail(taskId) {
  const task = state.tasks.find(t => t.id === taskId);
  if (!task) return;

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
          <button class="icon-btn modal-close-btn">
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
          <div class="detail-label">Agent</div>
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
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-close-btn-bottom">关闭</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-close-btn-bottom').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Refresh button
$('#page-tasks .btn-outline')?.addEventListener('click', loadTasks);

// ============ Memory Page ============
async function loadMemories(query) {
  try {
    const url = query ? `${API}/memory/search?q=${encodeURIComponent(query)}` : `${API}/memory`;
    const res = await fetch(url);
    const data = await res.json();
    state.memories = data.memories || [];
    renderMemories(query);
  } catch (e) {
    toast('error', '加载记忆失败', e.message);
  }
}

function renderMemories(query) {
  if (state.memories.length === 0) {
    dom.memoryGrid.innerHTML = `
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        <p>${query ? '未找到匹配的记忆' : '记忆库为空'}</p>
        <span class="empty-hint">${query ? '尝试不同的搜索词' : '对话中的重要信息会自动存储'}</span>
      </div>
    `;
    return;
  }

  dom.memoryGrid.innerHTML = state.memories.map(m => `
    <div class="memory-card" data-id="${m.id}">
      <div class="memory-card-header">
        <div class="memory-card-title">${esc(m.title)}</div>
        <div class="memory-card-importance">
          <div class="importance-bar">
            <div class="importance-fill" style="width:${(m.importance || 0) * 100}%"></div>
          </div>
        </div>
      </div>
      <div class="memory-card-snippet">${esc(m.content)}</div>
      <div class="memory-card-meta">
        <div class="memory-tags">
          ${(m.tags || []).map(t => `<span class="memory-tag">${esc(t)}</span>`).join('')}
        </div>
        <div class="memory-info">
          <span class="memory-source">${esc(m.source || 'chat')}</span>
          ${m.relevance !== undefined ? `<span class="memory-relevance">相关度 ${(m.relevance * 100).toFixed(0)}%</span>` : ''}
        </div>
      </div>
    </div>
  `).join('');

  dom.memoryGrid.querySelectorAll('.memory-card').forEach(card => {
    card.addEventListener('click', () => showMemoryDetail(card.dataset.id));
  });
}

function showMemoryDetail(memoryId) {
  const mem = state.memories.find(m => m.id === memoryId);
  if (!mem) return;

  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <div class="modal-header">
        <h3>${esc(mem.title)}</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-section">
          <div class="detail-label">内容</div>
          <div class="detail-value">${esc(mem.content)}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">标签</div>
          <div class="memory-tags">${(mem.tags || []).map(t => `<span class="memory-tag">${esc(t)}</span>`).join(' ')}</div>
        </div>
        <div class="detail-row">
          <div class="detail-section">
            <div class="detail-label">来源</div>
            <div class="detail-value">${esc(mem.source || 'chat')}</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">重要度</div>
            <div class="detail-value">${((mem.importance || 0) * 100).toFixed(0)}%</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">访问次数</div>
            <div class="detail-value">${mem.access_count || 0}</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">创建时间</div>
            <div class="detail-value">${formatTime(mem.created_at)}</div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-close-btn-bottom">关闭</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-close-btn-bottom').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Memory search
let memorySearchTimer = null;
dom.memorySearch?.addEventListener('input', () => {
  clearTimeout(memorySearchTimer);
  memorySearchTimer = setTimeout(() => {
    const q = dom.memorySearch.value.trim();
    loadMemories(q || undefined);
  }, 300);
});

// ============ Toast ============
function toast(type, title, msg) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `
    <div style="flex:1">
      <div style="font-weight:600;font-size:0.85rem">${esc(title)}</div>
      ${msg ? `<div style="font-size:0.75rem;color:var(--text-secondary);margin-top:2px">${esc(msg)}</div>` : ''}
    </div>
  `;
  dom.toast.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(40px)';
    el.style.transition = 'all 300ms';
    setTimeout(() => el.remove(), 300);
  }, 4000);
}

// ============ Utility ============
function esc(text) {
  if (!text) return '';
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ============ Init ============
async function init() {
  renderSessions();
  await checkHealth();
  connectWebSocket();
  setInterval(checkHealth, 30000);
  console.log('Symbio UI initialized');
}

document.addEventListener('DOMContentLoaded', init);
