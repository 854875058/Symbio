/* ============================================
   Symbio — Neural Command Center
   ============================================ */

const API = 'http://localhost:9090/api';

// ============ State ============
const state = {
  page: 'chat',
  sessions: [{ id: 'default', title: '新对话', time: '刚刚' }],
  currentSession: 'default',
  messages: [],
  models: [],
  memories: [],
  tokens: { input: 0, output: 0, total: 0 },
  cost: 0,
  connected: false,
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
};

// ============ Navigation ============
function switchPage(name) {
  state.page = name;
  dom.navTabs.forEach(t => t.classList.toggle('active', t.dataset.page === name));
  dom.pages.forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
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
    // Remove old messages but keep welcome
    const msgs = dom.messages.querySelectorAll('.message');
    msgs.forEach(m => m.remove());
    return;
  }

  dom.welcome.style.display = 'none';

  // Clear non-welcome content
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

function formatContent(text) {
  if (!text) return '';
  let html = esc(text);
  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// ============ Send Message ============
async function sendMessage() {
  const content = dom.input.value.trim();
  if (!content) return;

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

  // Show typing
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
  dom.sendBtn.disabled = !dom.input.value.trim();
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
  const dot = document.querySelector('.status-dot');
  if (dot) {
    dot.className = `status-dot ${state.connected ? 'online' : 'offline'}`;
  }
}

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
  setInterval(checkHealth, 30000);
  console.log('Symbio UI initialized');
}

document.addEventListener('DOMContentLoaded', init);
