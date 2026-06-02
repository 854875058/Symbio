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
  skills: [],
  skillDetail: null,
  tokens: { input: 0, output: 0, total: 0 },
  cost: 0,
  connected: false,
  ws: null,
  wsReconnectDelay: 1000,
  wsMaxReconnectDelay: 30000,
  wsReconnectTimer: null,
  streaming: false,
  streamContent: '',
  config: {},
  hitlItems: [],
  hitlFilter: 'pending',
  theme: localStorage.getItem('symbio-theme') || 'dark',
  pagesLoaded: {},
  virtualScrollEnabled: false,
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
  skillsGrid: $('#skills-grid'),
  skillsSearch: $('#skills-search'),
  btnImportSkill: $('#btn-import-skill'),
  configSection: $('#llm-config-section'),
  themeToggle: $('#theme-toggle'),
  dashboardCards: $('#dashboard-cards'),
  tokenBarChart: $('#token-bar-chart'),
  hitlGrid: $('#hitl-grid'),
  hitlFilter: $('#hitl-filter'),
};

// ============ Navigation ============
async function switchPage(name) {
  state.page = name;
  dom.navTabs.forEach(t => t.classList.toggle('active', t.dataset.page === name));
  dom.pages.forEach(p => p.classList.toggle('active', p.id === `page-${name}`));

  // Lazy load: only load data on first visit per page (avoids re-fetching on tab switch)
  if (name === 'models' && !state.pagesLoaded.models) { state.pagesLoaded.models = true; await loadModels(); await loadConfig(); }
  else if (name === 'models') { await loadModels(); }
  if (name === 'tasks' && !state.pagesLoaded.tasks) { state.pagesLoaded.tasks = true; await loadTasks(); }
  if (name === 'memory' && !state.pagesLoaded.memory) { state.pagesLoaded.memory = true; await loadMemories(); }
  if (name === 'skills' && !state.pagesLoaded.skills) { state.pagesLoaded.skills = true; await loadSkills(); }
  if (name === 'dashboard') await loadDashboard();
  if (name === 'hitl') await loadHitl();
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
      loadSessionMessages(el.dataset.id);
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
        ${msg.tokens ? `<span class="message-token-badge">${msg.tokens} tokens</span>` : ''}
        <button class="message-copy-btn" title="复制消息" data-content="${esc(msg.content).replace(/"/g, '&quot;')}">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        </button>
      </div>
    </div>
  `;

  // Attach copy handler
  div.querySelector('.message-copy-btn')?.addEventListener('click', function() {
    const text = this.dataset.content.replace(/&quot;/g, '"');
    navigator.clipboard.writeText(text).then(() => {
      toast('success', '已复制', '消息内容已复制到剪贴板');
    }).catch(() => {
      toast('error', '复制失败', '无法访问剪贴板');
    });
  });

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
  // Add subtle glow during streaming
  el.classList.add('streaming-glow');
  // Smooth scroll to bottom
  requestAnimationFrame(() => {
    dom.messages.scrollTop = dom.messages.scrollHeight;
  });
}

function finalizeStreamingEl(fullContent, tokenUsage) {
  const el = document.getElementById('streaming-msg');
  if (!el) return;
  el.removeAttribute('id');
  const bubble = el.querySelector('.message-bubble');
  bubble.innerHTML = formatContent(fullContent);
  const meta = el.querySelector('.message-meta');
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  const tokenBadge = tokenUsage ? `<span class="message-token-badge">${tokenUsage.total} tokens</span>` : '';
  const safeContent = esc(fullContent).replace(/"/g, '&quot;');
  meta.innerHTML = `<span>${time}</span>${tokenBadge}
    <button class="message-copy-btn" title="复制消息" data-content="${safeContent}">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
    </button>`;
  meta.querySelector('.message-copy-btn')?.addEventListener('click', function() {
    const text = this.dataset.content.replace(/&quot;/g, '"');
    navigator.clipboard.writeText(text).then(() => {
      toast('success', '已复制', '消息内容已复制到剪贴板');
    }).catch(() => {
      toast('error', '复制失败', '无法访问剪贴板');
    });
  });
}

function formatContent(text) {
  if (!text) return '';
  let html = esc(text);

  // Code blocks (with syntax highlighting header + copy button)
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const langLabel = lang || 'code';
    const highlighted = highlightSyntax(code.trim());
    return `<pre><div class="code-header"><span>${langLabel}</span><button class="code-copy-btn" onclick="copyCodeBlock(this)">复制</button></div><code>${highlighted}</code></pre>`;
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  // Strikethrough
  html = html.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // Headers
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Blockquotes
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // Horizontal rules
  html = html.replace(/^---+$/gm, '<hr>');

  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  // Unordered lists
  html = html.replace(/^[\-\*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

  // Ordered lists
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

  // Tables (simple)
  html = html.replace(/^\|(.+)\|$/gm, (match, content) => {
    const cells = content.split('|').map(c => c.trim());
    if (cells.every(c => /^[-:]+$/.test(c))) return ''; // separator row
    const isHeader = false;
    const tag = isHeader ? 'th' : 'td';
    return '<tr>' + cells.map(c => `<${tag}>${c}</${tag}>`).join('') + '</tr>';
  });
  html = html.replace(/((?:<tr>.*<\/tr>\n?)+)/g, '<table>$1</table>');

  // Paragraphs: double newlines
  html = html.replace(/\n\n/g, '</p><p>');
  // Single newlines to <br> (but not inside pre/table)
  html = html.replace(/\n/g, '<br>');

  return html;
}

function highlightSyntax(code) {
  // Basic keyword highlighting
  let h = code;
  // Comments (single-line)
  h = h.replace(/(\/\/.*$|#.*$)/gm, '<span class="cm">$1</span>');
  // Strings
  h = h.replace(/(&quot;[^&]*&quot;|&#39;[^&]*&#39;|"[^"]*"|'[^']*')/g, '<span class="str">$1</span>');
  // Numbers
  h = h.replace(/\b(\d+\.?\d*)\b/g, '<span class="num">$1</span>');
  // Keywords
  const kws = ['function', 'const', 'let', 'var', 'return', 'if', 'else', 'for', 'while', 'class', 'import', 'export', 'from', 'async', 'await', 'try', 'catch', 'throw', 'new', 'this', 'def', 'print', 'self', 'None', 'True', 'False', 'public', 'private', 'static', 'void', 'int', 'string', 'bool'];
  const kwRe = new RegExp('\\b(' + kws.join('|') + ')\\b', 'g');
  h = h.replace(kwRe, '<span class="kw">$1</span>');
  return h;
}

function copyCodeBlock(btn) {
  const pre = btn.closest('pre');
  const code = pre.querySelector('code');
  const text = code.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }).catch(() => {
    toast('error', '复制失败', '无法访问剪贴板');
  });
}
// Make copyCodeBlock globally accessible for inline onclick
window.copyCodeBlock = copyCodeBlock;

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

// ============ Keyboard Shortcuts ============
document.addEventListener('keydown', (e) => {
  // Esc to close modals
  if (e.key === 'Escape') {
    const modal = document.querySelector('.modal-overlay');
    if (modal) {
      modal.remove();
      return;
    }
  }
});

// ============ Theme Toggle ============
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('symbio-theme', theme);
  state.theme = theme;
}

dom.themeToggle?.addEventListener('click', () => {
  const newTheme = state.theme === 'dark' ? 'light' : 'dark';
  applyTheme(newTheme);
});

// Apply saved theme on load
applyTheme(state.theme);

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
  showLoading(dom.modelsGrid, '加载模型...');
  try {
    const res = await fetch(`${API}/models`);
    const data = await res.json();
    state.models = data.models || [];
    renderModels();
  } catch (e) {
    toast('error', '加载模型失败', e.message);
    dom.modelsGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
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
  showLoading(dom.tasksGrid, '加载任务...');
  try {
    const statusParam = state.taskFilter !== 'all' ? `?status=${state.taskFilter}` : '';
    const res = await fetch(`${API}/tasks${statusParam}`);
    const data = await res.json();
    state.tasks = data.tasks || [];
    renderTasks();
  } catch (e) {
    toast('error', '加载任务失败', e.message);
    dom.tasksGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
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

// ============ Memory Page ============
async function loadMemories(query) {
  showLoading(dom.memoryGrid, query ? '搜索记忆...' : '加载记忆...');
  try {
    const url = query ? `${API}/memory/search?q=${encodeURIComponent(query)}` : `${API}/memory`;
    const res = await fetch(url);
    const data = await res.json();
    state.memories = data.memories || [];
    renderMemories(query);
  } catch (e) {
    toast('error', '加载记忆失败', e.message);
    dom.memoryGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
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

// ============ Skills Page ============
async function loadSkills(query) {
  showLoading(dom.skillsGrid, query ? '搜索 Skills...' : '加载 Skills...');
  try {
    const url = query ? `${API}/skills/search?q=${encodeURIComponent(query)}` : `${API}/skills`;
    const res = await fetch(url);
    const data = await res.json();
    state.skills = data.skills || [];
    renderSkills(query);
  } catch (e) {
    toast('error', '加载 Skills 失败', e.message);
    dom.skillsGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
  }
}

function renderSkills(query) {
  if (state.skills.length === 0) {
    dom.skillsGrid.innerHTML = `
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
        <p>${query ? '未找到匹配的 Skills' : '暂无 Skills'}</p>
        <span class="empty-hint">${query ? '尝试不同的搜索词' : '点击上方按钮导入或创建 Skill'}</span>
      </div>
    `;
    return;
  }

  dom.skillsGrid.innerHTML = state.skills.map(sk => `
    <div class="skill-card" data-id="${sk.id}" onclick="showSkillDetailPage('${sk.id}')">
      <div class="skill-card-header">
        <div class="skill-card-info">
          <div class="skill-card-name">
            <span class="skill-icon-wrap">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
            </span>
            ${esc(sk.name)}
          </div>
          <div class="skill-card-version">v${esc(sk.version)}</div>
        </div>
        <span class="skill-source-badge skill-source-${sk.source}">${esc(sk.source)}</span>
      </div>
      <div class="skill-card-desc">${esc(sk.description || '暂无描述')}</div>
      <div class="skill-card-meta">
        <div class="skill-keywords">
          ${(sk.trigger_keywords || []).slice(0, 3).map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}
        </div>
        <span class="badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '启用' : '禁用'}</span>
      </div>
      ${sk.relevance !== undefined ? `<div class="skill-relevance">匹配度 ${(sk.relevance * 100).toFixed(0)}%</div>` : ''}
      <div class="skill-card-actions">
        <button class="skill-action-btn" onclick="showSkillDetail('${sk.id}')" title="查看详情">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
        <button class="skill-action-btn" onclick="editSkill('${sk.id}')" title="编辑">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
        <button class="skill-action-btn skill-action-danger" onclick="deleteSkill('${sk.id}')" title="删除">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
        </button>
      </div>
    </div>
  `).join('');
}

// Skills search
let skillsSearchTimer = null;
dom.skillsSearch?.addEventListener('input', () => {
  clearTimeout(skillsSearchTimer);
  skillsSearchTimer = setTimeout(() => {
    const q = dom.skillsSearch.value.trim();
    loadSkills(q || undefined);
  }, 300);
});

// Skills action buttons
document.getElementById('btn-auto-detect')?.addEventListener('click', autoDetectSkills);
document.getElementById('btn-import-dir')?.addEventListener('click', showImportDirModal);
document.getElementById('btn-create-skill')?.addEventListener('click', showCreateSkillModal);

async function autoDetectSkills() {
  toast('info', '正在扫描...', '检测已安装的 Claude Code、Codex 等 Skills');
  try {
    const res = await fetch(`${API}/skills/auto-detect`, { method: 'POST' });
    const data = await res.json();
    if (data.found > 0) {
      toast('success', '发现 Skills', `找到 ${data.found} 个新 Skill，已导入`);
      loadSkills();
    } else {
      toast('info', '未发现新 Skills', '未检测到新的已安装 Skills');
    }
  } catch (e) {
    toast('error', '检测失败', e.message);
  }
}

function showImportDirModal() {
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>从目录导入 Skills</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>目录路径</label>
          <input type="text" id="modal-dir-path" placeholder="例: /home/user/.claude/skills 或 C:\Users\skills">
        </div>
        <p style="font-size:0.75rem;color:var(--text-tertiary);margin-top:8px;">
          支持导入 Claude Code、Codex 等工具的 Skills 目录
        </p>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">导入</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const dirPath = overlay.querySelector('#modal-dir-path').value.trim();
    if (!dirPath) { toast('error', '验证失败', '请输入目录路径'); return; }
    try {
      const res = await fetch(`${API}/skills/import-dir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: dirPath }),
      });
      const data = await res.json();
      overlay.remove();
      if (data.imported > 0) {
        toast('success', '导入成功', `从 ${dirPath} 导入了 ${data.imported} 个 Skills`);
        loadSkills();
      } else {
        toast('info', '未发现 Skills', '该目录下未找到有效的 Skill 定义文件');
      }
    } catch (e) {
      toast('error', '导入失败', e.message);
    }
  });
}

function showCreateSkillModal() {
  showImportSkillModal();
}

function showImportSkillModal() {
  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>导入 Skill</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Skill 名称</label>
          <input type="text" id="modal-skill-name" placeholder="例: my-custom-skill">
        </div>
        <div class="form-group">
          <label>描述</label>
          <input type="text" id="modal-skill-desc" placeholder="简要描述 Skill 功能">
        </div>
        <div class="form-group">
          <label>版本</label>
          <input type="text" id="modal-skill-version" placeholder="1.0.0" value="1.0.0">
        </div>
        <div class="form-group">
          <label>来源</label>
          <select id="modal-skill-source">
            <option value="custom">自定义</option>
            <option value="external">外部</option>
            <option value="builtin">内置</option>
          </select>
        </div>
        <div class="form-group">
          <label>触发关键词（逗号分隔）</label>
          <input type="text" id="modal-skill-keywords" placeholder="关键词1, 关键词2">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">导入</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const name = overlay.querySelector('#modal-skill-name').value.trim();
    const description = overlay.querySelector('#modal-skill-desc').value.trim();
    const version = overlay.querySelector('#modal-skill-version').value.trim() || '1.0.0';
    const source = overlay.querySelector('#modal-skill-source').value;
    const keywordsRaw = overlay.querySelector('#modal-skill-keywords').value.trim();
    const keywords = keywordsRaw ? keywordsRaw.split(/[,，]/).map(k => k.trim()).filter(Boolean) : [];

    if (!name) {
      toast('error', '验证失败', 'Skill 名称不能为空');
      return;
    }

    try {
      const res = await fetch(`${API}/skills/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          description,
          version,
          source,
          enabled: true,
          trigger_keywords: keywords,
        }),
      });

      if (res.ok) {
        overlay.remove();
        toast('success', '已导入', `Skill ${name} 已导入`);
        loadSkills();
      } else {
        const data = await res.json();
        toast('error', '导入失败', data.detail || '未知错误');
      }
    } catch (e) {
      toast('error', '导入失败', e.message);
    }
  });
}

// Skill Detail
function showSkillDetail(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <div class="modal-header">
        <h3>${esc(sk.name)}</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-grid">
          <div class="detail-item"><label>版本</label><span>v${esc(sk.version)}</span></div>
          <div class="detail-item"><label>来源</label><span class="skill-source-badge skill-source-${sk.source}">${esc(sk.source)}</span></div>
          <div class="detail-item"><label>状态</label><span class="badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '启用' : '禁用'}</span></div>
          <div class="detail-item"><label>创建时间</label><span>${esc(sk.created_at || '未知')}</span></div>
        </div>
        <div class="detail-section">
          <label>描述</label>
          <p>${esc(sk.description || '暂无描述')}</p>
        </div>
        ${(sk.trigger_keywords && sk.trigger_keywords.length) ? `
        <div class="detail-section">
          <label>触发关键词</label>
          <div class="skill-keywords">${sk.trigger_keywords.map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}</div>
        </div>` : ''}
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Edit Skill
function editSkill(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>编辑 Skill</h3>
        <button class="icon-btn modal-close-btn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Skill 名称</label>
          <input type="text" id="edit-skill-name" value="${esc(sk.name)}">
        </div>
        <div class="form-group">
          <label>描述</label>
          <textarea id="edit-skill-desc">${esc(sk.description || '')}</textarea>
        </div>
        <div class="form-group">
          <label>版本</label>
          <input type="text" id="edit-skill-version" value="${esc(sk.version)}">
        </div>
        <div class="form-group">
          <label>触发关键词（逗号分隔）</label>
          <input type="text" id="edit-skill-keywords" value="${(sk.trigger_keywords || []).join(', ')}">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const name = overlay.querySelector('#edit-skill-name').value.trim();
    const description = overlay.querySelector('#edit-skill-desc').value.trim();
    const version = overlay.querySelector('#edit-skill-version').value.trim();
    const keywordsRaw = overlay.querySelector('#edit-skill-keywords').value.trim();
    const keywords = keywordsRaw ? keywordsRaw.split(/[,，]/).map(k => k.trim()).filter(Boolean) : [];
    if (!name) { toast('error', '验证失败', '名称不能为空'); return; }
    try {
      const res = await fetch(`${API}/skills/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description, version, trigger_keywords: keywords }),
      });
      if (res.ok) {
        overlay.remove();
        toast('success', '已更新', `Skill ${name} 已更新`);
        loadSkills();
      } else {
        const data = await res.json();
        toast('error', '更新失败', data.detail || '未知错误');
      }
    } catch (e) { toast('error', '更新失败', e.message); }
  });
}

// Delete Skill
async function deleteSkill(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  if (!confirm(`确定要删除 Skill "${sk.name}" 吗？`)) return;
  try {
    const res = await fetch(`${API}/skills/${id}`, { method: 'DELETE' });
    if (res.ok) {
      toast('success', '已删除', `Skill ${sk.name} 已删除`);
      loadSkills();
    } else {
      toast('error', '删除失败', '无法删除该 Skill');
    }
  } catch (e) { toast('error', '删除失败', e.message); }
}

// ============ Skill Detail Page ============
async function showSkillDetailPage(id) {
  const grid = document.getElementById('skills-grid');
  const detail = document.getElementById('skill-detail-page');
  if (!grid || !detail) return;

  grid.style.display = 'none';
  detail.style.display = 'flex';

  // Fetch detail from API
  let skillData = null;
  try {
    const res = await fetch(`${API}/skills/${id}/detail`);
    if (res.ok) skillData = await res.json();
  } catch(e) {}

  // Fallback to local data
  if (!skillData) {
    const sk = state.skills.find(s => s.id === id);
    if (sk) skillData = { skill: sk, files: [], readme: null, manifest: null, prompts: [], tests: [] };
  }
  if (!skillData || !skillData.skill) {
    toast('error', '加载失败', '无法获取 Skill 详情');
    backToSkillsGrid();
    return;
  }

  state.skillDetail = skillData;
  renderSkillDetailHeader(skillData.skill);
  renderSkillOverview(skillData);
  renderSkillDocs(skillData);
  renderSkillFiles(skillData, id);
  renderSkillConfig(skillData);
  renderSkillTests(skillData);
}

function backToSkillsGrid() {
  document.getElementById('skills-grid').style.display = '';
  document.getElementById('skill-detail-page').style.display = 'none';
  state.skillDetail = null;
}

function renderSkillDetailHeader(sk) {
  const el = document.getElementById('skill-detail-header');
  if (!el) return;
  el.innerHTML = `
    <div class="sdh-info">
      <div class="sdh-title">
        <h1>${esc(sk.name)}</h1>
        <span class="skill-version-badge">v${esc(sk.version)}</span>
        <span class="skill-source-badge skill-source-${sk.source}">${esc(sk.source)}</span>
      </div>
      <p class="sdh-desc">${esc(sk.description || '暂无描述')}</p>
      <div class="sdh-meta">
        <span class="sdh-meta-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          ${esc(sk.created_at || '未知')}
        </span>
        <span class="sdh-meta-item badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '已启用' : '已禁用'}</span>
      </div>
      ${(sk.trigger_keywords && sk.trigger_keywords.length) ? `
      <div class="sdh-keywords">${sk.trigger_keywords.map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}</div>
      ` : ''}
    </div>
  `;
}

function renderSkillOverview(data) {
  const el = document.getElementById('panel-overview');
  if (!el) return;
  const sk = data.skill;
  const manifest = data.manifest || {};

  let html = `<div class="skill-overview-grid">`;

  // Description card
  html += `<div class="so-card"><h4>描述</h4><p>${esc(sk.description || '暂无描述')}</p></div>`;

  // Metadata card
  html += `<div class="so-card"><h4>基本信息</h4>
    <div class="so-meta"><span>名称</span><span>${esc(sk.name)}</span></div>
    <div class="so-meta"><span>版本</span><span>v${esc(sk.version)}</span></div>
    <div class="so-meta"><span>来源</span><span>${esc(sk.source)}</span></div>
    <div class="so-meta"><span>状态</span><span>${sk.enabled ? '启用' : '禁用'}</span></div>
    <div class="so-meta"><span>创建时间</span><span>${esc(sk.created_at || '未知')}</span></div>
  </div>`;

  // Manifest info
  if (manifest.author || manifest.license || manifest.dependencies) {
    html += `<div class="so-card"><h4>包信息</h4>`;
    if (manifest.author) html += `<div class="so-meta"><span>作者</span><span>${esc(manifest.author)}</span></div>`;
    if (manifest.license) html += `<div class="so-meta"><span>许可证</span><span>${esc(manifest.license)}</span></div>`;
    if (manifest.dependencies) {
      html += `<div class="so-deps"><h5>依赖</h5>`;
      for (const [dep, ver] of Object.entries(manifest.dependencies)) {
        html += `<span class="so-dep-tag">${esc(dep)}: ${esc(ver)}</span>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }

  // Directory info
  html += `<div class="so-card"><h4>目录</h4><p class="so-path">${esc(data.directory || '未找到本地目录')}</p></div>`;

  html += `</div>`;
  el.innerHTML = html;
}

function renderSkillDocs(data) {
  const el = document.getElementById('panel-docs');
  if (!el) return;
  if (data.readme) {
    el.innerHTML = `<div class="skill-doc-content">${formatContent(data.readme)}</div>`;
  } else {
    el.innerHTML = `<div class="empty-state"><p>暂无文档</p><span class="empty-hint">在 Skill 目录下创建 skill.md 或 README.md 即可显示</span></div>`;
  }
}

function renderSkillFiles(data, skillId) {
  const el = document.getElementById('panel-files');
  if (!el) return;

  if (!data.files || data.files.length === 0) {
    el.innerHTML = `<div class="empty-state"><p>暂无文件</p><span class="empty-hint">${data.directory ? '目录为空' : '未找到 Skill 目录'}</span></div>`;
    return;
  }

  el.innerHTML = `
    <div class="file-split">
      <div class="file-tree" id="file-tree"></div>
      <div class="file-viewer" id="file-viewer">
        <div class="file-viewer-placeholder">&larr; 选择文件查看内容</div>
      </div>
    </div>
  `;

  renderFileTree(data.files, skillId);
}

function renderFileTree(files, skillId) {
  const tree = document.getElementById('file-tree');
  if (!tree) return;

  // Build tree structure
  const root = {};
  files.forEach(f => {
    const parts = f.name.split(/[\\/]/);
    let node = root;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node[part] = { file: f };
      } else {
        if (!node[part] || node[part].file) node[part] = {};
        node = node[part];
      }
    });
  });

  tree.innerHTML = renderTreeNode(root, skillId, 0);
}

function renderTreeNode(node, skillId, depth) {
  let html = '';
  const entries = Object.entries(node).sort((a, b) => {
    const aIsDir = !a[1].file;
    const bIsDir = !b[1].file;
    if (aIsDir && !bIsDir) return -1;
    if (!aIsDir && bIsDir) return 1;
    return a[0].localeCompare(b[0]);
  });

  for (const [name, val] of entries) {
    const indent = depth * 16;
    if (val.file) {
      const icon = getFileIcon(val.file.type);
      html += `<div class="ft-item ft-file" style="padding-left:${indent + 8}px" onclick="loadSkillFile('${skillId}', '${esc(val.file.name)}')">
        ${icon}<span class="ft-name">${esc(name)}</span>
        <span class="ft-size">${formatFileSize(val.file.size)}</span>
      </div>`;
    } else {
      html += `<div class="ft-item ft-folder" style="padding-left:${indent}px" onclick="this.classList.toggle('ft-collapsed')">
        <span class="ft-arrow">&#9660;</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
        <span class="ft-name">${esc(name)}</span>
      </div>
      <div class="ft-children">${renderTreeNode(val, skillId, depth + 1)}</div>`;
    }
  }
  return html;
}

function getFileIcon(type) {
  const icons = {
    markdown: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>',
    code: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/></svg>',
    config: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    text: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    script: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    other: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  };
  return icons[type] || icons.other;
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function loadSkillFile(skillId, filePath) {
  const viewer = document.getElementById('file-viewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="file-viewer-loading">加载中...</div>';

  try {
    const res = await fetch(`${API}/skills/${skillId}/file?path=${encodeURIComponent(filePath)}`);
    if (!res.ok) throw new Error('加载失败');
    const data = await res.json();

    const isCode = /\.(py|js|ts|json|yaml|yml|sh|html|css|md)$/.test(filePath);

    viewer.innerHTML = `
      <div class="fv-header">
        <span class="fv-path">${esc(filePath)}</span>
        <span class="fv-size">${formatFileSize(data.size)}</span>
        <div class="fv-actions">
          <button class="fv-btn fv-copy" onclick="navigator.clipboard.writeText(document.querySelector('.fv-content-edit')?.value || document.querySelector('.fv-content')?.textContent)">复制</button>
          <button class="fv-btn fv-edit" onclick="toggleSkillFileEdit('${skillId}', '${esc(filePath)}')">编辑</button>
        </div>
      </div>
      <div class="fv-content" data-raw="${esc(data.content).replace(/"/g, '&quot;')}">${isCode ? highlightSyntax(data.content) : esc(data.content)}</div>
    `;
  } catch(e) {
    viewer.innerHTML = `<div class="file-viewer-error">${esc(e.message)}</div>`;
  }
}

function toggleSkillFileEdit(skillId, filePath) {
  const viewer = document.getElementById('file-viewer');
  if (!viewer) return;

  const contentEl = viewer.querySelector('.fv-content');
  const editBtn = viewer.querySelector('.fv-edit');
  const headerEl = viewer.querySelector('.fv-header');

  // Check if already in edit mode
  const textarea = viewer.querySelector('.fv-content-edit');
  if (textarea) {
    // Switch back to view mode - restore original content
    const raw = contentEl?.dataset?.raw || '';
    const isCode = /\.(py|js|ts|json|yaml|yml|sh|html|css|md)$/.test(filePath);
    const contentDiv = document.createElement('div');
    contentDiv.className = 'fv-content';
    contentDiv.dataset.raw = raw;
    contentDiv.innerHTML = isCode ? highlightSyntax(raw) : esc(raw);
    textarea.replaceWith(contentDiv);
    editBtn.textContent = '编辑';
    editBtn.classList.remove('fv-editing');
    // Remove save/cancel buttons
    viewer.querySelector('.fv-save')?.remove();
    viewer.querySelector('.fv-cancel')?.remove();
    return;
  }

  if (!contentEl) return;
  const raw = contentEl.dataset.raw || contentEl.textContent;

  // Create textarea
  const textareaEl = document.createElement('textarea');
  textareaEl.className = 'fv-content-edit';
  textareaEl.value = raw;
  textareaEl.spellcheck = false;
  contentEl.replaceWith(textareaEl);

  // Update edit button
  editBtn.textContent = '取消';
  editBtn.classList.add('fv-editing');

  // Add save button
  const actionsEl = viewer.querySelector('.fv-actions');
  if (!viewer.querySelector('.fv-save')) {
    const saveBtn = document.createElement('button');
    saveBtn.className = 'fv-btn fv-save';
    saveBtn.textContent = '保存';
    saveBtn.onclick = () => saveSkillFile(skillId, filePath, textareaEl.value);
    actionsEl.insertBefore(saveBtn, editBtn);
  }

  textareaEl.focus();
}

async function saveSkillFile(skillId, filePath, content) {
  try {
    const res = await fetch(`${API}/skills/${skillId}/file`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '保存失败');
    }
    toast('success', '已保存', `${filePath} 已更新`);
    // Reload the file view
    loadSkillFile(skillId, filePath);
  } catch(e) {
    toast('error', '保存失败', e.message);
  }
}

function renderSkillConfig(data) {
  const el = document.getElementById('panel-config');
  if (!el) return;
  const manifest = data.manifest;
  if (!manifest) {
    el.innerHTML = `<div class="empty-state"><p>暂无配置</p><span class="empty-hint">在 Skill 目录下创建 skill.yaml 或 manifest.json 即可</span></div>`;
    return;
  }

  el.innerHTML = `
    <div class="skill-config-section">
      <h4>Manifest 配置</h4>
      <div class="skill-config-viewer">${highlightSyntax(JSON.stringify(manifest, null, 2))}</div>
    </div>
  `;
}

function renderSkillTests(data) {
  const el = document.getElementById('panel-tests');
  if (!el) return;
  if (!data.tests || data.tests.length === 0) {
    el.innerHTML = `<div class="empty-state"><p>暂无测试</p><span class="empty-hint">在 Skill 目录下创建 test_*.py 或 *.test.js 文件</span></div>`;
    return;
  }

  el.innerHTML = data.tests.map(t => `
    <div class="skill-test-item">
      <div class="sti-header">
        <span class="sti-name">${esc(t.name)}</span>
      </div>
      <pre class="sti-content"><code>${highlightSyntax(t.content)}</code></pre>
    </div>
  `).join('');
}

// Skill Detail Tab switching
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.skill-tab');
  if (!tab) return;
  const tabName = tab.dataset.tab;
  document.querySelectorAll('.skill-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.skill-tab-panel').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  document.getElementById(`panel-${tabName}`)?.classList.add('active');
});

// ============ Toast ============
function toast(type, title, msg) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;

  const iconSvg = {
    success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>',
    error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };

  el.innerHTML = `
    <div class="toast-icon ${type}">${iconSvg[type] || iconSvg.info}</div>
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

function showLoading(container, message = '加载中...') {
  container.innerHTML = `
    <div class="loading-state">
      <div class="loading-spinner"></div>
      <p>${message}</p>
    </div>
  `;
}

// ============ LLM Config ============
async function loadConfig() {
  try {
    const res = await fetch(`${API}/config`);
    const data = await res.json();
    state.config = data;
    renderConfig();
  } catch (e) {
    console.warn('加载 LLM 配置失败:', e.message);
    toast('error', '加载配置失败', e.message);
  }
}

async function saveConfig() {
  const anthropicKey = document.getElementById('config-anthropic-key')?.value?.trim() || '';
  const anthropicUrl = document.getElementById('config-anthropic-url')?.value?.trim() || '';
  const openaiKey = document.getElementById('config-openai-key')?.value?.trim() || '';
  const openaiUrl = document.getElementById('config-openai-url')?.value?.trim() || '';
  const modelLow = document.getElementById('config-model-low')?.value || '';
  const modelMedium = document.getElementById('config-model-medium')?.value || '';
  const modelHigh = document.getElementById('config-model-high')?.value || '';

  try {
    const res = await fetch(`${API}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        anthropic_api_key: anthropicKey,
        anthropic_base_url: anthropicUrl,
        openai_api_key: openaiKey,
        openai_base_url: openaiUrl,
        model_low: modelLow,
        model_medium: modelMedium,
        model_high: modelHigh,
      }),
    });

    if (res.ok) {
      const data = await res.json();
      if (data.success) {
        toast('success', '配置已保存', 'LLM 配置已更新');
        // Reload config to reflect saved state
        await loadConfig();
      } else {
        toast('error', '保存失败', '服务器返回异常');
      }
    } else {
      const data = await res.json();
      toast('error', '保存失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '保存失败', e.message);
  }
}

function renderConfig() {
  if (!dom.configSection) return;
  const c = state.config;

  // Build model options from state.models
  const modelOptions = state.models.map(m =>
    `<option value="${esc(m.model_id)}">${esc(m.display_name || m.model_id)}</option>`
  ).join('');

  // Helper to create a <select> with pre-selected value
  function tierSelect(id, selectedValue, label) {
    const defaultOptions = [
      { value: 'claude-3-5-haiku-20241022', label: 'Claude 3.5 Haiku' },
      { value: 'claude-sonnet-4-20250514', label: 'Claude Sonnet 4' },
      { value: 'claude-opus-4-20250514', label: 'Claude Opus 4' },
    ];

    // Merge: default options + models from state, deduplicated by model_id
    const seen = new Set();
    const allOptions = [];
    for (const opt of defaultOptions) {
      if (!seen.has(opt.value)) {
        seen.add(opt.value);
        allOptions.push(opt);
      }
    }
    for (const m of state.models) {
      if (!seen.has(m.model_id)) {
        seen.add(m.model_id);
        allOptions.push({ value: m.model_id, label: m.display_name || m.model_id });
      }
    }

    const optionsHtml = allOptions.map(opt =>
      `<option value="${esc(opt.value)}" ${opt.value === selectedValue ? 'selected' : ''}>${esc(opt.label)}</option>`
    ).join('');

    return `
      <div class="form-group">
        <label>${label}</label>
        <select id="${id}">${optionsHtml}</select>
      </div>
    `;
  }

  dom.configSection.innerHTML = `
    <div class="config-card">
      <div class="config-card-header">
        <h3>LLM 配置</h3>
        <button class="btn-primary" id="btn-save-config">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          保存配置
        </button>
      </div>
      <div class="config-card-body">
        <div class="config-row">
          <div class="config-group">
            <div class="config-section-title">Anthropic</div>
            <div class="form-group">
              <label>API Key</label>
              <input type="password" id="config-anthropic-key" value="${esc(c.anthropic_api_key || '')}" placeholder="sk-ant-...">
            </div>
            <div class="form-group">
              <label>Base URL</label>
              <input type="text" id="config-anthropic-url" value="${esc(c.anthropic_base_url || 'https://api.anthropic.com')}">
            </div>
          </div>
          <div class="config-group">
            <div class="config-section-title">OpenAI 兼容</div>
            <div class="form-group">
              <label>API Key</label>
              <input type="password" id="config-openai-key" value="${esc(c.openai_api_key || '')}" placeholder="sk-...">
            </div>
            <div class="form-group">
              <label>Base URL</label>
              <input type="text" id="config-openai-url" value="${esc(c.openai_base_url || 'https://api.openai.com/v1')}">
            </div>
          </div>
        </div>
        <div class="config-section-title">模型路由</div>
        <div class="config-tier-row">
          ${tierSelect('config-model-low', c.model_low, '简单任务 (low)')}
          ${tierSelect('config-model-medium', c.model_medium, '中等任务 (medium)')}
          ${tierSelect('config-model-high', c.model_high, '复杂任务 (high)')}
        </div>
      </div>
    </div>
  `;

  // Attach save handler
  document.getElementById('btn-save-config')?.addEventListener('click', saveConfig);
}

// ============ Sessions Sync ============
async function loadSessions() {
  try {
    const res = await fetch(`${API}/sessions`);
    const data = await res.json();
    if (data.sessions && data.sessions.length > 0) {
      state.sessions = data.sessions.map(s => ({
        id: s.id,
        title: s.title || '新对话',
        time: formatTime(s.updated_at || s.created_at) || '刚刚',
      }));
      // Keep currentSession if it still exists, otherwise pick the first one
      if (!state.sessions.find(s => s.id === state.currentSession)) {
        state.currentSession = state.sessions[0].id;
      }
    }
    renderSessions();
  } catch (e) {
    console.warn('加载会话列表失败，使用本地状态:', e.message);
    renderSessions();
  }
}

async function loadSessionMessages(sessionId) {
  try {
    const res = await fetch(`${API}/sessions/${sessionId}/messages`);
    const data = await res.json();
    if (data.messages) {
      state.messages = data.messages.map(m => ({
        role: m.role,
        content: m.content,
        timestamp: new Date(m.timestamp).getTime(),
        tokens: m.tokens || 0,
      }));
      // Update token stats from loaded messages
      let totalTokens = 0;
      state.messages.forEach(m => { totalTokens += (m.tokens || 0); });
      state.tokens.total = totalTokens;
      updateStatus();
    }
    renderMessages();
  } catch (e) {
    console.warn('加载消息历史失败:', e.message);
    state.messages = [];
    renderMessages();
  }
}

// ============ Dashboard Page ============
async function loadDashboard() {
  try {
    // Fetch memory stats
    const memRes = await fetch(`${API}/memory/stats`);
    const memData = await memRes.json();

    // Fetch sessions
    const sessRes = await fetch(`${API}/sessions`);
    const sessData = await sessRes.json();

    // Update cards
    const totalMessages = (sessData.sessions || []).reduce((sum, s) => sum + (s.message_count || 0), 0);
    const totalTokens = memData.total_tokens || state.tokens.total;
    const activeSessions = (sessData.sessions || []).length;
    const memoryCount = memData.total_count || 0;

    document.getElementById('dash-total-messages').textContent = totalMessages;
    document.getElementById('dash-total-tokens').textContent = formatNumber(totalTokens);
    document.getElementById('dash-active-sessions').textContent = activeSessions;
    document.getElementById('dash-memory-count').textContent = memoryCount;

    // Render bar chart from session token data
    renderTokenChart(sessData.sessions || []);
  } catch (e) {
    console.warn('加载仪表盘数据失败:', e.message);
    // Use local state as fallback
    document.getElementById('dash-total-tokens').textContent = formatNumber(state.tokens.total);
    document.getElementById('dash-active-sessions').textContent = state.sessions.length;
  }
}

function renderTokenChart(sessions) {
  const chart = dom.tokenBarChart;
  if (!chart) return;

  // Build data: last 7 sessions or fallback to mock
  const data = sessions.slice(-7).map(s => ({
    label: (s.title || '会话').substring(0, 6),
    value: s.token_count || s.tokens || 0,
  }));

  // If no data, show placeholder
  if (data.length === 0 || data.every(d => d.value === 0)) {
    const placeholders = ['会话1', '会话2', '会话3', '会话4', '会话5'];
    chart.innerHTML = placeholders.map((l, i) => {
      const h = Math.max(8, Math.floor(Math.random() * 80 + 20));
      return `<div class="bar-chart-bar"><div class="bar-chart-fill" style="height:${h}%" data-value="0"></div><div class="bar-chart-label">${l}</div></div>`;
    }).join('');
    return;
  }

  const maxVal = Math.max(...data.map(d => d.value), 1);
  chart.innerHTML = data.map(d => {
    const pct = Math.max(4, Math.round((d.value / maxVal) * 100));
    return `<div class="bar-chart-bar"><div class="bar-chart-fill" style="height:${pct}%" data-value="${d.value}"></div><div class="bar-chart-label">${esc(d.label)}</div></div>`;
  }).join('');
}

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

// Refresh dashboard button
$('#btn-refresh-dashboard')?.addEventListener('click', loadDashboard);

// ============ HITL Page ============
async function loadHitl() {
  showLoading(dom.hitlGrid, '加载审批列表...');
  try {
    const filter = state.hitlFilter;
    const url = filter === 'all' ? `${API}/hitl` : `${API}/hitl/pending`;
    const res = await fetch(url);
    const data = await res.json();
    state.hitlItems = data.requests || data.items || [];
    renderHitl();
  } catch (e) {
    toast('error', '加载审批列表失败', e.message);
    dom.hitlGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
  }
}

function renderHitl() {
  const filtered = state.hitlFilter === 'all'
    ? state.hitlItems
    : state.hitlItems.filter(i => i.status === state.hitlFilter);

  if (filtered.length === 0) {
    dom.hitlGrid.innerHTML = `
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2"><path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="8.5" cy="7" r="4"/><polyline points="17 11 19 13 23 9"/></svg>
        <p>${state.hitlFilter === 'pending' ? '暂无待审批项' : '无匹配记录'}</p>
        <span class="empty-hint">${state.hitlFilter === 'pending' ? 'HITL 请求将在此显示' : '尝试切换筛选条件'}</span>
      </div>
    `;
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
      </div>
      ${item.status === 'pending' ? `
        <div class="hitl-card-actions">
          <button class="btn-approve" data-id="${item.id}">通过</button>
          <button class="btn-reject" data-id="${item.id}">拒绝</button>
        </div>
      ` : ''}
    </div>
  `).join('');

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
}

function hitlStatusLabel(status) {
  const map = { pending: '待审批', approved: '已通过', rejected: '已拒绝' };
  return map[status] || status;
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
  renderHitl();
});
$('#btn-refresh-hitl')?.addEventListener('click', loadHitl);

// ============ Virtual Scroll (basic) ============
function setupVirtualScroll() {
  const container = dom.messages;
  if (!container) return;

  // Only enable if many messages (>100)
  container.addEventListener('scroll', () => {
    if (state.messages.length < 50) return;
    // Hide messages far from viewport
    const msgs = container.querySelectorAll('.message');
    const scrollTop = container.scrollTop;
    const viewHeight = container.clientHeight;
    msgs.forEach(msg => {
      const top = msg.offsetTop - container.offsetTop;
      const bottom = top + msg.offsetHeight;
      // Hide if far outside viewport (200px buffer)
      if (bottom < scrollTop - 400 || top > scrollTop + viewHeight + 400) {
        msg.style.visibility = 'hidden';
      } else {
        msg.style.visibility = 'visible';
      }
    });
  });
}

// ============ Init ============
async function init() {
  // Apply theme
  applyTheme(state.theme);

  await loadSessions();
  await checkHealth();
  connectWebSocket();
  setupVirtualScroll();
  setInterval(checkHealth, 30000);
  console.log('Symbio UI initialized');
}

document.addEventListener('DOMContentLoaded', init);
