/* ============================================
   Symbio UI — Agent 工作台：多窗格、交互式终端、目录选择弹窗
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Agent 工作台（多开平铺，批次C1） ============
state.workbench = { panes: [], seq: 0, pollTimer: null, wired: false, transcripts: [] };

async function loadWorkbench() {
  await wbLoadTranscripts();
  wbWireOnce();
  renderWorkbench();
  wbStartPolling();
}

async function wbLoadTranscripts() {
  try {
    const res = await fetch(`${API}/external-agents/transcripts`);
    const data = await res.json();
    state.workbench.transcripts = data.transcripts || [];
  } catch (e) {
    state.workbench.transcripts = [];
  }
  wbRenderTranscriptOptions();
}

// 接管会话下拉：按当前选中的 provider 过滤，只列出对应 provider 的会话。
// option 的 value 用「全局 transcripts 数组下标」保持稳定，过滤后接管仍取得对。
function wbRenderTranscriptOptions() {
  const sel = $('#wb-transcript');
  if (!sel) return;
  const provider = $('#wb-provider')?.value || '';
  const items = state.workbench.transcripts;
  const matched = items
    .map((t, i) => ({ t, i }))
    .filter(({ t }) => !provider || t.provider === provider);
  sel.innerHTML = matched.length
    ? matched.map(({ t, i }) => `<option value="${i}">${esc((t.title || t.external_session_id || t.path))}</option>`).join('')
    : `<option value="">（该 provider 下无可接管的会话）</option>`;
}

function wbGridCols(n) {
  if (n <= 1) return 1;
  if (n <= 4) return 2;   // 2→分屏，3-4→2×2
  if (n <= 9) return 3;   // 5-6→2×3 …
  return 4;
}

function renderWorkbench() {
  const grid = $('#wb-grid');
  const countEl = $('#wb-count');
  if (!grid) return;
  const panes = state.workbench.panes;
  if (countEl) countEl.textContent = `${panes.length} 个窗格`;
  if (!panes.length) {
    grid.style.gridTemplateColumns = '';
    grid.innerHTML = `<div class="empty-state-lg"><p>还没有窗格</p><span class="empty-hint">「+ 新任务窗格」开一个新 agent，或选一个已有会话「接管」。多开后自动平铺。</span></div>`;
    return;
  }
  grid.style.gridTemplateColumns = `repeat(${wbGridCols(panes.length)}, minmax(0, 1fr))`;
  grid.innerHTML = panes.map(p => {
    if (p.mode === 'terminal') {
      const kindLabel = { 'claude-code': 'claude 终端', 'codex': 'codex 终端', 'shell': 'shell 终端' }[p.termKind] || '终端';
      return `
    <div class="wb-pane wb-pane-terminal" data-pane="${p.id}">
      <div class="wb-pane-head">
        <span class="wb-pane-title">⌨ ${esc(kindLabel)}</span>
        <span class="wb-pane-sub">${esc(p.subtitle || '')}</span>
        <button class="wb-pane-close" data-pane="${p.id}" title="关闭窗格">✕</button>
      </div>
      <div class="wb-term-mount" data-term="${p.id}"></div>
    </div>`;
    }
    return `
    <div class="wb-pane" data-pane="${p.id}">
      <div class="wb-pane-head">
        <span class="wb-pane-title">${esc(p.provider)} · ${p.mode === 'new' ? '新任务' : '接管'}</span>
        <span class="wb-pane-sub">${esc(p.subtitle || '')}</span>
        <button class="wb-pane-close" data-pane="${p.id}" title="关闭窗格">✕</button>
      </div>
      <div class="wb-pane-stream" data-stream="${p.id}">${wbStreamHtml(p)}</div>
      <div class="wb-pane-compose">
        <textarea class="wb-pane-input" data-pane="${p.id}" rows="2" placeholder="给这个 agent 下任务，回车发送（Shift+Enter 换行）"${p.busy ? ' disabled' : ''}>${esc(p.draft || '')}</textarea>
      </div>
    </div>`;
  }).join('');

  // 终端窗格：HTML 重建后把 xterm 实例（重新）挂进对应容器
  panes.filter(p => p.mode === 'terminal').forEach(wbMountTerminal);
}

function wbStreamHtml(p) {
  if (p.busy && !p.messages.length) return `<div class="wb-pane-empty">运行中…</div>`;
  if (!p.messages.length) return `<div class="wb-pane-empty">${p.mode === 'attached' ? '等待消息…' : '输入任务开始'}</div>`;
  const body = p.messages.map(m => `
    <div class="wb-msg role-${esc(m.role)}">
      <span class="wb-msg-role">${esc(m.role)}</span>
      <div class="wb-msg-body">${esc(m.content)}</div>
    </div>`).join('');
  return body + (p.busy ? `<div class="wb-pane-empty">运行中…</div>` : '');
}

function wbUpdateStream(p) {
  const el = document.querySelector(`.wb-pane-stream[data-stream="${p.id}"]`);
  if (el) { el.innerHTML = wbStreamHtml(p); el.scrollTop = el.scrollHeight; }
  const input = document.querySelector(`.wb-pane-input[data-pane="${p.id}"]`);
  if (input) input.disabled = !!p.busy;
}

function wbPane(id) { return state.workbench.panes.find(p => p.id === id); }

async function wbCreateNewPane() {
  const provider = $('#wb-provider')?.value || 'claude-code';
  const workspace = ($('#wb-workspace')?.value || '.').trim() || '.';
  try {
    const res = await fetch(`${API}/external-agents/sessions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, workspace, label: `${provider} 工作台` }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.workbench.seq += 1;
    state.workbench.panes.push({
      id: `pane-${state.workbench.seq}`, provider, mode: 'new',
      sessionId: data.session.session_id, subtitle: workspace,
      messages: [], draft: '', busy: false,
    });
    renderWorkbench();
  } catch (e) { toast('error', '新建窗格失败', e.message); }
}

// 接管已有会话：直接在交互终端里 resume（claude --resume / codex resume），
// 而不是旧的 tail 轮询只读窗格。这样能真正在原会话里继续交互。
function wbAttachPane() {
  const idx = $('#wb-transcript')?.value;
  const t = state.workbench.transcripts[Number(idx)];
  if (!t) { toast('error', '接管失败', '没有可接管的会话'); return; }
  if (typeof Terminal === 'undefined') {
    toast('error', '终端不可用', 'xterm.js 未加载');
    return;
  }
  const resumeId = t.external_session_id || '';
  if (!resumeId) { toast('error', '接管失败', '该会话缺少可续接的 session id'); return; }
  state.workbench.seq += 1;
  state.workbench.panes.push({
    id: `pane-${state.workbench.seq}`, provider: t.provider, mode: 'terminal',
    termKind: t.provider, resumeId, subtitle: `接管 ${resumeId}`,
    cwd: ($('#wb-workspace')?.value || '.').trim() || '.',
    term: null, fit: null, ws: null, started: false,
  });
  renderWorkbench();
}

async function wbSend(paneId, text) {
  const p = wbPane(paneId);
  if (!p || p.busy || !text.trim()) return;
  p.draft = '';
  p.messages.push({ role: 'user', content: text });
  p.busy = true;
  wbUpdateStream(p);
  const input = document.querySelector(`.wb-pane-input[data-pane="${paneId}"]`);
  if (input) input.value = '';
  try {
    if (p.mode === 'new') {
      // 直接把指令发给后端：不在前端拼接历史（claude -p 对"对话记录+请回应"式
      // 提示常答非所问）。多轮记忆由后端自动续接——首轮捕获 session_id、后续轮
      // 自动 --resume 拿到 claude 原生记忆（批次C2），所以这里直接发原文即可。
      const res = await fetch(`${API}/external-agents/sessions/${encodeURIComponent(p.sessionId)}/run`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, approved: true, timeout: 300 }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const r = data.result || {};
      const out = (r.stdout || r.error || '(无输出)').trim();
      p.messages.push({ role: 'assistant', content: out });
    } else {
      const res = await fetch(`${API}/external-agents/live/${encodeURIComponent(p.liveId)}/send`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const news = (data.result && data.result.new_messages) || [];
      if (news.length) p.messages.push(...news);
    }
  } catch (e) {
    p.messages.push({ role: 'assistant', content: `⚠ 出错：${e.message}` });
  } finally {
    p.busy = false;
    wbUpdateStream(p);
  }
}

async function wbPollOne(p) {
  if (!p || p.mode !== 'attached' || p.busy) return;
  try {
    const res = await fetch(`${API}/external-agents/live/${encodeURIComponent(p.liveId)}/poll`, { method: 'POST' });
    const data = await res.json();
    if (res.ok && (data.messages || []).length) {
      p.messages.push(...data.messages);
      wbUpdateStream(p);
    }
  } catch (e) { /* 静默 */ }
}

function wbStartPolling() {
  wbStopPolling();
  state.workbench.pollTimer = setInterval(() => {
    const page = document.getElementById('page-workbench');
    if (page && !page.classList.contains('active')) return;
    for (const p of state.workbench.panes) wbPollOne(p);
  }, 3000);
}
function wbStopPolling() {
  if (state.workbench.pollTimer) { clearInterval(state.workbench.pollTimer); state.workbench.pollTimer = null; }
}

async function wbClosePane(paneId) {
  const p = wbPane(paneId);
  // 终端窗格：关连接、销毁 xterm 实例，防泄漏
  if (p && p.mode === 'terminal') {
    try { p.ws?.close(); } catch (e) {}
    try { p.term?.dispose(); } catch (e) {}
    p.ws = null; p.term = null; p.fit = null;
  }
  state.workbench.panes = state.workbench.panes.filter(x => x.id !== paneId);
  renderWorkbench();
  if (p && p.mode === 'attached' && p.liveId) {
    try { await fetch(`${API}/external-agents/live/${encodeURIComponent(p.liveId)}`, { method: 'DELETE' }); } catch (e) {}
  }
}

// ============ 工作台：交互式终端窗格 ============
function wbCreateTerminalPane() {
  if (typeof Terminal === 'undefined') {
    toast('error', '终端不可用', 'xterm.js 未加载');
    return;
  }
  const kind = $('#wb-term-kind')?.value || 'shell';
  const workspace = ($('#wb-workspace')?.value || '.').trim() || '.';
  state.workbench.seq += 1;
  state.workbench.panes.push({
    id: `pane-${state.workbench.seq}`, provider: kind, mode: 'terminal',
    termKind: kind, subtitle: workspace, cwd: workspace,
    term: null, fit: null, ws: null, started: false,
  });
  renderWorkbench();
}

// 把 xterm 挂进窗格容器并接 WebSocket（renderWorkbench 每次重建 DOM 后调用）
function wbMountTerminal(p) {
  const mount = document.querySelector(`.wb-term-mount[data-term="${p.id}"]`);
  if (!mount) return;

  // 首次：创建 xterm + fit + WS；重渲染：把已有实例重新 open 到新容器
  if (!p.term) {
    p.term = new Terminal({
      cursorBlink: true,
      fontFamily: 'JetBrains Mono, Consolas, monospace',
      fontSize: 13,
      // xterm 也画在 canvas 上，取计算值而不是硬编码，跟随主题
      theme: (() => {
        const cs = getComputedStyle(document.documentElement);
        const v = (n, f) => cs.getPropertyValue(n).trim() || f;
        return {
          background: v('--bg-void', '#16150f'),
          foreground: v('--text-primary', '#edeae3'),
          cursor: v('--accent', '#e07a5a'),
        };
      })(),
      scrollback: 4000,
    });
    if (typeof FitAddon !== 'undefined' && FitAddon.FitAddon) {
      p.fit = new FitAddon.FitAddon();
      p.term.loadAddon(p.fit);
    }
  }

  mount.innerHTML = '';
  p.term.open(mount);
  wbFitTerminal(p);

  if (!p.started) {
    p.started = true;
    wbConnectTerminal(p);
  }
}

function wbFitTerminal(p) {
  try {
    p.fit?.fit();
    if (p.ws && p.ws.readyState === WebSocket.OPEN && p.term) {
      p.ws.send(JSON.stringify({ type: 'resize', cols: p.term.cols, rows: p.term.rows }));
    }
  } catch (e) { /* 容器还没布局好，忽略 */ }
}

function wbConnectTerminal(p) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(withToken(`${proto}://${window.location.host}/ws/terminal`));
  p.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: 'start', kind: p.termKind, cwd: p.cwd,
      resume_id: p.resumeId || '',
      cols: p.term?.cols || 100, rows: p.term?.rows || 30,
    }));
    // 键盘输入 → PTY
    p.term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data }));
    });
  };
  ws.onmessage = (evt) => {
    let m;
    try { m = JSON.parse(evt.data); } catch (e) { return; }
    if (m.type === 'output') p.term.write(m.data);
    else if (m.type === 'exit') p.term.write('\r\n\x1b[33m[进程已退出]\x1b[0m\r\n');
    else if (m.type === 'error') p.term.write(`\r\n\x1b[31m[错误] ${m.message}\x1b[0m\r\n`);
  };
  ws.onclose = () => {
    if (p.term) p.term.write('\r\n\x1b[90m[连接已断开]\x1b[0m\r\n');
  };
  ws.onerror = () => {
    if (p.term) p.term.write('\r\n\x1b[31m[连接失败]\x1b[0m\r\n');
  };
}

function wbWireOnce() {
  if (state.workbench.wired) return;
  state.workbench.wired = true;
  $('#wb-new-task')?.addEventListener('click', wbCreateNewPane);
  $('#wb-attach')?.addEventListener('click', wbAttachPane);
  // provider 下拉切换时，接管会话下拉联动只显示对应 provider 的会话
  $('#wb-provider')?.addEventListener('change', wbRenderTranscriptOptions);
  // 浏览按钮：打开服务端目录选择弹窗，选中后回填到工作区输入框
  $('#wb-browse')?.addEventListener('click', () => openDirPicker((abs) => {
    const input = $('#wb-workspace');
    if (input) input.value = abs;
  }));
  // 起终端按钮：开一个交互式 PTY 终端窗格
  $('#wb-terminal')?.addEventListener('click', wbCreateTerminalPane);
  // 窗口尺寸变化时，让所有终端窗格重新 fit
  window.addEventListener('resize', () => {
    state.workbench.panes.filter(p => p.mode === 'terminal').forEach(wbFitTerminal);
  });
  const grid = $('#wb-grid');
  grid?.addEventListener('keydown', (e) => {
    const input = e.target.closest('.wb-pane-input');
    if (!input) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      wbSend(input.dataset.pane, input.value);
    }
  });
  grid?.addEventListener('input', (e) => {
    const input = e.target.closest('.wb-pane-input');
    if (!input) return;
    const p = wbPane(input.dataset.pane);
    if (p) p.draft = input.value;
  });
  grid?.addEventListener('click', (e) => {
    const close = e.target.closest('.wb-pane-close');
    if (close) wbClosePane(close.dataset.pane);
  });
}

// ============ 目录选择弹窗（服务端目录浏览）============
const dirPicker = { current: '', onSelect: null, wired: false };

function openDirPicker(onSelect) {
  dirPicker.onSelect = onSelect;
  dirPickerWireOnce();
  const overlay = $('#dirpicker-overlay');
  if (overlay) overlay.style.display = 'flex';
  // 记住触发元素，关闭后把键盘焦点还回去（MutationObserver 只管动态弹窗）
  if (!modalA11y.opener) modalA11y.opener = document.activeElement;
  const box = overlay?.querySelector('.dirpicker-modal');
  if (box) enhanceModal(box);
  // 从工作区输入框已有值所在目录起步；否则从盘符/根起步
  const start = ($('#wb-workspace')?.value || '').trim();
  dirPickerLoad(start && start !== '.' ? start : '');
}

function closeDirPicker() {
  const overlay = $('#dirpicker-overlay');
  if (overlay) overlay.style.display = 'none';
  restoreModalFocus();
}

async function dirPickerLoad(path) {
  const list = $('#dirpicker-list');
  const pathEl = $('#dirpicker-path');
  if (list) list.innerHTML = '<div class="dirpicker-loading">加载中…</div>';
  try {
    const res = await fetch(`${API}/fs/dirs?path=${encodeURIComponent(path || '')}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    dirPicker.current = data.path || '';
    if (pathEl) pathEl.textContent = data.path || '（选择一个盘符/根目录）';
    dirPickerRender(data);
  } catch (e) {
    if (list) list.innerHTML = `<div class="dirpicker-loading">加载失败：${esc(e.message)}</div>`;
  }
}

function dirPickerRender(data) {
  const list = $('#dirpicker-list');
  if (!list) return;
  const rows = [];
  if (data.parent !== null && data.parent !== undefined) {
    rows.push(`<div class="dirpicker-item dirpicker-up" data-path="${esc(data.parent)}">📂 .. （上一级）</div>`);
  }
  (data.entries || []).forEach((e) => {
    rows.push(`<div class="dirpicker-item" data-path="${esc(e.path)}">📁 ${esc(e.name)}</div>`);
  });
  list.innerHTML = rows.length ? rows.join('') : '<div class="dirpicker-loading">（此目录下无子文件夹）</div>';
  list.querySelectorAll('.dirpicker-item').forEach((el) => {
    // 双击进入；单击仅高亮（选中当前浏览目录仍用「选此目录」）
    el.addEventListener('dblclick', () => dirPickerLoad(el.dataset.path));
    el.addEventListener('click', () => {
      list.querySelectorAll('.dirpicker-item').forEach((x) => x.classList.remove('active'));
      el.classList.add('active');
    });
  });
}

function dirPickerWireOnce() {
  if (dirPicker.wired) return;
  dirPicker.wired = true;
  $('#dirpicker-close')?.addEventListener('click', closeDirPicker);
  $('#dirpicker-overlay')?.addEventListener('click', (e) => {
    if (e.target.id === 'dirpicker-overlay') closeDirPicker();
  });
  $('#dirpicker-select')?.addEventListener('click', () => {
    // 优先取高亮的子目录，否则取当前浏览目录
    const active = $('#dirpicker-list .dirpicker-item.active:not(.dirpicker-up)');
    const chosen = active ? active.dataset.path : dirPicker.current;
    if (!chosen) { toast('error', '未选择', '请先进入或选中一个目录'); return; }
    if (dirPicker.onSelect) dirPicker.onSelect(chosen);
    closeDirPicker();
  });
}
