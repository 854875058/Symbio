/* ============================================
   Symbio UI — 对话页：会话列表、消息渲染、WebSocket、发送、会话同步、虚拟滚动
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Sessions ============
// 会话项是 <button>，不是带 onclick 的 <div>。div 点得动但 Tab 到不了、
// 回车也不响应，读屏软件更不会把它念成"可点击"——对只用键盘的人来说，
// 这份列表等于不存在。用真按钮就自带焦点、键盘激活和 role。
// 外层 <ul>/<li> 让读屏能报出"共 N 项，第 3 项"。
function renderSessions() {
  dom.sessionsList.innerHTML = `
    <ul class="session-list-ul">
      ${state.sessions.map(s => {
        const on = s.id === state.currentSession;
        return `
        <li>
          <button type="button" class="session-item ${on ? 'active' : ''}" data-id="${esc(s.id)}"
                  ${on ? 'aria-current="true"' : ''}>
            <span class="session-dot" aria-hidden="true"></span>
            <span class="session-info">
              <span class="session-title">${esc(s.title)}</span>
              <span class="session-time">${esc(s.time)}</span>
            </span>
          </button>
        </li>`;
      }).join('')}
    </ul>`;

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
      <div class="message-bubble"></div>
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
  bubble.innerHTML = formatContent(text);
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

  // Headers：整体下移一级。消息正文是页面的下级内容，模型写的 `#` 不该
  // 变成与页面标题平级的 h1——否则读屏用户按标题浏览时，每条回复都伪装成
  // 一个新页面。h5/h6 已是底部，`####` 及更深都并到 h5。
  html = html.replace(/^#{4,6} (.+)$/gm, '<h5>$1</h5>');
  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');

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
    state.ws = new WebSocket(withToken(WS_URL));

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
    model: selectedChatModel(),
  }));
  return true;
}

function removeStreaming() {
  document.getElementById('streaming-msg')?.remove();
}

/**
 * 写底部状态栏的连接指示。
 *
 * online 有三个值：true 已连接 / false 已断开 / null 还不知道。
 * 第三种必须存在——页面刚加载、首次 /health 尚未回话时，界面既不该说
 * 已连接（后端可能根本没起）也不该说已断开（还没问过）。
 *
 * 连接状态只有底部状态栏一处（此前顶栏 + 底栏各显示一份，且文案还不一致：
 * 顶栏"断开" / 底栏"已断开" / 再被下面一段改写成"未连接"）
 */
function updateConnectionStatus(online) {
  const unknown = online === null || online === undefined;
  const dot = dom.statusDot;
  if (dot) {
    // 裸 .status-dot 即灰色未知态，颜色由 CSS 决定，这里不写内联色
    dot.className = `status-dot ${unknown ? '' : online ? 'online' : 'offline'}`.trim();
  }
  const connText = document.getElementById('status-conn-text');
  if (connText) {
    connText.textContent = unknown ? '检测中…' : online ? '已连接' : '已断开';
    connText.style.color = unknown ? 'var(--text-tertiary)' : online ? 'var(--green)' : 'var(--red)';
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
      body: JSON.stringify({
        message: content,
        session_id: state.currentSession,
        model: selectedChatModel(),
      }),
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

dom.chatModelSelect?.addEventListener('change', () => {
  state.selectedChatModel = dom.chatModelSelect.value;
  localStorage.setItem('symbio-chat-model', state.selectedChatModel);
});

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
